"""Isolated scheduler/deadline/storage contracts; never starts a trader process."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from astra_backend import deadline as D
from astra_gateway import scheduler as S, telemetry as T
from astra_gateway.store import GatewayStore

BJ = timezone(timedelta(hours=8))
AT = datetime(2026, 10, 5, 12, tzinfo=BJ)


def request(job_id, client="client-1", **changes):
    record = dict(job_run_id=job_id, scheduled_at=AT.isoformat(), caller="brain-cio",
                  client_request_id=client, request_id="", model="actual-model",
                  status="running", started_at=AT.isoformat(), completed_at="",
                  duration_ms=0, http_status=None, error_type="")
    record.update(changes)
    return record


class SharedDeadlineTests(unittest.TestCase):
    def test_nested_timeout_cannot_renew_parent(self):
        with patch.object(D.time, "monotonic", return_value=100) as clock:
            with D.deadline_scope(timeout=20):
                clock.return_value = 115
                with D.deadline_scope(timeout=20):
                    self.assertEqual(D.remaining(), 5)
                self.assertEqual(D.remaining(), 5)
        self.assertIsNone(D.current_deadline())

    def test_inference_scope_deducts_queue_time_and_caps_manual_calls(self):
        with patch.object(D.time, "time", return_value=1000), patch.object(D.time, "monotonic", return_value=50):
            with patch.dict(os.environ, {"ASTRA_INFERENCE_DEADLINE_EPOCH": "1020"}):
                with D.inference_scope():
                    self.assertEqual(D.remaining(), 20)
            with patch.dict(os.environ, {"ASTRA_INFERENCE_DEADLINE_EPOCH": ""}):
                with D.inference_scope():
                    self.assertEqual(D.remaining(), 810)

    def test_invalid_or_expired_boundary_fails_closed(self):
        with patch.object(D.time, "time", return_value=1000):
            for raw in ("nan", "inf", "bad", "999"):
                with self.subTest(raw=raw), patch.dict(os.environ, {"ASTRA_INFERENCE_DEADLINE_EPOCH": raw}):
                    with self.assertRaises(D.DeadlineExceeded), D.inference_scope():
                        self.fail("expired scope yielded")

    def test_retry_backoff_does_not_sleep_or_start_a_fresh_budget(self):
        with patch.object(D.time, "monotonic", return_value=100), patch.object(D.time, "sleep") as sleep:
            with D.deadline_scope(timeout=2):
                with self.assertRaises(D.DeadlineExceeded):
                    D.sleep_with_deadline(2)
            sleep.assert_not_called()


class SchedulerBudgetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = GatewayStore(Path(tmp.name) / "gateway.db")
        self.scheduler = S.GatewayScheduler(self.store, max_workers=1)
        self.scheduler.executor.shutdown(wait=True)
        self.scheduler.executor = MagicMock()
        self.scheduler.executor.submit.return_value = Future()
        self.spec = S.JobSpec("trader", "ai_factor_trader.py", 900, 1260)
        self.addCleanup(self.scheduler.shutdown)

    def tick(self, at):
        with patch.object(S, "current_jobs", return_value=(self.spec,)), patch.object(S, "load_schedule", return_value={}):
            return self.scheduler.tick(at)

    def test_cross_slot_running_job_is_recorded_once_and_never_overlaps(self):
        self.scheduler.running["trader"] = Future()
        self.assertEqual(self.tick(AT), [])
        self.assertEqual(self.tick(AT + timedelta(seconds=3)), [])
        rows = self.store.job_runs(20)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["status"], rows[0]["detail"]), ("skipped", "previous_run_active"))
        self.scheduler.executor.submit.assert_not_called()
        self.scheduler.running["trader"].set_result(None)
        self.assertEqual(self.tick(AT + timedelta(seconds=5)), [])
        self.assertEqual(self.tick(AT + timedelta(minutes=15)), ["trader"])

    def test_missed_window_is_durable_without_backfill(self):
        self.store.set_state("job.slot.trader", str(int(AT.timestamp()) // 900))
        self.assertEqual(self.tick(AT + timedelta(minutes=45, seconds=11)), [])
        rows = self.store.job_runs(20)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["detail"] for row in rows}, {"scheduler_window_missed"})
        self.scheduler.executor.submit.assert_not_called()
        self.assertEqual(self.tick(AT + timedelta(minutes=45, seconds=12)), [])
        self.assertEqual(len(self.store.job_runs(20)), 3)

    def test_original_slot_controls_queue_and_process_budget(self):
        done = MagicMock(returncode=0, stdout="done", stderr="")
        with patch.object(S.time, "time", return_value=AT.timestamp() + 180), patch.object(S.subprocess, "run", return_value=done) as run:
            self.scheduler._execute(self.spec, AT.isoformat())
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 690)
        self.assertEqual(float(kwargs["env"]["ASTRA_INFERENCE_DEADLINE_EPOCH"]), AT.timestamp() + 810)
        row = self.store.job_runs(1)[0]
        self.assertEqual(kwargs["env"]["ASTRA_JOB_RUN_ID"], str(row["id"]))
        self.assertEqual(kwargs["env"]["ASTRA_SCHEDULED_AT"], AT.isoformat())

    def test_exhausted_queue_never_launches_process(self):
        with patch.object(S.time, "time", return_value=AT.timestamp() + 810), patch.object(S.subprocess, "run") as run:
            self.scheduler._execute(self.spec, AT.isoformat())
        run.assert_not_called()
        row = self.store.job_runs(1)[0]
        self.assertEqual((row["status"], row["detail"]), ("skipped", "budget_exhausted_before_start"))

    def test_storage_admission_delay_is_deducted_immediately_before_launch(self):
        done = MagicMock(returncode=0, stdout="done", stderr="")
        with patch.object(S.time, "time", side_effect=[AT.timestamp(), AT.timestamp() + 30]), patch.object(S.subprocess, "run", return_value=done) as run:
            self.scheduler._execute(self.spec, AT.isoformat())
        self.assertEqual(run.call_args.kwargs["timeout"], 840)

    def test_storage_admission_can_exhaust_budget_without_a_process(self):
        with patch.object(S.time, "time", side_effect=[AT.timestamp() + 800, AT.timestamp() + 830]), patch.object(S.subprocess, "run") as run:
            self.scheduler._execute(self.spec, AT.isoformat())
        run.assert_not_called()
        rows = self.store.job_runs(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["status"], rows[0]["detail"]), ("skipped", "budget_exhausted_before_start"))

    def test_process_timeout_finishes_pending_attempt_and_does_not_fake_server_id(self):
        def timeout(*args, **kwargs):
            self.store.record_model_request(request(int(kwargs["env"]["ASTRA_JOB_RUN_ID"])))
            raise subprocess.TimeoutExpired("mock", kwargs["timeout"])
        with patch.object(S.time, "time", return_value=AT.timestamp()), patch.object(S.subprocess, "run", side_effect=timeout):
            self.scheduler._execute(self.spec, AT.isoformat())
        row = self.store.job_runs(1)[0]
        trace = self.store.model_requests(row["id"])[0]
        self.assertEqual(row["return_code"], 124)
        self.assertEqual(trace["status"], "cancelled")
        self.assertEqual(trace["request_id"], "")
        self.assertEqual(trace["error_type"], "JobTerminated")
        self.assertTrue(trace["completed_at"])
        self.assertEqual(datetime.fromisoformat(trace["completed_at"]).utcoffset(), timedelta(hours=8))

    def test_non_trader_timeout_unchanged_and_stale_boundary_removed(self):
        done = MagicMock(returncode=0, stdout="done", stderr="")
        with patch.dict(os.environ, {"ASTRA_INFERENCE_DEADLINE_EPOCH": "1"}), patch.object(S.subprocess, "run", return_value=done) as run:
            self.scheduler._execute(S.JobSpec("news", "mock.py", 60, 42), AT.isoformat())
        self.assertEqual(run.call_args.kwargs["timeout"], 42)
        self.assertNotIn("ASTRA_INFERENCE_DEADLINE_EPOCH", run.call_args.kwargs["env"])


class TraceStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "gateway.db"
        self.store = GatewayStore(self.path)
        self.job = self.store.begin_job("trader", AT.isoformat())

    def test_attempt_start_and_completion_upsert_one_row(self):
        self.store.record_model_request(request(self.job))
        self.store.record_model_request(request(self.job, status="success", request_id="new-api-123", completed_at=(AT + timedelta(seconds=3)).isoformat(), duration_ms=3000, http_status=200))
        rows = self.store.model_requests(self.job)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["status"], rows[0]["request_id"], rows[0]["model"]), ("success", "new-api-123", "actual-model"))
        self.assertEqual(rows[0]["scheduled_at"], AT.isoformat())
        self.assertEqual(len(self.store.recent_model_requests()), 1)

    def test_retry_attempts_remain_individually_linked(self):
        self.store.record_model_request(request(self.job, status="failed", request_id="server-1", completed_at=AT.isoformat(), http_status=524))
        self.store.record_model_request(request(self.job, "client-2", status="success", request_id="server-2", completed_at=AT.isoformat(), http_status=200))
        rows = self.store.model_requests(self.job)
        self.assertEqual([row["http_status"] for row in rows], [524, 200])
        self.assertEqual({row["job_run_id"] for row in rows}, {self.job})

    def test_older_expired_queue_slot_is_not_lost_after_a_newer_skip(self):
        later = (AT + timedelta(minutes=15)).isoformat()
        self.assertTrue(self.store.record_skipped_job("trader", later, "previous_run_active"))
        self.assertTrue(self.store.record_skipped_job("trader", AT.isoformat(), "budget_exhausted_before_start"))
        self.assertFalse(self.store.record_skipped_job("trader", AT.isoformat(), "budget_exhausted_before_start"))
        self.assertEqual(len([row for row in self.store.job_runs(10) if row["status"] == "skipped"]), 2)

    def test_recovery_closes_pending_only_preserving_completed_attempts(self):
        self.store.record_model_request(request(self.job))
        self.store.record_model_request(request(self.job, "client-2", status="success", request_id="server-ok", completed_at=AT.isoformat()))
        self.assertEqual(self.store.recover_stale_job_runs(), 1)
        rows = self.store.model_requests(self.job)
        self.assertEqual([r["status"] for r in rows], ["cancelled", "success"])
        self.assertEqual(rows[0]["error_type"], "WorkerInterrupted")
        self.assertEqual(rows[1]["request_id"], "server-ok")
        self.assertEqual(datetime.fromisoformat(rows[0]["completed_at"]).utcoffset(), timedelta(hours=8))

    def test_successful_job_closes_lost_completion_as_unknown_not_fake_success(self):
        self.store.record_model_request(request(self.job))
        self.store.finish_job(self.job, 0, "mock finished")
        row = self.store.model_requests(self.job)[0]
        self.assertEqual((row["status"], row["error_type"]), ("unknown", "TelemetryIncomplete"))
        self.assertTrue(row["completed_at"])
        self.assertEqual(row["request_id"], "")
        self.assertEqual(datetime.fromisoformat(row["completed_at"]).utcoffset(), timedelta(hours=8))

    def test_safe_projection_drops_prompt_url_secret_and_invalid_id(self):
        with patch.object(T, "DB_PATH", self.path):
            T.record_model_request(request(self.job, prompt="PRIVATE", secret="PRIVATE", url="https://private", request_id="Bearer PRIVATE", status="success", completed_at=AT.isoformat()))
        row = self.store.model_requests(self.job)[0]
        self.assertEqual(row["request_id"], "")
        self.assertNotIn("PRIVATE", repr(row))
        self.assertNotIn("prompt", row)

    def test_invalid_trace_is_ignored_without_affecting_inference(self):
        with patch.object(T, "DB_PATH", self.path):
            T.record_model_request(request(self.job, model="", status="success", completed_at=AT.isoformat()))
            T.record_model_request(request(self.job, status="success", completed_at="bad"))
            T.record_model_request(request(self.job, status="success"))
        self.assertEqual(self.store.model_requests(self.job), [])

    def test_aggregate_uses_actual_fallback_model_and_internal_trace_is_not_usage(self):
        with patch.object(T, "GatewayStore") as store:
            event = T.ModelCallTelemetry("brain", "requested-model", "high", "x", "y")
            event.finish("success", {"usage": {"_astra_trace": {"model": "actual-model"}}})
        record = store.return_value.record_model_call.call_args.args[0]
        self.assertEqual(record["model"], "actual-model")
        self.assertEqual(record["usage_keys"], "")
        self.assertEqual(record["cache_status"], "unreported")

    def test_additive_schema_preserves_old_job_writer_and_old_rows(self):
        old = self.path.parent / "old.db"
        with sqlite3.connect(old) as conn:
            conn.execute("CREATE TABLE job_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,job_name TEXT NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT NOT NULL DEFAULT '',return_code INTEGER,detail TEXT NOT NULL DEFAULT '')")
            conn.execute("INSERT INTO job_runs(job_name,status,started_at) VALUES ('trader','success','2026-10-05 12:00:00')")
        conn.close()
        upgraded = GatewayStore(old)
        self.assertEqual(upgraded.job_runs(10)[0]["scheduled_at"], "")
        with sqlite3.connect(old) as conn:
            conn.execute("INSERT INTO job_runs(job_name,status,started_at) VALUES ('trader','running','2026-10-05 12:15:00')")
        conn.close()
        self.assertEqual(len(upgraded.job_runs(10)), 2)
        self.assertEqual(upgraded.recent_model_requests(), [])

    def test_concurrent_old_database_initialization_serializes_column_migration(self):
        old = self.path.parent / "concurrent-old.db"
        with closing(sqlite3.connect(old)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE job_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,job_name TEXT NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT NOT NULL DEFAULT '',return_code INTEGER,detail TEXT NOT NULL DEFAULT '')")
            conn.execute("INSERT INTO job_runs(job_name,status,started_at) VALUES ('trader','success','2026-10-05 12:00:00')")
            conn.commit()
        barrier = threading.Barrier(2)
        real_connect = sqlite3.connect

        class ConcurrentConnection(sqlite3.Connection):
            def executescript(self, script):
                result = super().executescript(script)
                barrier.wait(timeout=5)
                return result

            def execute(self, sql, *args, **kwargs):
                result = super().execute(sql, *args, **kwargs)
                # If detection has no write transaction, reproduce the legal
                # interleaving where both services observe the old schema before
                # either ALTER starts. Transactional reads serialize naturally.
                if sql == "PRAGMA table_info(job_runs)" and not self.in_transaction:
                    captured = result.fetchall()
                    result.close()
                    barrier.wait(timeout=5)
                    return captured
                return result

        def connect(*args, **kwargs):
            kwargs["factory"] = ConcurrentConnection
            return real_connect(*args, **kwargs)

        with patch.object(sqlite3, "connect", side_effect=connect), ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(GatewayStore, old) for _ in range(2)]
            upgraded = [job.result(timeout=10) for job in jobs]
        self.assertEqual(len(upgraded), 2)
        for store in upgraded:
            self.assertEqual(store.job_runs(10)[0]["scheduled_at"], "")
        with closing(real_connect(old)) as conn:
            self.assertEqual([row[1] for row in conn.execute("PRAGMA table_info(job_runs)")].count("scheduled_at"), 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])


class AdminAttemptReadTests(unittest.TestCase):
    def test_attempts_remain_behind_existing_admin_authorization(self):
        from astra_backend.routers import system as api
        from fastapi import HTTPException
        with patch.object(api, "refresh_settings"), patch.object(api, "require_admin_header", side_effect=HTTPException(401)), patch.object(api, "GatewayStore") as store:
            with self.assertRaises(HTTPException):
                api.admin_agents(x_astra_admin_token=None)
        store.assert_not_called()

    def test_authenticated_response_adds_bounded_attempts(self):
        from astra_backend.routers import system as api
        with patch.object(api, "refresh_settings"), patch.object(api, "require_admin_header") as auth, patch.object(api, "GatewayStore") as store, patch.object(api, "agent_statuses", return_value=[]), patch.object(api, "secret_store_status", return_value={}):
            store.return_value.recent_model_requests.return_value = [{"request_id": "new-api-123"}]
            out = api.admin_agents(x_astra_admin_token="mock-admin")
        auth.assert_called_once_with("mock-admin")
        store.return_value.recent_model_requests.assert_called_once_with(100)
        self.assertEqual(out["model_requests"], [{"request_id": "new-api-123"}])
        self.assertIn("model_calls", out)


if __name__ == "__main__":
    unittest.main()
