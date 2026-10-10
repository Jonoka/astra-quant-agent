"""Actual local AnyIO/helper integration; no public network or production state.

Separate from the strict 156-case runner. Only loopback endpoints bound to an
ephemeral port by this scope are connectable; other local services are denied.
No Docker/WSL/real broker/scheduler/LLM/client process is started.
"""
from __future__ import annotations
import argparse
import builtins
from contextlib import ExitStack, contextmanager
import hashlib
import http.client
import importlib.abc
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
from urllib.parse import urlsplit, unquote

ROOT = Path(__file__).resolve().parents[2]
SUITES = {'test_actual_anyio.py': 4, 'test_actual_helper_listener.py': 3}
FORBIDDEN_IMPORTS = ('astra_gateway.worker', 'astra_gateway.scheduler', 'scripts.okx_rest',
                     'astra_backend.scheduler', 'astra_gateway.supervisor', 'astra_backend.okx_trade_service',
                     'astra_backend.llm_manager', 'scripts.okx_runtime', 'scripts.ai_factor_trader',
                     'scripts.ai_brain_trader', 'openai', 'anthropic')


class DenyClients(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in FORBIDDEN_IMPORTS):
            raise AssertionError('real business client/scheduler import refused: ' + fullname)


@contextmanager
def isolated(sandbox):
    sandbox = Path(sandbox).resolve()
    endpoints = set()
    endpoint_lock = threading.RLock()
    real_open, real_io, real_os_open = builtins.open, io.open, os.open
    real_connect, real_socket, real_getaddrinfo = sqlite3.connect, socket.socket, socket.getaddrinfo
    real_urlopen = urllib.request.urlopen
    real_ssl_connect, real_ssl_connect_ex = ssl.SSLSocket.connect, ssl.SSLSocket.connect_ex
    def inside(value): return Path(value).resolve().is_relative_to(sandbox)
    def guarded_file(original):
        def opened(path, mode='r', *args, **kwargs):
            if isinstance(path, int): return original(path, mode, *args, **kwargs)
            p = Path(path).resolve()
            if any(c in mode for c in 'wax+') and not inside(p): raise AssertionError('integration write outside sandbox')
            if not inside(p) and (p.name.startswith('.env') or 'credentials' in p.parts or
                    p.suffix in {'.enc', '.pem', '.key', '.db', '.sqlite'} or p.is_relative_to(ROOT / 'data')):
                raise AssertionError('integration real configuration/data read refused')
            return original(path, mode, *args, **kwargs)
        return opened
    def os_opened(path, flags, *args, **kwargs):
        mode = 'w' if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) else 'r'
        guarded_file(lambda p, m: None)(path, mode)
        return real_os_open(path, flags, *args, **kwargs)
    def connect(database, *args, **kwargs):
        raw = str(database)
        if raw != ':memory:':
            path = unquote(urlsplit(raw).path) if raw.startswith('file:') else raw
            if os.name == 'nt' and path.startswith('/') and len(path) > 2 and path[2] == ':': path = path[1:]
            if not inside(path): raise AssertionError('integration SQLite outside sandbox')
        return real_connect(database, *args, **kwargs)
    def loopback(host): return host in ('127.0.0.1', '::1', 'localhost')
    def destination(address):
        with endpoint_lock:
            if (not isinstance(address, tuple) or len(address) < 2 or not loopback(address[0])
                    or not any(port == address[1] for _, port in endpoints)):
                raise AssertionError('integration public/other-local-service socket refused')
    class LocalSocket(real_socket):
        def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0, fileno=None):
            if family not in (socket.AF_INET, socket.AF_INET6, getattr(socket, 'AF_UNIX', -1)) or type & 0xF != socket.SOCK_STREAM:
                raise AssertionError('integration raw/datagram socket refused')
            self._owned_endpoint = None
            super().__init__(family, type, proto, fileno)
        def bind(self, address):
            if not isinstance(address, tuple) or not loopback(address[0]) or address[1] != 0:
                raise AssertionError('integration bind must be synthetic ephemeral loopback')
            result = super().bind(address)
            actual = self.getsockname()
            self._owned_endpoint = (actual[0], actual[1])
            with endpoint_lock: endpoints.add(self._owned_endpoint)
            return result
        def connect(self, address): destination(address); return super().connect(address)
        def connect_ex(self, address): destination(address); return super().connect_ex(address)
        def sendto(self, *args, **kwargs): raise AssertionError('integration datagram send refused')
        def close(self):
            with endpoint_lock:
                if self._owned_endpoint is not None: endpoints.discard(self._owned_endpoint)
            return super().close()
    def getaddrinfo(host, *args, **kwargs):
        if not loopback(host): raise AssertionError('integration external DNS refused')
        return real_getaddrinfo(host, *args, **kwargs)
    def gethostbyname(host):
        if not loopback(host): raise AssertionError('integration external DNS refused')
        return '127.0.0.1' if host == 'localhost' else host
    def gethostbyaddr(host):
        if not loopback(host): raise AssertionError('integration external reverse DNS refused')
        return ('localhost', [], [host])
    def urlopen(request, *args, **kwargs):
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        target = urlsplit(url)
        if target.scheme != 'http': raise AssertionError('integration external/file/TLS URL refused')
        destination((target.hostname, target.port or 80))
        return real_urlopen(request, *args, **kwargs)
    def refused(*args, **kwargs): raise AssertionError('integration subprocess/public action refused')
    safe = {'PATH', 'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'windir', 'COMSPEC', 'TEMP', 'TMP', 'LANG', 'LC_ALL', 'PYTHONUTF8'}
    environment = {k: v for k, v in os.environ.items() if k in safe}
    environment.update(PYTHONDONTWRITEBYTECODE='1', ASTRA_DATA_DIR=str(sandbox / 'data'),
        ASTRA_ADMIN_DB=str(sandbox / 'admin.db'), ASTRA_GATEWAY_DB=str(sandbox / 'gateway.db'),
        ASTRA_QUANT_DB=str(sandbox / 'quant.db'), ASTRA_RISK_RESERVATION_DB=str(sandbox / 'risk.db'),
        ASTRA_MAINTENANCE_DB=str(sandbox / 'maintenance.sqlite'), ASTRA_LEDGER_SYNC_DISABLED='1')
    finder = DenyClients()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        stack.enter_context(patch.object(tempfile, 'tempdir', str(sandbox)))
        stack.enter_context(patch.object(builtins, 'open', guarded_file(real_open)))
        stack.enter_context(patch.object(io, 'open', guarded_file(real_io)))
        stack.enter_context(patch.object(os, 'open', os_opened))
        stack.enter_context(patch.object(sqlite3, 'connect', connect))
        stack.enter_context(patch.object(socket, 'socket', LocalSocket))
        stack.enter_context(patch.object(socket, 'getaddrinfo', getaddrinfo))
        stack.enter_context(patch.object(socket, 'gethostbyname', gethostbyname))
        stack.enter_context(patch.object(socket, 'gethostbyname_ex', lambda host: ('localhost', [], [gethostbyname(host)])))
        stack.enter_context(patch.object(socket, 'gethostbyaddr', gethostbyaddr))
        def ssl_connect(owner, address): destination(address); return real_ssl_connect(owner, address)
        def ssl_connect_ex(owner, address): destination(address); return real_ssl_connect_ex(owner, address)
        stack.enter_context(patch.object(ssl.SSLSocket, 'connect', ssl_connect))
        stack.enter_context(patch.object(ssl.SSLSocket, 'connect_ex', ssl_connect_ex))
        stack.enter_context(patch.object(urllib.request, 'urlopen', urlopen))
        stack.enter_context(patch.object(subprocess, 'Popen', refused))
        stack.enter_context(patch.object(os, 'system', refused))
        sys.meta_path.insert(0, finder)
        try: yield {'socket_policy': 'own ephemeral loopback only', 'business_import_policy': 'denied', 'sandbox': str(sandbox)}
        finally: sys.meta_path.remove(finder)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', action='append', choices=list(SUITES))
    args = parser.parse_args(argv)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / '.github/scripts'))
    pins = json.loads((ROOT / '.trellis/tasks/10-10-demo-maintenance-v1/baseline.json').read_text())['protected_sha256']
    before = {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in pins}
    if before != pins: raise RuntimeError('protected source changed')
    reports = []
    with tempfile.TemporaryDirectory(prefix='astra-actual-integration-') as temporary:
        with isolated(temporary) as policy:
            for name in args.suite or list(SUITES):
                path = ROOT / 'tests/maintenance_integration' / name
                spec = importlib.util.spec_from_file_location('_actual_integration_' + path.stem, path)
                module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
                result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(module))
                report = {'suite': name, 'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
                          'skipped': len(result.skipped), 'successful': result.wasSuccessful()}
                reports.append(report)
                if not result.wasSuccessful() or result.testsRun < SUITES[name] or result.skipped or result.expectedFailures or result.unexpectedSuccesses:
                    raise RuntimeError('actual integration failed/missing/skipped')
    after = {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in pins}
    if after != before: raise RuntimeError('actual integration changed protected bytes')
    versions = {}
    for dist in ('anyio', 'httpx', 'fastapi', 'starlette', 'uvicorn'):
        try: versions[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError: versions[dist] = None
    framework_gap = [name for name in ('fastapi', 'starlette', 'uvicorn') if versions[name] is None]
    print(json.dumps({'actual_integration': reports, 'python': sys.version.split()[0], 'framework_versions': versions,
        'isolation': {k: v for k, v in policy.items() if k != 'sandbox'},
        'full_fastapi_uvicorn_auth': 'blocked_missing_dependencies' if framework_gap else 'not_exercised_by_these_suites',
        'missing_framework_packages': framework_gap, 'linux_container_field': 'not_exercised'}, sort_keys=True))


if __name__ == '__main__': main()
