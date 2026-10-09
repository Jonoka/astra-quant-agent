"""Prevent partial discovery and unittest's skip/expected-failure greens."""
import unittest
from pathlib import Path
import tempfile

from run_taker_regressions import ALPHA_SUITES, SUITES, copy_source, require_complete_result


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

    def test_original_411_gate_and_additional_alpha_12_remain_separate(self):
        self.assertEqual(sum(SUITES.values()), 411)
        self.assertEqual(len(SUITES), 17)
        self.assertEqual(ALPHA_SUITES, {'tests.llm.test_alpha_transport_contract': 12})
        self.assertFalse(set(SUITES) & set(ALPHA_SUITES))
        require_complete_result(self.result(testsRun=12), 12)
        for changes in ({'testsRun': 11}, {'testsRun': 0},
                        {'testsRun': 12, 'skipped': [(None, 'fixture')]},
                        {'testsRun': 12, 'expectedFailures': [(None, 'fixture')]},
                        {'testsRun': 12, 'errors': [None]}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                require_complete_result(self.result(**changes), 12)

    def test_shared_preflight_requires_both_default_and_alpha_runs(self):
        import ast
        source = Path(__file__).with_name('run_deployment_preflight.py')
        calls = [n for n in ast.walk(ast.parse(source.read_text()))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name)
                 and n.func.value.id == 'subprocess' and n.func.attr == 'run'
                 and n.args and isinstance(n.args[0], ast.List)
                 and 'run_taker_regressions.py' in ast.unparse(n.args[0])]
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(len(c.args[0].elts) for c in calls), [2, 3])
        self.assertEqual(sum('--alpha' in ast.unparse(c.args[0]) for c in calls), 1)
        for call in calls:
            self.assertTrue(any(k.arg == 'check' and isinstance(k.value, ast.Constant)
                                and k.value.value is True for k in call.keywords))
