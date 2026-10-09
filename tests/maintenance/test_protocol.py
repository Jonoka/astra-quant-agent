"""Run directly/importlib; intentionally never import the tests package."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace

CORE_PATH = Path(__file__).resolve().parents[2] / "astra_backend" / "maintenance.py"
MODULE_NAME = "_astra_maintenance_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
m = sys.modules[MODULE_NAME]
SOURCE = "a" * 40
TARGET = "b" * 40
IMAGE = "ghcr.io/example/astra@sha256:" + "a" * 64
TARGET_IMAGE = "ghcr.io/example/astra@sha256:" + "b" * 64


class ProtocolFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="astra-maintenance-")
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.path = Path(self.temp.name) / "maintenance.sqlite"
        self.store = m.MaintenanceStore(self.path, clock=lambda: self.now)
        self.identities = {role: m.Identity(role, role + ":start-1", SOURCE, IMAGE) for role in m.REQUIRED_ROLES}
        for identity in self.identities.values():
            self.store.startup(identity)
        self.binding = m.Binding("op-1", "c" * 64, SOURCE, IMAGE, TARGET, TARGET_IMAGE, 1, self.identities)

    def proof(self, binding=None, **changes):
        binding = binding or self.binding
        identity = binding.instances["backend"]
        proof = m.RiskProof(binding.operation_id, binding.generation, binding.plan_sha256,
                            identity.source, identity.image, self.now, "DEMO", True, 0, 0, 0, 0, 0, 0)
        return replace(proof, **changes)

    def ack_all(self, binding=None):
        binding = binding or self.binding
        for identity in binding.instances.values():
            self.store.acknowledge(binding, identity)

    def request(self, binding=None):
        binding = binding or self.binding
        self.store.request(binding, self.now + 120, self.proof(binding))

    def normal(self):
        self.request()
        self.ack_all()
        self.store.pause(self.binding, self.proof())
        self.store.resume(self.binding, self.proof())

    def next_binding(self):
        return replace(self.binding, operation_id="op-2", generation=2)

    def candidate(self):
        return {role: m.Identity(role, role + ":start-2", TARGET, TARGET_IMAGE) for role in m.REQUIRED_ROLES}

    def stopped(self, binding=None):
        return {identity.instance_id for identity in (binding or self.binding).instances.values()}

    def switch(self, binding=None):
        binding = binding or self.binding
        self.store.begin_shutdown(binding)
        self.store.begin_switch(binding, self.stopped(binding))


class ProtocolTests(ProtocolFixture):
    def test_import_has_no_app_imports_or_default_store(self):
        self.assertNotIn("astra_backend.config", sys.modules)
        self.assertNotIn("dotenv", sys.modules)
        self.assertNotIn("tests", sys.modules)
        self.assertNotIn("socket", vars(m))
        self.assertEqual(self.store.status()["phase"], "HOLD")

    def test_explicit_absolute_separate_database(self):
        with self.assertRaises(m.MaintenanceError):
            m.MaintenanceStore("default.sqlite")
        other = Path(self.temp.name) / "existing.sqlite"
        with closing(sqlite3.connect(other)) as db:
            db.execute("CREATE TABLE trading(x)")
            db.commit()
        with self.assertRaises(m.MaintenanceError):
            m.MaintenanceStore(other)
        with closing(sqlite3.connect(other)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("trading",)])

    def test_identity_and_protocol_validation(self):
        for field, value in (("source", "short"), ("image", "image:latest"), ("role", "unknown"),
                             ("instance_id", ""), ("protocol", 0), ("protocol", True)):
            with self.subTest(field=field), self.assertRaises(m.MaintenanceError):
                replace(self.identities["backend"], **{field: value})

    def test_binding_requires_all_roles_and_coherent_approved_identity(self):
        for identities in ({}, {"backend": self.identities["backend"]},
                           {**self.identities, "gateway": replace(self.identities["gateway"], source=TARGET)}):
            with self.subTest(identities=identities), self.assertRaises(m.MaintenanceError):
                replace(self.binding, instances=identities)
        with self.assertRaises(TypeError):
            self.binding.instances["backend"] = self.identities["gateway"]
        self.assertEqual(m.Binding.from_dict(self.binding.as_dict()), self.binding)

    def test_fresh_verified_demo_and_all_six_zero_counts_required(self):
        changes = [{"mode": "LIVE"}, {"mode": "UNKNOWN"}, {"verified": False}, {"verified": 1},
                   {"captured_at": self.now - 31}, {"captured_at": self.now + 1}, {"captured_at": float("nan")},
                   {"generation": 2}, {"generation": True}, {"source": TARGET}, {"image": TARGET_IMAGE},
                   {"plan_sha256": "d" * 64}]
        for field in ("positions", "pending", "algo", "partial", "inflight", "unknown"):
            changes.extend([{field: value} for value in (1, None, False, "0", -1)])
        for change in changes:
            with self.subTest(change=change), self.assertRaises(m.MaintenanceError):
                self.store.request(self.binding, self.now + 120, self.proof(**change))
        self.assertTrue(self.store.status()["fenced"])

    def test_missing_risk_fields_not_zero(self):
        value = self.proof().as_dict()
        del value["algo"]
        with self.assertRaises(m.MaintenanceError):
            m.RiskProof.from_dict(value)

    def test_absolute_budget_and_duplicate_no_renewal(self):
        for deadline in (self.now, self.now - 1, self.now + 1201, float("inf"), True):
            with self.subTest(deadline=deadline), self.assertRaises(m.MaintenanceError):
                self.store.request(self.binding, deadline, self.proof())
        deadline = self.now + 120
        self.request()
        self.now += 30
        self.store.request(self.binding, deadline, self.proof())
        self.assertEqual(self.store.status()["deadline"], deadline)
        with self.assertRaises(m.MaintenanceError):
            self.store.request(self.binding, self.now + 120, self.proof())

    def test_duplicate_after_complete_or_new_operation_is_observation(self):
        deadline = self.now + 120
        self.normal()
        self.store.request(self.binding, deadline, self.proof())
        self.assertFalse(self.store.status()["fenced"])
        newer = self.next_binding()
        self.request(newer)
        self.store.request(self.binding, deadline, self.proof())
        self.assertEqual(self.store.status()["binding"]["operation_id"], "op-2")
        self.assertTrue(self.store.status()["fenced"])

    def test_queued_work_blocks_ack_and_pause_until_settled(self):
        self.normal()
        owner = self.identities["gateway"]
        activity = self.store.admit(owner, "queued-future")
        newer = self.next_binding()
        self.request(newer)
        with self.assertRaises(m.MaintenanceError):
            self.store.acknowledge(newer, owner)
        with self.assertRaises(m.AdmissionClosed):
            self.store.admit(owner, "manual-write")
        self.store.finish(activity, owner)
        self.ack_all(newer)
        self.store.pause(newer, self.proof(newer))
        self.assertEqual(self.store.status()["phase"], "PAUSED")

    def test_child_must_settle_before_parent_and_unknown_not_pruned(self):
        self.normal()
        owner = self.identities["gateway"]
        parent = self.store.admit(owner, "queued-future")
        newer = self.next_binding()
        self.request(newer)
        child = self.store.admit(owner, "child-process", parent_id=parent)
        with self.assertRaises(m.MaintenanceError):
            self.store.finish(parent, owner)
        with self.assertRaises(m.MaintenanceError):
            self.store.finish(child, self.identities["backend"])
        self.store.finish(child, owner)
        self.store.finish(parent, owner)
        with self.assertRaises(m.MaintenanceError):
            self.store.finish("unknown", owner)

    def test_supervisor_and_deadline_actions_refuse_maintenance(self):
        self.normal()
        for kind in ("supervisory-action", "normal-deadline-termination"):
            owner = self.identities["watchdog-gateway"]
            activity = self.store.admit(owner, kind)
            with self.assertRaises(m.MaintenanceError):
                self.request(self.next_binding())
            self.store.finish(activity, owner)
        self.request(self.next_binding())
        with self.assertRaises(m.AdmissionClosed):
            self.store.admit(owner, "supervisory-action")

    def test_same_generation_ack_barrier(self):
        self.request()
        self.store.acknowledge(self.binding, self.identities["backend"])
        with self.assertRaises(m.MaintenanceError):
            self.store.pause(self.binding, self.proof())
        with self.assertRaises(m.MaintenanceError):
            self.store.acknowledge(replace(self.binding, generation=2), self.identities["gateway"])
        self.ack_all()
        self.now += 31
        with self.assertRaises(m.MaintenanceError):
            self.store.pause(self.binding, self.proof())
        self.ack_all()
        self.store.pause(self.binding, self.proof())

    def test_expiry_and_cancel_never_finish_or_kill_work(self):
        self.normal()
        owner = self.identities["backend"]
        activity = self.store.admit(owner, "active-writer")
        newer = self.next_binding()
        self.request(newer)
        self.now += 121
        status = self.store.status()
        self.assertEqual(status["phase"], "HOLD")
        self.assertEqual(status["activities"][0]["activity_id"], activity)
        self.store.cancel(newer)
        self.assertTrue(self.store.status()["fenced"])
        self.store.finish(activity, owner)
        self.ack_all(newer)
        self.store.resume(newer, self.proof(newer))
        self.assertFalse(self.store.status()["fenced"])

    def test_reopen_retains_paused_and_cancelled(self):
        self.request()
        self.ack_all()
        self.store.pause(self.binding, self.proof())
        reopened = m.MaintenanceStore(self.path, clock=lambda: self.now)
        self.assertEqual(reopened.status()["phase"], "PAUSED")
        reopened.cancel(self.binding)
        self.assertEqual(m.MaintenanceStore(self.path, clock=lambda: self.now).status()["phase"], "CANCELLED")

    def test_disconnect_and_reused_start_identity_stay_hold(self):
        self.normal()
        owner = self.identities["gateway"]
        self.store.disconnect(owner)
        self.assertEqual(self.store.status()["phase"], "HOLD")
        with self.assertRaises(m.MaintenanceError):
            self.store.startup(owner)
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(self.binding, self.proof())

    def test_unknown_actor_blocks_request_and_is_retained(self):
        unknown = replace(self.identities["backend"], instance_id="backend:unknown")
        self.store.startup(unknown)
        with self.assertRaises(m.MaintenanceError):
            self.request()
        self.now += 10000
        self.assertIn(unknown.instance_id, {row["instance_id"] for row in self.store.status()["actors"]})

    def test_stale_requests_cannot_release_newer_fence(self):
        self.normal()
        newer = self.next_binding()
        self.request(newer)
        for action in (lambda: self.store.resume(self.binding, self.proof()),
                       lambda: self.store.cancel(self.binding), lambda: self.store.begin_shutdown(self.binding)):
            with self.assertRaises(m.MaintenanceError):
                action()
        self.assertTrue(self.store.status()["fenced"])

    def test_unknown_actor_cannot_be_ignored_by_resume_barrier(self):
        self.request()
        self.store.startup(replace(self.identities["backend"], instance_id="backend:unknown"))
        self.ack_all()
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(self.binding, self.proof())
        self.assertTrue(self.store.status()["fenced"])

    def test_switch_rebind_verify_explicit_resume(self):
        self.request()
        with self.assertRaises(m.MaintenanceError):
            self.store.begin_switch(self.binding, self.stopped())
        self.ack_all()
        self.store.pause(self.binding, self.proof())
        with self.assertRaises(m.MaintenanceError):
            self.store.begin_switch(self.binding, self.stopped())
        self.store.begin_shutdown(self.binding)
        self.assertEqual(self.store.status()["phase"], "STOPPING")
        self.assertTrue(self.store.status()["shutdown_requested"])
        with self.assertRaises(m.MaintenanceError):
            self.store.begin_switch(self.binding, set())
        self.store.begin_switch(self.binding, self.stopped())
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(self.binding, self.proof())
        instances = self.candidate()
        for identity in instances.values():
            self.store.startup(identity)
        ended = {i.instance_id for i in self.identities.values()}
        with self.assertRaises(m.MaintenanceError):
            self.store.rebind(self.binding, instances, set())
        candidate = self.store.rebind(self.binding, instances, ended)
        self.assertEqual(candidate.generation, 2)
        self.assertFalse(self.store.status()["shutdown_requested"])
        with self.assertRaises(m.MaintenanceError):
            self.store.begin_verify(candidate)
        self.ack_all(candidate)
        self.store.begin_verify(candidate)
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(candidate, self.proof())
        self.store.resume(candidate, self.proof(candidate))
        self.assertFalse(self.store.status()["fenced"])
        with self.assertRaises(m.MaintenanceError):
            self.store.admit(self.identities["gateway"], "old-worker")

    def test_expired_switch_cannot_resume_or_rebind(self):
        self.request()
        self.ack_all()
        self.store.pause(self.binding, self.proof())
        self.switch()
        self.now += 121
        with self.assertRaises(m.MaintenanceError):
            self.ack_all()
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(self.binding, self.proof())
        self.assertTrue(self.store.status()["fenced"])

    def test_startup_of_replacement_process_never_resumes_existing_operation(self):
        self.normal()
        new = replace(self.identities["gateway"], instance_id="gateway:new-start")
        self.store.startup(new)
        self.assertEqual(self.store.status()["phase"], "HOLD")
        self.ack_all()
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(self.binding, self.proof())
        with self.assertRaises(m.AdmissionClosed):
            self.store.admit(new, "scheduler")

    def test_fresh_risk_is_required_again_at_explicit_resume(self):
        self.request()
        self.ack_all()
        self.store.pause(self.binding, self.proof())
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(self.binding, self.proof(pending=1))
        self.now += 31
        self.ack_all()
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(self.binding, self.proof(captured_at=self.now - 31))
        self.assertTrue(self.store.status()["fenced"])

    def test_all_work_source_kinds_use_the_same_fence(self):
        self.normal()
        kinds = ("scheduler", "manual-order", "cache-write", "ledger-write", "notification", "background-writer")
        activities = [self.store.admit(self.identities["backend"], kind) for kind in kinds]
        newer = self.next_binding()
        self.request(newer)
        for kind in kinds:
            with self.subTest(kind=kind), self.assertRaises(m.AdmissionClosed):
                self.store.admit(self.identities["backend"], kind)
        self.assertEqual(len(self.store.status()["activities"]), len(kinds))
        for activity in activities:
            self.store.finish(activity, self.identities["backend"])
        self.ack_all(newer)
        self.store.pause(newer, self.proof(newer))

    def test_natural_shutdown_disconnect_requires_exact_external_end_proof(self):
        self.request()
        self.ack_all()
        self.store.pause(self.binding, self.proof())
        self.store.begin_shutdown(self.binding)
        self.store.begin_shutdown(self.binding)
        for changed in (replace(self.binding, generation=2), replace(self.binding, plan_sha256="d" * 64)):
            with self.assertRaises(m.MaintenanceError):
                self.store.begin_switch(changed, self.stopped())
        for identity in self.identities.values():
            self.store.disconnect(identity)
        self.assertEqual(self.store.status()["phase"], "HOLD")
        with self.assertRaises(m.MaintenanceError):
            self.store.begin_switch(self.binding, {self.identities["backend"].instance_id})
        self.store.begin_switch(self.binding, self.stopped())
        self.assertEqual(self.store.status()["phase"], "SWITCHING")

    def test_failed_candidate_restores_previous_paused_without_renewal(self):
        deadline = self.now + 120
        self.request()
        self.ack_all()
        self.store.pause(self.binding, self.proof())
        self.switch()
        instances = self.candidate()
        for identity in instances.values():
            self.store.startup(identity)
        candidate = self.store.rebind(self.binding, instances, self.stopped())
        self.ack_all(candidate)
        self.store.begin_verify(candidate)
        self.store.hold("candidate verification failed")
        self.ack_all(candidate)
        self.store.begin_recovery(candidate)
        self.assertTrue(self.store.status()["shutdown_requested"])
        self.store.begin_switch(candidate, self.stopped(candidate))
        restored = {role: m.Identity(role, role + ":restore-3", SOURCE, IMAGE) for role in m.REQUIRED_ROLES}
        for identity in restored.values():
            self.store.startup(identity)
        previous = self.store.rebind(candidate, restored, self.stopped(candidate))
        state = self.store.status()
        self.assertTrue(state["fenced"])
        self.assertFalse(state["shutdown_requested"])
        self.assertEqual(state["deadline"], deadline)
        self.assertEqual(previous.plan_sha256, self.binding.plan_sha256)
        self.assertEqual(previous.operation_id, self.binding.operation_id)
        self.ack_all(previous)
        self.store.begin_verify(previous)
        self.store.resume(previous, self.proof(previous))
        self.assertFalse(self.store.status()["fenced"])

    def test_expired_shutdown_and_recovery_stay_hold(self):
        self.request()
        self.ack_all()
        self.store.pause(self.binding, self.proof())
        self.store.begin_shutdown(self.binding)
        self.now += 121
        with self.assertRaises(m.MaintenanceError):
            self.store.begin_switch(self.binding, self.stopped())
        with self.assertRaises(m.MaintenanceError):
            self.store.begin_recovery(self.binding)
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(self.binding, self.proof())
        self.assertEqual(self.store.status()["phase"], "HOLD")

    def test_cancel_after_shutdown_retains_hold_and_original_deadline(self):
        deadline = self.now + 120
        self.request()
        self.ack_all()
        self.store.pause(self.binding, self.proof())
        self.store.begin_shutdown(self.binding)
        self.store.cancel(self.binding, "publication cancelled")
        state = self.store.status()
        self.assertEqual(state["phase"], "HOLD")
        self.assertEqual(state["deadline"], deadline)
        self.assertTrue(state["shutdown_requested"])
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(self.binding, self.proof())
        # Only explicit controller proof of all natural exits permits the
        # stopped-state transition needed to restore the approved previous pair.
        self.store.begin_switch(self.binding, self.stopped())
        restored = {role: m.Identity(role, role + ":cancel-restore", SOURCE, IMAGE) for role in m.REQUIRED_ROLES}
        for identity in restored.values():
            self.store.startup(identity)
        previous = self.store.rebind(self.binding, restored, self.stopped())
        self.assertTrue(self.store.status()["fenced"])
        self.assertEqual(self.store.status()["deadline"], deadline)
        self.ack_all(previous)
        self.store.begin_verify(previous)
        self.store.resume(previous, self.proof(previous))

    def test_startup_verification_is_tracked_before_import_and_blocks_ack(self):
        self.request()
        scope = self.store.admit_verification(self.identities["backend"])
        self.assertEqual(self.store.status()["activities"][0]["kind"], "startup-verification")
        with self.assertRaises(m.MaintenanceError):
            self.store.acknowledge(self.binding, self.identities["backend"])
        with self.assertRaises(m.AdmissionClosed):
            self.store.prepare_order(self.identities["backend"], "startup:send", "e" * 64, parent_id=scope)
        with self.assertRaises(m.AdmissionClosed):
            self.store.admit(self.identities["backend"], "startup:writer", parent_id=scope)
        self.store.finish(scope, self.identities["backend"])
        self.ack_all()
        self.store.pause(self.binding, self.proof())

    def test_unbound_candidate_verification_blocks_rebind_until_settled(self):
        self.request()
        self.ack_all()
        self.store.pause(self.binding, self.proof())
        self.switch()
        instances = self.candidate()
        watchdog = instances["watchdog-backend"]
        self.store.startup(watchdog)
        permit = self.store.begin_paused_startup(self.binding, watchdog)
        for role, identity in instances.items():
            if role != "watchdog-backend":
                self.store.startup(identity)
        with self.assertRaises(m.AdmissionClosed):
            self.store.admit_verification(instances["backend"])
        scope = self.store.admit_verification(instances["backend"], parent_id=permit)
        with self.assertRaises(m.MaintenanceError):
            self.store.rebind(self.binding, instances, self.stopped())
        with self.assertRaises(m.MaintenanceError):
            self.store.finish(scope, instances["backend"])
        self.store.complete_startup_verification(self.binding, instances["backend"], scope)
        with self.assertRaises(m.MaintenanceError):
            self.store.rebind(self.binding, instances, self.stopped())
        self.assertTrue(self.store.consume_startup_completion(self.binding, watchdog, permit, instances["backend"]))
        self.store.rebind(self.binding, instances, self.stopped())

    def test_startup_verification_refuses_virgin_normal_expired_or_unapproved(self):
        with self.assertRaises(m.AdmissionClosed):
            self.store.admit_verification(self.identities["backend"])
        self.normal()
        with self.assertRaises(m.AdmissionClosed):
            self.store.admit_verification(self.identities["backend"])
        self.request(self.next_binding())
        stranger = m.Identity("backend", "unapproved:process", "e" * 40, "ghcr.io/example/astra@sha256:" + "f" * 64)
        self.store.startup(stranger)
        with self.assertRaises(m.MaintenanceError):
            self.store.admit_verification(stranger)
        self.now += 121
        with self.assertRaises(m.AdmissionClosed):
            self.store.admit_verification(self.identities["backend"])

    def test_deleted_state_actions_never_recreate_blank_database(self):
        self.normal()
        parent = self.store.admit(self.identities["backend"], "writer")
        self.path.unlink()
        for action in (self.store.status,
                       lambda: self.store.admit(self.identities["backend"], "writer"),
                       lambda: self.store.finish(parent, self.identities["backend"])):
            with self.subTest(action=action), self.assertRaises(sqlite3.OperationalError):
                action()
            self.assertFalse(self.path.exists())

    def test_existing_empty_database_is_not_initialized(self):
        empty = Path(self.temp.name) / "empty.sqlite"
        with closing(sqlite3.connect(empty)):
            pass
        with self.assertRaises(m.MaintenanceError):
            m.MaintenanceStore(empty)
        with closing(sqlite3.connect(empty)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [])
    def test_unsupported_database_version_does_not_drop_existing_work(self):
        self.normal()
        activity = self.store.admit(self.identities["backend"], "writer")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE maintenance_meta SET version=999")
            db.commit()
        with self.assertRaises(m.MaintenanceError):
            m.MaintenanceStore(self.path)
        with self.assertRaises(m.MaintenanceError):
            self.store.status()
        with self.assertRaises(m.MaintenanceError):
            self.store.admit(self.identities["backend"], "writer")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT activity_id FROM maintenance_activities").fetchall(), [(activity,)])


if __name__ == "__main__":
    unittest.main()
