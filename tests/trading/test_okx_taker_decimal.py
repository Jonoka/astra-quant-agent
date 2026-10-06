"""Precision boundaries identified by independent review, without live inputs."""
from decimal import Inexact, localcontext
import json
import math
import unittest
from unittest.mock import patch

from scripts import okx_taker as taker
from scripts.factors import okx_quant_factors as qf
from tests.trading import test_okx_taker_consistency as fixtures


class DecimalArithmeticTests(unittest.TestCase):
    def test_negative_and_positive_underflow_are_missing_in_both_columns(self):
        for value in ("-1e-400", "1e-400", "1e-324", "-1e-324"):
            for column in (1, 2):
                with self.subTest(value=value, column=column):
                    row = ["meta", "0", "0"]
                    row[column] = value
                    self.assertIsNone(taker.parse_taker_row(row))
                    self.assertIsNone(taker.latest_taker_net([row]))
                    out = qf.compute_cvd_factors([row], [row] * 4, [103, 102, 101, 100])
                    self.assertIsNone(out["cvd_5m_usd"])
                    self.assertEqual(out["cvd_divergence"], "INSUFFICIENT_DATA")

    def test_true_signed_zero_and_subnormal_are_legal(self):
        for value in ("0", "-0", "0e-400", "-0e-400", 0, -0.0):
            with self.subTest(value=value):
                self.assertIsNotNone(taker.parse_taker_row(["meta", value, value]))
                self.assertEqual(taker.latest_taker_net([["meta", value, value]]), 0.0)
        self.assertEqual(taker.latest_taker_net([["meta", "0", "5e-324"]]), 5e-324)

    def test_numeric_strings_have_decimal_structure(self):
        for value in ("1_000", "１２", "0x10", "1e", "--1", "1 2"):
            self.assertIsNone(taker.parse_taker_row(["meta", value, "1"]))
        self.assertEqual(taker.latest_taker_net([["meta", " 1.25 ", "+2.25"]]), 1.0)

    def test_large_operands_do_not_erase_unit_delta(self):
        for exponent in (16, 50, 308):
            for sign in (-1, 1):
                with self.subTest(exponent=exponent, sign=sign):
                    low = 10 ** exponent
                    sell, buy = (low, low + 1) if sign > 0 else (low + 1, low)
                    # Both OKX string volumes and JSON integer volumes are exact.
                    for row in (["meta", str(sell), str(buy)], ["meta", sell, buy]):
                        self.assertEqual(taker.latest_taker_net([row]), float(sign))
                        out = qf.compute_cvd_factors([row], [row], [])
                        self.assertEqual(out["cvd_5m_usd"], float(sign))
                        self.assertEqual(out["cvd_1h_usd"], float(sign))

    def test_exact_zero_windows_do_not_create_either_divergence(self):
        for base in ("0", "10000", "10000000000000000000000000000000000000000"):
            # Build literal decimal operands without the default 28-digit context.
            rows = [["4", base + ".3", base], ["3", base, base + ".2"],
                    ["2", base, base + ".1"], ["1", base, base]]
            for prices in ([103, 102, 101, 100], [100, 101, 102, 103]):
                with self.subTest(base=base, prices=prices):
                    out = qf.compute_cvd_factors(rows, rows, prices)
                    self.assertEqual(out["cvd_divergence"], "NONE")

    def test_small_real_direction_is_not_suppressed_by_epsilon(self):
        for side, prices, expected in ((1, [103, 102, 101, 100], "BULLISH"),
                                       (-1, [100, 101, 102, 103], "BEARISH")):
            small = "0." + "0" * 39 + "1"
            row = ["meta", "0", small] if side > 0 else ["meta", small, "0"]
            out = qf.compute_cvd_factors([row], [row] * 4, prices)
            self.assertEqual(out["cvd_1h_usd"], 0.0)  # Existing two-place display.
            self.assertEqual(out["cvd_divergence"], expected)  # Unrounded input.

    def test_net_underflow_is_missing_after_exact_subtraction(self):
        row = ["meta", "1", "1." + "0" * 399 + "1"]
        self.assertIsNotNone(taker.parse_taker_row_decimal(row))
        self.assertIsNone(taker.latest_taker_net([row]))
        out = qf.compute_cvd_factors([row], [row] * 4, [103, 102, 101, 100])
        self.assertIsNone(out["cvd_5m_usd"])
        self.assertEqual(out["cvd_divergence"], "INSUFFICIENT_DATA")

    def test_overflow_and_ratio_underflow_are_missing(self):
        rows = [["meta", "0", "1e308"]] * 4
        self.assertEqual(qf.compute_cvd_factors(rows, rows, [4, 3, 2, 1])["cvd_divergence"],
                         "INSUFFICIENT_DATA")
        for row in (["meta", "5e-324", "1e308"], ["meta", "1e308", "5e-324"]):
            self.assertIsNone(qf.compute_cvd_factors([row], [row], [])["taker_buy_sell_ratio"])

    def test_local_decimal_context_cannot_change_calculation(self):
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            row = ["meta", "1000000000000000000000000000000", "1000000000000000000000000000001"]
            self.assertEqual(taker.latest_taker_net([row]), 1.0)
            pair = taker.parse_taker_row_decimal(["meta", "3", "1"])
            self.assertAlmostEqual(taker.taker_ratio_float(pair), 1 / 3)

    def test_output_remains_finite_float_json(self):
        row = ["meta", "10000000000000000", "10000000000000001"]
        out = qf.compute_cvd_factors([row], [row], [])
        encoded = json.dumps(out, allow_nan=False)
        self.assertEqual(json.loads(encoded), out)
        for field in ("cvd_5m_usd", "cvd_1h_usd", "taker_buy_sell_ratio"):
            self.assertIsInstance(out[field], float)
            self.assertTrue(math.isfinite(out[field]))


class DecimalPrimaryTests(fixtures.package_fixture._Base):
    def test_invalid_underflow_preserves_missing_cache_source(self):
        for row in (["meta", "-1e-400", "0"], ["meta", "0", "1e-400"],
                    ["meta", "1", "1." + "0" * 399 + "1"]):
            self.assertEqual(self._run(http={"taker-volume": {"code": "0", "data": [row]}})
                             ["takerNetUsd"], "N/A")


class DecimalSmartMoneyTests(fixtures.smart_fixture._HttpMixin, unittest.TestCase):
    def test_precise_scalar_and_missing_keep_compatible_outputs(self):
        for row, expected in ((["meta", "10000000000000000", "10000000000000001"], 1.0),
                              (["meta", "10000000000000001", "10000000000000000"], -1.0),
                              (["meta", "-1e-400", "0"], None),
                              (["meta", "0", "1e-400"], None),
                              (["meta", "-0", "0"], 0.0)):
            out = self._run({"long-short-pos-ratio": fixtures.smart_fixture.OKX_POS_RATIO,
                             "taker-volume": {"code": "0", "data": [row]}})
            self.assertEqual(out["notional"]["netNotionalUsdt"], expected)
            self.assertEqual(out["takerNetUsd"], "--" if expected is None else f"{round(expected, 0)} U")
            json.dumps(out, allow_nan=False)


class DecimalFactorTests(fixtures.factor_fixture._Base):
    def test_large_delta_and_underflow_preserve_factor_direction(self):
        for row, expected in ((["meta", "10000000000000000", "10000000000000001"], 1.0),
                              (["meta", "10000000000000001", "10000000000000000"], -1.0),
                              (["meta", "-1e-400", "0"], None),
                              (["meta", "0", "1e-400"], None),
                              (["meta", "-0", "0"], 0.0)):
            with patch.object(qf, "fetch_taker_volume", return_value=[row]):
                out = self._compute()["volume_money_flow"]
            self.assertEqual(out["cvd_5m_usd"], expected)
            self.assertEqual(out["taker_net_usd"], "--" if expected is None else f"{round(expected)} U")
