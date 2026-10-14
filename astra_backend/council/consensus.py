"""投委会报价单结构化解析与数理共识度算法（纯函数，无 I/O）。

本模块职责：
1. 校验并解析各交易员的 JSON 提案及兼容标准化报价单；
2. 逐标的计算方向共识度（Agreement Index，0.0~1.0）、加权综合置信度与席位分布；
3. 生成格式化【量化共识度矩阵卷宗 (Consensus Docket)】供 CIO 终审横向参考；
4. 识别多空严重对立的争议标的，为 Debate 对抗辩论模式提供精准靶向。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List

__all__ = [
    "parse_trader_proposal",
    "parse_trader_quote_table",
    "calculate_symbol_consensus",
    "calculate_council_consensus",
    "format_consensus_docket",
    "extract_disputed_symbols",
]


def _clean_token(text: str) -> str:
    """清理 markdown 加粗、反引号与首尾空格。"""
    return re.sub(r"[\*`]", "", text).strip()


_SYMBOL = re.compile(r"[A-Z0-9]{1,20}-USDT(?:-SWAP)?", re.IGNORECASE)
_ACTIONS = {"BUY_LONG", "SELL_SHORT", "WAIT"}
_TABLE_ACTIONS = {
    "BUY": "BUY_LONG", "LONG": "BUY_LONG", "多": "BUY_LONG", "做多": "BUY_LONG",
    "SELL": "SELL_SHORT", "SHORT": "SELL_SHORT", "空": "SELL_SHORT", "做空": "SELL_SHORT",
    "观望": "WAIT", "等待": "WAIT",
}
_PRICE_ALIASES = {
    "limit_price": ("entry_price", "limit_price"),
    "stop_loss": ("stop_loss_price", "stop_loss"),
    "take_profit": ("take_profit_price", "take_profit"),
    "margin": ("margin_usdt", "margin_usd", "margin"),
}
_MISSING = {"", "-", "—", "--", "n/a", "na", "null", "none"}


def _strict_object(pairs):
    """json.loads otherwise silently overwrites duplicate keys, including actions."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def _number(value, path, errors, *, table=False):
    if value is None:
        return None
    if table and isinstance(value, str):
        cleaned = re.sub(r"[\*`$%,]", "", value).strip()
        if cleaned.lower() in _MISSING:
            return None
        try:
            value = float(cleaned)
        except ValueError:
            pass
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{path}: expected a finite number or null")
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        value = float("inf")
    if not math.isfinite(value):
        errors.append(f"{path}: expected a finite number or null")
        return None
    return value


def _normalise_quote(symbol, item, path, errors, *, table=False):
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol.strip()):
        errors.append(f"{path}: invalid or missing symbol")
        return None
    symbol = symbol.strip().upper()
    if not isinstance(item, dict):
        errors.append(f"{path}: expected a decision object")
        return None
    for key in ("symbol", "instId"):
        if key in item and (not isinstance(item[key], str) or item[key].strip().upper() != symbol):
            errors.append(f"{path}.{key}: symbol conflicts with decision key")
    raw_action = item.get("action")
    action = raw_action.strip().upper() if isinstance(raw_action, str) else None
    if table:
        action = _TABLE_ACTIONS.get(action, action)
    if action not in _ACTIONS:
        errors.append(f"{path}.action: expected BUY_LONG, SELL_SHORT or WAIT")
        return None

    quote = {"symbol": symbol, "action": action}
    for target, aliases in _PRICE_ALIASES.items():
        values = [_number(item[key], f"{path}.{key}", errors, table=table)
                  for key in aliases if key in item]
        # Null is absence, not a conflicting alternate numeric value.
        present = [value for value in values if value is not None]
        if present and any(value != present[0] for value in present[1:]):
            errors.append(f"{path}.{target}: conflicting aliases")
        quote[target] = present[0] if present else None

    confidence = _number(item.get("confidence"), f"{path}.confidence", errors, table=table)
    if confidence is None:
        confidence = 50  # Preserve the legacy confidence default, never invent prices.
    if not 0 <= confidence <= 100:
        if table:
            confidence = max(0, min(100, confidence))
        else:
            errors.append(f"{path}.confidence: expected a value from 0 to 100")
    quote["confidence"] = int(confidence)
    quote["reason"] = ""
    for key in ("reasoning", "reason", "summary_reason"):
        if key in item:
            reason = item[key]
            if not isinstance(reason, str):
                errors.append(f"{path}.{key}: expected a string")
            elif not quote["reason"]:
                quote["reason"] = reason.strip()
    return quote


def _json_quotes(payload, errors):
    if isinstance(payload, dict) and "decisions" in payload:
        decisions = payload["decisions"]
    else:
        decisions = payload
    if isinstance(decisions, dict) and ("symbol" in decisions or "instId" in decisions):
        decisions = [decisions]
    if not isinstance(decisions, (dict, list)):
        errors.append("decisions: expected a symbol mapping or a list of decision objects")
        return []
    quotes = []
    entries = decisions.items() if isinstance(decisions, dict) else enumerate(decisions)
    for key, item in entries:
        path = f"decisions[{key}]"
        symbol = key if isinstance(decisions, dict) else (
            item.get("symbol", item.get("instId")) if isinstance(item, dict) else None)
        quote = _normalise_quote(symbol, item, path, errors)
        if quote is not None:
            quotes.append(quote)
    return quotes


def _legacy_quotes(text, errors):
    quotes = []
    saw_table = False
    for index, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if "|" not in line:
            continue
        parts = [_clean_token(part) for part in line.strip("|").split("|")]
        if not parts or not _SYMBOL.fullmatch(parts[0]):
            continue
        saw_table = True
        if len(parts) < 6:
            errors.append(f"line {index}: incomplete quote table row")
            continue
        item = dict(zip(("symbol", "action", "limit_price", "stop_loss", "take_profit",
                         "margin", "confidence", "reason"), parts))
        quote = _normalise_quote(parts[0], item, f"line {index}", errors, table=True)
        if quote is not None:
            quotes.append(quote)
    # Only explicit symbol/action lines are proposals. Narrative mentioning a
    # symbol, an unknown action or a JSON string must never become a WAIT vote.
    pattern = re.compile(
        r"^\s*(?:[-*]\s+)?([A-Z0-9]{1,20}-USDT(?:-SWAP)?)\s*[:：]\s*([^\s,，。;；]+)",
        re.IGNORECASE,
    )
    for index, line in enumerate(text.splitlines(), 1):
        match = pattern.match(line)
        if match:
            quote = _normalise_quote(match.group(1), {"action": match.group(2)},
                                     f"line {index}", errors, table=True)
            if quote is not None:
                quotes.append(quote)
    return quotes, "table" if saw_table else "text"


def parse_trader_proposal(proposal_text: str, expected_symbols=None) -> Dict[str, Any]:
    """Parse JSON or legacy quotes without turning malformed output into WAIT.

    ``valid`` checks structure, field types, duplicate conflicts and optional
    exact symbol coverage, not trade eligibility. Missing trade prices/margin remain
    None for the caller's semantic validation. On invalid input ``quotes`` can
    contain diagnostic rows; callers must check ``valid`` before using them.
    JSON accepts the deployed ``decisions`` mapping, a bare symbol mapping, or
    explicit symbol/instId objects in a list. Only finite JSON numbers are
    accepted; legacy tables additionally accept formatted numeric strings.
    """
    errors = []
    quotes = []
    format_name = "unknown"
    if not isinstance(proposal_text, str) or not proposal_text.strip():
        errors.append("proposal: expected non-empty text")
    else:
        text = proposal_text.strip()
        # One closed JSON fence may be surrounded by analysis. Never recover a
        # truncated JSON object by scanning any embedded symbol/action text.
        raw_json = text.startswith(("{", "["))
        fences = list(re.finditer(r"```(?:json)?\s*\n?(.*?)```", text, re.IGNORECASE | re.DOTALL))
        json_fences = [match for match in fences if match.group(1).strip().startswith(("{", "["))]
        json_like = (raw_json or bool(json_fences)
                     or bool(re.search(r'```json\b|"decisions"\s*:|^\s*[\{\[]',
                                       text, re.IGNORECASE | re.MULTILINE)))
        if json_like:
            format_name = "json"
            try:
                source = text
                if not raw_json:
                    if text.count("```") % 2:
                        raise ValueError("unclosed JSON fence")
                    if len(json_fences) > 1:
                        raise ValueError("multiple JSON proposal blocks")
                    if json_fences:
                        match = json_fences[0]
                        outside = text[:match.start()] + text[match.end():]
                        if re.search(r"^\s*[\{\[]", outside, re.MULTILINE):
                            raise ValueError("multiple or truncated JSON proposal blocks")
                        source = match.group(1).strip()
                payload = json.loads(source, object_pairs_hook=_strict_object,
                                     parse_constant=_reject_constant)
                quotes = _json_quotes(payload, errors)
            except (ValueError, TypeError, RecursionError) as exc:
                errors.append(f"proposal: invalid JSON ({exc})")
        else:
            quotes, format_name = _legacy_quotes(text, errors)
    unique = {}
    for quote in quotes:
        symbol = quote["symbol"]
        if symbol in unique and unique[symbol] != quote:
            errors.append(f"{symbol}: conflicting duplicate decisions")
        else:
            unique[symbol] = quote
    quotes = list(unique.values())
    if not quotes and not errors:
        errors.append("proposal: no symbol decisions found")
    expected = set()
    if expected_symbols is not None:
        if not isinstance(expected_symbols, (list, tuple, set, frozenset)):
            errors.append("expected_symbols: expected a list, tuple or set of symbols")
        else:
            for symbol in expected_symbols:
                if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol.strip()):
                    errors.append("expected_symbols: invalid symbol")
                else:
                    expected.add(symbol.strip().upper())
    missing = sorted(expected - set(unique))
    if missing:
        errors.append("proposal: missing symbols: " + ", ".join(missing))
    unexpected = sorted(set(unique) - expected) if expected_symbols is not None else []
    if unexpected:
        errors.append("proposal: unexpected symbols: " + ", ".join(unexpected))
    return {"quotes": quotes, "valid": not errors, "status": "invalid" if errors else "ok",
            "errors": errors, "missing_symbols": missing, "unexpected_symbols": unexpected,
            "format": format_name}


def parse_trader_quote_table(proposal_text: str) -> List[Dict[str, Any]]:
    """Backward-compatible list API, now also accepting structured JSON proposals.

    A malformed proposal is rejected atomically so partial rows cannot bias the
    consensus. Use parse_trader_proposal for diagnostics and coverage checks.
    """
    parsed = parse_trader_proposal(proposal_text)
    return parsed["quotes"] if parsed["valid"] else []


def calculate_symbol_consensus(
    symbol: str,
    quotes_by_role: Dict[str, Dict[str, Any]],
    roles: Dict[str, Any],
) -> Dict[str, Any]:
    """计算单个标的在多个席位提案中的方向共识度与置信度。

    参数:
    - symbol: 标的 ID (例如 BTC-USDT-SWAP)
    - quotes_by_role: { role_id: quote_dict }
    - roles: { role_id: role_spec } (含 weight 与 name)
    """
    actions_count: Dict[str, int] = {"BUY_LONG": 0, "SELL_SHORT": 0, "WAIT": 0}
    weighted_votes: Dict[str, float] = {"BUY_LONG": 0.0, "SELL_SHORT": 0.0, "WAIT": 0.0}
    seat_details: List[Dict[str, Any]] = []

    total_weight = 0.0
    for r_k, q in quotes_by_role.items():
        act = q.get("action", "WAIT")
        if act not in actions_count:
            act = "WAIT"
        actions_count[act] += 1

        r_spec = roles.get(r_k, {})
        w = float(r_spec.get("weight", 0.3) or 0.3)
        total_weight += w
        weighted_votes[act] += w

        conf = int(q.get("confidence", 50))
        r_name = r_spec.get("name", r_k)
        seat_details.append({
            "role_id": r_k,
            "role_name": r_name,
            "action": act,
            "confidence": conf,
            "weight": w,
            "limit_price": q.get("limit_price"),
            "stop_loss": q.get("stop_loss"),
            "take_profit": q.get("take_profit"),
            "reason": q.get("reason", ""),
        })

    if total_weight <= 0:
        total_weight = 1.0

    # 确定主流倾向 (Dominant Action)
    sorted_actions = sorted(weighted_votes.items(), key=lambda x: x[1], reverse=True)
    dominant_action, dominant_weight = sorted_actions[0]

    total_traders = len(quotes_by_role)
    agreement_score: float = 0.0
    status: str = "MODERATE_LEANING"

    # 计算方向共识度
    if total_traders > 0:
        dom_ratio = dominant_weight / total_weight
        dom_count = actions_count[dominant_action]

        # 检查是否存在明确多空对立 (同时有多和空)
        has_opposite_conflict = (actions_count["BUY_LONG"] > 0 and actions_count["SELL_SHORT"] > 0)

        if dom_count == total_traders:
            agreement_score = 1.0
            status = "STRONG_CONSENSUS"
        elif has_opposite_conflict:
            agreement_score = max(0.2, min(0.5, round(dom_ratio * 0.5, 2)))
            status = "CONFLICT_SPLIT"
        elif dom_ratio >= 0.65:
            agreement_score = round(dom_ratio, 2)
            status = "MODERATE_LEANING"
        else:
            agreement_score = round(dom_ratio, 2)
            status = "CONFLICT_SPLIT"

    # 加权平均置信度
    weighted_conf_sum = sum(
        detail["confidence"] * detail["weight"] for detail in seat_details
    )
    weighted_confidence = int(round(weighted_conf_sum / total_weight)) if total_weight > 0 else 50

    # 生成文字摘要 (如: 稳健:多(82) / 动能:多(78) / 量化:多(75))
    dist_parts = []
    for d in seat_details:
        short_name = d["role_name"].split("(")[0].strip().replace("资深交易员 ", "")
        act_label = "多" if d["action"] == "BUY_LONG" else ("空" if d["action"] == "SELL_SHORT" else "观望")
        dist_parts.append(f"{short_name}:{act_label}({d['confidence']})")
    distribution_str = " / ".join(dist_parts)

    return {
        "symbol": symbol,
        "dominant_action": dominant_action,
        "agreement_score": round(agreement_score, 2),
        "weighted_confidence": weighted_confidence,
        "status": status,
        "actions_count": actions_count,
        "distribution_summary": distribution_str,
        "seat_details": seat_details,
    }


def calculate_council_consensus(
    trader_proposals: Dict[str, Dict[str, Any]],
    roles: Dict[str, Any],
) -> Dict[str, Any]:
    """汇总全部交易员提案，生成整套标的共识度矩阵。"""
    quotes_by_symbol: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for r_k, prop in trader_proposals.items():
        if not isinstance(prop, dict) or prop.get("status", "ok") != "ok":
            continue
        content = prop.get("content", "")
        parsed_quotes = parse_trader_quote_table(content)
        for q in parsed_quotes:
            sym = q["symbol"]
            if sym not in quotes_by_symbol:
                quotes_by_symbol[sym] = {}
            quotes_by_symbol[sym][r_k] = q

    consensus_by_symbol: Dict[str, Dict[str, Any]] = {}
    disputed_symbols: List[str] = []
    strong_consensus_symbols: List[str] = []

    for sym, role_quotes in sorted(quotes_by_symbol.items()):
        metrics = calculate_symbol_consensus(sym, role_quotes, roles)
        consensus_by_symbol[sym] = metrics

        if metrics["status"] == "CONFLICT_SPLIT":
            disputed_symbols.append(sym)
        elif metrics["status"] == "STRONG_CONSENSUS" and metrics["dominant_action"] != "WAIT":
            strong_consensus_symbols.append(sym)

    return {
        "by_symbol": consensus_by_symbol,
        "disputed_symbols": disputed_symbols,
        "strong_consensus_symbols": strong_consensus_symbols,
        "total_symbols_evaluated": len(consensus_by_symbol),
    }


def extract_disputed_symbols(consensus_data: Dict[str, Any]) -> List[str]:
    """提取需要进入 Debate 模式对抗辩论的争议标的清单。"""
    disputed = consensus_data.get("disputed_symbols", [])
    if isinstance(disputed, list):
        return [str(s) for s in disputed]
    return []


def format_consensus_docket(consensus_data: Dict[str, Any]) -> str:
    """生成嵌入 CIO 卷宗的【量化共识度与报价对比矩阵】。"""
    by_sym = consensus_data.get("by_symbol", {})
    if not by_sym:
        return "【量化共识度矩阵】暂无有效标的报价提案数据"

    lines = [
        "====================================================",
        "【量化共识度矩阵 (Algorithmic Consensus Docket)】",
        "标的 | 综合倾向 | 共识度 | 加权置信度 | 席位分布 | 仲裁决策指引",
    ]

    for sym, data in sorted(by_sym.items()):
        act = data["dominant_action"]
        score = data["agreement_score"]
        conf = data["weighted_confidence"]
        status = data["status"]
        dist = data["distribution_summary"]

        pct_str = f"{int(score * 100)}%"
        if status == "STRONG_CONSENSUS":
            guidance = "全员强共识；若开仓应优先采纳，严格核准点位与止损止盈"
            tag = f"{pct_str} (强共识)"
        elif status == "CONFLICT_SPLIT":
            guidance = "多空撕裂分歧；审阅攻防论据，无压倒性胜率时建议审慎观望 WAIT"
            tag = f"{pct_str} (严重分歧)"
        else:
            guidance = "偏向共识；参考主流倾向并核验反向交易员指出之风险"
            tag = f"{pct_str} (相对多数)"

        lines.append(f"{sym} | {act} | {tag} | {conf} | {dist} | {guidance}")

    return "\n".join(lines)
