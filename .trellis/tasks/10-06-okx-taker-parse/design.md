# Design

A small standard-library-only scripts.okx_taker module returns validated
(buy, sell) pairs. Primary packages, SmartMoney, factor snapshot assembly and
CVD arithmetic share it. No acquisition or facade imports enter the helper.
Invalid latest rows remain missing rather than silently selecting an older row.
Malformed rows in the aligned divergence window leave divergence unavailable.

SmartMoney netNotionalUsdt becomes None when taker data is unavailable; its
string stays --. The factor overlay preserves that missingness and guards only
numeric comparisons. Genuine zero retains the existing representation.
The numerical thresholds and valid-input signal rules are unchanged.

Prompt priority, caching schema and backend field precedence stay intact;
regressions verify corrected numeric meaning through their existing paths.
Existing display rounding/unit labels are retained, without a new unit or
time-bucket interpretation. No maximum age is invented for factor snapshots.
