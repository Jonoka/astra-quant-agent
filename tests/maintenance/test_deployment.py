"""Controller acceptance: actual protocol/SQLite, fake Docker/broker/clock only."""
from dataclasses import replace
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / '.github/scripts'))
from astra_backend import maintenance as m
import maintenance_deployment as d
import runtime_release_upgrade as u


class FakeUpgrade:
    require_maintenance = staticmethod(u.require)
    digest_maintenance = staticmethod(u.digest)

    def __init__(self, test):
        self.test = test
        self.op, self.root_maintenance = test.op, test.live
        self.deployment_plan_sha256 = 'd' * 64
        self.state = {'phase': 'prepared'}
        self.calls, self.phases = [], []
        self.image = test.previous_image
        self.source = test.previous_source
        self.running = True
        self.ids = {'astraquant-backend': 'old-backend', 'astraquant-gateway': 'old-gateway'}
        self.restart_policies = {name: {'Name': 'unless-stopped', 'MaximumRetryCount': 0}
                                 for name in self.ids}
        self.apply_update = True
        self.fail_update = False
        self.inspect_drift = False
        self.os_identity = True
        self.external_writer = False
        self.legacy_scheduler = False
        self.protocol_label = '1'
        self.finish_on_sleep = False
        self.should_ack = True
        self.ack_roles = set(d.ROLES)

    def deployment_plan(self): return self.test.plan
    def phase(self, value): self.state['phase'] = value; self.phases.append(value)
    def assert_maintenance_ownership(self): self.calls.append(('ownership',))

    def command_maintenance(self, *args):
        self.calls.append(args)
        if args[:3] == ('docker', 'image', 'inspect'):
            image = args[3]
            source = self.test.previous_source if image == self.test.previous_image else self.test.target_source
            return json.dumps([{'RepoDigests': [image], 'Config': {'Labels': {
                'org.opencontainers.image.revision': source, d.LABEL: self.protocol_label,
                'io.jonoka.astra.maintenance-source-sha256': self.test.protocol_hash}}}]).encode()
        if args[:2] == ('docker', 'inspect'):
            name = args[2]
            return json.dumps([{'Id': self.ids[name],
                'Config': {'Image': 'external-image' if self.inspect_drift else self.image,
                           'Labels': {'org.opencontainers.image.revision': self.source}},
                'State': {'Running': self.running, 'Status': 'running' if self.running else 'exited'},
                'HostConfig': {'RestartPolicy': self.restart_policies[name]}}]).encode()
        if args[:2] == ('docker', 'exec'):
            return json.dumps({'exact_start': self.os_identity, 'role_matches': True, 'alive': True,
                'unsupported_producers': {'verified': True, 'counts': {
                    'qq_gateway_daemon': int(self.external_writer), 'daemon_web_sync': 0,
                    'market_stream': 0, 'legacy_scheduler': int(self.legacy_scheduler)}}}).encode()
        if args[:2] == ('docker', 'update'):
            if self.fail_update: raise u.GateError('fixture_update_failure')
            if self.apply_update:
                name = next(name for name, identity in self.ids.items() if identity == args[3])
                policy, _, retries = args[2].removeprefix('--restart=').partition(':')
                self.restart_policies[name] = {'Name': policy, 'MaximumRetryCount': int(retries or 0)}
            return b''
        raise AssertionError('unexpected/future production command')

    def input_command_maintenance(self, args, data):
        self.calls.append(args)
        binding = m.Binding.from_dict(json.loads(data))
        proof = self.test.proof(binding)
        return json.dumps({'proof': proof.as_dict(), 'read_only': True,
                           'account_uid_sha256': 'e' * 64}).encode()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='astra-controller-offline-')
        self.addCleanup(self.temp.cleanup)
        self.live = Path(self.temp.name) / 'live'
        self.op = Path(self.temp.name) / 'upgrade-v8.6.1-council-fixture'
        (self.live / 'data').mkdir(parents=True)
        self.op.mkdir()
        self.now = 1000.0
        self.previous_source, self.target_source = 'a' * 40, 'b' * 40
        self.previous_image = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'a' * 64
        self.target_image = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'b' * 64
        raw = (ROOT / 'astra_backend/maintenance.py').read_bytes()
        self.protocol_hash = hashlib.sha256(raw).hexdigest()
        for label in ('previous', 'release'):
            p = self.op / ('source-' + label) / 'astra_backend/maintenance.py'
            p.parent.mkdir(parents=True)
            p.write_bytes(raw)
        self.identities = {role: m.Identity(role, 'boot:22:' + str(i + 10) + ':1234',
                            self.previous_source, self.previous_image) for i, role in enumerate(d.ROLES)}
        self.store = m.MaintenanceStore(self.live / d.DATABASE, lambda: self.now)
        (self.live / 'data/maintenance_enabled').write_text('fixture\n')
        for identity in self.identities.values(): self.store.startup(identity)
        self.initial = m.Binding('cold-fixture', 'c' * 64, self.previous_source, self.previous_image,
                                 self.target_source, self.target_image, 1, self.identities)
        self.store.request(self.initial, 1120, self.proof(self.initial))
        self.ack(self.initial); self.store.pause(self.initial, self.proof(self.initial))
        self.store.resume(self.initial, self.proof(self.initial))
        self.plan = {'schema': 2, 'previous_source': self.previous_source,
            'previous_image': self.previous_image, 'release_source': self.target_source,
            'image': self.target_image, 'maintenance': {'protocol': 1, 'mode': 'maintenance', 'budget_seconds': 120,
            'drain_seconds': 10, 'generation': 2,
            'instances': {k: v.as_dict() for k, v in self.identities.items()},
            'previous_protocol_sha256': self.protocol_hash, 'target_protocol_sha256': self.protocol_hash,
            'account_uid_sha256': 'e' * 64, 'store_relative_path': d.DATABASE}}
        self.upgrade = FakeUpgrade(self)
        self.coordinator = d.MaintenanceCoordinator(self.upgrade, m, clock=lambda: self.now, sleep=self.sleep)

    def proof(self, binding):
        actor = binding.instances['backend']
        return m.RiskProof(binding.operation_id, binding.generation, binding.plan_sha256,
                           actor.source, actor.image, self.now, 'DEMO', True, 0, 0, 0, 0, 0, 0)

    def ack(self, binding):
        for identity in binding.instances.values(): self.store.acknowledge(binding, identity)

    def sleep(self, seconds):
        self.now += seconds
        state = self.store.status()
        if state['binding'] and self.upgrade.should_ack and not state['activities']:
            binding = m.Binding.from_dict(state['binding'])
            for role in self.upgrade.ack_roles:
                self.store.acknowledge(binding, binding.instances[role])
        if self.upgrade.finish_on_sleep and state['shutdown_requested']:
            self.upgrade.running = False

    def paused(self):
        self.coordinator.request_pause()
        self.assertEqual(self.store.status()['phase'], 'PAUSED')

    def no_open(self):
        self.assertTrue(self.store.status()['fenced'])
        with self.assertRaises(m.AdmissionClosed): self.store.admit(self.identities['backend'], 'late-order')

    def test_real_pause_barrier_and_timeout_never_kill_or_stop(self):
        self.upgrade.should_ack = False
        with self.assertRaisesRegex(u.GateError, 'drain_cancelled'):
            self.coordinator.request_pause()
        self.no_open()
        self.assertFalse(any(c[:2] in [('docker', 'stop'), ('docker', 'kill'), ('docker', 'update')]
                             for c in self.upgrade.calls))

    def test_queued_work_continues_after_publication_cancel(self):
        activity = self.store.admit(self.identities['gateway'], 'queued-future')
        with self.assertRaises(u.GateError): self.coordinator.request_pause()
        self.assertEqual(self.store.status()['activities'][0]['activity_id'], activity)
        self.store.finish(activity, self.identities['gateway'])
        self.no_open()

    def test_same_request_preserves_absolute_deadline(self):
        self.paused(); deadline = self.upgrade.state['maintenance_deadline']
        self.now += 2
        self.coordinator.request_pause()
        self.assertEqual(deadline, self.upgrade.state['maintenance_deadline'])

    def test_legacy_previous_missing_protocol_refused_before_fence(self):
        (self.op / 'source-previous/astra_backend/maintenance.py').unlink()
        with self.assertRaisesRegex(u.GateError, 'legacy_or_source_protocol'):
            self.coordinator.request_pause()
        self.assertEqual(self.store.status()['phase'], 'NORMAL')

    def test_image_without_protocol_refused(self):
        self.upgrade.protocol_label = '0'
        with self.assertRaises(u.GateError): self.coordinator.request_pause()
        self.assertEqual(self.store.status()['phase'], 'NORMAL')

    def test_actual_instance_drift_is_not_a_self_report_ack(self):
        self.upgrade.os_identity = False
        with self.assertRaisesRegex(u.GateError, 'actual_process'):
            self.coordinator.request_pause()
        self.assertEqual(self.store.status()['phase'], 'NORMAL')

    def test_unowned_resident_writer_refuses_pause(self):
        self.upgrade.external_writer = True
        with self.assertRaisesRegex(u.GateError, 'resident_writer'):
            self.coordinator.request_pause()
        self.assertEqual(self.store.status()['phase'], 'NORMAL')

    def test_unregistered_legacy_scheduler_refuses_pause(self):
        self.upgrade.legacy_scheduler = True
        with self.assertRaisesRegex(u.GateError, 'resident_writer'):
            self.coordinator.request_pause()
        self.assertEqual(self.store.status()['phase'], 'NORMAL')
        self.assertFalse(any(c[:2] == ('docker', 'update') for c in self.upgrade.calls))

    def test_real_orderly_stop_has_no_signal_or_docker_stop(self):
        self.paused(); self.upgrade.finish_on_sleep = True
        self.coordinator.orderly_stop()
        self.assertEqual(self.store.status()['phase'], 'SWITCHING')
        self.assertEqual(self.coordinator.ended, {i.instance_id for i in self.identities.values()})
        self.assertFalse(any(c[:2] in [('docker', 'stop'), ('docker', 'kill')] for c in self.upgrade.calls))

    def test_shutdown_timeout_remains_hold_without_kill(self):
        self.paused()
        with self.assertRaisesRegex(u.GateError, 'shutdown_incomplete'):
            self.coordinator.orderly_stop()
        self.no_open()
        self.assertFalse(any(c[:2] in [('docker', 'stop'), ('docker', 'kill')] for c in self.upgrade.calls))

    def policies(self):
        self.upgrade.state['maintenance_restart_policies'] = {name: {
            'container_id': self.upgrade.ids[name], 'policy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0}}
            for name in self.upgrade.ids}

    def test_resume_missing_policy_never_opens_admission(self):
        self.paused()
        with self.assertRaisesRegex(u.GateError, 'supervision_missing'):
            self.coordinator.explicit_resume()
        self.no_open()

    def test_resume_inspect_drift_never_opens_admission(self):
        self.paused(); self.policies(); self.upgrade.inspect_drift = True
        with self.assertRaises(u.GateError): self.coordinator.explicit_resume()
        self.no_open()

    def test_resume_update_failure_never_opens_admission(self):
        self.paused(); self.policies(); self.upgrade.fail_update = True
        with self.assertRaises(u.GateError): self.coordinator.explicit_resume()
        self.no_open()

    def test_acknowledged_but_unapplied_policy_restore_never_resumes(self):
        self.paused(); self.policies()
        for name in self.upgrade.ids:
            self.upgrade.restart_policies[name] = {'Name': 'no', 'MaximumRetryCount': 0}
        self.upgrade.apply_update = False
        with self.assertRaisesRegex(u.GateError, 'supervision_restore'):
            self.coordinator.explicit_resume()
        self.no_open()

    def test_final_fresh_risk_failure_after_policy_restore_stays_fenced(self):
        self.paused(); self.policies()
        original = self.upgrade.input_command_maintenance
        for change in ({'positions': 1}, {'captured_at': self.now - 1000}, {'verified': False}):
            def unsafe(args, data):
                result = json.loads(original(args, data))
                result['proof'].update(change)
                return json.dumps(result).encode()
            with self.subTest(change=change), \
                    patch.object(self.upgrade, 'input_command_maintenance', side_effect=unsafe):
                with self.assertRaises(m.MaintenanceError): self.coordinator.explicit_resume()
            self.no_open()
        self.assertTrue(any(c[:2] == ('docker', 'update') for c in self.upgrade.calls))

    def test_explicit_resume_is_last_opening_action(self):
        self.paused(); self.policies()
        phases = []
        real = self.upgrade.command_maintenance
        def command(*args):
            if args[:2] == ('docker', 'update'): phases.append(self.store.status()['phase'])
            return real(*args)
        self.upgrade.command_maintenance = command
        self.coordinator.explicit_resume()
        self.assertTrue(phases and all(p == 'PAUSED' for p in phases))
        self.assertEqual(self.store.status()['phase'], 'NORMAL')

    def test_stale_resume_cannot_release_newer_operation(self):
        self.paused(); self.policies(); old = self.coordinator.binding()
        self.coordinator.explicit_resume()
        new = replace(old, operation_id='new-operation', generation=old.generation + 1)
        self.store.request(new, self.now + 100, self.proof(new))
        with self.assertRaises(u.GateError): self.coordinator.explicit_resume()
        self.no_open()

    def test_startup_overlay_is_operational_only_and_enabled(self):
        values = self.coordinator.startup_environment()
        self.assertEqual(values['ASTRA_MAINTENANCE_ENABLED'], '1')
        self.assertEqual(values['ASTRA_SOURCE_COMMIT'], self.target_source)
        self.assertEqual(values['ASTRA_IMAGE_REF'], self.target_image)
        self.assertFalse(any('KEY' in k or 'TOKEN' in k or 'PASSWORD' in k for k in values))

    def test_bad_scope_and_boolean_budget_are_refused(self):
        scope = dict(self.plan['maintenance']); scope['budget_seconds'] = True
        with self.assertRaises(u.GateError): d.validate_scope(scope, self.plan, u.require)
        scope = dict(self.plan['maintenance']); scope['instances'] = {}
        with self.assertRaises(u.GateError): d.validate_scope(scope, self.plan, u.require)

    def test_old_plan_time_gate_still_uses_real_remaining(self):
        value = u.Upgrade.__new__(u.Upgrade)
        value.deployment_plan = lambda: {'runtime': {'schedule_seconds': 900, 'minimum_idle_window_seconds': 480}}
        with patch.object(u.time, 'time', return_value=421):
            with self.assertRaisesRegex(u.GateError, 'insufficient_idle_window'): value.idle_window()
        # At exactly 480, the real snapshot check is still required, not bypassed.
        with patch.object(u.time, 'time', return_value=420), patch.object(u, 'plain_path', side_effect=u.GateError('snapshot-required')):
            with self.assertRaisesRegex(u.GateError, 'snapshot-required'): value.idle_window()

    def test_legacy_execute_and_rollback_refuse_before_any_command(self):
        value = u.Upgrade.__new__(u.Upgrade)
        value.deployment_plan = lambda: {'schema': 1}
        value.state = {'phase': 'prepared'}
        with patch.object(u, 'run', side_effect=AssertionError('must not command')):
            with self.assertRaisesRegex(u.GateError, 'legacy_execute'): value.execute()
            with self.assertRaisesRegex(u.GateError, 'legacy_rollback'): value.rollback()

    def test_scope_missing_plan_is_not_an_executable_fallback(self):
        # Read-only schema1 parsing is not executable authorization.
        value = u.Upgrade.__new__(u.Upgrade)
        value.deployment_plan = lambda: u.validate_deployment_plan({'schema': 2})
        value.state = {'phase': 'candidate-active'}
        with patch.object(u, 'run', side_effect=AssertionError('must not stop or rename')):
            with self.assertRaises(u.GateError): value.execute()
            with self.assertRaises(u.GateError): value.rollback()

    def test_missing_watchdog_ack_blocks_real_barrier(self):
        self.upgrade.ack_roles.remove('watchdog-gateway')
        with self.assertRaisesRegex(u.GateError, 'drain_cancelled'):
            self.coordinator.request_pause()
        self.no_open()
        self.assertEqual({row['role'] for row in self.store.status()['acks']},
                         self.upgrade.ack_roles)

    def test_watchdog_exact_start_drift_cannot_use_old_ack(self):
        self.paused(); self.policies()
        original = self.upgrade.command_maintenance
        def command(*args):
            raw = original(*args)
            if args[:2] == ('docker', 'exec') and 'watchdog-gateway' in args:
                data = json.loads(raw); data['exact_start'] = False
                return json.dumps(data).encode()
            return raw
        self.upgrade.command_maintenance = command
        with self.assertRaisesRegex(u.GateError, 'actual_process'):
            self.coordinator.explicit_resume()
        self.no_open()

    def test_missing_candidate_protocol_refused_before_fence(self):
        (self.op / 'source-release/astra_backend/maintenance.py').unlink()
        with self.assertRaisesRegex(u.GateError, 'legacy_or_source_protocol'):
            self.coordinator.request_pause()
        self.assertEqual(self.store.status()['phase'], 'NORMAL')
        self.assertFalse(any(c[:2] == ('docker', 'update') for c in self.upgrade.calls))

    def test_cold_bootstrap_absence_never_creates_protocol_state(self):
        database = self.live / d.DATABASE
        database.unlink()
        with self.assertRaisesRegex(u.GateError, 'not_enabled_cold_initialization'):
            d.MaintenanceCoordinator(self.upgrade, m, clock=lambda: self.now, sleep=self.sleep)
        self.assertFalse(database.exists())
        self.assertFalse(self.upgrade.calls)

    def execution_fixture(self):
        value = u.Upgrade.__new__(u.Upgrade)
        value.op, value.state = self.op, {'phase': 'prepared', 'baseline': {}, 'endpoints': {}}
        self.plan['maintenance']['mode'] = 'normal'
        self.plan['runtime'] = {'schedule_seconds': 900, 'minimum_idle_window_seconds': 480}
        value.deployment_plan = lambda: self.plan
        for name in ('check_drift', 'capacity', 'runtime', 'validate_compose', 'image_metadata', 'pool_guard'):
            setattr(value, name, Mock())
        value.endpoints = Mock(return_value={})
        value.maintenance = Mock(return_value=self.coordinator)
        value.stop, value.rollback = Mock(), Mock()
        with closing(sqlite3.connect(self.live / u.GATEWAY_DATABASE)) as db:
            db.execute('CREATE TABLE job_runs(status TEXT)')
            db.commit()
        return value

    def test_normal_execute_479_refuses_before_fence_or_stop(self):
        value = self.execution_fixture()
        with patch.object(u, 'ROOT', self.live), patch.object(u.time, 'time', return_value=421):
            with self.assertRaisesRegex(u.GateError, 'insufficient_idle_window'):
                value.execute()
        self.assertEqual(self.store.status()['phase'], 'NORMAL')
        value.stop.assert_not_called(); value.rollback.assert_not_called()
        self.assertFalse(self.upgrade.calls)

    def test_normal_execute_480_still_requires_real_four_role_pause(self):
        value = self.execution_fixture()
        self.upgrade.ack_roles.remove('watchdog-backend')
        original_iterdir = Path.iterdir
        def processes(path):
            # Fake an empty process inventory; real Linux /proc/flock remains
            # separate acceptance. SQLite and 900/480 arithmetic are real.
            return iter(()) if path == Path('/proc') else original_iterdir(path)
        with patch.object(u, 'ROOT', self.live), patch.object(u.time, 'time', return_value=420), \
                patch.object(Path, 'iterdir', processes):
            with self.assertRaisesRegex(u.GateError, 'drain_cancelled'):
                value.execute()
        self.no_open()
        value.stop.assert_not_called(); value.rollback.assert_not_called()
        self.assertEqual(value.pool_guard.call_count, 1)

    def started(self, previous=False):
        source = self.previous_source if previous else self.target_source
        image = self.previous_image if previous else self.target_image
        generation = self.coordinator.binding().generation + 1
        identities = {role: m.Identity(role, 'boot:22:' + str(100 * generation + i) + ':5678',
                                      source, image) for i, role in enumerate(d.ROLES)}
        self.upgrade.source, self.upgrade.image, self.upgrade.running = source, image, True
        self.upgrade.ids = {name: str(generation) + ':' + name for name in self.upgrade.ids}
        for identity in identities.values(): self.store.startup(identity)
        self.coordinator.rebind_started(previous)
        return self.coordinator.binding()

    def test_candidate_starts_paused_and_old_generation_cannot_resume(self):
        self.paused(); old = self.coordinator.binding()
        self.upgrade.finish_on_sleep = True
        self.coordinator.orderly_stop()
        candidate = self.started()
        self.assertEqual(self.store.status()['phase'], 'VERIFYING')
        self.assertEqual(candidate.generation, old.generation + 1)
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(old, self.proof(old))
        with self.assertRaises(m.AdmissionClosed):
            self.store.admit(candidate.instances['backend'], 'candidate-before-resume')
        self.coordinator.explicit_resume()
        activity = self.store.admit(candidate.instances['backend'], 'candidate-after-resume')
        self.store.finish(activity, candidate.instances['backend'])

    def test_rollback_keeps_latest_database_records_and_deletions_paused(self):
        original = self.op / 'original-data'
        shutil.copytree(self.live / 'data', original)
        (original / 'deleted.json').write_bytes(b'stale-seed')
        self.paused(); self.upgrade.finish_on_sleep = True
        self.coordinator.orderly_stop(); candidate = self.started()
        orders = self.live / 'data/orders.db'
        with closing(sqlite3.connect(orders)) as db:
            db.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY,status TEXT)')
            db.execute("INSERT INTO orders VALUES(2,'latest-candidate-receipt')")
            db.commit()
        (self.live / 'data/receipt.json').write_bytes(b'latest')
        self.coordinator.orderly_stop(recovery=True)
        latest_binding = self.coordinator.binding()
        recovery = self.op / 'recovery'
        (recovery / 'data').mkdir(parents=True)
        shutil.copytree(original, recovery / 'data', dirs_exist_ok=True)
        value = u.Upgrade.__new__(u.Upgrade)
        value.op, value.state = self.op, {'extras': []}
        # Windows cannot provide the ownership/rename acceptance. Here only
        # filesystem transport uses temp fixtures; the maintenance DB is real.
        value.move = lambda source, target: source.rename(target)
        value.copy = lambda source, target: shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        value.carry(self.live, recovery, 'rollback-seed')
        restored = m.MaintenanceStore(recovery / d.DATABASE, clock=lambda: self.now)
        self.assertEqual(restored.status()['binding'], latest_binding.as_dict())
        self.assertTrue(restored.status()['fenced'])
        self.assertEqual((recovery / 'data/receipt.json').read_bytes(), b'latest')
        self.assertFalse((recovery / 'data/deleted.json').exists())
        with closing(sqlite3.connect(recovery / 'data/orders.db')) as db:
            self.assertEqual(db.execute('SELECT * FROM orders').fetchall(), [(2, 'latest-candidate-receipt')])
        self.assertNotEqual(candidate.generation, self.initial.generation)
        self.live.rename(self.op / 'retained-candidate-fixture')
        recovery.rename(self.live)
        self.store = m.MaintenanceStore(self.live / d.DATABASE, clock=lambda: self.now)
        self.coordinator.store = self.store
        rollback = self.started(previous=True)
        self.assertEqual(self.store.status()['phase'], 'VERIFYING')
        with self.assertRaises(m.MaintenanceError): self.store.resume(candidate, self.proof(candidate))
        self.coordinator.explicit_resume()
        self.assertEqual(self.store.status()['binding'], rollback.as_dict())
        self.assertEqual(self.store.status()['phase'], 'NORMAL')
        self.assertEqual((self.live / 'data/receipt.json').read_bytes(), b'latest')
        self.assertFalse((self.live / 'data/deleted.json').exists())

    def test_unsupported_rollback_refuses_before_restore_with_root_missing(self):
        # A lost ROOT cannot postpone the previous/candidate support gate until
        # after a filesystem restore. Neither image inspection nor file checks
        # here run Docker; FakeUpgrade supplies the metadata.
        self.live.rename(self.op / 'retained-fixture')
        value = u.Upgrade.__new__(u.Upgrade)
        value.op = self.op
        value.deployment_plan = self.upgrade.deployment_plan
        value.command_maintenance = self.upgrade.command_maintenance
        for name in ('sources', 'stop', 'move', 'resume_recovery_renames', 'start'):
            setattr(value, name, Mock(side_effect=AssertionError('no mutation or later gate')))
        for side in ('previous', 'release'):
            path = self.op / ('source-' + side) / 'astra_backend/maintenance.py'
            raw = path.read_bytes(); path.unlink()
            try:
                with self.subTest(side=side), patch.object(u, 'ROOT', self.live):
                    with self.assertRaisesRegex(u.GateError, 'legacy_or_source_protocol'):
                        value.rollback()
            finally:
                path.write_bytes(raw)
        self.upgrade.protocol_label = '0'
        with patch.object(u, 'ROOT', self.live):
            with self.assertRaisesRegex(u.GateError, 'image_protocol_refused'): value.rollback()
        for name in ('sources', 'stop', 'move', 'resume_recovery_renames', 'start'):
            getattr(value, name).assert_not_called()
        self.assertFalse(self.live.exists())


if __name__ == '__main__': unittest.main()
