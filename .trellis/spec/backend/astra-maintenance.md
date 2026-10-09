# DEMO maintenance contract (local implementation, not deployed)

## Authorization boundary

Implemented on a separate branch from fixed PR5. No production change, published
artifact or successful Linux/container/field acceptance is implied. The current
legacy deployment and legacy rollback are explicitly unsupported for maintenance.
The original prepared PR5 operation/helper/pin remain immutable.

## Runtime

Never-enabled ordinary operation remains the legacy behavior. Enabling is a cold
startup boundary before side-effectful imports/queues/spawns. A durable enable
latch and current identity metadata prevent deleted/missing/corrupt state from
falling back to compatibility. A process that has done unregistered ordinary work
cannot hot-enroll or acknowledge a zero-work barrier.

The pure SQLite reducer is in maintenance.py; adapters are separate. Admission
and fencing serialize in BEGIN IMMEDIATE. Queued futures/children/writers remain
active until actual finish/reap. Unknown crashed work and pre-send records remain.
Unknown business threads (including limbo) block ACK; names cannot whitelist them.
ASGI admission includes streaming and background response writers; abandoned
AnyIO work keeps a durable child scope and its parent until actual completion.
Unowned QQ/web-sync/market-stream/legacy-scheduler resident writers refuse maintenance, never get
silently killed. Linux process and thread evidence must be established separately.

NORMAL -> DRAINING -> PAUSED -> STOPPING -> SWITCHING -> VERIFYING -> NORMAL.
Cancellation/expiry/disconnect/restart stay CANCELLED/HOLD; no automatic resume.
STOPPING is a current-binding natural-exit request, not TERM/KILL. The controller
independently proves whole owned container exit before supplying ended instances.
Health/auth/persistence/singleton validation happens with admission still closed.
Explicit resume is the final opening action, after all fallible supervision
restoration, exact instance/source/image checks and fresh risk proof.

## Risk and orders

Only verified DEMO and six explicitly zero counts (positions, pending, algo,
partial, inflight, unknown) can pause/resume. Missing/future/stale proof is refusal.
Dedicated broker evidence uses GET only with explicit successful envelopes; no
existing orphan-cancelling reconcile, automatic cancel or close is used.

Journal PREPARED is durable before send; stable logical request identity is not
regenerated on retry. A newly sending mutation requires fresh admission even if
its computation was admitted before the fence. Unknown/mixed/partial outcomes
retain work/reservations and cannot resend. Clearly unsent/refused and entirely
rejected results are distinguished. Legacy acknowledged intent files are not
upgraded into fabricated pre-send evidence. Normal disabled behavior is preserved.

## Deployment and recovery

Schema1 remains read-only parse compatibility, not executable authorization; new
execute/rollback reject it before stop/rename. Executable schema2 normal mode
retains real 900-second scheduling and >=480 remaining plus the pause barrier.
Maintenance mode replaces only the time window; every source/config/auth/
ownership/state gate remains. Schema2 is an explicit bounded
maintenance extension with protocol/identity/account hashes and exact instances.
Both raw source and immutable image provenance must support reviewed protocol1.
Different protocol persistence bytes require independent compatibility review.

Packaged controller and protocol equal raw source bytes at the reviewed commit.
New helpers/checks require new hosted CI/provenance/OP/PIN; never patch a prepared
helper, repin changed approval or synthesize fake time. Maintenance drain failures
do not enter ordinary docker stop -t60/rollback. Only natural owned exits permit
snapshot/rename. Startup overlays contain only non-secret maintenance identity,
not .env/policy changes. Latest writable data and intentional deletions are carried
into protocol-capable recovery. An expired/unproven recovery remains held for
separate approval; it cannot silently mint a new budget. Missed cycles are skipped,
not replayed.

## Verification limits

Focused standalone runner uses temporary data, fake clocks/broker/commands, and
only audited SQLite subprocesses. It must not import tests/__init__ or load real
dotenv/credentials. Missing, failing, skipped and expected-failed required suites
fail CI. Windows AST/flock/AnyIO mocks are not Linux lock/container evidence.
All required old regressions remain, plus dedicated maintenance checks. No Actions
dispatch, Docker build or publication is authorized by local test results.
