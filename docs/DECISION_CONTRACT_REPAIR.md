# Decision/input contract repair (offline, 2026-10-14)

## Scope and provenance

Base: `60133ce82d4a0b91f94bc0c613a07e24aff049f5` (PR #8 merged).
This branch is a draft-PR repair candidate. It does not deploy, change runtime
configuration, change leverage/risk thresholds, or execute orders. PR #6 is not
included.

De-identified production observations supplied for this investigation reported
that the legacy consensus parser found no symbols in JSON proposals, and some
HTTP-success responses had empty or incomplete proposal content. The previous
CIO docket still contained the original proposal text: the failure was in the
structured consensus matrix, not proof that the CIO received no advice.

Original production response bytes and historical raw candles were not retained
in the supplied evidence. The committed fixture reconstructs the reported shape
using explicitly synthetic values and the 11-symbol pool. It is **not** a replay
of 105 original responses, a historical market backtest, or evidence of improved
trade frequency/profitability.

## Behavior

1. A completed model request and a valid proposal are separate facts.
   `transport_status=ok` with `semantic_status=invalid` is possible. Empty text,
   malformed/truncated JSON, missing/extra pool symbols, conflicting duplicates,
   invalid types/actions, and entry proposals missing positive prices or margin
   do not count as advisor success. They receive zero participation weight.
   Raw content remains in the audit transcript; the CIO and cross-examination
   docket receive an explicit invalid-proposal marker instead of that raw content.
   Semantic failure does not trigger an extra paid model request.
2. Complete JSON `decisions` mappings, supported aliases, JSON fences, and legacy
   quote tables can feed the same consensus calculation. No direction or weight
   algorithm was changed. A genuine all-WAIT proposal remains valid; an absent
   proposal is not a synthetic WAIT vote. Missing numeric entry data stays missing.
   The pre-existing confidence default of 50 is retained when omitted.
3. Existing candle fetches now retain a bounded, versioned evidence contract:
   instrument, source, bar, observation timestamp, up to three oldest-first raw
   OHLCV rows per 15m/1H/4H period, candle start/end UTC milliseconds, and the
   exchange confirmation flag. No extra market requests are added.
   Unconfirmed/future/misaligned/malformed/conflicting rows are excluded.
   The latest expected closed bucket, stale data and incomplete windows are
   explicitly distinguished and rechecked before prompt/journal rendering.
   Current closed data is evidence, not proof of a qualifying trade setup.
   Column semantics follow the [official OKX candle response](https://app.okx.com/docs-v5/en):
   `vol` is native market volume (contracts for derivatives, base currency for
   spot/margin), not quote-currency turnover or a proposed position size.
4. `proposal_symbols` carries canonical instrument IDs independently of display
   names. Existing Alpha handling and numerical indicators remain unchanged;
   missing Alpha or candle values are never manufactured.

## Reproduce

With the repository dependencies already available:

```sh
python .github/scripts/run_decision_contract_regressions.py
python scripts/replay_proposal_contract.py tests/data/proposal_contract_synthetic.json
git diff --check
```

The focused runner uses the repository's network/resource guard and reports test
counts, attempted egress, protected writes and configuration fingerprints. It is
not a full repository test run. Final local result: **463 tests passed**, with zero
network attempts, protected writes, or configuration fingerprint changes.
The extraction tests retain all old numerical
parity checks, excluding only the additive observation contract; new tests assert
the new field and data path independently.

Prompt-size check with the same 11 synthetic packages (99 closed rows): the
baseline market matrix was 8,172 characters / 13,430 UTF-8 bytes; the final compact
view is 23,957 characters / 29,679 bytes. The compact view avoids repeating the
full contract and saves 14,322 characters versus the first full-dictionary
rendering. This is a character/byte measurement, not a model-token or latency
benchmark. Provider output truncation is detected, not fixed by this change.

Synthetic replay: 3 rounds, 9 proposals, 5 valid and 4 invalid. Consensus symbol
counts are 11 / 0 / 11. All valid fixture decisions remain WAIT. There are no model
or order requests in the replay path. A future real replay requires saved,
de-identified original response bytes and the expected symbol list for each cycle.

An expanded legacy council run also has eight already-failing baseline tests
(deadline exact-value/budget assumptions and `seat_failed` text). The same eight
fail against the deployed baseline. They were not weakened to claim a full green
suite; detailed results are retained with the local validation evidence. The
focused contract gate is reported separately.

## Ordinary-PR verification and release boundary

The first PR run stopped before application tests with
`AssertionError: Missing or unreviewed release increment` in the historical
release source verifier. That verifier correctly rejected the new source delta;
its collection and release hashes describe previously reviewed historical
evidence and are not updated to make this ordinary source repair pass.

The existing PR workflow and preflight entry point now have an explicit
`--candidate-pr` mode, restricted to `pull_request` events. The independent
`pr_candidate_contract.json` pins the exact deployed baseline and tree, the
complete candidate delta, each predecessor/current SHA-256 and Git file mode.
Only the candidate contract and its verifier are self-hash exemptions; their
exact committed bytes require independent review and remote tree readback.
Unknown files, missing/deleted paths, wrong hashes/modes, dirty checkouts and
unexpected trust roots are rejected. Approval hashes are never derived from
the candidate at test runtime.

The candidate route runs the unchanged historical preflight on an isolated
checkout of `60133ce`, then the existing taker/Alpha/collection/retained gates
and the 463-test decision gate on the candidate. Focused runner and mode-boundary
refusal tests reject missing discovery, skipped tests, expected failures and
source tampering. Existing frontend checks and a final exact-source verification
remain in the same workflow. The original selected mode is retained for the
final check, so removing a contract cannot downgrade verification.

Neither historical source contract nor `build-release-image.yml` is changed.
The default preflight remains the original release verifier and cannot fall
back to candidate approval. Candidate success is marked
`release_eligibility=not_granted`: it is not release, deployment, or merge
authorization, and the historical release gate still rejects this candidate.

## Direction conflict requires a separate decision

The tracked factory prompt (`data/prompt_library.json`, base blob
`f7cb61e0213d634f6d5c4e5c138420136eccecbd`) contains both:

- Hard rule A: 1H MACD EXHAUSTING with Alpha <= 0 prohibits new positions.
- The short-entry gate requires BEAR_EXPANDING.
- A direction-table row says BEAR_EXHAUSTING with Alpha < 0 allows a short,
  while noting that the EXPANDING gate usually means WAIT.
- The Alpha >= 0 row attributes WAIT to rule A, even though positive Alpha
  does not satisfy that rule's threshold.

Two possible follow-on scopes, neither implemented:

1. Keep the existing hard rules and short-entry gate, and make the direction
   table/explanation consistent with them. This is a clarification proposal.
2. Deliberately permit a bearish-exhaustion entry under separately specified
   conditions. This needs an explicit strategy decision revising both rule A and
   the short-entry gate, followed by a separately approved evaluation.

Runtime effective prompts can merge `prompt_library.local.json` and an admin
override after module layout (`get_effective_system_prompt`). Matching factory
wording does not prove that the reported runtime policy hash came solely from the
factory file, and changing that file does not guarantee a live policy change.
Neither factory nor runtime prompt configuration is changed here.

## Remaining limits

No production installation, live LLM call, order submission or historical
profitability test was run. A three-candle evidence window does not manufacture
missing structure levels, longer lookbacks, position history or strategy
eligibility. Invalid/missing data can legitimately leave all decisions at WAIT.
The repair does not automatically increase output-token budgets, change model
selection, resolve the direction-policy conflict, or force trades.
