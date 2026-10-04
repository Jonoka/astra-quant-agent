# Retained Deployment Corrections

The deployment branch is `codex/deploy`; `main` follows upstream without local
application fixes. Development changes use separate `codex/*` branches before
integration into the deployment line.

## okx-public-domains-v1

The retained v8.6.1 deployment fork uses `https://openapi.okx.com` as the
ordered public market host and `https://www.okx.com` as its fallback. The
retired AWS host is absent from active unsigned market paths. This same-release
patch preserves private/authenticated endpoints, credentials, demo/live mode,
timeouts, parsers, caches, rate limits and missing-data/error propagation.

Owned application files are the OKX client/exchange/diagnostic adapters,
`scripts/okx_public.py`, market, factor, trader, ledger, news and backtest
consumers, their README comment, and the focused public-domain and market
fallback regressions. The hosted source guard enumerates this exact reviewed
delta together with `council-completion-v1`; it rejects any other application
file relative to upstream `e0b29fef1818e0ff9c6b210eb73234620e276a02`.

The release workflow builds the actual fork commit from `GITHUB_SHA`, and its
previous compatibility fixture is the deployed fork commit
`e4fe084fb67ef4060cf3478744ed2ed79308b893` with image
`ghcr.io/jonoka/astra-quant-agent@sha256:476179f0070987b06d48d6eefa76017ccfc90031de3eb754d60348e3efc3417d`.
Both sides remain v8.6.1 and gateway/database schemas and migrations must be
byte-equivalent; only writable records may advance during recovery.

Required hosted application suites include
`tests/venues/test_okx_public_domains.py`,
`tests/venues/test_market_data_service.py`,
`tests/venues/test_market_data_service_tails.py`,
`tests/venues/test_exchange_diagnostics_tails.py`,
`tests/core/test_exchange_listing_directory.py`, `tests/core/test_listing_gate.py`,
`tests/ops/test_brain_packages.py`,
`tests/venues/test_okx_public_data.py`, `tests/core/test_okx_client.py`,
`tests/core/test_news_sentiment_harvester.py`,
`tests/ops/test_factors_smart_money.py`, and the retained council and private
authentication/state suites. Public tests must exercise actual primary and
fallback requests and downstream returned data rather than constants.

The live unsigned listing directory follows the shared hosts; the demo directory
retains its original environment URL and simulation header. The runtime endpoint
gate requires v8.6.1 for both the candidate and previous fork, with a regression
covering preflight/recovery version and native authentication protection.

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
