"""Focused offline proposal/input-contract gate; not a full-repository CI claim."""
from pathlib import Path
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["OFFLINE_SUITE_RUNNING"] = "1"

from tests.offline_suite import OfflineGuard

TEST_MODULES = [
    "tests.core.test_council_json_contract",
    "tests.core.test_proposal_semantic_contract",
    "tests.core.test_proposal_replay",
    "tests.core.test_council_consensus",
    "tests.core.test_council_deadline",
    "tests.core.test_council_attribution",
    "tests.core.test_council_debate_mode",
    "tests.core.test_debate_council_verdict",
    "tests.core.test_debate_council_preflight",
    "tests.core.test_debate_seat_prompt",
    "tests.llm.test_council_manager",
    "tests.llm.test_llm_caching_enhancements",
    "tests.ops.test_brain_dispatch",
    "tests.llm.test_closed_candle_contract",
    "tests.llm.test_alpha_transport_contract",
    "tests.ops.test_brain_packages",
    "tests.trading.test_signal_snapshot_schema",
    "tests.extraction.test_factors_candles_15m_extraction",
    "tests.extraction.test_brain_package_extraction",
    "tests.llm.test_prompt_rendering_isolated",
    "tests.trading.test_factor_assembly_paths",
    "tests.ops.test_factor_library",
    "tests.llm.test_prompt_math_foundations",
    "tests.llm.test_prompt_cache_prefix_contract",
    "tests.llm.test_brain_prompt_module",
    "tests.audit.test_module_free_names",
    "tests.audit.test_no_vacuous_assertions",
    "tests.audit.test_no_contract_units_in_user_facing_copy",
]


def main():
    guard = OfflineGuard(ROOT).install()
    guard.prove_connection_guard()
    suite = unittest.defaultTestLoader.loadTestsFromNames(TEST_MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    clean = guard.report()
    return 0 if result.wasSuccessful() and clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
