"""Real production CLI/supervisor with controlled real Python app children.

Only the app command is substituted: FastAPI/Uvicorn are absent locally. The
launch gate, child lifetime/registration/verification are real; Bash, Linux
proc/cgroup/flock and containers are NOT proven by these Windows tests.
"""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from astra_backend import maintenance as m
from astra_backend import maintenance_runtime as r

APP_CHILD = """
import json,os,socket,sys,time,ssl,urllib.request,http.client
from pathlib import Path
def denied(*a,**kw): raise AssertionError('controlled app network refused')
socket.socket=socket.create_connection=denied
urllib.request.urlopen=denied
http.client.HTTPConnection.connect=http.client.HTTPSConnection.connect=denied
sys.path.insert(0,sys.argv[1])
from astra_backend import maintenance_runtime as r
runtime=r.get_runtime(os.environ['ASTRA_MAINTENANCE_ROLE']);runtime.startup()
verification=runtime.begin_startup_verification()
if sys.argv[4]=='crash': os._exit(7)
blocked=[]
for kind in ('manual-order','scheduler','cache-write','ledger-write','notification','http:POST'):
 try: runtime.admit(kind)
 except r.AdmissionClosed: blocked.append(kind)
 else: raise AssertionError('business admission opened')
try: runtime.store.prepare_order(runtime.identity,'controlled:new-order','e'*64)
except r.AdmissionClosed: blocked.append('broker-send')
else: raise AssertionError('broker admission opened')
Path(sys.argv[2]).write_text(json.dumps({'identity':runtime.identity.as_dict(),'blocked':blocked,'pid':os.getpid()}))
runtime.finish_activity(verification)
while not Path(sys.argv[3]).exists(): time.sleep(.02)
"""


def load_control():
    spec = importlib.util.spec_from_file_location('_supervisor_real_control', ROOT / 'scripts/maintenance_control.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SupervisorStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='astra-supervised-child-offline-')
        self.directory = Path(self.temp.name)
        (self.directory / 'logs').mkdir()
        self.store = m.MaintenanceStore(self.directory / 'maintenance.sqlite')
        self.old = {role: m.Identity(role, role + ':old', 'a' * 40,
                                   'ghcr.io/example/astra@sha256:' + 'a' * 64) for role in m.REQUIRED_ROLES}
        for identity in self.old.values(): self.store.startup(identity)
        self.binding = m.Binding('supervisor-fixture', 'd' * 64, 'a' * 40,
                                 'ghcr.io/example/astra@sha256:' + 'a' * 64, 'b' * 40,
                                 'ghcr.io/example/astra@sha256:' + 'b' * 64, 1, self.old)
        self.store.request(self.binding, time.time() + 120, self.proof())
        self.ack(); self.store.pause(self.binding, self.proof())
        self.store.begin_shutdown(self.binding)
        self.store.begin_switch(self.binding, {i.instance_id for i in self.old.values()})
        r.persist_enable_latch(self.store.path)
        self.processes, self.commands = [], []
        self.report, self.stop = self.directory / 'application-report.json', self.directory / 'stop'
        self.control = load_control()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.stop.write_text('controlled app exit')
        for process in self.processes: process.wait(timeout=10)
        self.temp.cleanup()

    def proof(self):
        actor = self.binding.instances['backend']
        return m.RiskProof(self.binding.operation_id, self.binding.generation, self.binding.plan_sha256,
                           actor.source, actor.image, time.time(), 'DEMO', True, 0, 0, 0, 0, 0, 0)

    def ack(self):
        for identity in self.binding.instances.values(): self.store.acknowledge(self.binding, identity)

    def recovery(self):
        # Reducer input, not fabricated evidence of real Linux exits.
        instances = {role: m.Identity(role, role + ':candidate', 'b' * 40,
                                      'ghcr.io/example/astra@sha256:' + 'b' * 64) for role in m.REQUIRED_ROLES}
        for identity in instances.values(): self.store.startup(identity)
        self.binding = self.store.rebind(self.binding, instances, {i.instance_id for i in self.old.values()})
        self.ack(); self.store.begin_verify(self.binding)
        self.store.begin_recovery(self.binding)
        self.store.begin_switch(self.binding, {i.instance_id for i in instances.values()})

    def launch(self, role='backend', *, previous=False, mode='hold', binding=None):
        side = 'a' if previous else 'b'
        environment = {'ASTRA_MAINTENANCE_ENABLED': '1', 'ASTRA_MAINTENANCE_DB': str(self.store.path),
                       'ASTRA_MAINTENANCE_LEGACY_PROCESS': '', 'ASTRA_SOURCE_COMMIT': side * 40,
                       'ASTRA_IMAGE_REF': 'ghcr.io/example/astra@sha256:' + side * 64,
                       'ASTRA_MAINTENANCE_STARTUP_BINDING': json.dumps((binding or self.binding).as_dict())}
        real_popen = subprocess.Popen  # Already guarded by the isolated runner.
        def controlled(command, **kwargs):
            expected = ([sys.executable, '-m', 'astra_gateway.worker'] if role == 'gateway' else
                        [sys.executable, '-m', 'astra_backend.maintenance_runtime', 'uvicorn',
                         'astra_backend.app:app', '--host', '0.0.0.0', '--port', '8080'])
            self.assertEqual(command, expected)
            self.assertTrue(kwargs['start_new_session'])
            self.commands.append(command)
            process = real_popen([sys.executable, '-c', APP_CHILD, str(ROOT), str(self.report), str(self.stop), mode], **kwargs)
            self.processes.append(process)
            return process
        errors = io.StringIO()
        with patch.dict(os.environ, environment), patch.object(r, 'ROOT', self.directory), \
                patch.object(r.subprocess, 'Popen', side_effect=controlled), \
                redirect_stdout(io.StringIO()), redirect_stderr(errors):
            result = self.control.main(['start-paused', '--role', 'watchdog-' + role,
                                       '--database', str(self.store.path), '--pid', str(os.getpid())])
        log = self.directory / 'logs' / ('astra_' + role + '.log')
        self.last_error = errors.getvalue() + (log.read_text(errors='replace')[-3500:] if log.exists() else '')
        return result

    def assert_actual_child(self, role, side):
        result = json.loads(self.report.read_text())
        self.assertEqual(result['pid'], self.processes[-1].pid)
        self.assertEqual(result['identity']['role'], role)
        self.assertEqual(result['identity']['source'], side * 40)
        self.assertEqual(len(result['blocked']), 7)
        self.assertIsNone(self.processes[-1].poll())
        self.assertTrue(self.store.status()['fenced'])
        self.assertEqual(self.store.status()['activities'], [])
        online = [json.loads(row['identity']) for row in self.store.status()['actors'] if row['online']]
        self.assertIn(result['identity'], online)

    def test_real_cli_starts_candidate_backend_paused(self):
        self.assertEqual(self.launch(), 0, self.last_error)
        self.assert_actual_child('backend', 'b')

    def test_real_cli_starts_candidate_gateway_paused(self):
        self.assertEqual(self.launch('gateway'), 0, self.last_error)
        self.assert_actual_child('gateway', 'b')

    def test_real_cli_starts_approved_previous_source_recovery_paused(self):
        self.recovery()
        self.assertEqual(self.launch(previous=True), 0, self.last_error)
        self.assert_actual_child('backend', 'a')

    def test_stale_generation_refuses_before_process_launch(self):
        self.assertEqual(self.launch(binding=replace(self.binding, generation=self.binding.generation + 1)), 75)
        self.assertEqual(self.commands, [])
        self.assertTrue(self.store.status()['fenced'])

    def test_missing_external_binding_refuses_before_process_launch(self):
        with patch.dict(os.environ, {'ASTRA_MAINTENANCE_STARTUP_BINDING': ''}):
            identity = m.Identity('watchdog-backend', r.process_instance(), 'b' * 40,
                                  'ghcr.io/example/astra@sha256:' + 'b' * 64)
            self.store.startup(identity)
            with self.assertRaises(m.AdmissionClosed): r.start_paused_component(r.Runtime(self.store, identity))
        self.assertEqual(self.processes, [])

    def test_existing_live_child_cannot_be_restarted_under_fence(self):
        self.assertEqual(self.launch(), 0, self.last_error)
        self.assertEqual(self.launch(), 75)
        self.assertEqual(len(self.processes), 1)
        self.assertIsNone(self.processes[0].poll())

    def test_crashed_child_preserves_unresolved_launch_and_verification(self):
        self.assertEqual(self.launch(mode='crash'), 75)
        self.assertEqual(self.processes[0].poll(), 7, self.last_error)
        self.assertEqual({row['kind'] for row in self.store.status()['activities']},
                         {'paused-startup:backend', 'startup-verification'})
        self.assertEqual(self.store.status()['phase'], 'HOLD')
        self.assertEqual(self.launch(), 75)
        self.assertEqual(len(self.processes), 1)

    def test_expired_operation_refuses_before_launch(self):
        self.store.clock = lambda: time.time() + 200
        with patch.object(self.control, 'startup_store', return_value=self.store):
            self.assertEqual(self.launch(), 75)
        self.assertEqual(self.processes, [])
        self.assertTrue(self.store.status()['fenced'])


if __name__ == '__main__':
    unittest.main()
