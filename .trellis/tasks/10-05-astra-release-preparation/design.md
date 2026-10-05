# Design

Retain the current local branch atop b25a200; remote deploy/OKX remain 90f9f3a and
main e0b29fe. No remote changes are authorized. Keep SOURCE_SHA=github.sha and
Ubuntu-hosted immutable candidate/published Compose smoke after a reviewed merge.

Update strict source allowlist with the complete reviewed non-.github delta,
including task/spec documents. Retain all prior paths and council success checks;
add nonempty deadline regression discovery. Update PREVIOUS_SHA/runtime PREVIOUS
to 90f9f3a558bdbea0171b19a42c58e2fae7ed8e9d, OLD_IMAGE to
ghcr.io/jonoka/astra-quant-agent@sha256:8b471e834dbfe633d720dc5d0ad0c4249e922dce91689719c46ca0fb6575b43b.
Keep upstream e0b29fef1818e0ff9c6b210eb73234620e276a02 and v8.6.1 fixed.
Retain council-completion-v1, require okx-public-domains-v1 and cycle-deadline-v1
candidate provenance. Keep runtime helper standalone in the deployment bundle.

Schema compatibility remains exact for auth/trading databases, existing gateway
columns/indexes, migration declarations and unknown objects. The only permitted
addition is job_runs.scheduled_at TEXT NOT NULL DEFAULT '', the independently
specified model_requests table with its unique client ID, and idx_model_requests_job.
Reject modified old columns, unrelated additions/removals, malformed approved
objects and dropping new data on rollback. Source schema/migration checks must
not derive their accepted shape from the candidate under test.

Use git archive of the verified previous source into this workspace, not another
production checkout or data copy. Seed genuine old schemas and synthetic settings/
records, run current schema entry points, write new records, then use the previous
implementation to read/write the same newest database. Exercise existing carry/
recovery logic with temporary paths and all external commands disabled. Add an
independent Linux real-flock suite and wire it on the hosted Linux runner.

Recovery probes finish and close before freezing the full latest-state tree:
read-only SQLite WAL inspection can materialize sidecars. Keep all WAL/SHM files
and strict post-copy drift rejection. Windows SQL rehearsal closes old-store test
connections after genuine transactions; POSIX service/ownership acceptance remains
a separate gate. Candidate-image locks run against its immutable image ID with no
network and a read-only root, then Compose/published smoke validates that same ID.

Capability findings: Docker Desktop CLI installed but desktop-linux engine pipe
absent; sole docker-desktop WSL2 distribution stopped. Read-only elevated capability
inspection confirmed this. Do not start it. Actual Linux locks and candidate/published
images cannot be tested locally in the current state. Windows Python/SQLite and
mock helper checks can run with explicit platform limits; no remote CI is invoked.
