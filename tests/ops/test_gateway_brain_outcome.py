"""Per-run inspection/brain status contracts. Only temp files and stub children."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from astra_backend.dashboard_payload.health import build_ai_health
from astra_backend.dashboard_payload.integrity_sidecars import merge_ai_health_failures
from astra_gateway import agents, scheduler
from astra_gateway.store import GatewayStore, job_run_outcome

AT = datetime(2026, 10, 5, 12, tzinfo=timezone(timedelta(hours=8)))


class BrainOutcomeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.store = GatewayStore(self.data / "astra_gateway.db")
        self.addCleanup(patch.stopall)
        patch.object(agents, "ROOT", self.root).start()
        patch.object(scheduler, "ROOT", self.root).start()
        patch.dict(os.environ, {"ASTRA_GATEWAY_DB": str(self.store.path)}).start()
        self.scheduled_at = AT.isoformat()

    def result(self, run_id, status="failed", **changes):
        payload = dict(job_run_id=run_id, scheduled_at=self.scheduled_at,
                       last_status=status, last_at=(AT + timedelta(seconds=10)).isoformat(),
                       last_error="collection deadline" if status == "failed" else None)
        payload.update(changes)
        return payload

    def finish(self, status="failed", return_code=0):
        run_id = self.store.begin_job("trader", self.scheduled_at)
        self.store.finish_job(run_id, return_code, "inspection completed", self.result(run_id, status))
        return self.store.job_runs(1)[0]

    def brain_agent(self):
        return next(row for row in agents.agent_statuses(self.store.job_runs()) if row["id"] == "trading_brain")

    def test_rc_zero_and_current_brain_failure_retains_both_facts(self):
        row = self.finish()
        self.assertEqual((row["status"], row["process_status"], row["brain_status"], row["return_code"]),
                         ("failed", "success", "failed", 0))
        self.assertEqual(row["detail"], "inspection completed")
        self.assertEqual(row["brain_detail"], "collection deadline")
        self.assertTrue(row["brain_result_at"])
        # The audit evidence is durable, not computed from today's sidecar.
        self.assertEqual(GatewayStore(self.store.path).job_runs(1)[0], row)

    def test_current_success_is_success(self):
        row = self.finish("ok")
        self.assertEqual((row["status"], row["brain_status"]), ("success", "ok"))

    def test_current_explicit_skip_is_separate_from_unknown(self):
        row = self.finish("skipped")
        self.assertEqual((row["status"], row["process_status"], row["brain_status"]),
                         ("completed", "success", "skipped"))

    def test_stale_failed_or_ok_result_never_controls_new_run(self):
        for stale_status in ("failed", "ok", "skipped"):
            with self.subTest(stale_status=stale_status):
                old = self.store.begin_job("trader", self.scheduled_at)
                new = self.store.begin_job("trader", self.scheduled_at)
                self.store.finish_job(new, 0, "done", self.result(old, stale_status))
                row = self.store.job_runs(1)[0]
                self.assertEqual((row["status"], row["brain_status"]), ("completed", "unknown"))
                self.assertEqual(row["brain_detail"], "")

    def test_matching_id_with_different_slot_is_unconfirmed(self):
        run = self.store.begin_job("trader", self.scheduled_at)
        self.store.finish_job(run, 0, "done", self.result(run, scheduled_at=(AT - timedelta(minutes=15)).isoformat()))
        self.assertEqual(self.store.job_runs(1)[0]["brain_status"], "unknown")

    def test_legacy_or_malformed_sidecars_fail_closed_without_faking_failure(self):
        for payload in ({"last_status": "failed"}, {"last_status": "ok"}, [], None,
                        self.result(True), self.result(1, last_status=["failed"])):
            with self.subTest(payload=payload):
                run = self.store.begin_job("trader", self.scheduled_at)
                if isinstance(payload, dict) and isinstance(payload.get("last_status"), list):
                    payload["job_run_id"] = run
                self.store.finish_job(run, 0, "done", payload)
                self.assertEqual(self.store.job_runs(1)[0]["brain_status"], "unknown")

    def test_process_failure_is_visible_even_when_brain_succeeded(self):
        row = self.finish("ok", 7)
        self.assertEqual((row["status"], row["process_status"], row["brain_status"]), ("failed", "failed", "ok"))

    def test_non_trader_rc_zero_keeps_existing_success_semantics(self):
        run = self.store.begin_job("news", self.scheduled_at)
        self.store.finish_job(run, 0, "done", self.result(run))
        row = self.store.job_runs(1)[0]
        self.assertEqual((row["status"], row["brain_status"]), ("success", "not_applicable"))

    def test_duplicate_finish_cannot_rewrite_terminal_evidence(self):
        row = self.finish()
        self.store.finish_job(row["id"], 0, "later", self.result(row["id"], "ok"))
        self.assertEqual(self.store.job_runs(1)[0], row)

    def test_previous_writer_terminal_fields_override_pending_process_columns(self):
        # Previous versions only know the original lifecycle columns. They can
        # finish/recover a row created by the new version during rollback.
        for status, code, expected in (("success", 0, "success"), ("failed", 7, "failed"),
                                       ("interrupted", None, "interrupted"),
                                       ("skipped", None, "skipped")):
            with self.subTest(status=status):
                row = job_run_outcome(dict(job_name="trader", status=status,
                    return_code=code, process_status="running", brain_status="pending"))
                self.assertEqual(row["process_status"], expected)
                self.assertEqual(row["brain_status"], "not_run" if status == "skipped" else "unknown")

    def test_previous_writer_process_changes_preserve_terminal_brain_evidence(self):
        row = job_run_outcome(dict(job_name="trader", status="failed", return_code=7,
            process_status="success", brain_status="ok", brain_result_at=self.scheduled_at,
            brain_detail="confirmed decision"))
        self.assertEqual((row["status"], row["process_status"], row["brain_status"]),
                         ("failed", "failed", "ok"))
        self.assertEqual((row["brain_result_at"], row["brain_detail"]),
                         (self.scheduled_at, "confirmed decision"))

    def test_scheduler_skips_and_recovery_have_explicit_process_outcomes(self):
        self.store.record_skipped_job("trader", self.scheduled_at, "scheduler_window_missed")
        row = self.store.job_runs(1)[0]
        self.assertEqual((row["process_status"], row["brain_status"]), ("skipped", "not_run"))
        run = self.store.begin_job("trader", self.scheduled_at)
        self.store.recover_stale_job_runs()
        row = self.store.job_runs(1)[0]
        self.assertEqual((row["status"], row["process_status"], row["brain_status"]), ("interrupted", "interrupted", "unknown"))

    def test_failure_outranks_missing_or_old_artifact(self):
        row = self.finish()
        agent = self.brain_agent()
        self.assertEqual(agent["health"], "degraded")
        self.assertEqual(agent["last_job_run_id"], row["id"])
        self.assertEqual((agent["process_status"], agent["brain_status"]), ("success", "failed"))
        (self.data / "ai_brain_decisions.json").write_text("{}")
        self.assertEqual(self.brain_agent()["health"], "degraded")

    def test_unconfirmed_new_run_does_not_inherit_agent_failure(self):
        self.finish()
        new = self.store.begin_job("trader", self.scheduled_at)
        self.store.finish_job(new, 0, "done")
        agent = self.brain_agent()
        self.assertEqual((agent["health"], agent["brain_status"]), ("unknown", "unknown"))

    def test_dashboard_reads_latest_run_not_old_success_history_or_sidecar(self):
        failed = self.finish()
        (self.data / "ai_health.json").write_text(json.dumps(self.result(999, "ok")))
        health = build_ai_health(self.data, [{"time": "old", "council_status": {"ran": True}}])
        self.assertEqual((health["last_status"], health["process_status"], health["job_run_id"]), ("failed", "success", failed["id"]))
        self.assertIsNone(health["last_council_status"])
        self.assertEqual(health["last_known_result"]["job_run_id"], 999)
        new = self.store.begin_job("trader", self.scheduled_at)
        self.store.finish_job(new, 0, "done")
        self.assertEqual(build_ai_health(self.data, [])["last_status"], "unknown")

    def test_dashboard_missing_db_is_read_only_and_marks_old_sidecar_historical(self):
        missing = self.data / "does-not-exist.db"
        (self.data / "ai_health.json").write_text(json.dumps(self.result(42)))
        with patch.dict(os.environ, {"ASTRA_GATEWAY_DB": str(missing)}):
            health = build_ai_health(self.data, [])
        self.assertFalse(missing.exists())
        self.assertEqual(health["last_status"], "unknown")
        self.assertEqual(health["last_known_result"]["last_status"], "failed")

    def test_scheduler_stub_correlates_health_and_honors_existing_factor_timeout(self):
        worker = scheduler.GatewayScheduler(self.store, max_workers=1)
        self.addCleanup(worker.shutdown)
        def done(*args, **kwargs):
            env = kwargs["env"]
            (self.data / "ai_health.json").write_text(json.dumps(self.result(int(env["ASTRA_JOB_RUN_ID"]))))
            return MagicMock(returncode=0, stdout="inspection completed", stderr="")
        with patch.object(scheduler.time, "time", return_value=AT.timestamp()), patch.object(scheduler.subprocess, "run", side_effect=done):
            worker._execute(scheduler.JobSpec("trader", "stub.py", 900, 1260), self.scheduled_at)
        self.assertEqual(self.store.job_runs(1)[0]["status"], "failed")
        with patch.object(scheduler.time, "time", return_value=1000), patch.object(scheduler.subprocess, "run", return_value=MagicMock(returncode=0, stdout="done", stderr="")) as run:
            with patch.dict(os.environ, {"ASTRA_EXECUTION_DEADLINE_EPOCH": "1", "ASTRA_INFERENCE_DEADLINE_EPOCH": "1"}):
                worker._execute(scheduler.JobSpec("factor_library", "stub.py", 60, 55), self.scheduled_at)
        self.assertEqual(run.call_args.kwargs["timeout"], 55)
        self.assertEqual(run.call_args.kwargs["env"]["ASTRA_EXECUTION_DEADLINE_EPOCH"], "1055")
        self.assertNotIn("ASTRA_INFERENCE_DEADLINE_EPOCH", run.call_args.kwargs["env"])

    def test_scheduler_timeout_preserves_correlated_brain_failure(self):
        worker = scheduler.GatewayScheduler(self.store, max_workers=1)
        self.addCleanup(worker.shutdown)
        def timeout(*args, **kwargs):
            (self.data / "ai_health.json").write_text(json.dumps(self.result(int(kwargs["env"]["ASTRA_JOB_RUN_ID"]))))
            raise subprocess.TimeoutExpired("stub", kwargs["timeout"])
        with patch.object(scheduler.time, "time", return_value=AT.timestamp()), patch.object(scheduler.subprocess, "run", side_effect=timeout):
            worker._execute(scheduler.JobSpec("trader", "stub.py", 900, 1260), self.scheduled_at)
        row = self.store.job_runs(1)[0]
        self.assertEqual((row["status"], row["process_status"], row["brain_status"], row["return_code"]), ("failed", "failed", "failed", 124))

    def test_old_schema_migrates_in_place_without_attaching_current_sidecar(self):
        path = self.data / "legacy.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE job_runs(id INTEGER PRIMARY KEY, job_name TEXT, status TEXT, started_at TEXT, finished_at TEXT, return_code INTEGER, detail TEXT)")
            connection.execute("INSERT INTO job_runs VALUES(7,'trader','success','old','old',0,'old output')")
            connection.commit()
        store = GatewayStore(path)
        row = store.job_runs(1)[0]
        self.assertEqual((row["id"], row["status"], row["process_status"], row["brain_status"]), (7, "completed", "success", "unknown"))
        self.assertEqual(row["detail"], "old output")
        with store.connect() as connection:
            self.assertEqual(connection.execute("SELECT status FROM job_runs").fetchone()["status"], "success")
        self.assertEqual(GatewayStore(path).job_runs(1)[0], row)

    def test_integrity_warning_requires_current_failed_result_and_retains_threshold(self):
        row = self.finish()
        sidecar = self.result(row["id"], consecutive_failures=2)
        (self.data / "ai_health.json").write_text(json.dumps(sidecar))
        errors = []
        merge_ai_health_failures(errors, self.data)
        self.assertEqual(len(errors), 1)
        self.assertIn("结果时间", errors[0])
        self.assertNotIn("本轮", errors[0])
        sidecar["consecutive_failures"] = 1
        (self.data / "ai_health.json").write_text(json.dumps(sidecar))
        errors = []
        merge_ai_health_failures(errors, self.data)
        self.assertEqual(errors, [])

    def test_integrity_does_not_mark_skipped_or_new_unknown_cycle_as_old_failure(self):
        old = self.finish()
        (self.data / "ai_health.json").write_text(json.dumps(self.result(old["id"], consecutive_failures=2)))
        new = self.store.begin_job("trader", self.scheduled_at)
        self.store.finish_job(new, 0, "done")
        errors = []
        merge_ai_health_failures(errors, self.data)
        self.assertEqual(errors, [])
        (self.data / "ai_health.json").write_text(json.dumps(self.result(new, "skipped", consecutive_failures=2)))
        merge_ai_health_failures(errors, self.data)
        self.assertEqual(errors, [])

    def test_legacy_failure_without_db_is_labeled_historical(self):
        (self.data / "ai_health.json").write_text(json.dumps({"consecutive_failures": 2, "last_error": "old error"}))
        with patch.dict(os.environ, {"ASTRA_GATEWAY_DB": str(self.data / "missing.db")}):
            errors = []
            merge_ai_health_failures(errors, self.data)
        self.assertEqual(len(errors), 1)
        self.assertIn("最近已记录", errors[0])
        self.assertNotIn("本轮", errors[0])

    def test_per_job_latest_survives_more_than_two_hundred_factor_runs(self):
        ids = {}
        for name in ("daily_briefing", "self_improvement"):
            run = self.store.begin_job(name, self.scheduled_at)
            self.store.finish_job(run, 1, "failed low-frequency job")
            ids[name] = run
        for _ in range(205):
            run = self.store.begin_job("factor_library", self.scheduled_at)
            self.store.finish_job(run, 0, "done")
        self.assertEqual({row["job_name"] for row in self.store.job_runs(200)}, {"factor_library"})
        latest = {row["job_name"]: row for row in self.store.latest_job_runs()}
        for name, run in ids.items():
            self.assertEqual((latest[name]["id"], latest[name]["status"]), (run, "failed"))
        worker = scheduler.GatewayScheduler(self.store, max_workers=1)
        self.addCleanup(worker.shutdown)
        with patch.object(scheduler, "load_schedule", return_value={}), patch.object(scheduler, "current_jobs", return_value=(
                scheduler.JobSpec("daily_briefing", "stub.py"), scheduler.JobSpec("self_improvement", "stub.py"))):
            for snapshot in (scheduler.scheduler_snapshot(self.store), worker.status()):
                jobs = {job["name"]: job for job in snapshot["jobs"]}
                for name, run in ids.items():
                    self.assertEqual((jobs[name]["latest_run"]["id"], jobs[name]["latest_run"]["status"]), (run, "failed"))
        self.assertEqual(next(row for row in agents.agent_statuses(self.store.latest_job_runs())
                              if row["id"] == "self_improvement")["health"], "degraded")

    def test_admin_agents_uses_per_job_latest_not_bounded_global_history(self):
        from astra_backend.routers import system
        with patch.object(system, "refresh_settings"), patch.object(system, "require_admin_header"), \
                patch.object(system, "GatewayStore", return_value=self.store), patch.object(system, "secret_store_status", return_value={}), \
                patch.object(self.store, "latest_job_runs", wraps=self.store.latest_job_runs) as latest:
            system.admin_agents("test-token")
        latest.assert_called_once_with()
