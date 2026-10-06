"""Offline regression for official OKX taker ordering and missing data."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.okx_taker import latest_taker_volumes, parse_taker_row
from scripts.factors import okx_quant_factors as qf
from scripts.brain.decisions import assemble_decision_cache
from astra_backend.dashboard_payload import factors, factors_view
from tests.ops import test_brain_packages as package_fixture
from tests.ops import test_factor_library as factor_fixture
from tests.ops import test_factors_smart_money as smart_fixture
from tests.llm import test_brain_prompt_module as prompt_fixture

VALID = [("10000", "20000", 10000.0), ("20000", "10000", -10000.0),
         ("15000", "15000", 0.0), (0, 0, 0.0)]
INVALID_VALUES = [None, "", " ", "bad", "nan", "inf", "-inf", float("nan"),
                  float("inf"), True, False, -1, "-1", {}, []]
BAD_ROWS = [None, [], ["t"], ["t", "1"], "t,1,2", {"1": 1, "2": 2}]


class ParserTests(unittest.TestCase):
    def test_package_and_standalone_import_paths(self):
        root = Path(__file__).resolve().parents[2]
        for standalone in (False, True):
            with self.subTest(standalone=standalone):
                code = "\n".join([
                    "import builtins, importlib, pathlib, sys",
                    f"root = pathlib.Path({str(root)!r})",
                    "sys.path[:0] = [str(root), str(root / 'scripts')]",
                    "original_import = builtins.__import__",
                    "def without_package_helpers(name, *args, **kwargs):",
                    "    if name in ('scripts.okx_public', 'scripts.okx_taker'):",
                    "        raise ImportError('Exercise existing script helper fallbacks')",
                    "    return original_import(name, *args, **kwargs)",
                    "builtins.__import__ = without_package_helpers" if standalone else "",
                    "prefix = ''" if standalone else "prefix = 'scripts.'",
                    "for name in ('brain.packages', 'factors.smart_money', 'factors.okx_quant_factors'):",
                    "    module = importlib.import_module(prefix + name)",
                    "    assert root in pathlib.Path(module.__file__).resolve().parents",
                    "    assert module.latest_taker_volumes([['t', '1', '2']]) == (2.0, 1.0)",
                    "    assert module.latest_taker_volumes.__module__ == prefix + 'okx_taker'",
                ])
                result = subprocess.run([sys.executable, "-B", "-c", code], cwd=root,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_official_order_and_real_zero(self):
        for sell, buy, expected in VALID:
            with self.subTest(sell=sell, buy=buy):
                pair = parse_taker_row(["t", sell, buy])
                self.assertEqual(pair[0] - pair[1], expected)
        self.assertEqual(parse_taker_row(("t", "1", "2", "extra")), (2.0, 1.0))

    def test_invalid_structure_is_missing(self):
        for row in BAD_ROWS:
            with self.subTest(row=row):
                self.assertIsNone(parse_taker_row(row))

    def test_invalid_volume_is_missing_in_either_column(self):
        for value in INVALID_VALUES:
            for column in (1, 2):
                with self.subTest(value=value, column=column):
                    row = ["t", 1, 2]
                    row[column] = value
                    self.assertIsNone(parse_taker_row(row))

    def test_invalid_latest_does_not_fall_back_to_old_data(self):
        self.assertIsNone(latest_taker_volumes([["t"], ["older", "1", "2"]]))
        for rows in (None, [], "bad", {0: ["t", 1, 2]}):
            self.assertIsNone(latest_taker_volumes(rows))


class PrimaryTests(package_fixture._Base):
    def test_correct_sign_and_genuine_zero(self):
        for sell, buy, expected in VALID:
            with self.subTest(sell=sell, buy=buy):
                pkg = self._run(http={"taker-volume": {"code": "0", "data": [["t", sell, buy]]}})
                self.assertEqual(pkg["takerNetUsd"], f"{round(expected / 1e4, 1)}万 U")
                self.assertEqual(pkg["data_quality"], "valid")

    def test_invalid_rows_do_not_become_numeric_zero(self):
        rows = BAD_ROWS + [["t", value, "1"] for value in INVALID_VALUES]
        for row in rows:
            with self.subTest(row=row):
                pkg = self._run(http={"taker-volume": {"code": "0", "data": [row]}})
                self.assertEqual(pkg["takerNetUsd"], "N/A")
                self.assertEqual(pkg["data_quality"], "valid")

    def test_corrected_cache_and_both_backend_payloads(self):
        for sell, buy, expected in VALID[:3]:
            pkg = self._run(http={"taker-volume": {"code": "0", "data": [["t", sell, buy]]}})
            with tempfile.TemporaryDirectory() as directory:
                cache = assemble_decision_cache(
                    [pkg], {pkg["instId"]: {"action": "WAIT"}}, set(), {}, "2026-10-06 15:45:24", "",
                    data_dir=directory, max_leverage=10, min_leverage=1,
                    safe_float=lambda value: float(value or 0), get_system_version_tag=lambda: "test",
                    validate=lambda *args: ("WAIT", "", 0))
                self.assertEqual(cache[pkg["instId"]]["raw_taker_vol"], pkg["takerNetUsd"])
                self.assertEqual(cache[pkg["instId"]]["decision"]["action"], "WAIT")
                data = {"cache": cache, "state": {}, "factor": {"instruments": [{
                    "instId": pkg["instId"], "volume_money_flow": {
                        "taker_net_usd": pkg["takerNetUsd"], "cvd_5m_usd": expected}}]}}
                paths = {}
                for key, content in data.items():
                    paths[key] = Path(directory, key + ".json")
                    paths[key].write_text(json.dumps(content), encoding="utf-8")
                pool = [{"instId": pkg["instId"], "name": "BTC"}]
                with patch.object(factors, "load_instruments", return_value=pool):
                    rows, _ = factors._build_factors_from_local_files(
                        paths["factor"], paths["cache"], paths["state"], [], "test")
                self.assertEqual(rows[0]["takerNetUsd"], pkg["takerNetUsd"])
                self.assertEqual(rows[0]["orderflow"]["cvd_5m_usd"], expected)
                with patch.object(factors_view, "load_instruments", return_value=pool):
                    rows, _ = factors_view.build_factors_list(
                        paths["cache"], paths["state"], paths["factor"], [], "test")
                self.assertEqual(rows[0]["takerNetUsd"], pkg["takerNetUsd"])


class SmartMoneyTests(smart_fixture._HttpMixin, unittest.TestCase):
    def _fetch(self, row):
        return self._run({"long-short-pos-ratio": smart_fixture.OKX_POS_RATIO,
                          "taker-volume": {"code": "0", "data": [row]}})

    def test_sign_and_zero_preserve_numeric_net(self):
        for sell, buy, expected in VALID:
            with self.subTest(sell=sell, buy=buy):
                sm = self._fetch(["t", sell, buy])
                self.assertEqual(sm["notional"]["netNotionalUsdt"], expected)
                self.assertNotEqual(sm["takerNetUsd"], "--")

    def test_bad_values_preserve_missing_numeric_and_text(self):
        for row in BAD_ROWS + [["t", "1", value] for value in INVALID_VALUES]:
            with self.subTest(row=row):
                sm = self._fetch(row)
                self.assertIsNone(sm["notional"]["netNotionalUsdt"])
                self.assertEqual(sm["takerNetUsd"], "--")


class FactorTests(factor_fixture._Base):
    def test_factor_assembly_sign_and_missing(self):
        for row in [["t", sell, buy] for sell, buy, _ in VALID] + BAD_ROWS:
            with self.subTest(row=row):
                with patch.object(qf, "fetch_taker_volume", return_value=[row]):
                    result = self._compute()
                mf = result["volume_money_flow"]
                pair = parse_taker_row(row)
                if pair is None:
                    self.assertEqual(mf["taker_net_usd"], "--")
                    self.assertIsNone(mf["cvd_5m_usd"])
                else:
                    self.assertEqual(mf["cvd_5m_usd"], pair[0] - pair[1])
                    self.assertNotEqual(mf["taker_net_usd"], "--")

    def test_smart_money_overlay_does_not_fabricate_zero(self):
        for net in (None, 0.0, 10000.0, -10000.0):
            with self.subTest(net=net):
                pool = {"BTC": {"longShortRatio": {"weightedLongRatio": 0.7},
                                "notional": {"netNotionalUsdt": net}}}
                result = self._compute(pool=pool)["smart_money_derivatives"]
                self.assertEqual(result["smart_money_flow_usd"] == "--", net is None)
                if net is None:
                    self.assertNotEqual(result["signal"], "BULL_ACCUMULATION")

    def test_parsed_smart_money_reaches_overlay_with_the_same_sign(self):
        source = SmartMoneyTests()
        source.setUp()
        for sell, buy, expected in VALID:
            with self.subTest(sell=sell, buy=buy):
                sm = source._fetch(["t", sell, buy])
                result = self._compute(pool={"BTC": sm})["smart_money_derivatives"]
                self.assertEqual(result["smart_money_flow_usd"], sm["takerNetUsd"])
                self.assertEqual(sm["notional"]["netNotionalUsdt"], expected)


class CvdTests(unittest.TestCase):
    def test_official_order_positive_negative_and_equal(self):
        for sell, buy, expected in VALID:
            with self.subTest(sell=sell, buy=buy):
                out = qf.compute_cvd_factors([["t", sell, buy]], [["t", sell, buy]], [])
                self.assertEqual(out["cvd_5m_usd"], expected)
                self.assertEqual(out["cvd_1h_usd"], expected)

    def test_bad_or_missing_latest_rows_are_unavailable(self):
        for row in BAD_ROWS + [["t", value, "1"] for value in INVALID_VALUES]:
            with self.subTest(row=row):
                out = qf.compute_cvd_factors([row], [row], [])
                for field in ("cvd_5m_usd", "cvd_1h_usd", "taker_buy_sell_ratio"):
                    self.assertIsNone(out[field])
                self.assertEqual(out["cvd_divergence"], "INSUFFICIENT_DATA")

    def test_bad_aligned_history_is_not_treated_as_no_divergence(self):
        out = qf.compute_cvd_factors([["t", 1, 2]],
                                    [["t", 1, 2], ["t"], ["t", 1, 2], ["t", 1, 2]],
                                    [100, 101, 102, 103])
        self.assertEqual(out["cvd_1h_usd"], 1)
        self.assertEqual(out["cvd_divergence"], "INSUFFICIENT_DATA")

    def test_zero_net_and_zero_sell_do_not_fake_missing_or_ratio(self):
        out = qf.compute_cvd_factors([["t", 2, 2]], [["t", 2, 2]], [])
        self.assertEqual(out["cvd_5m_usd"], 0)
        self.assertEqual(out["taker_buy_sell_ratio"], 1)
        out = qf.compute_cvd_factors([["t", 0, 0]], [["t", 0, 0]], [])
        self.assertEqual(out["cvd_5m_usd"], 0)
        self.assertIsNone(out["taker_buy_sell_ratio"])

    def test_overflowing_ratio_and_sum_remain_missing(self):
        row = ["t", 1e-308, 1e308]
        out = qf.compute_cvd_factors([row], [row] * 4, [100, 101, 102, 103])
        self.assertIsNone(out["taker_buy_sell_ratio"])
        self.assertEqual(out["cvd_divergence"], "INSUFFICIENT_DATA")


class PromptTests(prompt_fixture._PromptSandbox, smart_fixture._HttpMixin, unittest.TestCase):
    def _matrix(self, tiers, raw="1.0万 U"):
        pkg = prompt_fixture.RuntimeVarsTests._pkg(takerNetUsd=raw, quant_factors=tiers)
        out = {}
        self._call(packages=[pkg], runtime_context_out=out)
        return out["market_matrix"]

    def test_factor_priority_and_corrected_legacy_fallback(self):
        self.assertIn("5M主动吃单净差=-2.0万 U", self._matrix({
            "volume_money_flow": {"taker_net_usd": "-2.0万 U"}}))
        for tiers in ({"volume_money_flow": {}},
                      {"volume_money_flow": {"taker_net_usd": None}},
                      {"volume_money_flow": {"taker_net_usd": ""}}):
            with self.subTest(tiers=tiers):
                self.assertIn("5M主动吃单净差=1.0万 U", self._matrix(tiers))

    def test_explicit_missing_and_actual_zero_are_distinct(self):
        for value in ("--", "0 U", 0):
            with self.subTest(value=value):
                self.assertIn(f"5M主动吃单净差={value}", self._matrix({
                    "volume_money_flow": {"taker_net_usd": value}}))
        self.assertIn("5M主动吃单净差=N/A", self._matrix({"volume_money_flow": {}}, raw="N/A"))

    def test_whole_snapshot_missing_omits_the_tier_line(self):
        matrix = self._matrix({})
        self.assertIn("因子快照缺失", matrix)
        self.assertNotIn("5M主动吃单净差=", matrix)

    def test_parsed_smart_money_text_reaches_full_prompt(self):
        self.urls = []
        for row in [["t", sell, buy] for sell, buy, _ in VALID] + [["t", "bad", "1"]]:
            with self.subTest(row=row):
                sm = self._run({"long-short-pos-ratio": smart_fixture.OKX_POS_RATIO,
                                "taker-volume": {"code": "0", "data": [row]}})
                pkg = prompt_fixture.RuntimeVarsTests._pkg(smart_money={
                    "available": True, "weighted_long_pct": sm["weighted_long_pct"],
                    "net_flow_usdt": sm["takerNetUsd"]})
                out = {}
                self._call(packages=[pkg], runtime_context_out=out)
                self.assertIn("24H净流入=" + sm["takerNetUsd"], out["market_matrix"])
