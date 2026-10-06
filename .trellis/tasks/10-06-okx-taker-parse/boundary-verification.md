# Independent-review numeric boundary follow-up

The independent review of local commit 25a0235 found F1 tiny negative underflow,
F2 exact-zero decimal cancellation producing a divergence, and F3 large-integer
operand conversion losing a unit difference. F2/F3 also reproduce on the
deployed baseline, but all three belong to this approved parsing/validation
repair and have been addressed. The branch remains local and unshipped.

Original independent report and failures remain untouched at
`C:/Users/Administrator/Documents/Codex/2026-10-06/task-3/review-evidence`.
Copies and SHA256 preservation checks are under the local
`astra-taker-boundary-fix-verification` evidence directory. Its unchanged
review_boundaries.py reproduced 21 tests/5 failures before the follow-up in
`independent-20261006T105214411216Z.log`.

Original decimal values now undergo structural, sign, finite-output and
nonzero-underflow validation. Exact subtraction and window summation derive
sufficient precision from input digits/exponents. Signed true zero is allowed;
invalid observations stay missing. Large representable operands can retain a
unit net difference. Exact-zero windows remain NONE, while small genuine
nonzero flow retains its direction without an epsilon threshold.

Float-pair helper wrappers remain available for compatibility. Arithmetic
consumers use decimal/net helpers, then convert only final outputs to existing
floats and retain existing string rounding. No Decimal object is persisted and
no cache/backend schema changes. Pre-existing coarse display rounding can still
display a small valid flow as zero; this repair does not change its format.
JSON-decoded floats cannot recover lost original numeric tokens; OKX decimal
strings and integers remain exact. Price validation/time alignment is unchanged.

The final replay combines the original 16 suites/398 tests, the unchanged
reviewer suite/21 tests, and 13 new precision regression tests: **432 passing,
zero failures/errors/skips**. Log: `independent-20261006T105718904125Z.log`,
SHA256 413a461b4a3158f10adb65d04b6fde2b93eceac9909a4da488eea191d3f08d09.
The new tests also cover >28-digit precision, positive underflow, underflow
after subtraction, ratio overflow/underflow, ambient Decimal precision/traps,
genuine signed zero, and float/None JSON compatibility.

The existing local Linux dependency image is unchanged. Runs use network=none,
pull=never, read-only source/root filesystem and tmpfs scratch/source data.
The only source-data fixture is the pinned baseline prompt template copied by
the reviewer; credentials/configuration are not used. Existing test audit
warnings about prompt fixture reads persist and refer to this isolated tmpfs,
not live production configuration. No image build/pull/publish occurred.

The first new run had one failing new assertion expecting `1.0 U` where the
existing factor format is `1 U`; the assertion was corrected to preserve that
format, with no formatting code change. The complete rerun passed. All earlier
failure logs, including that one, are retained.

An execution transport disconnect prevented the first test command from
creating a process; retry succeeded. No automatic approval review rejected an
action. No remote Git writes, production access/mutation, restart, deployment,
market/model/order requests, snapshot-age policy, request-ID work or trade/risk
threshold changes occurred. Historical WAIT causality remains unproven.

Submit the follow-up local commit to the same independent review thread
`01a110c7-e3a3-744b-b61b-51bb96fb000e`. This implementing-agent replay is not
an independent re-review sign-off. The selected execution environment exposes
no cross-thread send_message capability; the parent task must relay the commit
and local evidence to that existing reviewer.
