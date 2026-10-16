"""Offline source/rendering contract for the allpattern_swing direction table.

This reads the shipped JSON, not a locally edited or active live profile. The
small table parser checks prompt wording and zero-boundary coverage; it is not
a trading engine, an LLM evaluation, or a profitability backtest. Existing hard
rules and short-entry gates remain authoritative.
"""
from __future__ import annotations

import copy
import json
import operator
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.prompt_library as prompts
import scripts.ai_brain_trader as brain


ROOT = Path(__file__).resolve().parents[2]
# Read the version-controlled seed at collection time, before per-test IO guards.
PROFILE = json.loads((ROOT / "data/prompt_library.json").read_text(encoding="utf-8"))[
    "profiles"]["allpattern_swing"]
TABLE_ROW = re.compile(
    r"^- (BULL_EXPANDING|BULL_EXHAUSTING|BEAR_EXPANDING|BEAR_EXHAUSTING)"
    r" ＋ alpha ([<>≤≥]) 0\s*⇒ (.+)$", re.MULTILINE)
COMPARISONS = {"<": operator.lt, ">": operator.gt,
               "≤": operator.le, "≥": operator.ge}
FACTOR_TITLE = "======================= 【因子证据与决策优先级】 ======================="


class PromptExhaustionConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.profile = copy.deepcopy(PROFILE)
        self.factor = next(m["content"] for m in self.profile["pipelines"]["trading_system"]
                           if m["id"] == "custom-ts-factor")
        self.entry = next(m["content"] for m in self.profile["pipelines"]["trading_system"]
                          if m["id"] == "custom-ts-entry")
        # Exercise the actual production entry point without reading an admin
        # override or loading the active/local profile. No model is requested.
        with patch.object(brain, "read_prompt_override", return_value=""), \
                patch.object(brain, "active_profile", side_effect=AssertionError("explicit profile required")):
            self.rendered = brain.get_effective_system_prompt(self.profile, context={})
        self.sources = {
            "flat JSON": self.profile["trading_system"],
            "factor module": self.factor,
            "effective system prompt": self.rendered,
        }

    def row_for(self, text, state, alpha):
        rows = TABLE_ROW.findall(text)
        self.assertEqual(len(rows), 8, "All four states need two disjoint alpha intervals")
        matches = [result for name, op, result in rows
                   if name == state and COMPARISONS[op](alpha, 0)]
        self.assertEqual(len(matches), 1, (state, alpha, matches))
        return matches[0]

    def test_direction_table_negative_zero_positive_matrix(self):
        expected = {
            "BULL_EXPANDING": ("WAIT", "WAIT", "允许做多"),
            "BULL_EXHAUSTING": ("WAIT", "WAIT", "允许做多"),
            "BEAR_EXPANDING": ("允许做空", "WAIT", "WAIT"),
            "BEAR_EXHAUSTING": ("WAIT", "WAIT", "WAIT"),
        }
        for label, text in self.sources.items():
            for state, actions in expected.items():
                for alpha, action in zip((-1.0, 0.0, 1.0), actions):
                    with self.subTest(source=label, state=state, alpha=alpha):
                        self.assertTrue(self.row_for(text, state, alpha).startswith(action))

    def test_bear_exhaustion_negative_alpha_is_unconditional_rule_a_wait(self):
        for label, text in self.sources.items():
            for alpha in (-1.0, -1e-12):
                with self.subTest(source=label, alpha=alpha):
                    self.assertEqual(self.row_for(text, "BEAR_EXHAUSTING", alpha), "WAIT（硬禁令 A）")

    def test_bear_exhaustion_zero_alpha_is_in_rule_a(self):
        for label, text in self.sources.items():
            with self.subTest(source=label):
                self.assertEqual(self.row_for(text, "BEAR_EXHAUSTING", 0.0), "WAIT（硬禁令 A）")

    def test_bear_exhaustion_positive_alpha_wait_does_not_misattribute_rule_a(self):
        for label, text in self.sources.items():
            for alpha in (1e-12, 1.0):
                with self.subTest(source=label, alpha=alpha):
                    row = self.row_for(text, "BEAR_EXHAUSTING", alpha)
                    self.assertEqual(row, "WAIT（未列入做多允许条件，且不满足做空门槛①②）")
                    self.assertNotIn("禁令 A", row)

    def test_bull_exhaustion_positive_alpha_still_allows_structural_pullback_long(self):
        for label, text in self.sources.items():
            with self.subTest(source=label):
                self.assertEqual(self.row_for(text, "BULL_EXHAUSTING", 1.0),
                                 "允许做多（须回踩结构位，禁止贴盘追）")

    def test_bear_expanding_negative_alpha_still_requires_four_short_gates(self):
        for label, text in self.sources.items():
            with self.subTest(source=label):
                self.assertEqual(self.row_for(text, "BEAR_EXPANDING", -1.0),
                                 "允许做空（另需通过做空四门槛）")

    def test_bear_expanding_zero_alpha_remains_wait_despite_short_gate_two(self):
        # Gate two's <= 0 is necessary, not sufficient; the direction table
        # already excludes zero. This repair must not silently loosen it.
        for label, text in self.sources.items():
            with self.subTest(source=label):
                self.assertEqual(self.row_for(text, "BEAR_EXPANDING", 0.0),
                                 "WAIT（多空分歧，不出手）")

    def test_flat_source_and_compiled_modules_are_identical(self):
        self.assertEqual(self.profile["trading_system"],
                         prompts.compile_modules(self.profile["pipelines"]["trading_system"]))

    def test_actual_renderer_preserves_factor_module_once(self):
        self.assertEqual(self.rendered.count(FACTOR_TITLE), 1)
        self.assertIn(self.factor, self.rendered)
        self.assertIn(self.entry, self.rendered)
        self.assertEqual(self.rendered.count(brain.READONLY_OUTPUT_SCHEMA_TITLE), 1)

    def test_clean_resolve_and_flat_import_render_same_direction_table(self):
        cleaned = prompts._clean_profile(copy.deepcopy(self.profile), "allpattern_swing")
        resolved = prompts.resolve_profile(cleaned)
        flat = copy.deepcopy(self.profile)
        flat.pop("pipelines")
        flat["editor_mode"] = "advanced"
        flat = prompts._clean_profile(flat, "allpattern_swing")
        expected = TABLE_ROW.findall(self.factor)
        for label, profile in (("clean", cleaned), ("resolve", resolved), ("flat import", flat)):
            with self.subTest(path=label), patch.object(brain, "read_prompt_override", return_value=""):
                rendered = brain.get_effective_system_prompt(profile, context={})
                self.assertEqual(TABLE_ROW.findall(profile["trading_system"]), expected)
                self.assertEqual(TABLE_ROW.findall(rendered), expected)

    def test_hard_rule_a_and_all_four_short_gates_are_unchanged(self):
        required = (
            "- **禁令 A｜动能衰竭不进场**：1H MACD 处于 EXHAUSTING（柱体收缩／加速度反向）且\n"
            "  composite_alpha_score ≤ 0 ⇒ 该标的**禁止开新仓**（多空皆禁）；必须等待动能重新扩张，\n"
            "  或价格回踩至结构位且 alpha 转正后再评估。",
            "① 1H MACD 为 BEAR_EXPANDING（动能向下扩张，**不接受 BEAR_EXHAUSTING**）；",
            "② composite_alpha_score ≤ 0 且 elite_divergence 不是 ALIGNED_LONG；",
            "③ 价格位于 24H VWAP 下方，或已有效跌破 VPVR POC；",
            "④ 1H RSI < 55 且 ADX ≥ 20。",
            "⇒ 四条同时成立才可输出 SELL_SHORT；任一条不满足一律 WAIT。",
        )
        for text in (self.profile["trading_system"], self.entry, self.rendered):
            for rule in required:
                with self.subTest(rule=rule):
                    self.assertIn(rule, text)

    def test_unlisted_directions_and_missing_evidence_still_wait(self):
        for label, text in self.sources.items():
            with self.subTest(source=label):
                self.assertIn("不在允许列的坚决 WAIT", text)
                self.assertIn("- 任一梯队显示缺失值 ⇒ WAIT（证据不闭环）", text)

    def test_no_bear_exhaustion_row_retains_conditional_short_permission(self):
        for label, text in self.sources.items():
            for state, _, result in TABLE_ROW.findall(text):
                if state == "BEAR_EXHAUSTING":
                    with self.subTest(source=label, result=result):
                        self.assertNotIn("允许做空", result)
                        self.assertNotIn("通常", result)

    def test_repaired_profile_passes_validation_without_mutating_source(self):
        before = copy.deepcopy(self.profile)
        report = prompts.validate_profile(self.profile)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(self.profile, before)


if __name__ == "__main__":
    unittest.main()
