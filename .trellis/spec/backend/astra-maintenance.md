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

Paused replacement launch is a separate durable supervisory scope, not ordinary
restart admission. The startup overlay binds the exact operation/generation;
only a new approved watchdog with proven old actors offline can launch its absent
app. The actual app must register itself and finish its accounted startup child
before the launch scope settles. Duplicate/live/crashed/old/stale/expired launches
stay refused; no kill or age-based pruning. Windows controlled child tests do not
prove the real Bash/proc/cgroup/namespace/lock chain.

### Startup completion: absence is not evidence

Registration and completion are distinct. A registered actor with zero child
activity may not yet have imported Uvicorn/app or begun verification. The durable
launch permit covers that entire gap. After genuine successful initialization,
the exact child atomically changes its one startup-verification row into
startup-complete, retaining owner and parent permit; no new DB schema is added.
The exact watchdog validates original operation/generation/source/image and live
identities and atomically consumes both rows. Generic finish cannot erase either
proof, no completed scope grants ordinary child work, duplicate/stale/other-child
proofs fail, and failures/disconnect/expiry retain unresolved rows and the fence.
Fast completion before any parent poll is valid only via this durable record.

Backend publishes completion after successful lifespan initialization, not merely
module import. Gateway publishes after store/scheduler/PID/heartbeat initialization,
never from failure finally. Legacy/normal non-maintenance finishing is unchanged.
Deterministic controlled-child regressions must force the pre-verification parent
poll, pre-poll fast completion, pre-verification crash, initialization failure,
wrong/replayed/stale/expired/disconnected proof, and closed business admissions.
Scheduling barrier files in tests are not readiness proofs. Extracted production
initializer functions use fake dependencies and do not establish full FastAPI/
Uvicorn/AnyIO/Linux/Bash/container or field acceptance.

Break-loop capture: this was a cross-layer contract/implicit-assumption/test-gap
failure. The first startup repair connected launch but inferred readiness from
zero work, so adjacent register/verify test calls hid the production import gap.
Prevention is the atomic durable completion protocol plus forced scheduling,
not more sleep, polling observations or optimistic ready events. Scope remains
backend/gateway startup; no new ticket, template tree or production path is added.

GET /api/v1/maintenance/status is implemented directly by the production ASGI
middleware and reads a stable DELETE-journal SQLite snapshot in memory. It emits
only version/status/protocol/phase/fenced, not credentials/account/order/actor data;
it neither polls nor writes expiry state. Other fenced business GET/write paths
remain closed. Deployment verification probes this contract, not broad /status.
In-memory HTTPX+real asyncio scheduling with selector I/O prohibited proves this
pure route only, not full FastAPI/auth/AnyIO lifecycle or network transport.

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
The real normal window is rechecked after drain, before each supervision mutation
and immediately before the natural exit request. Failure cancels publication and
keeps admission closed; ordinary runtime/risk/identity proofs are rechecked too.
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

## Actual local integration evidence

run_actual_integration.py is separate from the strict 156-case专项 runner.
It rejects credentials/production data, public DNS/network, datagrams, other
local services and real scheduler/broker/LLM imports/processes. Only ephemeral
loopback endpoints owned by that test scope (including real loop wakeup socket
pairs) can connect; synthetic listener ports are released at teardown.

Endpoint ownership is the canonical literal loopback address, exact port and AF,
not just the port. Reject localhost, scoped/mapped aliases and wrong AF; validate
numeric resolver results and completed peers against that same endpoint. Keep
the owning socket alive and serialize connect with close/unregistration. HTTP
uses a proxy-free private opener and checks redirects before transport. Eight
guard-only regressions use an independent transport tripwire even against the
old broken guard; they are not eight additional real application integrations.

Installed real AnyIO/HTTPX drives tasks, streams, background/abandoned worker
writes and accounted startup work with production adapters; Windows /proc remains
unverified and must not get a fabricated Linux ACK. Real urllib loopback requests
exercise actual Upgrade.verify/resume and the production status ASGI route, but
Docker/OS/account/ACK/file-owner boundaries and health/auth shell routes are explicit
synthetic substitutes. No completed fake framework initialization is claimed.
Seven actual integration cases and their source minima are recorded separately;
full FastAPI/Starlette/Uvicorn/auth and full physical verification stay blocked
when dependencies or runtime proofs are missing. A local/hosted partial pass is
not release, legacy-bootstrap, rollback or schedule-resume authorization.
