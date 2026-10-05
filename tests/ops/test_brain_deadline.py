"""Mock-only whole-cycle deadline, persistence and correlation regressions."""
import os
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from astra_backend import deadline
from scripts import ai_brain_trader as brain
from tests.ops.test_brain_dispatch import _Harness


class BrainDispatchDeadlineTests(_Harness):
    def test_exhausted_council_does_not_start_fallback_or_publish_fresh_history(self):
        clock = [100.0]
        fallback = Mock()
        def council(**kwargs):
            clock[0] += 60
            raise TimeoutError("mock upstream cancelled")
        self.council_cfg = {"enabled": True, "timeout_seconds": 420}
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), deadline.deadline_scope(timeout=120):
            result = self._run(debate_fn=council, execute_llm_request=fallback)
        self.assertIsNone(result)
        fallback.assert_not_called()
        self.assertEqual(self.written, {})
        self.assertEqual(self.health[0][0], "failed")
        self.assertEqual(self.telemetry.calls[0][0], ("failed",))

    def test_fallback_spends_only_remaining_whole_cycle_budget(self):
        clock = [100.0]
        calls = []
        def council(**kwargs):
            clock[0] += 40
            raise TimeoutError("mock HTTP 524")
        def fallback(**kwargs):
            calls.append((kwargs["timeout"], deadline.current_deadline()))
            return '{"decisions": {}}', "", {}, 1
        self.council_cfg = {"enabled": True, "timeout_seconds": 420}
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), deadline.deadline_scope(timeout=120):
            result = self._run(debate_fn=council, execute_llm_request=fallback, thinking_timeout=300)
        self.assertIs(result, self.written[self.paths["cache"]])
        self.assertEqual(calls, [(20.0, 160.0)])
        self.assertEqual(self.health, [("ok",)])

    def test_late_single_model_result_never_becomes_fresh_success(self):
        clock = [100.0]
        def fallback(**kwargs):
            clock[0] += 31
            return '{"decisions": {}}', "", {}, 1
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), deadline.deadline_scope(timeout=120):
            result = self._run(execute_llm_request=fallback, thinking_timeout=30)
        self.assertIsNone(result)
        self.assertEqual(self.written, {})
        self.assertEqual(self.health[0][0], "failed")

    def test_too_little_time_for_fallback_fails_before_request(self):
        request = Mock()
        with deadline.deadline_scope(timeout=64):
            result = self._run(execute_llm_request=request)
        self.assertIsNone(result)
        request.assert_not_called()
        self.assertEqual(self.written, {})

    def test_model_scope_reserves_last_minute_and_restores_it_for_persistence(self):
        clock = [100.0]
        model_deadlines, persistence_budgets = [], []
        def request(**kwargs):
            model_deadlines.append((kwargs["timeout"], deadline.current_deadline()))
            clock[0] += 119
            return '{"decisions": {}}', "", {}, 1
        def write(path, value):
            persistence_budgets.append((deadline.current_deadline(), deadline.remaining()))
            self._atomic_write(path, value)
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), deadline.deadline_scope(timeout=180):
            result = self._run(execute_llm_request=request, thinking_timeout=300, atomic_write_json=write)
        self.assertIs(result, self.written[self.paths["cache"]])
        self.assertEqual(model_deadlines, [(120.0, 220.0)])
        self.assertEqual(persistence_budgets, [(280.0, 61.0)] * 3)
        self.assertEqual(self.health, [("ok",)])

    def test_model_arriving_inside_persistence_reserve_is_rejected(self):
        clock = [100.0]
        def request(**kwargs):
            clock[0] += 121
            return '{"decisions": {}}', "", {}, 1
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), deadline.deadline_scope(timeout=180):
            result = self._run(execute_llm_request=request, thinking_timeout=300)
        self.assertIsNone(result)
        self.assertEqual(self.written, {})
        self.assertEqual(self.health[0][0], "failed")

    def test_actual_fallback_request_metadata_round_trips_without_secrets(self):
        trace = {"model": "actual-fallback", "request_id": "server-42", "client_request_id": "client-42",
                 "started_at": "2026-10-05T17:01:00+08:00", "completed_at": "2026-10-05T17:02:00+08:00",
                 "api_key": "MUST_NOT_PERSIST", "prompt": "MUST_NOT_PERSIST"}
        usage = {"total_tokens": 7, "_astra_trace": trace}
        with patch.dict(os.environ, {"ASTRA_JOB_RUN_ID": "42", "ASTRA_SCHEDULED_AT": "2026-10-05T17:00:00+08:00"}):
            result = self._run(brain_output=None, llm_result=('{"decisions": {}}', "", usage, 1))
        self.assertIs(result, self.written[self.paths["cache"]])
        execution = self.written[self.paths["history"]][0]["execution"]
        self.assertEqual(execution["job_run_id"], 42)
        self.assertEqual(execution["actual_model"], "actual-fallback")
        self.assertEqual(execution["request_id"], "server-42")
        self.assertEqual(execution["request_started_at"], trace["started_at"])
        self.assertEqual(execution["request_completed_at"], trace["completed_at"])
        self.assertNotIn("MUST_NOT_PERSIST", str(execution))
        self.assertEqual(self.telemetry.calls[0][0][1]["usage"]["_astra_trace"]["model"], "actual-fallback")

    def test_council_cio_identity_is_recorded_with_token_usage_still_unknown(self):
        trace = {"model": "actual-cio", "request_id": "cio-id"}
        self.council_cfg = {"enabled": True}
        self.council_result = ({"decisions": {}, "_astra_trace": trace}, {"advisors": {}})
        result = self._run()
        self.assertIs(result, self.written[self.paths["cache"]])
        self.assertEqual(self.written[self.paths["history"]][0]["execution"]["actual_model"], "actual-cio")
        self.assertEqual(self.telemetry.calls[0][0][1], {"usage": {"_astra_trace": trace}})

    def test_legacy_http_path_uses_correlated_transport_without_fabricating_server_id(self):
        from astra_backend.llm import transport
        from tests.ops.test_brain_dispatch import _Resp
        response = _Resp({"choices": [{"message": {"content": '{"decisions": {}}'}}]})
        response.headers = {}
        attempts = []
        self._patch_council()
        # _run patches the recorder for the existing harness; use its captured
        # keyword arguments once and then invoke dispatch under our recorder.
        self._run()
        self.written.clear()
        self.health.clear()
        self.kw["execute_llm_request"] = None
        with patch.object(transport, "_record_http_attempt", lambda record: attempts.append(dict(record))), \
             patch.object(transport.urllib.request, "urlopen", return_value=response):
            from scripts.brain.dispatch import dispatch_llm_and_persist_decisions
            result = dispatch_llm_and_persist_decisions(**self.kw)
        self.assertIs(result, self.written[self.paths["cache"]])
        self.assertEqual([item["status"] for item in attempts], ["running", "success"])
        metadata = self.written[self.paths["history"]][0]["execution"]
        self.assertEqual(metadata["actual_model"], "m1")
        self.assertEqual(metadata["client_request_id"], attempts[1]["client_request_id"])
        self.assertNotIn("request_id", metadata)
        self.assertNotIn("SECRET", json.dumps(attempts))


class BrainCollectionDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for item in (patch.object(brain, "DATA_DIR", self.tmp.name),
                     patch.object(brain, "AI_BRAIN_LOCK_FILE", os.path.join(self.tmp.name, "brain.lock"))):
            item.start()
            self.addCleanup(item.stop)

    def test_collection_wait_does_not_join_overdue_worker_or_accept_partial_packages(self):
        release, started = threading.Event(), threading.Event()
        contexts = []
        def fetch(instrument):
            contexts.append(deadline.current_deadline())
            if instrument == "slow":
                started.set()
                release.wait(1)
            return {"instId": instrument}
        before = time.monotonic()
        try:
            with patch.object(brain, "fetch_single_instrument_package", fetch), deadline.deadline_scope(timeout=0.04) as parent:
                with self.assertRaises(deadline.DeadlineExceeded):
                    brain._collect_packages(["fast", "slow"])
            self.assertTrue(started.is_set())
            self.assertLess(time.monotonic() - before, 0.3)
            self.assertTrue(all(value == parent for value in contexts))
        finally:
            release.set()

    def test_expired_collection_does_not_start_new_dependency(self):
        fetch = Mock()
        with patch.object(brain, "fetch_single_instrument_package", fetch):
            with self.assertRaises(deadline.DeadlineExceeded):
                with deadline.deadline_scope(timeout=0):
                    brain._collect_packages(["one"])
        fetch.assert_not_called()

    def test_queued_collection_worker_checks_expiry_before_starting_dependency(self):
        clock = [100.0]
        dependency = Mock()
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), deadline.deadline_scope(timeout=10):
            clock[0] += 11
            with self.assertRaises(deadline.DeadlineExceeded):
                brain._collection_worker(dependency)
        dependency.assert_not_called()

    def test_whole_cycle_caps_collection_and_reserves_120_seconds_for_inference(self):
        clock = [100.0]
        observed = []
        health, dispatch = Mock(), Mock()
        def collect(instruments):
            observed.append(deadline.remaining())
            clock[0] += observed[-1]
            raise deadline.DeadlineExceeded("mock slow collection")
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch.dict(os.environ, {"ASTRA_INFERENCE_DEADLINE_EPOCH": ""}), \
             patch.object(brain, "get_cpa_client_config", return_value=("mock", "mock")), \
             patch.object(brain, "capture_policy_snapshot", return_value=("hash", {}, "summary", "version")), \
             patch.object(brain, "_collect_packages", collect), \
             patch.object(brain, "_record_cycle_health", health), \
             patch.object(brain, "dispatch_llm_and_persist_decisions", dispatch), deadline.deadline_scope(timeout=260):
            result = brain.execute_batch_ai_brain_cycle()
        self.assertEqual(observed, [80.0])
        self.assertIsNone(result)
        health.assert_called_once_with("failed", "collection_deadline_exhausted")
        dispatch.assert_not_called()

    def test_collection_is_capped_even_when_unscheduled_cycle_has_more_time(self):
        observed = []
        def collect(instruments):
            observed.append(deadline.remaining())
            raise deadline.DeadlineExceeded("mock")
        with patch.dict(os.environ, {"ASTRA_INFERENCE_DEADLINE_EPOCH": ""}), \
             patch.object(brain, "get_cpa_client_config", return_value=("mock", "mock")), \
             patch.object(brain, "capture_policy_snapshot", return_value=("hash", {}, "summary", "version")), \
             patch.object(brain, "_collect_packages", collect), \
             patch.object(brain, "_record_cycle_health"):
            self.assertIsNone(brain.execute_batch_ai_brain_cycle())
        self.assertGreater(observed[0], 179)
        self.assertLessEqual(observed[0], 180)

    def test_cycle_reentry_is_rejected_before_collection_inference_or_success(self):
        held = [False]
        collection, health, dispatch = Mock(), Mock(), Mock()
        def flock(fd, mode):
            if mode & brain.fcntl.LOCK_NB:
                if held[0]:
                    raise BlockingIOError("mock locked")
                held[0] = True
            elif mode == brain.fcntl.LOCK_UN:
                held[0] = False
        def collect(instruments):
            collection()
            self.assertIsNone(brain.execute_batch_ai_brain_cycle())
            raise deadline.DeadlineExceeded("mock collection failure")
        with patch.object(brain.fcntl, "flock", flock), \
             patch.object(brain, "get_cpa_client_config", return_value=("mock", "mock")), \
             patch.object(brain, "capture_policy_snapshot", return_value=("hash", {}, "summary", "version")), \
             patch.object(brain, "_collect_packages", collect), \
             patch.object(brain, "_record_cycle_health", health), \
             patch.object(brain, "dispatch_llm_and_persist_decisions", dispatch):
            self.assertIsNone(brain.execute_batch_ai_brain_cycle())
        collection.assert_called_once()
        dispatch.assert_not_called()
        self.assertEqual(health.call_args_list, [call("skipped", "inference_lock_active"),
                                                 call("failed", "collection_deadline_exhausted")])
        self.assertFalse(held[0])

    def test_pending_margin_helper_does_not_acquire_the_whole_cycle_lock(self):
        with patch.object(brain.fcntl, "flock") as flock:
            brain._pending_order_margin_usdt({"sz": "1", "px": "1", "lever": "2", "instId": "mock"})
        flock.assert_not_called()

    def test_active_cycle_lock_records_skip_before_reading_runtime_or_starting_request(self):
        health, runtime, collection, dispatch = Mock(), Mock(), Mock(), Mock()
        with patch.object(brain.fcntl, "flock", side_effect=BlockingIOError("mock active")), \
             patch.object(brain, "_record_cycle_health", health), \
             patch.object(brain, "get_cpa_client_config", runtime), \
             patch.object(brain, "_collect_packages", collection), \
             patch.object(brain, "dispatch_llm_and_persist_decisions", dispatch):
            self.assertIsNone(brain.execute_batch_ai_brain_cycle())
        health.assert_called_once_with("skipped", "inference_lock_active")
        runtime.assert_not_called()
        collection.assert_not_called()
        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
