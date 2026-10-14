"""Replay saved proposal text locally. Never calls models or trading endpoints.

Input JSON: {"provenance": "...", "rounds": [{"cycle_id": "...",
"expected_symbols": ["BTC-USDT-SWAP"], "proposals": {"seat": "raw text"}}]}.
Only validity/counts enter the report; raw model content stays in the input.
This checks parsing and proposal completeness, not signal profitability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astra_backend.council.consensus import calculate_council_consensus
from astra_backend.council.debate import _validate_proposal


def replay_proposals(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("rounds"), list):
        raise ValueError("input must contain a rounds list")
    report = {"provenance": payload.get("provenance", "unspecified"),
              "scope": "offline proposal contract only; no LLM calls, orders or profitability claims",
              "round_count": 0, "proposal_count": 0, "valid_proposals": 0,
              "invalid_proposals": 0, "rounds": []}
    for row in payload["rounds"]:
        if not isinstance(row, dict) or not isinstance(row.get("proposals"), dict):
            raise ValueError("each round must contain a proposals mapping")
        expected = row.get("expected_symbols")
        if not isinstance(expected, list) or not expected:
            raise ValueError("each round requires non-empty expected_symbols for coverage validation")
        proposals, outcomes = {}, {}
        for seat, content in row["proposals"].items():
            validation = _validate_proposal(content, {"proposal_symbols": expected})
            valid = validation["valid"]
            proposals[seat] = {"status": "ok" if valid else "error", "content": content}
            outcomes[seat] = {"valid": valid, "format": validation["format"],
                              "errors": validation["errors"],
                              "missing_symbols": validation["missing_symbols"],
                              "parsed_symbols": [q["symbol"] for q in validation["quotes"]]}
            report["proposal_count"] += 1
            report["valid_proposals" if valid else "invalid_proposals"] += 1
        consensus = calculate_council_consensus(proposals, {})
        report["round_count"] += 1
        report["rounds"].append({"cycle_id": row.get("cycle_id"), "outcomes": outcomes,
                                 "consensus_symbol_count": consensus["total_symbols_evaluated"],
                                 "dominant_actions": {s: q["dominant_action"]
                                                      for s, q in consensus["by_symbol"].items()}})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="de-identified JSON fixture; read-only")
    args = parser.parse_args(argv)
    raw = args.input.read_bytes()
    report = replay_proposals(json.loads(raw))
    report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
