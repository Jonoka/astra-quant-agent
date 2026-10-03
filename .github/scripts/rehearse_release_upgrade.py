"""Hosted synthetic v8.5.1-to-v8.6.0 preservation and renderer rehearsal.

No production fixtures, network/model calls, or trading jobs are used.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

from cryptography.fernet import Fernet


SCHEMA_TITLE = "严格 JSON 规范契约与完整输出骨架 (JSON Schema)"

SAVED_RISK = {
    "ASTRA_MIN_ENTRY_CONFIDENCE": ("MIN_ENTRY_CONFIDENCE", 75.0),
    "ASTRA_MIN_RISK_REWARD": ("MIN_RISK_REWARD_RATIO", 1.6),
    "ASTRA_TIME_STOP_HOURS": ("TIME_STOP_HOURS", 4.0),
    "ASTRA_STOP_COOLDOWN_MINUTES": ("STOP_COOLDOWN_MINUTES", 15),
    "ASTRA_SCALE_OUT_TRIGGER_ATR": ("SCALE_OUT_TRIGGER_ATR", 2.2),
    "ASTRA_SCALE_OUT_RATIO": ("SCALE_OUT_RATIO", 0.4),
    "ASTRA_MAX_SAME_DIRECTION_POSITIONS": ("MAX_SAME_DIRECTION_POSITIONS", 3),
    "ASTRA_MIN_LEVERAGE": ("MIN_LEVERAGE", 2.0),
    "ASTRA_MAX_LEVERAGE": ("MAX_LEVERAGE", 5.0),
    "ASTRA_RISK_PER_TRADE_RATIO": ("RISK_PER_TRADE_EQUITY_RATIO", 0.02),
    "ASTRA_MAX_MARGIN_EQUITY_RATIO": ("MAX_MARGIN_EQUITY_RATIO", 0.2),
    "ASTRA_SINGLE_ASSET_EQUITY_RATIO": ("SINGLE_ASSET_EQUITY_RATIO", 0.3),
    "ASTRA_MAX_SINGLE_ASSET_MARGIN_USDT": ("MAX_SINGLE_ASSET_MARGIN", 600.0),
    "ASTRA_DAILY_LOSS_EQUITY_RATIO": ("DAILY_LOSS_EQUITY_RATIO", 0.05),
    "ASTRA_MAX_DAILY_LOSS_USDT": ("MAX_DAILY_LOSS_USDT", 150.0),
}
SAVED_POOL = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ARB", "SUI", "LINK", "ADA", "UNI"]


def verify_saved_strategy(source: Path, data: Path) -> None:
    environment = {**os.environ, **{key: str(value) for key, (_, value) in SAVED_RISK.items()}}
    subprocess.run([sys.executable, "-c", """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts import risk_constants as risk, instrument_pool as pool
expected = json.loads(sys.argv[3])
for key, (attribute, value) in expected.items():
    assert getattr(risk, attribute) == value, (key, getattr(risk, attribute), value)
pool.POOL_FILE = Path(sys.argv[2]) / 'instrument_pool.json'
before = pool.POOL_FILE.read_bytes()
loaded = pool.load_instruments()
assert [item['name'] for item in loaded] == json.loads(sys.argv[4])
assert pool.pool_is_trustworthy()
assert pool.POOL_FILE.read_bytes() == before
""", str(source), str(data), json.dumps(SAVED_RISK), json.dumps(SAVED_POOL)],
        env=environment, check=True, capture_output=True, text=True, timeout=30)


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
        (old / ".env").write_bytes(("ASTRA_OKX_ENV=demo\nOKX_IS_SIMULATED=1\nLLM_MODEL=synthetic-model\n" +
            "".join(f"{key}={value}\n" for key, (_, value) in SAVED_RISK.items())).encode())
        (data / "instrument_pool.json").write_text(json.dumps({"instruments": [
            {"instId": name + "-USDT-SWAP", "name": name, "ctVal": 1.0}
            for name in SAVED_POOL]}), encoding="utf-8")
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
        verify_saved_strategy(source, restored_data)
        # The actual production storage mode has a pristine official baseline
        # and no overlay. Exercise its selected profile and code-owned schema.
        official_data = root / "official-candidate" / "data"
        official_data.mkdir(parents=True)
        shutil.copy(source / "data/prompt_library.json", official_data / "prompt_library.json")
        official = json.loads((official_data / "prompt_library.json").read_text(encoding="utf-8"))
        verify_prompt_rendering(pl, official_data, official["active_profile_id"],
            {pipeline: next(m["content"].strip() for m in official["profiles"][official["active_profile_id"]]["pipelines"][pipeline]
                           if m["title"] != SCHEMA_TITLE and m.get("enabled", True) and m.get("content"))
             for pipeline in pl.TEMPLATE_KEYS})
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

        # Additional supported case: customized prior baseline, no local overlay.
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
    print("PASS: v8.5.1 state, exact saved risk/ten-asset pool, official baseline/schema refresh, credentials/admin, database backward writer and custom prompt modes")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
