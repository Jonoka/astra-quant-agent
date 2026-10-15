"""High-Performance Zero-Process Direct Public Market Data Service (market_data_service.py).

Public market data harvesting (tickers, orderbooks, indicators, candles) runs on
persistent connection-pooled HTTP Keep-Alive sessions with pure-Python fallbacks.
Failover chain: openapi.okx.com -> www.okx.com -> local math.
Zero process-spawning layers; public endpoints need no credentials.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.timeout import Timeout

from astra_backend.collection import endpoint_timing
from astra_backend.deadline import (DeadlineExceeded, check_deadline,
                                    current_deadline, sleep_with_deadline)

logger = logging.getLogger("market_data_service")

try:
    from scripts.okx_public import OKX_PUBLIC_HOSTS, validate_public_path
except ImportError:                                    # pragma: no cover - script import
    from okx_public import OKX_PUBLIC_HOSTS, validate_public_path

# 行情取数可观测性（第 137 刀）：失败计数 + 耗时/成功率。
# ⚠️ 双拼写铁律：本模块既以顶层名 `market_data_service`（SCRIPTS_DIR 在 sys.path 上，
# 生产/门面即如此）被导入，也以 `scripts.market_data_service`（仓库根在 sys.path 上，
# 部分测试即如此）被导入 —— 两种拼写是**不同的模块实例**，故必须写成 try/except 兼容
# 两种路径（scripts/README.md §双拼写）。
try:
    from market_data_health import note_call, note_failure
except ImportError:                                    # pragma: no cover - 包导入路径
    from scripts.market_data_health import note_call, note_failure


def _call_kind(method: str, path: str) -> str:
    """把请求路径归一成**有界**的指标 kind（`okx_public_get_ticker` 之类）。

    绝不把整条 URL/带参数的路径当标签：那会让基数随调用爆炸（Prometheus 反模式），
    也会把 query 里的内容（instId/limit…）带进监控面。OKX 公共路径是固定小集合
    （ticker/candles/…），仅允许固定端点的尾段；未知端点归为 unknown。
    """
    clean = str(path or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    fixed = {
        "/api/v5/market/ticker", "/api/v5/market/tickers",
        "/api/v5/market/books", "/api/v5/market/candles",
        "/api/v5/aigc/mcp/indicators", "/api/v5/public/funding-rate",
        "/api/v5/public/open-interest",
    }
    tail = clean.rsplit("/", 1)[-1] if clean in fixed else "unknown"
    return f"okx_public_{method}_{tail}"


class MarketDataResponseError(RuntimeError):
    """交易所**回了话但没给数据**（HTTP≠200 或 code≠0）。

    它不是 Python 异常，但语义上同样是"这次取数失败"：调用方拿不到数据。
    第 137 刀的事故里正是这种"没有异常、只是没数据"的情形最难察觉，
    所以它必须与真异常**进同一本失败账**（否则指标里 failed_calls 与 failures
    两个口径会对不上，运维一眼看到两个不同的数就会失去信任）。
    """


class _PublicRequestOutcome:
    """One logical call's evidence, never a host cache or shared circuit breaker.

    Only exhaustive endpoint failures may skip equivalent indicator requests.
    Missing, malformed, or application-error data does not establish that.
    """

    def __init__(self):
        self.endpoint_exhausted = False


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

_SESSION: Optional[requests.Session] = None
_BOUNDED_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = threading.Lock()

# OKX bar 合法字面量（大小写敏感：分钟小写 m，小时/天/周/月大写）
_OKX_VALID_BARS = {
    "1m", "3m", "5m", "15m", "30m",
    "1H", "2H", "4H", "6H", "12H",
    "1D", "3D", "1W", "1M", "3M",
    "1Hutc", "4Hutc", "1Dutc", "1Wutc", "1Mutc", "1Dutc8", "1Wutc8", "1Mutc8",
}


def normalize_bar(bar: str) -> str:
    """Normalize K线周期到 OKX 合法字面量（大小写容错：1h→1H、4h→4H、15M→15m）。

    OKX /market/candles 与 indicators 接口对 bar/timeframe 参数严格区分大小写：
    小写 1h/4h 一律报 Parameter bar error，导致「1H/4H 数据拿不到」——
    所有入口统一先过这里。已是合法值（含 1M 月份大写特例）原样返回。
    """
    raw = str(bar or "").strip()
    if not raw:
        return "15m"
    if raw in _OKX_VALID_BARS:
        return raw
    low = raw.lower()
    for unit, canon in (("h", "H"), ("d", "D"), ("w", "W")):
        if low.endswith(unit) and low[: -1].isdigit():
            return low[: -1] + canon
    if low.endswith("m") and low[: -1].isdigit():
        n = int(low[: -1])
        if n in (1, 3, 5, 15, 30):
            return f"{n}m"
    return raw  # 未知/非法字面量交给 OKX 报错，不在本地臆造


def get_market_session() -> requests.Session:
    """Keep ordinary clients' retry policy; budgeted reads own their retries."""
    global _SESSION, _BOUNDED_SESSION
    bounded = current_deadline() is not None
    session = _BOUNDED_SESSION if bounded else _SESSION
    if session is None:
        with _SESSION_LOCK:
            check_deadline()
            session = _BOUNDED_SESSION if bounded else _SESSION
            if session is None:
                session = requests.Session()
                session.headers.update(DEFAULT_HEADERS)
                # urllib3 cannot recompute the remaining shared budget before
                # retries/Retry-After. The deadline path retries explicitly below.
                retries = Retry(total=0) if bounded else Retry(
                    total=2, backoff_factor=0.35,
                    status_forcelist=[429, 500, 502, 503, 504],
                    raise_on_status=False,
                )
                adapter = HTTPAdapter(pool_connections=12, pool_maxsize=24,
                                      max_retries=retries, pool_block=False)
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                if bounded:
                    _BOUNDED_SESSION = session
                else:
                    _SESSION = session
    check_deadline()
    return session


def _retry_delay(response, attempt: int) -> float:
    """Match urllib3's retry delay without permitting an unbounded sleep."""
    raw = response.headers.get("Retry-After") if response is not None else None
    if raw:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            from email.utils import parsedate_to_datetime
            try:
                return max(0.0, parsedate_to_datetime(str(raw)).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return 0.0 if attempt == 0 else 0.7


def _public_request(method: str, path: str, *, params=None, payload=None,
                    timeout: float,
                    _outcome: Optional[_PublicRequestOutcome] = None) -> Optional[Dict[str, Any]]:
    if _outcome is not None:
        _outcome.endpoint_exhausted = False
    validate_public_path(path)
    check_deadline()
    session = get_market_session()
    kind = _call_kind(method.lower(), path)
    bounded = current_deadline() is not None
    endpoint_failures_only = True
    exhausted_hosts = set()
    for base in OKX_PUBLIC_HOSTS:
        for attempt in range(3 if bounded else 1):
            check_deadline()
            started = time.monotonic()
            retry_response = None
            retry = False
            endpoint_failure = False
            received_response = False
            try:
                with endpoint_timing(method, path, base) as trace:
                    limit = min(timeout, check_deadline())
                    options = {"timeout": Timeout(total=limit, connect=limit, read=limit)
                               if bounded else limit}
                    if bounded:
                        # Redirects are additional requests and must not renew
                        # the budget inside requests' own redirect loop.
                        options["allow_redirects"] = False
                    if method == "GET":
                        resp = session.get(f"{base}{path}", params=params, **options)
                    else:
                        resp = session.post(f"{base}{path}", json=payload,
                                            headers={"Content-Type": "application/json"},
                                            **options)
                    received_response = True
                    check_deadline()
                    if resp.status_code == 200:
                        data = resp.json()
                        check_deadline()
                        if str(data.get("code", "0")) == "0":
                            trace["result"] = "ok"
                            note_call(kind, time.monotonic() - started, ok=True)
                            return data
                        trace["result"] = "code_error"
                        code = str(data.get("code", ""))
                        reason = ("code=" + code
                                  if code.isascii() and code.isdigit() and len(code) <= 6
                                  else "code_error")
                    else:
                        trace["result"] = f"http_{resp.status_code}"
                        # Explicit allowlist: content/compatibility errors such
                        # as 400/422 (and unknown statuses) still try singles.
                        endpoint_failure = resp.status_code in {403, 429, 500, 502, 503, 504}
                        retry = bounded and resp.status_code in {429, 500, 502, 503, 504}
                        retry_response = resp
                        reason = f"http={resp.status_code}"
                    note_call(kind, time.monotonic() - started, ok=False)
                    # Health's once-only warning prints its exception. Supply a
                    # fixed label or numeric exchange code, never msg/query/body.
                    note_failure(kind, MarketDataResponseError(reason))
            except DeadlineExceeded:
                note_call(kind, time.monotonic() - started, ok=False)
                note_failure(kind, DeadlineExceeded())
                raise
            except Exception as exc:
                check_deadline()
                note_call(kind, time.monotonic() - started, ok=False)
                note_failure(kind, RuntimeError(type(exc).__name__))
                endpoint_failure = not received_response and isinstance(
                    exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))
                retry = bounded and isinstance(
                    exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))
            endpoint_failures_only = endpoint_failures_only and endpoint_failure
            if not retry or attempt == 2:
                break
            sleep_with_deadline(_retry_delay(retry_response, attempt))
        exhausted_hosts.add(base)
    check_deadline()
    if _outcome is not None:
        _outcome.endpoint_exhausted = endpoint_failures_only and len(exhausted_hosts) >= 2
    return None


def _public_get(path: str, params: Optional[Dict[str, Any]] = None,
                timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    """Try primary then fallback OKX public endpoints within one budget."""
    return _public_request("GET", path, params=params, timeout=timeout)


def _public_post(path: str, payload: Dict[str, Any],
                 timeout: float = 4.0, *,
                 _outcome: Optional[_PublicRequestOutcome] = None) -> Optional[Dict[str, Any]]:
    """Try primary then fallback OKX public POST endpoints within one budget."""
    if _outcome is not None:
        return _public_request("POST", path, payload=payload, timeout=timeout,
                               _outcome=_outcome)
    return _public_request("POST", path, payload=payload, timeout=timeout)


# ---------------------------------------------------------------------------
# 1. Ticker & Bulk Tickers
# ---------------------------------------------------------------------------

def fetch_ticker(inst_id: str, timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    """Fetch one instrument ticker: openapi→www 双域 REST 直连，失败返回 None。"""
    data = _public_get("/api/v5/market/ticker", params={"instId": inst_id}, timeout=timeout)
    if data and data.get("data"):
        return data["data"][0]
    return None


def fetch_tickers_bulk(inst_type: str = "SWAP", timeout: float = 4.0) -> Dict[str, Dict[str, Any]]:
    """Fetch all instrument tickers in ONE single network request."""
    data = _public_get("/api/v5/market/tickers", params={"instType": inst_type}, timeout=timeout)
    if data and data.get("data"):
        return {item["instId"]: item for item in data["data"] if "instId" in item}
    return {}


# ---------------------------------------------------------------------------
# 2. Orderbook Depth
# ---------------------------------------------------------------------------

def fetch_orderbook_depth(inst_id: str, sz: int = 5, timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    """Fetch orderbook depth directly via REST. Returns {'bids': [...], 'asks': [...]} —
    双域直连零进程，无备源（深度语义场所间不可比）。
    """
    data = _public_get("/api/v5/market/books", params={"instId": inst_id, "sz": sz}, timeout=timeout)
    if data and data.get("data"):
        return data["data"][0]
    return None


# ---------------------------------------------------------------------------
# 3. Technical Indicators (ADX, KDJ, BBWIDTH, CMF, RSI, etc.)
# ---------------------------------------------------------------------------

def _indicator_key(name: Any) -> str:
    """指标名的**唯一**规范化形态（大写、去掉 `-` 与 `_`）。

    为什么必须只有一处（第一百八十四刀）：本模块原来有**三种**写法 ——
    MCP 分支 `ind.upper().replace("-","")`、REST 兜底分支 `ind.upper()`（**保留横杠**）、
    本地计算/单指标 `ind.upper().replace("-","").replace("_","")`。
    ⇒ 同一个指标可能同时存在两种键（`"EMA20"` 与 `"EMA-20"`），后果有两个方向：
      - **读者取不到**：消费方按一种拼法取值、生产者按另一种存 ⇒ 静默"没有"（读不到≠没有）；
      - **`missing` 判定失明**：它按去横杠比较，于是 REST 已存 `"EMA-20"` 时仍判为缺失 ⇒
        每轮都白算一遍本地指标（浪费算力，且可能把同一指标写成两个键）。
    今天线上消费方用的名字（`adx`/`kdj`/`bbwidth`/`cmf`）都不带分隔符，所以没炸 —— 属**潜在**缺陷。
    """
    return str(name or "").upper().replace("-", "").replace("_", "")


def _local_math_indicators(
    inst_id: str,
    indicators: List[str],
    bar: str = "1H",
) -> Dict[str, Dict[str, str]]:
    """末级兜底：当 OKX MCP 指标接口与 REST 均不可用时（部署环境常见），
    用本地蜡烛（自带 openapi→www 双域容灾）纯 Python 计算 ADX/KDJ/BBWIDTH/CMF。
    输出与 OKX 官方口径对齐的字符串数值；样本不足时返回空 dict 让上层维持缺省。"""
    check_deadline()
    rows = fetch_candles(inst_id, bar=bar, limit=120)
    check_deadline()
    if not rows:
        return {}
    try:
        chron = list(reversed(rows))
        highs = [float(r[2]) for r in chron]
        lows = [float(r[3]) for r in chron]
        closes = [float(r[4]) for r in chron]
        vols = [float(r[5]) for r in chron]
    except (ValueError, IndexError):
        return {}

    result: Dict[str, Dict[str, str]] = {}
    for ind in indicators:
        check_deadline()
        key = _indicator_key(ind)
        try:
            if key == "ADX" and len(closes) >= 30:
                trs, pdms, ndms = [], [], []
                for i in range(1, len(closes)):
                    tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                    up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
                    trs.append(tr)
                    pdms.append(up if (up > dn and up > 0) else 0.0)
                    ndms.append(dn if (dn > up and dn > 0) else 0.0)
                p = 14
                atr = sum(trs[:p])
                pdm = sum(pdms[:p])
                ndm = sum(ndms[:p])
                dxs = []
                for i in range(p, len(trs)):
                    atr = atr - atr / p + trs[i]
                    pdm = pdm - pdm / p + pdms[i]
                    ndm = ndm - ndm / p + ndms[i]
                    pdi = 100.0 * pdm / atr if atr > 0 else 0.0
                    ndi = 100.0 * ndm / atr if atr > 0 else 0.0
                    denom = pdi + ndi
                    dxs.append(100.0 * abs(pdi - ndi) / denom if denom > 0 else 0.0)
                if len(dxs) >= p:
                    adx = sum(dxs[:p]) / p
                    for dx in dxs[p:]:
                        adx = (adx * (p - 1) + dx) / p
                    result["ADX"] = {"adx": f"{adx:.2f}"}
            elif key == "KDJ" and len(closes) >= 9:
                k = d = 50.0
                for i in range(8, len(closes)):
                    hh = max(highs[i - 8: i + 1])
                    ll = min(lows[i - 8: i + 1])
                    rsv = (closes[i] - ll) / (hh - ll) * 100.0 if hh > ll else 50.0
                    k = (2.0 * k + rsv) / 3.0
                    d = (2.0 * d + k) / 3.0
                j = 3.0 * k - 2.0 * d
                result["KDJ"] = {"k": f"{k:.2f}", "d": f"{d:.2f}", "j": f"{j:.2f}"}
            elif key in ("BBWIDTH", "BBANDWIDTH") and len(closes) >= 20:
                window = closes[-20:]
                mid = sum(window) / 20.0
                sd = (sum((x - mid) ** 2 for x in window) / 20.0) ** 0.5
                if mid > 0:
                    result["BBWIDTH"] = {"bbWidth": f"{(4.0 * sd / mid * 100.0):.2f}"}
            elif key == "CMF" and len(closes) >= 21:
                num = den = 0.0
                for i in range(-20, 0):
                    rng = highs[i] - lows[i]
                    mf = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / rng if rng > 0 else 0.0
                    num += mf * vols[i]
                    den += vols[i]
                result["CMF"] = {"cmf": f"{(num / den):.4f}" if den > 0 else "0.0000"}
        except DeadlineExceeded:
            raise
        except Exception:
            pass
    check_deadline()
    return result


def fetch_indicators_batch(
    inst_id: str,
    indicators: List[str],
    bar: str = "1H",
    timeout: float = 4.0,
) -> Dict[str, Dict[str, Any]]:
    """Fetch multiple technical indicators in ONE SINGLE HTTP POST request.
    
    Batches 4-6 per-instrument indicator queries into 1 fast call.
    Returns: {"ADX": {"adx": "20.1", ...}, "KDJ": {"k": "...", "d": "...", "j": "..."}, ...}
    """
    bar = normalize_bar(bar)
    ind_configs = {ind.upper(): {} for ind in indicators}
    payload = {
        "instId": inst_id,
        "timeframes": [bar],
        "indicators": ind_configs,
    }
    
    outcome = _PublicRequestOutcome()
    data = _public_post("/api/v5/aigc/mcp/indicators", payload, timeout=timeout,
                        _outcome=outcome)
    check_deadline()
    if outcome.endpoint_exhausted and indicators:
        # Both hosts already exhausted this call's retry policy. Compute the
        # whole set from one candle snapshot instead of repeating those same
        # endpoint failures and fetching identical candles per indicator.
        # A host could recover immediately afterward; the next logical call
        # probes normally. No failure state survives this invocation.
        local = _local_math_indicators(inst_id, indicators, bar)
        check_deadline()
        return local
    result: Dict[str, Dict[str, Any]] = {}
    
    if data and data.get("data"):
        try:
            tfs = data["data"][0].get("data", [{}])[0].get("timeframes", {}).get(bar, {}).get("indicators", {})
            for ind in indicators:
                check_deadline()
                key = _indicator_key(ind)
                items = tfs.get(key, [])
                if items and isinstance(items[0], dict):
                    result[key] = items[0].get("values", {})
            if result:
                check_deadline()
                return result
        except DeadlineExceeded:
            raise
        except Exception:
            check_deadline()
    
    check_deadline()
    # If MCP endpoint failed, fallback to querying individual indicator via REST or local math
    for ind in indicators:
        check_deadline()
        key = _indicator_key(ind)
        if key not in result:
            val = fetch_single_indicator(inst_id, ind, bar=bar, timeout=timeout)
            check_deadline()
            if val:
                result[key] = val
    
    # 末级兜底：MCP 批量接口与逐指标 REST 全灭 → 本地蜡烛纯 Python 计算
    missing = [ind for ind in indicators if _indicator_key(ind) not in result]
    if missing:
        for k, v in _local_math_indicators(inst_id, missing, bar).items():
            check_deadline()
            result.setdefault(k, v)
    
    check_deadline()
    return result


def fetch_single_indicator(
    inst_id: str,
    indicator: str,
    bar: str = "1H",
    timeout: float = 3.5,
) -> Dict[str, Any]:
    """Fetch or compute a single indicator: MCP REST，失败落纯 Python 本地数学。"""
    key = _indicator_key(indicator)
    bar = normalize_bar(bar)
    payload = {
        "instId": inst_id,
        "timeframes": [bar],
        "indicators": {key: {}},
    }
    data = _public_post("/api/v5/aigc/mcp/indicators", payload, timeout=timeout)
    check_deadline()
    if data and data.get("data"):
        try:
            tfs = data["data"][0].get("data", [{}])[0].get("timeframes", {}).get(bar, {}).get("indicators", {})
            items = tfs.get(key, [])
            if items and isinstance(items[0], dict):
                values = items[0].get("values", {})
                check_deadline()
                return values
        except DeadlineExceeded:
            raise
        except Exception:
            check_deadline()
    check_deadline()
    # 末级兜底：本地蜡烛 + 纯 Python 数学（MCP 端点不可达时的最后防线）
    values = _local_math_indicators(inst_id, [key], bar).get(key, {})
    check_deadline()
    return values


# ---------------------------------------------------------------------------
# 4. Candles
# ---------------------------------------------------------------------------

def fetch_candles(
    inst_id: str,
    bar: str = "15m",
    limit: int = 45,
    timeout: float = 4.0,
) -> List[List[str]]:
    """Fetch candles directly from OKX Official Market REST API with Keep-Alive."""
    bar = normalize_bar(bar)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 45
    limit = max(1, min(limit, 300))  # OKX 单次上限 300，超限直接报错返回空
    data = _public_get(
        "/api/v5/market/candles",
        params={"instId": inst_id, "bar": bar, "limit": limit},
        timeout=timeout,
    )
    if data and data.get("data"):
        return data["data"]
    return []


# ---------------------------------------------------------------------------
# 5. Funding Rate & Open Interest
# ---------------------------------------------------------------------------

def fetch_funding_rate(inst_id: str, timeout: float = 3.5) -> Optional[float]:
    """Fetch current perpetual funding rate as percentage."""
    data = _public_get("/api/v5/public/funding-rate", params={"instId": inst_id}, timeout=timeout)
    if data and data.get("data"):
        try:
            return round(float(data["data"][0].get("fundingRate", 0.0)) * 100, 4)
        except (ValueError, TypeError):
            pass
    return None


def fetch_open_interest(inst_id: str, timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    """Fetch open interest data."""
    data = _public_get("/api/v5/public/open-interest", params={"instId": inst_id}, timeout=timeout)
    if data and data.get("data"):
        return data["data"][0]
    return None
