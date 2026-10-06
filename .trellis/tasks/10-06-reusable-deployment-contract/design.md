# Design and recurrence evidence

The prior f53/171 deployment used an operation-specific pool_v2 supplement. Its
recorded scope preserved application image/source and changed only deployment
acceptance. Helper SHA256 00f0b2bc differs from the canonical helper's 561d8e74.
GitHub path history shows the canonical helper was last changed at f247950
(2026-10-05 15:48 UTC), before supplement approval (2026-10-06 03:26 UTC).
No pool_v2/baseline/supplement files are tracked at f96d01b. Both release bundles
still contain byte-identical canonical helper 561d8e74. The supplement's 18
pool tests stayed outside hosted CI, which ran only the original 37 helper tests.
This proves missing upstream propagation; the full original authorization
conversation was not independently re-fetched during this recurrence analysis.

The old previous source/image is repeated in runtime helper, staging, workflow
and tests. It is a valid historical fixture, but cannot identify today's runtime.
PR3 tested 411 parsing cases without actual release source verification. Thus the
old exact path set missed 13 reviewed paths and rejected first release fa6f66c.
Correction f96d01b restored strict gating; consolidate preflight to prevent repeats.

Use the existing helper/state machine and byte-preserving carry/recovery logic.
Require one operation plan with externally supplied SHA256. Current source/image
and exact pool identity are approved data, validated and frozen; do not derive
acceptance from the mutable candidate or update the plan to disguise drift.
Canonical helper uses instance previous identities and checks the plan at every
drift/start/continuity/strategy boundary. Keep risk/schema/native-auth policy gates.

Formal manual workflow receives explicit previous source/image inputs. It keeps
the old 90f reference only for retained historical compatibility tests and adds
current-baseline rehearsal/Compose acceptance. Actual packaged previous archive
uses the supplied verified current source. Target app delta stays the five exact
reviewed taker files; delivery changes do not alter application code.

One shared preflight command verifies actual checkout SHA/ancestry/exact paths,
source guard tests, canonical helper tests including pool/plan cases, Linux locks,
and the existing isolated 411+5 gate. PR checkout must fetch enough ancestry.
Both PR and manual release invoke it. Artifact/helper hashes and Git pins remain
mandatory before staging and immediately before execution.

Before stop, hold the persistent upgrade lock, recheck source/image/config/plan,
verify no active scheduled jobs/trader and a sufficient next-slot window. Stop
only the two Astra services, copy consistent stopped state and preserve current
data/config. On failure use original source/image with newest writable state;
never restore stale pre-cutover records. Wiki evidence uses its lock/CAS protocol.

Local Linux acceptance: 89 tests (37 original helper, 29 plan/capture/manifest,
12 source guard, 4 state rehearsal, 7 genuine process locks), zero failures,
errors or skipped tests. Separate actual-source SQLite upgrade/rollback
rehearsals passed against both historical 90f9f3a and current f53e579. All five
reviewed taker application blobs retain their exact approved SHA256 values.
Hosted shared-preflight execution, independent review, final official bundle
verification and production cutover remain separate acceptance steps.

Independent review R1 (P2) reproduced an obsolete accepted A->B operation
stopping a later C deployment. The original report and failed independent log
remain untouched. Rollback now checks journal phase, frozen non-writable source,
immutable image/config identity and captured previous/candidate/recovery container
IDs before every stop or ROOT rename. Partial Compose failures durably register
only matching operation images; unhealthy owned services do not require health
acceptance before recovery. Missing-ROOT and recovery-start retries keep their
explicit source and record bindings. Nineteen real Linux control-flow tests cover
foreign source/image/IDs, late drift, legitimate partial starts and both rename
interruptions, while preserving latest records and deletions. A journal without
source identity fails closed; no old operation plan is automatically reapproved.

Independent re-review closed R1 and reproduced R2: recovery->ROOT rename had
completed while disk journal still said candidate-retained, so a legitimate
retry refused to start the stopped original services. Recovery now reconciles
only complete stored recovery evidence, original/stopped snapshots, phase/layout,
frozen source and stopped operation-owned image/container identity. The ready
journal also freezes complete input bytes for safe retry before the first rename.
Twenty new tests inject real rename/phase/atomic-journal interruptions, reload
state.json from disk, and verify retained latest records, logs and deletions.
Missing trusted evidence and substituted data/permissions/source/image remain
fail-closed. Original independent R2 evidence is preserved outside the repo.
