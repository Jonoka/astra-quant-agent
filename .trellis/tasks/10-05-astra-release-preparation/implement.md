# Execution

1. Preserve verified base and document capabilities and authorization boundaries.
2. Implement strict source/provenance/baseline/CI gate changes in one owned area.
3. Implement explicit additive schema and forward/latest-state rollback checks in
   a separate owned area; keep the runtime helper standalone and fail closed.
4. Add a Linux real-lock gate, required hosted discovery, and local synthetic
   rehearsal/negative tests; never substitute platform stubs for Linux acceptance.
5. Run portable mock/SQLite suites, syntax/source-contract checks and independent
   review. Record command results and pending capabilities honestly.
6. Update executable code-spec and validation; save a local commit only.

Implement/check agent prompts begin with Active task and reference these artifacts.
