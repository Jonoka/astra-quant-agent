# Local repair verification

Historical first-commit verification. Independent review and its numeric
boundary follow-up are recorded in boundary-verification.md; the original
evidence files below are preserved.

Base: deployed `f53e579b0db091f351f271f79ebbb99da1e6f7c2`; separate branch
`fix/okx-taker-parse-validation`. Both original checkouts remain clean at their
original heads (workspace f53e579; D:\vps checkout e4fe084). The protected release
archive retains SHA256 c0d46ca9f9fb2804df2bc785d503b01b0f53c4d8925374115ccc07e8a78be5c9.

The final offline run covers 16 affected suites: 398 tests, zero failures,
errors or skips. Parser tests include positive/negative/equal/true zero,
malformed columns, null/blank/bad/nonfinite/negative values, invalid latest rows,
aligned CVD history, overflow and both existing helper import styles. Runtime
tests cover primary packages, SmartMoney-to-factor overlays and full prompt,
factor assembly, source priority/fallback, decision cache and both dashboard
payloads. Existing complete affected module suites were also run.

Final log: `astra-taker-fix-verification/tests-20261006T102907731385Z.log` with
matching JSON metadata. The Linux runtime is an existing local dependency image
sha256:93c98c6c30499e41878915b443fa5d5cd8d7cb3eae12a2fd0326360337a6a5eb.
Source is mounted read-only at /source; scratch data is under /tmp. Docker uses
network=none, pull=never, and a read-only root filesystem. No image was built,
pulled or published. The parent test harness also rejects real network calls.

Some existing prompt extraction tests emit configuration-dependency warnings.
Their reads are of pinned local source-checkout files under /source, not live
VPS configuration. They are not all independently config-sandboxed. These
warnings do not prove current production behavior or a historical decision.

The old extraction comparison fails identically on untouched f53e579 (208 !=
203). Evidence: `baseline-guard-20261006T102416968669Z.json`. The active test
now fingerprints the entire deployed function, allowing only the exact new
four-line validated parser block in place of the old two assignments. The old
fixture and other extraction assertions remain intact.

A separate mechanical source/oracle pass verifies 1,011 cases against direct
Decimal buy-minus-sell arithmetic and an explicit changed-file allowlist.
Evidence: `source-review-20261006T102928590305Z.json`. Python compilation and
git diff --check pass. The implementing agent reviewed the diff separately;
no independent second person or separately delegated agent has approved it.
That independent review remains pending for the parent task.

Only four existing runtime consumers and one pure helper change. Request
routes/periods/retries, scheduler/deadline locks, credentials/configuration,
trade/risk thresholds, prompt template/source priority, cache/backend source,
request IDs, snapshot expiry and deployment helpers are unchanged. SmartMoney
missing notional is now None rather than a fabricated zero; real zero survives.

Full prompt testing corrects the earlier isolated-getter interpretation:
an entirely absent factor snapshot omits T0.5 and shows a missing-snapshot
notice. The legacy taker fallback renders only when some tiers remain but the
field is absent/None/empty. Explicit -- stays missing. Prompt code is unchanged.

This is local-only and unshipped: no push, PR, merge, image publication,
deployment, restart, order, or new live market/model request. Request-ID and
snapshot-age work are still paused. Corrected signs may influence future
model judgments; they do not establish the cause of historical WAIT decisions.
