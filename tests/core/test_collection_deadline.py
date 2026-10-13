"""Offline fault injection for reconstructed collection ownership and publication.

Every external dependency is mocked. Threads are released and joined before a
fixture exits; tests must still run under the independent OfflineGuard runner.
"""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, ExitStack, redirect_stdout
from contextvars import copy_context
from unittest.mock import Mock, patch

from astra_backend import collection, deadline
from scripts import ai_brain_trader as brain, factor_library as factors
from scripts.brain import packages, snapshots
from scripts.factors import smart_money
from scripts.factors.defaults import build_default_factors

ITEM = {"instId": "BTC-USDT-SWAP", "name": "BTC", "ccy": "BTC",
        "type": "crypto", "precision": 2}


def fresh_item():
    item = build_default_factors(ITEM["instId"], ITEM["name"])
    item.update(ITEM, price=100.0, composite_alpha_score=12)
    item["trend_momentum"]["marker"] = "current"
    return item


def fresh_snapshot():
    return {"timestamp": time.time(), "instruments": [fresh_item()]}


class CancellationTests(unittest.TestCase):
    def test_owner_exit_cancels_copied_context(self):
        with deadline.cancellation_scope():
            context = copy_context()
        with self.assertRaises(deadline.DeadlineExceeded):
            context.run(deadline.check_deadline)

    def test_cancelled_ancestor_cannot_be_renewed(self):
        with deadline.cancellation_scope() as parent:
            with deadline.cancellation_scope():
                parent.set()
                with self.assertRaises(deadline.DeadlineExceeded):
                    with deadline.deadline_scope(timeout=100):
                        pass

    def test_cancelled_commit_rejected(self):
        with deadline.cancellation_scope() as parent:
            with deadline.cancellation_scope():
                parent.set()
                with self.assertRaises(deadline.DeadlineExceeded):
                    with deadline.commit_scope():
                        self.fail("cancelled owner published")

    def test_nested_gates_lock_ancestor_first(self):
        order = []
        class Lock:
            def __init__(self, label): self.label = label
            def acquire(self): order.append("+" + self.label)
            def release(self): order.append("-" + self.label)
        with deadline.cancellation_scope() as outer, deadline.cancellation_scope() as inner:
            with patch.object(outer, "commit_lock", Lock("outer")), \
                 patch.object(inner, "commit_lock", Lock("inner")):
                with deadline.commit_scope():
                    pass
        self.assertEqual(order, ["+outer", "+inner", "-inner", "-outer"])

    def test_cancellation_waits_for_already_entered_short_commit(self):
        entered, release, cancelling, cancelled = (threading.Event() for _ in range(4))
        def publish():
            with deadline.commit_scope():
                entered.set()
                release.wait(2)
        with deadline.cancellation_scope() as token:
            worker = threading.Thread(target=copy_context().run, args=(publish,))
            worker.start()
            self.assertTrue(entered.wait(1))
            def cancel():
                cancelling.set()
                token.set()
                cancelled.set()
            stopper = threading.Thread(target=cancel)
            stopper.start()
            try:
                self.assertTrue(cancelling.wait(1))
                self.assertFalse(cancelled.wait(.02))
            finally:
                release.set()
                worker.join(2)
                stopper.join(2)
            self.assertTrue(cancelled.is_set())

    def test_worker_cannot_start_dependency_after_abandonment(self):
        started, release, finished = (threading.Event() for _ in range(3))
        next_dependency = Mock()
        def worker():
            started.set()
            release.wait(2)
            try:
                deadline.check_deadline()
                next_dependency()
            except deadline.DeadlineExceeded:
                pass
            finally:
                finished.set()
        try:
            with deadline.deadline_scope(timeout=10):
                with brain._collection_future(worker):
                    self.assertTrue(started.wait(1))
                release.set()
                self.assertTrue(finished.wait(1))
            next_dependency.assert_not_called()
        finally:
            release.set()
            finished.wait(2)

    def test_unbounded_utility_wait_uses_none(self):
        self.assertEqual(brain._bounded_collection_call(lambda: "ok"), "ok")

    def test_explicit_invalid_monotonic_boundaries_fail_closed(self):
        for value in (float("nan"), float("inf"), True, "100"):
            with self.subTest(value=value), self.assertRaises(deadline.DeadlineExceeded):
                with deadline.deadline_scope(deadline=value):
                    self.fail("invalid deadline admitted")

    def test_execution_boundary_only_shortens_parent(self):
        with patch.object(deadline.time, "time", return_value=1000), \
             patch.object(deadline.time, "monotonic", return_value=100), \
             patch.dict(os.environ, {"ASTRA_EXECUTION_DEADLINE_EPOCH": "1002"}), \
             deadline.deadline_scope(timeout=10), deadline.execution_scope():
            self.assertEqual(deadline.current_deadline(), 102)

    def test_invalid_execution_boundary_starts_no_work(self):
        for value in ("nan", "inf", "bad", "-1"):
            with self.subTest(value=value), \
                 patch.dict(os.environ, {"ASTRA_EXECUTION_DEADLINE_EPOCH": value}), \
                 self.assertRaises(deadline.DeadlineExceeded):
                with deadline.execution_scope():
                    self.fail("invalid execution boundary admitted")


class FactorTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(self.tmp) / "factor.json"
        self.path.write_text('{"previous": true}')
        self.stack.enter_context(patch.object(factors, "DATA_DIR", self.tmp))
        self.stack.enter_context(patch.object(factors, "FACTOR_LIB_CACHE_FILE", str(self.path)))
        self.stack.enter_context(patch.object(factors, "TARGET_INSTRUMENTS", [dict(ITEM)]))
        self.stack.enter_context(patch.dict(os.environ, {"ASTRA_EXECUTION_DEADLINE_EPOCH": ""}))
        self.pool = self.stack.enter_context(patch.object(smart_money, "fetch_smart_money_pool", return_value={}))

    def assert_previous(self):
        self.assertEqual(json.loads(self.path.read_text()), {"previous": True})
        self.assertEqual(list(Path(self.tmp).iterdir()), [self.path])

    def test_complete_refresh_and_worker_context(self):
        seen = []
        def compute(*args):
            seen.append(deadline.current_deadline())
            return fresh_item()
        with patch.object(factors, "compute_instrument_factors", compute), deadline.deadline_scope(timeout=10):
            expected = deadline.current_deadline()
            snapshot = factors.update_factor_library()
        self.assertEqual(seen, [expected])
        self.assertEqual(snapshot, json.loads(self.path.read_text()))
        self.assertEqual(snapshot["instruments"][0]["instId"], ITEM["instId"])

    def test_slow_factor_wait_is_bounded_and_late_worker_cannot_publish(self):
        entered, release, finished = (threading.Event() for _ in range(3))
        def compute(*args):
            entered.set()
            release.wait(2)
            finished.set()
            return fresh_item()
        started = time.monotonic()
        try:
            with patch.object(factors, "compute_instrument_factors", compute), deadline.deadline_scope(timeout=.08):
                with self.assertRaises(deadline.DeadlineExceeded):
                    factors.update_factor_library()
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - started, .5)
            release.set()
            self.assertTrue(finished.wait(1))
            self.assert_previous()
        finally:
            release.set()
            finished.wait(2)

    def test_fsync_expiry_keeps_previous_and_cleans_temp(self):
        clock = [100.0]
        real_fsync = factors.os.fsync
        def expire(fd):
            real_fsync(fd)
            clock[0] += 2
        with patch.object(factors, "compute_instrument_factors", return_value=fresh_item()), \
             patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch.object(factors.os, "fsync", expire), deadline.deadline_scope(timeout=1):
            with self.assertRaises(deadline.DeadlineExceeded):
                factors.update_factor_library()
        self.assert_previous()

    def test_pool_deadline_propagates(self):
        self.pool.side_effect = deadline.DeadlineExceeded()
        with patch.object(factors, "compute_instrument_factors") as compute:
            with self.assertRaises(deadline.DeadlineExceeded):
                factors.update_factor_library()
        compute.assert_not_called()
        self.assert_previous()

    def test_ticker_deadline_prevents_later_dependencies(self):
        with patch.object(factors, "public_json_get", side_effect=deadline.DeadlineExceeded()), \
             patch.object(factors, "fetch_orderbook_depth") as depth, \
             patch.object(factors, "fetch_candles") as candles:
            with self.assertRaises(deadline.DeadlineExceeded):
                factors.compute_instrument_factors(ITEM, {})
        depth.assert_not_called()
        candles.assert_not_called()

    def test_invalid_scheduler_boundary_rejects_before_pool(self):
        with patch.dict(os.environ, {"ASTRA_EXECUTION_DEADLINE_EPOCH": "nan"}):
            with self.assertRaises(deadline.DeadlineExceeded):
                factors.update_factor_library()
        self.pool.assert_not_called()
        self.assert_previous()

    def test_cancel_before_commit_cannot_replace_file(self):
        ready, release, finished = (threading.Event() for _ in range(3))
        real_commit = deadline.commit_scope
        result = []
        @contextmanager
        def delayed_commit():
            ready.set()
            release.wait(2)
            with real_commit():
                yield
        def refresh():
            try:
                factors.update_factor_library()
            except deadline.DeadlineExceeded:
                result.append("cancelled")
            finally:
                finished.set()
        with patch.object(factors, "compute_instrument_factors", return_value=fresh_item()), \
             patch.object(factors, "commit_scope", delayed_commit), deadline.deadline_scope(timeout=10):
            try:
                with deadline.cancellation_scope():
                    worker = threading.Thread(target=copy_context().run, args=(refresh,))
                    worker.start()
                    self.assertTrue(ready.wait(1))
                release.set()
                self.assertTrue(finished.wait(1))
            finally:
                release.set()
                worker.join(2)
        self.assertEqual(result, ["cancelled"])
        self.assert_previous()


class SnapshotTests(unittest.TestCase):
    def test_exact_returned_snapshot_ignores_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "factors.json"
            path.write_text(json.dumps({"timestamp": time.time(), "instruments": []}))
            result = packages.load_quant_factor_tiers(ITEM["instId"], path=str(path), snapshot=fresh_snapshot())
        self.assertEqual(result["snapshot_source"]["freshness_status"], "CURRENT_REFRESH")
        self.assertEqual(result["trend_momentum"]["marker"], "current")

    def test_file_read_never_claims_current_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "factors.json"
            path.write_text(json.dumps(fresh_snapshot()))
            result = packages.load_quant_factor_tiers(ITEM["instId"], path=str(path))
        self.assertEqual(result["snapshot_source"]["freshness_status"], "NOT_ASSESSED")

    def test_invalid_or_stale_current_timestamps(self):
        for timestamp in (None, True, 0, "123", float("inf"), float("nan"), 10**1000,
                          time.time() - 181, time.time() + 2):
            with self.subTest(timestamp=str(timestamp)[:30]):
                snap = fresh_snapshot()
                snap["timestamp"] = timestamp
                self.assertEqual(packages.load_quant_factor_tiers(ITEM["instId"], snapshot=snap), {})

    def test_missing_none_partial_duplicate_or_unusable_current_snapshot(self):
        cases = [None, {}, {"timestamp": time.time(), "instruments": []}]
        for mutate in (lambda item: item.pop("trend_momentum"),
                       lambda item: item.update(price=0),
                       lambda item: item.update(price=True)):
            snap = fresh_snapshot()
            mutate(snap["instruments"][0])
            cases.append(snap)
        duplicate = fresh_snapshot()
        duplicate["instruments"].append(fresh_item())
        cases.append(duplicate)
        for snap in cases:
            with self.subTest(snap=snap):
                self.assertEqual(packages.load_quant_factor_tiers(ITEM["instId"], snapshot=snap), {})

    def test_empty_and_placeholder_tiers_do_not_establish_completeness(self):
        for tier in packages.QUANT_FACTOR_TIERS:
            for block in ({}, {"placeholder": True}):
                with self.subTest(tier=tier, block=block):
                    snap = fresh_snapshot()
                    snap["instruments"][0][tier] = block
                    self.assertEqual(packages.load_quant_factor_tiers(ITEM["instId"], snapshot=snap), {})

    def test_documented_missing_optional_source_values_remain_usable(self):
        snap = fresh_snapshot()
        snap["instruments"][0]["options_structure"]["available"] = False
        snap["instruments"][0]["smart_money_derivatives"]["elite_account_ratio"] = None
        self.assertTrue(packages.load_quant_factor_tiers(ITEM["instId"], snapshot=snap))

    def _write_prompt(self, path, builder=lambda **kw: "fresh prompt"):
        snapshots.write_prompt_snapshot(AI_LAST_PROMPT_FILE=str(path),
            _build_effective_prompt_text=builder, effective_system_prompt="system",
            os=os, prompt="prompt", time_str="time")

    def test_prompt_late_render_preserves_previous(self):
        clock = [100.0]
        def render(**kwargs):
            clock[0] += 2
            return "late"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.txt"
            path.write_text("previous")
            with patch.object(deadline.time, "monotonic", lambda: clock[0]), deadline.deadline_scope(timeout=1):
                with self.assertRaises(deadline.DeadlineExceeded):
                    self._write_prompt(path, render)
            self.assertEqual(path.read_text(), "previous")
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_prompt_fsync_cancellation_prevents_publication(self):
        real_fsync = os.fsync
        with tempfile.TemporaryDirectory() as tmp, deadline.cancellation_scope() as token:
            path = Path(tmp) / "prompt.txt"
            path.write_text("previous")
            def cancel(fd):
                real_fsync(fd)
                token.set()
            with patch.object(os, "fsync", cancel), self.assertRaises(deadline.DeadlineExceeded):
                self._write_prompt(path)
            self.assertEqual(path.read_text(), "previous")
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_refresh_returns_exact_object_and_propagates_failure(self):
        import types, sys
        value = fresh_snapshot()
        fake = types.SimpleNamespace(update_factor_library=Mock(return_value=value))
        with patch.dict(sys.modules, {"factor_library": fake}):
            self.assertIs(snapshots.update_factor_library_snapshot(WORKSPACE_DIR=brain.WORKSPACE_DIR,
                          os=os, sys=sys), value)
            fake.update_factor_library.side_effect = deadline.DeadlineExceeded()
            with self.assertRaises(deadline.DeadlineExceeded):
                snapshots.update_factor_library_snapshot(WORKSPACE_DIR=brain.WORKSPACE_DIR, os=os, sys=sys)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(self.tmp)
        self.snapshot = fresh_snapshot()
        self.package = {**ITEM, "price": 100, "bidPx": 99, "askPx": 101, "data_quality": "valid",
                        "recent_15m": [[100, 101, 99, 100, 1]] * 12,
                        "recent_1h": [[100, 101, 99, 100, 1]] * 8,
                        "recent_4h": [[100, 101, 99, 100, 1]] * 6,
                        "quant_factors": {"old": True}}
        def patcher(target, name, **kwargs):
            return self.stack.enter_context(patch.object(target, name, **kwargs))
        patcher(brain, "DATA_DIR", new=self.tmp)
        patcher(brain, "AI_BRAIN_LOCK_FILE", new=str(self.path / "brain.lock"))
        patcher(brain, "AI_LAST_PROMPT_FILE", new=str(self.path / "prompt.txt"))
        patcher(brain, "TARGET_INSTRUMENTS", new=[dict(ITEM)])
        self.stack.enter_context(patch.dict(os.environ, {"ASTRA_JOB_RUN_ID": "42",
            "ASTRA_SCHEDULED_AT": "2026-10-13T17:00:00+08:00",
            "ASTRA_EXECUTION_DEADLINE_EPOCH": "", "ASTRA_INFERENCE_DEADLINE_EPOCH": ""}))
        patcher(brain, "get_cpa_client_config", return_value=("mock", "mock"))
        patcher(brain, "capture_policy_snapshot", return_value=("hash", {}, "summary", "version"))
        self.collect = patcher(brain, "_collect_packages", side_effect=lambda targets: [copy.deepcopy(self.package)])
        self.refresh = patcher(brain, "update_factor_library_snapshot", side_effect=lambda **kw: self.snapshot)
        self.smart = patcher(smart_money, "fetch_smart_money_for_symbol", return_value={})
        self.pending = patcher(brain, "fetch_pending_orders_list", return_value=[])
        self.prompt = patcher(brain, "construct_full_market_prompt", return_value="prompt")
        patcher(brain, "active_profile", return_value={})
        patcher(brain, "get_effective_system_prompt", return_value="system")
        patcher(brain, "_build_effective_prompt_text", return_value="rendered")
        patcher(brain, "resolve_llm_runtime", return_value=("openai", "mock", "mock", "high", Mock(), "mock", 30))
        patcher(brain, "ModelCallTelemetry", return_value=Mock())
        self.dispatch = patcher(brain, "dispatch_llm_and_persist_decisions", return_value={"complete": True})

    def assert_blocked(self):
        self.assertIsNone(brain.execute_batch_ai_brain_cycle())
        self.dispatch.assert_not_called()
        self.prompt.assert_not_called()
        self.assertFalse((self.path / "prompt.txt").exists())

    def test_happy_path_consumes_current_snapshot(self):
        self.assertEqual(brain.execute_batch_ai_brain_cycle(), {"complete": True})
        package = self.dispatch.call_args.kwargs["packages"][0]
        self.assertEqual(package["quant_factors"]["trend_momentum"]["marker"], "current")
        self.assertNotIn("old", package["quant_factors"])
        self.assertEqual((self.path / "prompt.txt").read_text(), "rendered")

    def test_refresh_failure_does_not_fall_back_to_old_file(self):
        (self.path / "factor_library_snapshot.json").write_text(json.dumps(self.snapshot))
        self.refresh.side_effect = RuntimeError("unavailable")
        self.assert_blocked()
        self.assertEqual(brain.read_cycle_health()["last_status"], "failed")

    def test_none_refresh_stops_before_model(self):
        self.refresh.side_effect = lambda **kw: None
        self.assert_blocked()

    def test_missing_instrument_stops_before_model(self):
        self.snapshot["instruments"] = []
        self.assert_blocked()

    def test_stale_snapshot_stops_before_model(self):
        self.snapshot["timestamp"] = time.time() - 181
        self.assert_blocked()

    def test_bad_market_data_stops_before_refresh(self):
        self.package["data_quality"] = "invalid"
        self.assert_blocked()
        self.refresh.assert_not_called()

    def test_market_valid_flag_cannot_hide_missing_fields(self):
        self.package.pop("recent_1h")
        self.assert_blocked()
        self.refresh.assert_not_called()

    def test_nonfinite_ticker_is_not_valid_market_data(self):
        self.package["price"] = float("inf")
        self.assert_blocked()
        self.refresh.assert_not_called()

    def test_nonfinite_and_malformed_candle_values_stop_before_refresh(self):
        for row in ([float("nan"), 1, 1, 1, 0], [1, 1, 1, 1, float("inf")],
                    [True, 1, 1, 1, 0], [1, 1, 1], "invalid"):
            with self.subTest(row=row):
                self.package["recent_1h"] = [row] * 8
                self.assert_blocked()
        self.refresh.assert_not_called()

    def test_zero_volume_candles_remain_valid(self):
        for name in ("recent_15m", "recent_1h", "recent_4h"):
            self.package[name] = [[100, 101, 99, 100, 0]] * len(self.package[name])
        self.assertEqual(brain.execute_batch_ai_brain_cycle(), {"complete": True})

    def test_factor_failure_releases_real_flock(self):
        self.refresh.side_effect = deadline.DeadlineExceeded()
        self.assert_blocked()
        with open(brain.AI_BRAIN_LOCK_FILE, "a+") as handle:
            brain.fcntl.flock(handle.fileno(), brain.fcntl.LOCK_EX | brain.fcntl.LOCK_NB)
            brain.fcntl.flock(handle.fileno(), brain.fcntl.LOCK_UN)

    def test_failure_health_correlates_job_phase_without_exception_text(self):
        self.refresh.side_effect = deadline.DeadlineExceeded("SECRET_CONTENT")
        self.assert_blocked()
        health = brain.read_cycle_health()
        self.assertEqual(health["job_run_id"], 42)
        self.assertEqual(health["scheduled_at"], os.environ["ASTRA_SCHEDULED_AT"])
        self.assertEqual(health["collection_phase"], "factor_refresh")
        self.assertTrue(any(p["phase"] == "factor_refresh" and p["result"] == "DeadlineExceeded"
                            for p in health["collection_timings"]))
        self.assertNotIn("SECRET_CONTENT", json.dumps(health))

    def test_pending_orders_overlap_factor_refresh(self):
        entered, release = threading.Event(), threading.Event()
        def refresh(**kwargs):
            entered.set()
            if not release.wait(1):
                raise RuntimeError("pending orders did not overlap")
            return self.snapshot
        def pending():
            self.assertTrue(entered.wait(1))
            release.set()
            return []
        self.refresh.side_effect = refresh
        self.pending.side_effect = pending
        try:
            self.assertEqual(brain.execute_batch_ai_brain_cycle(), {"complete": True})
        finally:
            release.set()

    def test_pending_order_deadline_propagates(self):
        # The facade itself is patched above; invoke the original from the baseline function.
        self.stack.close()
        with patch.object(brain.okx_rest, "pending_orders", side_effect=deadline.DeadlineExceeded()):
            with self.assertRaises(deadline.DeadlineExceeded):
                brain.fetch_pending_orders_list()

    def test_skipped_health_preserves_failure_counts(self):
        brain._record_cycle_health("failed", "synthetic")
        with patch.dict(os.environ, {"ASTRA_JOB_RUN_ID": "43"}):
            brain._record_cycle_health("skipped", "inference_lock_active")
        health = brain.read_cycle_health()
        self.assertEqual((health["last_status"], health["job_run_id"]), ("skipped", 43))
        self.assertEqual((health["consecutive_failures"], health["total_failures"]), (1, 1))


class DiagnosticTests(unittest.TestCase):
    def test_logs_only_allowlisted_labels_and_safe_endpoint(self):
        output = io.StringIO()
        with redirect_stdout(output), collection.collection_trace(), \
             collection.collection_phase("SECRET_PHASE"), \
             collection.endpoint_timing("SECRET_METHOD", "/api/v5/market/ticker?secret=BODY", "https://secret.example") as info:
            info["result"] = "SECRET_RESULT"
        text = output.getvalue()
        self.assertNotIn("SECRET", text)
        self.assertNotIn("BODY", text)
        self.assertNotIn("secret.example", text)
        self.assertIn('"phase": "other"', text)
        self.assertIn('"endpoint": "/api/v5/market/ticker"', text)

    def test_shared_phase_records_are_bounded_and_copied(self):
        with redirect_stdout(io.StringIO()), collection.collection_trace():
            for _ in range(80):
                with collection.collection_phase("market_packages"):
                    pass
            result = collection.collection_diagnostics()
            self.assertEqual(len(result["collection_timings"]), 64)
            result["collection_timings"][0]["phase"] = "changed"
            self.assertEqual(collection.collection_diagnostics()["collection_timings"][0]["phase"], "market_packages")


if __name__ == "__main__":
    unittest.main()
