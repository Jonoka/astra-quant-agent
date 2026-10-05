# Local validation and remaining release gates

Local preparation is complete atop `b25a200a439a2f40b753a979c7c55119b406ccd7`.
No push, PR, merge, remote workflow, image publication, infrastructure startup or
production operation occurred. The parent will report the final local commit SHA.

## Passed in the current Windows environment

- All 62 new cycle/deadline/request-correlation mock cases: zero failures/errors/skips.
  The prior repair's complete 523-case mock result remains recorded separately.
- 10 source/provenance/CI gate tests: zero failures/errors/skips. The exact reviewed
  application delta has 61 paths; source guards additionally require current deployed
  `90f9f3a` ancestry, the official upstream pin, actual workflow SHA, and a clean tree.
- 26 portable runtime-helper cases: zero failures/errors/skips. They cover exact
  independent additive DDL, old schema/index protection, unknown object rejection,
  unique request IDs, assignment/function/class/import shadowing of migration
  declarations, full migration-method AST retention, image metadata,
  authorization and pre-stop drift rejection, including real WAL connection cleanup.
- Four actual-SQLite rehearsal tests: zero failures/errors/skips, including a genuine
  source change during recovery copying that must still reject preparation.
- Standalone rehearsal with actual archived previous storage implementations passed
  forward initialization, second-init idempotence, new request writes, old reads and
  writes, latest-state rollback and auth/trading/config continuity. Previous source
  archive PAX comment is `90f9f3a558bdbea0171b19a42c58e2fae7ed8e9d`; SHA256 is
  `a4780f22880eeaf209178d63a50daa747dc3b63089ce7e708462814896a66a49`.
  Original and stopped-snapshot trees retain the stale deletion fixture; recovery
  retains the latest deletion, all newly written records and the existing session.
- Python 3.11 syntax parsing, current interpreter compilation, workflow YAML/embedded
  Python parsing and whitespace/scope checks passed. This is not a Python 3.11 runtime
  execution claim; the available workstation interpreter is Python 3.14.

Rehearsal uses only synthetic temporary directories and actual SQLite transactions.
Network and external commands are disabled. A test-only connection subclass closes
old-store connections deterministically after real transactions; copy/service adapters
exercise recovery control flow without claiming Linux ownership or service acceptance.
The Windows previous fixture leaves the official frontend image symlink unmaterialized;
it is storage-source verification, not archive staging or image acceptance.

The rehearsal exposed a genuine helper ordering issue: read-only SQLite checks can
create WAL/SHM after a manifest is frozen. Runtime probes now explicitly close and
finish before the full tree is frozen; WAL/SHM remain covered by copying and strict
drift checks. No immutable read or auxiliary-file exclusion was introduced.

## Explicitly pending

Read-only elevated capability inspection confirmed Docker's `desktop-linux` context
but no `dockerDesktopLinuxEngine` pipe; the sole WSL2 `docker-desktop` distribution
is stopped. No engine/distribution was started, installed or replaced.

- Five real Linux cross-process flock cases: all five explicitly skipped here, and
  the acceptance command returned exit 1. Hosted CI and candidate-image execution
  require all five to run with genuine `fcntl.flock`, with zero skips.
- Eleven existing runtime-helper cases for POSIX paths, ownership/modes, links/FIFO,
  actual `cp`/recovery copying and nonblocking locks were excluded from the portable
  subset, without modifying or weakening the production/hosted tests.
- Hosted Python 3.11 dependency/runtime suite, real Linux migration/ownership behavior,
  candidate-image locks, candidate/published Compose health and administrator checks,
  immutable published image ID/digest and GHCR publication remain unexecuted.
- Authenticated production UI/round/New API correlation after deployment is pending;
  these local fixtures cannot establish real model or live scheduling acceptance.

The build workflow requires retained regressions, all 62 new cases with zero skips,
the four rehearsal cases with a verified previous source, real hosted Linux locks,
real candidate-image locks under `--network none`/read-only root, and candidate/
published Compose checks tied to the same immutable image ID. Staging requires all
nine independent attestations; candidate/published images require all three patch
labels. No manifest claiming these pending gates passed was created.

Evidence is retained outside application source in the task workspace:
`release-deadline-regression-results.txt`, `release-source-guard-results.txt`,
`release-portable-helper-results.txt`, `release-cycle-rehearsal-tests.txt`,
`release-cycle-upgrade-rehearsal.txt`, `release-linux-flock-capability.txt`,
`release-preparation-checks.txt`, `release-final-gate-tests.txt` (10 + 4 final
cases together), and the prior `final-mock-validation.txt`. Both implement agents
performed a final independent read-only check of each other's release areas;
the root fixed the declaration-shadowing finding and reran all affected gates.

## Next authorized stages still needed

Obtain explicit approval for feature-branch push and a review PR to `codex/deploy`;
the isolated checkout currently has a local-path origin, so a GitHub fork remote must
be selected explicitly. Merge and hosted build/publication also require approval.
Before a separately approved production cutover, refresh actual source/image/ref
baselines, confirm every hosted gate, pull the immutable image, make a fresh consistent
backup and use guarded no-build/no-deps cutover with latest-state rollback. Current
baseline is source `90f9f3a`, image digest `8b471e...`; old operation backups that would
return to `e4fe084` are not the rollback target for this repair.
