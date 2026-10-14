"""Synthetic refusal cases for the focused decision gate; no service calls."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).with_name("run_decision_contract_regressions.py")
SPEC = importlib.util.spec_from_file_location("decision_gate_under_test", PATH)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def result(*, successful=True, count=474, skipped=(), expected=()):
    return SimpleNamespace(wasSuccessful=lambda: successful, testsRun=count,
                           skipped=skipped, expectedFailures=expected)


class DecisionRegressionGateTests(unittest.TestCase):
    def test_frozen_module_minima_cover_the_reviewed_suite(self):
        self.assertEqual(sum(gate.TEST_SUITES.values()), 474)
        self.assertEqual(len(gate.TEST_SUITES), 28)
        self.assertEqual(gate.TEST_SUITES["tests.core.test_proposal_semantic_contract"], 12)

    def test_empty_contract_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Empty"):
            gate.load_required_suites({})

    def test_each_module_minimum_is_required(self):
        loader = Mock(errors=[])
        loader.loadTestsFromName.side_effect = [
            unittest.TestSuite([unittest.FunctionTestCase(lambda: None) for _ in range(3)]),
            unittest.TestSuite([unittest.FunctionTestCase(lambda: None)]),
        ]
        with self.assertRaisesRegex(RuntimeError, "second requires 2"):
            gate.load_required_suites({"first": 1, "second": 2}, loader)

    def test_missing_module_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Incomplete discovery"):
            gate.load_required_suites({"does_not_exist_astra_fixture": 1})

    def test_loader_errors_are_not_hidden_by_sufficient_count(self):
        loader = Mock(errors=["synthetic import failure"])
        loader.loadTestsFromName.return_value = Mock(countTestCases=lambda: 99)
        with self.assertRaisesRegex(RuntimeError, "Incomplete discovery"):
            gate.load_required_suites({"synthetic": 1}, loader)

    def test_invalid_minimum_is_rejected(self):
        for minimum in (0, -1, True, 1.5):
            loader = Mock(errors=[])
            loader.loadTestsFromName.return_value = Mock(countTestCases=lambda: 99)
            with self.subTest(minimum=minimum), self.assertRaises(RuntimeError):
                gate.load_required_suites({"synthetic": minimum}, loader)

    def test_complete_discovery_is_accepted(self):
        loader = Mock(errors=[])
        loader.loadTestsFromName.return_value = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        self.assertEqual(gate.load_required_suites({"synthetic": 1}, loader).countTestCases(), 1)

    def test_complete_success_is_accepted(self):
        gate.require_complete_result(result(), 474)

    def test_failure_is_rejected(self):
        with self.assertRaises(RuntimeError):
            gate.require_complete_result(result(successful=False), 474)

    def test_incomplete_execution_is_rejected(self):
        with self.assertRaises(RuntimeError):
            gate.require_complete_result(result(count=473), 474)

    def test_skip_is_rejected_even_when_unittest_reports_success(self):
        with self.assertRaises(RuntimeError):
            gate.require_complete_result(result(skipped=[("case", "synthetic")]), 474)

    def test_expected_failure_is_rejected_even_when_unittest_reports_success(self):
        with self.assertRaises(RuntimeError):
            gate.require_complete_result(result(expected=[("case", "synthetic")]), 474)

    def test_guard_failure_cannot_be_reported_as_pass(self):
        guard = Mock()
        guard.install.return_value = guard
        guard.report.return_value = False
        module = SimpleNamespace(OfflineGuard=Mock(return_value=guard))
        fake_spec = SimpleNamespace(loader=Mock())
        with patch.object(gate.importlib.util, "spec_from_file_location", return_value=fake_spec), \
             patch.object(gate.importlib.util, "module_from_spec", return_value=module), \
             patch.object(gate, "load_required_suites", return_value=unittest.TestSuite()), \
             patch.object(gate.unittest, "TextTestRunner") as runner:
            runner.return_value.run.return_value = result()
            with self.assertRaisesRegex(RuntimeError, "protected resource"):
                gate.main()
        guard.prove_connection_guard.assert_called_once()


if __name__ == "__main__":
    unittest.main()
