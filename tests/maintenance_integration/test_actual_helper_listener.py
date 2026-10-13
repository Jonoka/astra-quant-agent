"""Real urllib loopback + Upgrade.verify/resume + production ASGI status.

HTTP server is synthetic, health/auth shell routes use fixture data, Docker/OS/
account/role-ACK/file-ownership guards are explicit substitutes. No complete
FastAPI/auth/Uvicorn or physical deployment acceptance is claimed.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import gc
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import urllib.request

import anyio

from astra_backend import maintenance as m
from astra_backend import maintenance_runtime as r
from astra_backend.admin_auth import AdminAuthStore
from astra_backend.version import get_version
import maintenance_deployment as d
import runtime_release_upgrade as u


class SyntheticProofCoordinator(d.MaintenanceCoordinator):
    def check_instances(self, binding):
        # External Docker/proc/ACK evidence ONLY is synthetic. Core barriers are real.
        self.require({row['instance_id'] for row in self.store.status()['actors'] if row['online']} ==
                     {actor.instance_id for actor in binding.instances.values()}, 'synthetic_instance_binding')
        for actor in binding.instances.values(): self.store.acknowledge(binding, actor)
    def proof(self, binding):
        actor = binding.instances['backend']
        return m.RiskProof(binding.operation_id, binding.generation, binding.plan_sha256, actor.source,
                           actor.image, time.time(), 'DEMO', True, 0, 0, 0, 0, 0, 0)


class SyntheticDocker:
    require_maintenance = staticmethod(u.require)
    def __init__(self, test):
        self.test, self.op, self.root_maintenance = test, test.op, test.directory
        self.deployment_plan_sha256 = 'd' * 64
        self.state = {'phase': 'accepted-paused', 'maintenance_restart_policies': {
            name: {'container_id': 'old:' + name, 'policy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0}}
            for name in ('astraquant-backend', 'astraquant-gateway')}}
        self.policies = {name: {'Name': 'no', 'MaximumRetryCount': 0} for name in self.state['maintenance_restart_policies']}
        self.calls = []
    def deployment_plan(self): return self.test.plan
    def assert_maintenance_ownership(self): self.calls.append(('synthetic-ownership',))
    def command_maintenance(self, *args):
        self.calls.append(args)
        if args[:2] == ('docker', 'inspect'):
            name = args[2]
            return json.dumps([{'Id': name, 'Config': {'Image': self.test.target_image},
                'HostConfig': {'RestartPolicy': self.policies[name]}}]).encode()
        if args[:2] == ('docker', 'update'):
            self.policies[args[3]] = {'Name': args[2].removeprefix('--restart='), 'MaximumRetryCount': 0}
            return b''
        raise AssertionError('unapproved synthetic Docker command; never execute')


class ActualHelperListenerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='actual-helper-case-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name); (self.directory / 'data').mkdir()
        self.op = self.directory / 'upgrade-v8.6.1-council-actual-fixture'; self.op.mkdir()
        self.previous_image = 'ghcr.io/example/astra@sha256:' + 'a' * 64
        self.target_image = 'ghcr.io/example/astra@sha256:' + 'b' * 64
        self.store = m.MaintenanceStore(self.directory / d.DATABASE)
        (self.store.path.parent / 'maintenance_enabled').write_text('fixture')
        old = {role: m.Identity(role, 'boot:22:' + str(10+i) + ':1234', 'a'*40, self.previous_image)
               for i, role in enumerate(m.REQUIRED_ROLES)}
        for actor in old.values(): self.store.startup(actor)
        self.binding = m.Binding('cold-actual-fixture', 'd'*64, 'a'*40, self.previous_image, 'b'*40, self.target_image, 1, old)
        self.store.request(self.binding, time.time()+300, self.proof())
        self.ack(); self.store.pause(self.binding, self.proof()); self.store.resume(self.binding, self.proof())
        self.plan = {'previous_source': 'a'*40, 'previous_image': self.previous_image, 'release_source': 'b'*40,
                     'image': self.target_image, 'maintenance': {'protocol':1,'mode':'maintenance','budget_seconds':300,
                     'drain_seconds':100,'generation':2,'instances':{k:v.as_dict() for k,v in old.items()},
                     'previous_protocol_sha256':'f'*64,'target_protocol_sha256':'f'*64,
                     'account_uid_sha256':'e'*64,'store_relative_path':d.DATABASE}}
        self.driver = SyntheticDocker(self)
        self.coordinator = SyntheticProofCoordinator(self.driver, m)
        self.binding = self.coordinator.initial
        self.store.request(self.binding, time.time()+300, self.proof())
        self.ack(); self.store.pause(self.binding,self.proof()); self.store.begin_shutdown(self.binding)
        ended={actor.instance_id for actor in old.values()}; self.store.begin_switch(self.binding,ended)
        target={role:m.Identity(role,'boot:33:'+str(30+i)+':5678','b'*40,self.target_image) for i,role in enumerate(m.REQUIRED_ROLES)}
        for actor in target.values():self.store.startup(actor)
        self.binding=self.store.rebind(self.binding,target,ended);self.ack();self.store.begin_verify(self.binding)
        self.runtime=r.Runtime(self.store,target['backend'])
        self.auth=AdminAuthStore(self.directory/'fixture-auth.db')
        self.auth.create_user('offline_admin', 'Offline-'+secrets.token_hex(12)+'9', role='superadmin')
        self.requests=[]; self.saved={}; self.force_status_failure=False
        test=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                test.requests.append(self.path)
                messages=[]
                async def send(message):messages.append(message)
                async def receive():return {'type':'http.request','body':b'','more_body':False}
                async def shell(scope, receive, send):
                    # Actual FastAPI/auth router is NOT present: explicit shell fixture.
                    if scope['path']=='/api/v1/health':
                        payload={'version':get_version(),'status':'ok','credentials':{'okx_configured':False,'llm_configured':False,'simulated_trading':True}};status=200
                    elif scope['path']=='/api/v1/admin/auth/status':payload={'initialized':test.auth.has_users()};status=200
                    elif scope['path']=='/api/v1/admin/auth/me':payload={'detail':'synthetic unauthenticated shell'};status=401
                    else:payload={'detail':'not implemented by fixture'};status=404
                    body=json.dumps(payload).encode()
                    await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json')]})
                    await send({'type':'http.response.body','body':body})
                middleware=r.MaintenanceMiddleware(shell,test.runtime)
                async def exchange():
                    if test.force_status_failure and self.path==m.MAINTENANCE_STATUS_PATH:
                        # Disconnect exact registered actor: production route itself emits503.
                        test.store.disconnect(test.runtime.identity)
                    await middleware({'type':'http','method':'GET','path':self.path,'headers':[]},receive,send)
                anyio.run(exchange)
                start=next(message for message in messages if message['type']=='http.response.start')
                body=b''.join(message.get('body',b'') for message in messages if message['type']=='http.response.body')
                self.send_response(start['status']); self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        # Helper requests are sequential. Use one explicitly controlled listener
        # instead of introducing unregistered per-request threads whose presence
        # must invalidate the production Runtime's existing quiescence proof.
        self.server=HTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.runtime.register_threads([self.thread])
        self.thread.start();self.addCleanup(self.close_server)
        self.assertEqual(self.runtime.unknown_threads(),[])
        self.local='http://127.0.0.1:'+str(self.server.server_port)
        self.original_urlopen=urllib.request.urlopen
        self.operator=u.Upgrade.__new__(u.Upgrade)
        self.operator.op,self.operator.session=self.op,None
        self.operator.manifest_sha='f'*64
        self.operator.state={'phase':'accepted-paused','manifest_sha':'f'*64,'baseline':{},'endpoints':None}
        # Explicit physical/source/config/file-owner substitutes; real verify/resume body.
        self.operator.deployment_guard=lambda:self.driver.calls.append(('synthetic-deployment-guard',))
        self.operator.strategy_reader=lambda previous:self.driver.calls.append(('synthetic-native-reader',previous))
        self.operator.runtime=lambda previous,baseline:{'synthetic-backend':{'project':u.PROJECT,'health':'healthy'}}
        self.operator.worker_state=lambda:{'synthetic-singleton-worker':1}
        self.operator.continuity=lambda:self.driver.calls.append(('synthetic-state-continuity',))
        self.operator.log_check=lambda:{}
        self.operator.save=lambda name,value:self.saved.__setitem__(name,value)
        self.operator.phase=lambda phase:self.operator.state.__setitem__('phase',phase)
        self.operator.maintenance=lambda:self.coordinator
        with patch.object(u,'urlopen',side_effect=self.transport):self.operator.state['endpoints']=self.operator.endpoints(False)

    def close_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join(5)
        if self.thread.is_alive():raise AssertionError('synthetic listener did not stop')
        # Native auth-store context managers rely on SQLite GC for closure.
        # Collect only after every handler has ended; do not ignore file locks
        # or change production auth implementation to manufacture acceptance.
        gc.collect()
    def transport(self, request, timeout):
        # Redirect only the two fixed helper origins to OUR real loopback listener.
        target=urlsplit(request.full_url)
        if target.netloc not in ('127.0.0.1:8080','trader.jo2api.com'):raise AssertionError('unknown helper origin')
        redirected=urllib.request.Request(self.local+target.path,headers=dict(request.header_items()),method=request.get_method())
        return self.original_urlopen(redirected,timeout=timeout)
    def proof(self):
        actor=self.binding.instances['backend']
        return m.RiskProof(self.binding.operation_id,self.binding.generation,self.binding.plan_sha256,actor.source,
                          actor.image,time.time(),'DEMO',True,0,0,0,0,0,0)
    def ack(self):
        for actor in self.binding.instances.values():self.store.acknowledge(self.binding,actor)
    def test_actual_upgrade_verify_uses_loopback_and_keeps_admission_closed(self):
        with patch.object(u,'urlopen',side_effect=self.transport),patch.object(u.time,'sleep',return_value=None):
            self.operator.verify(False)
        self.assertEqual(len(self.saved['upgrade-observation.json']),7)
        self.assertGreaterEqual(self.requests.count(m.MAINTENANCE_STATUS_PATH),14)
        self.assertEqual(self.store.status()['phase'],'VERIFYING')
        with self.assertRaises(m.AdmissionClosed):self.store.admit(self.runtime.identity,'real-listener-post-verify-write')
        self.assertFalse(any(call[:2]==('docker','update') for call in self.driver.calls))
    def test_actual_upgrade_resume_opens_only_after_verify_and_synthetic_supervision(self):
        with patch.object(u,'urlopen',side_effect=self.transport),patch.object(u.time,'sleep',return_value=None):
            self.operator.resume()
        self.assertEqual(self.operator.state['phase'],'accepted')
        self.assertEqual(self.store.status()['phase'],'NORMAL')
        self.assertEqual(len(self.saved['upgrade-observation.json']),7)
        self.assertEqual(len([call for call in self.driver.calls if call[:2]==('docker','update')]),2)
    def test_production_status_failure_over_real_listener_blocks_resume(self):
        self.force_status_failure=True
        with patch.object(u,'urlopen',side_effect=self.transport),patch.object(u.time,'sleep',return_value=None):
            with self.assertRaisesRegex(u.GateError,'http_status'):self.operator.resume()
        self.assertTrue(self.store.status()['fenced'])
        self.assertFalse(any(call[:2]==('docker','update') for call in self.driver.calls))
