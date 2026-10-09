"""Local operator protocol. No public API or automatic broker reconciliation.

Metadata identifies the real process start. Source/image claims must be checked
independently by the host controller; this CLI cannot certify its own image.
The risk action is FUTURE read-only broker inspection, never invoked by startup.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import sqlite3
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from astra_backend.maintenance import AdmissionClosed, Binding, Identity, MaintenanceStore, RiskProof
from astra_backend.maintenance_runtime import (Runtime, process_instance, store_path,
                                               protocol_observed, startup_store, persist_enable_latch,
                                               unsupported_producers, start_paused_component)


def metadata(role, pid):
    result = Identity(role, process_instance(pid), os.environ.get("ASTRA_SOURCE_COMMIT", ""),
                      os.environ.get("ASTRA_IMAGE_REF", "")).as_dict()
    result["unsupported_producers"] = unsupported_producers()
    return result


def collect_risk(store, binding, broker, selected, expected_account_uid_sha256=None):
    """Query the actual demo account with GET only; missing fields are errors.

    The selected credential identity must already be independently verified by
    the operator and broker account configuration must return its actual UID.
    An environment mode flag alone never sets ``verified``.
    """
    expected = os.environ.get("ASTRA_VERIFIED_DEMO_ACCOUNT_UID", "")
    if expected_account_uid_sha256 is None and expected:
        expected_account_uid_sha256 = hashlib.sha256(expected.encode()).hexdigest()
    if (selected.mode != "demo" or not selected.simulated or not selected.configured
            or not isinstance(expected_account_uid_sha256, str) or len(expected_account_uid_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_account_uid_sha256)):
        raise ValueError("independently verified DEMO account UID required")
    configuration = broker.readonly_evidence("/api/v5/account/config", env=selected)
    uid = configuration[0].get("uid") if len(configuration) == 1 else None
    actual_hash = hashlib.sha256(str(uid).encode()).hexdigest() if uid else ""
    if actual_hash != expected_account_uid_sha256:
        raise ValueError("broker account identity differs from verified DEMO UID")
    positions = broker.readonly_evidence("/api/v5/account/positions", env=selected)
    for row in positions:
        if "pos" not in row:
            raise ValueError("position evidence missing size")
        if isinstance(row["pos"], bool) or not math.isfinite(float(row["pos"])):
            raise ValueError("position evidence contains an invalid size")
    pending = broker.readonly_evidence("/api/v5/trade/orders-pending", {"limit": "100"}, env=selected)
    # We only accept empty first pages. Any non-empty page is already unsafe,
    # so incomplete pagination can never be misrepresented as zero exposure.
    algo = []
    for kind in ("conditional", "oco", "trigger", "move_order", "iceberg", "twap"):
        algo.extend(broker.readonly_evidence("/api/v5/trade/orders-algo-pending",
                                  {"ordType": kind, "limit": "100"}, env=selected))
    state = store.status()
    proof = RiskProof(binding.operation_id, binding.generation, binding.plan_sha256,
                      binding.instances["backend"].source, binding.instances["backend"].image,
                      time.time(), "DEMO", True,
                      sum(abs(float(row["pos"])) > 0 for row in positions), len(pending), len(algo),
                      sum(str(row.get("state", "")) == "partially_filled" for row in pending),
                      sum(row["status"] == "PREPARED" for row in state["orders"])
                      + sum(row["kind"] == "broker-mutation" for row in state.get("activities", [])),
                      sum(row["status"] in {"UNKNOWN", "PARTIAL"} for row in state["orders"]))
    return {"proof": proof.as_dict(), "account_uid_sha256": actual_hash, "read_only": True}


class ReadOnlyEvidenceStore:
    """Inspect existing protocol records without initialization or expiry writes."""
    def __init__(self, path): self.path = Path(path)
    def status(self):
        if not self.path.is_absolute() or not self.path.is_file():
            raise ValueError("existing absolute maintenance database required")
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            if [tuple(row) for row in connection.execute("SELECT version FROM maintenance_meta")] != [(1,)]:
                raise ValueError("unsupported maintenance evidence database")
            return {"orders": [dict(row) for row in connection.execute("SELECT * FROM maintenance_orders")],
                    "activities": [dict(row) for row in connection.execute("SELECT * FROM maintenance_activities")],
                    "actors": [dict(row) for row in connection.execute("SELECT * FROM maintenance_actors")]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("metadata", "status", "poll", "is-normal", "mode", "should-exit",
                                          "begin-action", "finish-action", "start-paused", "risk"))
    parser.add_argument("--database", default=str(store_path()))
    parser.add_argument("--role", choices=("backend", "gateway", "watchdog-backend", "watchdog-gateway"))
    parser.add_argument("--pid", type=int, default=os.getpid())
    parser.add_argument("--activity")
    parser.add_argument("--binding-stdin", action="store_true")
    parser.add_argument("--account-uid-sha256")
    args = parser.parse_args(argv)
    try:
        observed = protocol_observed(args.database)
        if args.action == "mode":
            print("protocol" if observed else "legacy")
            return 0
        if args.action == "risk":
            if not args.binding_stdin or not args.account_uid_sha256:
                raise ValueError("risk requires externally verified binding stdin and approved DEMO account UID SHA256")
            if not observed or os.environ.get("ASTRA_MAINTENANCE_LEGACY_PROCESS") == "1":
                raise ValueError("legacy bootstrap/hot enrollment cannot collect protocol risk evidence")
            binding = Binding.from_dict(json.loads(sys.stdin.read()))
            identity = binding.instances["backend"]
            if (os.environ.get("ASTRA_SOURCE_COMMIT"), os.environ.get("ASTRA_IMAGE_REF")) != (identity.source, identity.image):
                raise ValueError("risk evidence source/image differs from verified backend binding")
            store = ReadOnlyEvidenceStore(args.database)
            actors = store.status()["actors"]
            live = {row["instance_id"]: json.loads(row["identity"]) for row in actors if row["online"]}
            if live != {item.instance_id: item.as_dict() for item in binding.instances.values()}:
                raise ValueError("risk binding differs from actual registered role context")
            from scripts import okx_rest
            from scripts.okx_runtime import current_environment
            print(json.dumps(collect_risk(store, binding, okx_rest, current_environment(),
                                          args.account_uid_sha256), sort_keys=True))
            return 0
        legacy_boot = os.environ.get("ASTRA_MAINTENANCE_LEGACY_PROCESS") == "1"
        if legacy_boot and observed:
            persist_enable_latch(args.database)
            raise ValueError("legacy process cannot hot-enroll or acknowledge")
        if not observed:
            if args.action == "metadata":
                print(json.dumps({"protocol": 0, "role": args.role,
                                  "instance_id": process_instance(args.pid), "hot_enrollment": False}))
                return 0
            if args.action in {"is-normal", "poll", "finish-action"}:
                return 0
            if args.action == "begin-action":
                print("legacy")
                return 0
            if args.action == "should-exit":
                return 75
            raise ValueError("protocol is not enabled; legacy bootstrap is blocked")
        if args.action == "metadata":
            print(json.dumps(metadata(args.role, args.pid), sort_keys=True))
            return 0
        if args.action in {"poll", "begin-action", "finish-action", "should-exit", "start-paused"} and args.role not in {
            "watchdog-backend", "watchdog-gateway"}:
            raise ValueError("application ACKs must come from their own runtime; CLI only controls watchdog roles")
        store = startup_store(Path(args.database))
        if args.action in {"status", "is-normal"}:
            state = store.status()
            if args.action == "status":
                print(json.dumps(state, sort_keys=True))
                return 0
            return 75 if state["fenced"] else 0
        claim = metadata(args.role, args.pid)
        claim.pop("unsupported_producers", None)
        runtime = Runtime(store, Identity.from_dict(claim))
        runtime.startup()
        if args.action == "should-exit":
            return 0 if runtime.shutdown_requested() else 75
        if args.action == "poll":
            return 75 if runtime.poll() else 0
        if args.action == "start-paused":
            print(start_paused_component(runtime))
            return 0
        if args.action == "begin-action":
            print(runtime.admit("supervisory-action"))
        elif args.action == "finish-action":
            if not args.activity:
                raise ValueError("exact action activity required")
            store.finish(args.activity, runtime.identity)
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"maintenance refused: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
