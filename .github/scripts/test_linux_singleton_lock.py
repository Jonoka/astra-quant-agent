"""Real Linux flock acceptance; never replace fcntl with a workstation stub."""
import ast
import functools
import inspect
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SOURCE_ROOT = os.environ.get('ASTRA_LOCK_TEST_SOURCE_ROOT')
ROOT = (Path(SOURCE_ROOT) if SOURCE_ROOT is not None else Path(__file__).resolve().parents[2]).resolve()
CHILD = r'''
import ast, functools, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import fcntl
from astra_backend.file_locks import file_lock
target = pathlib.Path(sys.argv[2])
mode = sys.argv[3]
if mode == 'file':
    print('WAITING', flush=True)
    with file_lock(target):
        print('HELD', flush=True)
        sys.stdin.readline()
elif mode == 'brain':
    tree = ast.parse((pathlib.Path(sys.argv[1]) / 'scripts/ai_brain_trader.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'single_brain_cycle')
    space = dict(os=os, fcntl=fcntl, wraps=functools.wraps, DATA_DIR=str(target.parent),
                 AI_BRAIN_LOCK_FILE=str(target), _record_cycle_health=lambda *a: None)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual-single-brain-cycle>', 'exec'), space)
    @space['single_brain_cycle']
    def hold():
        print('HELD', flush=True)
        sys.stdin.readline()
    hold()
'''


@unittest.skipUnless(sys.platform.startswith('linux'), 'requires a running Linux kernel and real fcntl.flock')
class LinuxSingletonLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import fcntl
        if not inspect.isbuiltin(fcntl.flock) or fcntl.flock.__module__ != 'fcntl':
            raise AssertionError('Real fcntl.flock is required; a mock is not acceptance')
        cls.fcntl = fcntl
        sys.path.insert(0, str(ROOT))
        from astra_backend.file_locks import file_lock, lock_is_held
        cls.file_lock = staticmethod(file_lock)
        cls.lock_is_held = staticmethod(lock_is_held)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='astra-real-flock-')
        self.addCleanup(self.directory.cleanup)
        self.target = Path(self.directory.name) / 'state.json'

    def child(self, mode):
        process = subprocess.Popen([sys.executable, '-u', '-c', CHILD, str(ROOT), str(self.target), mode],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.close_child, process)
        return process

    @staticmethod
    def close_child(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def line(self, process):
        # select gives readiness a bounded wait without an indefinitely blocking readline.
        import select
        deadline = time.monotonic() + 10
        data = bytearray()
        while True:
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            self.assertTrue(ready, 'lock worker did not report readiness within 10 seconds')
            byte = os.read(process.stdout.fileno(), 1)
            self.assertTrue(byte, 'lock worker exited before reporting readiness')
            if byte == b'\n':
                return data.decode('utf-8').strip()
            data.extend(byte)

    def brain_wrapper(self, health):
        node = next(n for n in ast.parse((ROOT / 'scripts/ai_brain_trader.py').read_text(encoding='utf-8')).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'single_brain_cycle')
        space = dict(os=os, fcntl=self.fcntl, wraps=functools.wraps, DATA_DIR=self.directory.name,
                     AI_BRAIN_LOCK_FILE=str(self.target), _record_cycle_health=lambda *a: health.append(a))
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual-single-brain-cycle>', 'exec'), space)
        return space['single_brain_cycle']

    def test_file_lock_is_reentrant_and_releases(self):
        with self.file_lock(self.target):
            with self.file_lock(self.target):
                self.assertTrue(self.lock_is_held(self.target))
        self.assertFalse(self.lock_is_held(self.target))
        with open(self.target.with_name('.state.json.lock'), 'a+') as handle:
            self.fcntl.flock(handle, self.fcntl.LOCK_EX | self.fcntl.LOCK_NB)

    def test_file_lock_blocks_another_process_until_release(self):
        holder = self.child('file')
        self.assertEqual(self.line(holder), 'WAITING')
        self.assertEqual(self.line(holder), 'HELD')
        waiter = self.child('file')
        self.assertEqual(self.line(waiter), 'WAITING')
        import select
        self.assertFalse(select.select([waiter.stdout], [], [], 0.3)[0], 'second writer bypassed flock')
        holder.communicate(input='release\n', timeout=10)
        self.assertEqual(holder.returncode, 0)
        self.assertEqual(self.line(waiter), 'HELD')
        waiter.communicate(input='release\n', timeout=10)
        self.assertEqual(waiter.returncode, 0)

    def test_kernel_releases_file_lock_after_worker_termination(self):
        holder = self.child('file')
        self.assertEqual(self.line(holder), 'WAITING')
        self.assertEqual(self.line(holder), 'HELD')
        holder.kill()
        holder.communicate(timeout=10)
        with open(self.target.with_name('.state.json.lock'), 'a+') as handle:
            self.fcntl.flock(handle, self.fcntl.LOCK_EX | self.fcntl.LOCK_NB)

    def test_brain_cycle_reports_busy_then_runs_after_release(self):
        holder = self.child('brain')
        self.assertEqual(self.line(holder), 'HELD')
        health, calls = [], []
        wrapped = self.brain_wrapper(health)(lambda: calls.append('ran') or 'result')
        self.assertIsNone(wrapped())
        self.assertEqual(calls, [])
        self.assertEqual(health, [('skipped', 'inference_lock_active')])
        holder.communicate(input='release\n', timeout=10)
        self.assertEqual(holder.returncode, 0)
        self.assertEqual(wrapped(), 'result')
        self.assertEqual(calls, ['ran'])

    def test_brain_cycle_releases_lock_on_exception(self):
        def fail():
            raise ValueError('synthetic failure')
        with self.assertRaisesRegex(ValueError, 'synthetic failure'):
            self.brain_wrapper([])(fail)()
        with open(self.target, 'a+') as handle:
            self.fcntl.flock(handle, self.fcntl.LOCK_EX | self.fcntl.LOCK_NB)

    def test_brain_cycle_refuses_same_thread_recursive_entry(self):
        health, calls = [], []
        wrapper = self.brain_wrapper(health)
        nested = wrapper(lambda: calls.append('nested'))
        outer = wrapper(lambda: nested())
        self.assertIsNone(outer())
        self.assertEqual(calls, [])
        self.assertEqual(health, [('skipped', 'inference_lock_active')])
        self.assertEqual(wrapper(lambda: 'released')(), 'released')

    def test_brain_cycle_refuses_another_thread_in_same_process(self):
        entered, release = threading.Event(), threading.Event()
        health, calls = [], []
        wrapper = self.brain_wrapper(health)
        def hold():
            entered.set()
            if not release.wait(10):
                raise AssertionError('synthetic lock holder was not released')
        thread = threading.Thread(target=wrapper(hold))
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            self.assertIsNone(wrapper(lambda: calls.append('overlap'))())
            self.assertEqual(calls, [])
            self.assertEqual(health, [('skipped', 'inference_lock_active')])
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(wrapper(lambda: 'released')(), 'released')


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LinuxSingletonLockTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() and result.testsRun >= 7 and not result.skipped else 1)
