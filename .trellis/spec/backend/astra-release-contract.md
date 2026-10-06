# Astra release preparation contract

The deployment fork is `Jonoka/astra-quant-agent`, deploy branch `codex/deploy`.
Each release builds its actual `github.sha`; upstream `main` remains unchanged.
PR3's reviewed taker repair extends the exact application path set. Its five
application files must match SHA-256 blobs from independently reviewed head
`8b6a4bf1948b6e19e175b881e36eb4d3bf78e504`; missing or changed files are rejected.
Formal release also requires the same offline 411-test taker gate and its five
self-tests before building, retaining all prior ancestry, migration, deadline,
Linux lock, Compose and immutable publication gates.
The live baseline must be supplied as full `previous_source` and immutable
`previous_image` workflow inputs, then independently reconciled with actual
production containers before staging. `90f9f3a` is retained only as an explicit
historical additive-migration fixture; it is never the current runtime default.
The observed 2026-10-06 runtime was source `f53e579b0db091f351f271f79ebbb99da1e6f7c2`
and image `ghcr.io/jonoka/astra-quant-agent@sha256:171d9f3ee64e3b5b8ba870902c1ed80ba0f38d50e67137716594472b5cea88cb`.
Refresh this observation before execution; neither this document nor an old
artifact authorizes accepting a changed live baseline.

The source guard enumerates the exact permitted application delta from upstream,
retaining council completion and OKX public-domain fixes as well as cycle deadlines.
Candidate and published images require `council-completion-v1`,
`okx-public-domains-v1`, and `cycle-deadline-v1` provenance labels.

Gateway compatibility permits only the reviewed `job_runs.scheduled_at` column,
the exact `model_requests` table, its client-request uniqueness constraint, and
its job index. Every pre-existing table, column, index, trigger and view must
retain its contract. Source AST and independent schema expectations reject
unknown migrations; candidate declarations are not their own approval oracle.

Synthetic acceptance initializes the previous source, migrates twice, writes
new data, verifies old-source reads and writes, and exercises recovery using the
latest writable state, including intentional deletions. It never restores an
outdated database or drops additive schema. Authentication, trading settings and
other source/database contracts remain protected.

SQLite probes must close their connections and finish before freezing the file
manifest used during recovery. Read-only WAL probes can create auxiliary files;
the guard compares the full post-probe tree and retains WAL/SHM in the latest
state copy. It must not ignore those files or use immutable reads that miss WAL
records. Genuine changes during the copy still abort recovery preparation.

Linux acceptance uses genuine cross-process `fcntl.flock`; Windows import mocks
cannot satisfy it. Candidate and immutable published images undergo isolated
Compose smoke checks. Missing, failed or entirely skipped suites fail hosted CI.
Unavailable local Linux/container infrastructure is recorded as pending, never
reported as passed and never started without authorization.

Local preparation authorizes edits, synthetic tests and local commits only.
Push, PR, merge, hosted release builds and production cutover require their own
authorization. Production only pulls the approved immutable image; it does not
build, run test suites or install dependencies. Before cutover, refresh and
recheck runtime/source/image baselines and prepare a fresh consistent backup.


## Canonical deployment plan and shared gate

The prior 11-asset supplement stayed outside Git and its 18 additional tests were
absent from release CI. Every bundle must now package the canonical helper,
stager and plan-capture tool from the exact release source. Separate operation
sidecars cannot silently replace repository helpers.

`prepare_deployment_plan.py` captures only source/image identities, asset names,
raw asset-file hash/owner/mode and protected configuration hashes from an explicitly
approved baseline. Its output is a new restricted operation file, not a runtime
configuration edit. Pass the returned external `--deployment-plan-sha256` to
staging and every helper action. Guards never re-pin a changed plan themselves.
Asset membership and order are approved explicitly; no permanent 10/11 count
restriction is used. Native readers must trust the exact unchanged raw bytes.
Risk constants, profile, prompt schema and rendering checks remain intact.

PR and formal release both run `run_deployment_preflight.py`: actual clean Git
HEAD/ancestry/exact application paths and five reviewed application hashes, then
original helper tests, plan tests, Linux locks, latest-state rehearsal, and the
411-test offline taker gate with its five negative-result self-tests. Missing,
failed, skipped or expected-failed required tests block both paths. Formal CI
also rehearses upgrade/rollback from the explicitly selected current source,
while retaining the historical additive fixture. Archives and provenance carry
that selected current source/image as rollback authority.

The helper validates its own/provenance bytes, externally pinned plan and live
pool/configuration at each boundary. Before any stop or recovery block, it requires
an adequate 15-minute scheduler window, a stable checkpointed in-memory database
snapshot with zero running jobs, and no active trader/brain process. A failed
pre-stop gate performs neither stop nor recovery. Later recovery copies latest
persisted data and deletions; it never restores the stale snapshot over new data.
Unapproved configuration drift stays preserved and blocks restart for review.
An already-deployed deadline gateway is accepted only with byte-identical gateway
source; the original independently specified additive migration remains supported.

Rollback also freezes operation-owned deployment identity. Before every stop or
ROOT rename, it reconciles the journal phase with non-writable source bytes and
metadata, previous/candidate image provenance and captured container IDs. Healthy,
unhealthy, stopped and partially created owned containers remain recoverable;
an external release or recreated same-image container is refused before mutation.
Candidate/recovery container IDs are journaled even after partial Compose failure.
Missing-ROOT recovery verifies its staged source and stopped/full recovery evidence
before restoring it. Rejected direct rollback records a separate identity-rejection
file and leaves prior failure evidence and the external deployment intact. An old
journal without deployment-source identity fails closed; no new approval pin is
derived from a changed live deployment.
