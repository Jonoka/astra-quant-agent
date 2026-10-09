# Implementation and verification

1. Pin isolated branch/worktree at PR5, capture protected hashes and original
   worktree HEAD/status; never update the original branches or prepared bundle.
2. Implement pure state/admission/order journal and focused offline tests.
3. Integrate scheduler queued scopes, actual child drain, full singleton lifetime,
   runtime/manual/broker/cache/ledger/writer gates, startup hold and supervision.
4. Add strict versioned maintenance plan/staging/controller contract. Keep normal
   >=480 and all existing safety checks; unsupported previous/rollback blocks.
5. Add retained maintenance patch/source allowlist/checks to CI code without
   dispatch. Existing alpha hashes remain independently fixed.
6. Run focused, isolated local suites with fake broker/clock/process/commands;
   verify no network/credentials/real dotenv/data touched and report skip limits.
7. Independent high-reasoning review of whole data flow. Repair concrete failures,
   repeat affected checks, compare protected files and PR5 fixed hashes.
8. Deliver reviewable local diff and evidence. No push/PR/merge/CI/image/OP/PIN/
   controller install/SSH/production operation. Clearly state bootstrap and
   Linux/container/field acceptance remain blocked or pending.

Stage milestones: core protocol first; runtime/controller integration next;
final report only after actual focused tests and scope review. Partial progress
is not production readiness or complete authorization.
