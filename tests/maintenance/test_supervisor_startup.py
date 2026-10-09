"""Real production CLI/supervisor with controlled real Python app children.

Only the app command is substituted: FastAPI/Uvicorn are absent locally. The
launch gate, child lifetime/registration/verification are real; Bash, Linux
proc/cgroup/flock and containers are NOT proven by these Windows tests.
"""
from contextlib import redirect_stdout, redirect_stderr
from contextlib import asynccontextmanager
import ast
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
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from astra_backend import maintenance as m
from astra_backend import maintenance_runtime as r


def wait_report(path, stage, process):
    """A deterministic scheduling barrier only, never production readiness."""
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if path.exists():
            try:
                value = json.loads(path.read_text())
                if value.get('stage') == stage: return value
            except json.JSONDecodeError:
                pass
        if process.poll() is not None: raise AssertionError('controlled child exited before barrier')
        time.sleep(.005)
    raise AssertionError('controlled child scheduling barrier timed out')

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
def report(stage,**extra):
 p=Path(sys.argv[2]);tmp=p.with_suffix('.partial')
 tmp.write_text(json.dumps({'stage':stage,'identity':runtime.identity.as_dict(),'pid':os.getpid(),**extra}));tmp.replace(p)
if sys.argv[4]=='register-block':
 report('registered')
 while not Path(sys.argv[3]+'.verify').exists(): time.sleep(.005)
if sys.argv[4]=='registered-crash':
 report('registered');os._exit(9)
verification=runtime.begin_startup_verification()
if sys.argv[4]=='verify-block':
 report('verifying',verification=verification)
 while not Path(sys.argv[3]+'.complete').exists(): time.sleep(.005)
if sys.argv[4]=='verify-fail': raise RuntimeError('controlled actual initialization failed')
if sys.argv[4]=='crash': os._exit(7)
blocked=[]
for kind in ('manual-order','scheduler','cache-write','ledger-write','notification','http:POST'):
 try: runtime.admit(kind)
 except r.AdmissionClosed: blocked.append(kind)
 else: raise AssertionError('business admission opened')
try: runtime.store.prepare_order(runtime.identity,'controlled:new-order','e'*64)
except r.AdmissionClosed: blocked.append('broker-send')
else: raise AssertionError('broker admission opened')
runtime.complete_startup_verification(verification)
report('completed',blocked=blocked,verification=verification)
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
        Path(str(self.stop) + '.verify').write_text('controlled barrier release')
        Path(str(self.stop) + '.complete').write_text('controlled barrier release')
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

    def launch(self, role='backend', *, previous=False, mode='hold', binding=None, on_poll=None, before_return=None):
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
            if mode == 'register-block': wait_report(self.report, 'registered', process)
            if mode == 'fast': wait_report(self.report, 'completed', process)
            if mode == 'verify-block': wait_report(self.report, 'verifying', process)
            if mode in {'registered-crash', 'verify-fail'}: process.wait(timeout=10)
            if before_return is not None: before_return(process)
            return process
        original_supervisor = r.start_paused_component
        def supervise(runtime):
            return original_supervisor(runtime, sleep=on_poll or time.sleep)
        errors = io.StringIO()
        with patch.dict(os.environ, environment), patch.object(r, 'ROOT', self.directory), \
                patch.object(r.subprocess, 'Popen', side_effect=controlled), \
                patch.object(self.control, 'start_paused_component', side_effect=supervise), \
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

    def test_registration_gap_forces_parent_poll_before_verification(self):
        seen = []
        def parent_poll(seconds):
            if not seen:
                state = self.store.status()
                self.assertEqual([row['kind'] for row in state['activities']], ['paused-startup:backend'])
                self.assertTrue(self.processes[0].poll() is None)
                self.assertEqual(json.loads(self.report.read_text())['stage'], 'registered')
                seen.append(True)
                Path(str(self.stop) + '.verify').write_text('now allow genuine verification')
            time.sleep(.005)
        self.assertEqual(self.launch(mode='register-block', on_poll=parent_poll), 0, self.last_error)
        self.assertEqual(seen, [True], 'supervisor returned before checking a durable completion proof')
        self.assert_actual_child('backend', 'b')

    def test_fast_durable_completion_precedes_first_parent_poll(self):
        def before_parent(process):
            report = json.loads(self.report.read_text())
            state = self.store.status()
            completed = next(row for row in state['activities'] if row['kind'] == 'startup-complete')
            self.assertEqual(completed['activity_id'], report['verification'])
            self.assertEqual(json.loads(completed['owner']), report['identity'])
            self.assertTrue(any(row['activity_id'] == completed['parent_id'] and row['kind'] == 'paused-startup:backend'
                                for row in state['activities']))
            self.assertIsNone(process.poll())
        self.assertEqual(self.launch(mode='fast', before_return=before_parent), 0, self.last_error)
        self.assert_actual_child('backend', 'b')

    def test_registered_crash_keeps_permit_and_actor_without_false_success(self):
        self.assertEqual(self.launch(mode='registered-crash'), 75)
        self.assertEqual(self.processes[0].poll(), 9)
        self.assertEqual([row['kind'] for row in self.store.status()['activities']], ['paused-startup:backend'])
        self.assertTrue(any(row['online'] and json.loads(row['identity'])['role'] == 'backend'
                            for row in self.store.status()['actors']))
        self.assertEqual(self.store.status()['phase'], 'HOLD')
        self.assertEqual(self.launch(), 75)
        self.assertEqual(len(self.processes), 1)

    def test_initialization_failure_never_publishes_completion(self):
        self.assertEqual(self.launch(mode='verify-fail'), 75)
        self.assertEqual(self.processes[0].poll(), 1)
        self.assertEqual({row['kind'] for row in self.store.status()['activities']},
                         {'paused-startup:backend', 'startup-verification'})
        self.assertEqual(self.store.status()['phase'], 'HOLD')

    def test_wrong_binding_child_and_generic_finish_cannot_complete_verification(self):
        checked = []
        def parent_poll(seconds):
            if not checked:
                report = json.loads(self.report.read_text())
                child = m.Identity.from_dict(report['identity'])
                state = self.store.status()
                permit = next(row for row in state['activities'] if row['kind'] == 'paused-startup:backend')
                watchdog = m.Identity.from_dict(json.loads(permit['owner']))
                for changed in (replace(self.binding, generation=self.binding.generation + 1),
                                replace(self.binding, operation_id='wrong-operation'),
                                replace(self.binding, plan_sha256='e' * 64)):
                    with self.assertRaises(m.MaintenanceError):
                        self.store.complete_startup_verification(changed, child, report['verification'])
                with self.assertRaises(m.MaintenanceError):
                    self.store.complete_startup_verification(self.binding, replace(child, instance_id='wrong:child'), report['verification'])
                with self.assertRaises(m.MaintenanceError): self.store.finish(report['verification'], child)
                with self.assertRaises(m.MaintenanceError): self.store.finish(permit['activity_id'], watchdog)
                self.assertFalse(self.store.consume_startup_completion(self.binding, watchdog, permit['activity_id'], child))
                self.assertEqual(self.store.status()['activities'], state['activities'])
                checked.append(True)
                Path(str(self.stop) + '.complete').write_text('real child may now successfully finish')
            time.sleep(.005)
        self.assertEqual(self.launch(mode='verify-block', on_poll=parent_poll), 0, self.last_error)
        self.assertEqual(checked, [True])

    def test_completion_replay_stale_consume_and_other_child_proof_are_refused(self):
        saved = {}
        def before_parent(process):
            report = json.loads(self.report.read_text())
            child = m.Identity.from_dict(report['identity'])
            state = self.store.status()
            permit = next(row for row in state['activities'] if row['kind'] == 'paused-startup:backend')
            watchdog = m.Identity.from_dict(json.loads(permit['owner']))
            with self.assertRaises(m.MaintenanceError):
                self.store.complete_startup_verification(self.binding, child, report['verification'])
            with self.assertRaises(m.AdmissionClosed):
                self.store.admit(child, 'late-background-write', parent_id=report['verification'])
            with self.assertRaises(m.AdmissionClosed):
                self.store.admit_verification(child, parent_id=report['verification'])
            with self.assertRaises(m.MaintenanceError):
                self.store.consume_startup_completion(replace(self.binding, generation=2), watchdog, permit['activity_id'], child)
            with self.assertRaises(m.MaintenanceError):
                self.store.consume_startup_completion(self.binding, watchdog, permit['activity_id'], replace(child, instance_id='other:child'))
            self.assertEqual(self.store.status()['activities'], state['activities'])
            saved.update(child=child, watchdog=watchdog, permit=permit['activity_id'], verification=report['verification'])
        self.assertEqual(self.launch(mode='fast', before_return=before_parent), 0, self.last_error)
        with self.assertRaises(m.MaintenanceError):
            self.store.consume_startup_completion(self.binding, saved['watchdog'], saved['permit'], saved['child'])
        with self.assertRaises(m.MaintenanceError):
            self.store.complete_startup_verification(self.binding, saved['child'], saved['verification'])

    def test_deadline_expiry_retains_even_a_completed_unconsumed_handshake(self):
        def before_parent(process):
            self.assertTrue(any(row['kind'] == 'startup-complete' for row in self.store.status()['activities']))
            self.store.clock = lambda: time.time() + 200
        with patch.object(self.control, 'startup_store', return_value=self.store):
            self.assertEqual(self.launch(mode='fast', before_return=before_parent), 75)
        self.assertEqual({row['kind'] for row in self.store.status()['activities']}, {'paused-startup:backend', 'startup-complete'})
        self.assertTrue(self.store.status()['fenced'])

    def test_disconnected_child_completion_cannot_be_consumed(self):
        def before_parent(process):
            report = json.loads(self.report.read_text())
            self.store.disconnect(m.Identity.from_dict(report['identity']))
        with patch.object(self.control, 'startup_store', return_value=self.store):
            # Deadline is shortened in the fixture, not renewed by the parent.
            original = r.start_paused_component
            def supervisor(runtime, **kwargs):
                def after_poll(seconds): self.store.clock = lambda: time.time() + 200
                return original(runtime, sleep=after_poll)
            with patch.object(r, 'start_paused_component', side_effect=supervisor):
                self.assertEqual(self.launch(mode='fast', before_return=before_parent), 75)
        self.assertTrue(any(row['kind'] == 'startup-complete' for row in self.store.status()['activities']))
        self.assertTrue(self.store.status()['fenced'])

    def initializer(self, role, begin=True):
        """Real reducer/runtime; identities are fixtures, NOT OS exit evidence."""
        watchdog = m.Identity('watchdog-' + role, 'initializer:watchdog:' + role, 'b' * 40,
                              'ghcr.io/example/astra@sha256:' + 'b' * 64)
        child = m.Identity(role, 'initializer:child:' + role, 'b' * 40,
                           'ghcr.io/example/astra@sha256:' + 'b' * 64)
        self.store.startup(watchdog)
        permit = self.store.begin_paused_startup(self.binding, watchdog)
        self.store.startup(child)
        runtime = r.Runtime(self.store, child)
        verification = self.store.admit_verification(child, parent_id=permit) if begin else None
        return runtime, watchdog, permit, verification

    @staticmethod
    def immediate(coroutine):
        # The extracted lifespan has immediate fake dependencies; no I/O loop.
        try:
            coroutine.send(None)
        except StopIteration as completed:
            return completed.value
        finally:
            coroutine.close()
        raise AssertionError('isolated initializer unexpectedly suspended')

    def backend_lifespan(self, runtime, verification, refresh):
        tree = ast.parse((ROOT / 'astra_backend/app.py').read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'lifespan')
        namespace = {'FastAPI': object, 'asynccontextmanager': asynccontextmanager,
                     'MAINTENANCE': runtime, '_STARTUP_VERIFICATION': verification, '_PAUSED_STARTUP': True,
                     'AdmissionClosed': m.AdmissionClosed, 'refresh_settings': refresh, 'os': os,
                     'settings': types.SimpleNamespace(admin_token='', setup_token=''),
                     'admin_auth': types.SimpleNamespace(initialize_from_legacy=lambda *_: self.fail('business init remains fenced')),
                     'start_gateway_supervisor': lambda: self.fail('standalone backend cannot start another worker'),
                     'stop_gateway_supervisor': lambda: None}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'production-lifespan-isolated', 'exec'), namespace)
        return namespace['lifespan']

    def test_production_backend_lifespan_marks_success_after_initialization(self):
        runtime, watchdog, permit, verification = self.initializer('backend')
        cache = types.ModuleType('astra_backend.dashboard_cache')
        cache.start_dashboard_background_worker = lambda: None
        cache.stop_dashboard_background_worker = lambda: None
        called = []
        def refresh():
            self.assertTrue(any(row['kind'] == 'startup-verification' for row in self.store.status()['activities']))
            called.append('refresh')
        lifespan = self.backend_lifespan(runtime, verification, refresh)
        async def check():
            async with lifespan(object()):
                self.assertEqual(called, ['refresh'])
                self.assertTrue(any(row['kind'] == 'startup-complete' for row in self.store.status()['activities']))
                self.assertTrue(self.store.consume_startup_completion(self.binding, watchdog, permit, runtime.identity))
        with patch.dict(os.environ, {'ASTRA_STANDALONE_GATEWAY': 'true',
                                    'ASTRA_MAINTENANCE_STARTUP_BINDING': json.dumps(self.binding.as_dict())}), \
                patch.dict(sys.modules, {'astra_backend.dashboard_cache': cache}):
            self.immediate(check())
        self.assertTrue(self.store.status()['fenced'])

    def test_production_backend_lifespan_failure_keeps_unresolved_verification(self):
        runtime, watchdog, permit, verification = self.initializer('backend')
        def failed_refresh(): raise RuntimeError('controlled production initializer failure')
        lifespan = self.backend_lifespan(runtime, verification, failed_refresh)
        async def check():
            async with lifespan(object()): self.fail('failed initialization cannot yield readiness')
        with self.assertRaisesRegex(RuntimeError, 'initializer failure'):
            self.immediate(check())
        self.assertEqual({row['kind'] for row in self.store.status()['activities']},
                         {'paused-startup:backend', 'startup-verification'})

    def gateway_initializer(self, runtime, watchdog, permit, fail_store=False):
        tree = ast.parse((ROOT / 'astra_gateway/worker.py').read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_run_locked')
        def gateway_store(path):
            self.assertTrue(any(row['kind'] == 'startup-verification' for row in self.store.status()['activities']))
            if fail_store: raise RuntimeError('controlled gateway store initialization failure')
            return object()
        def delivery(store, scheduler, maintenance, prune):
            self.assertTrue(any(row['kind'] == 'startup-complete' for row in self.store.status()['activities']))
            self.assertTrue(self.store.consume_startup_completion(self.binding, watchdog, permit, runtime.identity))
        namespace = {'os': os, 'signal': types.SimpleNamespace(SIGTERM=15, SIGINT=2, signal=lambda *_: None),
                     'stop': lambda *_: None, 'get_runtime': lambda role: runtime,
                     'PID_FILE': self.directory / 'controlled-gateway.pid', 'DB_PATH': self.directory / 'controlled-gateway.db',
                     'GatewayStore': gateway_store, 'GatewayScheduler': lambda *a, **kw: types.SimpleNamespace(shutdown=lambda **kw: None),
                     'AdmissionClosed': m.AdmissionClosed, 'log': lambda *_: None, 'write_heartbeat': lambda: True,
                     'time': time, 'PRUNE_INTERVAL_SECONDS': 3600, '_delivery_loop': delivery}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'production-gateway-initializer-isolated', 'exec'), namespace)
        return namespace['_run_locked']

    def test_production_gateway_marks_success_after_store_scheduler_and_heartbeat(self):
        runtime, watchdog, permit, _ = self.initializer('gateway', begin=False)
        initialize = self.gateway_initializer(runtime, watchdog, permit)
        with patch.dict(os.environ, {'ASTRA_MAINTENANCE_STARTUP_ACTIVITY': permit,
                                    'ASTRA_MAINTENANCE_STARTUP_BINDING': json.dumps(self.binding.as_dict())}):
            initialize()
        self.assertEqual(self.store.status()['activities'], [])
        self.assertTrue(self.store.status()['fenced'])

    def test_production_gateway_store_failure_does_not_complete_in_finally(self):
        runtime, watchdog, permit, _ = self.initializer('gateway', begin=False)
        initialize = self.gateway_initializer(runtime, watchdog, permit, fail_store=True)
        with patch.dict(os.environ, {'ASTRA_MAINTENANCE_STARTUP_ACTIVITY': permit,
                                    'ASTRA_MAINTENANCE_STARTUP_BINDING': json.dumps(self.binding.as_dict())}), \
                self.assertRaisesRegex(RuntimeError, 'store initialization failure'):
            initialize()
        self.assertEqual({row['kind'] for row in self.store.status()['activities']},
                         {'paused-startup:gateway', 'startup-verification'})

    def test_normal_nonmaintenance_startup_finishing_is_preserved(self):
        directory = self.directory / 'normal'
        directory.mkdir()
        store = m.MaintenanceStore(directory / 'normal.sqlite')
        for identity in self.old.values(): store.startup(identity)
        binding = replace(self.binding, operation_id='normal-fixture', instances=self.old)
        actor = binding.instances['backend']
        proof = m.RiskProof(binding.operation_id, binding.generation, binding.plan_sha256, actor.source,
                            actor.image, time.time(), 'DEMO', True, 0, 0, 0, 0, 0, 0)
        store.request(binding, time.time() + 120, proof)
        for identity in self.old.values(): store.acknowledge(binding, identity)
        store.pause(binding, proof); store.resume(binding, proof)
        runtime = r.Runtime(store, actor)
        activity = runtime.begin_startup_verification()
        runtime.complete_startup_verification(activity)
        self.assertEqual(store.status()['activities'], [])
        self.assertEqual(store.status()['phase'], 'NORMAL')
        legacy_dir = self.directory / 'legacy'
        legacy_dir.mkdir()
        legacy = r.LegacyRuntime(legacy_dir / 'absent.sqlite')
        with patch.dict(os.environ, {'ASTRA_MAINTENANCE_ENABLED': ''}):
            legacy.complete_startup_verification(legacy.begin_startup_verification())
        self.assertFalse(legacy.path.exists())


if __name__ == '__main__':
    unittest.main()
