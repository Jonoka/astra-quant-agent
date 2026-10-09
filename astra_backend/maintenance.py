"""DEMO maintenance protocol; explicit SQLite path, no application imports.

The controller supplies independently verified process/source/image identities.
This store serializes admission with the fence; it cannot establish OS liveness
or broker truth. Uncertain activities and orders are deliberately never reaped.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable, Mapping
from types import MappingProxyType
import uuid

PROTOCOL_VERSION = 1
REQUIRED_ROLES = ("backend", "gateway", "watchdog-backend", "watchdog-gateway")
MAX_BUDGET_SECONDS = 1200
PROOF_MAX_AGE_SECONDS = 30
ACK_MAX_AGE_SECONDS = 30
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA64 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}\Z")


class MaintenanceError(ValueError):
    """A missing or contradictory proof prevents a protocol transition."""


class AdmissionClosed(MaintenanceError):
    """The durable admission fence is closed."""


def _token(value: str, label: str) -> None:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise MaintenanceError("invalid " + label)


def _match(value: str, pattern: re.Pattern, label: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise MaintenanceError("invalid " + label)


def _number(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise MaintenanceError("invalid " + label)
    return float(value)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Identity:
    role: str
    instance_id: str
    source: str
    image: str
    protocol: int = PROTOCOL_VERSION

    def __post_init__(self):
        if self.role not in REQUIRED_ROLES:
            raise MaintenanceError("unsupported role")
        _token(self.instance_id, "process start identity")
        _match(self.source, _SHA40, "source")
        _match(self.image, _IMAGE, "immutable image")
        if type(self.protocol) is not int or self.protocol != PROTOCOL_VERSION:
            raise MaintenanceError("unsupported maintenance protocol")

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping) -> Identity:
        try:
            return cls(**value)
        except (TypeError, KeyError) as exc:
            raise MaintenanceError("invalid identity fields") from exc


@dataclass(frozen=True)
class Binding:
    operation_id: str
    plan_sha256: str
    previous_source: str
    previous_image: str
    target_source: str
    target_image: str
    generation: int
    instances: Mapping[str, Identity]

    def __post_init__(self):
        _token(self.operation_id, "operation")
        _match(self.plan_sha256, _SHA64, "external plan pin")
        for value in (self.previous_source, self.target_source):
            _match(value, _SHA40, "source")
        for value in (self.previous_image, self.target_image):
            _match(value, _IMAGE, "immutable image")
        if type(self.generation) is not int or self.generation < 1:
            raise MaintenanceError("invalid generation")
        if not isinstance(self.instances, Mapping) or set(self.instances) != set(REQUIRED_ROLES):
            raise MaintenanceError("all exact role instances are required")
        # Copy caller-owned mappings: mutating them must not alter a binding.
        object.__setattr__(self, "instances", MappingProxyType(dict(self.instances)))
        ids = set()
        pairs = set()
        for role, identity in self.instances.items():
            if not isinstance(identity, Identity) or identity.role != role:
                raise MaintenanceError("role identity mismatch")
            if identity.instance_id in ids:
                raise MaintenanceError("duplicate role instance")
            ids.add(identity.instance_id)
            pairs.add((identity.source, identity.image))
        if len(pairs) != 1 or not pairs.issubset({
            (self.previous_source, self.previous_image), (self.target_source, self.target_image)
        }):
            raise MaintenanceError("inconsistent approved source/image instances")

    def as_dict(self) -> dict:
        result = {name: getattr(self, name) for name in (
            "operation_id", "plan_sha256", "previous_source", "previous_image",
            "target_source", "target_image", "generation",
        )}
        result["instances"] = {key: value.as_dict() for key, value in self.instances.items()}
        return result

    @classmethod
    def from_dict(cls, value: Mapping) -> Binding:
        try:
            data = dict(value)
            data["instances"] = {key: Identity.from_dict(item) for key, item in data["instances"].items()}
            return cls(**data)
        except (TypeError, KeyError, AttributeError) as exc:
            raise MaintenanceError("invalid binding fields") from exc


@dataclass(frozen=True)
class RiskProof:
    operation_id: str
    generation: int
    plan_sha256: str
    source: str
    image: str
    captured_at: float
    mode: str
    verified: bool
    positions: int
    pending: int
    algo: int
    partial: int
    inflight: int
    unknown: int

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping) -> RiskProof:
        try:
            return cls(**value)
        except TypeError as exc:
            raise MaintenanceError("all risk evidence fields are required") from exc


def validate_risk_proof(proof: RiskProof, binding: Binding, now: float) -> None:
    if not isinstance(proof, RiskProof):
        raise MaintenanceError("verified DEMO risk proof required")
    if (proof.operation_id, proof.generation, proof.plan_sha256) != (
        binding.operation_id, binding.generation, binding.plan_sha256
    ) or type(proof.generation) is not int:
        raise MaintenanceError("stale risk proof binding")
    current = binding.instances["backend"]
    if (proof.source, proof.image) != (current.source, current.image):
        raise MaintenanceError("risk proof source/image mismatch")
    age = _number(now, "clock") - _number(proof.captured_at, "proof capture time")
    if age < 0 or age > PROOF_MAX_AGE_SECONDS:
        raise MaintenanceError("risk proof is stale or from the future")
    if proof.mode != "DEMO" or proof.verified is not True:
        raise MaintenanceError("DEMO identity has not been verified")
    for field in ("positions", "pending", "algo", "partial", "inflight", "unknown"):
        value = getattr(proof, field)
        if type(value) is not int or value != 0:
            raise MaintenanceError("unsafe or missing risk evidence: " + field)


class MaintenanceStore:
    """Durable fail-closed protocol. Opening the store never resumes work.

    All connections are short-lived. BEGIN IMMEDIATE is the cross-process
    admission/maintenance linearization point; FULL synchronous commits persist
    the send reservation before a caller is allowed to touch the network.
    """

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        if not self.path.is_absolute() or str(path) == ":memory:":
            raise MaintenanceError("an explicit absolute SQLite path is required")
        self.clock = clock
        initial_path_existed = self.path.exists()
        with self._connection(create=not initial_path_existed) as db:
            db.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {"maintenance_meta", "maintenance_state", "maintenance_operations",
                        "maintenance_actors", "maintenance_activities", "maintenance_acks", "maintenance_orders"}
            if tables and tables != expected:
                raise MaintenanceError("maintenance must use its own database")
            if tables:
                if db.execute("SELECT version FROM maintenance_meta").fetchall() != [(PROTOCOL_VERSION,)]:
                    raise MaintenanceError("unsupported maintenance database")
            else:
                if initial_path_existed:
                    raise MaintenanceError("existing empty maintenance state cannot be reset")
                # DDL stays in this explicit transaction (executescript commits implicitly).
                for statement in (
                    "CREATE TABLE maintenance_meta(version INTEGER NOT NULL)",
                    "CREATE TABLE maintenance_state(id INTEGER PRIMARY KEY CHECK(id=1), phase TEXT NOT NULL, binding TEXT, deadline REAL, reason TEXT NOT NULL, switched INTEGER NOT NULL, rebound INTEGER NOT NULL, shutdown INTEGER NOT NULL)",
                    "CREATE TABLE maintenance_operations(operation_id TEXT PRIMARY KEY, initial_binding TEXT NOT NULL, deadline REAL NOT NULL)",
                    "CREATE TABLE maintenance_actors(instance_id TEXT PRIMARY KEY, identity TEXT NOT NULL, online INTEGER NOT NULL)",
                    "CREATE TABLE maintenance_activities(activity_id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, parent_id TEXT, started_at REAL NOT NULL)",
                    "CREATE TABLE maintenance_acks(role TEXT PRIMARY KEY, binding TEXT NOT NULL, identity TEXT NOT NULL, captured_at REAL NOT NULL)",
                    "CREATE TABLE maintenance_orders(logical_intent TEXT PRIMARY KEY, request_hash TEXT NOT NULL, client_order_id TEXT UNIQUE NOT NULL, owner TEXT NOT NULL, activity_id TEXT NOT NULL, status TEXT NOT NULL, broker_order_id TEXT NOT NULL, updated_at REAL NOT NULL)",
                ):
                    db.execute(statement)
                db.execute("INSERT INTO maintenance_meta VALUES (?)", (PROTOCOL_VERSION,))
                db.execute("INSERT INTO maintenance_state VALUES (1,'HOLD',NULL,NULL,'startup requires explicit resume',0,0,0)")
            db.commit()

    @contextmanager
    def _connection(self, *, create: bool = False):
        # Creation is permitted only for a deliberately new constructor path.
        # Every subsequent action uses mode=rw: deletion must raise, never create
        # a blank replacement that silently drops outstanding work or journals.
        target = str(self.path) if create else self.path.as_uri() + "?mode=rw"
        db = sqlite3.connect(target, timeout=30, isolation_level=None, uri=not create)
        try:
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            versions = [row[0] for row in db.execute("SELECT version FROM maintenance_meta")]
            if versions != [PROTOCOL_VERSION]:
                raise MaintenanceError("maintenance metadata changed or is unsupported")
            now = _number(self.clock(), "clock")
            row = db.execute("SELECT * FROM maintenance_state WHERE id=1").fetchone()
            if row is None:
                raise MaintenanceError("maintenance state is missing")
            state = dict(row)
            if state["phase"] not in ("NORMAL", "DRAINING", "PAUSED", "STOPPING", "SWITCHING", "VERIFYING", "RESUMING", "CANCELLED", "HOLD"):
                raise MaintenanceError("maintenance state phase is corrupt")
            if state["binding"] is None:
                if state["phase"] != "HOLD" or state["deadline"] is not None:
                    raise MaintenanceError("unbound state cannot authorize normal work")
            else:
                try:
                    binding = Binding.from_dict(json.loads(state["binding"]))
                except (TypeError, ValueError) as exc:
                    raise MaintenanceError("persisted binding is corrupt") from exc
                deadline = _number(state["deadline"], "persisted deadline")
                operation = db.execute("SELECT * FROM maintenance_operations WHERE operation_id=?", (binding.operation_id,)).fetchone()
                if operation is None or operation["deadline"] != deadline:
                    raise MaintenanceError("persisted operation/deadline evidence changed")
            for flag in ("switched", "rebound", "shutdown"):
                if type(state[flag]) is not int or state[flag] not in (0, 1):
                    raise MaintenanceError("persisted maintenance flags are corrupt")
            if state["phase"] == "NORMAL" and state["shutdown"]:
                raise MaintenanceError("normal admission contradicts an orderly exit request")
            if state["phase"] != "NORMAL" and state["deadline"] is not None and now >= state["deadline"]:
                db.execute("UPDATE maintenance_state SET phase='HOLD',reason='absolute maintenance deadline expired' WHERE id=1")
                state["phase"] = "HOLD"
                state["reason"] = "absolute maintenance deadline expired"
            yield db, state, now
            db.commit()

    @staticmethod
    def _bound(state: dict, binding: Binding) -> None:
        if not isinstance(binding, Binding) or state["binding"] != _json(binding.as_dict()):
            raise MaintenanceError("stale operation, plan, generation or role identities")

    @staticmethod
    def _live(db, identity: Identity) -> None:
        if not isinstance(identity, Identity):
            raise MaintenanceError("exact identity required")
        actor = db.execute("SELECT * FROM maintenance_actors WHERE instance_id=?", (identity.instance_id,)).fetchone()
        if actor is None or not actor["online"] or actor["identity"] != _json(identity.as_dict()):
            raise MaintenanceError("unregistered or disconnected instance")

    @staticmethod
    def _clear(db) -> None:
        if db.execute("SELECT 1 FROM maintenance_activities LIMIT 1").fetchone():
            raise MaintenanceError("admitted/queued/child/writer activity remains")
        if db.execute("SELECT 1 FROM maintenance_orders WHERE status NOT IN ('ACKNOWLEDGED','REJECTED') LIMIT 1").fetchone():
            raise MaintenanceError("unresolved order journal remains")

    def _barrier(self, db, binding: Binding, now: float) -> None:
        self._clear(db)
        live_ids = {row[0] for row in db.execute("SELECT instance_id FROM maintenance_actors WHERE online=1")}
        if live_ids != {identity.instance_id for identity in binding.instances.values()}:
            raise MaintenanceError("unbound live or unknown actors block the barrier")
        rows = {row["role"]: row for row in db.execute("SELECT * FROM maintenance_acks")}
        for role, identity in binding.instances.items():
            self._live(db, identity)
            ack = rows.get(role)
            if ack is None or ack["binding"] != _json(binding.as_dict()) or ack["identity"] != _json(identity.as_dict()):
                raise MaintenanceError("missing current-generation ACK: " + role)
            if not 0 <= now - ack["captured_at"] <= ACK_MAX_AGE_SECONDS:
                raise MaintenanceError("expired ACK: " + role)

    def startup(self, identity: Identity) -> None:
        with self._transaction() as (db, state, now):
            old = db.execute("SELECT * FROM maintenance_actors WHERE instance_id=?", (identity.instance_id,)).fetchone()
            encoded = _json(identity.as_dict())
            if old and old["identity"] != encoded:
                raise MaintenanceError("process start identity reused with changed provenance")
            if old and not old["online"]:
                raise MaintenanceError("ended identity cannot be restarted; use new process start identity")
            if old is None:
                db.execute("INSERT INTO maintenance_actors VALUES (?,?,1)", (identity.instance_id, encoded))
                db.execute("UPDATE maintenance_state SET phase='HOLD',reason='new process startup requires explicit resume' WHERE id=1")
                db.execute("DELETE FROM maintenance_acks")

    def status(self) -> dict:
        with self._transaction() as (db, state, now):
            return {
                "protocol": PROTOCOL_VERSION, "phase": state["phase"], "fenced": state["phase"] != "NORMAL",
                "binding": json.loads(state["binding"]) if state["binding"] else None,
                "deadline": state["deadline"], "reason": state["reason"],
                "shutdown_requested": bool(state["shutdown"]),
                "activities": [dict(row) for row in db.execute("SELECT * FROM maintenance_activities ORDER BY activity_id")],
                "acks": [dict(row) for row in db.execute("SELECT * FROM maintenance_acks ORDER BY role")],
                "orders": [dict(row) for row in db.execute("SELECT * FROM maintenance_orders ORDER BY logical_intent")],
                "actors": [dict(row) for row in db.execute("SELECT * FROM maintenance_actors ORDER BY instance_id")],
            }

    def request(self, binding: Binding, deadline: float, proof: RiskProof) -> None:
        deadline = _number(deadline, "absolute deadline")
        with self._transaction() as (db, state, now):
            encoded = _json(binding.as_dict())
            old = db.execute("SELECT * FROM maintenance_operations WHERE operation_id=?", (binding.operation_id,)).fetchone()
            if old:
                if old["initial_binding"] != encoded or old["deadline"] != deadline:
                    raise MaintenanceError("duplicate operation cannot change binding or renew deadline")
                # Exact retries are observations, never transitions (including after resume/rebind).
                return
            if state["binding"] and state["phase"] != "NORMAL":
                raise MaintenanceError("another maintenance operation is still fenced")
            if not now < deadline <= now + MAX_BUDGET_SECONDS:
                raise MaintenanceError("maintenance deadline must be future and bounded")
            if state["binding"] and binding.generation <= json.loads(state["binding"])["generation"]:
                raise MaintenanceError("generation must advance")
            if any((i.source, i.image) != (binding.previous_source, binding.previous_image) for i in binding.instances.values()):
                raise MaintenanceError("request must bind the previous deployment")
            for identity in binding.instances.values():
                self._live(db, identity)
            known = {row[0] for row in db.execute("SELECT instance_id FROM maintenance_actors WHERE online=1")}
            if known != {i.instance_id for i in binding.instances.values()}:
                raise MaintenanceError("unbound live or unknown actors remain")
            validate_risk_proof(proof, binding, now)
            if db.execute("SELECT 1 FROM maintenance_activities WHERE kind IN ('supervisory-action','normal-deadline-termination') LIMIT 1").fetchone():
                raise MaintenanceError("supervisory action must settle before fencing")
            if db.execute("SELECT 1 FROM maintenance_orders WHERE status NOT IN ('ACKNOWLEDGED','REJECTED') LIMIT 1").fetchone():
                raise MaintenanceError("unresolved order journal refuses maintenance")
            db.execute("INSERT INTO maintenance_operations VALUES (?,?,?)", (binding.operation_id, encoded, deadline))
            db.execute("UPDATE maintenance_state SET phase='DRAINING',binding=?,deadline=?,reason='',switched=0,rebound=0,shutdown=0 WHERE id=1", (encoded, deadline))
            db.execute("DELETE FROM maintenance_acks")

    def admit(self, identity: Identity, kind: str, activity_id: str | None = None, parent_id: str | None = None) -> str:
        _token(kind, "activity kind")
        activity_id = activity_id or uuid.uuid4().hex
        _token(activity_id, "activity id")
        with self._transaction() as (db, state, now):
            self._live(db, identity)
            owner = _json(identity.as_dict())
            binding = Binding.from_dict(json.loads(state["binding"])) if state["binding"] else None
            if binding is None or binding.instances.get(identity.role) != identity:
                raise AdmissionClosed("instance is not currently bound")
            if parent_id is None:
                if state["phase"] != "NORMAL":
                    raise AdmissionClosed("maintenance admission is fenced")
            else:
                parent = db.execute("SELECT * FROM maintenance_activities WHERE activity_id=?", (parent_id,)).fetchone()
                if parent is None or parent["owner"] != owner or state["phase"] not in ("NORMAL", "DRAINING", "CANCELLED", "HOLD"):
                    raise AdmissionClosed("child must belong to admitted unsettled parent")
                if parent["kind"] == "startup-verification":
                    raise AdmissionClosed("startup verification grants no ordinary child admission")
                if state["phase"] != "NORMAL" and kind in ("supervisory-action", "normal-deadline-termination"):
                    raise AdmissionClosed("a parent scope cannot authorize stop/kill under a fence")
            old = db.execute("SELECT * FROM maintenance_activities WHERE activity_id=?", (activity_id,)).fetchone()
            if old:
                raise MaintenanceError("activity id already admitted")
            db.execute("INSERT INTO maintenance_activities VALUES (?,?,?,?,?)", (activity_id, owner, kind, parent_id, now))
            # Admission invalidates that owner's quiescence assertion.
            db.execute("DELETE FROM maintenance_acks WHERE role=?", (identity.role,))
        return activity_id

    def finish(self, activity_id: str, identity: Identity) -> None:
        with self._transaction() as (db, state, now):
            activity = db.execute("SELECT * FROM maintenance_activities WHERE activity_id=?", (activity_id,)).fetchone()
            if activity is None:
                raise MaintenanceError("unknown activity cannot be silently pruned")
            if activity["owner"] != _json(identity.as_dict()):
                raise MaintenanceError("activity owner mismatch")
            if db.execute("SELECT 1 FROM maintenance_activities WHERE parent_id=?", (activity_id,)).fetchone():
                raise MaintenanceError("child activity has not settled")
            if db.execute("SELECT 1 FROM maintenance_orders WHERE activity_id=? AND status NOT IN ('ACKNOWLEDGED','REJECTED')", (activity_id,)).fetchone():
                raise MaintenanceError("uncertain order send cannot be finished")
            db.execute("DELETE FROM maintenance_activities WHERE activity_id=?", (activity_id,))

    def admit_verification(self, identity: Identity) -> str:
        """Track controller-approved cold startup DDL/import verification.

        This narrowly named scope grants no trading, scheduler, cache, broker or
        ordinary writer admission. The controller must independently verify the
        actor's image/source before starting it. An unbound candidate can perform
        its existing startup initialization while remaining paused; this scope
        blocks ACK/rebind until the imports and database connections settle.
        """
        activity_id = uuid.uuid4().hex
        with self._transaction() as (db, state, now):
            self._live(db, identity)
            if not state["binding"] or state["phase"] not in ("DRAINING", "PAUSED", "SWITCHING", "VERIFYING", "HOLD"):
                raise AdmissionClosed("startup verification requires a bound fenced deployment")
            if now >= state["deadline"]:
                raise AdmissionClosed("startup verification cannot extend an expired operation")
            binding = Binding.from_dict(json.loads(state["binding"]))
            if state["shutdown"] and binding.instances.get(identity.role) == identity:
                raise AdmissionClosed("the exiting old instance cannot start verification")
            if (identity.source, identity.image) not in {
                (binding.previous_source, binding.previous_image), (binding.target_source, binding.target_image)
            }:
                raise MaintenanceError("startup verification provenance is not approved")
            db.execute("INSERT INTO maintenance_activities VALUES (?,?,?,?,?)", (
                activity_id, _json(identity.as_dict()), "startup-verification", None, now,
            ))
            db.execute("DELETE FROM maintenance_acks WHERE role=?", (identity.role,))
        return activity_id

    def acknowledge(self, binding: Binding, identity: Identity) -> None:
        with self._transaction() as (db, state, now):
            self._bound(state, binding)
            self._live(db, identity)
            if state["phase"] == "NORMAL" or binding.instances.get(identity.role) != identity:
                raise MaintenanceError("ACK must bind a fenced current instance")
            if db.execute("SELECT 1 FROM maintenance_activities WHERE owner=?", (_json(identity.as_dict()),)).fetchone():
                raise MaintenanceError("owner still has admitted work")
            db.execute("INSERT OR REPLACE INTO maintenance_acks VALUES (?,?,?,?)", (identity.role, _json(binding.as_dict()), _json(identity.as_dict()), now))

    def pause(self, binding: Binding, proof: RiskProof) -> None:
        with self._transaction() as (db, state, now):
            self._bound(state, binding)
            if state["phase"] not in ("DRAINING", "PAUSED") or now >= state["deadline"]:
                raise MaintenanceError("pause requires unexpired drain")
            self._barrier(db, binding, now)
            validate_risk_proof(proof, binding, now)
            db.execute("UPDATE maintenance_state SET phase='PAUSED',reason='' WHERE id=1")

    def cancel(self, binding: Binding, reason: str = "maintenance cancelled") -> None:
        with self._transaction() as (db, state, now):
            self._bound(state, binding)
            if state["phase"] == "NORMAL":
                raise MaintenanceError("completed operation cannot be cancelled")
            phase = "HOLD" if state["switched"] or state["shutdown"] else "CANCELLED"
            db.execute("UPDATE maintenance_state SET phase=?,reason=? WHERE id=1", (phase, str(reason)))

    def hold(self, reason: str) -> None:
        with self._transaction() as (db, state, now):
            db.execute("UPDATE maintenance_state SET phase='HOLD',reason=? WHERE id=1", (str(reason),))
            db.execute("DELETE FROM maintenance_acks")

    def disconnect(self, identity: Identity) -> None:
        with self._transaction() as (db, state, now):
            self._live(db, identity)
            db.execute("UPDATE maintenance_actors SET online=0 WHERE instance_id=?", (identity.instance_id,))
            db.execute("UPDATE maintenance_state SET phase='HOLD',reason='component disconnected' WHERE id=1")
            db.execute("DELETE FROM maintenance_acks WHERE role=?", (identity.role,))

    def begin_shutdown(self, binding: Binding) -> None:
        """Request natural exits only after the complete pause barrier.

        The admission fence remains closed. Components observe the durable
        flag for their exact binding and leave their loops without signals.
        """
        with self._transaction() as (db, state, now):
            self._bound(state, binding)
            if state["phase"] == "STOPPING" and state["shutdown"]:
                return
            if state["phase"] != "PAUSED" or now >= state["deadline"]:
                raise MaintenanceError("shutdown requires unexpired PAUSED state")
            self._barrier(db, binding, now)
            db.execute("UPDATE maintenance_state SET phase='STOPPING',shutdown=1 WHERE id=1")

    def begin_switch(self, binding: Binding, ended_instances: set[str]) -> None:
        """Apply controller-verified exact process end evidence; never stop them.

        This set is reducer input, not an OS liveness probe or authority to kill.
        Natural exits must already have completed before this transition.
        """
        with self._transaction() as (db, state, now):
            self._bound(state, binding)
            if state["phase"] not in ("STOPPING", "HOLD") or not state["shutdown"] or now >= state["deadline"]:
                raise MaintenanceError("switch requires unexpired orderly STOPPING")
            self._clear(db)
            old_ids = {identity.instance_id for identity in binding.instances.values()}
            if type(ended_instances) is not set or ended_instances != old_ids:
                raise MaintenanceError("all exact old instances must actually have ended")
            unknown = {row[0] for row in db.execute("SELECT instance_id FROM maintenance_actors WHERE online=1")} - old_ids
            if unknown:
                raise MaintenanceError("unknown live actors block switching")
            for instance in old_ids:
                db.execute("UPDATE maintenance_actors SET online=0 WHERE instance_id=?", (instance,))
            db.execute("UPDATE maintenance_state SET phase='SWITCHING',switched=1,rebound=0 WHERE id=1")

    def begin_recovery(self, binding: Binding) -> None:
        """Pause a failed but quiescent candidate for natural rollback exits.

        The original deadline and approval binding remain unchanged. An expired
        or unknown deployment stays HOLD for separate external approval.
        """
        with self._transaction() as (db, state, now):
            self._bound(state, binding)
            if not state["switched"] or state["phase"] not in ("SWITCHING", "VERIFYING", "HOLD") or now >= state["deadline"]:
                raise MaintenanceError("recovery requires unexpired switched operation")
            self._barrier(db, binding, now)
            db.execute("UPDATE maintenance_state SET phase='STOPPING',shutdown=1,reason='explicit previous-source recovery' WHERE id=1")

    def rebind(self, binding: Binding, instances: Mapping[str, Identity], ended_instances: set[str]) -> Binding:
        """Controller-only: ended_instances must come from actual stop/liveness proof.

        Every old exact process start identity must have ended. The new actors
        have independently checked target (or approved rollback) provenance.
        """
        candidate = replace(binding, generation=binding.generation + 1, instances=instances)
        with self._transaction() as (db, state, now):
            self._bound(state, binding)
            if not state["switched"] or state["phase"] not in ("SWITCHING", "HOLD") or now >= state["deadline"]:
                raise MaintenanceError("rebind requires unexpired switching/recovery")
            self._clear(db)
            old_ids = {i.instance_id for i in binding.instances.values()}
            new_ids = {i.instance_id for i in candidate.instances.values()}
            if type(ended_instances) is not set or ended_instances != old_ids or old_ids & new_ids:
                raise MaintenanceError("all previous exact instances must actually have ended")
            for identity in candidate.instances.values():
                self._live(db, identity)
            unknown = {row[0] for row in db.execute("SELECT instance_id FROM maintenance_actors WHERE online=1")} - old_ids - new_ids
            if unknown:
                raise MaintenanceError("unknown actors block rebinding")
            for instance in old_ids:
                db.execute("UPDATE maintenance_actors SET online=0 WHERE instance_id=?", (instance,))
            db.execute("UPDATE maintenance_state SET phase='SWITCHING',binding=?,rebound=1,shutdown=0,reason='' WHERE id=1", (_json(candidate.as_dict()),))
            db.execute("DELETE FROM maintenance_acks")
        return candidate

    def begin_verify(self, binding: Binding) -> None:
        with self._transaction() as (db, state, now):
            self._bound(state, binding)
            if state["phase"] not in ("SWITCHING", "HOLD", "VERIFYING") or not state["rebound"] or now >= state["deadline"]:
                raise MaintenanceError("verify requires unexpired rebound deployment")
            self._barrier(db, binding, now)
            db.execute("UPDATE maintenance_state SET phase='VERIFYING',reason='' WHERE id=1")

    def resume(self, binding: Binding, proof: RiskProof) -> None:
        with self._transaction() as (db, state, now):
            self._bound(state, binding)
            if state["phase"] == "NORMAL":
                return  # An exact retry never releases a newer binding.
            if state["shutdown"]:
                raise MaintenanceError("orderly exits have been requested; resume requires a rebound deployment")
            if state["switched"]:
                if state["phase"] != "VERIFYING" or now >= state["deadline"]:
                    raise MaintenanceError("post-switch resume requires unexpired VERIFYING")
            elif state["phase"] not in ("PAUSED", "CANCELLED", "HOLD"):
                raise MaintenanceError("resume requires explicit pause/cancel/HOLD resolution")
            self._barrier(db, binding, now)
            validate_risk_proof(proof, binding, now)
            # RESUMING and NORMAL commit atomically: an interrupted attempt
            # cannot expose an intermediate state with admissions open.
            db.execute("UPDATE maintenance_state SET phase='RESUMING' WHERE id=1")
            db.execute("UPDATE maintenance_state SET phase='NORMAL',reason='' WHERE id=1")

    def prepare_order(self, identity: Identity, logical_intent: str, request_hash: str, parent_id: str | None = None) -> dict:
        """Reserve exactly once before sending; retries always return send_allowed=False.

        request_hash covers the canonical network mutation payload. No network
        call belongs in this method or its transaction. A crash after reservation
        is intentionally unresolved, even if the process never reached send.
        """
        _token(logical_intent, "logical order intent")
        _match(request_hash, _SHA64, "order request hash")
        with self._transaction() as (db, state, now):
            self._live(db, identity)
            old = db.execute("SELECT * FROM maintenance_orders WHERE logical_intent=?", (logical_intent,)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise MaintenanceError("logical intent reused with changed request")
                return {**dict(old), "send_allowed": False}
            binding = Binding.from_dict(json.loads(state["binding"])) if state["binding"] else None
            if binding is None or binding.instances.get(identity.role) != identity:
                raise AdmissionClosed("order instance is not bound")
            owner = _json(identity.as_dict())
            # Each broker mutation is new admission. A pre-admitted queued
            # parent may finish writers/children, but cannot authorize a new
            # send after DRAINING has established the risk fence.
            if state["phase"] != "NORMAL":
                raise AdmissionClosed("order sending is fenced, including admitted descendants")
            if parent_id is not None:
                parent = db.execute("SELECT * FROM maintenance_activities WHERE activity_id=?", (parent_id,)).fetchone()
                if parent is None or parent["owner"] != owner:
                    raise AdmissionClosed("order is not part of already admitted work")
            activity_id = uuid.uuid4().hex
            client_order_id = "m" + hashlib.sha256(logical_intent.encode("utf-8")).hexdigest()[:31]
            db.execute("INSERT INTO maintenance_activities VALUES (?,?,?,?,?)", (activity_id, owner, "order-send", parent_id, now))
            db.execute("INSERT INTO maintenance_orders VALUES (?,?,?,?,?,'PREPARED','',?)", (logical_intent, request_hash, client_order_id, owner, activity_id, now))
            db.execute("DELETE FROM maintenance_acks WHERE role=?", (identity.role,))
            return {"logical_intent": logical_intent, "request_hash": request_hash, "client_order_id": client_order_id,
                    "activity_id": activity_id, "status": "PREPARED", "send_allowed": True}

    def record_order(self, identity: Identity, logical_intent: str, outcome: str, broker_order_id: str = "") -> None:
        if outcome not in ("ACKNOWLEDGED", "REJECTED", "UNKNOWN", "PARTIAL"):
            raise MaintenanceError("unsupported order outcome")
        if broker_order_id:
            _token(broker_order_id, "broker order id")
        if outcome in ("ACKNOWLEDGED", "PARTIAL") and not broker_order_id:
            raise MaintenanceError("broker identity required for acknowledged/partial result")
        with self._transaction() as (db, state, now):
            row = db.execute("SELECT * FROM maintenance_orders WHERE logical_intent=?", (logical_intent,)).fetchone()
            if row is None or row["owner"] != _json(identity.as_dict()):
                raise MaintenanceError("unknown intent or mismatched send owner")
            if row["status"] in ("ACKNOWLEDGED", "REJECTED"):
                if (row["status"], row["broker_order_id"]) != (outcome, broker_order_id):
                    raise MaintenanceError("terminal receipt cannot be overwritten")
                return
            if row["status"] in ("UNKNOWN", "PARTIAL") and outcome in ("ACKNOWLEDGED", "REJECTED"):
                raise MaintenanceError("uncertain receipt needs explicit reconciliation")
            if row["status"] == "PARTIAL" and outcome != "PARTIAL":
                raise MaintenanceError("partial order evidence must be retained")
            if row["broker_order_id"] and row["broker_order_id"] != broker_order_id:
                raise MaintenanceError("broker order identity changed")
            db.execute("UPDATE maintenance_orders SET status=?,broker_order_id=?,updated_at=? WHERE logical_intent=?", (outcome, broker_order_id, now, logical_intent))
            if outcome in ("ACKNOWLEDGED", "REJECTED"):
                db.execute("DELETE FROM maintenance_activities WHERE activity_id=?", (row["activity_id"],))

    def reconcile_order(self, binding: Binding, identity: Identity, logical_intent: str, outcome: str, *,
                        client_order_id: str, request_hash: str, broker_order_id: str = "",
                        verified: bool = False, captured_at: float | None = None) -> None:
        """Apply explicit read-only broker evidence, never perform a resend.

        Acknowledgment means the request is known, not that the account has no
        pending orders. The independent DEMO risk proof is still required before
        pause/resume. Controller/adapters must establish the evidence's truth.
        """
        if verified is not True or outcome not in ("ACKNOWLEDGED", "REJECTED"):
            raise MaintenanceError("verified terminal broker evidence required")
        _match(request_hash, _SHA64, "order request hash")
        _token(client_order_id, "client order id")
        if broker_order_id:
            _token(broker_order_id, "broker order id")
        if outcome == "ACKNOWLEDGED" and not broker_order_id:
            raise MaintenanceError("broker identity required")
        with self._transaction() as (db, state, now):
            self._bound(state, binding)
            self._live(db, identity)
            if binding.instances.get(identity.role) != identity:
                raise MaintenanceError("reconciliation owner is not current")
            if not 0 <= now - _number(captured_at, "broker evidence time") <= PROOF_MAX_AGE_SECONDS:
                raise MaintenanceError("stale broker evidence")
            row = db.execute("SELECT * FROM maintenance_orders WHERE logical_intent=?", (logical_intent,)).fetchone()
            if row is None or (row["client_order_id"], row["request_hash"]) != (client_order_id, request_hash):
                raise MaintenanceError("broker evidence does not identify persisted send")
            if row["status"] in ("ACKNOWLEDGED", "REJECTED"):
                if (row["status"], row["broker_order_id"]) != (outcome, broker_order_id):
                    raise MaintenanceError("terminal evidence cannot be overwritten")
                return
            if row["broker_order_id"] and row["broker_order_id"] != broker_order_id:
                raise MaintenanceError("broker identity changed")
            db.execute("UPDATE maintenance_orders SET status=?,broker_order_id=?,updated_at=? WHERE logical_intent=?", (outcome, broker_order_id, now, logical_intent))
            db.execute("DELETE FROM maintenance_activities WHERE activity_id=?", (row["activity_id"],))
