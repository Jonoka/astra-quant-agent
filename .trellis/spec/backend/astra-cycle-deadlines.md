# Scheduled brain deadlines and request correlation

## 1. Scope / trigger

A trader scheduled every 900 seconds must release its worker before the next
slot. Previously council seat retries renewed per-attempt timeouts and the
1260-second subprocess ceiling overlapped subsequent slots. This contract changes
execution budgets and observability, with no strategy, risk, credential, prompt,
trigger interval or automatic backfill change.

## 2. Signatures

- `deadline_scope(timeout=None, *, deadline=None)` inherits the minimum parent,
  explicit monotonic deadline and relative timeout; reset occurs on every exit.
- `inference_scope()` bounds an entire brain invocation to 810 seconds and any
  earlier scheduled boundary; `check_deadline(reserve=0)` raises
  `DeadlineExceeded`; `sleep_with_deadline(seconds)` cannot renew the allowance.
- `GatewayScheduler._execute(spec, scheduled_at=None)` deducts queue and SQLite
  admission time immediately before `subprocess.run`.
- `GatewayStore.begin_job(name, scheduled_at='')` remains backwards compatible.
  `record_skipped_job(name, scheduled_at, reason)` deduplicates that exact slot.
- Additive SQLite schema: `job_runs.scheduled_at TEXT NOT NULL DEFAULT ''` and
  `model_requests` with unique `client_request_id`, nullable `job_run_id`,
  `scheduled_at`, `caller`, `request_id`, `model`, `status`, `started_at`,
  `completed_at`, `duration_ms`, nullable `http_status`, `error_type`.
- `GET /api/v1/admin/agents` adds bounded `model_requests` (100 rows), preserving
  the existing administrator check and aggregated `model_calls` contract.

## 3. Contracts

For slot start S, the inference/brain boundary is S+810 seconds; the subprocess
hard boundary is S+870 seconds. The remaining 30 seconds protects the next slot.
The configured subprocess timeout is only an outer ceiling, never a renewed
1260-second allowance. Brain collection has a 180-second cap and reserves at least
120 seconds for models plus 60 seconds for persistence. The entire council,
fallback and parsing stage further reserves the final 60 seconds of the brain
budget; returning from that stage restores the parent budget for persistence.
All model retries, adaptive 400 resends and fallback candidates share the same
logical deadline. Executor workers inherit copied context; overdue executor
shutdown does not join workers past the deadline.

Scheduler child environment additions are `ASTRA_JOB_RUN_ID` (positive integer),
`ASTRA_SCHEDULED_AT` (ISO time with +08:00), and
`ASTRA_INFERENCE_DEADLINE_EPOCH` (finite Unix epoch, converted once to monotonic).
Non-trader subprocesses do not inherit the inference boundary.

Each actual HTTP attempt records `running` before sending and updates the same
locally generated UUID after completion. Server identity is accepted only from
request-ID headers; an absent server ID stays blank. Successful usage gains
`_astra_trace={model,request_id,client_request_id,started_at,completed_at}`.
It supplies actual model identity to aggregate telemetry and history.
HTTP success does not imply a valid trading decision or a persisted brain result.

History retains its old `time`/`time_str` meaning and gains `execution`: job ID,
scheduled time, actual model, server/local request IDs, request start/completion,
and `completed_at` when the history record is assembled for writing. This final
field is not a database commit acknowledgment. Existing history content is not
rewritten; new telemetry admits no prompt, response body, URL or credential.
Provider usage keys exclude internal `_astra_` fields; unknown council tokens
remain unknown.

## 4. Validation and error matrix

| Condition | Observable outcome |
| --- | --- |
| Previous trader active at new slot | `skipped`, `previous_run_active`; no overlap |
| Window missed or scheduler recovers | `skipped`, `scheduler_window_missed`; metadata only, no replay; at most 96 historical slots recovered |
| Queue/admission consumes inference boundary | `skipped`, `budget_exhausted_before_start`; no subprocess |
| Brain file lock occupied | cycle health `skipped`, `inference_lock_active`; no new inference/history |
| Invalid/expired deadline or late result | fail closed; no fresh success/history from that result |
| HTTP failure/timeout/cancel | separate safe attempt status, HTTP code and error category |
| Process fails or is killed | remaining running attempts become `cancelled`, `JobTerminated` |
| Worker recovers interrupted job | remaining running attempts become `cancelled`, `WorkerInterrupted` |
| Successful job with lost completion telemetry | remaining running attempts become `unknown`, `TelemetryIncomplete`, not fake success/cancellation |
| Telemetry cannot write | best effort; does not trigger a duplicate inference |

## 5. Good / base / bad cases

- Good: a 12:00 job queued until 12:03 has 690 seconds of process allowance,
  ending at 12:14:30, and inherits the original 12:13:30 brain boundary.
- Base: normal council success preserves decision output, fresh cache return,
  existing policy data and token-usage uncertainty; metadata is additive.
- Bad: a queued 12:00 admission at 12:13:50 cannot run with a fresh 1260 seconds.
  Its delayed skip record must remain visible even after a 12:15 skip was recorded.

## 6. Required tests

Mock clocks, HTTP responses, processes and temporary SQLite/files only. Assert
that 524/timeout/cancel and retry/adaptive/fallback sequences do not renew the
budget; late bodies cannot return success; all stages inherit deadlines; collection
and persistence reserves remain available; singleton reentry is refused; storage
delay is deducted; exact slot skips are durable and idempotent; terminal jobs close
pending attempts; old SQLite writers remain compatible; actual model and server
ID reach history/admin output; malformed/secret fields cannot reach new telemetry.

## 7. Wrong versus correct

Wrong: every seat retry calls HTTP with `timeout=420`; executor `with` then joins
late workers; fallback receives another full timeout; UI time is treated as the
New API completion time.

Correct: nested scopes use `min(parent, now + stage_timeout)`, worker contexts are
copied, sleeps/reads deduct remaining time, and model completion is linked through
actual server/local IDs and explicit start/completion timestamps.

## Runtime verification limits

Windows mock tests use a `fcntl` import/lock substitute, not a real Linux flock.
Socket-body reads shrink their timeout, but DNS, trickled response headers and
chunk framing can still block inside urllib; the subprocess hard boundary is the
final guard for those cases. A hosted Linux build/lock check and controlled mock
upstream canary remain deployment prerequisites. Production must not be changed
or probed by workstation tests.
