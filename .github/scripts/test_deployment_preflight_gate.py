"""Synthetic default-entrypoint regressions; never run deployment acceptance.

Only the dirfd subprocess executes, using synthetic TestCase bodies and the
real suite's entrypoint. Git, source verification, archive extraction, root
checks and all other preflight children are replaced with inert test doubles.
Source-retention tests use temporary Python fixtures and simulated Git output.
"""
import ast
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import run_deployment_preflight as preflight
import verify_deployment_source as guard


ROOT = Path(__file__).resolve().parents[2]
DIRFD = 'tests/maintenance_integration/test_dirfd_isolation.py'
MINIMUM = 14  # Independent contract, never inferred from the candidate runner.


class DeploymentPreflightGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='preflight-gate-')
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        self.scripts = self.source / '.github/scripts'
        self.scripts.mkdir(parents=True)
        self.dirfd = self.source / DIRFD
        self.dirfd.parent.mkdir(parents=True)
        for name in ('run_actual_integration.py', 'run_taker_regressions.py'):
            shutil.copyfile(ROOT / '.github/scripts' / name, self.scripts / name)
        self.output = io.StringIO()
        self.children = []
        self.write_suite()

    def write_suite(self, count=MINIMUM, outcome='pass'):
        # Keep the real imports and __main__ implementation. Replacing only
        # TestCase bodies tests the actual gate without application side effects.
        tree = ast.parse((ROOT / DIRFD).read_text(encoding='utf-8'))
        body = ['class DirFdIsolationTests(unittest.TestCase):']
        if not count:
            body.append('    pass')
        for index in range(count):
            if outcome == 'skip':
                body.append("    @unittest.skip('synthetic skip')")
            elif index == 0 and outcome in {'expected_failure', 'unexpected_success'}:
                body.append('    @unittest.expectedFailure')
            body.extend([
                f'    def test_{index:02d}(self):',
                "        with Path(__file__).with_suffix('.ran').open('a') as stream:",
                f"            stream.write('{index}\\n')",
            ])
            if index == 0 and outcome in {'failure', 'expected_failure'}:
                body.append("        self.fail('synthetic failure')")
            elif index == 0 and outcome == 'error':
                body.append("        raise RuntimeError('synthetic error')")
        replacement = ast.parse('\n'.join(body)).body[0]
        tree.body = [replacement if isinstance(node, ast.ClassDef)
                     and node.name == 'DirFdIsolationTests' else node for node in tree.body]
        self.dirfd.write_text(ast.unparse(tree) + '\n', encoding='utf-8')

    def run_default(self):
        real_run = subprocess.run

        def child(command, **kwargs):
            if str(self.dirfd) in command:
                self.children.append((command, kwargs))
                return real_run(command, **kwargs, capture_output=True, text=True)
            # No Git, sudo, application suite or deployment helper executes.
            return subprocess.CompletedProcess(command, 0, stdout=b'')

        with patch.object(preflight, '__file__', str(self.scripts / 'run_deployment_preflight.py')), \
                patch.object(preflight, 'verify') as verify, \
                patch.object(preflight, 'extract'), \
                patch.object(preflight.os, 'geteuid', return_value=0), \
                patch.object(preflight.sys, 'platform', 'linux'), \
                patch.object(preflight.subprocess, 'run', side_effect=child), \
                patch.dict(os.environ, {'ASTRA_CI_EXPECTED_SHA': 'b' * 40,
                                        'PYTHONDONTWRITEBYTECODE': '1'}, clear=True), \
                redirect_stdout(self.output):
            preflight.main()
            self.assertEqual(verify.call_count, 2)

    def assert_default_refuses(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_default()
        self.assertNotEqual(caught.exception.returncode, 0)
        self.assertEqual(len(self.children), 1)
        self.assertNotIn('DEPLOYMENT_PREFLIGHT_PASS', self.output.getvalue())

    def test_default_entrypoint_executes_all_fourteen_dirfd_cases(self):
        self.run_default()
        self.assertEqual(len(self.children), 1)
        command, options = self.children[0]
        self.assertEqual(command, [sys.executable, str(self.dirfd)])
        self.assertIs(options['check'], True)
        self.assertEqual(options['cwd'], self.source)
        self.assertEqual(self.dirfd.with_suffix('.ran').read_text().splitlines(),
                         [str(index) for index in range(MINIMUM)])
        self.assertIn('DEPLOYMENT_PREFLIGHT_PASS', self.output.getvalue())

    def test_dirfd_failure_cannot_report_preflight_pass(self):
        self.write_suite(outcome='failure')
        self.assert_default_refuses()

    def test_dirfd_error_cannot_report_preflight_pass(self):
        self.write_suite(outcome='error')
        self.assert_default_refuses()

    def test_missing_dirfd_suite_cannot_report_preflight_pass(self):
        self.dirfd.unlink()
        self.assert_default_refuses()

    def test_empty_or_partial_dirfd_execution_cannot_report_preflight_pass(self):
        for count in (0, MINIMUM - 1):
            with self.subTest(count=count):
                self.children.clear()
                self.write_suite(count=count)
                self.assert_default_refuses()

    def test_skipped_dirfd_cases_cannot_report_preflight_pass(self):
        self.write_suite(outcome='skip')
        self.assert_default_refuses()

    def test_expected_failure_or_unexpected_success_cannot_report_preflight_pass(self):
        for outcome in ('expected_failure', 'unexpected_success'):
            with self.subTest(outcome=outcome):
                self.children.clear()
                self.write_suite(outcome=outcome)
                self.assert_default_refuses()

    def write_source_fixture(self):
        def write(relative, text):
            target = self.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')

        write('astra_backend/maintenance.py', 'PROTOCOL_VERSION = 1\n')
        for relative, minimum in {**guard.MAINTENANCE_CASE_MINIMUMS,
                                  **guard.ACTUAL_CASE_MINIMUMS}.items():
            write(relative, 'class SyntheticCases:\n' + ''.join(
                f'    def test_{index:02d}(self): pass\n' for index in range(minimum)))
        council = [
            'test_council_success_returns_the_exact_persisted_fresh_cache',
            'test_council_success_records_ok_health_and_truthful_output_without_fake_usage',
            'test_successful_council_cache_reaches_real_downstream_management_gate',
            'test_council_persistence_failure_never_returns_cache_or_marks_success',
            *(f'test_fixture_{index}' for index in range(5)),
        ]
        write('tests/ops/test_brain_dispatch.py', 'class CouncilCompletionTests:\n' +
              ''.join(f'    def {name}(self): pass\n' for name in council))

    def verify_fixture(self, changes=None):
        revision = 'b' * 40
        changes = guard.APPLICATION_PATCH if changes is None else changes

        def git(command, **kwargs):
            values = {'rev-parse': (revision + '\n').encode(), 'merge-base': b'',
                      'diff': b'\0'.join(path.encode() for path in sorted(changes)),
                      'status': b''}
            return subprocess.CompletedProcess(command, 0, stdout=values[command[3]])

        with patch.object(guard.subprocess, 'run', side_effect=git), \
                patch.object(guard, 'TAKER_PATCH_SHA256', {}), \
                patch.object(guard, 'ALPHA_PATCH_SHA256', {}), \
                patch.object(guard, 'DEADLINE_REGRESSIONS', {}), \
                redirect_stdout(self.output):
            guard.verify(self.source, 'a' * 40, revision)

    def test_dirfd_path_cannot_disappear_from_retained_application_manifest(self):
        self.assertIn(DIRFD, guard.APPLICATION_PATCH)
        self.write_source_fixture()
        self.verify_fixture()
        with self.assertRaisesRegex(AssertionError, 'Missing retained patch'):
            self.verify_fixture(guard.APPLICATION_PATCH - {DIRFD})

    def test_missing_empty_or_reduced_dirfd_source_is_rejected(self):
        self.assertEqual(guard.ACTUAL_CASE_MINIMUMS[DIRFD], MINIMUM)
        self.write_source_fixture()
        self.verify_fixture()
        self.dirfd.unlink()
        with self.assertRaises(FileNotFoundError):
            self.verify_fixture()
        for count in (0, MINIMUM - 1):
            with self.subTest(count=count):
                self.dirfd.write_text('class SyntheticCases:\n    pass\n' + ''.join(
                    f'    def test_{index:02d}(self): pass\n' for index in range(count)))
                with self.assertRaisesRegex(AssertionError, 'Maintenance regression missing'):
                    self.verify_fixture()


if __name__ == '__main__':
    from run_taker_regressions import require_complete_result
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DeploymentPreflightGateTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    require_complete_result(result, 9)
