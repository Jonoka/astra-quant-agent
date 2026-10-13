"""Process adapters for the explicit-path maintenance protocol.

No store, configuration, threads or credentials are loaded at import time.
Source/image environment values are claims: the deployment controller must
independently verify them against the host before accepting any acknowledgement.
"""
from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import threading
import time
import uuid

from astra_backend.maintenance import (AdmissionClosed, Binding, Identity, MaintenanceError,
                                      MaintenanceStore, MAINTENANCE_STATUS_PATH)

PROTOCOL_VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
_scope = ContextVar("maintenance_activity", default=None)
_request_intent = ContextVar("maintenance_request_intent", default="")
_runtimes = {}
_runtime_lock = threading.Lock()
_process_nonce = uuid.uuid4().hex
_legacy_process = False
_anyio_tracking_installed = False


class UnknownChild(RuntimeError):
    """Keep an unreaped child scope durable rather than asserting completion."""


class OrderNotSent(ValueError):
    """Validation or admission refused before the broker send boundary."""


class UnknownOrderReceipt(RuntimeError):
    """A send may have reached the broker; its reservation must remain held."""


class BrokerRejected(RuntimeError):
    """An actual broker response explicitly rejected the mutation."""


def store_path() -> Path:
    return Path(os.environ.get("ASTRA_MAINTENANCE_DB") or
                Path(os.environ.get("ASTRA_DATA_DIR") or ROOT / "data") / "maintenance_state.sqlite")


def enable_latch(path=None) -> Path:
    return (Path(path) if path else store_path()).with_name("maintenance_enabled")


def protocol_observed(path=None) -> bool:
    database = Path(path) if path else store_path()
    return (os.environ.get("ASTRA_MAINTENANCE_ENABLED", "").lower() in {"1", "true", "yes"}
            or enable_latch(database).exists() or database.exists()
            or database.with_name("maintenance_identity.json").exists())


def persist_enable_latch(path=None):
    latch = enable_latch(path)
    if not latch.is_absolute():
        raise AdmissionClosed("explicit absolute maintenance state path required")
    latch.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(latch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("maintenance-v1\n")
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        directory = os.open(latch.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return True


class LegacyRuntime:
    """Ordinary compatibility only before this process has ever seen protocol.

    This process cannot hot-enroll or ACK. Appearance of any enable evidence
    permanently fences it; only a fresh protocol-aware process can register.
    """
    enabled = False
    identity = None

    def __init__(self, path):
        self.path, self.seen = Path(path), False
        self.store = self

    def fenced(self):
        if protocol_observed(self.path):
            persist_enable_latch(self.path)
            self.seen = True
        return self.seen

    def poll(self): return self.fenced()
    def shutdown_requested(self): return False
    def startup(self):
        if self.fenced():
            raise AdmissionClosed("running legacy process cannot hot-enroll or acknowledge")
    def begin_startup_verification(self): return self.admit("startup-verification")
    def admit(self, kind, *, parent=None):
        self.startup()
        return uuid.uuid4().hex
    def finish(self, activity, identity=None): pass
    def finish_activity(self, activity): pass
    def complete_startup_verification(self, activity): pass
    def wait_settled(self, heartbeat=None): pass
    def register_threads(self, threads): pass
    def activity(self, *args, **kwargs):
        return Runtime.activity(self, *args, **kwargs)
    def run_admitted(self, *args, **kwargs):
        return Runtime.run_admitted(self, *args, **kwargs)


def startup_store(path) -> MaintenanceStore:
    """Latch before application imports. Missing persisted state never resets."""
    path = Path(path)
    already_latched = enable_latch(path).exists()
    observed_metadata = path.with_name("maintenance_identity.json").exists()
    new_latch = persist_enable_latch(path)
    if not path.exists() and (already_latched or observed_metadata or not new_latch):
        raise AdmissionClosed("persisted maintenance database missing; operator recovery required")
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("SELECT version FROM maintenance_meta").fetchall() != [(PROTOCOL_VERSION,)]:
                raise AdmissionClosed("persisted maintenance database identity is invalid")
    return MaintenanceStore(path)


def process_instance(pid: int | None = None) -> str:
    """Linux boot ID + PID namespace + PID/start ticks; Windows is offline only."""
    pid = os.getpid() if pid is None else pid
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        namespace = (Path("/proc") / str(pid) / "ns" / "pid").stat().st_ino
        return f"{boot}:{namespace}:{pid}:{fields[19]}"
    except (OSError, IndexError):
        if os.name != "nt" or pid != os.getpid():
            raise RuntimeError("exact process start identity unavailable")
        return f"windows-offline:{pid}:{_process_nonce}"


class Runtime:
    enabled = True
    def __init__(self, store: MaintenanceStore, identity: Identity):
        self.store, self.identity = store, identity
        self._controlled_threads = {threading.main_thread()}
        self._thread_lock = threading.Lock()
        self._producer_lock = threading.RLock()
        self._finish_lock = threading.RLock()
        self._completed_parents = set()

    def finish_activity(self, activity):
        """Release completed work only after its actual children finish.

        A cancelled HTTP waiter can finish before an abandoned AnyIO worker.
        Remember only that locally observed completion, never guess completion
        of an unknown durable activity from its age or process identity.
        """
        with self._finish_lock:
            try:
                self.store.finish(activity, self.identity)
            except MaintenanceError:
                if not any(row["parent_id"] == activity for row in self.store.status()["activities"]):
                    raise
                self._completed_parents.add(activity)
            self._flush_completed_parents()

    def _flush_completed_parents(self):
        with self._finish_lock:
            while self._completed_parents:
                activities = self.store.status()["activities"]
                parents = {row["parent_id"] for row in activities}
                ready = self._completed_parents - parents
                if not ready:
                    return
                for activity in ready:
                    self.store.finish(activity, self.identity)
                    self._completed_parents.remove(activity)

    def register_threads(self, threads):
        with self._thread_lock:
            self._controlled_threads.update(threads)

    def unknown_threads(self):
        limbo_lock = getattr(threading, "_active_limbo_lock", None)
        if limbo_lock is None:
            return [threading.current_thread()]  # Unsupported evidence cannot ACK.
        with limbo_lock, self._thread_lock:
            # enumerate includes limbo objects queued by Thread.start. No name
            # prefix can turn an arbitrary business thread into an idle owner.
            return [thread for thread in threading.enumerate()
                    if thread not in self._controlled_threads]

    def startup(self):
        return self.store.startup(self.identity)

    def begin_startup_verification(self):
        if self.fenced():
            state = self.store.status()
            bound = state.get("binding") and Binding.from_dict(state["binding"]).instances.get(self.identity.role) == self.identity
            permit = None if bound else os.environ.get("ASTRA_MAINTENANCE_STARTUP_ACTIVITY")
            return self.store.admit_verification(self.identity, parent_id=permit)
        return self.admit("startup-verification")

    def complete_startup_verification(self, activity):
        """Called by the actual successful initializer, never its failure finally."""
        row = next((item for item in self.store.status()["activities"] if item["activity_id"] == activity), None)
        if row is None:
            raise MaintenanceError("startup activity missing or completion replayed")
        parent = next((item for item in self.store.status()["activities"] if item["activity_id"] == row["parent_id"]), None)
        if row["parent_id"] and parent is None:
            raise MaintenanceError("startup parent evidence disappeared; retain unresolved activity")
        if parent is None or not parent["kind"].startswith("paused-startup:"):
            return self.finish_activity(activity)  # Ordinary/bound startup semantics.
        try:
            binding = Binding.from_dict(json.loads(os.environ["ASTRA_MAINTENANCE_STARTUP_BINDING"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AdmissionClosed("startup completion requires its original external binding") from exc
        self.store.complete_startup_verification(binding, self.identity, activity)

    def fenced(self) -> bool:
        if not self.store.path.exists():
            raise AdmissionClosed("persisted maintenance database was removed")
        return bool(self.store.status()["fenced"])

    def poll(self) -> bool:
        if not self.store.path.exists():
            raise AdmissionClosed("persisted maintenance database was removed")
        status = self.store.status()
        limbo_lock = getattr(threading, "_active_limbo_lock", None)
        if status["fenced"] and status.get("binding"):
            # Hold local creation boundaries through observation and ACK commit,
            # but take the observation only inside the SQLite barrier transaction.
            # This revokes an earlier ACK on failed/unknown evidence, including a
            # missing limbo lock. It does not fence arbitrary births between polls.
            with self._producer_lock, (limbo_lock if limbo_lock is not None else nullcontext()):
                def quiescent():
                    if limbo_lock is None or self.unknown_threads():
                        return False
                    evidence = unsupported_producers()
                    return evidence["verified"] is True and not any(evidence["counts"].values())
                try:
                    self.store.reconcile_acknowledgement(
                        Binding.from_dict(status["binding"]), self.identity, quiescent)
                except AdmissionClosed:
                    pass
                except ValueError:
                    pass  # Stale or unapproved instances cannot manufacture ACK.
        return bool(status["fenced"])

    def admit(self, kind: str, *, parent=None):
        if not self.store.path.exists():
            raise AdmissionClosed("persisted maintenance database was removed")
        return self.store.admit(self.identity, kind, parent_id=parent)

    def shutdown_requested(self):
        status = self.store.status()
        binding = status.get("binding")
        return bool(status.get("shutdown_requested") and binding and
                    Binding.from_dict(binding).instances.get(self.identity.role) == self.identity)

    @contextmanager
    def activity(self, kind: str, *, child: bool = False):
        boundary = self._producer_lock if self.enabled and kind == "qq-daemon-launch" else nullcontext()
        with boundary, Runtime._activity(self, kind, child=child) as activity:
            yield activity

    @contextmanager
    def _activity(self, kind: str, *, child: bool = False):
        current = _scope.get()
        parent = current[1] if child and current and current[0] is self else None
        if child and current is None:
            parent = os.environ.get("ASTRA_MAINTENANCE_PARENT_ACTIVITY") or None
        activity = self.admit(kind, parent=parent)
        token = _scope.set((self, activity))
        settled = True
        try:
            yield activity
        except UnknownChild:
            settled = False
            raise
        finally:
            _scope.reset(token)
            if settled:
                self.finish_activity(activity)

    def run_admitted(self, activity, function, *args, **kwargs):
        token = _scope.set((self, activity))
        try:
            return function(*args, **kwargs)
        finally:
            _scope.reset(token)
            self.finish_activity(activity)

    def wait_settled(self, heartbeat=None):
        """Unknown child scopes cannot be replaced by a new singleton owner."""
        while True:
            try:
                self._flush_completed_parents()
                active = bool(self.unknown_threads()) or any(json.loads(activity["owner"]) == self.identity.as_dict()
                             for activity in self.store.status()["activities"])
            except Exception:
                active = True  # Loss of accounting is not proof of child exit.
            if not active:
                return
            if heartbeat is not None:
                heartbeat()
            time.sleep(.2)


def get_runtime(role: str | None = None) -> Runtime:
    global _legacy_process
    current = _scope.get()
    if current is not None and role is None:
        return current[0]
    role = role or os.environ.get("ASTRA_MAINTENANCE_ROLE", "backend")
    key = (str(store_path()), role, os.getpid())
    with _runtime_lock:
        if key not in _runtimes:
            if (_legacy_process or os.environ.get("ASTRA_MAINTENANCE_LEGACY_PROCESS") == "1"
                    or not protocol_observed()):
                _legacy_process = True
                # Also spans the __main__/canonical module boundary and child
                # environment snapshots; another module cannot hot-enroll us.
                os.environ["ASTRA_MAINTENANCE_LEGACY_PROCESS"] = "1"
                _runtimes[key] = LegacyRuntime(store_path())
                return _runtimes[key]
            # Descendants inherit the admitted owner's identity, never adopt a
            # new PID as a fabricated component acknowledgement.
            store = startup_store(store_path())
            inherited = os.environ.get("ASTRA_MAINTENANCE_OWNER")
            identity = Identity.from_dict(json.loads(inherited)) if inherited else Identity(
                role, process_instance(), os.environ.get("ASTRA_SOURCE_COMMIT", ""),
                os.environ.get("ASTRA_IMAGE_REF", ""), PROTOCOL_VERSION)
            _runtimes[key] = Runtime(store, identity)
        return _runtimes[key]


def guarded(kind: str, *, child=False, blocked=None):
    """Synchronous work source, including all its final writers."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            runtime = get_runtime()
            try:
                with runtime.activity(kind, child=child):
                    return function(*args, **kwargs)
            except AdmissionClosed:
                return blocked
        return wrapped
    return decorate


def install_anyio_thread_tracking():
    """Register actual AnyIO worker objects executing admitted ASGI work.

    The HTTP activity exists before its AnyIO queue submission. This adapter
    records only those workers, leaving arbitrary background/limbo threads
    unknown. It neither patches child writers nor grants a new admission.
    """
    global _anyio_tracking_installed
    if _anyio_tracking_installed:
        return
    import anyio.to_thread
    original = anyio.to_thread.run_sync
    @wraps(original)
    async def tracked(function, *args, **kwargs):
        current = _scope.get()
        if current is None or not current[0].enabled:
            return await original(function, *args, **kwargs)
        runtime, parent = current
        if parent is None:
            # Only the reviewed pure health/docs ASGI paths carry this context.
            # Register their actual pool worker, not an arbitrary thread name.
            def readonly(*values):
                runtime.register_threads([threading.current_thread()])
                return function(*values)
            return await original(readonly, *args, **kwargs)
        parent_row = next(row for row in runtime.store.status()["activities"]
                          if row["activity_id"] == parent)
        if parent_row["kind"] == "startup-verification":
            activity = runtime.store.admit_verification(runtime.identity, parent_id=parent)
        else:
            activity = runtime.admit("asgi-thread", parent=parent)
        @wraps(function)
        def execute(*values):
            runtime.register_threads([threading.current_thread()])
            return runtime.run_admitted(activity, function, *values)
        # The worker owns release. If queue cancellation cannot prove that the
        # callback will never execute, its persisted scope remains unresolved.
        return await original(execute, *args, **kwargs)
    anyio.to_thread.run_sync = tracked
    _anyio_tracking_installed = True


class MaintenanceMiddleware:
    """Account for the complete ASGI response and its background writers."""
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope.get("path", ""), scope.get("method", "GET")
        if method == "GET" and path == MAINTENANCE_STATUS_PATH:
            # This is the production ASGI route, not a bypass to broad app GETs.
            # No admission, polling, lazy app import, auth/config or expiry write.
            try:
                from astra_backend.version import get_version
                if self.runtime.enabled:
                    summary = self.runtime.store.readonly_summary(self.runtime.identity)
                elif protocol_observed(self.runtime.path):
                    raise AdmissionClosed("legacy instance cannot report protocol readiness")
                else:
                    summary = {"protocol": 0, "phase": "LEGACY", "fenced": False}
                body = json.dumps({"version": get_version(), "status": "ok", "maintenance": summary},
                                  sort_keys=True).encode()
                status = 200
            except (ValueError, OSError, sqlite3.Error):
                body, status = b'{"detail":"maintenance state unavailable"}', 503
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        activity = None
        if path not in {"/api/v1/health", "/api/docs", "/api/redoc", "/openapi.json"}:
            try:
                activity = self.runtime.admit("http:" + method)
            except AdmissionClosed:
                if method == "GET" and path in {"/api/v1/admin/auth/me", "/api/v1/admin/auth/status"} and self.runtime.enabled:
                    try:
                        activity = self.runtime.store.admit_verification(self.runtime.identity)
                    except ValueError:
                        pass
                if activity is None:
                    body = b'{"detail":"maintenance admission closed"}'
                    await send({"type": "http.response.start", "status": 503,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(body)).encode())]})
                    await send({"type": "http.response.body", "body": body})
                    return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        token = _scope.set((self.runtime, activity))
        intent = _request_intent.set(headers.get(b"x-astra-request-id", b"").decode("latin1"))
        try:
            return await self.app(scope, receive, send)
        finally:
            _request_intent.reset(intent)
            _scope.reset(token)
            if activity is not None:
                self.runtime.finish_activity(activity)
            self.runtime.poll()


def unsupported_producers():
    """Counts only. Unowned resident QQ writers prevent a maintenance ACK."""
    proc = Path("/proc")
    patterns = {"qq_gateway_daemon": b"astra_backend.qq_gateway_daemon",
                "daemon_web_sync": b"daemon_web_sync.py", "market_stream": b"scripts.market_stream",
                "legacy_scheduler": b"astra_backend.scheduler"}
    counts = {name: 0 for name in patterns}
    if not proc.is_dir():
        return {"verified": False, "counts": counts}
    verified = True
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
            for name, pattern in patterns.items():
                if (pattern in command or (name == "market_stream" and b"market_stream.py" in command)
                        or (name == "legacy_scheduler" and b"/astra_backend/scheduler.py" in command)):
                    counts[name] += 1
        except FileNotFoundError:
            pass  # Process exited while taking the read-only inventory.
        except OSError:
            verified = False
    return {"verified": verified, "counts": counts}


def start_paused_component(runtime, *, logfile=None, sleep=time.sleep):
    """Launch a controller-approved new app with every business admission shut.

    The durable launch scope remains on crashes, deadline/identity loss or an
    unverified child. There are no signals, retries or fabricated role ACKs.
    Windows process identity is explicitly offline; the host controller still
    requires independent Linux namespace/start/cgroup evidence before acceptance.
    """
    role = {"watchdog-backend": "backend", "watchdog-gateway": "gateway"}.get(runtime.identity.role)
    if role is None or not runtime.enabled:
        raise AdmissionClosed("paused startup requires a protocol watchdog")
    try:
        binding = Binding.from_dict(json.loads(os.environ["ASTRA_MAINTENANCE_STARTUP_BINDING"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise AdmissionClosed("externally pinned startup binding required") from exc
    activity = runtime.store.begin_paused_startup(binding, runtime.identity)
    environment = dict(os.environ)
    environment.pop("ASTRA_MAINTENANCE_OWNER", None)
    environment.pop("ASTRA_MAINTENANCE_PARENT_ACTIVITY", None)
    environment.update(ASTRA_MAINTENANCE_ROLE=role, ASTRA_MAINTENANCE_STARTUP_ACTIVITY=activity,
                       ASTRA_MAINTENANCE_DB=str(runtime.store.path))
    command = ([sys.executable, "-m", "astra_gateway.worker"] if role == "gateway" else
               [sys.executable, "-m", "astra_backend.maintenance_runtime", "uvicorn",
                "astra_backend.app:app", "--host", "0.0.0.0", "--port", "8080"])
    logfile = Path(logfile) if logfile is not None else ROOT / "logs" / ("astra_" + role + ".log")
    try:
        with logfile.open("ab") as output:
            process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        while True:
            state = runtime.store.status()
            if state["binding"] != binding.as_dict() or runtime.store.clock() >= state["deadline"] or process.poll() is not None:
                raise UnknownChild("paused child startup could not be verified")
            candidates = [Identity.from_dict(json.loads(row["identity"])) for row in state["actors"]
                          if row["online"] and json.loads(row["identity"])["role"] == role]
            if len(candidates) == 1:
                child = candidates[0]
                exact = (child.instance_id.startswith(f"windows-offline:{process.pid}:") if os.name == "nt"
                         else child.instance_id == process_instance(process.pid))
                if not exact or (child.source, child.image) != (runtime.identity.source, runtime.identity.image):
                    raise UnknownChild("paused child identity differs from the spawned process")
                if runtime.store.consume_startup_completion(binding, runtime.identity, activity, child):
                    return process.pid
            sleep(.05)
    except BaseException:
        runtime.store.hold("paused startup unresolved; explicit recovery required")
        raise


def start_thread(kind, function, *args, name=None, daemon=True):
    """Count a background source before Thread.start, through final function exit."""
    runtime = get_runtime()
    activity = runtime.admit(kind)
    thread = threading.Thread(target=runtime.run_admitted, args=(activity, function, *args),
                              name=name, daemon=daemon)
    runtime.register_threads([thread])
    try:
        thread.start()
    except BaseException:
        runtime.store.finish(activity, runtime.identity)
        raise
    return thread


def run_process(command, *, timeout=None, env=None, runtime=None, **kwargs):
    """Keep the admission until the child is reaped, even after a fenced timeout.

    An ordinary timeout still kills/reaps the child, but the kill is atomically
    admitted as an action that prevents maintenance from starting. When that
    admission is closed, cancel the result and wait without TERM/KILL.
    """
    runtime = runtime or get_runtime()
    if not runtime.enabled:
        runtime.startup()
        return subprocess.run(command, timeout=timeout, env=env, **kwargs)
    with runtime.activity("subprocess", child=True) as activity:
        child_env = dict(os.environ if env is None else env)
        child_env.update(ASTRA_MAINTENANCE_OWNER=json.dumps(runtime.identity.as_dict()),
                         ASTRA_MAINTENANCE_PARENT_ACTIVITY=activity,
                         ASTRA_MAINTENANCE_DB=str(runtime.store.path))
        capture = kwargs.pop("capture_output", False)
        check = kwargs.pop("check", False)
        if capture:
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process = subprocess.Popen(command, env=child_env, **kwargs)
        deadline = None if timeout is None else time.monotonic() + timeout
        expired = False
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=.2)
                    if deadline is not None and not expired and time.monotonic() >= deadline:
                        expired = True
                    break
                except subprocess.TimeoutExpired:
                    if deadline is None or time.monotonic() < deadline or expired:
                        continue
                    expired = True
                    try:
                        with runtime.activity("normal-deadline-termination"):
                            process.kill()
                            stdout, stderr = process.communicate()
                            break
                    except AdmissionClosed:
                        continue
        except BaseException:
            # Interrupt/disconnect never releases an unreaped child admission.
            # Reap without a kill; if reaping itself fails leave the scope unknown.
            try:
                process.communicate()
            except BaseException:
                runtime.store.hold("child drain could not be verified")
                raise UnknownChild("child exit has not been established")
            raise
        if expired:
            raise subprocess.TimeoutExpired(command, timeout, stdout, stderr)
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def order_send(logical_intent, request_payload, send, *, runtime=None):
    """Durable sending-before-network journal. Unknown/partial never permit retry."""
    runtime = runtime or get_runtime()
    canonical = json.dumps(request_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    try:
        journal = runtime.store.prepare_order(runtime.identity, logical_intent,
                                             hashlib.sha256(canonical.encode()).hexdigest())
    except AdmissionClosed:
        raise
    except ValueError as exc:
        try:
            uncertain = any(order["logical_intent"] == logical_intent
                            and order["status"] in {"PREPARED", "UNKNOWN", "PARTIAL"}
                            for order in runtime.store.status()["orders"])
        except Exception:
            uncertain = True
        if uncertain:
            raise UnknownOrderReceipt("existing uncertain logical request must be reconciled") from exc
        raise OrderNotSent(str(exc)) from exc
    if not journal["send_allowed"]:
        raise RuntimeError("logical order already journaled; explicit reconciliation required")
    try:
        rows = send(journal["client_order_id"])
    except BrokerRejected:
        runtime.store.record_order(runtime.identity, logical_intent, "REJECTED")
        raise
    except BaseException as exc:
        try:
            runtime.store.record_order(runtime.identity, logical_intent, "UNKNOWN")
        finally:
            if isinstance(exc, Exception):
                raise UnknownOrderReceipt(str(exc)) from exc
            raise
    order_id = next((str(row.get("ordId") or row.get("algoId") or "") for row in rows
                     if isinstance(row, dict) and (row.get("ordId") or row.get("algoId"))), "")
    partial = any(str(row.get("state", "")).lower() == "partially_filled" for row in rows if isinstance(row, dict))
    outcome = "PARTIAL" if partial else ("ACKNOWLEDGED" if order_id else "UNKNOWN")
    try:
        runtime.store.record_order(runtime.identity, logical_intent, outcome, broker_order_id=order_id)
    except Exception as exc:
        raise UnknownOrderReceipt("sent order receipt could not be persisted") from exc
    if not order_id:
        raise UnknownOrderReceipt("unknown order receipt; reservation retained")
    return rows


def serve_backend():
    """Uvicorn consumes same-binding STOPPING through its graceful exit flag."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("server", choices=["uvicorn"])
    parser.add_argument("application", choices=["astra_backend.app:app"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    run_backend(args.application, args.host, args.port)


def run_backend(application, host="0.0.0.0", port=8080):
    runtime = get_runtime("backend")
    runtime.startup()
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(application, host=host, port=port))
    stopped = threading.Event()
    def monitor():
        while not stopped.wait(.2):
            try:
                if runtime.shutdown_requested():
                    server.should_exit = True
                    return
                runtime.poll()
            except Exception:
                # State/identity loss keeps admissions shut and cannot be used
                # as permission to terminate another process or resume trading.
                continue
    watcher = threading.Thread(target=monitor, name="maintenance-control", daemon=True)
    runtime.register_threads([watcher])
    watcher.start()
    try:
        server.run()
    finally:
        stopped.set()
        watcher.join()


if __name__ == "__main__":
    # Share the canonical runtime/registry with app imports, not __main__ state.
    from astra_backend.maintenance_runtime import serve_backend as canonical_serve
    canonical_serve()
