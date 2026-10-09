# Design

## State and ownership

Add a pure-stdlib maintenance store in a separate writable SQLite file, without
changing any existing trading/gateway/admin database schema. SQLite transactions
serialize admission and fence creation across components. Importing the module
does not initialize data or read dotenv/credentials.

State: NORMAL -> DRAINING -> PAUSED -> SWITCHING -> VERIFYING -> RESUMING -> NORMAL.
Cancellation before switching becomes CANCELLED/HOLD with admission still shut;
expiry never opens a fence. Explicit safe resume can conclude a cancelled operation
only after work settles and fresh proofs pass. Post-switch ambiguity is HOLD and
requires identity-checked latest-state recovery. Budgets are absolute, not sliding.

Bindings contain operation ID, externally supplied plan SHA256, previous/target
full source and immutable image, generation and approved exact role instances.
Required roles: backend, gateway, watchdog-backend, watchdog-gateway.
App role instance includes process start identity, not only a reusable PID.
The root deployment controller validates Docker/source identity independently;
component self-report alone is not source/image or liveness evidence.

Every admitted unit is registered atomically before queue/spawn/send and released
after its final writer/subprocess really settles. Acks are same-generation and
require no activities of that owner. PAUSED requires every required current
instance ack, globally empty activities and fresh DEMO risk evidence. Unknown
crashed activities are retained, never pruned by a guessed PID or lease age.

## Shared interfaces (core owner finalizes exact names in forum)

`astra_backend/maintenance.py`: explicit-path store, validated binding/identity,
request/status/admit/finish/acknowledge/pause/cancel/begin_switch/begin_verify/
resume operations, risk-proof validator and order journal. No app imports.
Risk proofs include verified demo identity, positions/pending/algo/partial/inflight/
unknown counts, capture time and operation binding; absence is not zero.

`astra_backend/maintenance_runtime.py`: adapters for default data-root resolution,
role startup/poll/ack, admission contexts and non-killing subprocess drain. Actual
source/image identity must be independently checked by the packaged controller.
`scripts/maintenance_control.py`: safe metadata/ack/risk inspection protocol used
by controller through existing operator permissions, not a new public endpoint.
Broker proof collection is a dedicated read-only path, never existing reconcile.

Worker admission is registered before ThreadPoolExecutor submission so queued
futures count. During maintenance, scheduling returns without queueing, but the
loop retains heartbeat/lock and tracks running futures. Finally drains executor
and children before releasing singleton. Cancellation/timeout never calls kill
as a maintenance escalation; ordinary existing deadlines remain unchanged.

Backend middleware fences manual mutations and paid/manual job starts. Scheduled
background cache/ledger/writes and gateway warmup/notification sources use the
same activity owner. Background stop must join/settle, not merely flip a flag.
Watchdog uses protocol-aware supervised actions: check/register before restart,
never TERM/KILL under an active maintenance fence; publishes exact-instance ACK.
Entry points start maintained candidates with admission shut before importing
side-effectful app modules. Restart cannot manufacture NORMAL state.

## Orders

Before a mutating broker send, persist its logical intent/client identity and an
active send scope. Stable clOrdId is derived from logical intent, not regenerated
on a retry. A network exception or unverifiable response is UNKNOWN and remains
unresolved; do not release its reservation or automatically resend. Do not change
price/risk/strategy calculations. Existing acknowledged legacy intent files stay
unchanged and are not treated as proof of pre-send journaling.

## Release boundary

Old normal plan schema remains readable but is not executable by the new helper.
Executable normal mode also requires a schema2 protocol scope and the full barrier,
retaining real >=480/process/database gates in addition to that barrier.
Explicit maintenance scope is a versioned extension that requires both versions'
protocol marker/source bytes and maintenance-capable image provenance. Guard
source/config/candidate capacity/runtime/auth/Compose exactly as before, with only
the idle-time condition substituted by real admission/ack/risk proof.
The current legacy previous source cannot enter maintenance. This is a gate, not
an unimplemented fake bridge. Neither previous API nor a new candidate API is
assumed available to bootstrap legacy.

Package shared maintenance module bytes from the exact reviewed source (no
handwritten production sidecar). New helper/module/check changes require new
hosted CI, provenance, source archives and new OP/PIN; retain existing artifact.
Candidate/rollback start paused and source/container instances are rebound only
after old processes are proven stopped; verify health/auth/state/singleton before
explicit release. Existing stopped snapshot/latest writable carry/deletion and
rename recovery contracts stay authoritative.
Do not call docker stop -t60 to drain. Maintenance shutdown first waits for
protocol-aware processes to exit without kill; ambiguous/expired drain never
enters stop/rollback. Tests must show no order/task replay on late resume.

## Offline-only boundaries

Standalone focused runner installs network, real dotenv/credential and data-write
guards before imports, uses explicit temporary roots, and avoids tests/__init__.py
which loads dotenv. Linux flock tests remain mandatory CI/pending on Windows.
No builds, Docker runtime, SSH, exchange, GH auth or release actions are run here.

## Ownership

Core worker owns maintenance.py and protocol/journal unit tests. Runtime worker
owns application integration/adapters/watchdog/entrypoint and focused integration
tests. Main owns .github scripts/workflows/source guard, deployment contract and
cross-layer integration fixes. No worker reverts another's edits; interface
questions go to the local forum and native mailbox before overlapping changes.
