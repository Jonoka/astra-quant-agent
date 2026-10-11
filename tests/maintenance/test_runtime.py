"""Offline real SQLite/runtime tests; all processes and brokers are fakes."""
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
from pathlib import Path
import subprocess
import os
import sqlite3
import json
import threading
import types
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from astra_backend import maintenance as m
from astra_backend import maintenance_runtime as r


def complete_fake_await(coroutine):
    """Drive immediate fake await chains; never construct a socket-backed loop."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError("fake async path attempted real I/O suspension")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="astra-runtime-offline-")
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.store = m.MaintenanceStore(Path(self.temp.name) / "maintenance.sqlite", lambda: self.now)
        self.identities = {role: m.Identity(role, role + ":1", "a" * 40,
                                          "ghcr.io/example/astra@sha256:" + "a" * 64)
                           for role in m.REQUIRED_ROLES}
        for identity in self.identities.values():
            self.store.startup(identity)
        self.binding = m.Binding("initial", "b" * 64, "a" * 40,
                                 "ghcr.io/example/astra@sha256:" + "a" * 64,
                                 "c" * 40, "ghcr.io/example/astra@sha256:" + "c" * 64,
                                 1, self.identities)
        self.store.request(self.binding, 1100, self.proof())
        for identity in self.identities.values():
            self.store.acknowledge(self.binding, identity)
        self.store.pause(self.binding, self.proof())
        self.store.resume(self.binding, self.proof())
        self.runtime = r.Runtime(self.store, self.identities["gateway"])

    def proof(self, binding=None):
        binding = binding or self.binding
        identity = binding.instances["backend"]
        return m.RiskProof(binding.operation_id, binding.generation, binding.plan_sha256,
                           identity.source, identity.image, self.now, "DEMO", True, 0, 0, 0, 0, 0, 0)

    def fence(self):
        binding = replace(self.binding, operation_id="next", generation=2)
        self.store.request(binding, 1100, self.proof(binding))
        return binding

    def test_queued_scope_blocks_ack_before_execution(self):
        activity = self.runtime.admit("queued")
        binding = self.fence()
        with self.assertRaises(m.MaintenanceError):
            self.store.acknowledge(binding, self.runtime.identity)
        self.assertEqual(self.runtime.run_admitted(activity, lambda: 42), 42)
        self.assertTrue(self.runtime.poll())
        self.assertEqual(self.store.status()["activities"], [])

    def test_maintenance_deadline_never_kills_and_reaps_before_release(self):
        tests = self
        class Process:
            returncode = 0
            calls = 0
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    tests.fence()
                    tests.assertTrue(tests.store.status()["activities"])
                    raise subprocess.TimeoutExpired(["fake"], .2)
                tests.assertTrue(tests.store.status()["activities"])
                return "late output", ""
            def kill(self):
                tests.fail("maintenance timeout attempted kill")
        process = Process()
        with patch.object(r.subprocess, "Popen", return_value=process), \
             patch.object(r.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(subprocess.TimeoutExpired):
                r.run_process(["fake"], timeout=1, runtime=self.runtime, capture_output=True, text=True)
        self.assertEqual(process.calls, 2)
        self.assertEqual(self.store.status()["activities"], [])
        self.assertTrue(self.runtime.fenced())

    def test_normal_deadline_keeps_kill_action_until_reaped(self):
        tests = self
        class Process:
            returncode = -9
            calls = 0
            killed = False
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(["fake"], .2)
                with tests.assertRaises(m.MaintenanceError):
                    tests.fence()
                return "", ""
            def kill(self):
                self.killed = True
                kinds = {a["kind"] for a in tests.store.status()["activities"]}
                tests.assertIn("normal-deadline-termination", kinds)
        process = Process()
        with patch.object(r.subprocess, "Popen", return_value=process), \
             patch.object(r.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(subprocess.TimeoutExpired):
                r.run_process(["fake"], timeout=1, runtime=self.runtime)
        self.assertTrue(process.killed)
        self.assertEqual(self.store.status()["activities"], [])

    def test_unreaped_child_remains_unknown(self):
        class Process:
            def communicate(self, timeout=None):
                raise KeyboardInterrupt()
        with patch.object(r.subprocess, "Popen", return_value=Process()):
            with self.assertRaises(r.UnknownChild):
                r.run_process(["fake"], runtime=self.runtime)
        self.assertTrue(self.store.status()["activities"])
        self.assertTrue(self.runtime.fenced())

    def test_order_persists_before_send_and_unknown_never_retries(self):
        sent = []
        def broker(client_id):
            journal = self.store.status()["orders"][0]
            self.assertEqual(journal["status"], "PREPARED")
            self.assertEqual(journal["client_order_id"], client_id)
            sent.append(client_id)
            raise OSError("fake lost response")
        with self.assertRaises(r.UnknownOrderReceipt):
            r.order_send("logical-1", {"size": "1"}, broker, runtime=self.runtime)
        with self.assertRaises(RuntimeError):
            r.order_send("logical-1", {"size": "1"}, broker, runtime=self.runtime)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.store.status()["orders"][0]["status"], "UNKNOWN")
        self.assertTrue(self.store.status()["activities"])

    def test_new_order_denied_after_fence(self):
        self.fence()
        with self.assertRaises(m.AdmissionClosed):
            r.order_send("later", {"size": "1"}, lambda _: self.fail("broker send"), runtime=self.runtime)

    def test_partial_order_retains_scope(self):
        rows = r.order_send("partial", {"size": "2"},
                            lambda _: [{"ordId": "broker-1", "state": "partially_filled"}], runtime=self.runtime)
        self.assertTrue(rows)
        self.assertEqual(self.store.status()["orders"][0]["status"], "PARTIAL")
        self.assertTrue(self.store.status()["activities"])

    def test_child_inherits_exact_owner_not_its_pid(self):
        with self.runtime.activity("job"):
            with patch.object(r, "get_runtime", return_value=self.runtime), \
                 patch.object(r.subprocess, "Popen") as popen:
                popen.return_value.communicate.return_value = ("", "")
                popen.return_value.returncode = 0
                r.run_process(["fake"], runtime=self.runtime)
        environment = popen.call_args.kwargs["env"]
        self.assertIn(self.runtime.identity.instance_id, environment["ASTRA_MAINTENANCE_OWNER"])
        self.assertTrue(environment["ASTRA_MAINTENANCE_PARENT_ACTIVITY"])

    def test_actual_rest_order_boundary_journals_stable_client_id(self):
        from scripts import okx_rest
        from scripts.okx_runtime import OKXEnvironment
        selected = OKXEnvironment("demo", "fake-key", "fake-secret", "fake-passphrase")
        sent = []
        def broker(method, path, params, **kwargs):
            self.assertEqual(self.store.status()["orders"][0]["status"], "PREPARED")
            sent.append(params["clOrdId"])
            return [{"ordId": "fake-order", "sCode": "0"}]
        with patch.object(r, "get_runtime", return_value=self.runtime), \
             patch.object(okx_rest, "_request_once", side_effect=broker):
            rows = okx_rest.place_order("BTC-USDT-SWAP", "buy", "1", env=selected,
                                        logical_intent="operator-intent-1")
            self.assertEqual(rows[0]["ordId"], "fake-order")
            with self.assertRaises(RuntimeError):
                okx_rest.place_order("BTC-USDT-SWAP", "buy", "1", env=selected,
                                     logical_intent="operator-intent-1")
            with self.assertRaises(r.OrderNotSent):
                okx_rest.place_order("BTC-USDT-SWAP", "buy", "2", env=selected,
                                     logical_intent="operator-intent-1")
        self.assertEqual(len(sent), 1)
        self.assertRegex(sent[0], r"^m[0-9a-f]{31}$")

    def test_actual_rest_post_denied_but_readonly_get_available_under_fence(self):
        from scripts import okx_rest
        from scripts.okx_runtime import OKXEnvironment
        selected = OKXEnvironment("demo", "fake-key", "fake-secret", "fake-passphrase")
        self.fence()
        with patch.object(r, "get_runtime", return_value=self.runtime), \
             patch.object(okx_rest, "_request_once", return_value=[]) as broker:
            with self.assertRaises(m.AdmissionClosed):
                okx_rest.cancel_order("BTC-USDT-SWAP", "fake-order", env=selected)
            self.assertEqual(okx_rest.pending_orders(env=selected), [])
        self.assertEqual(broker.call_count, 1)
        self.assertEqual(broker.call_args.args[0], "GET")

    def test_ordinary_runtime_preserves_original_subprocess_and_no_store(self):
        database = Path(self.temp.name) / "never-enabled.sqlite"
        legacy = r.LegacyRuntime(database)
        with patch.dict(os.environ, {"ASTRA_MAINTENANCE_ENABLED": ""}), \
             patch.object(r.subprocess, "run", return_value=subprocess.CompletedProcess(["fake"], 0)) as run:
            result = r.run_process(["fake"], timeout=17, runtime=legacy, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        run.assert_called_once_with(["fake"], timeout=17, env=None, capture_output=True, text=True)
        self.assertFalse(database.exists())
        self.assertFalse(r.enable_latch(database).exists())

    def test_hot_enable_never_enrolls_or_acknowledges_even_after_flag_removed(self):
        database = Path(self.temp.name) / "hot.sqlite"
        with patch.dict(os.environ, {"ASTRA_MAINTENANCE_DB": str(database), "ASTRA_MAINTENANCE_ENABLED": "",
                                    "ASTRA_MAINTENANCE_LEGACY_PROCESS": ""}), \
             patch.object(r, "_runtimes", {}), patch.object(r, "_legacy_process", False):
            runtime = r.get_runtime("gateway")
            self.assertFalse(runtime.enabled)
            runtime.admit("ordinary-work")
            os.environ["ASTRA_MAINTENANCE_ENABLED"] = "1"
            with self.assertRaises(m.AdmissionClosed):
                runtime.admit("hot-new-work")
            self.assertTrue(runtime.poll())
            self.assertTrue(r.enable_latch(database).exists())
            self.assertFalse(database.exists())
            os.environ["ASTRA_MAINTENANCE_ENABLED"] = ""
            self.assertTrue(runtime.fenced())
            with self.assertRaises(m.AdmissionClosed):
                runtime.startup()

    def test_latched_deletion_refuses_restart_without_creating_database(self):
        r.persist_enable_latch(self.store.path)
        self.store.path.unlink()
        with self.assertRaises(m.AdmissionClosed):
            r.startup_store(self.store.path)
        with self.assertRaises(m.AdmissionClosed):
            self.runtime.admit("deleted-state")
        self.assertFalse(self.store.path.exists())

    def test_corrupt_or_identity_seen_state_never_falls_back(self):
        corrupt = Path(self.temp.name) / "corrupt.sqlite"
        corrupt.write_text("fake corrupt SQLite", encoding="utf-8")
        with self.assertRaises(sqlite3.Error):
            r.startup_store(corrupt)
        self.assertTrue(r.enable_latch(corrupt).exists())
        corrupt.unlink()
        with self.assertRaises(m.AdmissionClosed):
            r.startup_store(corrupt)
        self.assertFalse(corrupt.exists())

    def test_enabled_missing_identity_latches_before_app_imports(self):
        database = Path(self.temp.name) / "missing-identity.sqlite"
        with patch.dict(os.environ, {"ASTRA_MAINTENANCE_DB": str(database), "ASTRA_MAINTENANCE_ENABLED": "1",
                                    "ASTRA_MAINTENANCE_LEGACY_PROCESS": "", "ASTRA_MAINTENANCE_OWNER": "",
                                    "ASTRA_SOURCE_COMMIT": "", "ASTRA_IMAGE_REF": ""}), \
             patch.object(r, "_runtimes", {}), patch.object(r, "_legacy_process", False):
            with self.assertRaises(m.MaintenanceError):
                r.get_runtime("gateway")
        self.assertTrue(r.enable_latch(database).exists())
        self.assertTrue(database.exists())

    def test_explicit_manual_intents_distinguish_new_orders_and_block_unknown_retry(self):
        from scripts import okx_rest
        from scripts.okx_runtime import OKXEnvironment
        selected = OKXEnvironment("demo", "fake-key", "fake-secret", "fake-passphrase")
        with patch.object(r, "get_runtime", return_value=self.runtime), \
             patch.object(okx_rest, "_request_once", side_effect=OSError("fake lost response")) as broker, \
             patch.dict(os.environ, {"ASTRA_JOB_RUN_ID": "", "ASTRA_SCHEDULED_AT": "", "ASTRA_ORDER_INTENT_ID": ""}):
            with self.assertRaises(r.OrderNotSent):
                okx_rest.place_order("BTC-USDT-SWAP", "buy", "1", env=selected)
            self.assertEqual(broker.call_count, 0)
            with self.assertRaises(r.UnknownOrderReceipt):
                okx_rest.place_order("BTC-USDT-SWAP", "buy", "1", env=selected, logical_intent="manual-1")
            with self.assertRaises(RuntimeError):
                okx_rest.place_order("BTC-USDT-SWAP", "buy", "1", env=selected, logical_intent="manual-1")
            self.assertEqual(broker.call_count, 1)
            with self.assertRaises(r.UnknownOrderReceipt):
                okx_rest.place_order("BTC-USDT-SWAP", "buy", "1", env=selected, logical_intent="manual-2")
        orders = self.store.status()["orders"]
        self.assertEqual(len(orders), 2)
        self.assertNotEqual(orders[0]["client_order_id"], orders[1]["client_order_id"])

    def test_verified_rejection_closes_send_scope(self):
        def reject(_): raise r.BrokerRejected("fake broker sCode=51000")
        with self.assertRaises(r.BrokerRejected):
            r.order_send("rejected", {"size": "1"}, reject, runtime=self.runtime)
        self.assertEqual(self.store.status()["orders"][0]["status"], "REJECTED")
        self.assertEqual(self.store.status()["activities"], [])

    def test_shutdown_requires_current_exact_role_instance(self):
        binding = self.fence()
        for identity in self.identities.values():
            self.store.acknowledge(binding, identity)
        self.store.pause(binding, self.proof(binding))
        self.store.begin_shutdown(binding)
        self.assertTrue(self.runtime.shutdown_requested())
        stranger = r.Runtime(self.store, replace(self.runtime.identity, instance_id="gateway:new-start"))
        self.assertFalse(stranger.shutdown_requested())
        with self.assertRaises(m.AdmissionClosed):
            self.runtime.admit("stop-restart")

    def test_actual_broker_mixed_receipt_stays_unknown_and_cannot_retry(self):
        from scripts import okx_rest
        from scripts.okx_runtime import OKXEnvironment
        selected = OKXEnvironment("demo", "fake-key", "fake-secret", "fake-passphrase")
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({"code": "0", "data": [
                    {"algoId": "accepted-1", "sCode": "0"},
                    {"algoId": "rejected-2", "sCode": "51000"}]}).encode()
        with patch.object(r, "get_runtime", return_value=self.runtime), \
             patch.object(okx_rest, "urlopen", return_value=Response()) as network:
            with self.assertRaises(r.UnknownOrderReceipt):
                okx_rest.request("POST", "/api/v5/trade/cancel-algos",
                                 [{"algoId": "accepted-1"}, {"algoId": "rejected-2"}],
                                 env=selected, logical_intent="batch-1")
            with self.assertRaises(RuntimeError):
                okx_rest.request("POST", "/api/v5/trade/cancel-algos",
                                 [{"algoId": "accepted-1"}, {"algoId": "rejected-2"}],
                                 env=selected, logical_intent="batch-1")
        self.assertEqual(network.call_count, 1)
        self.assertEqual(self.store.status()["orders"][0]["status"], "UNKNOWN")
        self.assertTrue(self.store.status()["activities"])

    def test_readonly_evidence_missing_data_is_not_zero(self):
        from scripts import okx_rest
        from scripts.okx_runtime import OKXEnvironment
        selected = OKXEnvironment("demo", "fake-key", "fake-secret", "fake-passphrase")
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.payload).encode()
        for payload in ({"code": "0"}, {"data": []}, {"code": "0", "data": [None]}):
            with self.subTest(payload=payload), \
                 patch.object(okx_rest, "urlopen", return_value=Response(payload)):
                with self.assertRaises(RuntimeError):
                    okx_rest.readonly_evidence("/api/v5/account/positions", env=selected)
        with patch.object(okx_rest, "urlopen", return_value=Response({"code": "0", "data": []})) as network:
            self.assertEqual(okx_rest.readonly_evidence("/api/v5/account/positions", env=selected), [])
        self.assertEqual(network.call_args.args[0].get_method(), "GET")

    def test_same_manual_sender_id_with_changed_body_cannot_bypass_unknown(self):
        from scripts import okx_rest
        from scripts.okx_runtime import OKXEnvironment
        selected = OKXEnvironment("demo", "fake-key", "fake-secret", "fake-passphrase")
        token = r._request_intent.set("manual-http-request-1")
        try:
            with patch.object(r, "get_runtime", return_value=self.runtime), \
                 patch.object(okx_rest, "_request_once", side_effect=OSError("fake response lost")) as broker, \
                 patch.dict(os.environ, {"ASTRA_JOB_RUN_ID": "", "ASTRA_SCHEDULED_AT": ""}):
                with self.assertRaises(r.UnknownOrderReceipt):
                    okx_rest.place_order("BTC-USDT-SWAP", "buy", "1", env=selected)
                with self.assertRaises(r.UnknownOrderReceipt):
                    okx_rest.place_order("BTC-USDT-SWAP", "buy", "2", env=selected)
            self.assertEqual(broker.call_count, 1)
            self.assertEqual(len(self.store.status()["orders"]), 1)
        finally:
            r._request_intent.reset(token)

    def test_unknown_thread_name_cannot_fabricate_pause_ack(self):
        binding = self.fence()
        release, entered = threading.Event(), threading.Event()
        def work():
            entered.set()
            release.wait(3)
        thread = threading.Thread(target=work, name="maintenance-control")
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            with patch.object(r, "unsupported_producers", return_value={"verified": True, "counts": {}}):
                self.runtime.poll()
            self.assertIn(thread, self.runtime.unknown_threads())
            self.assertNotIn("gateway", {ack["role"] for ack in self.store.status()["acks"]})
        finally:
            release.set()
            thread.join(2)
        with patch.object(r, "unsupported_producers", return_value={"verified": True, "counts": {}}):
            self.runtime.poll()
        self.assertIn("gateway", {ack["role"] for ack in self.store.status()["acks"]})

    def test_new_background_thread_is_admitted_before_start_and_kept_until_finish(self):
        release, entered = threading.Event(), threading.Event()
        def work():
            self.assertEqual(self.store.status()["activities"][0]["kind"], "capture")
            entered.set()
            release.wait(3)
        with patch.object(r, "get_runtime", return_value=self.runtime):
            thread = r.start_thread("capture", work)
        self.assertTrue(entered.wait(1))
        binding = self.fence()
        try:
            with self.assertRaises(m.MaintenanceError):
                self.store.acknowledge(binding, self.runtime.identity)
        finally:
            release.set()
            thread.join(2)
        self.assertEqual(self.store.status()["activities"], [])

    def test_anyio_adapter_registers_only_actual_admitted_worker_objects(self):
        anyio = types.ModuleType("anyio")
        anyio.__path__ = []
        dispatch = types.ModuleType("anyio.to_thread")
        anyio.to_thread = dispatch
        workers = []
        async def original(function, *args, **kwargs):
            result = []
            thread = threading.Thread(target=lambda: result.append(function(*args)))
            workers.append(thread)
            thread.start()
            thread.join(2)
            return result[0]
        dispatch.run_sync = original
        with patch.dict(sys.modules, {"anyio": anyio, "anyio.to_thread": dispatch}), \
             patch.object(r, "_anyio_tracking_installed", False), self.runtime.activity("http:GET"):
            r.install_anyio_thread_tracking()
            self.assertEqual(complete_fake_await(dispatch.run_sync(lambda: 17)), 17)
        self.assertIn(workers[0], self.runtime._controlled_threads)
        self.assertEqual(self.store.status()["activities"], [])

    def test_abandoned_anyio_worker_keeps_parent_until_actual_writer_finish(self):
        anyio = types.ModuleType("anyio")
        anyio.__path__ = []
        dispatch = types.ModuleType("anyio.to_thread")
        anyio.to_thread = dispatch
        release, entered = threading.Event(), threading.Event()
        workers = []
        async def original(function, *args, **kwargs):
            thread = threading.Thread(target=lambda: function(*args))
            workers.append(thread)
            thread.start()
            self.assertTrue(entered.wait(1))
            raise RuntimeError("fake cancelled async waiter")
        dispatch.run_sync = original
        def writer():
            entered.set()
            release.wait(3)
        try:
            with patch.dict(sys.modules, {"anyio": anyio, "anyio.to_thread": dispatch}), \
                 patch.object(r, "_anyio_tracking_installed", False):
                r.install_anyio_thread_tracking()
                with self.assertRaises(RuntimeError), self.runtime.activity("http:GET"):
                    complete_fake_await(dispatch.run_sync(writer, abandon_on_cancel=True))
            binding = self.fence()
            self.assertEqual({row["kind"] for row in self.store.status()["activities"]}, {"http:GET", "asgi-thread"})
            with self.assertRaises(m.MaintenanceError):
                self.store.acknowledge(binding, self.runtime.identity)
        finally:
            release.set()
            for worker in workers:
                worker.join(2)
        self.assertEqual(self.store.status()["activities"], [])

    def test_incomplete_or_unidentified_batch_receipt_retains_unknown(self):
        from scripts import okx_rest
        from scripts.okx_runtime import OKXEnvironment
        selected = OKXEnvironment("demo", "fake-key", "fake-secret", "fake-passphrase")
        class Response:
            def __init__(self, rows): self.rows = rows
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({"code": "0", "data": self.rows}).encode()
        receipts = (
            [{"algoId": "one", "sCode": "0"}],
            [{"algoId": "one", "sCode": "51000"}],
            [{"algoId": "one", "sCode": "0"}, {"algoId": "two"}],
            [{"algoId": "one", "sCode": "0"}, {"sCode": "0"}],
            [{"algoId": "one", "sCode": "0"}, {"algoId": "one", "sCode": "0"}],
            [{"algoId": "one", "sCode": "0"}, {"algoId": "other", "sCode": "0"}],
        )
        for index, rows in enumerate(receipts):
            with self.subTest(rows=rows), patch.object(r, "get_runtime", return_value=self.runtime), \
                 patch.object(okx_rest, "urlopen", return_value=Response(rows)):
                with self.assertRaises(r.UnknownOrderReceipt):
                    okx_rest.request("POST", "/api/v5/trade/cancel-algos",
                                     [{"algoId": "one"}, {"algoId": "two"}], env=selected,
                                     logical_intent="incomplete-" + str(index))
        self.assertTrue(all(row["status"] == "UNKNOWN" for row in self.store.status()["orders"]))
        self.assertEqual(len(self.store.status()["activities"]), len(receipts))

    def test_entire_rejected_batch_and_global_rejection_release_journal(self):
        from scripts import okx_rest
        from scripts.okx_runtime import OKXEnvironment
        selected = OKXEnvironment("demo", "fake-key", "fake-secret", "fake-passphrase")
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.payload).encode()
        for index, payload in enumerate((
            {"code": "1", "data": [{"algoId": "one", "sCode": "51000"},
                                     {"algoId": "two", "sCode": "51000"}]},
            {"code": "51000", "data": []},
        )):
            with patch.object(r, "get_runtime", return_value=self.runtime), \
                 patch.object(okx_rest, "urlopen", return_value=Response(payload)):
                with self.assertRaises(r.BrokerRejected):
                    okx_rest.request("POST", "/api/v5/trade/cancel-algos",
                                     [{"algoId": "one"}, {"algoId": "two"}], env=selected,
                                     logical_intent="rejected-batch-" + str(index))
        self.assertEqual(self.store.status()["activities"], [])
        self.assertTrue(all(row["status"] == "REJECTED" for row in self.store.status()["orders"]))

    def test_subprocess_check_flag_preserves_standard_failure_semantics(self):
        process = types.SimpleNamespace(returncode=2, communicate=lambda **kwargs: ("output", "error"))
        with patch.object(r.subprocess, "Popen", return_value=process) as popen:
            result = r.run_process(["fake"], runtime=self.runtime, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("check", popen.call_args.kwargs)
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                r.run_process(["fake"], runtime=self.runtime, check=True)
        self.assertEqual(failure.exception.returncode, 2)
        self.assertEqual(failure.exception.output, "output")
        self.assertEqual(self.store.status()["activities"], [])

    def test_standalone_scheduler_is_inventory_blocker(self):
        command = types.SimpleNamespace(read_bytes=lambda: b"python\0-m\0astra_backend.scheduler\0")
        class Entry:
            name = "123"
            def __truediv__(self, name): return command
        proc = types.SimpleNamespace(is_dir=lambda: True, iterdir=lambda: [Entry()])
        with patch.object(r, "Path", return_value=proc):
            evidence = r.unsupported_producers()
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["counts"]["legacy_scheduler"], 1)

    def test_pure_health_anyio_worker_registered_without_writer_admission(self):
        anyio = types.ModuleType("anyio")
        anyio.__path__ = []
        dispatch = types.ModuleType("anyio.to_thread")
        anyio.to_thread = dispatch
        workers = []
        async def original(function, *args, **kwargs):
            result = []
            thread = threading.Thread(target=lambda: result.append(function(*args)))
            workers.append(thread)
            thread.start()
            thread.join(2)
            return result[0]
        dispatch.run_sync = original
        async def health(scope, receive, send):
            self.assertEqual(self.store.status()["activities"], [])
            self.assertEqual(await dispatch.run_sync(lambda: "healthy"), "healthy")
        self.fence()
        with patch.dict(sys.modules, {"anyio": anyio, "anyio.to_thread": dispatch}), \
             patch.object(r, "_anyio_tracking_installed", False):
            r.install_anyio_thread_tracking()
            complete_fake_await(r.MaintenanceMiddleware(health, self.runtime)(
                {"type": "http", "path": "/api/v1/health", "method": "GET"}, None, None))
        self.assertIn(workers[0], self.runtime._controlled_threads)
        self.assertEqual(self.store.status()["activities"], [])


class AckReconciliationTests(unittest.TestCase):
    """Fresh ACK invalidation, including deterministically ordered barriers.

    Only the stdlib maintenance core, temporary SQLite and test-owned threads
    run here. Process identity and risk/source pins are synthetic fixtures.
    """
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="astra-ack-offline-")
        self.addCleanup(self.temp.cleanup)
        self.store = m.MaintenanceStore(Path(self.temp.name) / "maintenance.sqlite", lambda: 1000.0)
        self.identities = {role: m.Identity(role, role + ":ack", "a" * 40,
                                          "ghcr.io/example/astra@sha256:" + "a" * 64)
                           for role in m.REQUIRED_ROLES}
        for identity in self.identities.values():
            self.store.startup(identity)
        self.binding = m.Binding("ack-reconciliation", "b" * 64, "a" * 40,
                                 "ghcr.io/example/astra@sha256:" + "a" * 64,
                                 "c" * 40, "ghcr.io/example/astra@sha256:" + "c" * 64,
                                 1, self.identities)
        self.store.request(self.binding, 1100.0, self.proof())
        self.runtime = r.Runtime(self.store, self.identities["gateway"])
        for identity in self.identities.values():
            self.store.acknowledge(self.binding, identity)

    def proof(self, binding=None):
        binding = binding or self.binding
        return m.RiskProof(binding.operation_id, binding.generation, binding.plan_sha256,
                           "a" * 40, "ghcr.io/example/astra@sha256:" + "a" * 64,
                           1000.0, "DEMO", True, 0, 0, 0, 0, 0, 0)

    def roles(self):
        return {row["role"] for row in self.store.status()["acks"]}

    def assert_revoked(self, phase="DRAINING"):
        self.assertEqual(self.roles(), set(m.REQUIRED_ROLES) - {"gateway"})
        self.assertEqual(self.store.status()["phase"], phase)
        with self.assertRaises(m.MaintenanceError):
            self.store.pause(self.binding, self.proof())

    def clean_poll(self):
        with patch.object(r, "unsupported_producers", return_value={"verified": True, "counts": {}}):
            return self.runtime.poll()

    def test_unknown_thread_after_ack_revokes_and_recovery_requires_fresh_poll(self):
        release = threading.Event()
        unknown = threading.Thread(target=release.wait)
        unknown.start()
        try:
            self.assertIn(unknown, self.runtime.unknown_threads())
            self.assertTrue(self.runtime.poll())
            self.assert_revoked()
        finally:
            release.set()
            unknown.join(3)
        self.assertFalse(unknown.is_alive())
        self.assert_revoked()  # Merely exiting never restores the old proof.
        self.assertTrue(self.clean_poll())
        self.store.pause(self.binding, self.proof())
        self.assertEqual(self.store.status()["phase"], "PAUSED")

    def test_nonzero_producer_inventory_revokes_fresh_ack(self):
        with patch.object(r, "unsupported_producers", return_value={"verified": True, "counts": {"writer": 1}}):
            self.assertTrue(self.runtime.poll())
        self.assert_revoked()

    def test_unverified_producer_inventory_revokes_fresh_ack(self):
        with patch.object(r, "unsupported_producers", return_value={"verified": False, "counts": {"writer": 0}}):
            self.assertTrue(self.runtime.poll())
        self.assert_revoked()

    def test_missing_limbo_lock_revokes_instead_of_skipping_poll(self):
        with patch.object(r.threading, "_active_limbo_lock", None):
            self.assertTrue(self.runtime.poll())
        self.assert_revoked()

    def test_inventory_exception_commits_revocation_before_propagating(self):
        with patch.object(r, "unsupported_producers", side_effect=RuntimeError("inventory failed")):
            with self.assertRaisesRegex(RuntimeError, "inventory failed"):
                self.runtime.poll()
        self.assert_revoked()

    def test_valueerror_probe_does_not_rollback_revocation(self):
        with patch.object(r, "unsupported_producers", side_effect=ValueError("inventory malformed")):
            self.assertTrue(self.runtime.poll())
        self.assert_revoked()

    def test_thread_inventory_exception_also_revokes(self):
        with patch.object(self.runtime, "unknown_threads", side_effect=RuntimeError("thread inventory failed")):
            with self.assertRaisesRegex(RuntimeError, "thread inventory failed"):
                self.runtime.poll()
        self.assert_revoked()

    def test_probe_interrupt_is_committed_before_it_is_reraised(self):
        with patch.object(r, "unsupported_producers", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.poll()
        self.assert_revoked()

    def test_malformed_inventory_revokes_before_keyerror(self):
        with patch.object(r, "unsupported_producers", return_value={"verified": True}):
            with self.assertRaises(KeyError):
                self.runtime.poll()
        self.assert_revoked()

    def test_new_verification_activity_revokes_until_finished_and_repolled(self):
        activity = self.store.admit_verification(self.runtime.identity)
        self.assertTrue(self.clean_poll())
        self.assert_revoked()
        self.runtime.finish_activity(activity)
        self.assert_revoked()
        self.clean_poll()
        self.store.pause(self.binding, self.proof())

    def test_reappeared_durable_activity_cannot_preserve_stale_ack(self):
        # Fault injection: a restored activity and a stale ACK coexist. Ordinary
        # production admission already removes this ACK in the same transaction.
        with self.store._transaction() as (db, state, now):
            db.execute("INSERT INTO maintenance_activities VALUES (?,?,?,?,?)",
                       ("restored-activity", json.dumps(self.runtime.identity.as_dict(), sort_keys=True,
                                                       separators=(",", ":")), "restored", None, now))
        self.assertIn("gateway", self.roles())
        self.clean_poll()
        self.assert_revoked()
        self.assertEqual(len(self.store.status()["activities"]), 1)

    def test_stale_binding_cannot_revoke_current_proof_or_run_probe(self):
        before = self.store.status()
        stale = replace(self.binding, generation=2)
        with self.assertRaises(m.MaintenanceError):
            self.store.reconcile_acknowledgement(stale, self.runtime.identity,
                                                lambda: self.fail("stale probe ran"))
        self.assertEqual(self.store.status(), before)

    def test_changed_identity_cannot_revoke_current_proof_or_run_probe(self):
        before = self.store.status()
        changed = replace(self.runtime.identity, source="d" * 40)
        with self.assertRaises(m.MaintenanceError):
            self.store.reconcile_acknowledgement(self.binding, changed,
                                                lambda: self.fail("wrong-identity probe ran"))
        self.assertEqual(self.store.status(), before)

    def test_repeated_invalid_poll_never_removes_peer_proofs(self):
        with patch.object(r, "unsupported_producers", return_value={"verified": False, "counts": {}}):
            for _ in range(3):
                self.runtime.poll()
                self.assert_revoked()

    def test_poll_observation_holds_sqlite_lock_until_revocation_before_pause(self):
        poll_start, probe_entered, release_probe = (threading.Event() for _ in range(3))
        pause_attempted, pause_done = threading.Event(), threading.Event()
        outcomes, failures = [], []
        original_connection = self.store._connection

        @contextmanager
        def traced_connection(*args, **kwargs):
            with original_connection(*args, **kwargs) as db:
                if threading.current_thread() is pauser:
                    db.set_trace_callback(lambda sql: pause_attempted.set()
                                          if sql == "BEGIN IMMEDIATE" else None)
                yield db

        def probe():
            probe_entered.set()
            if not release_probe.wait(5):
                raise AssertionError("test did not release probe")
            return {"verified": False, "counts": {}}

        def poll():
            try:
                if not poll_start.wait(5):
                    raise AssertionError("test did not start poll")
                self.runtime.poll()
            except BaseException as exc:
                failures.append(exc)

        def pause():
            try:
                if not probe_entered.wait(5):
                    raise AssertionError("probe did not start")
                try:
                    self.store.pause(self.binding, self.proof())
                except m.MaintenanceError:
                    outcomes.append("refused")
                else:
                    outcomes.append("accepted")
            except BaseException as exc:
                failures.append(exc)
            finally:
                pause_done.set()

        poller, pauser = threading.Thread(target=poll), threading.Thread(target=pause)
        self.runtime.register_threads([poller, pauser])
        with patch.object(self.store, "_connection", traced_connection), \
             patch.object(r, "unsupported_producers", side_effect=probe):
            # Start both before poll acquires the real limbo lock.
            poller.start()
            pauser.start()
            try:
                poll_start.set()
                self.assertTrue(probe_entered.wait(5))
                self.assertTrue(pause_attempted.wait(5))
                self.assertFalse(pause_done.is_set())
            finally:
                release_probe.set()
                poller.join(6)
                pauser.join(6)
        self.assertFalse(poller.is_alive())
        self.assertFalse(pauser.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(outcomes, ["refused"])
        self.assert_revoked()

    def test_pause_wins_then_failed_poll_moves_to_hold_and_blocks_shutdown(self):
        draining_snapshot = self.store.status()
        self.store.pause(self.binding, self.proof())
        with patch.object(self.store, "status", return_value=draining_snapshot), \
             patch.object(r, "unsupported_producers", return_value={"verified": False, "counts": {}}):
            self.runtime.poll()
        self.assert_revoked("HOLD")
        with self.assertRaises(m.MaintenanceError):
            self.store.begin_shutdown(self.binding)
        self.clean_poll()
        self.assertEqual(self.store.status()["phase"], "HOLD")
        self.store.resume(self.binding, self.proof())  # Explicit recovery only.
        newer = replace(self.binding, operation_id="after-recovery", generation=2)
        self.store.request(newer, 1100.0, self.proof(newer))
        with self.assertRaises(m.MaintenanceError):
            self.store.pause(newer, self.proof(newer))
        for identity in self.identities.values():
            self.store.acknowledge(newer, identity)
        self.store.pause(newer, self.proof(newer))
        self.store.resume(newer, self.proof(newer))
        self.assertEqual(self.store.status()["phase"], "NORMAL")

    def test_shutdown_wins_then_failed_poll_preserves_shutdown_in_hold(self):
        self.store.pause(self.binding, self.proof())
        self.store.begin_shutdown(self.binding)
        with patch.object(r, "unsupported_producers", return_value={"verified": False, "counts": {}}):
            self.runtime.poll()
        self.assert_revoked("HOLD")
        self.assertTrue(self.store.status()["shutdown_requested"])
        self.clean_poll()
        with self.assertRaises(m.MaintenanceError):
            self.store.resume(self.binding, self.proof())

    def test_resume_wins_against_fenced_poll_so_normal_producers_are_not_rechecked(self):
        self.store.pause(self.binding, self.proof())
        snapshot = self.store.status()
        self.store.resume(self.binding, self.proof())
        with patch.object(self.store, "status", return_value=snapshot), \
             patch.object(r, "unsupported_producers", return_value={"verified": True, "counts": {"writer": 1}}) as probe:
            self.runtime.poll()
            probe.assert_not_called()
        self.assertEqual(self.store.status()["phase"], "NORMAL")
        newer = replace(self.binding, operation_id="after-resume", generation=2)
        self.store.request(newer, 1100.0, self.proof(newer))
        self.assertEqual(self.roles(), set())
        with self.assertRaises(m.MaintenanceError):
            self.store.pause(newer, self.proof(newer))

    def test_normal_admission_revokes_proof_without_spurious_hold_from_old_poll(self):
        self.store.pause(self.binding, self.proof())
        snapshot = self.store.status()
        self.store.resume(self.binding, self.proof())
        activity = self.runtime.admit("ordinary-work")
        self.assertNotIn("gateway", self.roles())
        with patch.object(self.store, "status", return_value=snapshot), \
             patch.object(r, "unsupported_producers", return_value={"verified": True, "counts": {}}) as probe:
            self.runtime.poll()
            probe.assert_not_called()
        self.assertEqual(self.store.status()["phase"], "NORMAL")
        self.runtime.finish_activity(activity)
        self.assertNotIn("gateway", self.roles())


if __name__ == "__main__":
    unittest.main()
