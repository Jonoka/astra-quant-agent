"""Shared monotonic deadlines and non-secret execution context.

Nested operations may shorten, never renew, the caller's remaining budget.
Executor callers must copy their context into workers explicitly.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import math
import os
import time

BRAIN_PERSISTENCE_SECONDS = 60.0
MIN_MODEL_SECONDS = 120.0
COLLECTION_MAX_SECONDS = 180.0


class DeadlineExceeded(TimeoutError):
    """The shared operation budget has expired."""


_deadline: ContextVar[float | None] = ContextVar("astra_deadline", default=None)
_caller: ContextVar[str] = ContextVar("astra_request_caller", default="")


def current_deadline() -> float | None:
    return _deadline.get()


def remaining() -> float:
    deadline = current_deadline()
    return math.inf if deadline is None else max(0.0, deadline - time.monotonic())


def check_deadline(reserve: float = 0.0) -> float:
    left = remaining() - max(0.0, reserve)
    if left <= 0:
        raise DeadlineExceeded("shared execution deadline exhausted")
    return left


@contextmanager
def deadline_scope(timeout: float | None = None, *, deadline: float | None = None):
    parent = current_deadline()
    limits = [v for v in (parent, deadline) if v is not None]
    if timeout is not None:
        limits.append(time.monotonic() + max(0.0, float(timeout)))
    token = _deadline.set(min(limits) if limits else None)
    try:
        check_deadline()
        yield current_deadline()
    finally:
        _deadline.reset(token)


@contextmanager
def inference_scope():
    """Translate the scheduler's wall-clock boundary once per brain cycle."""
    raw = os.environ.get("ASTRA_INFERENCE_DEADLINE_EPOCH", "")
    deadline = None
    if raw:
        try:
            epoch = float(raw)
            if not math.isfinite(epoch):
                raise ValueError
            deadline = time.monotonic() + max(0.0, epoch - time.time())
        except ValueError:
            raise DeadlineExceeded("invalid scheduled inference deadline") from None
    # Manual brain invocations are bounded as well. Scheduled calls are further
    # shortened by the original slot boundary, including queue/collection time.
    with deadline_scope(timeout=810.0, deadline=deadline):
        yield current_deadline()


def sleep_with_deadline(seconds: float) -> None:
    seconds = max(0.0, seconds)
    left = check_deadline()
    if seconds >= left:
        raise DeadlineExceeded("retry backoff exceeds remaining execution budget")
    time.sleep(seconds)
    check_deadline()


@contextmanager
def request_scope(caller: str):
    token = _caller.set(str(caller)[:80])
    try:
        yield
    finally:
        _caller.reset(token)


def beijing_now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds")


def cycle_metadata() -> dict:
    """Only explicitly admitted non-secret scheduler fields are read."""
    raw_id = os.environ.get("ASTRA_JOB_RUN_ID", "")
    return {
        "job_run_id": int(raw_id) if raw_id.isdigit() else None,
        "scheduled_at": os.environ.get("ASTRA_SCHEDULED_AT", "")[:40],
        "caller": _caller.get(),
    }
