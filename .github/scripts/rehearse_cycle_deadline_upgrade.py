"""Portable synthetic SQLite forward/rollback rehearsal, never deployment.

The caller supplies the independently verified previous git archive. All writable
fixtures are invented and temporary; Docker, shell and network calls are denied.
Portable copy adapters exercise the existing carry/rollback control flow without
claiming POSIX ownership, process locks, image health or service acceptance.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from unittest.mock import Mock, patch


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def denied(*args, **kwargs):
    raise AssertionError("external commands and network are forbidden in synthetic rehearsal")


class ClosingConnection(sqlite3.Connection):
    """Close test connections deterministically after the real transaction exit.

    Older auth/trading code relies on garbage collection after ``with db``;
    deterministic cleanup avoids open file handles during Windows tree renames.
    SQL, commit and rollback still use the actual SQLite implementation.
    """
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def trade_write(manager, data, marker):
    manager.DATA_DIR, manager.DB_PATH = str(data), str(data / "astra_quant.db")
    manager.init_database()
    with manager.get_db() as db:
        db.execute("INSERT INTO trades(bill_id,time,inst,action,direction,venue,environment,source_bill_id,comment) "
                   "VALUES (?, '2026-10-05', 'SYNTHETIC-USDT-SWAP','close','long','okx','demo',?,?)",
                   (marker, marker, "synthetic-" + marker))


def model_record(caller):
    return {"caller": caller, "model": "synthetic-model", "reasoning_effort": "high",
            "status": "success", "started_at": "2026-10-05 12:00:00", "duration_ms": 1,
            "input_chars": 1, "output_chars": 1, "prompt_fingerprint": "synthetic",
            "prompt_transport": "python-direct", "input_tokens": 1, "output_tokens": 1,
            "reasoning_tokens": 0, "total_tokens": 2, "cached_tokens": 0,
            "cache_status": "unreported", "usage_keys": "total_tokens", "error_type": ""}


def rehearse(source: Path, previous: Path) -> dict:
    source, previous = source.resolve(), previous.resolve()
    for root in (source, previous):
        for name in ("astra_gateway/store.py", "astra_backend/admin_auth.py", "scripts/db_manager.py"):
            if not (root / name).is_file():
                raise AssertionError("verified source fixture is missing a storage entry point")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    helper = load_module(Path(__file__).with_name("runtime_release_upgrade.py"), "deadline_runtime_rehearsal")
    helper.gateway_source_compatibility(previous / "astra_gateway/store.py", source / "astra_gateway/store.py")
    for name in ("astra_backend/admin_auth.py", "scripts/db_manager.py", "astra_gateway/events.py"):
        assert (previous / name).read_bytes().replace(b"\r\n", b"\n") == (source / name).read_bytes().replace(b"\r\n", b"\n"), "unreviewed storage source change"

    real_connect = sqlite3.connect
    def closing_connect(*args, **kwargs):
        kwargs.setdefault("factory", ClosingConnection)
        return real_connect(*args, **kwargs)
    with patch.object(sqlite3, "connect", closing_connect), \
         patch.object(helper, "run", denied), patch.object(subprocess, "run", denied), \
         patch.object(subprocess, "Popen", denied), patch.object(urllib.request, "urlopen", denied), \
         patch.object(socket, "create_connection", denied), patch.object(socket.socket, "connect", denied), \
         patch.object(socket.socket, "connect_ex", denied), \
         tempfile.TemporaryDirectory(prefix="astra-deadline-rehearsal-") as temporary:
        operation = Path(temporary).resolve() / "operation"
        operation.mkdir()
        original, live = operation / "original-deployment", operation / "live"
        (original / "data").mkdir(parents=True)
        (original / "source-marker").write_text("previous-source", encoding="utf-8")
        (original / ".env").write_text("ASTRA_STANDALONE_GATEWAY=true\nASTRA_OKX_ENV=demo\nOKX_IS_SIMULATED=1\n", encoding="utf-8")
        (original / "data/orders.json").write_text('{"synthetic":"prior"}', encoding="utf-8")
        (original / "data/risk_config.json").write_text('{"synthetic_revision":"prior"}', encoding="utf-8")
        (original / "data/intentionally-deleted.json").write_text("stale-default", encoding="utf-8")
        old_gateway = load_module(previous / "astra_gateway/store.py", "deadline_old_gateway")
        new_gateway = load_module(source / "astra_gateway/store.py", "deadline_new_gateway")
        old_admin = load_module(previous / "astra_backend/admin_auth.py", "deadline_old_admin")
        new_admin = load_module(source / "astra_backend/admin_auth.py", "deadline_new_admin")
        old_trading = load_module(previous / "scripts/db_manager.py", "deadline_old_trading")
        new_trading = load_module(source / "scripts/db_manager.py", "deadline_new_trading")
        old = old_gateway.GatewayStore(original / "data/astra_gateway.db")
        old.set_state("synthetic-state", "prior")
        old.record_model_call(model_record("prior"))
        old_job = old.begin_job("synthetic-prior")
        old.finish_job(old_job, 0, "synthetic")
        auth = old_admin.AdminAuthStore(original / "data/astra_admin.db")
        identity = auth.create_user("syntheticoperator", "SyntheticPassword12345", "superadmin")
        session = auth.login("syntheticoperator", "SyntheticPassword12345")["session_token"]
        trade_write(old_trading, original / "data", "prior")
        baseline = helper.db_state(original)
        stopped = operation / "stopped-snapshot"
        shutil.copytree(original, stopped)
        snapshot = helper.tree_manifest(original)
        shutil.copytree(original, live)
        (live / "source-marker").write_text("candidate-source", encoding="utf-8")
        data = live / "data"
        current = new_gateway.GatewayStore(data / "astra_gateway.db")
        upgraded = helper.db_state(live)
        helper.compatible_databases(baseline, upgraded)
        new_gateway.GatewayStore(data / "astra_gateway.db")
        assert helper.db_state(live) == upgraded, "forward initialization is not idempotent"
        assert new_admin.AdminAuthStore(data / "astra_admin.db").validate_session(session)["id"] == identity["id"]
        run_id = current.begin_job("synthetic-candidate", "2026-10-05T12:15:00+08:00")
        attempt = {"job_run_id": run_id, "scheduled_at": "2026-10-05T12:15:00+08:00",
                   "caller": "synthetic", "client_request_id": "synthetic-client-id", "request_id": "synthetic-server-id",
                   "model": "synthetic-model", "status": "running", "started_at": "2026-10-05T12:15:01+08:00",
                   "completed_at": "", "duration_ms": 0, "http_status": None, "error_type": ""}
        current.record_model_request(attempt)
        attempt.update(status="success", completed_at="2026-10-05T12:15:02+08:00", duration_ms=1000, http_status=200)
        current.record_model_request(attempt)
        current.finish_job(run_id, 0, "synthetic")
        current.record_skipped_job("synthetic-candidate", "2026-10-05T12:30:00+08:00", "previous_run_active")
        current.record_model_call(model_record("candidate"))
        trade_write(new_trading, data, "candidate")
        (data / "risk_config.json").write_text('{"synthetic_revision":"latest"}', encoding="utf-8")
        (data / "orders.json").write_text('{"synthetic":"latest"}', encoding="utf-8")
        (data / "intentionally-deleted.json").unlink()

        old_current = old_gateway.GatewayStore(data / "astra_gateway.db")
        assert {row["caller"] for row in old_current.model_calls()} == {"prior", "candidate"}
        assert any(row.get("scheduled_at") for row in old_current.job_runs())
        old_current.set_state("old-writer", "latest")
        old_current.record_model_call(model_record("old-writer"))
        old_current.finish_job(old_current.begin_job("synthetic-old-writer"), 0, "synthetic")
        trade_write(old_trading, data, "old-writer")
        newest = helper.db_state(live)
        helper.compatible_databases(upgraded, newest)
        assert current.model_requests(run_id)[0]["request_id"] == "synthetic-server-id"

        obj = helper.Upgrade.__new__(helper.Upgrade)
        obj.op, obj.manifest_sha, obj.state = operation, "synthetic-manifest", {
            "phase": "candidate-active", "manifest_sha": "synthetic-manifest", "extras": [],
            "snapshot": snapshot, "databases": baseline, "prompt_refreshed": False}
        obj.sources = lambda: helper.gateway_source_compatibility(previous / "astra_gateway/store.py", source / "astra_gateway/store.py")
        obj.state['deployment_sources'] = {
            'previous': helper.deployment_source(original),
            'release': helper.deployment_source(live)}
        obj.image_metadata = Mock(return_value="synthetic-image-not-inspected")
        obj.capacity, obj.stop = Mock(), Mock()
        obj.start = Mock()
        obj.phase = lambda phase: obj.state.update(phase=phase)
        def portable_copy(source_path, destination):
            obj.allowed(source_path)
            obj.allowed(destination)
            helper.require(source_path.exists() and not destination.exists(), "copy_destination_exists")
            before = helper.files_manifest(source_path)
            if source_path.is_dir():
                shutil.copytree(source_path, destination, copy_function=shutil.copy2)
            else:
                shutil.copy2(source_path, destination)
            helper.require(helper.files_manifest(source_path) == before == helper.files_manifest(destination), "portable_copy_bytes")
        obj.copy = portable_copy
        def verify_latest(previous=False):
            assert previous is True
            restored = old_gateway.GatewayStore(live / "data/astra_gateway.db")
            assert restored.get_state("old-writer") == "latest"
            assert {row["caller"] for row in restored.model_calls()} == {"prior", "candidate", "old-writer"}
            with restored.connect() as db:
                assert db.execute("SELECT request_id,status FROM model_requests").fetchall()[0][:] == ("synthetic-server-id", "success")
            assert old_admin.AdminAuthStore(live / "data/astra_admin.db").validate_session(session)["id"] == identity["id"]
            assert (live / "data/risk_config.json").read_text(encoding="utf-8") == '{"synthetic_revision":"latest"}'
            assert (live / "data/orders.json").read_text(encoding="utf-8") == '{"synthetic":"latest"}'
            assert not (live / "data/intentionally-deleted.json").exists()
            assert (live / "source-marker").read_text(encoding="utf-8") == "previous-source"
            with sqlite3.connect(live / "data/astra_quant.db") as db:
                assert {row[0] for row in db.execute("SELECT bill_id FROM trades")} == {"prior", "candidate", "old-writer"}
            helper.compatible_databases(newest, helper.db_state(live))
        obj.verify = verify_latest
        # Root UID/mode and service acceptance are intentionally outside this
        # portable rehearsal; unchanged production gates are never relaxed.
        with patch.object(helper, "ROOT", live), patch.object(helper, "env_gate", lambda root: None), \
                patch.object(helper, 'containers', return_value={}):
            obj.rollback()
        assert obj.state["phase"] == "rolled-back"
        obj.start.assert_called_once_with(True)
        assert helper.tree_manifest(stopped) == snapshot, "independent snapshot was modified"
        assert helper.tree_manifest(original) == snapshot, "original deployment was modified"
        for name in ("astra_gateway.db", "astra_admin.db", "astra_quant.db"):
            with sqlite3.connect(live / "data" / name) as db:
                assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        return {"portable_checks": {"actual_previous_forward_schema": "passed", "idempotence": "passed",
                                    "old_reader_writer": "passed", "latest_state_rollback": "passed",
                                    "auth_trading_config_retention": "passed"},
                "external_commands": "disabled", "fixtures": "synthetic_temporary_only",
                "pending": ["POSIX ownership and real process locks", "candidate/published image and service acceptance"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(rehearse(args.source, args.previous), sort_keys=True))


if __name__ == "__main__":
    main()
