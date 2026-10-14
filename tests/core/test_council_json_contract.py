"""Offline contract tests for JSON proposals and legacy consensus compatibility."""
from __future__ import annotations

import json
import unittest

from astra_backend.council.consensus import (
    calculate_council_consensus,
    parse_trader_proposal,
    parse_trader_quote_table,
)
from tests.core.test_council_consensus import SAMPLE_PROPOSAL_TABLE


BTC = "BTC-USDT-SWAP"
ETH = "ETH-USDT-SWAP"
SOL = "SOL-USDT-SWAP"


def proposal(decisions):
    return json.dumps({"macro_assessment": "仅测试，无交易调用", "position_management": [],
                       "pending_orders_management": [], "decisions": decisions})


def trade(action="BUY_LONG", **updates):
    value = {"action": action, "entry_price": 100.0, "stop_loss_price": 90.0,
             "take_profit_price": 120.0, "margin_usdt": 10.0, "confidence": 82,
             "summary_reason": "测试用显式数值"}
    value.update(updates)
    return value


class TraderJSONContractTests(unittest.TestCase):
    def assertInvalid(self, content, expected=None):
        parsed = parse_trader_proposal(content, expected)
        self.assertFalse(parsed["valid"], parsed)
        self.assertEqual(parsed["status"], "invalid")
        self.assertTrue(parsed["errors"])
        # The historical list API must not leak partial votes on invalid input.
        if expected is None:
            self.assertEqual(parse_trader_quote_table(content), [])
        return parsed

    def test_deployed_decisions_schema_and_aliases(self):
        payload = proposal({BTC: trade(), ETH: {"action": "WAIT", "reasoning": "缺少优势"},
                            SOL: {"action": "SELL_SHORT", "limit_price": 185.5,
                                  "stop_loss": 192, "take_profit": 172.5,
                                  "margin_usd": 120, "confidence": 75, "reason": "阻力"}})
        parsed = parse_trader_proposal(payload, [BTC, ETH, SOL])
        self.assertTrue(parsed["valid"], parsed)
        self.assertEqual(parsed["format"], "json")
        self.assertEqual(parsed["missing_symbols"], [])
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["status"], "ok")
        btc, eth, sol = parsed["quotes"]
        self.assertEqual(btc, {"symbol": BTC, "action": "BUY_LONG", "limit_price": 100,
                               "stop_loss": 90, "take_profit": 120, "margin": 10,
                               "confidence": 82, "reason": "测试用显式数值"})
        self.assertEqual(eth["action"], "WAIT")
        self.assertIsNone(eth["limit_price"])
        self.assertEqual(sol["margin"], 120)
        self.assertEqual(sol["action"], "SELL_SHORT")
        self.assertEqual(parse_trader_quote_table(payload), parsed["quotes"])

    def test_fenced_json_with_surrounding_analysis(self):
        for prefix in ("```json\n", "```JSON\n", "```\n"):
            with self.subTest(prefix=prefix):
                text = "分析：缺乏概率优势。\n" + prefix + proposal({BTC: {"action": "WAIT"}}) + "\n```\n结束。"
                parsed = parse_trader_proposal(text)
                self.assertTrue(parsed["valid"], parsed)
                self.assertEqual(parsed["quotes"][0]["action"], "WAIT")

    def test_bare_mapping_and_explicit_symbol_lists(self):
        examples = [
            {BTC: trade()},
            [{"symbol": BTC, **trade()}],
            {"decisions": [{"instId": BTC, **trade()}]},
            {"symbol": BTC, **trade()},
        ]
        for example in examples:
            with self.subTest(example=example):
                parsed = parse_trader_proposal(json.dumps(example))
                self.assertTrue(parsed["valid"], parsed)
                self.assertEqual(parsed["quotes"][0]["symbol"], BTC)

    def test_valid_wait_can_omit_prices_or_use_explicit_zero(self):
        for decision in ({"action": "WAIT"},
                         {"action": "WAIT", "entry_price": 0, "stop_loss_price": 0,
                          "take_profit_price": 0, "margin_usdt": 0},
                         {"action": "WAIT", "entry_price": None, "margin_usdt": None}):
            with self.subTest(decision=decision):
                parsed = parse_trader_proposal(proposal({BTC: decision}), [BTC])
                self.assertTrue(parsed["valid"], parsed)
                self.assertEqual(parsed["quotes"][0]["action"], "WAIT")

    def test_missing_trade_fields_remain_missing_for_semantic_validation(self):
        for action in ("BUY_LONG", "SELL_SHORT"):
            parsed = parse_trader_proposal(proposal({BTC: {"action": action}}))
            self.assertTrue(parsed["valid"], parsed)
            quote = parsed["quotes"][0]
            self.assertEqual(quote["action"], action)
            for field in ("limit_price", "stop_loss", "take_profit", "margin"):
                self.assertIsNone(quote[field])
            self.assertNotIn("alpha", quote)
            self.assertNotIn("leverage", quote)

    def test_expected_symbol_coverage_does_not_fabricate_wait(self):
        parsed = self.assertInvalid(proposal({BTC: {"action": "WAIT"}}), [BTC, ETH])
        self.assertEqual(parsed["missing_symbols"], [ETH])
        self.assertEqual([q["symbol"] for q in parsed["quotes"]], [BTC])
        self.assertIn("missing symbols", " ".join(parsed["errors"]))

    def test_unexpected_symbols_are_rejected_when_pool_is_known(self):
        parsed = self.assertInvalid(proposal({BTC: {"action": "WAIT"}, ETH: trade()}), [BTC])
        self.assertEqual(parsed["unexpected_symbols"], [ETH])
        self.assertEqual(parsed["missing_symbols"], [])
        self.assertEqual(len(parsed["quotes"]), 2)  # Diagnostic only, never a fabricated vote.

    def test_invalid_expected_symbols_types_do_not_raise(self):
        for expected in (1, True, BTC, {BTC: "WAIT"}, [None], [1], ["not-a-symbol"]):
            with self.subTest(expected=expected):
                parsed = self.assertInvalid(proposal({BTC: {"action": "WAIT"}}), expected)
                self.assertIn("expected_symbols", " ".join(parsed["errors"]))

    def test_blank_empty_or_non_proposal_output_is_invalid(self):
        for text in (None, 0, {}, [], "", "  \n", "纯分析，无标的表格", "WAIT",
                     '"WAIT"', "null", "{}", "[]", '{"decisions": {}}',
                     '{"macro_assessment": "WAIT"}', "结论 BTC-USDT-SWAP: WAIT 只是例子"):
            with self.subTest(text=text):
                self.assertInvalid(text)

    def test_json_type_errors_and_unknown_actions_are_rejected(self):
        for value in (None, True, "WAIT", 5, ["WAIT"], {"BTC": {"action": "WAIT"}},
                      {BTC: None}, {BTC: "WAIT"}, {BTC: {}}, {BTC: {"action": False}},
                      {BTC: {"action": "HOLD"}}, {BTC: {"action": "WAITING"}},
                      [{"action": "WAIT"}], [{"symbol": BTC, "action": "CANCEL"}]):
            with self.subTest(value=value):
                self.assertInvalid(proposal(value))

    def test_known_numeric_fields_reject_wrong_types_and_nonfinite(self):
        for field in ("entry_price", "limit_price", "stop_loss_price", "stop_loss",
                      "take_profit_price", "take_profit", "margin_usdt", "margin_usd",
                      "margin", "confidence"):
            for bad in (True, "100", "unknown", [], {}, float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value=bad):
                    self.assertInvalid(proposal({BTC: {"action": "BUY_LONG", field: bad}}))
        self.assertInvalid('{"decisions":{"BTC-USDT-SWAP":{"action":"WAIT","confidence":1e999}}}')
        self.assertInvalid(proposal({BTC: {"action": "WAIT", "confidence": 101}}))
        self.assertInvalid(proposal({BTC: {"action": "WAIT", "confidence": -1}}))

    def test_wrong_reason_and_conflicting_symbol_fields_are_rejected(self):
        for key in ("reason", "reasoning", "summary_reason"):
            self.assertInvalid(proposal({BTC: {"action": "WAIT", key: ["no edge"]}}))
        self.assertInvalid(proposal({BTC: {"symbol": ETH, "action": "WAIT"}}))
        self.assertInvalid(proposal({BTC: {"instId": False, "action": "WAIT"}}))
        self.assertInvalid(json.dumps([{"symbol": BTC, "instId": ETH, "action": "WAIT"}]))

    def test_truncated_json_never_falls_back_to_embedded_wait_text(self):
        for text in ('{"decisions": {"BTC-USDT-SWAP": {"action": "WAIT"}',
                     '{"decisions": {\nBTC-USDT-SWAP: WAIT',
                     '```json\n{"decisions": {\nBTC-USDT-SWAP: WAIT',
                     '```json\n{"decisions": {\nBTC-USDT-SWAP: WAIT\n```',
                     '分析后提案：\n{\nBTC-USDT-SWAP: WAIT',
                     '```\n{\nBTC-USDT-SWAP: WAIT',
                     proposal({BTC: {"action": "WAIT"}}) + " trailing junk"):
            with self.subTest(text=text):
                self.assertInvalid(text)

    def test_duplicate_json_keys_and_conflicting_rows_are_rejected(self):
        self.assertInvalid('{"decisions":{"BTC-USDT-SWAP":{"action":"BUY_LONG","action":"WAIT"}}}')
        self.assertInvalid('{"decisions":{"BTC-USDT-SWAP":{"action":"BUY_LONG"},"BTC-USDT-SWAP":{"action":"WAIT"}}}')
        self.assertInvalid(json.dumps([{"symbol": BTC, "action": "BUY_LONG"},
                                       {"symbol": BTC, "action": "SELL_SHORT"}]))
        self.assertInvalid(proposal({BTC: {"action": "WAIT"}, BTC.lower(): {"action": "BUY_LONG"}}))
        self.assertInvalid("BTC-USDT-SWAP | WAIT | - | - | - | - | 50\n"
                           "BTC-USDT-SWAP | BUY_LONG | 100 | 90 | 120 | 10 | 80")
        self.assertInvalid("BTC-USDT-SWAP: WAIT\nBTC-USDT-SWAP: BUY_LONG")

    def test_identical_repeated_rows_are_deduplicated(self):
        parsed = parse_trader_proposal(json.dumps([{"symbol": BTC, "action": "WAIT"}] * 2))
        self.assertTrue(parsed["valid"], parsed)
        self.assertEqual(len(parsed["quotes"]), 1)

    def test_conflicting_numeric_aliases_are_rejected(self):
        for aliases in (("entry_price", "limit_price"), ("stop_loss_price", "stop_loss"),
                        ("take_profit_price", "take_profit"), ("margin_usdt", "margin_usd"),
                        ("margin_usdt", "margin")):
            first, second = aliases
            self.assertInvalid(proposal({BTC: {"action": "BUY_LONG", first: 10, second: 20}}))
            parsed = parse_trader_proposal(proposal({BTC: {"action": "BUY_LONG", first: 10, second: 10}}))
            self.assertTrue(parsed["valid"], parsed)

    def test_multiple_fenced_proposals_are_ambiguous(self):
        text = "```json\n" + proposal({BTC: {"action": "WAIT"}}) + "\n```"
        self.assertInvalid(text + "\n" + text)
        self.assertInvalid(text + '\n```json\n{"decisions":')
        self.assertInvalid(text + '\n{"decisions":')
        self.assertInvalid('{"decisions":\n' + text)

    def test_raw_json_reason_with_fences_does_not_override_payload(self):
        parsed = parse_trader_proposal(proposal({BTC: {"action": "WAIT", "reason": '```{"action":"UNKNOWN"}```'}}))
        self.assertTrue(parsed["valid"], parsed)
        self.assertEqual(parsed["quotes"][0]["action"], "WAIT")

    def test_table_compatibility_and_formatted_numbers(self):
        parsed = parse_trader_proposal(SAMPLE_PROPOSAL_TABLE)
        self.assertTrue(parsed["valid"], parsed)
        self.assertEqual(parsed["format"], "table")
        self.assertEqual(len(parsed["quotes"]), 4)
        parsed = parse_trader_proposal("| **BTC-USDT-SWAP** | 做多 | $78,250 | 76,500 | 81,750 | 150 | 82% | 回踩 |")
        self.assertTrue(parsed["valid"], parsed)
        self.assertEqual(parsed["quotes"][0]["limit_price"], 78250)
        self.assertEqual(parsed["quotes"][0]["confidence"], 82)

    def test_bad_or_truncated_table_does_not_create_wait(self):
        for text in ("BTC-USDT-SWAP | UNKNOWN | - | - | - | - | 50",
                     "BTC-USDT-SWAP | WAITING | - | - | - | - | 50",
                     "BTC-USDT-SWAP | WAIT | -",
                     "BTC-USDT-SWAP | BUY_LONG | bad | 90 | 120 | 10 | 80",
                     "BTC-USDT-SWAP | WAIT | - | - | - | - | NaN",
                     "BTC-USDT-SWAP: UNKNOWN",
                     "BTC-USDT-SWAP | WAIT | - | - | - | - | 50\nETH-USDT-SWAP: UNKNOWN",
                     "BTC-USDT-SWAP | WAIT | - | - | - | - | 50\nBTC-USDT-SWAP: BUY_LONG"):
            with self.subTest(text=text):
                self.assertInvalid(text)

    def test_invalid_partial_json_is_rejected_atomically(self):
        self.assertInvalid(proposal({BTC: trade(), ETH: {"action": "UNKNOWN"}}))


class JSONConsensusRegressionTests(unittest.TestCase):
    def test_three_json_seats_generate_nonzero_consensus_and_preserve_weights(self):
        roles = {"a": {"name": "甲", "weight": 0.35}, "b": {"name": "乙", "weight": 0.35},
                 "c": {"name": "丙", "weight": 0.30}}
        contents = {"a": proposal({BTC: trade(), ETH: {"action": "WAIT"}}),
                    "b": proposal({BTC: trade(), ETH: {"action": "WAIT"}}),
                    "c": proposal({BTC: trade(), ETH: {"action": "WAIT"}})}
        # Reproduce the observed scale without network, clocks or dependencies.
        for _ in range(35):
            result = calculate_council_consensus({key: {"status": "ok", "content": text}
                                                 for key, text in contents.items()}, roles)
            self.assertEqual(result["total_symbols_evaluated"], 2)
            self.assertEqual(result["strong_consensus_symbols"], [BTC])
            self.assertEqual(result["by_symbol"][BTC]["weighted_confidence"], 82)
            self.assertEqual(result["by_symbol"][BTC]["agreement_score"], 1.0)
            self.assertEqual(result["by_symbol"][ETH]["actions_count"]["WAIT"], 3)

    def test_invalid_proposals_and_explicit_non_ok_statuses_never_vote(self):
        for status in ("error", "semantic_invalid", "partial", "", None):
            with self.subTest(status=status):
                result = calculate_council_consensus(
                    {"a": {"status": status, "content": proposal({BTC: trade()})}}, {})
                self.assertEqual(result["total_symbols_evaluated"], 0)
        result = calculate_council_consensus(
            {"a": {"status": "ok", "content": proposal({BTC: trade(), ETH: {"action": "BAD"}})}}, {})
        self.assertEqual(result["total_symbols_evaluated"], 0)

    def test_legacy_proposal_without_status_still_votes(self):
        result = calculate_council_consensus({"a": {"content": SAMPLE_PROPOSAL_TABLE}}, {})
        self.assertEqual(result["total_symbols_evaluated"], 4)


if __name__ == "__main__":
    unittest.main()
