"""Hosted-only official Compose smoke with synthetic demo configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def command(args: list[str], **kwargs) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          timeout=90, **kwargs).stdout.strip()


def http(path: str, *, payload=None, token=None, expected=200):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Astra-Session"] = token
    request = Request("http://127.0.0.1:8080" + path,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers)
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as exc:
        response = exc
    with response:
        assert response.status == expected, f"{path}: HTTP {response.status}, expected {expected}"
        body = response.read()
        return json.loads(body) if "application/json" in response.headers.get("Content-Type", "") else body


def main(source: Path, image: str, previous: Path) -> None:
    assert "/app/plugins" not in (source / "docker-compose.yml").read_text(encoding="utf-8"), \
        "Release Compose must not restore the removed plugin bind"
    assert (source / "docker-compose.yml").read_bytes() == (previous / "docker-compose.yml").read_bytes(), \
        "v8.6.0-to-corrected-v8.6.1 Compose contract unexpectedly changed"
    metadata = json.loads(command(["docker", "image", "inspect", image]))[0]
    expected_image_id = os.environ.get("EXPECTED_IMAGE_ID")
    if expected_image_id:
        assert metadata["Id"] == expected_image_id, "Published image differs from the smoke-tested candidate"
    assert (metadata["Os"], metadata["Architecture"]) == ("linux", "amd64")
    labels = metadata["Config"]["Labels"]
    assert labels["org.opencontainers.image.revision"] == os.environ["SOURCE_SHA"]
    assert labels["org.opencontainers.image.version"] == os.environ["SOURCE_VERSION"]
    assert labels["org.opencontainers.image.source"] == "https://github.com/" + os.environ["SOURCE_REPOSITORY"]
    assert labels["io.jonoka.astra.upstream-revision"] == os.environ["UPSTREAM_SHA"]
    assert labels["io.jonoka.astra.council-completion-patch"] == "council-completion-v1"
    assert labels["io.jonoka.astra.build-recipe-sha256"] == os.environ["BUILD_RECIPE_SHA256"], \
        "Image build recipe differs from the reviewed derived recipe"
    with tempfile.TemporaryDirectory(prefix="astra-compose-smoke-") as tmp:
        root = Path(tmp)
        for directory in ("data", "logs", "backups"):
            (root / directory).mkdir()
        shutil.copy(source / "data/prompt_library.json", root / "data/prompt_library.json")
        shutil.copy(source / "docker-compose.yml", root / "docker-compose.yml")
        password = "A1" + secrets.token_hex(24)
        print("::add-mask::" + password)
        (root / ".env").write_text(
            "ASTRA_SETUP_TOKEN=" + password + "\nASTRA_ADMIN_TOKEN=\n"
            "ASTRA_OKX_ENV=demo\nOKX_IS_SIMULATED=1\n"
            "ASTRA_STANDALONE_GATEWAY=true\nLLM_API_KEY=\nOPENAI_API_KEY=\n"
            "ASTRA_ALT_VENUE_FALLBACK=0\nASTRA_LEDGER_SYNC_DISABLED=1\n",
            encoding="utf-8")
        (root / ".env").chmod(0o600)
        # JSON is valid YAML. The release base and both healthchecks stay intact.
        override = {"services": {service: {"image": image} for service in ("backend", "gateway")}}
        (root / "ci-override.json").write_text(json.dumps(override), encoding="utf-8")
        compose = ["docker", "compose", "-p", "astra-ci-smoke", "--project-directory", str(root),
                   "-f", str(root / "docker-compose.yml"), "-f", str(root / "ci-override.json")]

        def inspect_services():
            result = {}
            for service in ("backend", "gateway"):
                identifier = command(compose + ["ps", "-q", service])
                assert identifier, f"{service} has no running container"
                result[service] = json.loads(command(["docker", "inspect", identifier]))[0]
            return result

        def stable_probe():
            states = inspect_services()
            for service, state in states.items():
                assert state["Image"] == metadata["Id"], f"Wrong image in {service}"
                assert state["State"]["Status"] == "running"
                assert state["State"].get("Health", {}).get("Status") == "healthy"
                assert state["RestartCount"] == 0, f"{service} restarted"
                assert state["Config"]["Healthcheck"]["Test"][0] == "CMD-SHELL"
                mounts = {mount["Destination"] for mount in state["Mounts"]}
                assert mounts == {"/app/.env", "/app/data", "/app/logs", "/app/backups"}, \
                    f"Unexpected writable mount set for {service}: {sorted(mounts)}"
                assert "/app/plugins" not in mounts, "Removed plugin bind was restored"
                processes = command(["docker", "top", state["Id"], "-eo", "pid,args"])
                workers = [line for line in processes.splitlines() if "-m astra_gateway.worker" in line]
                assert len(workers) == (1 if service == "gateway" else 0), f"Wrong worker ownership in {service}"
            health = http("/api/v1/health")
            assert health["version"] == os.environ["SOURCE_VERSION"].removeprefix("v")
            assert health["status"] == "ok"
            assert health["credentials"] == {"okx_configured": False, "llm_configured": False,
                                             "simulated_trading": True}
            assert http("/api/v1/status")["version"] == health["version"]

        command(compose + ["config", "--quiet"])
        try:
            # Compose startup itself may wait for the backend watchdog's first
            # successful probe; use the full deadline for this hosted operation.
            subprocess.run(compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "240",
                                      "backend", "gateway"], check=True, timeout=300)
            stable_probe()
            assert http("/")
            assert http("/admin/login")
            assert http("/images/dashboard_preview.png") == (source / "docs/images/dashboard_preview.png").read_bytes(), \
                "Official landing preview asset missing or changed in image"
            assert http("/api/v1/admin/auth/status")["initialized"] is True
            http("/api/v1/admin/auth/me", expected=401)
            http("/api/v1/admin/auth/init", payload={"username": "secondadmin", "password": password}, expected=403)
            login = http("/api/v1/admin/auth/login", payload={"username": "admin", "password": password})
            token = login["session_token"]
            print("::add-mask::" + token)
            assert http("/api/v1/admin/auth/me", token=token)["user"]["username"] == "admin"
            http("/api/v1/admin/auth/logout", payload={}, token=token)
            http("/api/v1/admin/auth/me", token=token, expected=401)
            for _ in range(3):
                time.sleep(30)
                stable_probe()
            command(compose + ["exec", "-T", "backend", "python3", "/app/scripts/migrate_r20_to_astra.py", "--check"])
            for service in ("backend", "gateway"):
                command(compose + ["exec", "-T", service, "python3", "-c",
                                   "from pathlib import Path; p=Path('/app/data/.ci-writable'); p.write_text('ok'); p.unlink()"])
            logs = "\n".join(p.read_text(errors="replace") for p in (root / "logs").rglob("*.log"))
            assert "Traceback (most recent call last):" not in logs, "Runtime traceback observed"
            assert "gateway worker already running" not in logs, "Duplicate gateway worker observed"
            print("PASS: immutable image, official backend/gateway Compose healthchecks, auth protection, expected mounts, single worker, restart0 and 90s stability")
        finally:
            # Only synthetic CI data is inspected here; never render dotenv or config.
            if sys.exc_info()[0] is not None:
                for path in (root / "logs").rglob("*.log"):
                    print(f"Synthetic CI diagnostic: {path.name}")
                    print("\n".join(path.read_text(errors="replace").splitlines()[-60:]))
            subprocess.run(compose + ["ps"], check=False, timeout=30)
            subprocess.run(compose + ["down", "--timeout", "20"], check=False, timeout=60)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), sys.argv[2], Path(sys.argv[3]).resolve())
