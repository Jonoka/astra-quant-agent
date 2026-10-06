# Validation and handoff

Baseline source: `90f9f3a558bdbea0171b19a42c58e2fae7ed8e9d`.
Branch: `codex/astra-cycle-deadline` in the isolated workstation clone.
The original checkout, production containers/configuration, trading parameters,
authentication and deployment remain untouched. No push, paid inference, restart,
real order or backfill was performed.

## Completed behavior

- Absolute slot boundary includes executor queueing and SQLite admission wait.
  Trader hard stop is next slot minus 30 seconds; brain stop minus 90 seconds.
- Collection cap 180 seconds, preserving 120 model + 60 persistence seconds;
  council/seat/retry/400 adaptive resend/fallback/parser deadlines cannot renew.
- Expired short fallback candidates may advance while the parent still has time;
  exhausted parent/backoff budgets cannot start a fresh request.
- Brain singleton covers the complete cycle instead of the margin helper.
  Busy locks and skipped slots have explicit safe reasons; no stale work replay.
- Per-attempt safe metadata links job/trigger/actual model/server and local IDs/
  request start/completion/status. History metadata and authenticated admin output
  are additive; aggregate tokens remain unknown when the provider did not report.
- Pending attempts are closed on process failure/recovery. A successful job with
  incomplete telemetry is marked unknown, not invented success or cancellation.

## Final checks

523 isolated mock/regression tests passed in one combined run (45.451 seconds):
0 failures, 0 errors, 0 skipped. This includes all 62 new regression tests.
21 changed Python files compiled; `git diff --check` passed.

Final evidence is `../final-mock-validation.txt` relative to the repository;
the workstation runner is `../mock_test_runner.py` and is intentionally outside
the application source. It clears inherited provider credentials, forbids real
HTTP/external socket connections, uses temporary configuration/database paths,
and explicitly labels the Windows `fcntl` substitution. Only the stdlib's exact
Windows socketpair caller may create an internal loopback pipe. GC before temporary
directory cleanup accommodates old SQLite fixtures that leave unreachable handles.

Combined suites:

```text
tests.ops.test_cycle_deadline
tests.ops.test_gateway_scheduler
tests.ops.test_gateway_cache_stats
tests.ops.test_job_runs_retention
tests.core.test_router_system
tests.llm.test_llm_resilience
tests.llm.test_llm_deadline
tests.llm.test_llm_call_tails
tests.llm.test_llm_transport_tails
tests.core.test_council_deadline
tests.ops.test_brain_deadline
tests.ops.test_brain_dispatch
tests.core.test_debate_council_verdict
tests.core.test_council_debate_mode
tests.core.test_debate_council_preflight
tests.core.test_council_manager
tests.llm.test_council_manager
tests.llm.test_leverage_range_and_council
tests.core.test_council_attribution
tests.extraction.test_brain_dispatch_extraction
tests.trading.test_cycle_brain_stage
```

Run from the workspace parent with `.\astra-deadline\.venv\Scripts\python.exe
-X utf8 .\mock_test_runner.py` followed by these module names. The dependencies
installed solely into this disposable venv were FastAPI, httpx support,
python-multipart and Jinja2 dependencies; application requirements were not edited.

Cross-review repaired admission-delay timeout renewal, lost older skipped slots,
pending telemetry left on successful jobs, missing persistence reserve, and short
candidate expiration prematurely ending the parent chain. Legacy tests now use
monotonic fake clocks, actual HTTP response objects, a temporary config path and
the platform-common directory-replacement error contract. The formerly failing
combined cross-examination regression passes in the final combined run.

## Remaining deployment checks

This is code/mocks completion, not production acceptance. Windows cannot establish
real Linux flock, exact production dependency/image compatibility or live provider
header behavior. urllib DNS/response-header/chunk framing can outlive an idle socket
timeout; the process hard boundary protects the following slot, but must be checked
with a controlled mock upstream on Linux. Hosted build, source allowlist/schema
compatibility checks and a controlled canary are still required after separate
authorization to push/build/deploy. Missing server request IDs remain explicitly
missing; matching those cases to New API billing logs cannot be claimed as verified.

## Independent PR #2 review and fixes (2026-10-06)

Started from verified current local/remote f247950 and base 90f9f3a; PR #2 was
OPEN/Draft with base codex/deploy, no reviews/checks. Two independent reviewers
examined budget/transport/persistence and migration/release, then cross-reviewed
fixes. The original checkout and production remained untouched.

- P1 fixed: lock waiting could exhaust the budget then publish cache/position
  instructions before detecting failure. A synthetic interleaving reproduced
  actual writes. Locks now use bounded deadline polling and checks after acquire;
  atomic replace rechecks after fsync. Timeout preserves previous file content.
- P1 fixed: simultaneous old-WAL-DB initializers could both detect a missing
  scheduled_at column and one raise duplicate-column OperationalError. Write
  transactions now serialize detection/ALTER; an independent negative control
  reproduces the failure without the fix, and ten fixed runs succeeded.
- P2 fixed: a hard-killed slow body/chunk reader could lose an already known
  upstream request ID. Success/error headers now upsert identity before body
  reading; actual kernel process termination retains the known ID/status.
- P2 fixed: scheduler-closed requests used naive completion times while HTTP
  completions had offsets. All new request terminal timestamps use explicit +08:00;
  existing job timestamps retain their original contract.
- Successful health cannot bypass expiry, including delayed telemetry. Failure/
  skip health auditing still persists after timeout, without renewing the budget.

632 distinct Linux Python 3.11.17 tests passed: zero failures/errors/skips.
Breakdown: 569 full regressions (the retained 523, 17 new review cases, and 29
existing shared-lock/atomic/health compatibility cases); 37 runtime helper cases,
10 source gates, four real previous-storage upgrade/latest-state rollback cases,
seven real singleton locks, five controlled loopback HTTP cases. Source guards
require 79 named cases and the exact 64-path application delta from upstream,
retaining council/OKX source and provenance pins/labels. Workflow YAML, embedded
Python parsing and diff whitespace checks passed.

These tests ran the frozen current worktree source in the existing local Linux
image as a dependency environment, with Docker network none/read-only and only
synthetic temporary data. They do not attest a new candidate image or current-head
Compose. The previous f247950 candidate image/Compose evidence remains historical.
The source snapshot SHA256 was
e4fbc8bd3504d1a803793cd8c030063af08a865cf17abf0ace643cb6accb2e4b;
all exported source bytes were rechecked unchanged after execution. Evidence in the
workspace outside the repo: review-linux-full.log, review-linux-http.log,
review-linux-acceptance/source-hashes.json; independent reviewers retained their
migration negative-control and lock/header review scripts there as well.

Initial workstation Python lacked fastapi, an external runner selected the wheel
with wrong case, and the existing Docker environment stopped during setup; none
constituted a passing runtime test. Final acceptance above ran successfully after
using the offline wheel and normally starting the already-authorized Desktop.
No host installation or security/network/virtualization setting was changed.

No unresolved blocking code defect was found. Low-priority model_requests retention
and historical references after job pruning remain unchanged for a separate
capacity policy review. Hosted GitHub gates, a new current-head candidate/published
immutable image and Compose, Windows host forwarding and authenticated production
round/New API acceptance remain unexecuted. Code is ready for owner-approved merge
and formal image validation; local source tests alone do not authorize deployment.
