"""Linux runner isolation regressions; no Astra application code is imported.

Every fixture, including the simulated repository and outside database, lives
in one test-owned temporary tree. Denial cases put a tripwire in front of the
real os.open so a broken guard cannot perform the forbidden operation.
The default deployment preflight runs this separately from actual integration.
Direct execution also requires all 14 cases, with no skips or expected failures.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / '.github/scripts'))
import run_actual_integration as runner


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux /proc/self/fd regression')
class DirFdIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='dirfd-guard-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.sandbox = self.base / 'sandbox'
        self.outside = self.base / 'outside'
        self.repository = self.base / 'repository'
        for directory in (self.sandbox / 'data', self.outside, self.repository / 'data'):
            directory.mkdir(parents=True)
        (self.sandbox / 'data' / 'fixture.db').write_bytes(b'synthetic inside')
        (self.outside / 'fixture.db').write_bytes(b'synthetic outside')
        previous_cwd = Path.cwd()
        self.addCleanup(os.chdir, previous_cwd)
        os.chdir(self.repository)
        root_patch = patch.object(runner, 'ROOT', self.repository)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.original_open = os.open

    def directory_fd(self, path):
        fd = self.original_open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        return fd

    @contextmanager
    def guarded(self, *, deny_syscall=False):
        raw = (Mock(side_effect=AssertionError('raw os.open tripwire reached'))
               if deny_syscall else Mock(wraps=self.original_open))
        with patch.object(os, 'open', raw), runner.isolated(self.sandbox):
            yield raw

    def assert_refused(self, raw, path, flags=os.O_RDONLY, **kwargs):
        with self.assertRaisesRegex(AssertionError, 'integration '):
            os.open(path, flags, **kwargs)
        raw.assert_not_called()

    def test_relative_read_and_write_use_directory_fd_and_preserve_arguments(self):
        directory = self.directory_fd(self.sandbox / 'data')
        with self.guarded() as raw:
            with os.fdopen(os.open('fixture.db', os.O_RDONLY, dir_fd=directory), 'rb') as stream:
                self.assertEqual(stream.read(), b'synthetic inside')
            raw.assert_called_once_with('fixture.db', os.O_RDONLY, dir_fd=directory)
            raw.reset_mock()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            with os.fdopen(os.open('created.txt', flags, 0o600, dir_fd=directory), 'wb') as stream:
                stream.write(b'synthetic write')
            raw.assert_called_once_with('created.txt', flags, 0o600, dir_fd=directory)
            self.assertEqual((self.sandbox / 'data' / 'created.txt').read_bytes(), b'synthetic write')

    def test_temp_directory_data_cleanup_does_not_resolve_against_repository(self):
        self.assertTrue(shutil.rmtree.avoids_symlink_attacks)
        with self.guarded():
            with tempfile.TemporaryDirectory(dir=self.sandbox) as temporary:
                data = Path(temporary) / 'data'
                data.mkdir()
                (data / 'fixture.db').write_bytes(b'cleanup fixture')
            self.assertFalse(Path(temporary).exists())
            self.assertTrue((self.repository / 'data').is_dir())

    def test_external_database_read_is_rejected_before_syscall(self):
        directory = self.directory_fd(self.outside)
        os.chdir(self.sandbox)
        with self.guarded(deny_syscall=True) as raw:
            self.assert_refused(raw, 'fixture.db', dir_fd=directory)

    def test_external_write_flags_are_rejected_before_syscall(self):
        directory = self.directory_fd(self.outside)
        os.chdir(self.sandbox)
        with self.guarded(deny_syscall=True) as raw:
            for flags in (os.O_WRONLY, os.O_RDWR, os.O_CREAT, os.O_TRUNC, os.O_APPEND):
                with self.subTest(flags=flags):
                    self.assert_refused(raw, 'created.txt', flags, dir_fd=directory)
        self.assertFalse((self.outside / 'created.txt').exists())

    def test_parent_traversal_cannot_escape_sandbox(self):
        directory = self.directory_fd(self.sandbox)
        os.chdir(self.sandbox / 'data')
        with self.guarded(deny_syscall=True) as raw:
            self.assert_refused(raw, '../outside/fixture.db', dir_fd=directory)
            self.assert_refused(raw, '../outside/created.txt', os.O_WRONLY | os.O_CREAT, dir_fd=directory)

    def test_symlink_cannot_redirect_directory_fd_access_outside(self):
        (self.sandbox / 'link').symlink_to(self.outside, target_is_directory=True)
        directory = self.directory_fd(self.sandbox)
        os.chdir(self.sandbox)
        with self.guarded(deny_syscall=True) as raw:
            self.assert_refused(raw, 'link/fixture.db', dir_fd=directory)
            self.assert_refused(raw, 'link/created.txt', os.O_WRONLY | os.O_CREAT, dir_fd=directory)

    def test_absolute_paths_override_external_or_invalid_directory_fd(self):
        outside_fd = self.directory_fd(self.outside)
        inside_file = self.sandbox / 'data' / 'fixture.db'
        with self.guarded() as raw:
            for directory in (outside_fd, -1):
                with self.subTest(dir_fd=directory):
                    with os.fdopen(os.open(inside_file, os.O_RDONLY, dir_fd=directory), 'rb') as stream:
                        self.assertEqual(stream.read(), b'synthetic inside')
                    raw.assert_called_with(inside_file, os.O_RDONLY, dir_fd=directory)

    def test_absolute_outside_paths_are_rejected_with_inside_directory_fd(self):
        directory = self.directory_fd(self.sandbox)
        with self.guarded(deny_syscall=True) as raw:
            self.assert_refused(raw, self.outside / 'fixture.db', dir_fd=directory)
            self.assert_refused(raw, self.outside / 'created.txt', os.O_WRONLY | os.O_CREAT, dir_fd=directory)

    def test_bytes_and_pathlike_relative_paths_are_forwarded_unchanged(self):
        directory = self.directory_fd(self.sandbox / 'data')
        with self.guarded() as raw:
            for path in (b'fixture.db', Path('fixture.db')):
                with self.subTest(path=path):
                    with os.fdopen(os.open(path, os.O_RDONLY, dir_fd=directory), 'rb') as stream:
                        self.assertEqual(stream.read(), b'synthetic inside')
                    raw.assert_called_with(path, os.O_RDONLY, dir_fd=directory)

    def test_missing_fd_resolution_fails_closed(self):
        directory = self.directory_fd(self.sandbox)
        os.chdir(self.sandbox)
        with self.guarded(deny_syscall=True) as raw:
            self.assert_refused(raw, 'created.txt', os.O_WRONLY | os.O_CREAT, dir_fd=-1)
            with patch.object(os, 'readlink', side_effect=OSError(errno.ENOENT, 'no proc fd links')):
                self.assert_refused(raw, 'created.txt', os.O_WRONLY | os.O_CREAT, dir_fd=directory)

    def test_no_directory_fd_keeps_cwd_resolution(self):
        os.chdir(self.sandbox / 'data')
        with self.guarded():
            with os.fdopen(os.open('fixture.db', os.O_RDONLY), 'rb') as stream:
                self.assertEqual(stream.read(), b'synthetic inside')
            with os.fdopen(os.open('fixture.db', os.O_RDONLY, dir_fd=None), 'rb') as stream:
                self.assertEqual(stream.read(), b'synthetic inside')

    def test_linux_at_fdcwd_uses_cwd_and_preserves_arguments(self):
        os.chdir(self.sandbox / 'data')
        with self.guarded() as raw:
            with os.fdopen(os.open('fixture.db', os.O_RDONLY, dir_fd=-100), 'rb') as stream:
                self.assertEqual(stream.read(), b'synthetic inside')
            raw.assert_called_once_with('fixture.db', os.O_RDONLY, dir_fd=-100)
            raw.reset_mock()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            with os.fdopen(os.open('created.txt', flags, 0o600, dir_fd=-100), 'wb') as stream:
                stream.write(b'synthetic AT_FDCWD write')
            raw.assert_called_once_with('created.txt', flags, 0o600, dir_fd=-100)
            self.assertEqual((self.sandbox / 'data' / 'created.txt').read_bytes(), b'synthetic AT_FDCWD write')

    def test_linux_at_fdcwd_rejects_external_database_reads_and_writes(self):
        os.chdir(self.outside)
        with self.guarded(deny_syscall=True) as raw:
            self.assert_refused(raw, 'fixture.db', dir_fd=-100)
            self.assert_refused(raw, 'created.txt', os.O_WRONLY | os.O_CREAT, dir_fd=-100)
        self.assertFalse((self.outside / 'created.txt').exists())

    def test_at_fdcwd_number_is_not_assumed_on_other_platforms(self):
        os.chdir(self.sandbox)
        with self.guarded(deny_syscall=True) as raw:
            with patch.object(sys, 'platform', 'darwin'):
                self.assert_refused(raw, 'created.txt', os.O_WRONLY | os.O_CREAT, dir_fd=-100)


if __name__ == '__main__':
    from run_taker_regressions import require_complete_result
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DirFdIsolationTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    require_complete_result(result, 14)
