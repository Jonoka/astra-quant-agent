# Design

Baseline: remote codex/deploy and live source 90f9f3a558bdbea0171b19a42c58e2fae7ed8e9d.
Original D:/vps checkouts are read-only. All changes use this isolated clone.

A shared astra_backend.deadline module owns monotonic DeadlineExceeded,
deadline_scope(timeout=None, deadline=None), current_deadline(), remaining(),
check_deadline(), sleep_with_deadline(), and request_scope(caller). Nested scopes
can only shorten a parent budget; executor workers run copy_context().run.
cycle_metadata() reads only ASTRA_JOB_RUN_ID and ASTRA_SCHEDULED_AT.
inference_scope() converts ASTRA_INFERENCE_DEADLINE_EPOCH to monotonic once.

Scheduler keeps the 900-second trigger interval and singleton running map.
Trader process deadline is next slot minus 30 seconds; inference deadline is
next slot minus 90 seconds. Queue time counts. Existing 1260-second timeout is
an outer historical ceiling, never a fresh allowance. Started job ID and trigger
time are passed to the subprocess through selected environment additions.
Skipped trader slots are recorded once as skipped job_runs with a stable reason;
no automatic backfill, concurrent trader, strategy or risk change.

LLM execute scopes its entire request/fallback chain, not each attempt. HTTP
connect/read and retry sleeps use remaining time; body reads recalculate socket
timeouts. Council members and their optional retry share a seat deadline; council
executor waits are bounded, do not wait past deadline on shutdown, and cannot
start CIO with insufficient time. Inference deadline leaves persistence margin.
Brain collection is bounded and context is copied into collection workers;
unfinished results fail closed instead of accepting partial fresh data.
Collection is capped at 180 seconds and reserves 120 model + 60 persistence
seconds. Model/council/fallback parsing has a nested deadline reserving the last
60 brain seconds for persistence. The original misplaced single_brain_cycle
decorator is moved from the margin helper to the entire brain function, and lock
contention records skipped cycle health.

New model_requests SQLite table stores only safe per-attempt metadata; old
model_calls remain aggregated. record_model_request is best-effort through
astra_gateway.telemetry, with no direct auth/config reads. Successful usage
adds _astra_trace={model,request_id,client_request_id,started_at,completed_at};
dispatch uses it for actual-model history and aggregate telemetry. Missing server
IDs remain missing, paired with a generated local attempt ID; never fabricate a
New API ID. History gets additive execution metadata, keeps old time semantics.
All terminal jobs close remaining running attempts; a successful job with lost
completion telemetry is unknown/TelemetryIncomplete, not invented success.
Skip deduplication permits out-of-order slot completion. The existing authenticated
admin/agents route exposes a bounded safe per-attempt list.

No image, CI release pin, production configuration, authentication or deployment
changes. Hosted build/source allowlist adaptation remains a separate reviewed
deployment prerequisite after owner approval to push/build.
