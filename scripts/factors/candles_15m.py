"""15M K 线 → 技术指标（结构优化阶段 4·B3 第三十二刀）。

原样搬自 `scripts/factor_library.py::compute_instrument_factors` 的
「15M Candles → ATR, RSI, VWAP Bias, Vol Ratio, OBV」一段。

## 2026-10 变更：Pillar 6（微积分/定积分/概率）整段摘除

原实现紧随其后还有一段"Pillar 6: Calculus, Definite Integrals & Probability
Theory"，把 15M 的 `closes/highs/lows/vols` 喂给 `calculate_calculus` 并写回
`factors["calculus_dynamics"] / definite_integrals / probability_theory`。
该段已随**数理系统整体退场**删除（不再计算、不进提示词、不参与打分；
三个 Pillar 的键位在 `defaults.py` 里保留为 `retired: True` 的诊断占位）。

于是本模块的注入面**收缩为零**：不再需要 `calculate_calculus` 由调用方传入。

## ⚠️ 本模块**没有**取数依赖（这是刻意的）

`fetch_candles(...)` 留在门面，本模块只吃**已经取回的** `raw_candles`。
于是：

- **零新增注入面** —— 门面对 `fetch_candles` 的 `patch.object` 缝不受影响；
- 本模块可以纯函数式地测（喂构造 K 线即可），不需要任何 mock。

## 五处易错点（均原样保留）

1. **K 线是"倒序"的**：`reversed(raw_candles)` 转成时间正序之后再算。
   OKX 返回 newest-first，若忘了 reverse，ATR/RSI 全部反向计算 —— 而且
   **不会报错**，只会给出错误的数值。
2. **索引含义**：`c[2]=high, c[3]=low, c[4]=close, c[5]=volume`。
   把 2/3 写反（high/low 互换）在 `max(high-low, ...)` 里**看不出来**
   （绝对值对称），但会让 RSI 的 close 序列错位。
3. **ATR 是"简单移动平均"不是 Wilder 平滑**：`sum(tr_list[-14:]) / 14`。
4. **RSI 的 `avg_l == 0` 分支给 `rs = 100.0`**（不是除零）。
5. **`atr_pct` / `vwap_bias_pct` 都会再除以 `price`**：
   `price > 0` 与 `v_sum > 0` 是**两道独立的除零守卫**，不能合并。
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Tuple

__all__ = ["derive_candle_series", "compute_15m_indicators"]

#: K 线列索引（OKX: [ts, open, high, low, close, vol, ...]）
_COL_HIGH = 2
_COL_LOW = 3
_COL_CLOSE = 4
_COL_VOL = 5

# An evidence window, not a new trading rule. Never infer closure from position
# in the response ([-2]), a ticker price, or elapsed time without OKX confirm=1.
_BAR_MS = {"15m": 900_000, "1H": 3_600_000, "4H": 14_400_000}
_CLOSED_WINDOW = 3
_CANDLE_SOURCE = "OKX:/api/v5/market/candles"


def _timestamp_ms(value):
    if type(value) is str and value.isascii() and value.isdigit():
        try:
            value = int(value)
        except ValueError:
            return None
    return value if type(value) is int and value > 0 else None


def build_closed_candle_contract(raw_candles, *, inst_id, bar, observed_at_ms=None):
    """Keep at most three verified closed OHLCV rows, oldest first.

    All price/volume fields retain their source representation. Timestamps are
    UTC epoch milliseconds; end_ts_ms is derived from the documented bar width.
    STALE means the last completed interval is absent, not an invented age limit.
    Existing indicator calculations and execution gates do not consume this.
    """
    observed = _timestamp_ms(observed_at_ms if observed_at_ms is not None
                             else int(time.time() * 1000))
    duration = _BAR_MS.get(bar)
    result = {"schema": "okx_closed_candles_v1", "source": _CANDLE_SOURCE,
              "instId": inst_id, "bar": bar, "observed_at_ms": observed,
              "status": "MISSING", "available": False, "window_complete": False,
              "order": "oldest_first", "candles": [], "latest_end_ts_ms": None,
              "age_ms": None, "excluded": {"unconfirmed": 0, "unknown_confirm": 0,
                                             "invalid": 0}}
    if not isinstance(inst_id, str) or not inst_id or duration is None or observed is None:
        result["status"] = "INVALID_SOURCE"
        return result
    if not isinstance(raw_candles, (list, tuple)):
        result["status"] = "INVALID_SOURCE"
        return result
    if not raw_candles:
        return result
    candidates, seen, conflicts = {}, {}, set()
    for row in raw_candles:
        # Inconsistent rows for the same interval are not a choice of which
        # price/confirmation to trust, even if only one version says confirm=1.
        ts = _timestamp_ms(row[0]) if isinstance(row, (list, tuple)) and row else None
        if ts is not None:
            signature = [(type(value), value) for value in
                         list(row[:6]) + [row[8] if len(row) >= 9 else None]]
            if ts in seen and seen[ts] != signature:
                conflicts.add(ts)
                result["excluded"]["invalid"] += 1
            seen[ts] = signature
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            result["excluded"]["unknown_confirm"] += 1
            continue
        confirm = row[8]
        if type(confirm) not in (str, int) or confirm not in ("1", 1):
            kind = "unconfirmed" if type(confirm) in (str, int) and confirm in ("0", 0) else "unknown_confirm"
            result["excluded"][kind] += 1
            continue
        try:
            values = [float(value) if type(value) in (str, int, float) else float("nan")
                      for value in row[1:6]]
            op, high, low, close, volume = values
            valid = (ts is not None and ts % duration == 0 and ts + duration <= observed
                     and all(math.isfinite(value) for value in values)
                     and min(op, high, low, close) > 0 and volume >= 0
                     and low <= min(op, close) <= max(op, close) <= high)
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            result["excluded"]["invalid"] += 1
            continue
        candle = {"ts_ms": ts, "end_ts_ms": ts + duration,
                  "open": row[1], "high": row[2], "low": row[3],
                  "close": row[4], "volume": row[5], "confirm": confirm}
        candidates[ts] = candle
    rows = [candidates[ts] for ts in sorted(candidates) if ts not in conflicts][-_CLOSED_WINDOW:]
    result["candles"] = rows
    result["available"] = bool(rows)
    result["window_complete"] = (len(rows) == _CLOSED_WINDOW and all(
        right["ts_ms"] - left["ts_ms"] == duration for left, right in zip(rows, rows[1:])))
    result["status"] = "NO_CONFIRMED_CANDLES"
    if rows:
        end = rows[-1]["end_ts_ms"]
        result["latest_end_ts_ms"] = end
        result["age_ms"] = observed - end
        result["status"] = ("CONFIRMED_CURRENT" if end == observed // duration * duration
                            else "STALE")
    return result


def validate_closed_candle_contracts(contracts, *, inst_id, now_ms=None):
    """Recheck provenance, closure and age before prompt/journal transport.

    No fallback to recent_* (which lost confirm/timestamps), price, or a later
    factor-library snapshot. An absent contract remains explicitly missing.
    """
    now = _timestamp_ms(now_ms if now_ms is not None else int(time.time() * 1000))
    contracts = contracts if isinstance(contracts, dict) else {}
    output = {}
    for bar in _BAR_MS:
        source = contracts.get(bar)
        missing = build_closed_candle_contract([], inst_id=inst_id, bar=bar, observed_at_ms=now)
        if source is None:
            output[bar] = missing
            continue
        observed = _timestamp_ms(source.get("observed_at_ms")) if isinstance(source, dict) else None
        if (not isinstance(source, dict) or source.get("schema") != "okx_closed_candles_v1"
                or source.get("source") != _CANDLE_SOURCE or source.get("instId") != inst_id
                or source.get("bar") != bar or observed is None or now is None or observed > now
                or not isinstance(source.get("candles"), list)):
            missing["status"] = "INVALID_SOURCE"
            output[bar] = missing
            continue
        raw = [[row.get("ts_ms"), row.get("open"), row.get("high"), row.get("low"),
                row.get("close"), row.get("volume"), None, None, row.get("confirm")]
               if isinstance(row, dict) else [] for row in source["candles"]]
        checked = build_closed_candle_contract(raw, inst_id=inst_id, bar=bar, observed_at_ms=observed)
        # Preserve honest missing/invalid counts even after filtering at collection.
        excluded = source.get("excluded")
        if isinstance(excluded, dict):
            for key in checked["excluded"]:
                count = excluded.get(key)
                if type(count) is int and count >= 0:
                    checked["excluded"][key] += count
        if not checked["candles"] and any(checked["excluded"].values()):
            checked["status"] = "NO_CONFIRMED_CANDLES"
        checked["assessed_at_ms"] = now
        if checked["candles"]:
            end = checked["latest_end_ts_ms"]
            checked["age_ms"] = now - end
            checked["status"] = ("CONFIRMED_CURRENT" if end == now // _BAR_MS[bar] * _BAR_MS[bar]
                                 else "STALE")
        output[bar] = checked
    return output


def derive_candle_series(raw_candles: List[Any], *, safe_float
                         ) -> Tuple[List[float], List[float], List[float], List[float]]:
    """`raw_candles` → `(closes, highs, lows, vols)`，**时间正序**。

    ⚠️ `reversed(...)` 是必须的：OKX 返回 newest-first。忘了 reverse 不会报错，
    只会让 ATR/RSI/OBV 全部按反向时间计算。
    """
    closes = [safe_float(c[_COL_CLOSE]) for c in reversed(raw_candles)]
    highs = [safe_float(c[_COL_HIGH]) for c in reversed(raw_candles)]
    lows = [safe_float(c[_COL_LOW]) for c in reversed(raw_candles)]
    vols = [safe_float(c[_COL_VOL]) for c in reversed(raw_candles)]
    return closes, highs, lows, vols


def compute_15m_indicators(raw_candles: List[Any], factors: Dict[str, Any], *,
                           safe_float) -> Tuple[
                               List[float], List[float], List[float], List[float]]:
    """按原顺序写入 15M 指标，返回派生序列。

    调用方保证 `raw_candles` 非空且 `len >= 15`（门面里的判断是 `>= 15`）。
    """
    factors.setdefault("closed_candle_contract", {})["15m"] = build_closed_candle_contract(
        raw_candles, inst_id=factors.get("instId"), bar="15m")
    closes, highs, lows, vols = derive_candle_series(raw_candles, safe_float=safe_float)

    # ATR 14
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    if len(tr_list) >= 14:
        atr = sum(tr_list[-14:]) / 14
        factors["volatility_channel"]["atr_14"] = round(atr, 4)
        if factors["price"] > 0:
            factors["volatility_channel"]["atr_pct"] = round(atr / factors["price"] * 100, 2)

    # RSI 14
    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in diffs]
    losses = [-d if d < 0 else 0 for d in diffs]
    if len(gains) >= 14:
        avg_g = sum(gains[-14:]) / 14
        avg_l = sum(losses[-14:]) / 14
        rs = (avg_g / avg_l) if avg_l > 0 else 100.0
        factors["trend_momentum"]["rsi_14"] = round(100.0 - (100.0 / (1.0 + rs)), 1)
    else:
        # ★ 不足 14 根算不出 RSI ⇒ 显式缺失。默认块的 `rsi_14=50.0` 会被
        #   打分侧读成"中性偏多"（`rsi >= 50 ⇒ +15` 趋势分）—— 那 15 分是假的。
        factors["trend_momentum"]["rsi_14"] = None

    # VWAP Bias
    pv_sum = sum(closes[i] * vols[i] for i in range(len(closes)))
    v_sum = sum(vols)
    if v_sum > 0:
        vwap = pv_sum / v_sum
        factors["trend_momentum"]["vwap_bias_pct"] = round((factors["price"] - vwap) / vwap * 100, 2)
    else:
        # ★ 成交量合计为 0 ⇒ 算不出 VWAP：显式缺失，不用默认 0.0 冒充"正好贴在 VWAP 上"
        factors["trend_momentum"]["vwap_bias_pct"] = None

    # Vol Ratio（最后一根已收盘的量 vs 它前面 5 根均量；杜绝 vols[-1] 跳动未收盘 K 线把量比拉至 0.03）
    if len(vols) >= 7:
        avg_v5 = sum(vols[-7:-2]) / 5
        if avg_v5 > 0:
            factors["volume_money_flow"]["vol_ratio_15m"] = round(vols[-2] / avg_v5, 2)

    # OBV
    obv = 0
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]: obv += vols[i]
        elif closes[i] < closes[i-1]: obv -= vols[i]
    factors["volume_money_flow"]["obv_flow"] = "BULL_FLOW" if obv > 0 else ("BEAR_FLOW" if obv < 0 else "NEUTRAL")

    return closes, highs, lows, vols


def mark_15m_missing(factors: Dict[str, Any]) -> None:
    """15M K 线拿不到时，把 15M 派生的因子键**显式标缺失**。

    ⚠️ 2026-10「不许假数据」审计：门面的 15M 分支是
    `if d["data"] and len(...) >= 15: compute_15m_indicators(...)` **没有 else**
    ⇒ 取数失败时 `build_default_factors` 的默认值留在原地：
    `rsi_14=50.0`（打分侧 `rsi >= 50 ⇒ +15` **凭空加 15 分趋势分**）、
    `vwap_bias_pct=0.0`（"正好贴在 VWAP 上"）、`atr/atr_pct/vol_ratio/obv_flow`。
    这里与 `okx_quant_factors.mark_momentum_missing` 同纪律：数值 `None`、状态缺失。
    """
    factors.setdefault("closed_candle_contract", {})["15m"] = build_closed_candle_contract(
        [], inst_id=factors.get("instId"), bar="15m")
    tm = factors.setdefault("trend_momentum", {})
    for key in ("rsi_14", "vwap_bias_pct", "vol_ratio", "ema21_slope_pct"):
        tm[key] = None
    vc = factors.setdefault("volatility_channel", {})
    for key in ("atr_14", "atr_pct", "atr", "atr_15m"):
        vc[key] = None
    mf = factors.setdefault("volume_money_flow", {})
    mf["obv_flow"] = "INSUFFICIENT_DATA"


__all__ = ["derive_candle_series", "compute_15m_indicators", "mark_15m_missing",
           "build_closed_candle_contract", "validate_closed_candle_contracts"]
