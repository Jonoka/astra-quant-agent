"""Offline entrypoint integration. Windows flock is a mock, not Linux acceptance."""
import ast
import hashlib
import io
import json
from contextlib import redirect_stdout
from dataclasses import replace
import importlib.util
from pathlib import Path
import os
import sys
import tempfile
import threading
import types
import secrets
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from astra_backend import maintenance as m
from astra_backend import maintenance_runtime as r


def complete_fake_await(coroutine):
    """Immediate fake awaits only; unexpected suspension is a failed fixture."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError("fake async path attempted real I/O suspension")


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="astra-entrypoint-offline-")
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.store = m.MaintenanceStore(Path(self.temp.name) / "maintenance.sqlite", lambda: self.now)
        self.identities = {role: m.Identity(role, role + ":start", "a" * 40,
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

    def test_scheduler_counts_queued_future_before_pause_and_joins(self):
        scheduler_module = load_script("_maintenance_scheduler_test", "astra_gateway/scheduler.py")
        class Store:
            state = {}
            def get_state(self, key): return self.state.get(key, "")
            def set_state(self, key, value): self.state[key] = value
        jobs = (scheduler_module.JobSpec("first", "fake.py", 60),
                scheduler_module.JobSpec("second", "fake.py", 60))
        release, entered = threading.Event(), threading.Event()
        scheduler = scheduler_module.GatewayScheduler(Store(), max_workers=1, maintenance=self.runtime)
        def execute(*args):
            entered.set()
            release.wait(5)
        scheduler._execute = execute
        self.addCleanup(release.set)
        with patch.object(scheduler_module, "current_jobs", return_value=jobs), \
             patch.object(scheduler_module, "load_schedule", return_value={}):
            self.assertEqual(scheduler.tick(), ["first", "second"])
            self.assertTrue(entered.wait(1))
            self.assertEqual(len(self.store.status()["activities"]), 2)
            self.assertFalse(scheduler.running["second"].running())
            binding = self.fence()
            self.assertEqual(scheduler.tick(), [])
            with self.assertRaises(m.MaintenanceError):
                self.store.acknowledge(binding, self.runtime.identity)
            stopped = threading.Event()
            waiter = threading.Thread(target=lambda: (scheduler.shutdown(), stopped.set()))
            waiter.start()
            self.assertFalse(stopped.wait(.05))
            release.set()
            waiter.join(2)
            self.assertTrue(stopped.is_set())
            self.assertEqual(self.store.status()["activities"], [])

    def test_worker_keeps_mock_flock_until_actual_locked_loop_returns(self):
        tree = ast.parse((ROOT / "astra_gateway/worker.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
        events, release, entered = [], threading.Event(), threading.Event()
        class Flock:
            LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4
            def flock(self, handle, operation): events.append(operation)
        def loop():
            entered.set()
            release.wait(5)
            events.append("children-reaped")
        namespace = {"LOCK_FILE": Path(self.temp.name) / "worker.lock", "fcntl": Flock(),
                     "log": lambda _: None, "_run_locked": loop}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "worker-isolated", "exec"), namespace)
        worker = threading.Thread(target=namespace["run"])
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(events, [3])
        release.set()
        worker.join(2)
        self.assertEqual(events, [3, "children-reaped", 4])

    def test_http_get_writer_is_admitted_and_fenced(self):
        backend = r.Runtime(self.store, self.identities["backend"])
        messages = []
        async def send(message): messages.append(message)
        async def application(scope, receive, send):
            self.assertEqual(self.store.status()["activities"][0]["kind"], "http:GET")
            binding = self.fence()
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"streamed", "more_body": False})
            # Background tasks run after the final body. They still own work.
            with self.assertRaises(m.MaintenanceError):
                self.store.acknowledge(binding, backend.identity)
            self.assertEqual(r._scope.get()[1], self.store.status()["activities"][0]["activity_id"])
        middleware = r.MaintenanceMiddleware(application, backend)
        scope = {"type": "http", "path": "/api/all", "method": "GET", "headers": []}
        complete_fake_await(middleware(scope, None, send))
        self.assertEqual(self.store.status()["activities"], [])
        complete_fake_await(middleware(scope, None, send))
        self.assertEqual(messages[-2]["status"], 503)

    def test_watchdog_cli_action_is_atomic_and_unknown_action_blocks_ack(self):
        control = load_script("_maintenance_control_test", "scripts/maintenance_control.py")
        identity = self.identities["watchdog-gateway"]
        runtime = r.Runtime(self.store, identity)
        with runtime.activity("supervisory-action"):
            with self.assertRaises(m.MaintenanceError):
                self.fence()
        # An interrupted shell action remains durable and cannot fabricate ACK.
        activity = runtime.admit("supervisory-action")
        self.store.hold("fake interrupted supervisor")
        with self.assertRaises(m.MaintenanceError):
            self.store.acknowledge(self.binding, identity)
        with patch.object(control, "metadata", return_value=identity.as_dict()):
            self.assertEqual(control.main(["finish-action", "--database", str(self.store.path),
                                           "--role", identity.role, "--activity", activity]), 0)
        self.assertEqual(self.store.status()["activities"], [])
        self.assertTrue(self.store.status()["fenced"])

    def test_risk_collector_uses_verified_actual_uid_and_only_get(self):
        control = load_script("_maintenance_control_risk_test", "scripts/maintenance_control.py")
        calls = []
        class Broker:
            def readonly_evidence(self, path, params=None, **kwargs):
                calls.append(("GET", path))
                return [{"uid": "fake-account"}] if path.endswith("/config") else []
        selected = types.SimpleNamespace(mode="demo", simulated=True, configured=True, identity="fake:demo")
        with patch.dict(os.environ, {"ASTRA_VERIFIED_DEMO_ACCOUNT_UID": "fake-account"}):
            evidence = control.collect_risk(self.store, self.binding, Broker(), selected)
        m.validate_risk_proof(m.RiskProof.from_dict(evidence["proof"]), self.binding, evidence["proof"]["captured_at"])
        self.assertEqual(set(method for method, _ in calls), {"GET"})
        self.assertEqual(evidence["account_uid_sha256"], hashlib.sha256(b"fake-account").hexdigest())
        self.assertNotIn("broker_uid", evidence)
        self.assertNotIn("account_identity", evidence)

    def test_risk_collector_rejects_nan_or_unverified_identity(self):
        control = load_script("_maintenance_control_nan_test", "scripts/maintenance_control.py")
        class Broker:
            def readonly_evidence(self, path, params=None, **kwargs):
                return [{"uid": "fake-account"}] if path.endswith("/config") else [{"pos": "NaN"}]
        selected = types.SimpleNamespace(mode="demo", simulated=True, configured=True, identity="fake:demo")
        with patch.dict(os.environ, {"ASTRA_VERIFIED_DEMO_ACCOUNT_UID": "fake-account"}):
            with self.assertRaises(ValueError):
                control.collect_risk(self.store, self.binding, Broker(), selected)
        with patch.dict(os.environ, {"ASTRA_VERIFIED_DEMO_ACCOUNT_UID": ""}):
            with self.assertRaises(ValueError):
                control.collect_risk(self.store, self.binding, Broker(), selected)

    def test_real_order_submit_boundary_releases_only_known_unsent_or_rejected(self):
        from scripts import okx_rest
        tree = ast.parse((ROOT / "scripts/trader/order_submit.py").read_text(encoding="utf-8"))
        original = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "submit_protected_limit_order")
        index = next(index for index, node in enumerate(original.body)
                     if isinstance(node, ast.Try) and isinstance(node.body[0], ast.Assign)
                     and isinstance(node.body[0].value, ast.Call)
                     and getattr(node.body[0].value.func, "attr", "") == "place_order")
        arguments = ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[])
        function = ast.FunctionDef(name="submit_boundary", args=arguments,
                                  body=original.body[index:], decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        releases = []
        namespace = {"_reservation": "reservation-1", "inst_id": "BTC-USDT-SWAP", "side": "buy",
                     "size": 1, "pos_side": "long", "ord_type": "limit", "entry_px": 100,
                     "effective_tp": 120, "effective_sl": 90, "venue_ctx": None,
                     "okx_rest": okx_rest, "record_open_intent": lambda *a: None,
                     "confirm_signal_reservation": lambda *a: None,
                     "release_signal_reservation": lambda *a: releases.append(a)}
        exec(compile(module, "order-submit-boundary-isolated", "exec"), namespace)
        for error, release in ((m.AdmissionClosed("fenced"), True), (r.OrderNotSent("invalid"), True),
                               (ValueError("invalid local attachment"), True),
                               (r.BrokerRejected("broker rejected"), True),
                               (r.UnknownOrderReceipt("response lost"), False),
                               (RuntimeError("unknown duplicate refused"), False)):
            with self.subTest(error=type(error).__name__), \
                 patch.object(r, "get_runtime", return_value=self.runtime), \
                 patch.object(okx_rest, "place_order", side_effect=error):
                releases.clear()
                accepted, _ = namespace["submit_boundary"]()
                self.assertFalse(accepted)
                self.assertEqual(bool(releases), release)

    def test_worker_stopping_exits_naturally_before_any_new_tick(self):
        tree = ast.parse((ROOT / "astra_gateway/worker.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_delivery_loop")
        heartbeats = []
        namespace = {"RUNNING": True, "write_heartbeat": lambda: heartbeats.append("alive")}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "worker-stop-isolated", "exec"), namespace)
        runtime = types.SimpleNamespace(shutdown_requested=lambda: True)
        scheduler = types.SimpleNamespace(tick=lambda: self.fail("STOPPING must not schedule"))
        namespace["_delivery_loop"](None, scheduler, runtime, 0)
        self.assertEqual(heartbeats, ["alive"])

    def test_backend_stopping_uses_uvicorn_exit_flag_without_signal(self):
        binding = self.fence()
        for identity in self.identities.values():
            self.store.acknowledge(binding, identity)
        self.store.pause(binding, self.proof(binding))
        self.store.begin_shutdown(binding)
        backend = r.Runtime(self.store, self.identities["backend"])
        created = []
        class Server:
            def __init__(self, config):
                self.should_exit = False
                created.append(self)
            def run(self):
                deadline = threading.Event()
                for _ in range(10):
                    if self.should_exit:
                        return
                    deadline.wait(.05)
                raise AssertionError("server did not consume orderly shutdown")
        uvicorn = types.ModuleType("uvicorn")
        uvicorn.Server = Server
        uvicorn.Config = lambda *a, **kw: None
        with patch.dict(sys.modules, {"uvicorn": uvicorn}), \
             patch.object(r, "get_runtime", return_value=backend):
            r.run_backend("fake-application")
        self.assertTrue(created[0].should_exit)

    def test_auth_read_verification_remains_available_under_fence(self):
        backend = r.Runtime(self.store, self.identities["backend"])
        self.fence()
        async def application(scope, receive, send):
            self.assertEqual(self.store.status()["activities"][0]["kind"], "startup-verification")
        middleware = r.MaintenanceMiddleware(application, backend)
        complete_fake_await(middleware({"type": "http", "path": "/api/v1/admin/auth/me", "method": "GET"}, None, None))
        self.assertEqual(self.store.status()["activities"], [])

    def test_risk_cli_uses_fresh_binding_and_pinned_uid_without_control_writes(self):
        from scripts import okx_rest, okx_runtime
        control = load_script("_maintenance_control_readonly_cli_test", "scripts/maintenance_control.py")
        selected = okx_runtime.OKXEnvironment("demo", "fake-key", "fake-secret", "fake-passphrase")
        binding = replace(self.binding, operation_id="next-external-op", generation=2)
        before = self.store.path.read_bytes()
        output = io.StringIO()
        def evidence(path, params=None, **kwargs):
            return [{"uid": "fake-uid"}] if path.endswith("/config") else []
        with patch.dict(os.environ, {"ASTRA_MAINTENANCE_ENABLED": "1", "ASTRA_MAINTENANCE_LEGACY_PROCESS": "",
                                    "ASTRA_SOURCE_COMMIT": "a" * 40, "ASTRA_IMAGE_REF": self.identities["backend"].image}), \
             patch.object(sys, "stdin", io.StringIO(json.dumps(binding.as_dict()))), \
             patch.object(okx_rest, "readonly_evidence", side_effect=evidence), \
             patch.object(okx_runtime, "current_environment", return_value=selected), redirect_stdout(output):
            result = control.main(["risk", "--database", str(self.store.path), "--binding-stdin",
                                   "--account-uid-sha256", hashlib.sha256(b"fake-uid").hexdigest()])
        self.assertEqual(result, 0)
        collected = json.loads(output.getvalue())
        self.assertEqual(collected["proof"]["operation_id"], "next-external-op")
        self.assertEqual(collected["proof"]["generation"], 2)
        self.assertNotIn("fake-uid", output.getvalue())
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(r.enable_latch(self.store.path).exists())

    def test_qq_launch_guard_refuses_fresh_daemon_action_after_fence(self):
        tree = ast.parse((ROOT / "astra_backend/qq_bind.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "ensure_qq_gateway_daemon_running")
        namespace = {"_ensure_qq_gateway_daemon_running": lambda: self.fail("must not launch QQ daemon")}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "qq-launch-isolated", "exec"), namespace)
        self.fence()
        with patch.object(r, "get_runtime", return_value=self.runtime):
            with self.assertRaises(m.AdmissionClosed):
                namespace["ensure_qq_gateway_daemon_running"]()

    def test_qq_capture_fence_cleans_unsent_session_without_thread(self):
        tree = ast.parse((ROOT / "astra_backend/qq_bind.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "start_openid_capture")
        sessions = {}
        namespace = {"Optional": __import__("typing").Optional, "Any": object, "secrets": secrets,
                     "threading": threading, "time": time, "_CAPTURE_LOCK": threading.Lock(),
                     "_CAPTURE_SESSIONS": sessions, "_gc_tasks": lambda: None,
                     "_OpenidCaptureSession": lambda *a, **kw: types.SimpleNamespace(),
                     "_run_capture_thread": lambda _: self.fail("must not start capture")}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "qq-capture-isolated", "exec"), namespace)
        notifications = types.ModuleType("astra_backend.notifications")
        notifications._env = lambda: {}
        self.fence()
        with patch.dict(sys.modules, {"astra_backend.notifications": notifications}), \
             patch.object(r, "get_runtime", return_value=self.runtime):
            with self.assertRaises(m.AdmissionClosed):
                namespace["start_openid_capture"]("fake-app", "fake-client-secret", timeout=5)
        self.assertEqual(sessions, {})

    def test_resident_unsupported_qq_writer_prevents_runtime_ack(self):
        self.fence()
        with patch.object(r, "unsupported_producers", return_value={
            "verified": True, "counts": {"qq_gateway_daemon": 1}}):
            self.runtime.poll()
        self.assertEqual(self.store.status()["acks"], [])

    def test_cli_cannot_fabricate_application_role_ack(self):
        control = load_script("_maintenance_control_no_fake_ack_test", "scripts/maintenance_control.py")
        self.fence()
        with patch.dict(os.environ, {"ASTRA_MAINTENANCE_LEGACY_PROCESS": ""}):
            self.assertEqual(control.main(["poll", "--role", "backend", "--database", str(self.store.path)]), 75)
        self.assertEqual(self.store.status()["acks"], [])

    def test_manual_git_timeout_after_fence_drains_without_kill(self):
        import subprocess
        tree = ast.parse((ROOT / "astra_backend/routers/system.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "git")
        namespace = {"ROOT": ROOT, "subprocess": subprocess, "run_process": r.run_process}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "manual-git-isolated", "exec"), namespace)
        tests = self
        class Process:
            returncode, calls = 0, 0
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    tests.fence()
                    raise subprocess.TimeoutExpired(["git"], .2)
                tests.assertTrue(tests.store.status()["activities"])
                return "late write complete", ""
            def kill(self): tests.fail("fenced manual command must not kill")
        with patch.object(r, "get_runtime", return_value=self.runtime), \
             patch.object(r.subprocess, "Popen", return_value=Process()), \
             patch.object(r.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(RuntimeError, "timed out"), self.runtime.activity("http:POST"):
                namespace["git"](["fake-local-command"])
        self.assertEqual(self.store.status()["activities"], [])


if __name__ == "__main__":
    unittest.main()
