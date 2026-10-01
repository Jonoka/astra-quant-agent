"""Hosted-only rehearsal against the exact upstream migration implementation."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from cryptography.fernet import Fernet
from migrate_runtime_env import migrate_env_bytes


def database_content(path: Path) -> list[str]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return list(db.iterdump())


def files_digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def main(source: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "official_migration", source / "scripts/migrate_r20_to_astra.py")
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    sys.path.insert(0, str(source))
    from astra_backend.admin_auth import AdminAuthStore

    with tempfile.TemporaryDirectory(prefix="astra-migration-rehearsal-") as tmp:
        root = Path(tmp)
        data = root / "data"
        data.mkdir()
        (root / "logs").mkdir()
        mig.ROOT, mig.DATA, mig.LOGS = root, data, root / "logs"
        mig.RENAME_PAIRS = mig._pairs(root)
        mig.SUPERSEDED_DIR = root / ".archive/astra-migration-superseded"

        admin = AdminAuthStore(data / "r20_admin.db")
        user = admin.create_user("cioperator", "SyntheticAdmin12345", "superadmin")
        del admin
        for name in ("r20_quant.db", "r20_gateway.db"):
            # Abrupt process exit leaves committed transactions in WAL, exactly
            # the stopped-runtime edge case the official migrator must preserve.
            subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA wal_autocheckpoint=0')
db.execute('CREATE TABLE fixture(id INTEGER PRIMARY KEY, value TEXT)')
db.executemany('INSERT INTO fixture VALUES (?, ?)', [(1, 'retained'), (2, '中文')])
db.commit()
os._exit(0)
""", str(data / name)], check=True)
            assert (data / (name + "-wal")).stat().st_size > 0
        before_dbs = {name: database_content(data / name)
                      for name in ("r20_admin.db", "r20_quant.db", "r20_gateway.db")}

        key = Fernet.generate_key()
        secrets = {"R20_ADMIN_TOKEN": "synthetic-admin-token-123",
                   "R20_QQ_CLIENT_SECRET": "synthetic-qq-secret", "UNCHANGED": "中文"}
        (data / ".r20_secret_key").write_bytes(key)
        (data / "r20_secrets.enc").write_bytes(
            Fernet(key).encrypt(json.dumps(secrets, ensure_ascii=False).encode()))
        config = {"scope": ["data", "r20_backend", "r20_gateway"],
                  "exclude": ["data/r20_admin.db*"],
                  "key_env": "R20_BACKUP_ENCRYPTION_KEY", "note": "keep unrelated r20 note"}
        config_bytes = json.dumps(config).encode()
        (data / "backup_methods.json").write_bytes(config_bytes)
        env_before = (
            b"# R20_ comment remains\r\nR20_OKX_ENV=demo\r\n"
            b" R20_STANDALONE_GATEWAY =true\r\nOKX_IS_SIMULATED=1\r\n"
            b"LLM_API_KEY=synthetic-provider=bytes\r\n"
            b"R20_ADMIN_DB='/app/data/r20_admin.db' # retained comment\r\n"
            b"R20_QUANT_DB=\r\nR20_LABEL='literal R20_ value'\r\n"
            b"export R20_GATEWAY_DB=./data/r20_gateway.db\r\nNO_FINAL_NEWLINE=value")
        expected_env = (
            b"# R20_ comment remains\r\nASTRA_OKX_ENV=demo\r\n"
            b" ASTRA_STANDALONE_GATEWAY =true\r\nOKX_IS_SIMULATED=1\r\n"
            b"LLM_API_KEY=synthetic-provider=bytes\r\n"
            b"ASTRA_ADMIN_DB='/app/data/astra_admin.db' # retained comment\r\n"
            b"ASTRA_QUANT_DB=\r\nASTRA_LABEL='literal R20_ value'\r\n"
            b"export ASTRA_GATEWAY_DB=./data/astra_gateway.db\r\nNO_FINAL_NEWLINE=value")
        env_after = migrate_env_bytes(env_before, mig.RUNTIME_FILE_NAMES)
        assert env_after == expected_env
        assert migrate_env_bytes(env_after, mig.RUNTIME_FILE_NAMES) == env_after
        for conflict in (b"R20_OKX_ENV=demo\nASTRA_OKX_ENV=live\n",
                         b"R20_SETUP_TOKEN=first\nR20_SETUP_TOKEN=second\n"):
            try:
                migrate_env_bytes(conflict, mig.RUNTIME_FILE_NAMES)
            except ValueError:
                pass
            else:
                raise AssertionError("Conflicting environment keys were accepted")
        assert migrate_env_bytes(b"R20_ADMIN_DB=/custom/keep.db\n", mig.RUNTIME_FILE_NAMES) == (
            b"ASTRA_ADMIN_DB=/custom/keep.db\n")

        before_files = files_digest(root)
        assert mig.main([]) == 0
        assert files_digest(root) == before_files, "Dry-run changed runtime files"
        assert mig.main(["--check"]) == 3
        assert mig.main(["--apply"]) == 0
        assert mig.main(["--check"]) == 0
        for old, content in before_dbs.items():
            new = old.replace("r20_", "astra_", 1)
            assert database_content(data / new) == content, "Database content changed"
            assert not list(data.glob(old + "*")), "Legacy WAL/SHM orphan remains"
        assert (data / ".astra_secret_key").read_bytes() == key
        expected_secrets = {"ASTRA_" + k[4:] if k.startswith("R20_") else k: v
                            for k, v in secrets.items()}
        assert mig._load_store(data / "astra_secrets.enc", data / ".astra_secret_key") == expected_secrets
        assert (root / ".archive/astra-migration-config-backup/backup_methods.json").read_bytes() == config_bytes
        expected_config = json.loads(config_bytes)
        expected_config["scope"] = ["data", "astra_backend", "astra_gateway"]
        expected_config["exclude"] = ["data/astra_admin.db*"]
        expected_config["key_env"] = "ASTRA_BACKUP_ENCRYPTION_KEY"
        assert json.loads((data / "backup_methods.json").read_bytes()) == expected_config
        before_repeat = files_digest(root)
        assert mig.main(["--apply"]) == 0
        assert files_digest(root) == before_repeat, "Repeated migration changed state"
        migrated_admin = AdminAuthStore(data / "astra_admin.db")
        assert migrated_admin.get_user(user["id"])["username"] == "cioperator"
        assert migrated_admin.login("cioperator", "SyntheticAdmin12345")["session_token"]
    print("PASS: official migration, admin/database/WAL integrity, encrypted values, config and dotenv preservation")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
