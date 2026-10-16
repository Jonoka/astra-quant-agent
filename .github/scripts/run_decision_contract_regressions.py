"""Focused offline proposal/input-contract gate; not a full-repository CI claim."""
from pathlib import Path
import importlib.util
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["OFFLINE_SUITE_RUNNING"] = "1"

TEST_SUITES = {
    "tests.core.test_council_json_contract": 30,
    "tests.core.test_proposal_semantic_contract": 12,
    "tests.core.test_proposal_replay": 2,
    "tests.core.test_council_consensus": 7,
    "tests.core.test_council_deadline": 6,
    "tests.core.test_council_attribution": 2,
    "tests.core.test_council_debate_mode": 3,
    "tests.core.test_debate_council_verdict": 7,
    "tests.core.test_debate_council_preflight": 5,
    "tests.core.test_debate_seat_prompt": 4,
    "tests.llm.test_council_manager": 12,
    "tests.llm.test_llm_caching_enhancements": 14,
    "tests.ops.test_brain_dispatch": 46,
    "tests.llm.test_closed_candle_contract": 20,
    "tests.llm.test_alpha_transport_contract": 12,
    "tests.ops.test_brain_packages": 47,
    "tests.trading.test_signal_snapshot_schema": 8,
    "tests.extraction.test_factors_candles_15m_extraction": 30,
    "tests.extraction.test_brain_package_extraction": 14,
    "tests.llm.test_prompt_rendering_isolated": 25,
    "tests.llm.test_prompt_exhaustion_consistency": 14,
    "tests.trading.test_factor_assembly_paths": 27,
    "tests.ops.test_factor_library": 73,
    "tests.llm.test_prompt_math_foundations": 17,
    "tests.llm.test_prompt_cache_prefix_contract": 6,
    "tests.llm.test_brain_prompt_module": 33,
    "tests.audit.test_module_free_names": 3,
    "tests.audit.test_no_vacuous_assertions": 5,
    "tests.audit.test_no_contract_units_in_user_facing_copy": 4,
}


def load_required_suites(specification, loader=None):
    """Each named module must satisfy its independently frozen minimum."""
    if not specification:
        raise RuntimeError("Empty decision regression contract")
    loader = loader or unittest.TestLoader()
    suite = unittest.TestSuite()
    for name, minimum in specification.items():
        loaded = loader.loadTestsFromName(name)
        if type(minimum) is not int or minimum < 1 or loaded.countTestCases() < minimum or loader.errors:
            raise RuntimeError(f"Incomplete discovery: {name} requires {minimum} tests")
        suite.addTests(loaded)
    return suite


def require_complete_result(result, minimum):
    if (not result.wasSuccessful() or result.testsRun < minimum
            or result.skipped or result.expectedFailures):
        raise RuntimeError("Decision gate failed, incomplete, skipped or expected-failed")


def main():
    # Install before importing tests/__init__ or any application module.
    spec = importlib.util.spec_from_file_location("_decision_offline_guard", ROOT / "tests/offline_suite.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    guard = module.OfflineGuard(ROOT).install()
    guard.prove_connection_guard()
    suite = load_required_suites(TEST_SUITES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    clean = guard.report()
    require_complete_result(result, sum(TEST_SUITES.values()))
    if not clean:
        raise RuntimeError("Decision gate attempted network or protected resource mutation")
    print(f"DECISION_CI_RESULT sha={os.environ.get('ASTRA_CI_EXPECTED_SHA', 'local-validation')} "
          f"modules={len(TEST_SUITES)} tests={result.testsRun} skips=0 expected_failures=0 "
          "release_eligibility=not_granted", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
