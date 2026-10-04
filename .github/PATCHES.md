# Retained Deployment Corrections

The deployment branch is `codex/deploy`; `main` follows upstream without local
application fixes. Development changes use separate `codex/*` branches before
integration into the deployment line.

## council-completion-v1

Upstream through main `e0b29fef1818e0ff9c6b210eb73234620e276a02` (v8.6.1)
uses single-model-only `content` in shared success telemetry. A successful
committee writes its decision files but then returns None/failed health, which
prevents fresh trading decisions and AI position management from reaching their
consumer. This correction records output length from the actual successful
branch and preserves failed-inference/persistence execution guards.

Owned application files: `scripts/brain/dispatch.py` and
`tests/ops/test_brain_dispatch.py`. The required `CouncilCompletionTests` suite
checks exact persisted cache return, health, output length, fallback/failures,
and the actual downstream management gate. Upstream's former tests locked in
the broken behavior; retaining those old assertions is not a repair.

## Updating Upstream Without Losing the Correction

1. Update fork `main` by an upstream fast-forward. Merge the selected pinned
   upstream commit into a separate branch based on `codex/deploy`.
2. Resolve conflicts while preserving the corrected committee return and its
   regression suite. Never overwrite dispatch.py with an upstream copy or reset
   the deployment branch to upstream.
3. Update the pinned upstream/version and prior-production identities in the
   reviewed deployment helpers. Review changes to prompts, risk behavior,
   persistence schemas and latest-state recovery.
4. Build the corrected fork commit. The workflow's source is
   `Jonoka/astra-quant-agent@GITHUB_SHA`, with required upstream ancestry, a narrow
   application delta guard, positive test discovery and council regressions.
   A separate checkout of official source would omit this patch and is prohibited.
5. Deploy only the smoke-tested immutable image after state/backward-compatibility
   and runtime gates pass. Record corrected source and upstream revision labels.

If upstream implements an equivalent repair, first show the retained behavioral
regression passes against that implementation. Retiring this patch or changing
the application allowlist is an explicit reviewed change; do not silently drop
the guard during a version bump. All builds and substantive tests run on hosted
Actions; production only pulls images and performs named no-build cutovers.
