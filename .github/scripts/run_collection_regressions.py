"""Explicit collection and retained deadline/council PR regressions, offline only.

The guard is Python-level, not a native-code sandbox. No production inputs or
credentials are copied, and subprocesses are restricted to reviewed local probes.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

from run_taker_regressions import copy_source, require_complete_result

COLLECTION_SUITES = {
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
}

RETAINED_SUITES = {
    'tests.core.test_council_deadline': 6,
    'tests.llm.test_llm_deadline': 28,
    'tests.ops.test_brain_deadline': 20,
    'tests.ops.test_cycle_deadline': 24,
    'tests.ops.test_file_lock_deadline': 7,
    'tests.ops.test_brain_atomic_deadline': 4,
    'tests.core.test_council_debate_mode': 3,
    'tests.llm.test_council_manager': 12,
    'tests.llm.test_llm_call_tails': 80,
    'tests.llm.test_llm_transport_tails': 36,
    'tests.llm.test_llm_resilience': 16,
    'tests.ops.test_brain_dispatch': 46,
}

LOCK_CHILD = "import fcntl,sys; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read(1)"


def clean_environment(environment):
    env = dict(environment)
    for key in list(env):
        if (key.startswith('ASTRA_') or key in ('GITHUB_TOKEN', 'GH_TOKEN')
                or key.endswith(('API_KEY', 'ACCESS_TOKEN', 'SECRET_KEY'))):
            env.pop(key, None)
    env.update(PYTHONDONTWRITEBYTECODE='1', ASTRA_LEDGER_SYNC_DISABLED='1', OFFLINE_SUITE_RUNNING='1')
    return env


def is_lock_fixture_child(tokens):
    if not isinstance(tokens, (tuple, list)) or len(tokens) != 4:
        return False
    target = Path(str(tokens[3])).resolve()
    return (str(tokens[0]) == sys.executable and tokens[1] == '-c'
            and tokens[2] == LOCK_CHILD and target.is_relative_to(Path(tempfile.gettempdir()).resolve())
            and target.name == '.synthetic.json.lock')


def load_required_suites(specification, loader=None):
    if not specification:
        raise RuntimeError('Empty collection regression contract')
    loader = loader or unittest.TestLoader()
    suites = []
    for name, minimum in specification.items():
        suite = loader.loadTestsFromName(name)
        if minimum < 1 or suite.countTestCases() < minimum or loader.errors:
            raise RuntimeError(f'Incomplete discovery: {name} requires {minimum} tests')
        suites.append((name, minimum, suite))
    return suites


def worker(source, retained, sha):
    sys.path[:0] = [str(source), str(source / 'scripts')]
    os.chdir(source)
    os.environ['ASTRA_DATA_DIR'] = str(source / 'data')
    # Install before importing tests/__init__ or any application module.
    spec = importlib.util.spec_from_file_location('_collection_offline_guard', source / 'tests/offline_suite.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Guard(module.OfflineGuard):
        def audit(self, event, args):
            if event == 'subprocess.Popen' and retained and is_lock_fixture_child(args[1]):
                self.children.append('python synthetic flock fixture')
                return
            super().audit(event, args)

    guard = Guard(source)
    guard.DIAG_LOG = str(source.parent / 'offline-guard.log')
    guard.install()
    guard.prove_connection_guard()
    import requests
    import urllib.request
    transports = []
    original_request = requests.Session.request
    original_urlopen = urllib.request.urlopen

    def check(url, label):
        host = urlsplit(str(getattr(url, 'full_url', url))).hostname or ''
        if not guard._is_loopback_host(host):
            transports.append(label)
            raise RuntimeError('Offline regression blocked pre-transport: ' + label)

    def request(self, method, url, *args, **kwargs):
        check(url, 'requests')
        return original_request(self, method, url, *args, **kwargs)

    def urlopen(url, *args, **kwargs):
        check(url, 'urllib')
        return original_urlopen(url, *args, **kwargs)

    requests.Session.request = request
    urllib.request.urlopen = urlopen
    for probe in (lambda: requests.get('https://192.0.2.1/'), lambda: urllib.request.urlopen('https://192.0.2.1/')):
        try:
            probe()
        except RuntimeError as error:
            assert 'blocked pre-transport' in str(error)
        else:
            raise RuntimeError('Pre-transport guard failed self-test')
    assert transports == ['requests', 'urllib']
    transports.clear()
    specification = RETAINED_SUITES if retained else COLLECTION_SUITES
    label = 'RETAINED_DEADLINE_COUNCIL' if retained else 'COLLECTION'
    total = 0
    try:
        for name, minimum, suite in load_required_suites(specification):
            discovered = suite.countTestCases()
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            total += result.testsRun
            print(f'{label}_MODULE module={name} minimum={minimum} discovered={discovered} '
                  f'tests={result.testsRun} failures={len(result.failures)} errors={len(result.errors)} '
                  f'skips={len(result.skipped)} expected_failures={len(result.expectedFailures)} '
                  f'unexpected_successes={len(result.unexpectedSuccesses)}', flush=True)
            require_complete_result(result, max(minimum, discovered))
    finally:
        if not guard.report() or transports:
            raise RuntimeError('Offline regression guard recorded forbidden activity')
    print(f'{label}_PR_PASS sha={sha} modules={len(specification)} tests={total} skips=0 expected_failures=0 release_eligibility=not_granted', flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a', encoding='utf-8') as out:
            out.write(f'{label}: {total} tests passed across {len(specification)} modules, zero skips/expected failures. PR head: `{sha}`. This is not release approval.\n\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retained', action='store_true')
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--sha', default=os.environ.get('ASTRA_CI_EXPECTED_SHA', 'local-uncommitted-validation'))
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Collection regressions require Linux')
    if args.worker:
        worker(args.worker.resolve(), args.retained, args.sha)
        return
    source = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='astra-collection-pr-') as tmp:
        scratch = Path(tmp) / 'source'
        copy_source(source, scratch)
        command = [sys.executable, str(scratch / '.github/scripts/run_collection_regressions.py'),
                   '--worker', str(scratch), '--sha', args.sha]
        if args.retained:
            command.append('--retained')
        subprocess.run(command, check=True, env=clean_environment(os.environ), cwd=scratch, timeout=420)


if __name__ == '__main__':
    main()
