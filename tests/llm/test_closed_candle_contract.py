"""Synthetic offline contract regressions, NOT a historical market backtest.

No historical raw candles were supplied. All rows below are explicitly generated
fixtures; external reads are stubbed and no model/order endpoint is invoked.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.brain import packages, prompt
from scripts.factors import candles_15m
from scripts.factors.defaults import build_default_factors
from scripts.trader import factors as trader_factors
from scripts.trader.signal_snapshot import build_signal_snapshot
from tests.llm import test_alpha_transport_contract as alpha_tests


NOW = 1_800_000_120_000
INST = "BTC-USDT-SWAP"
DURATIONS = {"15m": 900_000, "1H": 3_600_000, "4H": 14_400_000}


def synthetic_rows(bar="15m", count=24, *, forming=True):
    duration = DURATIONS[bar]
    boundary = NOW // duration * duration
    return [[str(boundary - (i if forming else i + 1) * duration),
             "100.1234567890123456789", "103", "99", "102.25", "0" if i == 1 else "10",
             "0", "0", "0" if forming and i == 0 else "1"] for i in range(count)]


def contract(rows=None, *, bar="15m", observed=NOW, inst_id=INST):
    return candles_15m.build_closed_candle_contract(
        synthetic_rows(bar) if rows is None else rows,
        inst_id=inst_id, bar=bar, observed_at_ms=observed)


class ClosedCandleEvidenceTests(unittest.TestCase):
    def test_uses_explicit_confirm_and_bounds_orders_preserves_raw_ohlcv(self):
        rows = synthetic_rows()
        original = copy.deepcopy(rows)
        result = contract(rows)
        self.assertEqual(result["status"], "CONFIRMED_CURRENT")
        self.assertTrue(result["available"])
        self.assertTrue(result["window_complete"])
        self.assertEqual([r["ts_ms"] for r in result["candles"]],
                         [int(rows[i][0]) for i in (3, 2, 1)])
        self.assertEqual(result["candles"][-1]["open"], rows[1][1])
        self.assertEqual(result["candles"][-1]["volume"], "0")
        self.assertEqual(result["excluded"]["unconfirmed"], 1)
        self.assertEqual(rows, original)

    def test_all_closed_response_keeps_first_row_not_assumed_second_row(self):
        rows = synthetic_rows(forming=False)
        self.assertEqual(contract(rows)["candles"][-1]["ts_ms"], int(rows[0][0]))

    def test_time_alone_and_legacy_six_column_rows_never_prove_closure(self):
        for flag in ("0", 0, None, True, False, "true", 1.0, "", 2):
            rows = synthetic_rows(forming=False)
            for row in rows:
                row[8] = flag
            with self.subTest(flag=flag):
                result = contract(rows)
                self.assertEqual(result["status"], "NO_CONFIRMED_CANDLES")
                self.assertFalse(result["available"])
                self.assertEqual(result["candles"], [])
        self.assertEqual(contract([r[:6] for r in synthetic_rows()])["candles"], [])

    def test_future_or_not_yet_ended_confirmed_rows_are_not_valid_evidence(self):
        current = synthetic_rows(count=1)[0]
        current[8] = "1"
        future = copy.deepcopy(current)
        future[0] = str(NOW + DURATIONS["15m"])
        for row in (current, future):
            result = contract([row])
            self.assertFalse(result["available"])
            self.assertEqual(result["excluded"]["invalid"], 1)

    def test_seconds_instead_of_milliseconds_and_off_bucket_timestamps_are_rejected(self):
        for bar in DURATIONS:
            row = synthetic_rows(bar, count=1, forming=False)[0]
            for timestamp in (str(int(row[0]) + 1), str(int(row[0]) // 1000),
                              "1799999100001", "1799999100"):
                bad = copy.deepcopy(row)
                bad[0] = timestamp
                with self.subTest(bar=bar, timestamp=timestamp):
                    result = contract([bad], bar=bar)
                    self.assertEqual(result["candles"], [])
                    self.assertFalse(result["available"])
                    self.assertEqual(result["excluded"]["invalid"], 1)

    def test_invalid_ohlcv_and_timestamps_do_not_get_coerced_to_zero(self):
        bad = [(0, True), (0, "nan"), (0, "1.5"), (0, 0), (0, 10**400), (0, NOW - 1),
               (1, None), (1, True), (1, "nan"), (1, "inf"), (1, "bad"), (1, "0"),
               (2, "98"), (3, "104"), (4, "104"), (5, "-1"), (5, "nan")]
        for column, value in bad:
            row = synthetic_rows(count=1, forming=False)[0]
            row[column] = value
            with self.subTest(column=column, value=value):
                self.assertEqual(contract([row])["candles"], [])

    def test_stale_closed_data_stays_visible_but_not_current_confirmation(self):
        result = contract(synthetic_rows()[2:])
        self.assertEqual(result["status"], "STALE")
        self.assertTrue(result["available"])
        self.assertEqual(result["age_ms"], NOW - result["latest_end_ts_ms"])

    def test_short_or_gapped_windows_explicitly_report_incomplete(self):
        rows = synthetic_rows(forming=False)
        for selected in (rows[:1], rows[:2], [rows[0], rows[2], rows[3]]):
            result = contract(selected)
            self.assertFalse(result["window_complete"])
            self.assertEqual(result["status"], "CONFIRMED_CURRENT")

    def test_unsorted_and_identical_duplicates_do_not_change_verified_window(self):
        rows = synthetic_rows(forming=False)
        result = contract(list(reversed(rows)) + [copy.deepcopy(rows[0])])
        self.assertEqual(result["candles"], contract(rows)["candles"])

    def test_conflicting_duplicates_are_excluded_without_selecting_a_price(self):
        rows = synthetic_rows(forming=False)
        for index, changed in [(4, "101"), (4, "110"), (8, "0"), (8, None)]:
            conflict = copy.deepcopy(rows[0])
            conflict[index] = changed
            for batch in (rows + [conflict], [conflict] + rows):
                result = contract(batch)
                self.assertNotIn(int(rows[0][0]), [r["ts_ms"] for r in result["candles"]])
                self.assertEqual(result["status"], "STALE")
                self.assertGreaterEqual(result["excluded"]["invalid"], 1)

    def test_revalidation_checks_identity_time_and_provenance(self):
        for field, value in [("instId", "ETH-USDT-SWAP"), ("bar", "1H"),
                             ("source", "ticker"), ("schema", "other"),
                             ("observed_at_ms", NOW + 1), ("candles", {})]:
            source = contract()
            source[field] = value
            with self.subTest(field=field):
                result = candles_15m.validate_closed_candle_contracts(
                    {"15m": source}, inst_id=INST, now_ms=NOW)["15m"]
                self.assertEqual(result["status"], "INVALID_SOURCE")
                self.assertEqual(result["candles"], [])

    def test_duplicate_signature_distinguishes_boolean_from_numeric_one(self):
        row = synthetic_rows(count=1, forming=False)[0]
        row[1:6] = [1, 3, 0.5, 2, 1]
        row[8] = 1
        self.assertEqual(contract([row])["status"], "CONFIRMED_CURRENT")
        self.assertEqual(len(contract([row, copy.deepcopy(row)])["candles"]), 1)
        for column in (1, 5, 8):
            duplicate = copy.deepcopy(row)
            duplicate[column] = True
            for batch in ([row, duplicate], [duplicate, row]):
                with self.subTest(column=column, batch=batch):
                    result = contract(batch)
                    self.assertFalse(result["available"])
                    self.assertEqual(result["candles"], [])
                    self.assertGreaterEqual(result["excluded"]["invalid"], 1)

    def test_prompt_time_revalidation_cannot_keep_old_current_label(self):
        result = candles_15m.validate_closed_candle_contracts(
            {"15m": contract()}, inst_id=INST, now_ms=NOW + DURATIONS["15m"])["15m"]
        self.assertEqual(result["status"], "STALE")


class ClosedCandleTransportTests(unittest.TestCase):
    render = alpha_tests.AlphaTransportContractTests.render

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = patch.object(candles_15m.time, "time", return_value=NOW / 1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def package(self, rows=None):
        rows = rows if rows is not None else {bar: synthetic_rows(bar) for bar in DURATIONS}
        with patch.object(packages, "public_json_get", return_value={"code": "0", "data": [
                {"last": "999", "bidPx": "998", "askPx": "1000", "open24h": "999"}]}), \
                patch.object(packages, "load_quant_factor_tiers", return_value={"composite_alpha_score": 0}), \
                patch("builtins.print"):
            return packages.fetch_single_instrument_package(
                {"instId": INST, "name": "比特币", "type": "synthetic", "precision": 2},
                fetch_candles=lambda inst, *, bar, limit: rows.get(bar, []),
                fetch_single_indicator=lambda *a, **kw: {})

    def test_real_package_to_final_prompt_has_all_timeframes_without_ticker_substitution(self):
        pkg = self.package()
        text = self.render([pkg])
        self.assertEqual(pkg["price"], 999.0)
        self.assertIn("composite_alpha_score=0 |", text)
        for bar in DURATIONS:
            evidence = pkg["closed_candle_contract"][bar]
            self.assertEqual(evidence["status"], "CONFIRMED_CURRENT")
            self.assertEqual(len(evidence["candles"]), 3)
            line = next(line for line in text.splitlines() if line.startswith(f"  {bar}: "))
            view = json.loads(line.split(": ", 1)[1])
            self.assertEqual(view["rows"][-1][0], evidence["candles"][-1]["ts_ms"])
            self.assertEqual(view["rows"][-1][5], "102.25")
            self.assertEqual(view["rows"][-1][7], "1")
            self.assertNotEqual(view["rows"][-1][5], pkg["price"])
        self.assertIn("rows列=[ts_ms,end_ts_ms,O,H,L,C,V,confirm]", text)
        self.assertIn("source=OKX:/api/v5/market/candles", text)
        self.assertIn("衍生品:contracts；SPOT/MARGIN:base_ccy", text)
        self.assertIn("仅作同标的量能证据，不作仓位或跨标的金额比较", text)
        self.assertIn("不是USDT成交额，不换算", text)
        self.assertIn("现价与派生指标不能代替收盘确认", text)

    def test_missing_and_unconfirmed_contracts_are_explicit_in_final_prompt(self):
        legacy = {bar: [row[:6] for row in synthetic_rows(bar)] for bar in DURATIONS}
        text = self.render([self.package(legacy)])
        self.assertIn('"status":"NO_CONFIRMED_CANDLES"', text)
        self.assertNotIn('"status":"CONFIRMED_CURRENT"', text)
        pkg = self.package({})
        self.assertIn('"status":"MISSING"', self.render([pkg]))
        pkg.pop("closed_candle_contract")
        pkg["price"] = 999
        self.assertIn('"rows":[]', self.render([pkg]))

    def test_renderer_rechecks_stale_and_invalid_records_instead_of_trusting_labels(self):
        pkg = self.package()
        pkg["closed_candle_contract"]["15m"] = contract(synthetic_rows()[2:])
        pkg["closed_candle_contract"]["1H"]["observed_at_ms"] = NOW + 1
        pkg["closed_candle_contract"]["4H"]["candles"][0]["close"] = float("nan")
        text = self.render([pkg])
        self.assertIn('"status":"STALE"', text)
        self.assertIn('"status":"INVALID_SOURCE"', text)
        self.assertNotIn('NaN', text)
        self.assertIn('"window_complete":false', text)

    def test_proposal_symbols_use_unique_ids_without_changing_display_names(self):
        pkg = self.package()
        other = copy.deepcopy(pkg)
        other.update(instId="ETH-USDT-SWAP", name="以太坊")
        context = {}
        original = prompt.construct_full_market_prompt
        with patch.object(prompt, "construct_full_market_prompt",
                          side_effect=lambda *a, **kw: original(*a, runtime_context_out=context, **kw)):
            text = self.render([pkg, other, pkg])
        self.assertEqual(context["proposal_symbols"], [INST, "ETH-USDT-SWAP"])
        self.assertEqual(context["active_instruments"], "比特币,以太坊,比特币")
        self.assertEqual(text.count("【已闭合K线确认契约】"), 1)
        self.assertEqual(text.count("rows列=[ts_ms,end_ts_ms,O,H,L,C,V,confirm]"), 1)
        self.assertEqual(text.count("source=OKX:/api/v5/market/candles"), 1)

    def test_factor_indicator_observation_does_not_overwrite_calculations(self):
        factors = build_default_factors(INST, "synthetic")
        factors["price"] = 101
        rows = synthetic_rows()
        values = candles_15m.compute_15m_indicators(rows, factors, safe_float=float)
        self.assertEqual(values[0], [float(r[4]) for r in reversed(rows)])
        self.assertEqual(factors["closed_candle_contract"]["15m"]["status"], "CONFIRMED_CURRENT")
        candles_15m.mark_15m_missing(factors)
        self.assertEqual(factors["closed_candle_contract"]["15m"]["status"], "MISSING")

    def test_execution_factors_to_signal_snapshot_preserve_collected_source(self):
        rows = {bar: synthetic_rows(bar, count=60) for bar in DURATIONS}
        with patch.object(trader_factors, "public_json_get", return_value={"code": "0", "data": []}):
            f = trader_factors.fetch_single_instrument_data(
                {"instId": INST, "name": "synthetic", "type": "crypto", "base_sz": 1,
                 "precision": 2, "ctVal": 1}, [], 100,
                news_sentiment_file=str(self.root / "absent.json"),
                fetch_candles_direct=lambda inst, bar, count: rows[bar],
                instrument_profile=lambda *a: {}, load_adaptive_config=lambda: {})
        snap = build_signal_snapshot(f, data_dir=str(self.root))
        for bar in DURATIONS:
            self.assertEqual(snap["closed_candle_contract"][bar]["candles"],
                             f["closed_candle_contract"][bar]["candles"])
            self.assertEqual(snap["closed_candle_contract"][bar]["observed_at_ms"], NOW)

    def test_snapshot_never_reconstructs_entry_candles_from_later_library(self):
        lib = {"instruments": [{"instId": INST, "closed_candle_contract": {"15m": contract()}}]}
        (self.root / "factor_library_snapshot.json").write_text(json.dumps(lib))
        snap = build_signal_snapshot({"instId": INST, "price": 999}, data_dir=str(self.root))
        for result in snap["closed_candle_contract"].values():
            self.assertEqual(result["status"], "MISSING")
            self.assertEqual(result["candles"], [])


if __name__ == "__main__":
    unittest.main()
