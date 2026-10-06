"""Deterministic mock-only regression coverage for logical LLM deadlines."""
import io
import json
import socket
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from astra_backend import deadline
from astra_backend.llm import call, transport


class Clock:
    now = 100.0

    def advance(self, seconds):
        self.now += seconds


class Response:
    def __init__(self, clock, *, delay=0, headers=None, payload=None, chunks=None):
        self.clock, self.delay = clock, delay
        self.headers = headers or {}
        payload = payload or {"id": "not-a-new-api-id", "choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 4}}
        self.chunks = iter(chunks if chunks is not None else [json.dumps(payload).encode(), b""])
        self.timeouts = []
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=self.timeouts.append)))
        self.closed = False

    def read1(self, size):
        self.clock.advance(self.delay)
        return next(self.chunks, b"")

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def http_error(code, body="upstream", request_id=""):
    return urllib.error.HTTPError("https://mock.invalid", code, "mock error", {"X-Request-Id": request_id}, io.BytesIO(body.encode()))


class LlmDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.records = []
        self.starts = []
        self.real_record = transport._record_http_attempt
        self.events = []
        self.stack = [
            patch.object(deadline.time, "monotonic", lambda: self.clock.now),
            patch.object(transport.time, "perf_counter", lambda: self.clock.now),
            patch.object(deadline.time, "sleep", self.clock.advance),
            patch.object(transport, "_record_http_attempt", lambda record: (self.starts if record["status"] == "running" else self.records).append(record)),
            patch.object(transport.urllib.request, "urlopen", side_effect=AssertionError("unmocked network forbidden")),
        ]
        for item in self.stack:
            item.start()
            self.addCleanup(item.stop)
        self.runtime = {"model": "primary", "base_url": "https://mock.invalid/v1", "api_key": "mock-secret", "thinking_timeout": 10, "request_attempts": 3, "fallback_model_ids": ["backup"]}

    def execute(self, **kwargs):
        return call.execute_llm_request(
            lambda: dict(self.runtime),
            lambda name: {**self.runtime, "model": name, "thinking_timeout": 999},
            self.events.append, [{"role": "user", "content": "private-prompt"}], **kwargs,
        )

    def test_normal_success_trace_matches_safe_attempt(self):
        with patch.object(transport.urllib.request, "urlopen", return_value=Response(self.clock, headers={"X-Newapi-Request-Id": "upstream-123"})), deadline.request_scope("seat:analyst"), patch.dict("os.environ", {"ASTRA_JOB_RUN_ID": "42", "ASTRA_SCHEDULED_AT": "2026-10-05T12:00:00+08:00"}):
            content, reasoning, usage, latency = self.execute()
        self.assertEqual((content, reasoning, latency), ("ok", "", 0))
        trace = usage["_astra_trace"]
        self.assertEqual(trace["request_id"], "upstream-123")
        self.assertEqual(trace["model"], "primary")
        self.assertEqual(self.records[0]["client_request_id"], trace["client_request_id"])
        self.assertEqual(self.records[0]["caller"], "seat:analyst")
        self.assertEqual(self.records[0]["job_run_id"], 42)
        self.assertEqual(self.starts[0]["client_request_id"], trace["client_request_id"])
        self.assertNotIn("completed_at", self.starts[0])
        serialized = json.dumps(self.records)
        for secret in ("mock-secret", "private-prompt", "mock.invalid", "not-a-new-api-id"):
            self.assertNotIn(secret, serialized)

    def test_missing_header_does_not_use_response_body_id(self):
        with patch.object(transport.urllib.request, "urlopen", return_value=Response(self.clock)):
            usage = self.execute()[2]
        self.assertEqual(usage["_astra_trace"]["request_id"], "")

    def test_known_response_identity_is_recorded_before_body_read_can_block(self):
        def blocked_body(response):
            self.assertEqual(self.starts[-1]["request_id"], "known-server-id")
            self.assertEqual(self.starts[-1]["http_status"], 200)
            self.assertEqual(len({item["client_request_id"] for item in self.starts}), 1)
            self.clock.advance(10)
            raise deadline.DeadlineExceeded("synthetic body deadline")
        with patch.object(transport.urllib.request, "urlopen", return_value=Response(self.clock, headers={"X-Request-Id": "known-server-id"})), \
             patch.object(transport, "_read_with_deadline", blocked_body):
            with self.assertRaises(deadline.DeadlineExceeded):
                self.execute(allow_fallback=False)

    def test_known_error_identity_is_recorded_before_error_body_can_block(self):
        def blocked_body(response):
            self.assertEqual(self.starts[-1]["request_id"], "known-error-id")
            self.assertEqual(self.starts[-1]["http_status"], 429)
            self.assertEqual(len({item["client_request_id"] for item in self.starts}), 1)
            self.clock.advance(10)
            raise deadline.DeadlineExceeded("synthetic error body deadline")
        with patch.object(transport.urllib.request, "urlopen", side_effect=http_error(429, request_id="known-error-id")), \
             patch.object(transport, "_read_with_deadline", blocked_body):
            with self.assertRaises(deadline.DeadlineExceeded):
                self.execute(allow_fallback=False)

    def test_body_trickle_expires_total_budget_and_closes_response(self):
        resp = Response(self.clock, delay=3, chunks=[b"{", b'"x"', b":", b"1", b"}", b""])
        with patch.object(transport.urllib.request, "urlopen", return_value=resp) as opened:
            with self.assertRaises(deadline.DeadlineExceeded):
                self.execute()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(resp.timeouts, [10, 7, 4, 1])
        self.assertTrue(resp.closed)
        self.assertEqual(self.records[0]["status"], "timeout")
        self.assertEqual(self.records[0]["error_type"], "deadline_exceeded")

    def test_response_arriving_after_deadline_is_rejected(self):
        def late_open(req, timeout):
            self.clock.advance(11)
            return Response(self.clock)
        with patch.object(transport.urllib.request, "urlopen", side_effect=late_open) as opened:
            with self.assertRaises(deadline.DeadlineExceeded):
                self.execute()
        self.assertEqual(opened.call_count, 1)

    def test_hard_error_fallback_uses_remaining_and_actual_model(self):
        timeouts = []
        def opened(req, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                self.clock.advance(6)
                raise http_error(401, "unauthorized", request_id="failed-401")
            return Response(self.clock, headers={"X-Request-Id": "backup-success"})
        with patch.object(transport.urllib.request, "urlopen", side_effect=opened):
            usage = self.execute()[2]
        self.assertEqual(timeouts, [10, 4])
        self.assertEqual([r["model"] for r in self.records], ["primary", "backup"])
        self.assertEqual(self.records[0]["http_status"], 401)
        self.assertEqual(self.records[0]["request_id"], "failed-401")
        self.assertEqual(usage["_astra_trace"]["model"], "backup")

    def test_524_retry_uses_remaining_after_backoff(self):
        timeouts = []
        def opened(req, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                self.clock.advance(3)
                raise http_error(524, request_id="failed-524")
            return Response(self.clock)
        with patch.object(transport.urllib.request, "urlopen", side_effect=opened):
            self.execute()
        self.assertEqual(timeouts, [10, 5])
        self.assertEqual(self.records[0]["request_id"], "failed-524")
        self.assertEqual(self.records[1]["model"], "primary")

    def test_retry_backoff_counts_against_total_and_stops_before_next_attempt(self):
        def failed(req, timeout):
            self.clock.advance(8.5)
            raise http_error(503)
        self.runtime["fallback_model_ids"] = []
        with patch.object(transport.urllib.request, "urlopen", side_effect=failed) as opened:
            with self.assertRaises(deadline.DeadlineExceeded):
                self.execute()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.clock.now, 108.5)

    def test_524_at_deadline_cannot_start_fallback(self):
        def failed(req, timeout):
            self.clock.advance(10)
            raise http_error(524)
        with patch.object(transport.urllib.request, "urlopen", side_effect=failed) as opened:
            with self.assertRaises(deadline.DeadlineExceeded):
                self.execute()
        self.assertEqual(opened.call_count, 1)

    def test_adaptive_400_is_two_attempts_with_shared_budget(self):
        timeouts = []
        def opened(req, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                self.clock.advance(7)
                raise http_error(400, "invalid parameter: temperature", "rejected-400")
            self.assertNotIn("temperature", json.loads(req.data))
            return Response(self.clock, headers={"X-Request-Id": "adaptive-success"})
        with patch.object(transport.urllib.request, "urlopen", side_effect=opened):
            usage = self.execute()[2]
        self.assertEqual(timeouts, [10, 3])
        self.assertEqual([r["status"] for r in self.records], ["failed", "success"])
        self.assertNotEqual(self.records[0]["client_request_id"], self.records[1]["client_request_id"])
        self.assertEqual(usage["_astra_trace"]["request_id"], "adaptive-success")

    def test_adaptive_400_cannot_renew_expired_budget(self):
        def failed(req, timeout):
            self.clock.advance(10)
            raise http_error(400, "invalid parameter")
        with patch.object(transport.urllib.request, "urlopen", side_effect=failed) as opened:
            with self.assertRaises(deadline.DeadlineExceeded):
                self.execute()
        self.assertEqual(opened.call_count, 1)

    def test_cancelled_connection_retries_inside_remaining_budget(self):
        timeouts = []
        def opened(req, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                self.clock.advance(3)
                raise urllib.error.URLError("context canceled")
            return Response(self.clock)
        with patch.object(transport.urllib.request, "urlopen", side_effect=opened):
            self.execute()
        self.assertEqual(timeouts, [10, 5])
        self.assertEqual(self.records[0]["status"], "cancelled")

    def test_timeout_consuming_budget_has_no_retry_or_fallback(self):
        def failed(req, timeout):
            self.clock.advance(timeout)
            raise socket.timeout("timed out")
        with patch.object(transport.urllib.request, "urlopen", side_effect=failed) as opened:
            with self.assertRaises(deadline.DeadlineExceeded):
                self.execute()
        self.assertEqual(opened.call_count, 1)

    def candidate_chain(self):
        runtime = {**self.runtime, "thinking_timeout": 30, "fallback_model_ids": ["short", "final"]}
        return call.execute_llm_request(
            lambda: runtime,
            lambda model: {**runtime, "model": model, "thinking_timeout": 10 if model == "short" else 999},
            self.events.append, [{"role": "user", "content": "private-prompt"}],
        )

    def test_short_candidate_expiry_allows_next_inside_original_parent_budget(self):
        seen = []
        def opened(req, timeout):
            model = json.loads(req.data)["model"]
            seen.append((model, timeout))
            if model == "primary":
                self.clock.advance(3)
                raise http_error(401, "unauthorized")
            if model == "short":
                return Response(self.clock, delay=6)
            return Response(self.clock, headers={"X-Request-Id": "final-success"})
        with patch.object(transport.urllib.request, "urlopen", side_effect=opened):
            usage = self.candidate_chain()[2]
        self.assertEqual(seen, [("primary", 30), ("short", 10), ("final", 15)])
        self.assertEqual(usage["_astra_trace"]["model"], "final")
        self.assertEqual(self.records[1]["status"], "timeout")
        self.assertEqual(self.clock.now, 115)

    def test_parent_expiry_during_short_candidate_does_not_start_next(self):
        seen = []
        def opened(req, timeout):
            model = json.loads(req.data)["model"]
            seen.append(model)
            if model == "primary":
                self.clock.advance(3)
                raise http_error(401, "unauthorized")
            # A blocked transport may return after both the seat and parent
            # boundary; the process hardguard covers uncancellable urllib work.
            self.clock.advance(28)
            return Response(self.clock)
        with patch.object(transport.urllib.request, "urlopen", side_effect=opened):
            with self.assertRaises(deadline.DeadlineExceeded):
                self.candidate_chain()
        self.assertEqual(seen, ["primary", "short"])
        self.assertTrue(self.events[-1]["deadline_hit"])

    def test_nested_seat_deadline_is_not_renewed_by_new_call(self):
        with deadline.deadline_scope(timeout=4):
            self.clock.advance(3)
            with patch.object(transport.urllib.request, "urlopen", return_value=Response(self.clock)) as opened:
                self.execute(timeout=90)
            self.assertEqual(opened.call_args.kwargs["timeout"], 1)
            self.clock.advance(1)
            with self.assertRaises(deadline.DeadlineExceeded):
                self.execute(timeout=90)

    def test_telemetry_failure_does_not_retry_successful_model(self):
        with patch.object(transport.urllib.request, "urlopen", return_value=Response(self.clock)) as opened, patch("astra_gateway.telemetry.record_model_request", side_effect=OSError("store unavailable")), patch.object(transport, "_record_http_attempt", self.real_record):
            self.execute()
        self.assertEqual(opened.call_count, 1)
