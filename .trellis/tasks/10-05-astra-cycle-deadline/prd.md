# Astra cycle deadlines and request correlation

The owner approved code repair and isolated mock tests after the October 5
read-only diagnosis. Deployment, push, paid inference and trading are excluded.

Acceptance: a scheduled trader cycle cannot consume the next 15-minute slot;
collection, council, seat retries and single-model fallback use remaining total
time, with persistence time reserved. Existing concurrency and trading/risk
semantics stay unchanged. Expired budgets must fail observably rather than
present stale output as a fresh success. Every skipped slot has a durable reason.
Request metadata correlates job, trigger, request start/completion, actual model,
provider request ID when available and safe error/status category. Preserve old
history timestamps and schemas through additive metadata only. Never log prompts,
responses, credentials, private URLs or personal identifiers in new telemetry.

Tests use temporary directories, mock clocks/network/processes and fake responses.
Cover slow upstream, 524, cancellation/retry, exhausted fallback, cross-slot
and singleton protection, normal council success, and metadata round trip.
