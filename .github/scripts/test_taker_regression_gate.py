"""Prevent partial discovery and unittest's skip/expected-failure greens."""
import unittest
from pathlib import Path
import tempfile

from run_taker_regressions import copy_source, require_complete_result


class RegressionGateTests(unittest.TestCase):
    def result(self, **changes):
        result = unittest.TestResult()
        result.testsRun = 411
        for name, value in changes.items():
            setattr(result, name, value)
        return result

    def test_complete_success_is_accepted(self):
        require_complete_result(self.result(), 411)

    def test_empty_or_partial_execution_fails(self):
        for count in (0, 410):
            with self.subTest(count=count), self.assertRaises(RuntimeError):
                require_complete_result(self.result(testsRun=count), 411)

    def test_skipped_and_expected_failures_fail(self):
        for field in ("skipped", "expectedFailures"):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                require_complete_result(self.result(**{field: [(None, "fixture")]}), 411)

    def test_failures_errors_and_unexpected_successes_fail(self):
        for field in ("failures", "errors", "unexpectedSuccesses"):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                require_complete_result(self.result(**{field: [None]}), 411)

    def test_copy_preserves_test_fixtures_and_excludes_runtime_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            destination = Path(temporary) / "scratch"
            fixtures = {
                "data/prompt_library.json": "tracked template",
                "data/runtime.db": "runtime",
                "logs/runtime.log": "runtime",
                ".env": "credentials",
                ".git/config": "credentials",
                "tests/data/brain.json": "test fixture",
                "scripts/example.py": "source",
            }
            for relative, content in fixtures.items():
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            copy_source(source, destination)
            self.assertEqual((destination / "data/prompt_library.json").read_text(), "tracked template")
            self.assertEqual((destination / "tests/data/brain.json").read_text(), "test fixture")
            self.assertTrue((destination / "scripts/example.py").is_file())
            for relative in ("data/runtime.db", "logs", ".env", ".git"):
                self.assertFalse((destination / relative).exists(), relative)
