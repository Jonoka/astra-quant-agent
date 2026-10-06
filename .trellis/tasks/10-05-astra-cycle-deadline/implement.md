# Execution

1. Verify remote refs, local cleanliness and active tasks; keep original trees.
2. Implement shared deadline and safe request metadata boundary.
3. Repair LLM transport/retries and council/brain budget propagation.
4. Bound trader subprocess to its slot and persist deduplicated skip reasons.
5. Add temporary, mock-only regressions; run relevant existing suites in the
   isolated workstation checkout, as expressly approved by the owner.
6. Review full diff, schema compatibility, policy preservation and secret safety.
7. Commit locally; do not push, run production probes, restart or deploy.

Recovery: discard this isolated branch only after delivery; production unchanged.

## Owner-approved review follow-up

Use independent reviewers for inference/persistence and release/migration, reproduce
findings with synthetic data, implement minimal corrections, require actual Linux
lock/HTTP/SQLite and retained regressions, then normally push the current original
branch and update PR #2. Recheck remote drift immediately before pushing; do not
force, replace the PR, merge, dispatch release workflows or operate production.
