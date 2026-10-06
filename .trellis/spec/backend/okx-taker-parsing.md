# OKX taker row contract

Source: https://www.okx.com/docs-v5/en/#trading-statistics-rest-api-get-taker-volume

Rows have at least three columns: timestamp, sell volume, buy volume. The pure
shared parser returns (buy, sell) and consumers compute buy minus sell. Volumes
must be finite nonnegative numeric strings/numbers; booleans, null, blank/bad
strings and short rows are unavailable. Extra columns are allowed; timestamp
metadata is not a volume and is not interpreted by this arithmetic helper.

Do not replace an invalid latest row with older data. Missing scalar factors
remain None, string factors -- (primary legacy N/A); a malformed aligned
divergence window remains INSUFFICIENT_DATA. Genuine zero stays numeric zero.
SmartMoney missing numeric net is None, and overlays must preserve it.

Validate the original decimal token's structure/sign/finiteness before float
conversion. Nonzero values that convert to float zero are unavailable, as are
nonfinite float magnitudes. Signed mathematical zero remains valid. String
volumes and JSON integers retain all input digits; JSON floats can preserve
only their already-decoded shortest decimal representation.

Subtract and sum original Decimal volumes exactly, with precision derived from
their digit/exponent span and a carry allowance, not the default 28-digit context.
Decide divergence from the unrounded Decimal sum. Do not add an epsilon or new
direction threshold. Unsupported final magnitudes/underflow remain unavailable.
Convert only final numeric outputs to the existing floats, then retain existing
display rounding. A small valid flow may display zero at that existing precision.
Ratios also retain float output/rounding and remain missing if unrepresentable.

The float-pair parse_taker_row/latest_taker_volumes wrappers remain compatible
but must not be used for net arithmetic. Producers use the decimal/net helpers;
no Decimal object enters persisted snapshots, cache or backend JSON payloads.

Existing route/period/retry, formatting/unit labels, model source priority and
trade/risk thresholds remain unchanged. Corrected input signs can affect model
judgment; that is not evidence of a historical WAIT cause. Snapshot refresh and
request-cache TTLs are not a business maximum-age contract. This fix adds no
snapshot expiry policy or request-ID behavior.

Full prompt rendering matters: an entirely absent factor snapshot omits T0.5
and displays the snapshot-missing notice. Legacy taker fallback is rendered
only when some factor tiers remain but the flow field is absent/None/empty.
An explicit -- stays missing. Testing only the local getter is insufficient.

Required checks: valid positive/negative/equal volume, invalid structure/values,
no silent zero, original pure CVD scenarios, genuine zero in cache/prompt/backend,
factor preferred and legacy fallback, and existing affected module regressions.
