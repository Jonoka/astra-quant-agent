# Deadline repair release preparation

The owner approved continuing code repair and isolated verification. Preserve
b25a200 and retained council/OKX corrections. Prepare strict publication/source,
current-baseline, incremental gateway-schema and latest-state rollback gates.
Only synthetic credentials, temporary data and mocked model/network requests may
be used. Complete local checks supported by already available tooling; report
specific Linux/container capability gaps. Do not start stopped infrastructure,
change persistent permissions or switch environments to bypass a gap.

No push, PR, merge, remote workflow invocation, GHCR publication, production
inspection/mutation/restart, paid inference, real order or configuration changes.
Local commits are authorized. Exact reviewed source checks remain mandatory.

Acceptance: source guard accepts exactly the reviewed delta and rejects unknown
edits/missing retained fixes; baseline equals the accepted 90f9f3a source and
8b471e image; only the known additive gateway schema is allowed; synthetic forward
upgrade and old-reader/writer rollback preserve newest writable records/config;
new regression/Linux lock/candidate smoke gates are wired with nonempty positive
test discovery. Unsupported live-image/Linux checks remain pending, never passed.
