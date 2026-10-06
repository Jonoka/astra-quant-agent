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

## Later PR #2 review acceptance

The owner's later authorization covers normal feature-branch push and updating
the same draft PR. Independent review found and fixed persistence-lock/atomic
publication deadlines, concurrent old-DB column migration and request-ID/timezone
observability issues. See the cycle task validation for 632 actual Linux tests,
zero skips and precise current-source versus historical image scope. All retained
release baseline pins, labels, exact schema/AST guards and latest-state rollback
contracts remain. New candidate/GHCR/published Compose and production gates remain
pending; no release workflow was dispatched and no deployment occurred.

## Approved hosted release and direct CI correction

PR #2 was marked Ready and merged as 19181fe8ffa1640da67ac9e946265e092be1d754;
the merge tree exactly matched accepted 4e532a2 and retained deployed 90f9f3a.
First hosted build run 37398704112 passed source ancestry, official tests and
both state rehearsals, then stopped before build/publication because unittest's
shared default loader retained the first directory as its top-level root.

Only the repeated tests/* discovery now creates a fresh TestLoader and uses the
project root for qualified module names. Independent AST comparison confirms all
targets, minima and zero-skip assertions unchanged; isolated .github helpers,
source/rollback pins, labels, package visibility and workflow permissions remain.
An independent Linux 3.11.17 replay of the exact corrected YAML body passed 226
tests across all 11 target suites, plus 10 source gates, with zero failures/errors/
skips. The old-loader negative control and same-basename module check passed.
Evidence: workspace/independent-discovery-replay/replay.log. A fresh hosted build
must reach terminal success before any image or deployment attestation is made.
No production operation is included in Ready/merge/build authorization.

## Second hosted CI correction: shallow acceptance mount

Run 37399376984 passed the deadline regressions, hosted Linux locks, state
rehearsals, source guard and candidate build, then failed before candidate lock
tests: os.environ.get eagerly evaluated parents[2] for the shallow /acceptance
mount despite an explicit /app source root. No image was published.

The helper now evaluates its repository-path fallback only when the source-root
environment variable is absent; the empty-string behavior remains unchanged.
A source-guard regression imports the actual helper from the exact shallow path
and requires discovery of all seven lock cases. All 11 source checks passed.
Independent Linux verification used the existing dependency image with frozen
current source files and the exact shallow mount, network disabled, read-only
root and tmpfs: seven real fcntl cases passed, zero skips, Docker exit 0.
Evidence: workspace/independent-shallow-lock/shallow-lock.log and source hashes.
This proves the entry-point correction; acceptance of a newly built candidate
and its published immutable digest still requires the next hosted run. No
production operations are authorized by this CI correction.
