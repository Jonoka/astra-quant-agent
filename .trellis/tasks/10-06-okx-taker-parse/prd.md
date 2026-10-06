# OKX taker parsing correction

Approved local-only repair from deployed source f53e579. OKX rows are
`[timestamp, sellVol, buyVol]`; primary and SmartMoney parsing invert net flow,
and factor parsing accepts malformed rows as numeric zero.

Accept buy-minus-sell consistently in all three paths. Reject missing columns,
null/empty/bad/nonfinite/negative volumes and preserve genuine zero. Preserve
existing fields, formatting, public request routes, periods, retries, and trade
conditions. Verify model factor priority/fallback, cache and backend payload
semantics with offline synthetic data and complete affected module regressions.

Only local code, tests and commit are authorized. No push, PR, merge, image
build/publish, deployment, production changes, or new live requests. Snapshot
age thresholds and request-ID changes are excluded. No historical WAIT causal
claim or same-response historical recomputation is supported.
