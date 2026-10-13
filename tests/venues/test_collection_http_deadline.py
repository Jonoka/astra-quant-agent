"""Offline faults for one shared HTTP collection budget and safe diagnostics.

All HTTP boundaries are mocked. Fake-clock cases prove late results cannot
renew the budget, open fallbacks, or publish a new cache entry. The real
thread cases are released in cleanup and never open a socket.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from contextvars import copy_context
import io
import importlib
from email.message import Message
import json
import os
import threading
import time
from types import CodeType, FunctionType
import unittest
import urllib.error
import urllib.request
from urllib.response import addinfourl
from unittest.mock import MagicMock, patch

import requests

from astra_backend import deadline, http_deadline
from scripts import market_data_service as mds, okx_public, okx_rest
from scripts.factors import okx_quant_factors as qf, smart_money as sm
from scripts.okx_runtime import OKXEnvironment


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def advance(self, seconds):
        self.now += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class Response:
    def __init__(self, payload=None, *, status=200, hook=None, headers=None):
        self.payload = payload if payload is not None else {"code": "0", "data": [1]}
        self.status_code = status
        self.headers = headers or {}
        self.hook = hook

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _value(self):
        if self.hook:
            self.hook()
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload

    def read(self):
        return json.dumps(self._value()).encode()

    def json(self):
        return self._value()


class FakeClockTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = patch.object(deadline.time, "monotonic", lambda: self.clock.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = patch.object(deadline.time, "sleep", self.clock.sleep)
        sleep.start()
        self.addCleanup(sleep.stop)
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        qf._reset_cache_for_tests()
        self.addCleanup(qf._reset_cache_for_tests)
        health = __import__(mds.note_call.__module__, fromlist=["reset"])
        health.reset()
        self.health = health
        self.addCleanup(health.reset)

    def public(self, opener, timeout=10):
        return okx_public.public_json_get(
            "/api/v5/market/ticker?instId=SECRET_QUERY", opener=opener,
            timeout=timeout, user_agent="test")

    def request(self, session, method="GET", timeout=10):
        with patch.object(mds, "get_market_session", return_value=session):
            if method == "POST":
                return mds._public_post("/api/v5/aigc/mcp/indicators",
                                        {"secret_body": "SECRET_BODY"}, timeout=timeout)
            return mds._public_get("/api/v5/market/ticker?instId=SECRET_QUERY",
                                   {"instId": "SECRET_PARAM"}, timeout=timeout)

    def test_public_fallback_recomputes_remaining_timeout(self):
        timeouts = []
        def opener(req, timeout):
            timeouts.append(timeout)
            self.clock.advance(0.3)
            return Response({"code": "50001"} if len(timeouts) == 1 else None)
        with deadline.deadline_scope(timeout=1):
            self.assertEqual(self.public(opener)["data"], [1])
        self.assertEqual(len(timeouts), 2)
        self.assertAlmostEqual(timeouts[0], 1)
        self.assertAlmostEqual(timeouts[1], 0.7)

    def test_public_success_after_expiry_is_rejected_without_fallback(self):
        opener = MagicMock(return_value=Response(hook=lambda: self.clock.advance(1.1)))
        with deadline.deadline_scope(timeout=1), self.assertRaises(deadline.DeadlineExceeded):
            self.public(opener)
        self.assertEqual(opener.call_count, 1)

    def test_public_transport_failure_after_expiry_stops_failover(self):
        def fail(*args, **kwargs):
            self.clock.advance(1.1)
            raise OSError("SECRET_EXCEPTION")
        opener = MagicMock(side_effect=fail)
        with deadline.deadline_scope(timeout=1), self.assertRaises(deadline.DeadlineExceeded):
            self.public(opener)
        self.assertEqual(opener.call_count, 1)
        self.assertNotIn("SECRET_EXCEPTION", self.output.getvalue())

    def test_public_deadline_exception_is_never_swallowed(self):
        opener = MagicMock(side_effect=deadline.DeadlineExceeded("SECRET_EXCEPTION"))
        with self.assertRaises(deadline.DeadlineExceeded):
            self.public(opener)
        self.assertEqual(opener.call_count, 1)

    def test_public_expired_budget_never_enters_opener(self):
        opener = MagicMock()
        with deadline.deadline_scope(timeout=1):
            self.clock.advance(1)
            with self.assertRaises(deadline.DeadlineExceeded):
                self.public(opener)
        opener.assert_not_called()

    def test_requests_get_and_post_receive_combined_remaining_budget(self):
        for method in ("GET", "POST"):
            session = MagicMock()
            getattr(session, method.lower()).return_value = Response()
            with deadline.deadline_scope(timeout=0.25):
                self.assertEqual(self.request(session, method)["data"], [1])
            options = getattr(session, method.lower()).call_args.kwargs
            self.assertLessEqual(options["timeout"].total, 0.25)
            self.assertLessEqual(options["timeout"].connect_timeout, 0.25)
            self.assertFalse(options["allow_redirects"])

    def test_requests_fallback_json_error_uses_reduced_budget(self):
        session = MagicMock()
        session.get.side_effect = [
            Response(ValueError("SECRET_EXCEPTION"), hook=lambda: self.clock.advance(0.3)),
            Response(),
        ]
        with deadline.deadline_scope(timeout=1):
            self.assertEqual(self.request(session)["data"], [1])
        calls = session.get.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(calls[1].kwargs["timeout"].total, 0.7)
        self.assertTrue(calls[1].args[0].startswith("https://www.okx.com"))
        self.assertNotIn("SECRET_EXCEPTION", self.output.getvalue())

    def test_requests_late_success_is_failed_without_host_or_indicator_fallback(self):
        session = MagicMock()
        session.post.return_value = Response(hook=lambda: self.clock.advance(1.1))
        with patch.object(mds, "get_market_session", return_value=session), \
             patch.object(mds, "fetch_single_indicator") as fallback, \
             deadline.deadline_scope(timeout=1), self.assertRaises(deadline.DeadlineExceeded):
            mds.fetch_indicators_batch("SECRET_SYMBOL", ["adx"])
        self.assertEqual(session.post.call_count, 1)
        fallback.assert_not_called()
        snap = self.health.snapshot()
        self.assertEqual(snap["failed_calls"]["okx_public_post_indicators"], 1)
        self.assertNotIn("okx_public_post_indicators", snap["last_success_ms"])

    def test_requests_deadline_exception_skips_retries_and_hosts(self):
        session = MagicMock()
        session.get.side_effect = deadline.DeadlineExceeded("SECRET_EXCEPTION")
        with deadline.deadline_scope(timeout=1), self.assertRaises(deadline.DeadlineExceeded):
            self.request(session)
        self.assertEqual(session.get.call_count, 1)

    def test_indicator_wrappers_reject_response_that_arrives_after_deadline(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                self.clock.now = 100.0
                def late(*args, **kwargs):
                    self.clock.advance(1.1)
                    return {"data": [{"data": [{"timeframes": {"1H": {
                        "indicators": {"ADX": [{"values": {"adx": "20"}}]}
                    }}}]}]}
                with patch.object(mds, "_public_post", side_effect=late), \
                     patch.object(mds, "_local_math_indicators") as fallback, \
                     deadline.deadline_scope(timeout=1), self.assertRaises(deadline.DeadlineExceeded):
                    if batch:
                        mds.fetch_indicators_batch("BTC-USDT-SWAP", ["adx"])
                    else:
                        mds.fetch_single_indicator("BTC-USDT-SWAP", "adx")
                fallback.assert_not_called()

    def test_indicator_postprocessing_expiry_is_not_swallowed_or_returned(self):
        clock = self.clock
        class LateValues(dict):
            def get(self, key, default=None):
                if key == "values":
                    clock.advance(1.1)
                return super().get(key, default)
        data = {"data": [{"data": [{"timeframes": {"1H": {
            "indicators": {"ADX": [LateValues(values={"adx": "20"})]}
        }}}]}]}
        for batch in (False, True):
            with self.subTest(batch=batch):
                self.clock.now = 100.0
                with patch.object(mds, "_public_post", return_value=data), \
                     patch.object(mds, "_local_math_indicators") as fallback, \
                     deadline.deadline_scope(timeout=1), self.assertRaises(deadline.DeadlineExceeded):
                    if batch:
                        mds.fetch_indicators_batch("BTC-USDT-SWAP", ["adx"])
                    else:
                        mds.fetch_single_indicator("BTC-USDT-SWAP", "adx")
                fallback.assert_not_called()

    def test_indicator_fallback_expiry_stops_before_next_indicator(self):
        def late(*args, **kwargs):
            self.clock.advance(1.1)
            return {"adx": "20"}
        with patch.object(mds, "_public_post", return_value=None), \
             patch.object(mds, "fetch_single_indicator", side_effect=late) as single, \
             patch.object(mds, "_local_math_indicators") as local, \
             deadline.deadline_scope(timeout=1), self.assertRaises(deadline.DeadlineExceeded):
            mds.fetch_indicators_batch("BTC-USDT-SWAP", ["adx", "cmf"])
        single.assert_called_once()
        local.assert_not_called()

    def test_requests_500_is_retried_under_original_budget(self):
        session = MagicMock()
        def send(*args, **kwargs):
            self.clock.advance(0.2)
            return Response(status=500 if session.get.call_count == 1 else 200)
        session.get.side_effect = send
        with deadline.deadline_scope(timeout=1):
            self.assertEqual(self.request(session)["data"], [1])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args_list[0].args[0], session.get.call_args_list[1].args[0])
        self.assertAlmostEqual(session.get.call_args_list[1].kwargs["timeout"].total, 0.8)

    def test_requests_429_retry_after_cannot_outlive_budget(self):
        session = MagicMock()
        session.get.return_value = Response(status=429, headers={"Retry-After": "10"})
        with deadline.deadline_scope(timeout=1), self.assertRaises(deadline.DeadlineExceeded):
            self.request(session)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_requests_bounded_connection_retry_and_host_fallback_are_finite(self):
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ConnectionError("SECRET_EXCEPTION")
        with deadline.deadline_scope(timeout=10):
            self.assertIsNone(self.request(session))
        self.assertEqual(session.get.call_count, 6)
        self.assertEqual(self.clock.sleeps, [0, 0.7, 0, 0.7])
        self.assertNotIn("SECRET_EXCEPTION", self.output.getvalue())

    def test_requests_business_message_and_query_never_reach_logs(self):
        session = MagicMock()
        session.get.return_value = Response({"code": "51001", "msg": "SECRET_RESPONSE"})
        with deadline.deadline_scope(timeout=1), patch.dict(os.environ, {"ASTRA_JOB_RUN_ID": "123"}):
            self.assertIsNone(self.request(session))
        output = self.output.getvalue()
        for forbidden in ("SECRET_QUERY", "SECRET_PARAM", "SECRET_RESPONSE"):
            self.assertNotIn(forbidden, output)
        self.assertIn('"job_run_id": 123', output)
        self.assertIn('"endpoint": "/api/v5/market/ticker"', output)
        self.assertIn('"remaining_ms"', output)
        self.assertIn('"elapsed_ms"', output)
        self.assertIn("51001", self.health.stats()["last_error"]["okx_public_get_ticker"])

    def test_unknown_public_path_cannot_leak_into_health_or_endpoint_labels(self):
        session = MagicMock()
        session.get.return_value = Response(status=400)
        with patch.object(mds, "get_market_session", return_value=session):
            self.assertIsNone(mds._public_get("/api/v5/market/SECRET_PATH?key=SECRET_QUERY"))
        self.assertNotIn("SECRET_", self.output.getvalue())
        self.assertNotIn("SECRET_", str(self.health.snapshot()))
        self.assertIn('"endpoint": "other"', self.output.getvalue())
        self.assertIn("okx_public_get_unknown", self.health.stats()["by_kind"])

    def test_unknown_business_code_is_not_reflected_in_health(self):
        session = MagicMock()
        session.get.return_value = Response({"code": "SECRET_CODE", "msg": "SECRET_RESPONSE"})
        with deadline.deadline_scope(timeout=1):
            self.assertIsNone(self.request(session))
        self.assertNotIn("SECRET_", self.output.getvalue())
        self.assertNotIn("SECRET_", str(self.health.stats()))

    def test_private_timeout_is_capped_without_logging_signed_inputs(self):
        env = OKXEnvironment("demo", "SECRET_KEY", "SECRET_SIGNING", "SECRET_PASSPHRASE")
        opener = MagicMock(return_value=Response())
        with patch.object(okx_rest, "urlopen", opener), deadline.deadline_scope(timeout=0.25):
            self.assertEqual(okx_rest.request("GET", "/api/v5/account/balance",
                                             {"ccy": "SECRET_QUERY"}, env=env), [])
        self.assertLessEqual(opener.call_args.kwargs["timeout"], 0.25)
        self.assertNotIn("SECRET_", self.output.getvalue())
        self.assertNotIn("OK-ACCESS", self.output.getvalue())

    def test_private_deadline_is_not_wrapped_in_runtime_error(self):
        env = OKXEnvironment("demo", "key", "secret", "pass")
        with patch.object(okx_rest, "urlopen", side_effect=deadline.DeadlineExceeded("secret")), \
             self.assertRaises(deadline.DeadlineExceeded):
            okx_rest.request("GET", "/api/v5/account/balance", env=env)

    def test_private_late_success_is_rejected(self):
        env = OKXEnvironment("demo", "key", "secret", "pass")
        opener = MagicMock(return_value=Response(hook=lambda: self.clock.advance(1.1)))
        with patch.object(okx_rest, "urlopen", opener), deadline.deadline_scope(timeout=1), \
             self.assertRaises(deadline.DeadlineExceeded):
            okx_rest.request("GET", "/api/v5/account/balance", env=env)
        self.assertEqual(opener.call_count, 1)

    def test_private_network_wrapper_omits_original_exception_message(self):
        env = OKXEnvironment("demo", "key", "secret", "pass")
        with patch.object(okx_rest, "urlopen", side_effect=OSError("SECRET_EXCEPTION")), \
             self.assertRaises(RuntimeError) as error:
            okx_rest.request("GET", "/api/v5/account/balance", env=env)
        self.assertNotIn("SECRET_EXCEPTION", str(error.exception))
        self.assertNotIn("SECRET_EXCEPTION", self.output.getvalue())
        self.assertTrue(error.exception.__suppress_context__)

    def test_quant_late_result_cannot_write_cache_or_rate_ledger(self):
        opener = MagicMock(return_value=Response(hook=lambda: self.clock.advance(1.1)))
        with patch.object(qf.urllib.request, "urlopen", opener), deadline.deadline_scope(timeout=1), \
             self.assertRaises(deadline.DeadlineExceeded):
            qf._public_get("/api/v5/rubik/stat/taker-volume", cache_key="test", ttl=30)
        self.assertEqual(opener.call_count, 1)
        self.assertNotIn("test", qf._CACHE)
        self.assertEqual(qf._RUBIK_LAST_AT[0], 0)

    def test_quant_retry_backoff_does_not_open_another_call_after_budget(self):
        opener = MagicMock(side_effect=urllib.error.HTTPError("secret", 429, "SECRET_MSG", {}, None))
        with patch.object(qf.urllib.request, "urlopen", opener), deadline.deadline_scope(timeout=0.2), \
             self.assertRaises(deadline.DeadlineExceeded):
            qf._public_get("/api/v5/rubik/stat/taker-volume")
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertNotIn("SECRET_MSG", self.output.getvalue())

    def test_quant_throttle_refuses_wait_larger_than_budget(self):
        qf._RUBIK_LAST_AT[0] = 100
        with patch.object(qf.time, "time", return_value=100), \
             patch.object(qf, "_RUBIK_MIN_INTERVAL", 0.42), \
             patch.object(qf.urllib.request, "urlopen") as opener, \
             deadline.deadline_scope(timeout=0.2), self.assertRaises(deadline.DeadlineExceeded):
            qf._public_get("/api/v5/rubik/stat/taker-volume")
        opener.assert_not_called()
        self.assertEqual(self.clock.sleeps, [])

    def test_quant_cached_result_cannot_be_returned_after_expiry(self):
        qf._cache_put("test", [1])
        with deadline.deadline_scope(timeout=1):
            self.clock.advance(1)
            with self.assertRaises(deadline.DeadlineExceeded):
                qf._cache_get("test", 30)

    def test_quant_cancellation_prevents_cache_and_rate_side_effects(self):
        with deadline.cancellation_scope() as token:
            token.set()
            with self.assertRaises(deadline.DeadlineExceeded):
                qf._cache_put("test", [1])
            with self.assertRaises(deadline.DeadlineExceeded):
                qf._mark_rubik_called("/api/v5/rubik/stat/taker-volume")
        self.assertNotIn("test", qf._CACHE)
        self.assertEqual(qf._RUBIK_LAST_AT[0], 0)

    def test_quant_missing_snapshots_are_not_cached_as_fresh(self):
        with patch.object(qf, "_public_get", return_value=None):
            self.assertEqual(qf.fetch_option_snapshot("BTC-USD"), {})
            self.assertEqual(qf.fetch_quarterly_basis_inputs("BTC"), {})
            self.assertEqual(qf.fetch_loan_rate_snapshot(), {})
        self.assertEqual(qf._CACHE, {})

    def test_smart_money_deadline_is_not_partial_or_a_new_fallback(self):
        with patch.object(sm, "public_json_get", side_effect=deadline.DeadlineExceeded("secret")) as call, \
             self.assertRaises(deadline.DeadlineExceeded):
            sm.fetch_smart_money_for_symbol("BTC")
        self.assertEqual(call.call_count, 1)

    def test_smart_money_late_taker_aborts_instead_of_returning_partial_data(self):
        def response(path, **kwargs):
            if "taker-volume" in path:
                self.clock.advance(1.1)
            return {"code": "0", "data": [["100", "1", "2"]]}
        with patch.object(sm, "public_json_get", side_effect=response) as call, \
             deadline.deadline_scope(timeout=1), self.assertRaises(deadline.DeadlineExceeded):
            sm.fetch_smart_money_for_symbol("BTC")
        self.assertEqual(call.call_count, 2)


class SessionPolicyTests(unittest.TestCase):
    def test_only_deadline_session_disables_implicit_retry(self):
        with patch.object(mds, "_SESSION", None), patch.object(mds, "_BOUNDED_SESSION", None):
            normal = mds.get_market_session()
            with deadline.deadline_scope(timeout=1):
                bounded = mds.get_market_session()
            try:
                self.assertIsNot(normal, bounded)
                self.assertEqual(normal.adapters["https://"].max_retries.total, 2)
                self.assertEqual(bounded.adapters["https://"].max_retries.total, 0)
                self.assertIs(mds.get_market_session(), normal)
            finally:
                normal.close()
                bounded.close()


class WorkerBudgetTests(unittest.TestCase):
    def test_smart_money_threads_inherit_deadline_and_caller_context(self):
        seen = []
        def fake(ccy, price=0, timeout=3.5):
            seen.append((ccy, deadline.current_deadline(), deadline.cycle_metadata()["caller"]))
            return {"value": 1}
        with deadline.deadline_scope(timeout=1) as expected, deadline.request_scope("test_caller"), \
             patch.object(sm, "fetch_smart_money_for_symbol", fake):
            output = sm.fetch_smart_money_pool([{"ccy": "BTC"}, {"ccy": "ETH"}])
        self.assertEqual(set(output), {"BTC", "ETH"})
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(item[1:] == (expected, "test_caller") for item in seen))

    def test_smart_money_unbounded_threads_still_inherit_caller_context(self):
        seen = []
        def fake(ccy, price=0, timeout=3.5):
            seen.append(deadline.cycle_metadata()["caller"])
            return {"value": 1}
        with deadline.request_scope("test_caller"), patch.object(sm, "fetch_smart_money_for_symbol", fake):
            output = sm.fetch_smart_money_pool([{"ccy": "BTC"}])
        self.assertEqual(set(output), {"BTC"})
        self.assertEqual(seen, ["test_caller"])

    def test_smart_money_timeout_returns_before_blocked_worker_and_rejects_late_side_effect(self):
        started = threading.Event()
        release = threading.Event()
        done = threading.Event()
        effects = []
        def slow(ccy, price=0, timeout=3.5):
            started.set()
            try:
                release.wait(2)
                deadline.check_deadline()
                effects.append("late")
                return {"value": 1}
            finally:
                done.set()
        start = time.monotonic()
        try:
            with patch.object(sm, "fetch_smart_money_for_symbol", slow), \
                 deadline.deadline_scope(timeout=0.05), self.assertRaises(deadline.DeadlineExceeded):
                sm.fetch_smart_money_pool([{"ccy": "BTC"}])
            self.assertTrue(started.is_set())
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertFalse(done.is_set())
        finally:
            release.set()
            self.assertTrue(done.wait(1))
        self.assertEqual(effects, [])

    def test_smart_money_early_failure_cancels_sibling_before_absolute_deadline(self):
        sibling_started = threading.Event()
        release = threading.Event()
        done = threading.Event()
        effects = []
        def fake(ccy, price=0, timeout=3.5):
            if ccy == "BTC":
                self.assertTrue(sibling_started.wait(1))
                raise deadline.DeadlineExceeded("early child failure")
            sibling_started.set()
            try:
                release.wait(2)
                deadline.check_deadline()
                effects.append("late")
                return {"value": 1}
            finally:
                done.set()
        try:
            with patch.object(sm, "fetch_smart_money_for_symbol", fake), \
                 deadline.deadline_scope(timeout=10), self.assertRaises(deadline.DeadlineExceeded):
                sm.fetch_smart_money_pool([{"ccy": "BTC"}, {"ccy": "ETH"}])
            self.assertFalse(done.is_set())
        finally:
            release.set()
            self.assertTrue(done.wait(1))
        self.assertEqual(effects, [])

    def test_copied_cancelled_worker_cannot_start_an_http_request(self):
        opener = MagicMock()
        with deadline.deadline_scope(timeout=10), deadline.cancellation_scope():
            context = copy_context()
        with self.assertRaises(deadline.DeadlineExceeded):
            context.run(okx_public.public_json_get, "/api/v5/market/ticker",
                        opener=opener, timeout=3, user_agent="test")
        opener.assert_not_called()

    def test_rubik_lock_wait_is_bounded_by_remaining_budget(self):
        qf._RUBIK_LOCK.acquire()
        start = time.monotonic()
        try:
            with deadline.deadline_scope(timeout=0.025), self.assertRaises(deadline.DeadlineExceeded):
                qf._throttle_rubik("/api/v5/rubik/stat/taker-volume")
        finally:
            qf._RUBIK_LOCK.release()
        self.assertLess(time.monotonic() - start, 0.5)


class UrllibRedirectTests(unittest.TestCase):
    """Real urllib handler dispatch over an in-memory fake HTTPS transport."""

    def setUp(self):
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        qf._reset_cache_for_tests()
        self.addCleanup(qf._reset_cache_for_tests)

    def fake_transport(self, *, code=302, hook=None):
        calls = []
        class FakeHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, request):
                calls.append(request.full_url)
                if hook:
                    hook()
                headers = Message()
                headers["Location"] = "https://redirect.invalid/SECRET_LOCATION"
                response = addinfourl(io.BytesIO(b""), headers, request.full_url, code=code)
                response.msg = "Redirect"
                return response
        def build(*handlers):
            # Install only an in-memory HTTPS transport and error dispatch.
            # No default HTTP, proxy, FTP or file handler can escape this seam.
            director = urllib.request.OpenerDirector()
            for handler in (FakeHTTPSHandler(), urllib.request.HTTPDefaultErrorHandler(),
                            urllib.request.HTTPErrorProcessor(), *handlers):
                director.add_handler(handler)
            return director
        @contextmanager
        def fake_scope():
            # Offline runners deliberately wrap urlopen before app imports.
            # Select private-opener dispatch only inside this entirely fake
            # transport; production selector behavior is checked separately.
            with patch.object(http_deadline, "build_opener", side_effect=build), \
                 patch.object(http_deadline, "_is_stdlib_urlopen", return_value=True):
                yield
        return calls, fake_scope()

    def test_selector_accepts_real_stdlib_code_but_rejects_injected_openers(self):
        # Inspect the installed stdlib code without running it or exposing an
        # unguarded transport. Missing globals also make this probe uncallable.
        module_code = urllib.request.__loader__.get_code("urllib.request")
        code = next(item for item in module_code.co_consts
                    if isinstance(item, CodeType) and item.co_name == "urlopen")
        stdlib_probe = FunctionType(code, {"__name__": "urllib.request"})
        self.assertTrue(http_deadline._is_stdlib_urlopen(stdlib_probe))
        self.assertFalse(http_deadline._is_stdlib_urlopen(MagicMock()))
        self.assertFalse(http_deadline._is_stdlib_urlopen(lambda *args, **kw: None))

    def read(self, route):
        if route == "public":
            return okx_public.public_json_get(
                "/api/v5/market/ticker?instId=SECRET_QUERY",
                opener=urllib.request.urlopen, timeout=3, user_agent="test")
        if route == "quant":
            return qf._public_get("/api/v5/market/ticker", {"instId": "SECRET_QUERY"})
        env = OKXEnvironment("demo", "SECRET_KEY", "SECRET_SIGNING", "SECRET_PASSPHRASE")
        return okx_rest.request("GET", "/api/v5/account/balance", env=env)

    def test_every_stdlib_redirect_status_is_rejected_before_location_request(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                calls, fake = self.fake_transport(code=code)
                with fake, deadline.deadline_scope(timeout=1), self.assertRaises(urllib.error.HTTPError) as error:
                    http_deadline.open_with_deadline(
                        urllib.request.Request("https://www.okx.com/api/v5/market/ticker"),
                        opener=urllib.request.urlopen, timeout=3)
                error.exception.close()
                self.assertEqual(len(calls), 1)
                self.assertNotIn("redirect.invalid", calls[0])

    def test_all_three_routes_block_redirects_and_preserve_global_opener(self):
        original_global = urllib.request._opener
        for route in ("public", "quant", "private"):
            with self.subTest(route=route):
                calls, fake = self.fake_transport()
                with fake, deadline.deadline_scope(timeout=1):
                    if route == "quant":
                        self.assertIsNone(self.read(route))
                    else:
                        expected = urllib.error.HTTPError if route == "public" else RuntimeError
                        with self.assertRaises(expected):
                            self.read(route)
                self.assertEqual(len(calls), 1 if route == "private" else 2)
                self.assertTrue(all("redirect.invalid" not in url for url in calls))
                self.assertIs(urllib.request._opener, original_global)
        self.assertNotIn("SECRET_", self.output.getvalue())

    def test_cancellation_at_redirect_stops_all_three_routes_before_fallback(self):
        for route in ("public", "quant", "private"):
            with self.subTest(route=route), deadline.deadline_scope(timeout=10), \
                 deadline.cancellation_scope() as token:
                calls, fake = self.fake_transport(hook=token.set)
                with fake, self.assertRaises(deadline.DeadlineExceeded):
                    self.read(route)
                self.assertEqual(len(calls), 1)
                self.assertEqual(qf._CACHE, {})

    def test_expiry_at_redirect_stops_all_three_routes_before_fallback(self):
        for route in ("public", "quant", "private"):
            with self.subTest(route=route):
                clock = FakeClock()
                with patch.object(deadline.time, "monotonic", lambda: clock.now), \
                     deadline.deadline_scope(timeout=1):
                    calls, fake = self.fake_transport(hook=lambda: clock.advance(1.1))
                    with fake, self.assertRaises(deadline.DeadlineExceeded):
                        self.read(route)
                    self.assertEqual(len(calls), 1)

    def test_budgeted_injected_openers_keep_their_existing_patch_seam(self):
        opener = MagicMock(return_value=Response())
        with patch.object(http_deadline, "build_opener", side_effect=AssertionError("Mock must stay in control")) as build, \
             deadline.deadline_scope(timeout=1):
            self.assertIs(http_deadline.open_with_deadline(
                urllib.request.Request("https://www.okx.com/api/v5/market/ticker"),
                opener=opener, timeout=3), opener.return_value)
        opener.assert_called_once()
        build.assert_not_called()

    def test_import_while_stdlib_is_mocked_cannot_bypass_mock_transport(self):
        try:
            with patch.object(urllib.request, "urlopen", return_value=Response()) as opener:
                importlib.reload(http_deadline)
                with patch.object(http_deadline, "build_opener", side_effect=AssertionError("Mock must stay in control")) as build, \
                     deadline.deadline_scope(timeout=1):
                    http_deadline.open_with_deadline(
                        urllib.request.Request("https://www.okx.com/api/v5/market/ticker"),
                        opener=opener, timeout=3)
                opener.assert_called_once()
                build.assert_not_called()
        finally:
            importlib.reload(http_deadline)

    def test_no_budget_keeps_injected_transport_without_new_opener(self):
        opener = MagicMock(return_value=Response())
        with patch.object(http_deadline, "build_opener") as build:
            http_deadline.open_with_deadline(
                urllib.request.Request("https://www.okx.com/api/v5/market/ticker"),
                opener=opener, timeout=3)
        build.assert_not_called()
        self.assertEqual(opener.call_args.kwargs["timeout"], 3)

    def test_cancelled_late_response_is_closed_before_caller_receives_it(self):
        with deadline.deadline_scope(timeout=10), deadline.cancellation_scope() as token:
            response = MagicMock()
            def late(*args, **kwargs):
                token.set()
                return response
            with self.assertRaises(deadline.DeadlineExceeded):
                http_deadline.open_with_deadline(
                    urllib.request.Request("https://www.okx.com/api/v5/market/ticker"),
                    opener=late, timeout=3)
        response.close.assert_called_once()


class CommitBoundaryTests(unittest.TestCase):
    def setUp(self):
        qf._reset_cache_for_tests()
        self.addCleanup(qf._reset_cache_for_tests)

    def assert_cancelled_commit(self, operation):
        at_boundary = threading.Event()
        release = threading.Event()
        done = threading.Event()
        errors = []
        real_commit = deadline.commit_scope
        @contextmanager
        def pause_before_gate():
            at_boundary.set()
            if not release.wait(2):
                raise AssertionError("Commit test was not released")
            with real_commit():
                yield
        def worker():
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        thread = None
        try:
            with patch.object(qf, "commit_scope", pause_before_gate), \
                 deadline.deadline_scope(timeout=10):
                with deadline.cancellation_scope():
                    thread = threading.Thread(target=copy_context().run, args=(worker,), daemon=True)
                    thread.start()
                    self.assertTrue(at_boundary.wait(1))
                # Cancellation is now committed while the worker is paused
                # after its data lock/check and before acquiring the commit gate.
                release.set()
                self.assertTrue(done.wait(1))
        finally:
            release.set()
            if thread:
                thread.join(1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], deadline.DeadlineExceeded)

    def test_owner_exit_before_cache_commit_cannot_publish_a_new_value(self):
        qf._CACHE["test"] = (100.0, ["old"])
        self.assert_cancelled_commit(lambda: qf._cache_put("test", ["late"]))
        self.assertEqual(qf._CACHE["test"], (100.0, ["old"]))

    def test_owner_exit_before_quota_commit_cannot_update_rate_ledger(self):
        qf._RUBIK_LAST_AT[0] = 100.0
        self.assert_cancelled_commit(lambda: qf._mark_rubik_called("/api/v5/rubik/stat/taker-volume"))
        self.assertEqual(qf._RUBIK_LAST_AT[0], 100.0)


if __name__ == "__main__":
    unittest.main()
