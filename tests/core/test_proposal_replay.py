"""Replay is deterministic and cannot mistake synthetic cases for historical data."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.replay_proposal_contract import replay_proposals


class ProposalReplayTests(unittest.TestCase):
    def test_fixture_classifies_without_request_or_order_side_effects(self):
        fixture = Path(__file__).resolve().parents[1] / "data/proposal_contract_synthetic.json"
        payload = json.loads(fixture.read_text())
        with patch("astra_backend.llm_manager.execute_llm_request", side_effect=AssertionError("no LLM")):
            result = replay_proposals(payload)
        self.assertIn("synthetic", result["provenance"])
        self.assertEqual(result["round_count"], 3)
        self.assertEqual(result["proposal_count"], 9)
        self.assertEqual(result["valid_proposals"], 5)
        self.assertEqual(result["invalid_proposals"], 4)
        self.assertEqual([r["consensus_symbol_count"] for r in result["rounds"]], [11, 0, 11])
        self.assertTrue(all(a == "WAIT" for r in result["rounds"] for a in r["dominant_actions"].values()))
        self.assertEqual(replay_proposals(payload), result)

    def test_coverage_requires_known_expected_symbols(self):
        with self.assertRaises(ValueError):
            replay_proposals({"rounds": [{"proposals": {}}]})


if __name__ == "__main__":
    unittest.main()
