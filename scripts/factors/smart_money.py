"""聪明钱与大户多空明细数据采集模块（Pillar 5）。

为解决 OKX 内部 CLI 移除后聪明钱数据缺失（US-014 遗留）的问题，本模块提供单源采集：
1. 数据源：OKX Rubik 官方公开统计端点（合约多空账户数/持仓比 + 深度成交量）；
2. 容灾：数据源不可用时优雅返回 None，保留显式缺失语义，绝不伪造虚假中性信号。
"""
from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

try:
    from scripts.okx_public import public_json_get
except ImportError:                                    # pragma: no cover - script import
    from okx_public import public_json_get

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def fetch_smart_money_for_symbol(
    ccy: str,
    price: float = 0.0,
    *,
    timeout: float = 3.5,
) -> Optional[Dict[str, Any]]:
    """采集单标的顶级大户持仓多空比与主动资金流（OKX Rubik 公开统计端点）。"""
    base_ccy = str(ccy or "").upper().strip()
    if not base_ccy:
        return None

    res_okx = _fetch_from_okx_rubik(base_ccy, price=price, timeout=timeout)
    if res_okx and res_okx.get("longShortRatio"):
        return res_okx

    return None


def _fetch_from_okx_rubik(ccy: str, price: float = 0.0, timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    w_long: Optional[float] = None
    ls_ratio: Optional[float] = None
    net_notional_usd = 0.0
    taker_str = "--"

    try:
        d = public_json_get(f"/api/v5/rubik/stat/contracts/long-short-pos-ratio?ccy={ccy}&period=5m",
                            opener=urllib.request.urlopen, timeout=timeout,
                            user_agent=_HEADERS["User-Agent"])
        if d.get("code") == "0" and d.get("data") and len(d["data"]) > 0:
            raw_ratio = float(d["data"][0][1])
            ls_ratio = raw_ratio
            w_long = round(raw_ratio / (1.0 + raw_ratio), 4)
    except Exception:
        pass

    if w_long is None:
        try:
            d2 = public_json_get(f"/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period=5m",
                                 opener=urllib.request.urlopen, timeout=timeout,
                                 user_agent=_HEADERS["User-Agent"])
            if d2.get("code") == "0" and d2.get("data") and len(d2["data"]) > 0:
                raw_ratio2 = float(d2["data"][0][1])
                ls_ratio = raw_ratio2
                w_long = round(raw_ratio2 / (1.0 + raw_ratio2), 4)
        except Exception:
            pass

    if w_long is None:
        return None

    try:
        d_t = public_json_get(f"/api/v5/rubik/stat/taker-volume?ccy={ccy}&instType=CONTRACTS&period=5m",
                              opener=urllib.request.urlopen, timeout=timeout,
                              user_agent=_HEADERS["User-Agent"])
        if d_t.get("code") == "0" and d_t.get("data") and len(d_t["data"]) > 0:
            b_vol = float(d_t["data"][0][1])
            s_vol = float(d_t["data"][0][2])
            net_notional_usd = b_vol - s_vol
            taker_str = (
                f"{round(net_notional_usd / 1e4, 1)}万 U"
                if abs(net_notional_usd) >= 1e4
                else f"{round(net_notional_usd, 0)} U"
            )
    except Exception:
        pass

    return {
        "longShortRatio": {
            "weightedLongRatio": w_long,
            "longShortRatio": ls_ratio,
        },
        "notional": {
            "netNotionalUsdt": net_notional_usd,
        },
        "winRate": {},
        "takerNetUsd": taker_str,
        "lsRatio": ls_ratio,
        "weighted_long_pct": round(w_long * 100, 1),
    }


def fetch_smart_money_pool(
    instruments: List[Dict[str, Any]],
    *,
    max_workers: int = 6,
    timeout: float = 3.5,
) -> Dict[str, Any]:
    """并发采集标的池内全部币种的聪明钱与大户数据，返回标准 pool 字典。"""
    pool: Dict[str, Any] = {}
    if not instruments:
        return pool

    def _fetch_one(item: Dict[str, Any]) -> tuple[str, Optional[Dict[str, Any]]]:
        ccy = item.get("ccy") or item.get("name") or (item.get("instId", "").split("-")[0] if "-" in item.get("instId", "") else "")
        price = float(item.get("price") or 0.0)
        res = fetch_smart_money_for_symbol(ccy, price=price, timeout=timeout)
        return ccy, res

    with ThreadPoolExecutor(max_workers=min(max_workers, len(instruments))) as executor:
        for ccy, sm in executor.map(_fetch_one, instruments):
            if ccy and sm:
                pool[ccy] = sm

    return pool
