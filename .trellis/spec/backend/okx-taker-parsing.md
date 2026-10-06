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
