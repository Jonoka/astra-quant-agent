# Astra release preparation contract

The deployment fork is `Jonoka/astra-quant-agent`, deploy branch `codex/deploy`.
Each release builds its actual `github.sha`; upstream `main` remains unchanged.
PR3's reviewed taker repair extends the exact application path set. Its five
application files must match SHA-256 blobs from independently reviewed head
`8b6a4bf1948b6e19e175b881e36eb4d3bf78e504`; missing or changed files are rejected.
Formal release also requires the same offline 411-test taker gate and its five
self-tests before building, retaining all prior ancestry, migration, deadline,
Linux lock, Compose and immutable publication gates.
The reviewed current runtime baseline is source
`90f9f3a558bdbea0171b19a42c58e2fae7ed8e9d`, upstream
`e0b29fef1818e0ff9c6b210eb73234620e276a02`, and image
`ghcr.io/jonoka/astra-quant-agent@sha256:8b471e834dbfe633d720dc5d0ad0c4249e922dce91689719c46ca0fb6575b43b`.

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
