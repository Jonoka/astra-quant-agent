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
