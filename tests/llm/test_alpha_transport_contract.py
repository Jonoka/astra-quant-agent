"""Synthetic, offline regression for the factor snapshot -> final prompt contract."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.brain import packages, prompt
from scripts import evolution_shield, prompt_library
from scripts.trader.signal_snapshot import build_signal_snapshot


MISSING = object()
BAD_VALUES = [None, True, False, "0", "1.5", "", "bad", [], {},
              float("nan"), float("inf"), float("-inf")]


class AlphaTransportContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "factor_library_snapshot.json"

    def write_snapshot(self, alpha=MISSING, timestamp=1000, inst_id="BTC-USDT-SWAP", tiers=True):
        item = {"instId": inst_id}
        if tiers:
            item["trend_momentum"] = {"macd_hist": 12.5}
        if alpha is not MISSING:
            item["composite_alpha_score"] = alpha
        payload = {"instruments": [
            {"instId": "ETH-USDT-SWAP", "composite_alpha_score": 88,
             "trend_momentum": {"macd_hist": 99}}, item]}
        if timestamp is not MISSING:
            payload["timestamp"] = timestamp
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def package(self):
        # Exercise actual package assembly while all external market reads are stubs.
        with patch.dict("os.environ", {"ASTRA_DATA_DIR": str(self.root)}), \
                patch.object(packages, "public_json_get", return_value={"code": "0", "data": []}), \
                patch.object(packages.time, "time", return_value=2000), \
                patch("builtins.print"):
            return packages.fetch_single_instrument_package(
                {"instId": "BTC-USDT-SWAP", "name": "BTC", "type": "crypto", "precision": 2},
                fetch_candles=lambda *a, **kw: [],
                fetch_single_indicator=lambda *a, **kw: {})

    def render(self, pkgs):
        # Actual renderer AND actual module layout; only memory/account strategy context
        # is synthetic. No model is invoked, and no production profile is loaded.
        profile = {"name": "synthetic", "pipelines": {"trading_user": [
            {"id": "synthetic-market", "title": "Market", "source": "custom",
             "enabled": True, "content": "{{market_matrix}}"}]}}
        with patch.object(evolution_shield, "render_trading_memory", return_value=""), \
                patch.object(prompt_library, "BASELINE_FILE", self.root / "absent-profile.json"), \
                patch.object(prompt_library, "LOCAL_FILE", self.root / "absent-local.json"):
            return prompt.construct_full_market_prompt(
                pkgs, current_time_str="2026-10-09 17:00:00", active_positions_detail=[],
                pending_orders_detail=[], safe_float=lambda v: float(v or 0),
                sl_atr_mult_for=lambda p: 1.5, build_risk_budget_text=lambda x: "synthetic",
                active_profile=lambda: profile, apply_module_layout=prompt_library.apply_module_layout,
                system_version="synthetic", ai_memory_md_file=str(self.root / "memory.md"),
                ai_memory_file=str(self.root / "memory.json"),
                news_sentiment_file=str(self.root / "news.json"),
                max_leverage=1, min_leverage=1, max_scale_in_count=0,
                min_scale_in_confidence=100, max_margin_equity_ratio=0)

    def test_finite_values_reach_final_layout_without_coercion(self):
        for value in [0, 0.0, -0.0, 17.25, -21.5, 10**400]:
            with self.subTest(value=value):
                self.write_snapshot(value)
                pkg = self.package()
                self.assertEqual(pkg["quant_factors"]["composite_alpha_score"], value)
                text = self.render([pkg])
                self.assertIn(f"composite_alpha_score={value} |", text)
                self.assertIn("source_instId=BTC-USDT-SWAP", text)
                self.assertIn("snapshot_timestamp=1000", text)
                self.assertIn("upstream_evidence_quality=NOT_REPORTED", text)

    def test_invalid_or_missing_alpha_is_not_zero(self):
        for value in [MISSING, *BAD_VALUES]:
            with self.subTest(value=value):
                self.write_snapshot(value)
                pkg = self.package()
                self.assertNotIn("composite_alpha_score", pkg["quant_factors"])
                self.assertIn("composite_alpha_score=-- |", self.render([pkg]))

    def test_renderer_defends_against_invalid_direct_input(self):
        pkg = self.package()
        for value in BAD_VALUES:
            with self.subTest(value=value):
                pkg["quant_factors"] = {"composite_alpha_score": value}
                self.assertIn("composite_alpha_score=-- |", self.render([pkg]))

    def test_unknown_bad_future_and_pre_cycle_timestamps_do_not_gate_alpha(self):
        for timestamp in [MISSING, None, True, "bad", "1000", [], {}, 0, -1,
                          float("nan"), float("inf"), 10**400, 1000, 3000]:
            with self.subTest(timestamp=timestamp):
                self.write_snapshot(0, timestamp)
                pkg = self.package()
                self.assertEqual(pkg["quant_factors"]["composite_alpha_score"], 0)
                text = self.render([pkg])
                self.assertIn("composite_alpha_score=0 |", text)
                self.assertIn("freshness_status=NOT_ASSESSED", text)
                if timestamp is not MISSING and type(timestamp) is int and timestamp in (1000, 3000):
                    self.assertEqual(pkg["quant_factors"]["snapshot_source"]["age_seconds_at_read"],
                                     2000 - timestamp)
                else:
                    self.assertIsNone(pkg["quant_factors"]["snapshot_source"]["snapshot_timestamp"])

    def test_coin_and_snapshot_association_survives_file_replacement(self):
        self.write_snapshot(-7, 1000)
        first = self.package()
        self.write_snapshot(6, 1500)
        second = self.package()
        self.assertEqual(first["quant_factors"]["trend_momentum"]["macd_hist"], 12.5)
        self.assertIn("composite_alpha_score=-7 |", self.render([first]))
        self.assertIn("snapshot_timestamp=1000", self.render([first]))
        self.assertIn("composite_alpha_score=6 |", self.render([second]))
        self.assertIn("snapshot_timestamp=1500", self.render([second]))
        self.assertNotIn("composite_alpha_score=88", self.render([first, second]))

    def test_missing_wrong_coin_corrupt_and_non_object_snapshots_stay_missing(self):
        for content in [None, "{", "[]", '{"instruments": {}}',
                        '{"instruments": [{"instId": "ETH-USDT-SWAP", "composite_alpha_score": 88}]}']:
            with self.subTest(content=content):
                if content is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(content)
                pkg = self.package()
                self.assertEqual(pkg["quant_factors"], {})
                self.assertIn("composite_alpha_score=-- |", self.render([pkg]))

    def test_alpha_only_does_not_invent_tier_availability(self):
        self.write_snapshot(0, tiers=False)
        text = self.render([self.package()])
        self.assertIn("7 梯队因子快照缺失", text)
        self.assertIn("composite_alpha_score=0 |", text)

    def test_missing_instrument_identity_cannot_match_a_missing_id(self):
        self.path.write_text(json.dumps({"instruments": [
            {"composite_alpha_score": 88, "trend_momentum": {}}]}))
        for inst_id in [None, "None"]:
            self.assertEqual(packages.load_quant_factor_tiers(inst_id, path=str(self.path)), {})
        self.assertIsNone(build_signal_snapshot({}, data_dir=str(self.root))["composite_alpha_score"])

    def test_library_dictionary_shape_keeps_signal_legacy_compatibility(self):
        self.path.write_text(json.dumps({"instruments": {
            "synthetic": {"instId": "BTC-USDT-SWAP", "composite_alpha_score": 0}}}))
        self.assertEqual(build_signal_snapshot({"instId": "BTC-USDT-SWAP"},
                         data_dir=str(self.root))["composite_alpha_score"], 0)

    def test_signal_zero_precedence_and_legacy_fallback(self):
        self.write_snapshot(99)
        for primary, legacy, expected in [(0, 33, 0), (0.0, 33, 0.0),
                                           (12, 33, 12), (None, 0, 0), (None, -5, -5)]:
            with self.subTest(primary=primary, legacy=legacy):
                result = build_signal_snapshot({"instId": "BTC-USDT-SWAP",
                    "composite_alpha_score": primary, "alpha_score": legacy}, data_dir=str(self.root))
                self.assertEqual(result["composite_alpha_score"], expected)

    def test_signal_invalid_primary_uses_valid_legacy_then_same_coin_library(self):
        for value in [MISSING, *BAD_VALUES]:
            with self.subTest(value=value):
                f = {"instId": "BTC-USDT-SWAP"}
                if value is not MISSING:
                    f["composite_alpha_score"] = value
                self.write_snapshot(0)
                f["alpha_score"] = -3
                self.assertEqual(build_signal_snapshot(f, data_dir=str(self.root))["composite_alpha_score"], -3)
                f["alpha_score"] = value if value is not MISSING else None
                self.assertEqual(build_signal_snapshot(f, data_dir=str(self.root))["composite_alpha_score"], 0)
                self.write_snapshot(value)
                self.assertIsNone(build_signal_snapshot(f, data_dir=str(self.root))["composite_alpha_score"])

    def test_signal_missing_wrong_coin_or_corrupt_library_does_not_fabricate(self):
        for content in ["{", "[]", '{"instruments": [{"instId": "ETH-USDT-SWAP", "composite_alpha_score": 88}]}']:
            self.path.write_text(content)
            self.assertIsNone(build_signal_snapshot({"instId": "BTC-USDT-SWAP"},
                              data_dir=str(self.root))["composite_alpha_score"])


if __name__ == "__main__":
    unittest.main()
