"""Six baseline-compatible behavioral fault probes, with no new-API imports.

The exact same module is run in the immutable-base copy and the candidate.
External HTTP/model/trading paths are explicit test doubles under OfflineGuard.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from astra_backend import deadline
from scripts import ai_brain_trader as brain, factor_library as factors
from scripts.brain import snapshots
from scripts.factors import smart_money

ITEM = {"instId": "BTC-USDT-SWAP", "name": "BTC", "type": "crypto", "precision": 2}


class BaselineCompatibleFaultTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(self.tmp) / "factor.json"
        self.path.write_text('{"previous":true}')
        for name, value in (("DATA_DIR", self.tmp), ("FACTOR_LIB_CACHE_FILE", str(self.path)),
                            ("TARGET_INSTRUMENTS", [dict(ITEM)])):
            self.stack.enter_context(patch.object(factors, name, value))
        self.pool = self.stack.enter_context(patch.object(smart_money, "fetch_smart_money_pool", return_value={}))
        self.stack.enter_context(patch.dict(os.environ, {
            "ASTRA_EXECUTION_DEADLINE_EPOCH": "", "ASTRA_INFERENCE_DEADLINE_EPOCH": ""}))

    def test_factor_worker_inherits_deadline(self):
        seen = []
        def compute(*args):
            seen.append(deadline.current_deadline())
            return dict(ITEM)
        with patch.object(factors, "compute_instrument_factors", compute), deadline.deadline_scope(timeout=5):
            expected = deadline.current_deadline()
            factors.update_factor_library()
        self.assertEqual(seen, [expected])

    def test_factor_wait_does_not_join_past_deadline(self):
        finished = threading.Event()
        def compute(*args):
            time.sleep(.25)
            finished.set()
            return dict(ITEM)
        caught = None
        started = time.monotonic()
        try:
            with patch.object(factors, "compute_instrument_factors", compute), deadline.deadline_scope(timeout=.04):
                try:
                    factors.update_factor_library()
                except deadline.DeadlineExceeded as exc:
                    caught = exc
            elapsed = time.monotonic() - started
        finally:
            finished.wait(2)
        self.assertLess(elapsed, .18, "owner joined the slow worker beyond its deadline")
        self.assertIsInstance(caught, deadline.DeadlineExceeded)
        self.assertEqual(json.loads(self.path.read_text()), {"previous": True})

    def test_factor_fsync_expiry_rejects_publication(self):
        clock = [100.0]
        original = os.fsync
        def expire(fd):
            original(fd)
            clock[0] += 2
        with patch.object(factors, "compute_instrument_factors", return_value=dict(ITEM)), \
             patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch.object(os, "fsync", expire), deadline.deadline_scope(timeout=1):
            with self.assertRaises(deadline.DeadlineExceeded):
                factors.update_factor_library()
        self.assertEqual(json.loads(self.path.read_text()), {"previous": True})
        self.assertEqual(list(Path(self.tmp).iterdir()), [self.path])

    def test_pool_deadline_is_not_a_missing_optional_source(self):
        self.pool.side_effect = deadline.DeadlineExceeded("synthetic timeout")
        with patch.object(factors, "compute_instrument_factors", return_value=dict(ITEM)) as compute:
            with self.assertRaises(deadline.DeadlineExceeded):
                factors.update_factor_library()
        compute.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), {"previous": True})

    def test_late_prompt_render_cannot_replace_snapshot(self):
        clock = [100.0]
        path = Path(self.tmp) / "prompt.txt"
        path.write_text("previous")
        def render(**kw):
            clock[0] += 2
            return "late"
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), deadline.deadline_scope(timeout=1):
            with self.assertRaises(deadline.DeadlineExceeded):
                snapshots.write_prompt_snapshot(AI_LAST_PROMPT_FILE=str(path),
                    _build_effective_prompt_text=render, effective_system_prompt="system",
                    os=os, prompt="prompt", time_str="time")
        self.assertEqual(path.read_text(), "previous")

    def test_missing_current_factor_stops_before_prompt_and_model(self):
        package = {**ITEM, "price": 100., "bidPx": 99., "askPx": 101., "data_quality": "valid",
                   "recent_15m": [[100., 101., 99., 100., 0.]] * 12,
                   "recent_1h": [[100., 101., 99., 100., 0.]] * 8,
                   "recent_4h": [[100., 101., 99., 100., 0.]] * 6,
                   "quant_factors": {"old_factor": True}}
        values = {
            "DATA_DIR": self.tmp, "AI_BRAIN_LOCK_FILE": str(Path(self.tmp) / "brain.lock"),
            "AI_LAST_PROMPT_FILE": str(Path(self.tmp) / "prompt.txt"), "TARGET_INSTRUMENTS": [dict(ITEM)],
            "get_cpa_client_config": Mock(return_value=("mock", "mock")),
            "capture_policy_snapshot": Mock(return_value=("hash", {}, "summary", "version")),
            "_collect_packages": Mock(return_value=[package]),
            "update_factor_library_snapshot": Mock(return_value=None),
            "fetch_pending_orders_list": Mock(return_value=[]),
            "construct_full_market_prompt": Mock(return_value="prompt"),
            "active_profile": Mock(return_value={}), "get_effective_system_prompt": Mock(return_value="system"),
            "write_prompt_snapshot": Mock(), "ModelCallTelemetry": Mock(),
            "resolve_llm_runtime": Mock(return_value=("openai", "mock", "mock", "high", Mock(), "mock", 30)),
            "dispatch_llm_and_persist_decisions": Mock(return_value={"unexpected": True}),
        }
        with ExitStack() as stack:
            for name, value in values.items():
                stack.enter_context(patch.object(brain, name, value))
            stack.enter_context(patch.object(smart_money, "fetch_smart_money_for_symbol", return_value={}))
            self.assertIsNone(brain.execute_batch_ai_brain_cycle())
        values["construct_full_market_prompt"].assert_not_called()
        values["dispatch_llm_and_persist_decisions"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
