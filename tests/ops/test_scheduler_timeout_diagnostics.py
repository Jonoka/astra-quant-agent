"""Timeout diagnostics are bounded, non-secret, and never change job outcomes."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from astra_gateway import collection_timeout as C
from astra_gateway import scheduler as S
from astra_gateway.store import GatewayStore

AT = "2026-10-15T08:00:00+00:00"
JOB = 7
PREFIX = "timeout after 55s; execution_budget_exhausted"


def phase(**changes):
    record = dict(kind="phase", job_run_id=JOB, scheduled_at=AT,
                  phase="factor_smart_money", result="ok", elapsed_ms=12.5,
                  remaining_ms=54200)
    record.update(changes)
    return record


def endpoint(**changes):
    record = phase(kind="endpoint", phase="factor_computation", method="POST",
                   endpoint="/api/v5/aigc/mcp/indicators", host="openapi.okx.com")
    record.update(changes)
    return record


def line(record):
    return "[Collection] " + json.dumps(record) + "\n"


def summary(stdout=None, stderr=None):
    text = C.collection_timeout_summary(stdout, stderr, JOB, AT)
    return json.loads(text) if text else {}


class CollectionTimeoutSummaryTests(unittest.TestCase):
    def test_phase_outcomes_and_stream_order_are_retained_separately(self):
        stdout = (line(phase()) + line(phase(phase="factor_computation", result="ReadTimeout"))
                  + line(phase(phase="factor_refresh", result="DeadlineExceeded"))
                  + line(phase(phase="factor_publish", elapsed_ms=7)))
        result = summary(stdout, line(phase(phase="collection", result="RuntimeError")))
        self.assertEqual(result["stdout"]["last_successful_phase"]["phase"], "factor_publish")
        self.assertEqual(result["stdout"]["first_failed_phase"]["phase"], "factor_computation")
        self.assertEqual(result["stderr"]["first_failed_phase"]["phase"], "collection")
        self.assertNotIn("last_successful_phase", result["stderr"])
        self.assertNotIn("active", json.dumps(result))

    def test_bytes_text_none_and_truncated_utf8(self):
        for raw in (line(phase()), line(phase()).encode(), b"\xff\xfe\n" + line(phase()).encode() + b"\xe2\x82"):
            with self.subTest(raw_type=type(raw).__name__):
                self.assertEqual(summary(raw)["stdout"]["last_successful_phase"]["elapsed_ms"], 12.5)
                self.assertEqual(summary(None, raw)["stderr"]["last_successful_phase"]["elapsed_ms"], 12.5)
        for raw in (None, b"", "", [], {}, 1, object()):
            with self.subTest(raw_type=type(raw).__name__):
                self.assertEqual(summary(raw), {})

    def test_endpoint_summary_ranks_completed_maxima_and_counts_errors(self):
        stdout = (line(endpoint(elapsed_ms=100, result="http_503"))
                  + line(endpoint(elapsed_ms=90, result="ReadTimeout"))
                  + line(endpoint(elapsed_ms=50))
                  + line(endpoint(endpoint="/api/v5/market/candles", method="GET", elapsed_ms=200)))
        result = summary(stdout, line(endpoint(elapsed_ms=60, result="http_503")))
        top = result["top_completed_endpoints"]
        self.assertEqual(top[0]["endpoint"], "/api/v5/market/candles")
        self.assertEqual(top[1]["max_ms"], 100)
        self.assertEqual(top[1]["count"], 4)
        self.assertEqual(top[1]["error_count"], 3)
        self.assertEqual(top[1]["top_errors"], {"http_503": 2, "ReadTimeout": 1})
        self.assertNotIn("stdout", result)
        self.assertNotIn("active", json.dumps(result))

    def test_error_counts_include_classes_beyond_the_top_three(self):
        raw = "".join(line(endpoint(result=result)) for result in (
            "ReadTimeout", "ReadTimeout", "http_503", "http_503", "http_502", "http_501"))
        top = summary(raw)["top_completed_endpoints"][0]
        self.assertEqual(top["count"], 6)
        self.assertEqual(top["error_count"], 6)
        self.assertEqual(top["top_errors"], {"ReadTimeout": 2, "http_503": 2, "http_501": 1})

    def test_endpoint_aggregation_keeps_phases_distinct(self):
        raw = (line(endpoint(phase="factor_smart_money", elapsed_ms=200, result="ReadTimeout"))
               + line(endpoint(phase="factor_computation", elapsed_ms=100)))
        top = summary(raw)["top_completed_endpoints"]
        self.assertEqual([item["phase"] for item in top], ["factor_smart_money", "factor_computation"])
        self.assertEqual([item["count"] for item in top], [1, 1])
        self.assertEqual([item["error_count"] for item in top], [1, 0])

    def test_requires_matching_run_and_schedule_with_integer_id(self):
        for changed in ({"job_run_id": JOB + 1}, {"job_run_id": str(JOB)}, {"job_run_id": float(JOB)}, {"job_run_id": "７"},
                        {"job_run_id": True}, {"scheduled_at": "2026-10-15T08:01:00+00:00"},
                        {"scheduled_at": None}, {"scheduled_at": []}):
            with self.subTest(changed=changed):
                self.assertEqual(summary(line(phase(**changed))), {})
        for run_id in (True, 0, -1, str(JOB), None):
            self.assertEqual(C.collection_timeout_summary(line(phase()), None, run_id, AT), "")
        self.assertEqual(C.collection_timeout_summary(line(phase()), None, JOB, None), "")

    def test_missing_identity_or_required_fields_are_rejected(self):
        for field in ("kind", "job_run_id", "scheduled_at", "phase", "result", "elapsed_ms"):
            record = phase()
            del record[field]
            with self.subTest(field=field):
                self.assertEqual(summary(line(record)), {})
        for field in ("endpoint", "host", "method"):
            record = endpoint()
            del record[field]
            with self.subTest(field=field):
                self.assertEqual(summary(line(record)), {})

    def test_extra_fields_and_ordinary_output_cannot_leak(self):
        secret = "secret-SHOULD-NEVER-APPEAR"
        raw = (f"Authorization: Bearer {secret}\nhttps://example.invalid/?key={secret}\n"
               + line(phase(message=secret, url=secret, headers={"Authorization": secret},
                            values={"balance": secret}, nested={"payload": secret}))
               + line(endpoint(message=secret, body=secret, values=[secret])))
        text = C.collection_timeout_summary(raw, secret, JOB, AT)
        self.assertNotIn(secret, text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("values", text)
        self.assertNotIn("https://", text)
        self.assertIn("factor_smart_money", text)

    def test_non_allowlisted_or_wrongly_typed_labels_are_rejected(self):
        for changed in ({"kind": "secret"}, {"phase": "secret"}, {"result": "secret"},
                        {"kind": []}, {"phase": {}}, {"result": []},
                        {"result": "http_099"}, {"result": "http_600"},
                        {"result": "http_５００"}, {"result": "http_500?token=secret"}):
            with self.subTest(changed=changed):
                self.assertEqual(summary(line(phase(**changed))), {})
        for changed in ({"endpoint": "/api/v5/market/candles?key=secret"},
                        {"endpoint": "https://www.okx.com/api/v5/market/candles"},
                        {"host": "www.okx.com:443"}, {"host": "secret.example"},
                        {"method": "GET secret"}, {"endpoint": []}, {"host": {}}, {"method": []}):
            with self.subTest(changed=changed):
                self.assertEqual(summary(line(endpoint(**changed))), {})

    def test_writer_placeholders_are_allowed(self):
        self.assertTrue(summary(line(phase(phase="other"))))
        self.assertTrue(summary(line(endpoint(phase="", endpoint="other", host="configured_host", method="OTHER"))))
        self.assertTrue(summary(line(phase(result="http_599"))))

    def test_timings_are_finite_nonnegative_numbers_not_booleans(self):
        for bad in (-1, True, False, "12.5", [], {}, float("nan"), float("inf"), -float("inf"), 10 ** 400):
            for field in ("elapsed_ms", "remaining_ms"):
                with self.subTest(field=field, bad_type=type(bad).__name__):
                    self.assertEqual(summary(line(phase(**{field: bad}))), {})
        self.assertEqual(summary(line(phase(elapsed_ms=None))), {})
        self.assertTrue(summary(line(phase(elapsed_ms=0, remaining_ms=0))))
        self.assertTrue(summary(line(phase(remaining_ms=None))))

    def test_large_finite_timings_do_not_overflow_aggregation(self):
        result = summary(line(endpoint(elapsed_ms=1e308)) * 3)
        self.assertEqual(result["top_completed_endpoints"][0]["max_ms"], 1e308)
        self.assertEqual(result["top_completed_endpoints"][0]["count"], 3)
        self.assertNotIn("Infinity", json.dumps(result))

    def test_malformed_deep_duplicate_nonstandard_and_partial_json_is_skipped(self):
        malformed = ["[]", "true", "null", "1", "{}", "{", "{\"kind\":\"phase\"}",
                     "[" * 1100 + "]" * 1100,
                     json.dumps(phase())[:-1] + ',"job_run_id":7}',
                     json.dumps(phase())[:-1] + ',"extra":NaN}',
                     json.dumps(phase())[:-1] + ',"extra":Infinity}',
                     json.dumps(phase()) + " trailing-secret"]
        for bad in malformed:
            with self.subTest(length=len(bad)):
                raw = "[Collection] " + bad + "\n"
                self.assertEqual(summary(raw), {})
                self.assertTrue(summary(raw + line(phase())))
        self.assertEqual(summary("noise " + line(phase())), {})
        self.assertEqual(summary(line(phase()).encode().replace(b"factor_smart_money", b"factor_smart_\xffmoney")), {})

    def test_stream_and_line_budgets_retain_a_labeled_tail(self):
        raw = "secret" * C._MAX_STREAM_UNITS + "\n" + line(phase())
        result = summary(raw)
        self.assertEqual(result["scope"], "bounded_tail")
        self.assertIn("last_successful_phase", result["stdout"])
        huge_line = line(phase(message="secret" * C._MAX_LINE_UNITS))
        result = summary(huge_line + line(phase()))
        self.assertEqual(result["scope"], "bounded_tail")
        self.assertNotIn("secret", json.dumps(result))
        result = summary(line(phase(result="ReadTimeout")) + "noise\n" * C._MAX_LINES + line(phase()))
        self.assertEqual(result["scope"], "bounded_tail")
        self.assertNotIn("first_failed_phase", result["stdout"])

    def test_json_parsing_work_is_bounded_for_both_streams(self):
        raw = line(phase()) * (C._MAX_LINES * 10)
        with patch.object(C.json, "loads", wraps=json.loads) as parse:
            text = C.collection_timeout_summary(raw, raw.encode(), JOB, AT)
        self.assertTrue(text)
        self.assertLessEqual(parse.call_count, 2 * C._MAX_LINES)
        self.assertTrue(all(len(call.args[0]) <= C._MAX_LINE_UNITS for call in parse.call_args_list))

    def test_cut_line_cannot_be_reinterpreted_as_a_complete_record(self):
        valid = line(phase())
        # Bound begins exactly at a forged marker, but the preceding byte is not LF.
        raw = "x" + valid + "x" * (C._MAX_STREAM_UNITS - len(valid))
        self.assertEqual(summary(raw), {})

    def test_output_is_bounded_without_cutting_json_or_the_timeout_prefix(self):
        raw = line(phase()) + line(phase(phase="factor_computation", result="DeadlineExceeded"))
        for index, path in enumerate(sorted(C._ENDPOINTS)):
            raw += line(endpoint(endpoint=path, elapsed_ms=index + 1, result="ConnectionRefusedError"))
        text = C.collection_timeout_summary(raw, raw, JOB, AT)
        parsed = json.loads(text)
        self.assertLessEqual(len(text), C._MAX_SUMMARY_UNITS)
        self.assertLessEqual(len(PREFIX + "; collection=" + text), 2000)
        self.assertLessEqual(len(parsed["top_completed_endpoints"]), 3)
        self.assertIn("stdout", parsed)
        self.assertIn("stderr", parsed)

    def test_internal_parser_failure_returns_no_summary(self):
        with patch.object(C, "_records", side_effect=RuntimeError("secret")):
            self.assertEqual(C.collection_timeout_summary(line(phase()), None, JOB, AT), "")


class SchedulerTimeoutDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="astra-timeout-diagnostics-")
        self.addCleanup(tmp.cleanup)
        self.store = GatewayStore(Path(tmp.name) / "gateway.db")
        self.scheduler = S.GatewayScheduler(self.store, max_workers=1)
        self.addCleanup(self.scheduler.shutdown)
        self.spec = next(spec for spec in S.JOBS if spec.name == "factor_library")

    def execute_timeout(self, stdout, stderr=None, spec=None):
        def timeout(*args, **kwargs):
            run_id = int(kwargs["env"]["ASTRA_JOB_RUN_ID"])
            out = stdout(run_id) if callable(stdout) else stdout
            err = stderr(run_id) if callable(stderr) else stderr
            raise subprocess.TimeoutExpired("synthetic", kwargs["timeout"], output=out, stderr=err)
        with patch.object(S.subprocess, "run", side_effect=timeout) as run:
            self.scheduler._execute(spec or self.spec, AT)
        return self.store.job_runs(1)[0], run.call_args.kwargs

    def test_real_scheduler_stores_sanitized_bytes_and_original_failure_semantics(self):
        def output(run_id):
            return ("secret-raw-output\n" + line(phase(job_run_id=run_id))
                    + line(endpoint(job_run_id=run_id, result="http_503", elapsed_ms=100))).encode()
        row, kwargs = self.execute_timeout(output)
        self.assertEqual(kwargs["timeout"], 55)
        self.assertTrue(kwargs["text"])
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(row["return_code"], 124)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["process_status"], "failed")
        self.assertTrue(row["detail"].startswith(PREFIX + "; collection="))
        self.assertIn('"max_ms":100', row["detail"])
        self.assertNotIn("secret", row["detail"])
        self.assertLessEqual(len(row["detail"]), 2000)

    def test_storage_keeps_timeout_prefix_with_maximal_safe_summary(self):
        def output(run_id):
            raw = line(phase(job_run_id=run_id))
            raw += line(phase(job_run_id=run_id, result="DeadlineExceeded"))
            for index, path in enumerate(sorted(C._ENDPOINTS)):
                raw += line(endpoint(job_run_id=run_id, endpoint=path, elapsed_ms=index + 1,
                                     result="ConnectionRefusedError"))
            return raw
        row, _ = self.execute_timeout(output, output)
        self.assertTrue(row["detail"].startswith(PREFIX + "; collection="))
        self.assertLessEqual(len(row["detail"]), 2000)
        payload = json.loads(row["detail"].split("; collection=", 1)[1])
        self.assertTrue(payload["top_completed_endpoints"])
        self.assertIn("first_failed_phase", payload["stdout"])
        self.assertIn("last_successful_phase", payload["stderr"])

    def test_stderr_is_inspected_even_when_stdout_has_no_diagnostics(self):
        row, _ = self.execute_timeout("secret", lambda run_id: line(phase(job_run_id=run_id, result="DeadlineExceeded")))
        payload = json.loads(row["detail"].split("; collection=", 1)[1])
        self.assertEqual(payload["stderr"]["first_failed_phase"]["result"], "DeadlineExceeded")
        self.assertNotIn("secret", row["detail"])

    def test_no_valid_diagnostics_preserves_exact_old_detail(self):
        for out, err in ((None, None), ("secret", b"secret"), (line(phase(job_run_id=999)), None),
                         ("[Collection] {", None)):
            with self.subTest(out_type=type(out).__name__):
                row, _ = self.execute_timeout(out, err)
                self.assertEqual(row["detail"], PREFIX)
                self.assertEqual(row["return_code"], 124)

    def test_parser_exception_does_not_mask_timeout_or_finish_it_twice(self):
        with patch.object(S, "collection_timeout_summary", side_effect=RuntimeError("secret")):
            row, _ = self.execute_timeout(line(phase()))
        self.assertEqual(row["detail"], PREFIX)
        self.assertEqual(row["return_code"], 124)
        self.assertEqual(len(self.store.job_runs(10)), 1)

    def test_trader_health_remains_failed_despite_a_linked_ok_brain_snapshot(self):
        spec = next(spec for spec in S.JOBS if spec.name == "trader")
        def output(run_id):
            return line(phase(job_run_id=run_id)).encode()
        def brain_result():
            row = self.store.job_runs(1)[0]
            return dict(job_run_id=row["id"], scheduled_at=AT, last_status="ok", last_at=AT)
        with patch.object(S.time, "time", return_value=datetime.fromisoformat(AT).timestamp()), \
                patch.object(S, "read_brain_result", side_effect=brain_result):
            row, kwargs = self.execute_timeout(output, spec=spec)
        self.assertEqual(kwargs["timeout"], 870)
        self.assertEqual(row["return_code"], 124)
        self.assertEqual(row["brain_status"], "ok")
        self.assertEqual(row["process_status"], "failed")
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["detail"].startswith("timeout after 870s; execution_budget_exhausted"))
        self.assertIn('"last_successful_phase"', row["detail"])

    def test_success_and_non_timeout_failures_keep_existing_details(self):
        for code, out, err, expected in ((0, "success-tail", "ignored", "success-tail"),
                                         (1, "ignored", "failure-tail", "failure-tail")):
            with self.subTest(code=code), patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess("synthetic", code, out, err)), \
                    patch.object(S, "collection_timeout_summary") as parser:
                self.scheduler._execute(self.spec, AT)
                row = self.store.job_runs(1)[0]
                self.assertEqual(row["return_code"], code)
                self.assertEqual(row["detail"], expected)
                parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
