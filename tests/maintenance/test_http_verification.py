"""Actual production ASGI status + HTTPX/urllib bridge, no sockets/app imports.

This exercises the route implemented by MaintenanceMiddleware and the real
deployment status probe. It is not full FastAPI/auth/AnyIO/container acceptance.
"""
import asyncio
import selectors
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / '.github/scripts'))
from astra_backend import maintenance as m
from astra_backend import maintenance_runtime as r
import runtime_release_upgrade as u


def drive_in_memory(coroutine):
    """Real asyncio/HTTPX scheduling, with selector I/O forbidden by the harness.

    No endpoint or event/task is mocked. The socket-free loop removes only the
    OS wakeup pipe; this proves the pure route, not thread/AnyIO wakeup behavior.
    The runner's existing socket/network prohibition remains unchanged.
    """
    class NoIOSelector(selectors.SelectSelector):
        def select(self, timeout=None):
            if self.get_map(): raise AssertionError('offline ASGI attempted selector I/O')
            return []
    class SocketFreeLoop(asyncio.SelectorEventLoop):
        def _make_self_pipe(self):
            self._ssock = self._csock = None
        def _close_self_pipe(self):
            pass
        def _write_to_self(self):
            raise AssertionError('thread wakeup is outside this pure-route acceptance')
    with asyncio.Runner(loop_factory=lambda: SocketFreeLoop(selector=NoIOSelector())) as runner:
        return runner.run(coroutine)


class HttpVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='astra-http-status-offline-')
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.store = m.MaintenanceStore(Path(self.temp.name) / 'maintenance.sqlite', lambda: self.now)
        self.old = {role: m.Identity(role, role + ':old', 'a' * 40,
                                   'ghcr.io/example/astra@sha256:' + 'a' * 64) for role in m.REQUIRED_ROLES}
        for identity in self.old.values(): self.store.startup(identity)
        self.binding = m.Binding('http-fixture', 'd' * 64, 'a' * 40,
                                 'ghcr.io/example/astra@sha256:' + 'a' * 64, 'b' * 40,
                                 'ghcr.io/example/astra@sha256:' + 'b' * 64, 1, self.old)
        self.store.request(self.binding, 1120, self.proof())
        self.ack(); self.store.pause(self.binding, self.proof())
        self.runtime = r.Runtime(self.store, self.old['backend'])

    def proof(self):
        actor = self.binding.instances['backend']
        return m.RiskProof(self.binding.operation_id, self.binding.generation, self.binding.plan_sha256,
                           actor.source, actor.image, self.now, 'DEMO', True, 0, 0, 0, 0, 0, 0)

    def ack(self):
        for identity in self.binding.instances.values(): self.store.acknowledge(self.binding, identity)

    def switched(self, previous=False):
        if self.store.status()['phase'] == 'VERIFYING':
            self.store.begin_recovery(self.binding)
        else:
            self.store.begin_shutdown(self.binding)
        ended = {identity.instance_id for identity in self.binding.instances.values()}
        self.store.begin_switch(self.binding, ended)
        side = 'a' if previous else 'b'
        generation = self.binding.generation + 1
        instances = {role: m.Identity(role, role + ':new:' + str(generation), side * 40,
                                      'ghcr.io/example/astra@sha256:' + side * 64) for role in m.REQUIRED_ROLES}
        for identity in instances.values(): self.store.startup(identity)
        self.binding = self.store.rebind(self.binding, instances, ended)
        self.ack(); self.store.begin_verify(self.binding)
        self.runtime = r.Runtime(self.store, instances['backend'])

    def http(self, path, method='GET', base='http://maintenance.offline'):
        async def forbidden_application(scope, receive, send):
            self.fail('status/read refusal must not invoke broad application routes')
        application = r.MaintenanceMiddleware(forbidden_application, self.runtime)
        async def exchange():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                         base_url=base, trust_env=False) as client:
                return await client.request(method, path)
        return drive_in_memory(exchange())

    def bridge(self, request, timeout):
        # Only the transport is in-memory; no synthetic endpoint response.
        response = self.http(request.full_url, request.get_method())
        class Response(io.BytesIO):
            status = response.status_code
            headers = response.headers
        return Response(response.content)

    def probe(self):
        with patch.object(u, 'urlopen', side_effect=self.bridge):
            for base in ('http://127.0.0.1:8080', 'https://trader.jo2api.com'):
                observed = u.verify_maintenance_status(base, m)
                self.assertEqual(observed['version'], '8.6.1')
                self.assertTrue(observed['maintenance']['fenced'])

    def test_paused_get_is_actual_http_and_has_no_persistent_or_application_side_effect(self):
        before = self.store.path.read_bytes()
        names = {p.name for p in self.store.path.parent.iterdir()}
        response = self.http(m.MAINTENANCE_STATUS_PATH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['maintenance'], {'protocol': 1, 'phase': 'PAUSED', 'fenced': True})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual({p.name for p in self.store.path.parent.iterdir()}, names)
        self.assertEqual(set(response.json()), {'version', 'status', 'maintenance'})
        for secret in ('http-fixture', 'instance_id', 'orders', 'account', 'credentials', 'image', 'plan_sha256'):
            self.assertNotIn(secret, response.text)

    def test_all_business_writes_and_broad_get_stay_closed(self):
        for path, method in ((m.MAINTENANCE_STATUS_PATH, 'POST'), ('/api/v1/status', 'GET'),
                             ('/api/v1/admin/orders', 'POST'), ('/api/v1/admin/jobs/trader', 'POST')):
            with self.subTest(path=path, method=method): self.assertEqual(self.http(path, method).status_code, 503)
        with self.assertRaises(m.AdmissionClosed): self.store.prepare_order(self.runtime.identity, 'http:order', 'e' * 64)
        self.assertEqual(self.store.status()['activities'], [])

    def test_candidate_verify_and_before_explicit_resume_use_real_status_probe(self):
        self.switched(); before = self.store.path.read_bytes()
        self.probe()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.status()['phase'], 'VERIFYING')
        with self.assertRaises(m.AdmissionClosed): self.runtime.admit('http:POST')

    def test_recovery_verify_and_before_explicit_resume_use_same_real_status_probe(self):
        self.switched(); self.switched(previous=True)
        before = self.store.path.read_bytes(); self.probe()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.status()['phase'], 'VERIFYING')
        self.assertTrue(self.store.status()['fenced'])

    def test_expired_status_observes_hold_without_expiry_write_or_resume(self):
        self.switched(); self.now = 1200
        before = self.store.path.read_bytes()
        response = self.http(m.MAINTENANCE_STATUS_PATH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['maintenance']['phase'], 'HOLD')
        self.assertEqual(self.store.path.read_bytes(), before)
        with self.assertRaises(m.MaintenanceError): self.store.resume(self.binding, self.proof())

    def test_disconnected_status_instance_is_not_healthy(self):
        self.store.disconnect(self.runtime.identity)
        before = self.store.path.read_bytes()
        self.assertEqual(self.http(m.MAINTENANCE_STATUS_PATH).status_code, 503)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_missing_or_corrupt_state_is_not_created_or_exposed(self):
        self.store.path.unlink()
        self.assertEqual(self.http(m.MAINTENANCE_STATUS_PATH).status_code, 503)
        self.assertFalse(self.store.path.exists())
        self.store.path.write_bytes(b'corrupt')
        response = self.http(m.MAINTENANCE_STATUS_PATH)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.path.read_bytes(), b'corrupt')
        self.assertEqual(response.json(), {'detail': 'maintenance state unavailable'})


if __name__ == '__main__':
    unittest.main()
