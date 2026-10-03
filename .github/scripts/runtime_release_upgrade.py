"""Restricted v8.5.1 -> v8.6.0 runtime cutover; never builds or migrates data.

Run as root on the approved host, after hosted checks and image pull. The JSON
manifest is an operator attestation, not a substitute for successful hosted CI.
An optional existing administrator session arrives through an inherited descriptor.
All command output is captured; failures expose gate names, never subprocess data.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

ROOT = Path("/opt/r20-quantum-trader")
BACKUPS = Path("/opt/r20-quantum-trader-backups")
PROJECT = "r20-quantum-trader"
SERVICES = ("backend", "gateway")
NAMES = tuple("astraquant-" + service for service in SERVICES)
PREVIOUS = "a913957689a920c6e0eba835d1570d7d83e7f4a9"
RELEASE = "1b332aa58dfc58bfde8bbdaff0faeeb86631313f"
OLD_IMAGE = "ghcr.io/jonoka/astra-quant-agent@sha256:878f03e298a37615903c9fc629c8e12c5a9b65f238c9dd269dd2a85c745c197c"
IMAGE_RE = r"ghcr\.io/jonoka/astra-quant-agent@sha256:[0-9a-f]{64}"
WRITABLE = (".env", "data", "logs", "backups", ".archive", "plugins")
OVERRIDE = "docker-compose.override.yml"
PROMPT = "data/prompt_library.json"
# Dynamic positions, decisions, ledgers, sessions and scheduler records are
# intentionally copied after stop, rather than being mistaken for config drift.
CONFIG = (
    ".env", "data/llm_models.json", "data/llm_providers.json",
    "data/venue_env_profile.json", "data/prompt_library.json",
    "data/prompt_library.local.json", "data/risk_config.json",
    "data/council_config.json", "data/notification_schedule.json",
    "data/evolution_config.json", "data/trading_session.json",
    "data/instrument_pool.json",
    "data/astra_secrets.enc", "data/.astra_secret_key",
    "data/astra_backup_secrets.enc", "data/.astra_backup_secret_key",
)
LOG_PATTERNS = {
    "traceback": rb"Traceback \(most recent call last\)",
    "import_failure": rb"(?:ModuleNotFoundError|ImportError):",
    "fatal": rb"\b(?:FATAL|CRITICAL)\b",
    "duplicate_worker": rb"gateway worker already running",
    "error": rb"\bERROR\b",
}


class GateError(RuntimeError):
    """Only constant, secret-free gate identifiers may be exposed."""


def require(condition, gate):
    if not condition:
        raise GateError(gate)


def run(*args, timeout=360):
    try:
        result = subprocess.run([str(a) for a in args], check=True, capture_output=True,
                                timeout=timeout)
        return result.stdout + result.stderr if args[:2] == ("docker", "logs") else result.stdout
    except (subprocess.SubprocessError, OSError):
        raise GateError("command_failed") from None


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def plain_path(path):
    """Reject traversal and symlink aliases, including nonexistent destinations."""
    require(path.is_absolute() and ".." not in path.parts, "absolute_plain_path")
    require(path.resolve() == path, "symlink_path")
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), "symlink_path")
    return path


def operation_path(path):
    plain_path(path)
    plain_path(ROOT)
    plain_path(BACKUPS)
    require(path.parent == BACKUPS and re.fullmatch(r"upgrade-v8\.6\.0-[A-Za-z0-9_-]+", path.name),
            "operation_path")
    require(path.is_dir() and path.stat().st_uid == 0 and
            stat.S_IMODE(path.stat().st_mode) == 0o700, "operation_permissions")
    require(BACKUPS.is_dir() and BACKUPS.stat().st_uid == 0 and
            not (BACKUPS.stat().st_mode & 0o022), "backup_parent_permissions")
    return path


def relative_path(value):
    path = Path(value)
    require(value and not path.is_absolute() and ".." not in path.parts and
            "." not in path.parts and "\\" not in value and path.as_posix() == value,
            "manifest_relative_path")
    return path


def source_representation(actual, reference):
    """Accept raw bytes or exclusively the proven UTF-8 LF-to-CRLF checkout."""
    if actual == reference:
        return "raw"
    try:
        text = reference.decode("utf-8", errors="strict")
        actual.decode("utf-8", errors="strict")
    except UnicodeError:
        raise GateError("previous_runtime_source_drift") from None
    require(all(ord(c) >= 32 and ord(c) != 127 or c in "\n\t" for c in text) and "\n" in text and
            actual == reference.replace(b"\n", b"\r\n"), "previous_runtime_source_drift")
    return "proven_utf8_crlf"


def tree_manifest(root):
    """Includes empty directories and the root's owner/mode; rejects special files."""
    plain_path(root)
    require(root.exists(), "tree_missing")
    paths = [root, *sorted(root.rglob("*"))] if root.is_dir() else [root]
    result = {}
    for path in paths:
        info = path.lstat()
        value = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
        if stat.S_ISLNK(info.st_mode):
            # Both pinned releases have exactly this Git link. Never traverse
            # it, and validate its lexical target as well as its resolved path.
            link = os.readlink(path)
            relative = path.relative_to(root).as_posix()
            require((relative == "frontend/public/images" or
                     (root.name == "frontend" and relative == "public/images")) and
                    path.parts[-3:] == ("frontend", "public", "images") and
                    link == "../../docs/images", "tree_symlink_not_allowed")
            source_root = path.parents[2]
            plain_path(source_root)
            resolved = path.resolve()
            require(resolved == source_root / "docs/images" and
                    source_root in resolved.parents, "tree_symlink_escape")
            value["symlink"] = link
        elif stat.S_ISREG(info.st_mode):
            value.update(sha256=digest(path), bytes=info.st_size)
        elif stat.S_ISDIR(info.st_mode):
            value["directory"] = True
        else:
            raise GateError("tree_special_file")
        result[path.relative_to(root).as_posix()] = value
    return result


def files_manifest(root):
    return {name: value["sha256"] for name, value in tree_manifest(root).items()
            if "sha256" in value}


def canonical_container(value):
    result = copy.deepcopy(value)
    result["mounts"] = sorted(result["mounts"], key=lambda m: (m["Destination"], m["Source"]))
    result["ports"] = {port: sorted(bindings, key=lambda b: (b["HostIp"], b["HostPort"]))
                       if bindings else bindings for port, bindings in (result["ports"] or {}).items()}
    return result


def containers():
    ids = run("docker", "ps", "-aq").split()
    if not ids:
        return {}
    raw = json.loads(run("docker", "inspect", *(p.decode() for p in ids)))
    return {c["Name"].lstrip("/"): canonical_container({
        "id": c["Id"], "image": c["Image"], "ref": c["Config"]["Image"],
        "restart": c["RestartCount"], "state": c["State"]["Status"],
        "health": c["State"].get("Health", {}).get("Status"),
        "mounts": c["Mounts"], "ports": c["HostConfig"]["PortBindings"],
        "healthcheck": c["Config"].get("Healthcheck"),
        "project": (c["Config"]["Labels"] or {}).get("com.docker.compose.project"),
        "service": (c["Config"]["Labels"] or {}).get("com.docker.compose.service"),
    }) for c in raw}


def unchanged_unrelated(before, after):
    def protected(values):
        return {name: {k: v for k, v in canonical_container(c).items() if k != "health"}
                for name, c in values.items() if c["project"] != PROJECT}
    require(protected(before) == protected(after), "unrelated_container_drift")


def patch_override(path, previous, image):
    """Use YAML scalar marks so comments, quoting, CRLF and all other bytes survive."""
    from yaml.nodes import MappingNode, ScalarNode

    def pairs(node):
        require(isinstance(node, MappingNode), "override_mapping")
        values = {}
        for key, value in node.value:
            require(isinstance(key, ScalarNode) and key.value not in values, "override_duplicate_key")
            values[key.value] = value
        return values

    text = path.read_bytes().decode("utf-8")
    before = yaml.safe_load(text)
    expected = {"services": {s: {"image": previous} for s in SERVICES}}
    require(before == expected, "override_scope")
    nodes = pairs(yaml.compose(text))
    require(set(nodes) == {"services"}, "override_scope")
    service_nodes = pairs(nodes["services"])
    require(set(service_nodes) == set(SERVICES), "override_scope")
    replacements = []
    for service in SERVICES:
        fields = pairs(service_nodes[service])
        require(set(fields) == {"image"}, "override_scope")
        node = fields["image"]
        require(isinstance(node, ScalarNode) and node.value == previous and
                node.style in (None, "'", '"'), "override_image_scalar")
        quote = node.style or ""
        replacements.append((node.start_mark.index, node.end_mark.index, quote + image + quote))
    require(replacements[0][:2] != replacements[1][:2], "override_alias")
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    require(yaml.safe_load(text) == {"services": {s: {"image": image} for s in SERVICES}},
            "override_patch_scope")
    path.write_bytes(text.encode("utf-8"))


@contextmanager
def upgrade_lock(path):
    import fcntl
    plain_path(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                stat.S_IMODE(info.st_mode) == 0o600, "lock_permissions")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GateError("upgrade_lock_busy") from None
        yield
    finally:
        os.close(fd)  # Never unlink: contenders must keep referring to the same inode.


def request(base, path, token=None, expected=200):
    headers = {"User-Agent": "AstraQuant-Deployment/1.0"}
    if token:
        headers["X-Astra-Session"] = token
    req = Request(base + path, headers=headers)
    try:
        response = urlopen(req, timeout=15)
    except HTTPError as exc:
        response = exc
    with response:
        require(response.status == expected, "http_status")
        body = response.read()
        return json.loads(body) if "application/json" in response.headers.get("Content-Type", "") else body


def env_gate(root):
    path = root / ".env"
    require(path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600 and
            path.stat().st_uid == path.stat().st_gid == 0, "dotenv_permissions")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        require(key not in values, "dotenv_duplicate_key")
        values[key] = value
    require(values.get("ASTRA_STANDALONE_GATEWAY") == "true" and
            values.get("ASTRA_OKX_ENV") == "demo" and values.get("OKX_IS_SIMULATED") == "1",
            "effective_safe_flags")
    for key in ("ASTRA_ADMIN_DB", "ASTRA_QUANT_DB", "ASTRA_GATEWAY_DB"):
        require(not values.get(key), "external_database_path")


def db_state(root):
    result = {}
    for path in sorted((root / "data").rglob("*")):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            sqlite = stream.read(16) == b"SQLite format 3\0"
        if not sqlite:
            continue
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15) as db:
            require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "sqlite_integrity")
            tables = {}
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                quoted = '"' + name.replace('"', '""') + '"'
                tables[name] = {
                    "columns": [list(row[1:]) for row in db.execute("PRAGMA table_info(" + quoted + ")")],
                    "rows": db.execute("SELECT COUNT(*) FROM " + quoted).fetchone()[0],
                }
            indexes = [list(row) for row in db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('index','trigger','view') ORDER BY type,name")]
            result[path.relative_to(root).as_posix()] = {"tables": tables, "indexes": indexes}
    require("data/astra_admin.db" in result, "administrator_database_missing")
    return result


def compatible_databases(before, after):
    """Both pinned releases own identical SQLite schemas; reject migrations."""
    for name, old in before.items():
        require(name in after, "database_removed")
        new = after[name]
        for table, values in old["tables"].items():
            require(table in new["tables"], "database_table_removed")
            columns = {c[0]: c for c in new["tables"][table]["columns"]}
            prior = {c[0]: c for c in values["columns"]}
            require(all(columns.get(k) == c for k, c in prior.items()), "database_column_changed")
            extra = {k: c for k, c in columns.items() if k not in prior}
            require(not extra, "unknown_database_migration")
            if name == "data/astra_quant.db" and table != "sqlite_sequence":
                require(new["tables"][table]["rows"] >= values["rows"], "trading_rows_lost")
        require(new["indexes"] == old["indexes"], "database_index_changed")
        extra_tables = set(new["tables"]) - set(old["tables"])
        require(not extra_tables, "unknown_database_table")


def admin_identity(root):
    with sqlite3.connect((root / "data/astra_admin.db").as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id,username,password_hash,salt,iterations,role,enabled,created_at "
                          "FROM admin_users ORDER BY id").fetchall()
    require(rows and any(row[5] == "superadmin" and row[6] for row in rows), "initialized_administrator")
    return sha(repr(rows).encode())


def source_literal(path, name):
    for node in ast.parse(path.read_bytes()).body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(node.value)
    raise GateError("source_compatibility_literal")


def read_session(fd):
    if fd is None:
        return None
    require(fd >= 0 and fd not in (1, 2), "session_descriptor")
    session = os.read(fd, 4097).decode("ascii").strip()
    require(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", session), "session_input")
    return session


class Upgrade:
    def __init__(self, operation, session):
        self.op = operation_path(operation)
        self.session = session
        self.manifest = json.loads((self.op / "manifest.json").read_bytes())
        self.manifest_sha = digest(self.op / "manifest.json")
        self.image = self.manifest["image"]
        require(re.fullmatch(IMAGE_RE, self.image) and self.image != OLD_IMAGE, "candidate_digest")
        require(self.manifest["previous_source"] == PREVIOUS and
                self.manifest["release_source"] == RELEASE and self.manifest["schema"] == 1,
                "source_pins")
        self.state = self.read("state.json") if (self.op / "state.json").exists() else {}

    def read(self, name):
        return json.loads((self.op / name).read_bytes())

    def save(self, name, value):
        path = plain_path(self.op / name)
        temporary = plain_path(self.op / (name + ".tmp"))
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(self.op, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def phase(self, phase):
        self.state["phase"] = phase
        self.save("state.json", self.state)

    def allowed(self, path):
        plain_path(path)
        require(path == ROOT or ROOT in path.parents or self.op in path.parents, "copy_move_boundary")
        require(path not in (self.op, BACKUPS), "copy_move_boundary")
        return path

    def copy(self, source, target):
        self.allowed(source)
        self.allowed(target)
        require(source.exists() and not target.exists(), "copy_destination_exists")
        before = tree_manifest(source)
        run("cp", "-a", "--", source, target)
        require(tree_manifest(source) == before == tree_manifest(target), "copy_byte_owner_mode_mismatch")

    def move(self, source, target):
        self.allowed(source)
        self.allowed(target)
        require(source.exists() and not target.exists(), "rename_destination_exists")
        os.rename(source, target)

    def compose(self, source, *args):
        self.allowed(source)
        return run("docker", "compose", "-p", PROJECT, "--project-directory", ROOT,
                   "--env-file", ROOT / ".env", "-f", source / "docker-compose.yml",
                   "-f", source / OVERRIDE, *args)

    def image_metadata(self, previous=False):
        image = OLD_IMAGE if previous else self.image
        metadata = json.loads(run("docker", "image", "inspect", image))[0]
        labels = metadata["Config"]["Labels"]
        require(image in metadata["RepoDigests"] and metadata["Os"] == "linux" and
                metadata["Architecture"] == "amd64", "image_digest_platform")
        require(labels["org.opencontainers.image.revision"] == (PREVIOUS if previous else RELEASE) and
                labels["org.opencontainers.image.version"] == ("v8.5.1" if previous else "v8.6.0") and
                labels["org.opencontainers.image.source"] == "https://github.com/0xethanq/astra-quant-agent",
                "image_source_version")
        return metadata["Id"]

    def sources(self):
        require(digest(self.op / "manifest.json") == self.manifest_sha, "manifest_drift")
        for label in ("previous", "release"):
            official_link = self.op / ("source-" + label) / "frontend/public/images"
            require(official_link.is_symlink() and os.readlink(official_link) == "../../docs/images",
                    "official_source_symlink_missing")
            expected = self.manifest["source_files"][label]
            require(expected and "docker-compose.yml" in expected and PROMPT in expected, "source_manifest")
            for name, value in expected.items():
                relative_path(name)
                require(re.fullmatch(r"[0-9a-f]{64}", value), "source_hash")
            require(files_manifest(self.op / ("source-" + label)) == expected, "raw_source_drift")
        proof = self.manifest["compatibility"]
        require(proof["previous_source"] == PREVIOUS and proof["release_source"] == RELEASE and
                proof["image"] == self.image and proof["rollback_preserves_new_records"] is True,
                "compatibility_identity")
        require(re.fullmatch(r"https://github\.com/[Jj]onoka/astra-quant-agent/actions/runs/[0-9]+", proof["hosted_run"]),
                "compatibility_evidence")
        require(all(proof["checks"].get(k) == "passed" for k in (
            "state_preservation", "backward_read_write", "helper_tests", "published_compose_smoke")),
            "backward_compatibility_unverified")
        require(set(proof["reviewed_formats"]) == {"sqlite", "encrypted_credentials", "settings", "prompts", "trading_records"},
                "state_format_review_required")
        old, new = self.op / "source-previous", self.op / "source-release"
        require(source_literal(old / "astra_backend/admin_auth.py", "SCHEMA") ==
                source_literal(new / "astra_backend/admin_auth.py", "SCHEMA"), "administrator_schema_changed")
        for name in ("scripts/db_manager.py", "astra_backend/risk_reservation.py",
                     "astra_gateway/store.py", "Dockerfile", "docker-compose.yml"):
            require(digest(old / name) == digest(new / name), "trading_schema_source_changed")

    def guard(self, root):
        # Record every non-writable source/custom file, plus stable configuration.
        result = {}
        for child in sorted(root.iterdir()):
            if child.name in WRITABLE or child.name == ".git":
                continue
            result[child.name] = tree_manifest(child)
        for name in CONFIG:
            path = root / name
            result[name] = tree_manifest(path) if path.exists() else None
        return result

    def check_drift(self):
        require(self.manifest_sha == self.state["manifest_sha"], "manifest_drift")
        self.sources()
        require(self.guard(ROOT) == self.state["guard"], "source_config_drift")
        require(tree_manifest(self.op / "candidate") == self.state["candidate"], "candidate_drift")

    def source_runtime(self):
        evidence = {}
        for name, expected in self.manifest["source_files"]["previous"].items():
            if Path(name).parts[0] in (*WRITABLE, ".github"):
                continue
            path = ROOT / name
            require(path.is_file(), "previous_runtime_source_drift")
            reference = (self.op / "source-previous" / name).read_bytes()
            require(sha(reference) == expected, "raw_source_drift")
            actual = path.read_bytes()
            evidence[name] = {"sha256": sha(actual), "reference_sha256": expected,
                              "representation": source_representation(actual, reference)}
        known = {Path(n).parts[0] for n in self.manifest["source_files"]["previous"]}
        extras = sorted(p.name for p in ROOT.iterdir()
                        if p.name not in known | set(WRITABLE) | {".git", OVERRIDE})
        # Do not silently drop a custom file nested in an application source tree.
        for path in ROOT.rglob("*"):
            name = path.relative_to(ROOT).as_posix()
            top = path.relative_to(ROOT).parts[0]
            if path.is_file() and top in known - set(WRITABLE) - {".github"}:
                require(name in self.manifest["source_files"]["previous"] or
                        "__pycache__" in path.parts, "custom_nested_source_requires_review")
        self.save("previous-source-representations.json", evidence)
        return extras

    def validate_compose(self, candidate):
        self.compose(ROOT, "config", "--quiet")
        self.compose(candidate, "config", "--quiet")
        before = json.loads(self.compose(ROOT, "config", "--format", "json"))
        after = json.loads(self.compose(candidate, "config", "--format", "json"))
        require(set(before["services"]) == set(SERVICES), "compose_services")
        expected = copy.deepcopy(before)
        for service in SERVICES:
            spec = expected["services"][service]
            require(spec["image"] == OLD_IMAGE, "compose_previous_image")
            spec["image"] = self.image
            volumes = spec["volumes"]
            require(len(volumes) == 4 and {v["target"] for v in volumes} == {"/app/" + n for n in (".env", "data", "logs", "backups")},
                    "compose_mount_set")
            for volume in volumes:
                require(volume["type"] == "bind" and not volume.get("read_only", False) and
                        volume["source"] == str(ROOT / volume["target"].removeprefix("/app/")), "compose_mount")
        require(after == expected, "compose_unexpected_change")

    def runtime(self, previous, baseline=None):
        now = containers()
        require({n for n, c in now.items() if c["project"] == PROJECT} == set(NAMES), "project_inventory")
        image_id = self.image_metadata(previous)
        for service, name in zip(SERVICES, NAMES):
            c = now[name]
            require(c["service"] == service and c["ref"] == (OLD_IMAGE if previous else self.image) and
                    c["image"] == image_id, "runtime_identity")
            require(c["state"] == "running" and c["health"] == "healthy" and c["restart"] == 0,
                    "runtime_health_restart")
            mounts = {m["Destination"]: m for m in c["mounts"]}
            targets = (".env", "data", "logs", "backups")
            require(len(c["mounts"]) == len(targets) and set(mounts) == {"/app/" + n for n in targets}, "runtime_mount_set")
            require(all(mounts["/app/" + n]["Type"] == "bind" and mounts["/app/" + n]["RW"] and
                        mounts["/app/" + n]["Source"] == str(ROOT / n) for n in targets), "runtime_mount")
            require(c["healthcheck"] and c["healthcheck"]["Test"][0] == "CMD-SHELL", "runtime_healthcheck")
            if baseline:
                old = baseline[name]
                expected_mounts = old["mounts"]
                require(c["mounts"] == expected_mounts and c["ports"] == old["ports"] and
                        c["healthcheck"] == old["healthcheck"], "runtime_mount_port_healthcheck_drift")
        if baseline:
            unchanged_unrelated(baseline, now)
        return now

    def endpoints(self, previous):
        version = "8.5.1" if previous else "8.6.0"
        local = "http://127.0.0.1:8080"
        h = request(local, "/api/v1/health")
        require(h["version"] == version and h["status"] == "ok", "local_health")
        for base in (local, "https://trader.jo2api.com"):
            require(request(base, "/api/v1/status")["version"] == version and
                    request(base, "/api/v1/health")["version"] == version, "endpoint_version")
            request(base, "/api/v1/admin/auth/me", expected=401)
            require(request(base, "/api/v1/admin/auth/status")["initialized"] is True, "auth_initialized")
        identity = None
        if self.session:
            user = request(local, "/api/v1/admin/auth/me", token=self.session)["user"]
            require(user["role"] in ("admin", "superadmin") and user["enabled"], "existing_session_auth")
            identity = sha(json.dumps({k: user[k] for k in ("id", "username", "role", "enabled")}, sort_keys=True).encode())
        return {"credentials": h["credentials"], "authenticated_identity": identity,
                "authenticated_session_probe": "passed" if self.session else "unexercised_no_supplied_session"}

    def worker_state(self):
        code = r'''from pathlib import Path
import json, os
workers, backends = [], []
for p in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        args = p.read_bytes().split(b'\0')
        if b'astra_gateway.worker' in args: workers.append(int(p.parent.name))
        if b'astra_backend.app:app' in args: backends.append(int(p.parent.name))
    except OSError: pass
owner, held = None, False
if workers:
    owner = int(Path('/app/data/astra_gateway.pid').read_text().strip())
    ino = str(Path('/app/data/.astra_gateway.lock').stat().st_ino)
    held = any(' FLOCK ' in line and line.split()[4] == str(owner) and
               line.split()[5].split(':')[-1] == ino for line in Path('/proc/locks').read_text().splitlines())
print(json.dumps({'workers':workers,'backends':backends,'owner':owner,'held':held}))
'''
        result = {}
        for service, name in zip(SERVICES, NAMES):
            value = json.loads(run("docker", "exec", name, "python3", "-c", code))
            require(len(value["workers"]) == (1 if service == "gateway" else 0) and
                    len(value["backends"]) == (1 if service == "backend" else 0), "single_worker")
            if service == "gateway":
                require(value["held"] and value["owner"] == value["workers"][0], "worker_lock_owner")
            result[service] = value
        return result

    def strategy_reader(self, previous=False):
        # Actual application readers only; no job, model, order or evolution call.
        code = r'''import json, hashlib
from pathlib import Path
from scripts import risk_constants as risk, instrument_pool as pool, prompt_library as pl
expected = {'MIN_ENTRY_CONFIDENCE':75.0,'MIN_RISK_REWARD_RATIO':1.6,
 'TIME_STOP_HOURS':4.0,'STOP_COOLDOWN_MINUTES':15,'SCALE_OUT_TRIGGER_ATR':2.2,
 'SCALE_OUT_RATIO':0.4,'MAX_SAME_DIRECTION_POSITIONS':3,'MIN_LEVERAGE':2.0,
 'MAX_LEVERAGE':5.0,'RISK_PER_TRADE_EQUITY_RATIO':0.02,'MAX_MARGIN_EQUITY_RATIO':0.2,
 'SINGLE_ASSET_EQUITY_RATIO':0.3,'MAX_SINGLE_ASSET_MARGIN':600.0,
 'DAILY_LOSS_EQUITY_RATIO':0.05,'MAX_DAILY_LOSS_USDT':150.0}
assert all(getattr(risk,k) == v for k,v in expected.items())
paths = [Path('/app/data') / n for n in ('instrument_pool.json','prompt_library.json','prompt_library.local.json')]
before = [p.read_bytes() if p.exists() else None for p in paths]
items = pool.load_instruments()
assert pool.pool_is_trustworthy()
assert [i['name'] for i in items] == ['BTC','ETH','SOL','XRP','DOGE','ARB','SUI','LINK','ADA','UNI']
profile = pl.active_profile()
assert profile['id'] == 'allpattern_swing'
base = pl.base_template_text('trading_system')
modules = pl.text_to_modules(base,'base')
assert len(modules) == 1 and 'macro_assessment' in modules[0]['content']
rendered = pl.apply_module_layout(base,profile,'trading_system','upgrade')
assert rendered.count(modules[0]['content']) == 1
assert before == [p.read_bytes() if p.exists() else None for p in paths]
print(json.dumps({'risk':expected,'pool':[i['name'] for i in items],
 'profile':profile['id'],'schema_sha256':hashlib.sha256(modules[0]['content'].encode()).hexdigest(),
 'rendered_sha256':hashlib.sha256(rendered.encode()).hexdigest()}))
'''
        result = json.loads(run("docker", "exec", NAMES[0], "python3", "-c", code))
        self.save("previous-strategy-reader.json" if previous else "release-strategy-reader.json", result)
        if not previous and self.state.get("prompt_refreshed"):
            require(digest(ROOT / PROMPT) == digest(self.op / "source-release" / PROMPT), "refreshed_prompt_drift")
        return result

    def capacity(self):
        import shutil
        require(ROOT.stat().st_dev == self.op.stat().st_dev, "atomic_rename_filesystem")
        size = sum(v.get("bytes", 0) for v in tree_manifest(ROOT).values())
        # Full stopped snapshot, staged writable state, recovery tree and recovery
        # displacement copies can coexist; never free space by deleting evidence.
        require(shutil.disk_usage(self.op).free > 5 * size + 1024**3, "snapshot_recovery_capacity")

    def preflight(self):
        require(not self.state and not (self.op / "candidate").exists(), "operation_already_prepared")
        self.sources()
        self.capacity()
        extras = self.source_runtime()
        env_gate(ROOT)
        baseline = self.runtime(True)
        endpoints = self.endpoints(True)
        self.worker_state()
        self.strategy_reader(previous=True)
        self.image_metadata(False)  # Must already have been pulled by digest.
        databases = db_state(ROOT)
        guard = self.guard(ROOT)
        self.copy(self.op / "source-release", self.op / "candidate")
        self.copy(ROOT / OVERRIDE, self.op / "candidate" / OVERRIDE)
        patch_override(self.op / "candidate" / OVERRIDE, OLD_IMAGE, self.image)
        self.validate_compose(self.op / "candidate")
        self.state = {"manifest_sha": self.manifest_sha, "guard": guard,
                      "candidate": tree_manifest(self.op / "candidate"), "baseline": baseline,
                      "endpoints": endpoints, "admin": admin_identity(ROOT), "databases": databases,
                      "extras": extras}
        self.check_drift()
        self.phase("prepared")

    def stop(self):
        # ID selection still works if candidate .env/Compose is damaged.
        now = containers()
        targets = {n: c for n, c in now.items() if c["project"] == PROJECT}
        require(set(targets) <= set(NAMES) and all(c["service"] == n.removeprefix("astraquant-")
                for n, c in targets.items()), "stop_project_scope")
        if targets:
            run("docker", "stop", "-t", "60", *(c["id"] for c in targets.values()))
        after = containers()
        require(all(after[n]["state"] == "exited" and after[n]["id"] == c["id"]
                    for n, c in targets.items()), "services_not_stopped")

    def carry(self, source, target, prefix):
        for name in (*WRITABLE, *self.state["extras"]):
            relative_path(name)
            require(len(Path(name).parts) == 1, "writable_top_level")
            existing = target / name
            # Move even absent-in-runtime release seeds aside: no default may
            # overwrite an intentional deletion of a persisted file/directory.
            if existing.exists():
                self.move(existing, self.op / (prefix + "-" + name))
            if (source / name).exists():
                self.copy(source / name, existing)

    def prompt_refresh(self, candidate):
        path = candidate / PROMPT
        old = self.op / "source-previous" / PROMPT
        new = self.op / "source-release" / PROMPT
        refreshed = path.is_file() and digest(path) == digest(old)
        if refreshed:
            # Retain runtime owner and mode, replacing only proven baseline bytes.
            path.write_bytes(new.read_bytes())
        counts = {"baseline": 0, "custom": 0}
        for plugin in (candidate / "plugins").rglob("*"):
            if plugin.is_file():
                previous = self.op / "source-previous" / plugin.relative_to(candidate)
                label = "baseline" if previous.is_file() and digest(plugin) == digest(previous) else "custom"
                counts[label] += 1
        self.state["prompt_refreshed"] = refreshed
        self.save("baseline-classification.json", {"prompt_refreshed": refreshed,
                  "plugins_retained_without_bind": counts})

    def restore_prompt(self, root):
        path = root / PROMPT
        if self.state.get("prompt_refreshed") and path.is_file() and digest(path) == digest(self.op / "source-release" / PROMPT):
            path.write_bytes((self.op / "source-previous" / PROMPT).read_bytes())

    def log_marks(self):
        return {p.relative_to(ROOT / "logs").as_posix(): {"bytes": p.stat().st_size, "sha256": digest(p)}
                for p in (ROOT / "logs").rglob("*") if p.is_file()}

    def log_check(self):
        counts = dict.fromkeys(LOG_PATTERNS, 0)
        for path in (ROOT / "logs").rglob("*"):
            if not path.is_file():
                continue
            mark = self.state["log_marks"].get(path.relative_to(ROOT / "logs").as_posix())
            data = path.read_bytes()
            if mark and sha(data[:mark["bytes"]]) == mark["sha256"]:
                data = data[mark["bytes"]:]
            for key, pattern in LOG_PATTERNS.items():
                counts[key] += len(re.findall(pattern, data, re.I))
        for name in NAMES:
            data = run("docker", "logs", "--since", self.state["started"], name)
            for key, pattern in LOG_PATTERNS.items():
                counts[key] += len(re.findall(pattern, data, re.I))
        self.save("new-log-counts.json", counts)
        require(not any(count for name, count in counts.items() if name != "error"), "fatal_new_logs")
        return counts

    def start(self, previous):
        self.state["log_marks"] = self.log_marks()
        self.state["started"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.phase("recovery-starting" if previous else "starting")
        self.compose(ROOT, "config", "--quiet")
        self.compose(ROOT, "up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "240", *SERVICES)

    def continuity(self):
        env_gate(ROOT)
        require(admin_identity(ROOT) == self.state["admin"], "administrator_identity_drift")
        compatible_databases(self.state["databases"], db_state(ROOT))
        for name, expected in self.state.get("protected_config", {}).items():
            path = ROOT / name
            require((tree_manifest(path) if path.exists() else None) == expected, "configuration_credential_drift")

    def verify(self, previous=False):
        require(self.state and self.manifest_sha == self.state["manifest_sha"], "operation_state_identity")
        self.strategy_reader(previous)
        samples = []
        first_workers = None
        for index in range(7):
            now = self.runtime(previous, self.state["baseline"])
            require(self.endpoints(previous) == self.state["endpoints"], "credential_auth_continuity")
            workers = self.worker_state()
            require(first_workers is None or first_workers == workers, "worker_pid_changed")
            first_workers = workers
            self.continuity()
            counts = self.log_check()
            samples.append({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "healthy": True, "restart_zero": True, "single_worker": True,
                            "log_categories": counts,
                            "unrelated_health": {n: c["health"] for n, c in now.items() if c["project"] != PROJECT}})
            self.save("rollback-observation.json" if previous else "upgrade-observation.json", samples)
            if index != 6:
                time.sleep(15)

    def execute(self):
        require(self.state.get("phase") == "prepared", "preflight_required")
        # Every fallible pre-stop gate stays outside the recovery block; drift
        # must not restart or otherwise disturb a healthy previous deployment.
        self.check_drift()
        self.capacity()
        self.runtime(True, self.state["baseline"])
        require(self.endpoints(True) == self.state["endpoints"], "prestop_auth_drift")
        self.validate_compose(self.op / "candidate")
        self.image_metadata(False)
        self.check_drift()
        try:
            self.phase("stopping")
            self.stop()
            self.phase("stopped")
            require(self.guard(ROOT) == self.state["guard"], "poststop_config_drift")
            self.state["databases"] = db_state(ROOT)
            self.state["admin"] = admin_identity(ROOT)
            self.copy(ROOT, self.op / "stopped-snapshot")
            self.state["snapshot"] = tree_manifest(self.op / "stopped-snapshot")
            self.phase("snapshot-verified")
            candidate = self.op / "candidate"
            self.carry(ROOT, candidate, "release-seed")
            self.prompt_refresh(candidate)
            self.state["protected_config"] = {n: tree_manifest(candidate / n) if (candidate / n).exists() else None for n in CONFIG}
            self.phase("candidate-ready")
            require(tree_manifest(ROOT) == self.state["snapshot"], "stopped_state_drift")
            self.move(ROOT, self.op / "original-deployment")
            self.phase("root-moved")
            self.move(candidate, ROOT)
            self.phase("candidate-active")
            self.start(False)
            self.verify()
            self.phase("accepted")
        except BaseException as exc:
            self.save("failure.json", {"phase": self.state.get("phase"), "type": type(exc).__name__,
                      "gate": str(exc) if isinstance(exc, GateError) else "runtime_failure"})
            try:
                self.rollback()
            except BaseException:
                raise GateError("upgrade_failed_recovery_incomplete") from None
            raise GateError("upgrade_failed_previous_runtime_recovered") from None

    def rollback(self):
        require(self.state and self.state.get("phase") not in ("prepared", "rolled-back"), "rollback_phase")
        require(self.manifest_sha == self.state["manifest_sha"], "manifest_drift")
        self.sources()
        self.image_metadata(True)
        original = self.op / "original-deployment"
        failed = self.op / "failed-candidate"
        recovery = self.op / "recovery"
        # If the process died between the two renames, restore the explicitly
        # identified tree. The independent stopped snapshot remains untouched.
        if not ROOT.exists():
            if recovery.exists() and failed.exists():
                require(tree_manifest(recovery) == self.state["recovery_manifest"], "recovery_tree_drift")
                self.move(recovery, ROOT)
                self.phase("recovery-active")
            else:
                require(original.exists() and not failed.exists(), "missing_root_recovery_ambiguous")
                self.move(original, ROOT)
                self.phase("stopped")
        if original.exists() and not failed.exists():
            require(tree_manifest(self.op / "stopped-snapshot") == self.state["snapshot"], "snapshot_evidence_drift")
            env_gate(ROOT)
            self.capacity()
            self.stop()
            self.phase("recovery-stopped")
            latest = tree_manifest(ROOT)
            latest_db = db_state(ROOT)
            compatible_databases(self.state["databases"], latest_db)
            require(tree_manifest(original) == self.state["snapshot"], "original_source_drift")
            require(not recovery.exists(), "partial_recovery_requires_review")
            self.copy(original, recovery)
            self.carry(ROOT, recovery, "recovery-seed")
            self.restore_prompt(recovery)
            require(tree_manifest(ROOT) == latest, "poststart_state_changed_during_recovery")
            self.state["databases"] = latest_db
            self.state["admin"] = admin_identity(ROOT)
            self.state["protected_config"] = {n: tree_manifest(recovery / n) if (recovery / n).exists() else None for n in CONFIG}
            self.state["recovery_manifest"] = tree_manifest(recovery)
            self.phase("recovery-ready")
            self.move(ROOT, failed)
            self.phase("candidate-retained")
            self.move(recovery, ROOT)
            self.phase("recovery-active")
        elif failed.exists():
            require(self.state["phase"] in ("recovery-active", "recovery-starting"), "recovery_phase_ambiguous")
        else:
            # Failure while stopping, snapshotting or preparing, before rename.
            require(not original.exists(), "original_tree_ambiguous")
            self.state["protected_config"] = {n: tree_manifest(ROOT / n) if (ROOT / n).exists() else None for n in CONFIG}
        self.start(True)
        self.verify(previous=True)
        self.phase("rolled-back")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "execute", "rollback", "verify"))
    parser.add_argument("operation", type=Path)
    parser.add_argument("--session-fd", type=int, default=None,
                        help="Inherited descriptor containing an existing session token; never a token argument")
    parser.add_argument("--previous", action="store_true", help="Verify recovered v8.5.1")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        require(os.geteuid() == 0, "root_required")
        require(not args.previous or args.mode == "verify", "previous_verify_only")
        operation_path(args.operation)
        session = read_session(args.session_fd)

        def interrupted(_number, _frame):
            raise GateError("interrupted")

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        with upgrade_lock(BACKUPS / ".upgrade.lock"):
            upgrade = Upgrade(args.operation, session)
            if args.mode == "verify":
                upgrade.verify(args.previous)
            else:
                getattr(upgrade, args.mode)()
        print("PASS: " + args.mode)
    except BaseException as exc:
        print("FAIL: " + (str(exc) if isinstance(exc, GateError) else "runtime_failure"))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
