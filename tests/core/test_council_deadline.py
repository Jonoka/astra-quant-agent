"""Mock-only shared council deadlines; no provider requests or config writes."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from astra_backend import deadline
from astra_backend.council import debate


RESOLVED = {"model": "requested", "base_url": "mock", "api_key": "mock",
            "api_format": "openai_chat", "effort": "high", "requested": "requested",
            "registered": True, "fallback": False, "reason": ""}


class CouncilDeadlineTests(unittest.TestCase):
    def test_seat_524_retry_spends_only_the_same_seat_deadline(self):
        clock = [100.0]
        calls = []
        def llm(**kwargs):
            calls.append((kwargs["timeout"], deadline.current_deadline()))
            if len(calls) == 1:
                clock[0] += 60
                raise TimeoutError("HTTP 524 upstream timeout")
            return "proposal", "", {"_astra_trace": {"model": "actual-fallback"}}, 1
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch.object(deadline.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch("astra_backend.llm_manager.execute_llm_request", llm):
            result = debate._call_single_trader(lambda _: RESOLVED, "seat", {}, "market", "system", 100)
        self.assertEqual(calls, [(100.0, 200.0), (38.5, 200.0)])
        self.assertEqual(result["model_used"], "actual-fallback")
        self.assertEqual(result["status"], "ok")

    def test_seat_deadline_exhaustion_does_not_retry_or_return_success(self):
        clock = [10.0]
        call = Mock()
        def fail(**kwargs):
            call()
            clock[0] += 40
            raise TimeoutError("upstream cancelled")
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch("astra_backend.llm_manager.execute_llm_request", fail):
            with self.assertRaises(deadline.DeadlineExceeded):
                debate._call_single_trader(lambda _: RESOLVED, "seat", {}, "market", "system", 40)
        call.assert_called_once()

    def test_retry_with_insufficient_backoff_and_reasoning_time_is_not_started(self):
        clock = [10.0]
        call = Mock()
        def fail(**kwargs):
            call()
            clock[0] += 36
            raise TimeoutError("HTTP 524")
        with patch.object(deadline.time, "monotonic", lambda: clock[0]), \
             patch("astra_backend.llm_manager.execute_llm_request", fail):
            result = debate._call_single_trader(lambda _: RESOLVED, "seat", {}, "market", "system", 40)
        call.assert_called_once()
        self.assertEqual(result["status"], "error")

    def test_stage_wait_is_bounded_and_workers_inherit_parent_context(self):
        release, started = threading.Event(), threading.Event()
        inherited = []
        def slow(*args):
            inherited.append(deadline.current_deadline())
            started.set()
            release.wait(1)
            return {"status": "ok"}
        before = time.monotonic()
        try:
            with deadline.deadline_scope(timeout=0.5) as parent, \
                 patch.object(debate, "MIN_SAFE_REASONING_TIME", 0.001):
                result = debate._run_parallel_seats(
                    {"seat": {}}, ["seat"], slow, lambda key, left: (), 0.04)
            self.assertTrue(started.is_set())
            self.assertLess(time.monotonic() - before, 0.3)
            self.assertLessEqual(inherited[0], parent)
            self.assertEqual(result["seat"]["reason"], "stage_deadline_exhausted")
        finally:
            release.set()

    def test_insufficient_cio_time_never_starts_request(self):
        request = Mock()
        with patch("astra_backend.llm_manager.execute_llm_request", request), \
             deadline.deadline_scope(timeout=4):
            with self.assertRaises(TimeoutError):
                debate.execute_council_debate(
                    lambda: {"roles": {}}, lambda _: RESOLVED, Mock(), Mock(), "market", "system", 420)
        request.assert_not_called()

    def test_normal_modes_return_same_fresh_output_and_cio_trace(self):
        roles = {"cio": {"is_arbitrator": True}, "seat": {"name": "seat"}}
        trace = {"model": "actual-cio", "request_id": "server-id"}
        def llm(**kwargs):
            self.assertLessEqual(kwargs["timeout"], 30)
            self.assertEqual(deadline.cycle_metadata()["caller"], "council.cio")
            return '{"decisions": {}, "_astra_trace": {"request_id": "fabricated"}}', "", {"_astra_trace": trace}, 1
        for mode in ("standard", "cross_examination", "debate"):
            with self.subTest(mode=mode), patch("astra_backend.llm_manager.execute_llm_request", llm), \
                 patch.object(debate, "_normalize_cio_adopted_roles"), deadline.deadline_scope(timeout=30):
                out, transcript = debate.execute_council_debate(
                    lambda: {"roles": roles, "consensus_mode": mode}, lambda _: RESOLVED,
                    lambda *args: {"status": "ok", "content": "WAIT", "weight": 1},
                    lambda *args: {"status": "ok", "content": "critique"}, "market", "system", 420)
            self.assertIs(out["council_transcript"], transcript)
            self.assertIs(out["_astra_trace"], trace)
            self.assertEqual(transcript["arbitrator"]["model_used"], "actual-cio")


if __name__ == "__main__":
    unittest.main()
