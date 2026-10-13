"""Gateway-owned scheduler running existing jobs in isolated subprocesses."""
from __future__ import annotations
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import subprocess
import sys
import time
import threading
from typing import Any

from astra_backend.time_utils import parse_beijing
from astra_backend.schedule_store import load_schedule
from astra_backend.backup_store import list_jobs as list_backup_jobs
from astra_gateway.store import GatewayStore
from astra_backend.maintenance_runtime import AdmissionClosed, get_runtime, run_process

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BJ_TZ = timezone(timedelta(hours=8))
TRADER_SLOT_GUARD_SECONDS = 30
TRADER_PERSISTENCE_SECONDS = 60


@dataclass(frozen=True)
class JobSpec:
    name: str
    script: str
    interval_seconds: int | None = None
    timeout_seconds: int = 600
    schedule_key: str = ""
    default_times: tuple[str, ...] = ()
    offset_seconds: int = 0


JOBS = (
    # trader 超时 840→1260s：投委会新预算 240s + 网关故障时单模型内部重试链(~600s)
    # 最坏 ~1150s，840s 会把整周期腰斩且连降级透明记录都写不出（2026-09-10 05:00 实测）
    JobSpec("trader", "ai_factor_trader.py", 15 * 60, 1260),
    JobSpec("factor_library", "factor_library.py", 60, 55),
    JobSpec("news", "news_sentiment_harvester.py", 10 * 60, 300, offset_seconds=180),
    # 而 OI 重建本身是 5m 粒度、提示词每 15 分钟才消费一次 ⇒ 10 分钟足够新鲜（引擎侧
    # 30 分钟才算 stale，留 3 倍余量）。超时 300s 给慢网络留头。
    JobSpec("daily_briefing", "daily_summary_and_backup.py", None, 600, "briefing_times", ("08:00", "20:00")),
    JobSpec("self_improvement", "self_improvement_engine.py", None, 1200, "self_improvement_times", ("02:00", "08:00", "14:00", "20:00")),
)


def backup_job_specs() -> tuple[JobSpec, ...]:
    specs: list[JobSpec] = []
    for index, job in enumerate(list_backup_jobs()):
        if not job.get("enabled"):
            continue
        name = "nightly_backup" if index == 0 or job.get("id") == "nightly-default" else f"backup:{job['id']}"
        specs.append(JobSpec(name, "nightly_backup_and_clean.py", None, 1800, f"backup_job:{job['id']}", tuple(job.get("schedule_times", ["02:00"]))))
    return tuple(specs)


def current_jobs() -> tuple[JobSpec, ...]:
    return (*JOBS, *backup_job_specs())


def scheduler_snapshot(store: GatewayStore) -> dict[str, Any]:
    schedule = load_schedule()
    now = datetime.now(BJ_TZ)
    jobs = []
    for spec in current_jobs():
        raw = store.get_state(f"job.last.{spec.name}")
        try:
            last = parse_beijing(raw)
        except ValueError:
            last = None
        value = schedule.get(spec.schedule_key) if spec.schedule_key else None
        times = tuple(str(item) for item in value) if isinstance(value, list) else ((str(value),) if isinstance(value, str) else spec.default_times)
        schedule_text = f"每 {spec.interval_seconds // 60} 分钟 (错峰 +{spec.offset_seconds // 60}m)" if (spec.interval_seconds and spec.offset_seconds) else (f"每 {spec.interval_seconds // 60} 分钟" if spec.interval_seconds else "、".join(times))
        jobs.append({
            "name": spec.name,
            "script": spec.script,
            "last_scheduled_at": last.isoformat() if last else "",
            "schedule": schedule_text,
            "timezone": "Asia/Shanghai",
            "overdue": bool(spec.interval_seconds and last and (now - last).total_seconds() > spec.interval_seconds * 2),
            "offset_seconds": spec.offset_seconds,
        })
    return {"jobs": jobs, "recent_runs": store.job_runs(30)}


class GatewayScheduler:
    def __init__(self, store: GatewayStore, max_workers: int = 3, maintenance=None):
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="astra-job")
        self.running: dict[str, Future[None]] = {}
        self.maintenance = maintenance or get_runtime("gateway")

    def _last_at(self, name: str) -> datetime | None:
        raw = self.store.get_state(f"job.last.{name}")
        try:
            return parse_beijing(raw)
        except ValueError:
            return None

    def initialize_migration_baseline(self, now: datetime | None = None) -> None:
        now = now or datetime.now(BJ_TZ)
        for spec in current_jobs():
            if not self.store.get_state(f"job.last.{spec.name}"):
                self.store.set_state(f"job.last.{spec.name}", now.isoformat())

    def _scheduled_times(self, spec: JobSpec, schedule: dict[str, Any]) -> tuple[str, ...]:
        if spec.schedule_key.startswith("backup_job:"):
            return spec.default_times
        value = schedule.get(spec.schedule_key)
        # Also check fallback keys if list key not found
        if value is None and spec.schedule_key == "self_improvement_times":
            value = schedule.get("self_improvement_time")
        if isinstance(value, list):
            return tuple(str(item) for item in value)
        if isinstance(value, str):
            return (value,)
        return spec.default_times

    def due(self, spec: JobSpec, now: datetime, schedule: dict[str, Any]) -> bool:
        last = self._last_at(spec.name)
        if spec.interval_seconds:
            if spec.name == "trader":
                slot = int(now.timestamp()) // spec.interval_seconds
                last_slot = int(last.timestamp()) // spec.interval_seconds if last else -1
                observed = self.store.get_state("job.slot.trader")
                if observed.isdigit():
                    last_slot = max(last_slot, int(observed))
                return slot > last_slot and int(now.timestamp()) % spec.interval_seconds < 10
            if spec.offset_seconds:
                # Staggered execution aligned to clock with offset to prevent resource collisions
                ts = int(now.timestamp())
                slot = (ts - spec.offset_seconds) // spec.interval_seconds
                last_slot = (int(last.timestamp()) - spec.offset_seconds) // spec.interval_seconds if last else -1
                sec_in_slot = (ts - spec.offset_seconds) % spec.interval_seconds
                return slot > last_slot and sec_in_slot < 30
            return not last or (now - last).total_seconds() >= spec.interval_seconds
        minute = now.strftime("%H:%M")
        if minute not in self._scheduled_times(spec, schedule):
            return False
        return not last or last.date() != now.date() or last.strftime("%H:%M") != minute

    def _execute(self, spec: JobSpec, scheduled_at: str | None = None) -> None:
        scheduled_at = scheduled_at or datetime.now(BJ_TZ).isoformat()
        timeout = spec.timeout_seconds
        env = dict(os.environ)
        if spec.name == "trader" and spec.interval_seconds:
            scheduled = parse_beijing(scheduled_at)
            slot_end = (int(scheduled.timestamp()) // spec.interval_seconds + 1) * spec.interval_seconds
            hard_deadline = slot_end - TRADER_SLOT_GUARD_SECONDS
            inference_deadline = hard_deadline - TRADER_PERSISTENCE_SECONDS
            if time.time() >= inference_deadline:
                self.store.record_skipped_job(spec.name, scheduled_at, "budget_exhausted_before_start")
                return
            env["ASTRA_INFERENCE_DEADLINE_EPOCH"] = str(inference_deadline)
        else:
            # A non-trader subprocess must not inherit a stale trader boundary.
            env.pop("ASTRA_INFERENCE_DEADLINE_EPOCH", None)
        run_id = self.store.begin_job(spec.name, scheduled_at)
        env["ASTRA_JOB_RUN_ID"] = str(run_id)
        env["ASTRA_SCHEDULED_AT"] = scheduled_at
        try:
            command = [sys.executable, str(SCRIPTS / spec.script)]
            if spec.schedule_key.startswith("backup_job:"):
                command.extend(["--job-id", spec.schedule_key.split(":", 1)[1]])
            if spec.name == "trader" and spec.interval_seconds:
                # SQLite admission can block too. Deduct it immediately before
                # launching; never grant a timeout computed before begin_job.
                admitted_at = time.time()
                if admitted_at >= inference_deadline:
                    self.store.skip_started_job(run_id, "budget_exhausted_before_start")
                    return
                timeout = min(timeout, hard_deadline - admitted_at)
            result = run_process(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=env,
            )
            detail = (result.stderr if result.returncode else result.stdout)[-2000:]
            self.store.finish_job(run_id, result.returncode, detail)
        except subprocess.TimeoutExpired:
            self.store.finish_job(run_id, 124, f"timeout after {timeout:g}s; execution_budget_exhausted")
        except Exception as exc:
            self.store.finish_job(run_id, 1, f"{type(exc).__name__}: {exc}")

    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(BJ_TZ)
        self.running = {name: future for name, future in self.running.items() if not future.done()}
        try:
            activity = self.maintenance.admit("scheduler-tick")
        except AdmissionClosed:
            self.maintenance.poll()
            return []
        return self.maintenance.run_admitted(activity, self._tick_admitted, now)

    def _tick_admitted(self, now: datetime) -> list[str]:
        schedule = load_schedule()
        launched: list[str] = []
        for spec in current_jobs():
            if spec.name == "trader" and spec.interval_seconds:
                slot = int(now.timestamp()) // spec.interval_seconds
                observed = self.store.get_state("job.slot.trader")
                last = self._last_at(spec.name)
                prior = int(observed) if observed.isdigit() else (int(last.timestamp()) // spec.interval_seconds if last else slot - 1)
                # Record missed windows after a stalled scheduler/restart, but do
                # not replay stale model or trading work. Limit recovery metadata.
                for missed in range(max(prior + 1, slot - 96), slot):
                    at = datetime.fromtimestamp(missed * spec.interval_seconds, BJ_TZ).isoformat()
                    self.store.record_skipped_job(spec.name, at, "scheduler_window_missed")
                if slot > prior and (spec.name in self.running or int(now.timestamp()) % spec.interval_seconds >= 10):
                    reason = "previous_run_active" if spec.name in self.running else "scheduler_window_missed"
                    at = datetime.fromtimestamp(slot * spec.interval_seconds, BJ_TZ).isoformat()
                    self.store.record_skipped_job(spec.name, at, reason)
                    self.store.set_state("job.slot.trader", str(slot))
                    continue
            if spec.name in self.running or not self.due(spec, now, schedule):
                continue
            try:
                # Register before submit: queued Futures block pause just like
                # running children. No fresh queue admission after a fence.
                activity = self.maintenance.admit("scheduler-job:" + spec.name)
            except AdmissionClosed:
                break
            try:
                self.store.set_state(f"job.last.{spec.name}", now.isoformat())
                if spec.name == "trader" and spec.interval_seconds:
                    self.store.set_state("job.slot.trader", str(int(now.timestamp()) // spec.interval_seconds))
                self.running[spec.name] = self.executor.submit(
                    self.maintenance.run_admitted, activity, self._execute, spec, now.isoformat())
                self.maintenance.register_threads(self.executor._threads)
            except BaseException:
                self.maintenance.store.finish(activity, self.maintenance.identity)
                raise
            launched.append(spec.name)
        return launched

    def status(self) -> dict[str, Any]:
        schedule = load_schedule()
        result = []
        now = datetime.now(BJ_TZ)
        for spec in current_jobs():
            last = self._last_at(spec.name)
            schedule_text = f"每 {spec.interval_seconds // 60} 分钟 (错峰 +{spec.offset_seconds // 60}m)" if (spec.interval_seconds and spec.offset_seconds) else (f"每 {spec.interval_seconds // 60} 分钟" if spec.interval_seconds else "、".join(self._scheduled_times(spec, schedule)))
            result.append({
                "name": spec.name,
                "script": spec.script,
                "running": spec.name in self.running and not self.running[spec.name].done(),
                "last_scheduled_at": last.isoformat() if last else "",
                "schedule": schedule_text,
                "timezone": "Asia/Shanghai",
                "overdue": bool(spec.interval_seconds and last and (now - last).total_seconds() > spec.interval_seconds * 2),
                "offset_seconds": spec.offset_seconds,
            })
        return {"jobs": result, "recent_runs": self.store.job_runs(30)}

    def shutdown(self, heartbeat=None) -> None:
        self.maintenance.register_threads([threading.current_thread()])
        while any(not future.done() for future in self.running.values()):
            if heartbeat is not None:
                heartbeat()
            time.sleep(.2)
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.maintenance.wait_settled(heartbeat)
