"""Synthetic, offline response-contract cases. These are not market backtests."""
import json
import unittest
from unittest.mock import patch

from astra_backend.council import debate
from astra_backend.council.consensus import calculate_council_consensus
from astra_backend import deadline


BTC = "BTC-USDT-SWAP"
ETH = "ETH-USDT-SWAP"
RESOLVED = {"model": "fixture-model", "base_url": "https://invalid.example",
            "api_key": "fixture", "api_format": "openai_chat", "effort": "high",
            "requested": "fixture-model", "registered": True, "fallback": False,
            "reason": "fixture"}


def wait_content(symbols=(BTC, ETH)):
    return json.dumps({"decisions": {s: {"action": "WAIT", "confidence": 55,
                                       "reasoning": "Fixture: confirmation missing"}
                                    for s in symbols}})


class ProposalSemanticContractTests(unittest.TestCase):
    def call(self, content, retry=False):
        responses = [(content, "analysis", {"_astra_trace": {"model": "actual"}}, 13)]
        if retry:
            responses.insert(0, RuntimeError("HTTP 502"))
        clock = [10.0]
        with patch("astra_backend.llm_manager.execute_llm_request", side_effect=responses) as request, \
             patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch.object(deadline.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            result = debate._call_single_trader(
                lambda _: RESOLVED, "seat", {"weight": 0.3}, "market", "rules", 60,
                {"proposal_symbols": [BTC, ETH]})
        return result, request.call_count

    def test_complete_wait_is_semantic_success_and_real_consensus(self):
        result, calls = self.call(wait_content())
        self.assertEqual(calls, 1)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["semantic_status"], "ok")
        self.assertEqual(result["transport_status"], "ok")
        self.assertEqual(result["weight"], 0.3)
        consensus = calculate_council_consensus({"seat": result}, {"seat": {"weight": 0.3}})
        self.assertEqual(consensus["total_symbols_evaluated"], 2)
        self.assertTrue(all(x["dominant_action"] == "WAIT" for x in consensus["by_symbol"].values()))

    def test_empty_prose_and_truncated_are_not_success_or_wait_votes(self):
        for content in ("", "  \n", None, "analysis only", '{"decisions": {', '{"decisions": {}}'):
            with self.subTest(content=content):
                result, calls = self.call(content)
                self.assertEqual(calls, 1, "semantic invalidity must not silently consume another LLM request")
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["transport_status"], "ok")
                self.assertEqual(result["semantic_status"], "invalid")
                self.assertEqual(result["weight"], 0.0)
                self.assertEqual(result["latency_ms"], 13)
                self.assertEqual(result["model_used"], "actual")
                self.assertTrue(result["validation"]["errors"])
                self.assertEqual(calculate_council_consensus({"seat": result}, {})["total_symbols_evaluated"], 0)

    def test_partial_symbol_coverage_never_fabricates_missing_wait(self):
        result, _ = self.call(wait_content((BTC,)))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["validation"]["missing_symbols"], [ETH])
        self.assertEqual([q["symbol"] for q in result["validation"]["quotes"]], [BTC])

    def test_entry_requires_prices_and_margin_but_does_not_change_direction_rules(self):
        entry = {"action": "SELL_SHORT", "confidence": 80, "entry_price": 100,
                 "stop_loss_price": 110, "take_profit_price": 80, "margin_usdt": 20}
        content = json.dumps({"decisions": {BTC: entry}})
        self.assertTrue(debate._validate_proposal(content, {"proposal_symbols": [BTC]})["valid"])
        for key in ("entry_price", "stop_loss_price", "take_profit_price", "margin_usdt"):
            for value in (None, 0, -1, float("inf"), True):
                with self.subTest(key=key, value=value):
                    altered = json.dumps({"decisions": {BTC: {**entry, key: value}}})
                    self.assertFalse(debate._validate_proposal(altered)["valid"])

    def test_retry_output_is_checked_with_same_contract(self):
        result, calls = self.call("", retry=True)
        self.assertEqual(calls, 2)
        self.assertEqual(result["status"], "error")
        result, calls = self.call(wait_content(), retry=True)
        self.assertEqual(calls, 2)
        self.assertEqual(result["status"], "ok")

    def test_invalid_output_is_preserved_for_audit_but_not_sent_as_vote(self):
        raw = 'malformed proposal says BUY_LONG despite missing confirmation'
        result, _ = self.call(raw)
        self.assertEqual(result["content"], raw)
        docket = debate._proposal_docket_text(result)
        self.assertNotIn(raw, docket)
        self.assertIn("非 WAIT 票", docket)

    def test_all_modes_exclude_invalid_raw_text_from_cio_and_peer_dockets(self):
        sentinel = "INVALID_RAW_SENTINEL do not treat this as a vote"
        invalid, _ = self.call(sentinel)
        roles = {"cio": {"is_arbitrator": True}, "long": {"weight": 1},
                 "short": {"weight": 1}, "invalid": {"weight": 1}}
        proposals = {
            "long": {"status": "ok", "content": f"{BTC} | BUY_LONG | 100 | 90 | 120 | 10 | 75 | synthetic"},
            "short": {"status": "ok", "content": f"{BTC} | SELL_SHORT | 100 | 110 | 80 | 10 | 75 | synthetic"},
            "invalid": invalid,
        }
        for mode in ("standard", "cross_examination", "debate"):
            cio_inputs, critique_inputs = [], []
            def request(**kwargs):
                cio_inputs.extend(message["content"] for message in kwargs["messages"])
                return '{"decisions": {}}', "", {}, 1
            def critique(*args):
                critique_inputs.extend(args[2:4])
                return {"status": "ok", "content": "synthetic critique"}
            with self.subTest(mode=mode), \
                 patch("astra_backend.llm_manager.execute_llm_request", request), \
                 patch.object(deadline.time, "sleep", return_value=None):
                _, transcript = debate.execute_council_debate(
                    lambda: {"roles": roles, "consensus_mode": mode}, lambda _: RESOLVED,
                    lambda key, *args: proposals[key], critique, "market", "rules", 60)
            self.assertEqual(transcript["advisors"]["invalid"]["content"], sentinel)
            self.assertTrue(cio_inputs)
            for content in cio_inputs + critique_inputs:
                self.assertNotIn(sentinel, content)
            self.assertTrue(any("非 WAIT 票" in content for content in cio_inputs))
            metrics = transcript["consensus_metrics"]["by_symbol"][BTC]
            self.assertEqual(len(metrics["seat_details"]), 2)
            self.assertEqual(metrics["actions_count"], {"BUY_LONG": 1, "SELL_SHORT": 1, "WAIT": 0})


if __name__ == "__main__":
    unittest.main()
