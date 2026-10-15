"""Fail-closed discovery, isolation and offline contract tests for PR regressions."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

from run_collection_regressions import (
    COLLECTION_SUITES, RETAINED_SUITES, LOCK_CHILD,
    clean_environment, is_lock_fixture_child, load_required_suites,
)
from run_taker_regressions import require_complete_result


class CollectionRegressionGateTests(unittest.TestCase):
    def test_all_32_modules_preserve_original_minimums_and_add_both_25_case_suites(self):
        self.assertEqual(COLLECTION_SUITES, {
            'tests.core.test_collection_deadline': 43,
            'tests.core.test_collection_regression': 6,
            'tests.core.test_module_tails': 57,
            'tests.core.test_router_system': 56,
            'tests.extraction.test_brain_package_extraction': 14,
            'tests.extraction.test_brain_snapshots_extraction': 5,
            'tests.extraction.test_dashboard_integrity_sidecars_extraction': 33,
            'tests.extraction.test_factors_smart_money_extraction': 4,
            'tests.llm.test_alpha_transport_contract': 12,
            'tests.ops.test_ai_brain_trader': 43,
            'tests.ops.test_brain_deadline': 20,
            'tests.ops.test_brain_packages': 47,
            'tests.ops.test_factor_library': 73,
            'tests.ops.test_factors_smart_money': 27,
            'tests.ops.test_gateway': 59,
            'tests.ops.test_gateway_agents': 29,
            'tests.ops.test_gateway_brain_outcome': 24,
            'tests.ops.test_gateway_scheduler': 67,
            'tests.ops.test_scheduler_timeout_diagnostics': 25,
            'tests.trading.test_okx_quant_factors': 62,
            'tests.ui.test_dashboard_health_tails': 9,
            'tests.venues.test_collection_http_deadline': 48,
            'tests.venues.test_indicator_failure_dedup': 25,
            'tests.venues.test_market_data_health': 21,
            'tests.venues.test_market_data_health_tails': 34,
            'tests.venues.test_market_data_service': 21,
            'tests.venues.test_market_data_service_tails': 51,
            'tests.venues.test_okx_public_data': 25,
            'tests.venues.test_okx_public_domains': 5,
            'tests.venues.test_okx_rest': 30,
            'tests.venues.test_okx_rest_tails': 102,
            'tests.venues.test_okx_v5_official_contract': 9,
        })
        self.assertEqual(len(COLLECTION_SUITES), 32)
        self.assertEqual(sum(COLLECTION_SUITES.values()), 1086)

    def test_retained_deadline_and_council_completion_are_explicit(self):
        import verify_deployment_source as source
        for path, cases in source.DEADLINE_REGRESSIONS.items():
            name = path[:-3].replace('/', '.')
            self.assertGreaterEqual(RETAINED_SUITES[name], len(cases))
        self.assertEqual(RETAINED_SUITES['tests.ops.test_brain_dispatch'], 46)
        self.assertEqual(len(RETAINED_SUITES), 12)

    def test_empty_discovery_contract_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Empty'):
            load_required_suites({})

    def test_per_module_partial_discovery_is_rejected(self):
        for name, minimum in (
            ('module', 43),
            ('tests.ops.test_scheduler_timeout_diagnostics', 25),
            ('tests.venues.test_indicator_failure_dedup', 25),
        ):
            loader = Mock(errors=[])
            loader.loadTestsFromName.return_value.countTestCases.return_value = minimum - 1
            with self.subTest(module=name), self.assertRaisesRegex(RuntimeError, f'requires {minimum}'):
                load_required_suites({name: minimum}, loader)

    def test_discovery_errors_are_rejected_even_above_minimum(self):
        loader = Mock(errors=['failed import'])
        loader.loadTestsFromName.return_value.countTestCases.return_value = 43
        with self.assertRaisesRegex(RuntimeError, 'Incomplete discovery'):
            load_required_suites({'module': 43}, loader)

    def test_complete_discovery_is_accepted(self):
        loader = Mock(errors=[])
        loader.loadTestsFromName.return_value.countTestCases.return_value = 43
        self.assertEqual(len(load_required_suites({'module': 43}, loader)), 1)

    def test_zero_partial_skipped_expected_failed_or_unexpected_success_are_rejected(self):
        for changes in ({'testsRun': 0}, {'testsRun': 42}, {'skipped': [(None, 'skip')]},
                        {'expectedFailures': [(None, 'expected')]}, {'failures': [(None, 'failure')]},
                        {'errors': [(None, 'error')]}, {'unexpectedSuccesses': [None]}):
            result = unittest.TestResult()
            result.testsRun = 43
            for key, value in changes.items():
                setattr(result, key, value)
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                require_complete_result(result, 43)

    def test_runtime_settings_and_credentials_are_removed(self):
        env = clean_environment({'ASTRA_GATEWAY_DB': '/production/db', 'ASTRA_MODEL': 'live',
                                 'GITHUB_TOKEN': 'secret', 'GH_TOKEN': 'secret', 'PROVIDER_API_KEY': 'secret',
                                 'MODEL_ACCESS_TOKEN': 'secret', 'SECRET_KEY': 'secret', 'PATH': '/usr/bin'})
        self.assertEqual(env, {'PATH': '/usr/bin', 'PYTHONDONTWRITEBYTECODE': '1',
                              'ASTRA_LEDGER_SYNC_DISABLED': '1', 'OFFLINE_SUITE_RUNNING': '1'})

    def test_only_exact_local_flock_child_is_allowed(self):
        target = str(Path(tempfile.gettempdir()) / 'synthetic-ci' / '.synthetic.json.lock')
        good = [sys.executable, '-c', LOCK_CHILD, target]
        self.assertTrue(is_lock_fixture_child(good))
        for bad in (good + ['extra'], ['sh', '-c', LOCK_CHILD, target],
                    [sys.executable, '-c', LOCK_CHILD + '; import socket', target],
                    [sys.executable, '-c', LOCK_CHILD, '/production/.synthetic.json.lock'],
                    [sys.executable, '-c', LOCK_CHILD, target + '.other']):
            with self.subTest(bad=bad):
                self.assertFalse(is_lock_fixture_child(bad))

    def test_pr_workflow_is_exact_head_read_only_and_non_deploying(self):
        import yaml
        root = Path(__file__).resolve().parents[2]
        data = yaml.safe_load((root / '.github/workflows/test-taker-parsing.yml').read_text())
        events = data.get('on', data.get(True))
        self.assertEqual(set(events), {'pull_request'})
        self.assertEqual(events['pull_request']['branches'], ['codex/deploy'])
        self.assertEqual(data['permissions'], {'contents': 'read'})
        job = data['jobs']['taker-regressions']
        checkout = next(x for x in job['steps'] if x.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with']['ref'], '${{ github.event.pull_request.head.sha }}')
        self.assertEqual(checkout['with']['fetch-depth'], 0)
        self.assertIs(checkout['with']['persist-credentials'], False)
        text = '\n'.join(str(x.get('run', '')) for x in job['steps'])
        self.assertIn('run_deployment_preflight.py --collection-pr', text)
        self.assertIn('npm ci --ignore-scripts --no-audit --no-fund', text)
        self.assertIn('run_collection_frontend.mjs', text)
        for forbidden in ('docker push', 'ssh ', 'workflow_dispatch', 'secrets.', 'packages: write'):
            self.assertNotIn(forbidden, str(data))


if __name__ == '__main__':
    unittest.main()
