"""Synthetic file-lock waits share the absolute caller deadline."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from astra_backend import deadline, file_locks


class FileLockDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "synthetic.json"

    def test_without_deadline_retains_blocking_and_reentrant_lock(self):
        calls = []
        with patch.object(file_locks.fcntl, "flock", lambda fd, flags: calls.append(flags)):
            with file_locks.file_lock(self.target):
                with file_locks.file_lock(self.target):
                    self.assertTrue(file_locks.lock_is_held(self.target))
        self.assertEqual(calls, [file_locks.fcntl.LOCK_EX, file_locks.fcntl.LOCK_UN])
        self.assertFalse(file_locks.lock_is_held(self.target))

    def test_contended_lock_wait_expires_before_entering_or_renewing_budget(self):
        clock = [100.0]
        calls = []
        def contend(fd, flags):
            calls.append(flags)
            raise BlockingIOError("synthetic contention")
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch.object(deadline.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch.object(file_locks.fcntl, "flock", contend), deadline.deadline_scope(timeout=0.12):
            with self.assertRaises(deadline.DeadlineExceeded):
                with file_locks.file_lock(self.target):
                    self.fail("entered an exhausted lock")
        self.assertTrue(calls)
        self.assertTrue(all(flags == file_locks.fcntl.LOCK_EX | file_locks.fcntl.LOCK_NB for flags in calls))
        self.assertFalse(file_locks.lock_is_held(self.target))

    def test_contended_then_available_lock_enters_inside_original_deadline(self):
        clock, calls = [100.0], []
        def available(fd, flags):
            calls.append(flags)
            if len(calls) == 1:
                raise BlockingIOError("synthetic contention")
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch.object(deadline.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch.object(file_locks.fcntl, "flock", available), deadline.deadline_scope(timeout=1) as boundary:
            with file_locks.file_lock(self.target):
                self.assertEqual(deadline.current_deadline(), boundary)
                self.assertAlmostEqual(deadline.remaining(), 0.95)
        self.assertEqual(calls[-1], file_locks.fcntl.LOCK_UN)

    def test_expiry_at_acquisition_unlocks_without_entering(self):
        clock, calls = [100.0], []
        def acquire(fd, flags):
            calls.append(flags)
            if flags != file_locks.fcntl.LOCK_UN:
                clock[0] += 2
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch.object(file_locks.fcntl, "flock", acquire), deadline.deadline_scope(timeout=1):
            with self.assertRaises(deadline.DeadlineExceeded):
                with file_locks.file_lock(self.target):
                    self.fail("entered a lock acquired after expiry")
        self.assertEqual(calls[-1], file_locks.fcntl.LOCK_UN)
        self.assertFalse(file_locks.lock_is_held(self.target))

    def test_expired_reentry_does_not_enter_or_release_the_outer_lock(self):
        clock, calls = [100.0], []
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch.object(file_locks.fcntl, "flock", lambda fd, flags: calls.append(flags)), \
             deadline.deadline_scope(timeout=1):
            with file_locks.file_lock(self.target):
                clock[0] += 2
                with self.assertRaises(deadline.DeadlineExceeded):
                    with file_locks.file_lock(self.target):
                        self.fail("expired recursive entry")
                self.assertTrue(file_locks.lock_is_held(self.target))
                self.assertEqual(len(calls), 1)
        self.assertEqual(calls[-1], file_locks.fcntl.LOCK_UN)


@unittest.skipUnless(sys.platform.startswith("linux"), "requires genuine Linux flock")
class LinuxFileLockDeadlineTests(unittest.TestCase):
    setUp = FileLockDeadlineTests.setUp
    def test_real_cross_process_contention_times_out_then_lock_remains_usable(self):
        import select
        child = subprocess.Popen([
            sys.executable, "-c",
            "import fcntl,sys; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read(1)",
            str(file_locks._lock_path(self.target)[0]),
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            readable, _, _ = select.select([child.stdout], [], [], 3)
            self.assertTrue(readable, "synthetic child did not acquire its lock in time")
            self.assertEqual(os.read(child.stdout.fileno(), 128).strip(), b"ready")
            started = time.monotonic()
            with deadline.deadline_scope(timeout=0.12):
                with self.assertRaises(deadline.DeadlineExceeded):
                    with file_locks.file_lock(self.target):
                        self.fail("cross-process contended lock entered")
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(file_locks.lock_is_held(self.target))
        finally:
            try:
                child.communicate("x", timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=3)
        self.assertEqual(child.returncode, 0)
        with deadline.deadline_scope(timeout=1), file_locks.file_lock(self.target):
            self.assertTrue(file_locks.lock_is_held(self.target))

    def test_real_same_process_thread_contention_preserves_absolute_deadline(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def holder():
            try:
                with file_locks.file_lock(self.target):
                    entered.set()
                    release.wait(2)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=holder)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with deadline.deadline_scope(timeout=0.12):
                with self.assertRaises(deadline.DeadlineExceeded):
                    with file_locks.file_lock(self.target):
                        self.fail("same-process contended lock entered")
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        with deadline.deadline_scope(timeout=1), file_locks.file_lock(self.target):
            self.assertTrue(file_locks.lock_is_held(self.target))


if __name__ == "__main__":
    unittest.main()
