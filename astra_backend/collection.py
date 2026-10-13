"""Bounded, non-secret collection diagnostics shared by copied workers."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json
import math
import threading
import time
from urllib.parse import urlsplit

from astra_backend.deadline import check_deadline, cycle_metadata, remaining

_ENDPOINTS = frozenset({
    "/api/v5/market/ticker", "/api/v5/market/tickers", "/api/v5/market/books",
    "/api/v5/market/candles", "/api/v5/market/index-tickers",
    "/api/v5/aigc/mcp/indicators", "/api/v5/public/funding-rate",
    "/api/v5/public/open-interest", "/api/v5/public/instruments",
    "/api/v5/public/interest-rate-loan-quota", "/api/v5/public/liquidation-orders",
    "/api/v5/public/opt-summary", "/api/v5/rubik/stat/taker-volume",
    "/api/v5/rubik/stat/contracts/long-short-pos-ratio",
    "/api/v5/rubik/stat/contracts/long-short-account-ratio",
    "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader",
    "/api/v5/rubik/stat/contracts/long-short-position-ratio-contract-top-trader",
    "/api/v5/rubik/stat/contracts/open-interest-history",
    "/api/v5/rubik/stat/option/open-interest-volume-ratio",
    "/api/v5/account/balance", "/api/v5/account/positions",
    "/api/v5/account/positions-history", "/api/v5/account/bills",
    "/api/v5/account/set-leverage", "/api/v5/trade/order",
    "/api/v5/trade/order-algo", "/api/v5/trade/amend-algos",
    "/api/v5/trade/amend-order", "/api/v5/trade/cancel-algos",
    "/api/v5/trade/cancel-order", "/api/v5/trade/close-position",
    "/api/v5/trade/fills", "/api/v5/trade/orders-pending",
    "/api/v5/trade/orders-algo-pending", "/api/v5/trade/orders-history",
})

_PHASES = frozenset({
    "collection", "market_packages", "market_validation", "factor_refresh",
    "factor_smart_money", "factor_computation", "factor_publish", "smart_money",
    "pending_orders", "factor_wait", "factor_validation", "prompt_assembly",
    "prompt_publish",
})
_RESULTS = frozenset({
    "ok", "error", "code_error", "invalid_data", "DeadlineExceeded", "TimeoutError",
    "ConnectionError", "HTTPError", "URLError", "OSError", "ValueError", "TypeError",
    "RuntimeError", "JSONDecodeError", "KeyError", "CancelledError", "SSLError",
    "ReadTimeout", "ConnectTimeout", "Timeout", "PermissionError", "BrokenPipeError",
    "ConnectionResetError", "ConnectionRefusedError", "KeyboardInterrupt", "SystemExit",
})


def _safe_result(value):
    if value in _RESULTS:
        return value
    if (isinstance(value, str) and len(value) == 8 and value.startswith("http_")
            and value[5:].isascii() and value[5:].isdigit()
            and 100 <= int(value[5:]) <= 599):
        return value
    return "error"


def _metadata():
    metadata = cycle_metadata()
    scheduled_at = metadata["scheduled_at"]
    try:
        datetime.fromisoformat(scheduled_at)
    except (ValueError, TypeError):
        scheduled_at = ""
    # Caller labels can be externally chosen; they are not needed for collection.
    return {"job_run_id": metadata["job_run_id"], "scheduled_at": scheduled_at}


_phase = ContextVar("astra_collection_phase", default="")
_trace = ContextVar("astra_collection_trace", default=None)


def _budget_ms():
    value = remaining()
    return round(value * 1000, 1) if math.isfinite(value) else None


def _emit(record):
    # No exception messages, URLs/queries, headers, request or response bodies.
    try:
        print("[Collection] " + json.dumps(record, sort_keys=True), flush=True)
    except (OSError, ValueError):
        pass  # Diagnostics must not replace the original collection outcome.


@contextmanager
def collection_trace():
    trace = {"phases": [], "failed_phase": "", "lock": threading.Lock()}
    token = _trace.set(trace)
    try:
        yield trace
    finally:
        _trace.reset(token)


def collection_diagnostics():
    trace = _trace.get()
    if trace is None:
        return {}
    with trace["lock"]:
        return {"collection_phase": trace["failed_phase"] or _phase.get(),
                "collection_timings": [dict(item) for item in trace["phases"]]}


@contextmanager
def collection_phase(name):
    """Fixed caller-chosen phase names; elapsed time includes blocked waits."""
    name = name if isinstance(name, str) and name in _PHASES else "other"
    token = _phase.set(name)
    started = time.monotonic()
    result = "ok"
    try:
        check_deadline()
        yield
        check_deadline()
    except BaseException as exc:
        result = _safe_result(type(exc).__name__)
        raise
    finally:
        record = {"phase": name, "elapsed_ms": round(max(0, time.monotonic() - started) * 1000, 1),
                  "remaining_ms": _budget_ms(), "result": result}
        trace = _trace.get()
        if trace is not None:
            with trace["lock"]:
                if len(trace["phases"]) < 64:
                    trace["phases"].append(record)
                if result != "ok":
                    trace["failed_phase"] = trace["failed_phase"] or name
        _emit({"kind": "phase", **_metadata(), **record})
        _phase.reset(token)


@contextmanager
def endpoint_timing(method, path, host):
    """Only a fixed endpoint and admitted OKX host reach the diagnostic log."""
    try:
        endpoint = urlsplit(str(path)).path
    except ValueError:
        endpoint = "other"
    if endpoint not in _ENDPOINTS:
        endpoint = "other"
    try:
        clean_host = urlsplit(str(host)).hostname or str(host)
    except ValueError:
        clean_host = "configured_host"
    if clean_host not in ("openapi.okx.com", "www.okx.com", "us.okx.com", "eea.okx.com"):
        clean_host = "configured_host"
    method = str(method).upper()
    if method not in ("GET", "POST"):
        method = "OTHER"
    trace = {"result": "ok"}
    started = time.monotonic()
    try:
        yield trace
    except BaseException as exc:
        trace["result"] = type(exc).__name__
        raise
    finally:
        result = _safe_result(str(trace["result"]))
        _emit({"kind": "endpoint", **_metadata(), "phase": _phase.get(),
               "method": method, "endpoint": endpoint, "host": clean_host,
               "result": result, "elapsed_ms": round(max(0, time.monotonic() - started) * 1000, 1),
               "remaining_ms": _budget_ms()})
