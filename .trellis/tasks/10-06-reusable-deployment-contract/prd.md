# Reusable Astra deployment contract and current taker cutover

The owner approved prevention of repeated deployment defects and explicitly
continued this taker deployment. Implement and commit the delivery correction,
run isolated upgrade/recovery tests and independent review, complete hosted CI
and formal publication, then guarded backup/pull/cutover on the existing Astra
server and observe naturally scheduled AI rounds. No manual model/order calls,
strategy/risk/credential/demo changes, request-ID repair or snapshot-expiry policy.

The live read-only preflight at 2026-10-06 14:33 UTC found source f53e579,
image 171d9f3e, healthy backend/gateway with zero restarts, trusted 11-asset pool
SHA256 2bd5872da567dae5c4479444461b3edeb98c2bfd0af0135d4c748ec083945966,
unchanged dotenv SHA256 02f51685020502de99a4c1f0609f05346d738fd6f724adfed84a9c30c4a8479a,
demo true, available upgrade lock and an active natural trader. Revalidate all
mutable facts before deployment; this preflight is not a stop authorization window.

Acceptance:

- Canonical version-controlled helper validates an externally pinned deployment
  plan binding current/rollback source and immutable image, target source/image,
  exact asset-reader names, full raw pool hash/owner/mode and protected config.
  Missing/unapproved plans, altered files, wrong images/sources and drift fail.
- Remove runtime authority from hard-coded previous versions and ten/eleven
  asset lists. Retain explicit historical compatibility fixtures as fixtures only.
- Preserve schema/auth/risk/prompt/data protections and latest-state recovery.
  Upgrade and rollback tests include genuine old/new readers, candidate writes,
  intentional deletions, pool drift and refusing stale snapshot recovery.
- PR and formal publication share the same cheap release preflight, including
  actual Git ancestry/exact-delta verification and canonical helper regressions;
  keep the existing 411 taker regressions and all official release gates.
- Package the helper and raw archives from the same verified source and bind the
  approved current baseline into provenance; reject helper/archive/image drift.
- Production only pulls immutable hosted images and runs named --no-build
  --no-deps cutovers. Check lock and active jobs/window immediately before stop.
- Validate exact runtime source/digest, native auth protection, healthy services,
  restart zero, one worker, unchanged 11 assets/config and unrelated services.
  Observe at least two consecutive fresh natural AI rounds; extend observation
  if success/persistence or parsing evidence is inconclusive. Never manufacture
  rounds, success or order evidence.

Independent review must precede production mutation. If automatic approval
rejects an action for missing authorization, retry once with the owner's exact
scope evidence, then stop that action and report its blocker.
