"""Focused offline acceptance: temp data, no real dotenv/credentials or network.

Do not discover the repository's tests package: its initializer loads dotenv.
Only the reviewed SQLite concurrency child is allowed to create a real process.
Linux flock/container acceptance is a distinct, never inferred requirement.
"""
from __future__ import annotations

import argparse
import ast
import builtins
from contextlib import ExitStack, contextmanager
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SUITES = {
    'test_protocol.py': 33,
    'test_journal.py': 10,
    'test_concurrency.py': 5,
    'test_runtime.py': 30,
    'test_entrypoints.py': 16,
    'test_deployment.py': 31,
}


def load_file(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def require_complete(result, minimum):
    if (not result.wasSuccessful() or result.testsRun < minimum or result.skipped
            or result.expectedFailures or result.unexpectedSuccesses):
        raise RuntimeError('maintenance suite failed, incomplete or skipped')


@contextmanager
def isolated(sandbox):
    """Install guards before application imports, preserving only safe env keys."""
    sandbox = Path(sandbox).resolve()
    real_open, real_io, real_connect = builtins.open, io.open, sqlite3.connect
    real_popen = subprocess.Popen
    child_source = ROOT / 'tests/maintenance/test_concurrency.py'
    assignments = [n for n in ast.parse(child_source.read_text()).body
                   if isinstance(n, ast.Assign) and any(
                       isinstance(t, ast.Name) and t.id == 'CHILD' for t in n.targets)]
    approved_child = ast.literal_eval(assignments[0].value)

    def inside(path):
        return Path(path).resolve().is_relative_to(sandbox)

    def guarded_file(original):
        def open_file(path, mode='r', *args, **kwargs):
            if isinstance(path, int):
                return original(path, mode, *args, **kwargs)
            p = Path(path).resolve()
            if any(flag in mode for flag in 'wax+') and not inside(p):
                raise AssertionError('offline write outside temporary sandbox')
            if not inside(p) and (p.name.startswith('.env') or 'credentials' in p.parts
                                 or p.suffix in {'.enc', '.pem', '.key', '.db', '.sqlite'}
                                 or p.is_relative_to(ROOT / 'data')):
                raise AssertionError('offline real configuration/data read refused')
            return original(path, mode, *args, **kwargs)
        return open_file

    def connect(database, *args, **kwargs):
        raw = str(database)
        if raw != ':memory:':
            from urllib.parse import unquote, urlsplit
            path = unquote(urlsplit(raw).path) if raw.startswith('file:') else raw
            if os.name == 'nt' and path.startswith('/') and len(path) > 2 and path[2] == ':':
                path = path[1:]
            if not inside(path):
                raise AssertionError('offline SQLite outside temporary sandbox')
        return real_connect(database, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError('offline network refused')

    def popen(command, *args, **kwargs):
        if (not isinstance(command, (list, tuple)) or len(command) != 8
                or Path(command[0]).resolve() != Path(sys.executable).resolve()
                or command[1:3] != ['-c', approved_child]
                or Path(command[3]).resolve() != ROOT / 'astra_backend/maintenance.py'
                or not inside(command[4])
                or command[7] not in {'admit', 'request', 'order', 'order-crash', 'crash'}):
            raise AssertionError('offline unapproved subprocess refused')
        child_env = {key: value for key, value in os.environ.items() if key in safe_keys}
        child_env['PYTHONDONTWRITEBYTECODE'] = '1'
        kwargs['env'] = child_env
        return real_popen(command, *args, **kwargs)

    safe_keys = {'PATH', 'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'windir', 'COMSPEC',
                 'TEMP', 'TMP', 'LANG', 'LC_ALL', 'PYTHONUTF8', 'PYTHONPATH'}
    env = {k: v for k, v in os.environ.items() if k in safe_keys}
    env.update(PYTHONDONTWRITEBYTECODE='1', ASTRA_DATA_DIR=str(sandbox / 'data'),
               ASTRA_GATEWAY_DB=str(sandbox / 'gateway.db'),
               ASTRA_ADMIN_DB=str(sandbox / 'admin.db'),
               ASTRA_QUANT_DB=str(sandbox / 'quant.db'),
               ASTRA_RISK_RESERVATION_DB=str(sandbox / 'risk.db'),
               ASTRA_MAINTENANCE_DB=str(sandbox / 'maintenance.sqlite'),
               ASTRA_LEDGER_SYNC_DISABLED='1')
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch.object(tempfile, 'tempdir', str(sandbox)))
        stack.enter_context(patch.object(builtins, 'open', guarded_file(real_open)))
        stack.enter_context(patch.object(io, 'open', guarded_file(real_io)))
        stack.enter_context(patch.object(sqlite3, 'connect', connect))
        stack.enter_context(patch.object(subprocess, 'Popen', popen))
        for owner, name in [(socket, 'socket'), (socket, 'create_connection'),
                            (urllib.request, 'urlopen'),
                            (http.client.HTTPConnection, 'connect'),
                            (http.client.HTTPSConnection, 'connect')]:
            stack.enter_context(patch.object(owner, name, no_network))
        yield


def protected_hashes():
    baseline = ROOT / '.trellis/tasks/10-10-demo-maintenance-v1/baseline.json'
    if not baseline.is_file():
        return {}
    pins = json.loads(baseline.read_text())['protected_sha256']
    actual = {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in pins}
    if actual != pins:
        raise RuntimeError('protected PR5/policy/source bytes changed')
    return actual


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=list(SUITES), action='append')
    parser.add_argument('--require-linux', action='store_true')
    args = parser.parse_args(argv)
    if args.require_linux and not sys.platform.startswith('linux'):
        raise RuntimeError('Linux maintenance acceptance is unavailable on this host')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    before = protected_hashes()
    reports = []
    with tempfile.TemporaryDirectory(prefix='astra-maintenance-offline-') as temporary:
        with isolated(temporary):
            for name in args.suite or list(SUITES):
                path = ROOT / 'tests/maintenance' / name
                if not path.is_file():
                    raise RuntimeError('mandatory maintenance suite missing: ' + name)
                module = load_file(path, '_maintenance_acceptance_' + path.stem)
                suite = unittest.defaultTestLoader.loadTestsFromModule(module)
                result = unittest.TextTestRunner(verbosity=2).run(suite)
                reports.append({'suite': name, 'tests': result.testsRun,
                                'failures': len(result.failures), 'errors': len(result.errors),
                                'skipped': len(result.skipped), 'successful': result.wasSuccessful()})
                require_complete(result, SUITES[name])
    if protected_hashes() != before:
        raise RuntimeError('offline suite changed protected bytes')
    print(json.dumps({'offline_maintenance': reports,
                      'linux_flock': 'not_proven_by_this_runner',
                      'container_or_production': 'not_exercised'}, sort_keys=True))


if __name__ == '__main__':
    main()
