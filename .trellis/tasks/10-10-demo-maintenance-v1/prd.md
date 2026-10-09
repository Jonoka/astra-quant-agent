# DEMO maintenance deployment protocol

## Authorization and scope

The owner approved implementation on an independent local branch and offline
verification. Base: `948df24e458853cf9c6966c2d8641c18e6dc0917` (fixed PR5).
No VPS/SSH, exchange/network probes, credentials, production state, push, merge,
Actions dispatch, image build/publication or new live OP/PIN are authorized.
The only network operation was one approved anonymous fixed-SHA Git fetch.

## Required behavior

1. DEMO-only maintenance closes every new admission, drains admitted/queued
   work, obtains current-generation component acknowledgements, performs a
   consistent cutover, starts the candidate paused, verifies it, and resumes
   only explicitly. All startup/rollback ambiguity stays fenced.
2. All scheduler/manual/order/cache/ledger/notification/write sources participate.
   Track queued futures, child processes and active writers; zero job rows alone
   is insufficient. Preserve the worker singleton lock through actual drain.
3. Maintenance timeout cancels publication, never kills admitted work. Disconnect,
   crash, restart, missing acknowledgements and unknown order outcomes cannot
   resume trading. A live old worker/child blocks a replacement.
4. Bind requests and acknowledgements to operation, external plan pin, approved
   source/image and exact component instance. Duplicate requests cannot renew
   deadlines; stale generations cannot release a newer operation.
5. Live/unknown mode, positions, pending/conditional/partial orders, unresolved
   requests or unverified risk proof refuse maintenance. No automatic cancel,
   close, strategy changes or weakening of risk settings.
6. Preserve normal release's real 900-second slot and >=480 seconds remaining,
   plus all existing provenance/configuration/auth/state/rollback gates.
   Maintenance replaces only the time-window condition using real pause/risk/
   ownership proofs and an absolute bounded budget.
7. Previous and candidate must both support the protocol. Legacy bootstrap and
   legacy-image rollback remain explicitly blocked; no fabricated lease ACK,
   signals or supervisory bridge is included in this implementation.
8. Preserve latest writable records and intentional deletions on rollback; do
   not restore stale order state. Missed trading cycles are not replayed.
9. Necessary sending-before-network order journal records stable idempotency
   identity and unknown outcomes. It must not pretend legacy intents are this
   journal. Never resubmit unknown requests merely because a lease expires.
10. Preserve PR5 alpha files, policy/prompts/models/assets/leverage/risk values,
    original worktrees and prepared PR5 bundle/helper/pin bytes.

## Acceptance

Focused offline tests exercise actual reducers/admission/queue/process/command
paths with temporary data, fake broker, clocks and commands. They cover races,
timeout-without-kill, supervision, restart-paused, stale requests, duplicates,
unsupported versions, order unknown/partial cases, source guards, normal >=480
behavior and latest-state recovery. No generalized production-reading test
discovery. Linux real flock and container/field acceptance are separate pending
gates, not inferred from Windows tests. CI code is updated but not dispatched.

## Approval retained for later

No implementation result authorizes first legacy transition, live use, external
supervisor signals/restart changes, real risk checks, publication or deployment.
