"""Read-only registry of ASTRA managed agents and workers."""
from __future__ import annotations
import time
from pathlib import Path
from typing import Any

from astra_gateway.store import job_run_outcome

ROOT = Path(__file__).resolve().parents[1]

AGENTS = (
    {"id": "trading_brain", "name": "交易主脑", "role": "全市场决策与持仓裁决", "job": "trader", "output": "ai_brain_decisions.json", "prompt_transport": "python-direct"},
    {"id": "self_improvement", "name": "自进化 Agent", "role": "平仓归因与长期记忆更新", "job": "self_improvement", "output": "self_improvement_report.json", "prompt_transport": "python-direct"},
    {"id": "factor_engine", "name": "因子引擎", "role": "多因子与微积分状态生成", "job": "factor_library", "output": "factor_library_snapshot.json", "prompt_transport": "none"},
    {"id": "news_engine", "name": "新闻情绪引擎", "role": "宏观新闻与事件风险", "job": "news", "output": "news_sentiment.json", "prompt_transport": "none"},
    {"id": "backup_agent", "name": "灾备 Agent", "role": "异地备份与本地清理", "job": "nightly_backup", "output": "", "prompt_transport": "none"},
)


def agent_statuses(job_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_job: dict[str, dict[str, Any]] = {}
    for run in job_runs:
        latest_by_job.setdefault(str(run.get("job_name")), job_run_outcome(run))
    result = []
    for agent in AGENTS:
        output = ROOT / "data" / agent["output"] if agent["output"] else None
        age = max(0, int(time.time() - output.stat().st_mtime)) if output and output.exists() else None
        run = latest_by_job.get(agent["job"], {})
        health = "healthy"
        if agent["output"] and age is None:
            health = "cold"
        if run.get("status") in {"failed", "interrupted"}:
            health = "degraded"
        elif agent["job"] == "trader" and run:
            if run.get("status") == "running":
                health = "running"
            elif run.get("brain_status") != "ok":
                health = "unknown" if run.get("brain_status") == "unknown" else "idle"
        elif agent["job"] == "trader" and age is not None:
            health = "unknown"
        result.append({**agent, "health": health, "output_age_seconds": age,
                       "last_run_status": run.get("status", "not-run"),
                       "last_run_at": run.get("started_at", ""),
                       "last_job_run_id": run.get("id"),
                       "scheduled_at": run.get("scheduled_at", ""),
                       "process_status": run.get("process_status", "not-run"),
                       "brain_status": run.get("brain_status", "unknown" if agent["job"] == "trader" else "not_applicable"),
                       "brain_result_at": run.get("brain_result_at", ""),
                       "brain_detail": run.get("brain_detail", "")})
    return result
