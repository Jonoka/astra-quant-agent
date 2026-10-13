"""Genuine installed AnyIO tasks/threads with production maintenance adapters.

The inner ASGI streaming/background app and four actor IDs are synthetic; this
is not full FastAPI, Linux identity, broker or scheduler acceptance.
"""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import anyio
import anyio.to_thread
import httpx

from astra_backend import maintenance as m
from astra_backend import maintenance_runtime as r


class ActualAnyIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='actual-anyio-case-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = m.MaintenanceStore(self.directory / 'maintenance.sqlite')
        self.identities = {role: m.Identity(role, role + ':fixture', 'a' * 40,
                            'ghcr.io/example/astra@sha256:' + 'a' * 64) for role in m.REQUIRED_ROLES}
        for identity in self.identities.values(): self.store.startup(identity)
        self.binding = m.Binding('actual-integration', 'd' * 64, 'a' * 40,
            'ghcr.io/example/astra@sha256:' + 'a' * 64, 'b' * 40,
            'ghcr.io/example/astra@sha256:' + 'b' * 64, 1, self.identities)
        self.store.request(self.binding, time.time() + 120, self.proof())
        self.ack(); self.store.pause(self.binding, self.proof()); self.store.resume(self.binding, self.proof())
        self.runtime = r.Runtime(self.store, self.identities['backend'])
        original, flag = anyio.to_thread.run_sync, r._anyio_tracking_installed
        r._anyio_tracking_installed = False
        r.install_anyio_thread_tracking()
        self.addCleanup(setattr, anyio.to_thread, 'run_sync', original)
        self.addCleanup(setattr, r, '_anyio_tracking_installed', flag)

    def proof(self):
        actor = self.binding.instances['backend']
        return m.RiskProof(self.binding.operation_id, self.binding.generation, self.binding.plan_sha256,
            actor.source, actor.image, time.time(), 'DEMO', True, 0, 0, 0, 0, 0, 0)

    def ack(self):
        for identity in self.binding.instances.values(): self.store.acknowledge(self.binding, identity)

    def fence(self):
        self.binding = m.Binding('actual-fenced', 'e' * 64, self.binding.previous_source,
            self.binding.previous_image, self.binding.target_source, self.binding.target_image, 2, self.identities)
        self.store.request(self.binding, time.time() + 120, self.proof())

    def check_settled_ack_boundary(self):
        self.assertEqual(self.store.status()['activities'], [])
        self.assertEqual(self.runtime.unknown_threads(), [])
        self.runtime.poll()
        evidence = r.unsupported_producers()
        if evidence['verified'] and not any(evidence['counts'].values()):
            self.assertTrue(any(row['role'] == 'backend' for row in self.store.status()['acks']))
        else:
            # Windows cannot certify /proc: do not fake a Linux ACK as passed.
            self.assertFalse(any(row['role'] == 'backend' for row in self.store.status()['acks']))

    def test_real_anyio_stream_and_background_writer_keep_http_admission(self):
        async def scenario():
            streaming, continue_stream, writer_started = anyio.Event(), anyio.Event(), anyio.Event()
            release_writer = threading.Event()
            output = self.directory / 'background.txt'
            def writer():
                anyio.from_thread.run_sync(writer_started.set)
                if not release_writer.wait(10): raise AssertionError('controlled writer timeout')
                output.write_text('actual background write')
            async def application(scope, receive, send):
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
                streaming.set(); await continue_stream.wait()
                await send({'type': 'http.response.body', 'body': b'last', 'more_body': False})
                await anyio.to_thread.run_sync(writer)
            responses = []
            async def request():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=r.MaintenanceMiddleware(application, self.runtime)),
                                             base_url='http://actual.offline', trust_env=False) as client:
                    responses.append(await client.get('/synthetic-stream'))
            try:
                with anyio.fail_after(15):
                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(request)
                        await streaming.wait(); self.fence()
                        self.assertTrue(self.store.status()['activities'])
                        self.assertTrue(self.runtime.poll())
                        self.assertFalse(any(row['role'] == 'backend' for row in self.store.status()['acks']))
                        continue_stream.set(); await writer_started.wait()
                        self.assertEqual({row['kind'] for row in self.store.status()['activities']}, {'http:GET', 'asgi-thread'})
                        self.assertFalse(output.exists())
                        release_writer.set()
                self.assertEqual(responses[0].content, b'firstlast')
                self.assertEqual(output.read_text(), 'actual background write')
                self.assertEqual(self.store.status()['activities'], [])
                self.check_settled_ack_boundary()
            finally: release_writer.set(); continue_stream.set()
        anyio.run(scenario)

    def test_actual_abandoned_anyio_thread_retains_parent_until_final_write(self):
        async def scenario():
            started, request_done = anyio.Event(), anyio.Event()
            release = threading.Event(); output = self.directory / 'abandoned.txt'
            cancel = []
            def writer():
                anyio.from_thread.run_sync(started.set)
                if not release.wait(10): raise AssertionError('controlled abandoned writer timeout')
                output.write_text('late actual write')
            async def application(scope, receive, send):
                await anyio.to_thread.run_sync(writer, abandon_on_cancel=True)
            async def request():
                try:
                    with anyio.CancelScope() as scope:
                        cancel.append(scope)
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=r.MaintenanceMiddleware(application, self.runtime)),
                                                     base_url='http://actual.offline', trust_env=False) as client:
                            await client.get('/synthetic-cancel')
                finally: request_done.set()
            try:
                with anyio.fail_after(15):
                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(request)
                        await started.wait(); self.fence(); cancel[0].cancel(); await request_done.wait()
                        self.assertEqual(len(self.store.status()['activities']), 2)
                        self.assertFalse(output.exists())
                        self.assertTrue(self.runtime.poll())
                        release.set()
                        while self.store.status()['activities']: await anyio.sleep(.002)
                self.assertEqual(output.read_text(), 'late actual write')
                self.check_settled_ack_boundary()
            finally: release.set()
        anyio.run(scenario)

    def test_real_anyio_startup_child_work_prevents_durable_completion(self):
        self.fence(); self.ack(); self.store.pause(self.binding, self.proof())
        self.store.begin_shutdown(self.binding)
        self.store.begin_switch(self.binding, {identity.instance_id for identity in self.identities.values()})
        watchdog = m.Identity('watchdog-backend', 'new:watchdog', 'b' * 40, self.binding.target_image)
        child = m.Identity('backend', 'new:child', 'b' * 40, self.binding.target_image)
        self.store.startup(watchdog); permit = self.store.begin_paused_startup(self.binding, watchdog)
        self.store.startup(child); runtime = r.Runtime(self.store, child)
        verification = self.store.admit_verification(child, parent_id=permit)
        async def scenario():
            started = anyio.Event(); release = threading.Event()
            def initialize():
                anyio.from_thread.run_sync(started.set)
                if not release.wait(10): raise AssertionError('startup thread timeout')
                (self.directory / 'initialized.txt').write_text('actual initializer writer')
            async def work():
                token = r._scope.set((runtime, verification))
                try: await anyio.to_thread.run_sync(initialize)
                finally: r._scope.reset(token)
            try:
                with anyio.fail_after(15):
                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(work); await started.wait()
                        with self.assertRaises(m.MaintenanceError): self.store.complete_startup_verification(self.binding, child, verification)
                        self.assertFalse(self.store.consume_startup_completion(self.binding, watchdog, permit, child))
                        release.set()
                self.store.complete_startup_verification(self.binding, child, verification)
                self.assertTrue(self.store.consume_startup_completion(self.binding, watchdog, permit, child))
                self.assertTrue(self.store.status()['fenced'])
            finally: release.set()
        anyio.run(scenario)

    def test_actual_unknown_thread_and_network_guard_are_fail_closed(self):
        stop, started = threading.Event(), threading.Event()
        def unowned(): started.set(); stop.wait(10)
        thread = threading.Thread(target=unowned, name='AnyIO worker thread')
        thread.start(); self.addCleanup(thread.join, 10); self.addCleanup(stop.set)
        self.assertTrue(started.wait(2)); self.fence(); self.runtime.poll()
        self.assertFalse(any(row['role'] == 'backend' for row in self.store.status()['acks']))
        with socket.socket() as client:
            with self.assertRaises(AssertionError): client.connect(('203.0.113.1', 443))
            with self.assertRaises(AssertionError): client.connect(('127.0.0.1', 8080))
        with self.assertRaises(AssertionError): socket.getaddrinfo('example.com', 443)
        with self.assertRaises(AssertionError): socket.gethostbyname('example.com')
        with self.assertRaises(AssertionError): __import__('scripts.okx_rest')
        stop.set(); thread.join(2); self.check_settled_ack_boundary()
