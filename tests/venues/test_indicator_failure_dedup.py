"""Offline transport-level regression gates for per-call indicator deduplication.

The real request, retry, host-failover and local-math code runs against mocked
Session.post/get boundaries. No exchange, credential, config, or server is used.
Suppressing only the new outcome metadata exercises the old fallback path for
fixed-fixture count/output comparisons; it does not replace the transport.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stdout
import io
import threading
import unittest
from unittest.mock import MagicMock, patch

import requests

from astra_backend import deadline
from scripts import market_data_service as mds


INDICATORS = ["adx", "kdj", "bbwidth", "cmf"]
REMOTE = {"ADX": {"adx": "21.3"}, "KDJ": {"k": "31", "d": "32", "j": "29"},
          "BBWIDTH": {"bbWidth": "2.3"}, "CMF": {"cmf": "0.1250"}}
LOCAL = {"ADX": {"adx": "100.00"}, "KDJ": {"k": "68.75", "d": "68.75", "j": "68.75"},
         "BBWIDTH": {"bbWidth": "2.07"}, "CMF": {"cmf": "0.2500"}}


def candles():
    """Fixed 120-row, latest-first OHLCV fixture, including nonflat volume."""
    return [[str(1700000000000 + i * 3600000), f"{100 + i / 10:.1f}",
             f"{102 + i / 10:.1f}", f"{98 + i / 10:.1f}",
             f"{100.5 + i / 10:.1f}", str(10 + i), "1", "1", "1"]
            for i in reversed(range(120))]


def indicator_payload(values=REMOTE):
    return {"code": "0", "data": [{"data": [{"timeframes": {"1H": {
        "indicators": {key: [{"values": value}] for key, value in values.items()}
    }}}]}]}


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.payload = payload if payload is not None else {"code": "0", "data": []}
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def advance(self, seconds):
        self.now += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class IndicatorFailureDedupTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for patcher in (
            patch.object(deadline.time, "monotonic", lambda: self.clock.now),
            patch.object(deadline.time, "sleep", self.clock.sleep),
            patch.object(mds, "note_call"), patch.object(mds, "note_failure"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def session(self, response, *, singles_succeed=False, rows=None):
        session = MagicMock()

        def post(url, *, json, **kwargs):
            if singles_succeed and len(json["indicators"]) == 1:
                return Response(payload=indicator_payload())
            if isinstance(response, BaseException):
                raise response
            return response

        session.post.side_effect = post
        session.get.return_value = Response(payload={"code": "0", "data":
                                                     candles() if rows is None else rows})
        return session

    def run_batch(self, session, *, legacy=False, bounded=True, indicators=None):
        original_post = mds._public_post

        def no_metadata(path, payload, timeout=4.0, **kwargs):
            # Exactly the prior call contract: transport has no outcome sink.
            return original_post(path, payload, timeout=timeout)

        with patch.object(mds, "get_market_session", return_value=session), \
             (patch.object(mds, "_public_post", side_effect=no_metadata)
              if legacy else nullcontext()), \
             (deadline.deadline_scope(timeout=55) if bounded else nullcontext()):
            return mds.fetch_indicators_batch("SYN-USDT-SWAP",
                                             INDICATORS if indicators is None else indicators,
                                             bar="1h")

    def assert_counts(self, session, posts, gets):
        self.assertEqual(session.post.call_count, posts)
        self.assertEqual(session.get.call_count, gets)

    def compare_endpoint_failure(self, response, posts_before, posts_after):
        before = self.session(response)
        after = self.session(response)
        self.assertEqual(self.run_batch(before, legacy=True), LOCAL)
        self.assertEqual(self.run_batch(after), LOCAL)
        self.assert_counts(before, posts_before, 4)
        self.assert_counts(after, posts_after, 1)
        self.assertEqual({call.args[0] for call in after.post.call_args_list},
                         {host + "/api/v5/aigc/mcp/indicators" for host in mds.OKX_PUBLIC_HOSTS})
        self.assertEqual(after.get.call_args.kwargs["params"],
                         {"instId": "SYN-USDT-SWAP", "bar": "1H", "limit": 120})

    def test_403_posts_ten_to_two_and_candles_four_to_one_exact_output(self):
        self.compare_endpoint_failure(Response(403), 10, 2)

    def test_503_posts_thirty_to_six_and_candles_four_to_one_exact_output(self):
        self.compare_endpoint_failure(Response(503), 30, 6)

    def test_transport_connection_and_timeout_failures_are_exhaustive(self):
        for failure in (requests.exceptions.ConnectionError(), requests.exceptions.Timeout()):
            with self.subTest(failure=type(failure).__name__):
                self.compare_endpoint_failure(failure, 30, 6)

    def test_other_allowlisted_http_failures_preserve_retry_policy(self):
        for status in (429, 500, 502, 504):
            with self.subTest(status=status):
                session = self.session(Response(status))
                self.assertEqual(self.run_batch(session), LOCAL)
                self.assert_counts(session, 6, 1)

    def test_unbounded_request_path_keeps_its_existing_session_policy(self):
        session = self.session(Response(503))
        self.assertEqual(self.run_batch(session, bounded=False), LOCAL)
        self.assert_counts(session, 2, 1)
        # Adapter-managed retries are unchanged; the wrapper adds none here.
        self.assertEqual(self.clock.sleeps, [])

    def test_normal_complete_batch_and_existing_partial_return_are_unchanged(self):
        for values in (REMOTE, {"ADX": REMOTE["ADX"]}):
            for legacy in (False, True):
                with self.subTest(values=values, legacy=legacy):
                    session = self.session(Response(payload=indicator_payload(values)))
                    self.assertEqual(self.run_batch(session, legacy=legacy), values)
                    self.assert_counts(session, 1, 0)

    def test_code_error_empty_incompatible_and_bad_json_still_try_singles(self):
        cases = [
            (Response(payload={"code": "51000", "data": []}), 6),
            (Response(payload={"code": "0", "data": []}), 5),
            (Response(payload={"code": "0", "data": [{"data": []}]}), 5),
            (Response(payload={"code": "0", "data": [{"data": [{"timeframes": "bad"}]}]}), 5),
            (Response(payload=ValueError("invalid JSON")), 6),
            (Response(payload=["unexpected JSON type"]), 6),
        ]
        for response, posts in cases:
            with self.subTest(payload=response.payload):
                session = self.session(response, singles_succeed=True)
                self.assertEqual(self.run_batch(session), REMOTE)
                self.assert_counts(session, posts, 0)

    def test_request_content_and_unknown_http_statuses_still_try_singles(self):
        for status in (400, 401, 404, 409, 413, 415, 422, 451, 501):
            with self.subTest(status=status):
                session = self.session(Response(status), singles_succeed=True)
                self.assertEqual(self.run_batch(session), REMOTE)
                self.assert_counts(session, 6, 0)

    def test_mixed_hosts_with_content_or_application_failure_are_not_deduped(self):
        for secondary in (Response(400), Response(422),
                          Response(payload={"code": "51000"}),
                          Response(payload=ValueError("invalid JSON"))):
            session = self.session(Response(403), singles_succeed=True)
            original = session.post.side_effect

            def post(url, *, json, **kwargs):
                if len(json["indicators"]) > 1 and url.startswith(mds.OKX_PUBLIC_HOSTS[1]):
                    return secondary
                return original(url, json=json, **kwargs)

            session.post.side_effect = post
            with self.subTest(status=secondary.status_code, payload=secondary.payload):
                self.assertEqual(self.run_batch(session), REMOTE)
                self.assert_counts(session, 6, 0)

    def test_mixed_retry_outcomes_require_every_attempt_to_qualify(self):
        session = self.session(Response(403), singles_succeed=True)
        sequence = iter([Response(503), Response(payload=ValueError("invalid JSON")), Response(403)])
        original = session.post.side_effect

        def post(url, *, json, **kwargs):
            return next(sequence) if len(json["indicators"]) > 1 else original(url, json=json, **kwargs)

        session.post.side_effect = post
        self.assertEqual(self.run_batch(session), REMOTE)
        self.assert_counts(session, 7, 0)

    def test_unclassified_exceptions_do_not_prove_endpoint_exhaustion(self):
        session = self.session(RuntimeError("unclassified failure"), singles_succeed=True)
        self.assertEqual(self.run_batch(session), REMOTE)
        self.assert_counts(session, 6, 0)

    def test_connection_exception_while_parsing_a_response_is_not_transport_evidence(self):
        session = self.session(Response(payload=requests.exceptions.ConnectionError()),
                               singles_succeed=True)
        self.assertEqual(self.run_batch(session), REMOTE)
        self.assert_counts(session, 10, 0)

    def test_host_recovery_within_the_batch_uses_remote_result(self):
        session = self.session(Response(403))
        session.post.side_effect = [Response(403), Response(payload=indicator_payload())]
        self.assertEqual(self.run_batch(session), REMOTE)
        self.assert_counts(session, 2, 0)

    def test_next_logical_call_probes_again_without_negative_cache(self):
        session = self.session(Response(403))
        self.assertEqual(self.run_batch(session), LOCAL)
        session.post.side_effect = None
        session.post.return_value = Response(payload=indicator_payload())
        self.assertEqual(self.run_batch(session), REMOTE)
        self.assert_counts(session, 3, 1)

    def test_recovery_after_exhausted_batch_waits_until_next_logical_call(self):
        # Intentional tradeoff: a server recovering just after both batch host
        # failures could have answered a later single request. This invocation
        # uses one candle snapshot instead; output equivalence only holds when
        # the endpoint stays failed and the candle fixture is unchanged.
        before = self.session(Response(403), singles_succeed=True)
        after = self.session(Response(403), singles_succeed=True)
        self.assertEqual(self.run_batch(before, legacy=True), REMOTE)
        self.assert_counts(before, 6, 0)
        self.assertEqual(self.run_batch(after), LOCAL)
        self.assert_counts(after, 2, 1)

    def test_none_without_explicit_metadata_preserves_single_fallback_seam(self):
        with patch.object(mds, "_public_post", return_value=None), \
             patch.object(mds, "fetch_single_indicator", side_effect=lambda _, ind, **kw:
                          REMOTE[ind.upper()]) as single, \
             patch.object(mds, "_local_math_indicators") as local:
            self.assertEqual(mds.fetch_indicators_batch("SYN-USDT-SWAP", INDICATORS), REMOTE)
        self.assertEqual(single.call_count, 4)
        local.assert_not_called()

    def test_missing_candles_stay_empty_with_only_one_fetch(self):
        for status, posts in ((403, 2), (503, 6)):
            with self.subTest(status=status):
                session = self.session(Response(status), rows=[])
                self.assertEqual(self.run_batch(session), {})
                self.assert_counts(session, posts, 1)

    def test_no_requested_indicators_does_not_fetch_candles(self):
        session = self.session(Response(403))
        self.assertEqual(self.run_batch(session, indicators=[]), {})
        self.assert_counts(session, 2, 0)

    def test_direct_post_default_return_contract_and_outcome_reset(self):
        session = self.session(Response(403))
        outcome = mds._PublicRequestOutcome()
        path = "/api/v5/aigc/mcp/indicators"
        with patch.object(mds, "get_market_session", return_value=session):
            self.assertIsNone(mds._public_post(path, {"indicators": {}}, 4.0))
            self.assertIsNone(mds._public_post(path, {"indicators": {}}, _outcome=outcome))
            self.assertTrue(outcome.endpoint_exhausted)
            session.post.side_effect = None
            session.post.return_value = Response(payload=indicator_payload())
            self.assertEqual(mds._public_post(path, {}, _outcome=outcome), indicator_payload())
            self.assertFalse(outcome.endpoint_exhausted)

    def test_expired_parent_starts_no_transport_or_local_fallback(self):
        session = self.session(Response(403))
        with deadline.deadline_scope(timeout=55):
            self.clock.advance(55)
            with self.assertRaises(deadline.DeadlineExceeded):
                self.run_batch(session)
        self.assert_counts(session, 0, 0)

    def test_expiry_during_batch_does_not_start_candle_fallback(self):
        session = self.session(Response(403))

        def late(*args, **kwargs):
            self.clock.advance(28)
            return Response(403)

        session.post.side_effect = late
        with self.assertRaises(deadline.DeadlineExceeded):
            self.run_batch(session)
        self.assert_counts(session, 2, 0)

    def test_one_candle_fallback_uses_original_55_second_deadline(self):
        for candle_duration, expires in ((2, False), (4, True)):
            with self.subTest(candle_duration=candle_duration):
                self.clock.now = 100.0
                session = self.session(Response(403))
                seen_deadlines = []

                def post(*args, **kwargs):
                    seen_deadlines.append(deadline.current_deadline())
                    self.clock.advance(26)
                    return Response(403)

                def get(*args, **kwargs):
                    seen_deadlines.append(deadline.current_deadline())
                    self.assertEqual(kwargs["timeout"].total, 3)
                    self.clock.advance(candle_duration)
                    return Response(payload={"code": "0", "data": candles()})

                session.post.side_effect = post
                session.get.side_effect = get
                if expires:
                    with self.assertRaises(deadline.DeadlineExceeded):
                        self.run_batch(session)
                else:
                    self.assertEqual(self.run_batch(session), LOCAL)
                self.assertEqual(seen_deadlines, [155, 155, 155])
                self.assert_counts(session, 2, 1)

    def test_retry_after_cannot_renew_budget_or_start_local_fallback(self):
        session = self.session(Response(503, headers={"Retry-After": "60"}))
        with self.assertRaises(deadline.DeadlineExceeded):
            self.run_batch(session)
        self.assert_counts(session, 1, 0)
        self.assertEqual(self.clock.sleeps, [])

    def test_parent_collection_budget_is_not_extended_by_indicator_fallback(self):
        session = self.session(Response(403))
        with deadline.deadline_scope(timeout=180):
            self.clock.advance(170)
            with patch.object(mds, "_local_math_indicators", wraps=mds._local_math_indicators) as local:
                self.assertEqual(self.run_batch(session), LOCAL)
            local.assert_called_once_with("SYN-USDT-SWAP", INDICATORS, "1H")
            self.assertEqual(deadline.current_deadline(), 280)
            self.assertEqual(deadline.remaining(), 10)
        self.assert_counts(session, 2, 1)

    def test_concurrent_failure_and_compatibility_calls_keep_outcomes_independent(self):
        session = self.session(Response(403))
        local_started = threading.Event()
        compatible_finished = threading.Event()

        def post(url, *, json, **kwargs):
            if json["instId"] == "FAILED-USDT-SWAP":
                return Response(403)
            if len(json["indicators"]) > 1:
                self.assertTrue(local_started.wait(2), "failed call never reached local math")
                return Response(payload={"code": "51000"})
            return Response(payload=indicator_payload())

        def get(*args, **kwargs):
            local_started.set()
            self.assertTrue(compatible_finished.wait(2), "compatibility call did not finish")
            return Response(payload={"code": "0", "data": candles()})

        def run(symbol):
            try:
                with deadline.deadline_scope(timeout=55):
                    return mds.fetch_indicators_batch(symbol, INDICATORS)
            finally:
                if symbol == "COMPAT-USDT-SWAP":
                    compatible_finished.set()

        session.post.side_effect = post
        session.get.side_effect = get
        with patch.object(mds, "get_market_session", return_value=session), \
             ThreadPoolExecutor(max_workers=2) as pool:
            failed = pool.submit(run, "FAILED-USDT-SWAP")
            compatible = pool.submit(run, "COMPAT-USDT-SWAP")
            self.assertEqual(failed.result(timeout=5), LOCAL)
            self.assertEqual(compatible.result(timeout=5), REMOTE)
        self.assert_counts(session, 8, 1)
        self.assertEqual(sum(call.kwargs["json"]["instId"] == "FAILED-USDT-SWAP"
                             for call in session.post.call_args_list), 2)


if __name__ == "__main__":
    unittest.main()
