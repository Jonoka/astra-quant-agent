"""Read a bounded, allowlisted summary of completed Collection log records."""
from __future__ import annotations

import json
import math

# Keep the reader in sync with the writer rather than copying its vocabularies.
from astra_backend.collection import _ENDPOINTS, _PHASES, _safe_result

_MAX_STREAM_UNITS = 64 * 1024  # bytes before decoding, or characters for text
_MAX_LINES = 256
_MAX_LINE_UNITS = 4096
_MAX_SUMMARY_UNITS = 1800  # leaves room for the scheduler's existing timeout prefix
_HOSTS = frozenset({"openapi.okx.com", "www.okx.com", "us.okx.com", "eea.okx.com",
                    "configured_host"})


def _number(value):
    # bool is an int subclass; unbounded JSON integers can overflow isfinite.
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except (OverflowError, ValueError):
        return False


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate diagnostic key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite diagnostic number")


def _records(raw, job_run_id, scheduled_at):
    """Return sanitized records and whether bounded input was discarded."""
    if type(raw) not in (bytes, str):
        return [], False
    limited = len(raw) > _MAX_STREAM_UNITS
    tail = raw[-_MAX_STREAM_UNITS:]
    if limited:
        # Never interpret a line cut in half by the tail bound as a new record.
        tail = tail.partition(b"\n" if isinstance(tail, bytes) else "\n")[2]
    if isinstance(tail, bytes):
        tail = tail.decode("utf-8", errors="replace")
    lines = tail.splitlines()
    limited = limited or len(lines) > _MAX_LINES
    records = []
    for line in lines[-_MAX_LINES:]:
        if len(line) > _MAX_LINE_UNITS:
            limited = True
            continue
        if not line.startswith("[Collection] "):
            continue
        try:
            record = json.loads(line[len("[Collection] "):],
                                object_pairs_hook=_unique_object,
                                parse_constant=_invalid_constant)
            if not isinstance(record, dict):
                continue
            if (type(record.get("job_run_id")) is not int
                    or record["job_run_id"] != job_run_id
                    or record.get("scheduled_at") != scheduled_at):
                continue
            kind, phase, result = (record.get(key) for key in ("kind", "phase", "result"))
            if (type(kind) is not str or kind not in ("phase", "endpoint")
                    or type(phase) is not str
                    or phase not in (_PHASES | {"other", ""} if kind == "endpoint" else _PHASES | {"other"})
                    or type(result) is not str or _safe_result(result) != result
                    or not _number(record.get("elapsed_ms"))):
                continue
            remaining = record.get("remaining_ms")
            if remaining is not None and not _number(remaining):
                continue
            safe = {"kind": kind, "phase": phase, "result": result,
                    "elapsed_ms": round(record["elapsed_ms"], 1)}
            if remaining is not None:
                safe["remaining_ms"] = round(remaining, 1)
            if kind == "endpoint":
                endpoint, host, method = (record.get(key) for key in ("endpoint", "host", "method"))
                if (type(endpoint) is not str or endpoint not in _ENDPOINTS | {"other"}
                        or type(host) is not str or host not in _HOSTS
                        or type(method) is not str or method not in ("GET", "POST", "OTHER")):
                    continue
                safe.update(endpoint=endpoint, host=host, method=method)
            records.append(safe)
        except (ValueError, TypeError, OverflowError, RecursionError):
            continue
    return records, limited


def collection_timeout_summary(stdout, stderr, job_run_id, scheduled_at):
    """Return at most 1800 safe characters, or nothing if diagnostics fail.

    Phases preserve each stream's order, never an invented stdout/stderr order.
    Endpoint counts/timings cover completed records in the retained sample, not
    active requests or the whole run. A bounded tail may omit earlier failures.
    """
    try:
        if type(job_run_id) is not int or job_run_id <= 0 or type(scheduled_at) is not str:
            return ""
        summary = {"scope": "captured_output"}
        endpoints = {}
        for stream, raw in (("stdout", stdout), ("stderr", stderr)):
            records, limited = _records(raw, job_run_id, scheduled_at)
            if limited:
                summary["scope"] = "bounded_tail"
            phases = {}
            for record in records:
                if record["kind"] == "phase":
                    phase = {key: value for key, value in record.items() if key != "kind"}
                    if record["result"] == "ok":
                        phases["last_successful_phase"] = phase
                    else:
                        phases.setdefault("first_failed_phase", phase)
                    continue
                key = (record["phase"], record["endpoint"], record["host"], record["method"])
                endpoint = endpoints.setdefault(key, {
                    "phase": key[0], "endpoint": key[1], "host": key[2], "method": key[3],
                    "count": 0, "max_ms": 0, "error_count": 0, "top_errors": {},
                })
                endpoint["count"] += 1
                # Max avoids overflowing a sum of individually finite timings.
                endpoint["max_ms"] = max(endpoint["max_ms"], record["elapsed_ms"])
                if record["result"] != "ok":
                    endpoint["error_count"] += 1
                    errors = endpoint["top_errors"]
                    errors[record["result"]] = errors.get(record["result"], 0) + 1
            if phases:
                summary[stream] = phases
        if endpoints:
            top = sorted(endpoints.values(), key=lambda item: (
                -item["max_ms"], item["phase"], item["endpoint"], item["host"], item["method"]))[:3]
            for endpoint in top:
                endpoint["top_errors"] = dict(sorted(endpoint["top_errors"].items(),
                                                    key=lambda item: (-item[1], item[0]))[:3])
            summary["top_completed_endpoints"] = top
        if len(summary) == 1:
            return ""
        while True:
            text = json.dumps(summary, separators=(",", ":"), allow_nan=False)
            if len(text) <= _MAX_SUMMARY_UNITS:
                return text
            # Retain phase outcomes first. Never truncate serialized JSON.
            if summary.get("top_completed_endpoints"):
                summary["top_completed_endpoints"].pop()
            else:
                return ""
    except Exception:
        return ""  # Observability must not mask the original process timeout.
