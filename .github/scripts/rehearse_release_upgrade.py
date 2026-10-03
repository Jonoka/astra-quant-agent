"""Hosted synthetic v8.4-to-v8.5 preservation and real prompt-renderer rehearsal.

No production fixtures, network/model calls, or trading jobs are used.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

from cryptography.fernet import Fernet


SCHEMA_TITLE = "严格 JSON 规范契约与完整输出骨架 (JSON Schema)"


def digests(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def seed_previous_admin(previous: Path, data: Path, password: str) -> dict:
    """Create a synthetic identity/session with the pinned old implementation."""
    initialized = subprocess.run([sys.executable, "-c", """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from astra_backend.admin_auth import AdminAuthStore
admin = AdminAuthStore(Path(sys.argv[2]) / 'astra_admin.db')
user = admin.create_user('cioperator', sys.stdin.read(), 'superadmin')
# A fixture password is carried in stdin, never a command argument.
print(json.dumps({'user': user}))
""", str(previous), str(data)], input=password, check=True,
        capture_output=True, text=True, timeout=30)
    identity = json.loads(initialized.stdout)
    authenticated = subprocess.run([sys.executable, "-c", """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from astra_backend.admin_auth import AdminAuthStore
admin = AdminAuthStore(Path(sys.argv[2]) / 'astra_admin.db')
print(json.dumps(admin.login('cioperator', sys.stdin.read())))
""", str(previous), str(data)], input=password, check=True,
        capture_output=True, text=True, timeout=30)
    session = json.loads(authenticated.stdout)
    assert session["user"]["id"] == identity["user"]["id"]
    return {"user_id": identity["user"]["id"], "session_token": session["session_token"]}


def verify_prompt_rendering(pl, data: Path, profile_id: str, markers: dict[str, str]) -> None:
    """Exercise real read/render entry points without allowing persisted rewrites."""
    # SQLite connections can checkpoint/remove WAL sidecars during lazy imports.
    # Database read/write/integrity preservation is asserted independently below;
    # this gate owns the prompt files and absence of a newly created overlay.
    prompt_files = ("prompt_library.json", "prompt_library.local.json")
    before = {name: (data / name).read_bytes() if (data / name).exists() else None
              for name in prompt_files}
    with patch.object(pl, "BASELINE_FILE", data / "prompt_library.json"), \
         patch.object(pl, "LOCAL_FILE", data / "prompt_library.local.json"):
        active = pl.active_profile()
        assert active["id"] == profile_id
        schema = pl.base_template_text("trading_system")
        schema_modules = pl.text_to_modules(schema, "base")
        assert len(schema_modules) == 1 and schema_modules[0]["title"] == SCHEMA_TITLE
        assert "macro_assessment" in schema_modules[0]["content"]
        for pipeline, marker in markers.items():
            rendered = pl.apply_module_layout(pl.base_template_text(pipeline), active, pipeline, "upgrade")
            assert marker in rendered, f"Custom {pipeline} content disappeared"
            # Old non-schema prose must survive as well as newly appended markers.
            for module in active["pipelines"][pipeline]:
                if module.get("enabled", True) and module["title"] != SCHEMA_TITLE:
                    assert module.get("content", "").strip() in rendered
            if pipeline == "trading_system":
                assert rendered.count(SCHEMA_TITLE) == 1, "Schema duplicated or missing"
                assert rendered.count(schema_modules[0]["content"]) == 1, "Current schema not rendered exactly once"
        # Repeat through the public reader to catch accidental lazy migrations.
        assert pl.active_profile()["id"] == profile_id
    after = {name: (data / name).read_bytes() if (data / name).exists() else None
             for name in prompt_files}
    assert after == before, "Read/render rewrote prompt configuration or created a local overlay"


def main(source: Path, previous: Path) -> None:
    sys.path.insert(0, str(source))
    from astra_backend.admin_auth import AdminAuthStore
    from astra_backend.llm.store import init_llm_config, get_active_llm_runtime
    from astra_gateway import secrets as secret_store
    from astra_gateway.store import GatewayStore
    from scripts import db_manager
    from scripts import prompt_library as pl

    old_library = json.loads((previous / "data/prompt_library.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="astra-upgrade-rehearsal-") as tmp:
        root = Path(tmp)
        old = root / "old"
        data = old / "data"
        data.mkdir(parents=True)
        (old / ".env").write_bytes(b"ASTRA_OKX_ENV=demo\nOKX_IS_SIMULATED=1\nLLM_MODEL=synthetic-model\n")
        old_auth = seed_previous_admin(previous, data, "SyntheticAdmin12345")
        # Initialize actual prior schemas, rather than proving preservation only
        # for arbitrary tables that no runtime component ever opens.
        subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from astra_gateway.store import GatewayStore
from scripts import db_manager
data = Path(sys.argv[2])
gateway = GatewayStore(data / 'astra_gateway.db')
gateway.set_state('synthetic-state', 'retained')
with gateway.connect() as db:
    db.execute("INSERT INTO model_calls(caller,model,reasoning_effort,status,started_at,duration_ms,input_chars,output_chars,prompt_fingerprint) VALUES ('prior','synthetic','high','success','2026-09-30',1,2,3,'fake')")
db_manager.DATA_DIR = str(data)
db_manager.DB_PATH = str(data / 'astra_quant.db')
db_manager.init_database()
with db_manager.get_db() as db:
    db.execute("INSERT INTO trades(bill_id,time,inst,action,direction,venue,environment,source_bill_id,comment) VALUES ('prior','2026-09-30','BTC-USDT-SWAP','close','long','okx','demo','prior','synthetic ledger')")
""", str(previous), str(data)], check=True, capture_output=True, text=True, timeout=30)
        for filename in ("astra_quant.db", "astra_gateway.db"):
            with sqlite3.connect(data / filename) as db:
                db.execute("CREATE TABLE retained_records(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT)")
                db.executemany("INSERT INTO retained_records VALUES(?,?,?)", [
                    (1, "order", "synthetic-order"), (2, "ledger", "retained-中文")])
        key = Fernet.generate_key()
        credentials = {"OKX_API_KEY": "synthetic-okx", "LLM_API_KEY": "synthetic-llm",
                       "BINANCE_API_KEY": "synthetic-retired-slot"}
        (data / ".astra_secret_key").write_bytes(key)
        (data / "astra_secrets.enc").write_bytes(Fernet(key).encrypt(json.dumps(credentials).encode()))
        settings = {
            "llm_models.json": {"defaults_seeded": True,
                                "active_model_id": "synthetic-model", "active_provider_id": "fixture",
                                "providers": [{"id": "fixture", "enabled": True,
                                               "base_url": "https://synthetic.invalid/v1",
                                               "api_key": "synthetic-provider-key",
                                               "api_format": "openai_responses",
                                               "models": [{"id": "synthetic-model"}]}]},
            "venue_env_profile.json": {"environment": "demo", "trading_enabled": False},
            "position_trackers.json": {"synthetic-position": {"quantity": 2}},
            "ai_brain_decisions.json": {"timestamp": "2026-09-30", "decision": "WAIT"},
        }
        for filename, payload in settings.items():
            (data / filename).write_text(json.dumps(payload), encoding="utf-8")
        shutil.copy(previous / "data/prompt_library.json", data / "prompt_library.json")
        profile = copy.deepcopy(old_library["profiles"][old_library["active_profile_id"]])
        profile["id"] = "custom-upgrade"
        profile["name"] = "Synthetic preserved custom profile"
        markers = {}
        for pipeline in pl.TEMPLATE_KEYS:
            marker = "SYNTHETIC_CUSTOM_" + pipeline.upper()
            markers[pipeline] = marker
            profile.setdefault("pipelines", {}).setdefault(pipeline, []).append({
                "id": "fixture-" + pipeline, "title": "Fixture " + pipeline,
                "content": marker, "enabled": True, "locked": False, "source": "custom"})
        (data / "prompt_library.local.json").write_text(json.dumps({
            "version": 2, "active_profile_id": profile["id"],
            "profiles": {profile["id"]: profile}, "revisions": []}), encoding="utf-8")
        before = digests(old)
        candidate = root / "candidate"
        shutil.copytree(old, candidate)
        assert digests(candidate) == before, "Persistent copy changed bytes"
        # Only demonstrably pristine baseline is refreshed; local override survives.
        assert (candidate / "data/prompt_library.json").read_bytes() == (previous / "data/prompt_library.json").read_bytes()
        shutil.copy(source / "data/prompt_library.json", candidate / "data/prompt_library.json")
        for filename, digest in before.items():
            if filename != "data/prompt_library.json":
                assert hashlib.sha256((candidate / filename).read_bytes()).hexdigest() == digest
        restored_data = candidate / "data"
        assert json.loads(Fernet((restored_data / ".astra_secret_key").read_bytes()).decrypt(
            (restored_data / "astra_secrets.enc").read_bytes())) == credentials
        with patch.object(secret_store, "KEY_FILE", restored_data / ".astra_secret_key"), \
             patch.object(secret_store, "STORE_FILE", restored_data / "astra_secrets.enc"):
            assert secret_store.load_secrets() == credentials, "Runtime lost encrypted credential slots"
        restored_admin = AdminAuthStore(restored_data / "astra_admin.db")
        retained_user = restored_admin.get_user(old_auth["user_id"])
        assert retained_user["username"] == "cioperator" and retained_user["role"] == "superadmin"
        assert restored_admin.validate_session(old_auth["session_token"])["id"] == old_auth["user_id"]
        assert restored_admin.login("cioperator", "SyntheticAdmin12345")["session_token"]
        config = init_llm_config(restored_data / "llm_models.json")
        assert config["active_model_id"] == "synthetic-model"
        assert config["active_provider_id"] == "fixture"
        runtime = get_active_llm_runtime(config)
        assert runtime["base_url"] == "https://synthetic.invalid/v1"
        assert runtime["api_key"] == "synthetic-provider-key"
        assert runtime["api_format"] == "openai_responses"
        gateway = GatewayStore(restored_data / "astra_gateway.db")
        assert gateway.get_state("synthetic-state") == "retained"
        assert gateway.model_calls()[0]["caller"] == "prior"
        assert gateway.model_stats()["total_calls"] == 1
        with gateway.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(model_calls)")}
            assert {"cached_tokens", "cache_status", "usage_keys"} <= columns
            db.execute("INSERT INTO model_calls(caller,model,reasoning_effort,status,started_at,duration_ms,input_chars,output_chars,prompt_fingerprint,cached_tokens,cache_status,usage_keys) VALUES ('candidate','synthetic','high','success','2026-10-03',1,2,3,'fake',2,'hit','cached_tokens')")
        with patch.object(db_manager, "DATA_DIR", str(restored_data)), \
             patch.object(db_manager, "DB_PATH", str(restored_data / "astra_quant.db")):
            upgrade = db_manager.init_database()
            assert upgrade["mode"] in {"noop", "rebuild"}
            assert db_manager.init_database()["mode"] == "noop", "Database upgrade was not idempotent"
            with db_manager.get_db() as db:
                assert db.execute("SELECT bill_id,comment FROM trades").fetchall()[0]["bill_id"] == "prior"
                db.execute("INSERT INTO trades(bill_id,time,inst,action,direction,venue,environment,source_bill_id,comment) VALUES ('candidate','2026-10-03','BTC-USDT-SWAP','close','long','okx','demo','candidate','post-start ledger')")
        # A recovery must retain records written after new startup. Exercise the
        # old reader/writer against the upgraded databases without restoring them.
        subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from astra_gateway.store import GatewayStore
from scripts import db_manager
data = Path(sys.argv[2])
gateway = GatewayStore(data / 'astra_gateway.db')
assert {r['caller'] for r in gateway.model_calls()} == {'prior', 'candidate'}
assert gateway.model_stats()['total_calls'] == 2
gateway.set_state('rollback-write', 'ok')
with gateway.connect() as db:
    db.execute("INSERT INTO model_calls(caller,model,reasoning_effort,status,started_at,duration_ms,input_chars,output_chars,prompt_fingerprint) VALUES ('rollback','synthetic','high','success','2026-10-03',1,2,3,'fake')")
db_manager.DATA_DIR = str(data)
db_manager.DB_PATH = str(data / 'astra_quant.db')
db_manager.init_database()
with db_manager.get_db() as db:
    assert {r['bill_id'] for r in db.execute('SELECT bill_id FROM trades')} == {'prior', 'candidate'}
    db.execute("INSERT INTO trades(bill_id,time,inst,action,direction,venue,environment,source_bill_id) VALUES ('rollback','2026-10-03','BTC-USDT-SWAP','close','long','okx','demo','rollback')")
""", str(previous), str(restored_data)], check=True, capture_output=True, text=True, timeout=30)
        assert gateway.get_state("rollback-write") == "ok"
        assert {r["caller"] for r in gateway.model_calls()} == {"prior", "candidate", "rollback"}
        with sqlite3.connect(restored_data / "astra_quant.db") as db:
            assert {row[0] for row in db.execute("SELECT bill_id FROM trades")} == {
                "prior", "candidate", "rollback"}, "Rollback writer lost post-start ledger records"
        for filename in ("astra_admin.db", "astra_quant.db", "astra_gateway.db"):
            with sqlite3.connect(restored_data / filename) as db:
                assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                if filename != "astra_admin.db":
                    assert db.execute("SELECT * FROM retained_records ORDER BY id").fetchall() == [
                        (1, "order", "synthetic-order"), (2, "ledger", "retained-中文")]
        verify_prompt_rendering(pl, restored_data, "custom-upgrade", markers)

        # Production-shaped case: customized v8.4 baseline, no local overlay.
        # Only public old source and invented markers enter this fixture.
        legacy = root / "custom-legacy" / "data"
        legacy.mkdir(parents=True)
        legacy_library = copy.deepcopy(old_library)
        legacy_profile = legacy_library["profiles"][legacy_library["active_profile_id"]]
        legacy_markers = {}
        for pipeline in pl.TEMPLATE_KEYS:
            marker = "SYNTHETIC_LEGACY_BASELINE_" + pipeline.upper()
            legacy_markers[pipeline] = marker
            module = next(m for m in legacy_profile["pipelines"][pipeline]
                          if m["title"] != SCHEMA_TITLE and m.get("content"))
            module["content"] += "\n" + marker
        legacy_bytes = (json.dumps(legacy_library, ensure_ascii=False, indent=2) + "\n").encode()
        (legacy / "prompt_library.json").write_bytes(legacy_bytes)
        assert legacy_bytes != (previous / "data/prompt_library.json").read_bytes()
        carried = root / "custom-candidate" / "data"
        shutil.copytree(legacy, carried)
        assert not (carried / "prompt_library.local.json").exists()
        verify_prompt_rendering(pl, carried, legacy_library["active_profile_id"], legacy_markers)
        assert (carried / "prompt_library.json").read_bytes() == legacy_bytes
        assert not (carried / "prompt_library.local.json").exists()
    print("PASS: v8.4 state bytes, runtime credentials/config, prior admin/session, real database upgrade/rollback and both custom prompt storage modes")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
