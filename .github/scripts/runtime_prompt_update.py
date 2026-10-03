"""Narrow, runtime-only v8.5.1 prompt/config cutover. Outputs contain no secrets."""
import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

ROOT = Path('/opt/r20-quantum-trader')
BACKUPS = Path('/opt/r20-quantum-trader-backups')
SOURCE = 'a913957689a920c6e0eba835d1570d7d83e7f4a9'
IMAGE = 'ghcr.io/jonoka/astra-quant-agent@sha256:878f03e298a37615903c9fc629c8e12c5a9b65f238c9dd269dd2a85c745c197c'
PROMPT_SHA = '29cb6cdb796c4aea4625c68cdc63680133ffdb29f9ed48bdc3944569c98d2cf1'
OLD_SHA = '5b9d72cf1e063943e89f65f86bd0d01477d0b8c35dd72aabffb468e7cdbf2783'
CHANGES = {
    'ASTRA_MIN_ENTRY_CONFIDENCE': ('80', '75'),
    'ASTRA_MIN_RISK_REWARD': ('2.0', '1.6'),
    'ASTRA_TIME_STOP_HOURS': ('8', '4'),
    'ASTRA_STOP_COOLDOWN_MINUTES': ('30', '15'),
    'ASTRA_SCALE_OUT_TRIGGER_ATR': ('1.2', '2.2'),
    'ASTRA_SCALE_OUT_RATIO': ('0.50', '0.40'),
}
FILES = ('.env', 'data/prompt_library.json')
GUARDS = ('docker-compose.yml', 'docker-compose.override.yml',
          'scripts/prompt_library.py', 'scripts/risk_constants.py')
SERVICES = ('backend', 'gateway')


class GateError(RuntimeError):
    pass


def require(ok, gate):
    if not ok:
        raise GateError(gate)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def plain(path):
    require(path.is_absolute() and path.resolve() == path and
            all(not p.is_symlink() for p in (path, *path.parents)), 'plain_path')
    return path


def metadata(path):
    plain(path)
    s = path.stat()
    require(stat.S_ISREG(s.st_mode), 'regular_file')
    return dict(sha=sha(path.read_bytes()), uid=s.st_uid, gid=s.st_gid,
                mode=stat.S_IMODE(s.st_mode))


def atomic(path, data, meta):
    plain(path)
    fd, temporary = tempfile.mkstemp(prefix='.prompt-update-', dir=path.parent)
    try:
        os.fchmod(fd, meta['mode'])
        os.fchown(fd, meta['uid'], meta['gid'])
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def patch_env(data):
    """Only replace six numeric value spans; preserve comments/whitespace/newlines."""
    for key, (before, after) in CHANGES.items():
        pattern = rb'(?m)^[ \t]*(?:export[ \t]+)?' + key.encode() + rb'[ \t]*=[ \t]*([^\r\n]*)'
        matches = list(re.finditer(pattern, data))
        require(len(matches) == 1, 'env_key_count')
        m = matches[0]
        value = re.fullmatch(rb'([ \t]*)([0-9]+(?:\.[0-9]+)?)([ \t]*(?:#[^\r\n]*)?)', m[1])
        require(value is not None and float(value[2]) == float(before), 'env_before_value')
        start, end = m.start(1) + value.start(2), m.start(1) + value.end(2)
        data = data[:start] + after.encode() + data[end:]
    return data


def validate_prompt(data):
    require(sha(data) == PROMPT_SHA, 'official_prompt_sha')
    value = json.loads(data)
    require(value['active_profile_id'] == 'allpattern_swing' and
            set(value['profiles']) == {'allpattern_swing'}, 'official_profile')


@contextmanager
def lock(path):
    import fcntl
    plain(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), 'lock_regular')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GateError('lock_busy') from None
        yield
    finally:
        os.close(fd)


def run(*args, timeout=300):
    try:
        return subprocess.run(list(map(str, args)), capture_output=True,
                              check=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        raise GateError('command_failed') from None


class Update:
    def __init__(self, operation):
        self.op = plain(operation)
        require(self.op.parent == BACKUPS and re.fullmatch(r'prompts-v8\.5\.1-[\w-]+', self.op.name), 'operation_path')

    def save(self, value):
        atomic(self.op / 'manifest.json', json.dumps(value, sort_keys=True).encode(),
               dict(mode=0o600, uid=0, gid=0))

    def load(self):
        require(self.op.stat().st_uid == 0 and stat.S_IMODE(self.op.stat().st_mode) == 0o700, 'operation_permissions')
        info = metadata(self.op / 'manifest.json')
        require(info['mode'] == 0o600 and info['uid'] == 0 and info['gid'] == 0, 'manifest_permissions')
        return json.loads((self.op / 'manifest.json').read_bytes())

    def compose(self, *args):
        return run('docker', 'compose', '-p', 'r20-quantum-trader', '--project-directory', ROOT,
                   '--env-file', ROOT / '.env', '-f', ROOT / 'docker-compose.yml',
                   '-f', ROOT / 'docker-compose.override.yml', *args)

    def guards(self):
        require(not (ROOT / 'data/prompt_library.local.json').exists() and
                not (ROOT / 'data/prompt_library.local.json').is_symlink(), 'overlay_absent')
        return {p: metadata(ROOT / p) for p in GUARDS}

    def runtime(self, healthy=True):
        image = json.loads(run('docker', 'image', 'inspect', IMAGE))[0]
        require(IMAGE in image.get('RepoDigests', []) and image['Os'] == 'linux'
                and image['Architecture'] == 'amd64', 'runtime_image_pin_platform')
        require(image['Config']['Labels'].get('org.opencontainers.image.revision') == SOURCE
                and image['Config']['Labels'].get('org.opencontainers.image.version') == 'v8.5.1',
                'runtime_image_source_version')
        items = json.loads(run('docker', 'inspect', *(f'astraquant-{s}' for s in SERVICES)))
        for c, service in zip(items, SERVICES):
            require(c['Image'] == image['Id'] and c['Config']['Image'] == IMAGE, 'runtime_image')
            labels = c['Config']['Labels']
            require(labels.get('com.docker.compose.project') == 'r20-quantum-trader' and
                    labels.get('com.docker.compose.service') == service and
                    labels.get('org.opencontainers.image.revision') == SOURCE, 'runtime_source_project')
            if healthy:
                require(c['State']['Status'] == 'running' and
                        c['State'].get('Health', {}).get('Status') == 'healthy' and
                        c['RestartCount'] == 0, 'runtime_health')

    def prepare(self, prompt):
        plain(ROOT); plain(BACKUPS)
        require(BACKUPS.stat().st_uid == 0 and not BACKUPS.stat().st_mode & 0o022, 'backup_parent')
        require(not self.op.exists(), 'operation_exists')
        self.runtime(); self.compose('config', '--quiet')
        guards = self.guards()
        candidate = plain(prompt).read_bytes(); validate_prompt(candidate)
        original = {p: metadata(ROOT / p) for p in FILES}
        require(original[FILES[0]]['mode'] == 0o600 and original[FILES[0]]['uid'] == 0, 'env_permissions')
        require(original[FILES[1]]['sha'] == OLD_SHA, 'old_official_prompt')
        env = patch_env((ROOT / '.env').read_bytes())
        self.op.mkdir(mode=0o700)
        for i, p in enumerate(FILES):
            atomic(self.op / f'original-{i}', (ROOT / p).read_bytes(), dict(mode=0o600, uid=0, gid=0))
            require(sha((self.op / f'original-{i}').read_bytes()) == original[p]['sha'], 'backup_verified')
        for i, data in enumerate((env, candidate)):
            atomic(self.op / f'installed-{i}', data, dict(mode=0o600, uid=0, gid=0))
        self.save(dict(original=original, guards=guards,
                       installed={p: sha(data) for p, data in zip(FILES, (env, candidate))}, phase='prepared'))
        self.drift(self.load(), False)

    def drift(self, m, recovery):
        require(self.guards() == m['guards'], 'guard_drift')
        for i, p in enumerate(FILES):
            original = m['original'][p]
            current = metadata(ROOT / p)
            allowed = {original['sha'], m['installed'][p]} if recovery else {original['sha']}
            require(current['sha'] in allowed and all(current[k] == original[k] for k in ('uid', 'gid', 'mode')), 'config_drift')
            for kind, digest in (('original', original['sha']), ('installed', m['installed'][p])):
                info = metadata(self.op / f'{kind}-{i}')
                require(info['mode'] == 0o600 and info['uid'] == 0 and info['gid'] == 0
                        and info['sha'] == digest, f'{kind}_drift')
        validate_prompt((self.op / 'installed-1').read_bytes())

    def start(self):
        self.compose('config', '--quiet')
        self.compose('up', '-d', '--no-build', '--no-deps', '--force-recreate',
                     '--wait', '--wait-timeout', '180', *SERVICES)
        self.runtime()

    def stop(self):
        self.compose('stop', '-t', '30', *SERVICES)
        self.runtime(False)
        items = json.loads(run('docker', 'inspect', *(f'astraquant-{s}' for s in SERVICES)))
        require(all(c['State']['Status'] == 'exited' for c in items), 'stopped')

    def smoke(self, updated):
        expected = {k: float(v[1 if updated else 0]) for k, v in CHANGES.items()}
        code = """import json, os
from scripts import prompt_library as p
from astra_backend.risk_config import normalize, process_values
profile = p.active_profile()
assert p.validate_profile(profile)['valid']
expected = json.loads(__import__('sys').argv[1])
assert all(float(os.environ[k]) == v for k,v in expected.items())
if __import__('sys').argv[2] == 'allpattern_swing':
    assert set(normalize(expected)) == set(expected)
assert all(float(process_values()[k]) == v for k,v in expected.items())
assert p.load_library()['active_profile_id'] == __import__('sys').argv[2]
assert profile['trading_system'] and profile['trading_user']
print('reader_config_ok')
"""
        for service in SERVICES:
            run('docker', 'exec', f'astraquant-{service}', 'python3', '-c', code,
                json.dumps(expected), 'allpattern_swing' if updated else 'stable')

    def execute(self, rollback=False):
        m = self.load()
        with ExitStack() as stack:
            for path in (BACKUPS / '.upgrade.lock', ROOT / '..env.lock',
                         ROOT / 'data/.prompt_library.local.json.lock'):
                stack.enter_context(lock(path))
            require(m['phase'] in (('applying', 'applied', 'rollback_failed') if rollback else ('prepared',)), 'operation_phase')
            self.runtime(not rollback); self.drift(m, rollback)
            self.compose('config', '--quiet')
            try:
                self.stop()
                self.drift(m, rollback)
                m['phase'] = 'applying' if not rollback else 'rollback_failed'; self.save(m)
                for i, p in enumerate(FILES):
                    data = (self.op / f'{"original" if rollback else "installed"}-{i}').read_bytes()
                    atomic(ROOT / p, data, m['original'][p])
                self.start(); self.smoke(not rollback)
                m['phase'] = 'rolled_back' if rollback else 'applied'; self.save(m)
            except BaseException:
                # Keep stopped if drift was observed; never overwrite new edits.
                # Otherwise restore exact configuration, retaining all trading data.
                self.drift(m, True)
                self.stop()
                self.drift(m, True)
                for i, p in enumerate(FILES):
                    atomic(ROOT / p, (self.op / f'original-{i}').read_bytes(), m['original'][p])
                self.start(); self.smoke(False)
                m['phase'] = 'rolled_back'; self.save(m)
                raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('operation', type=Path)
    parser.add_argument('--prompt', type=Path)
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, 'root_required')
        update = Update(args.operation)
        if args.command == 'prepare':
            require(args.prompt is not None, 'prompt_required')
            with ExitStack() as stack:
                for path in (BACKUPS / '.upgrade.lock', ROOT / '..env.lock', ROOT / 'data/.prompt_library.local.json.lock'):
                    stack.enter_context(lock(path))
                update.prepare(args.prompt)
        else:
            update.execute(args.command == 'rollback')
        print(json.dumps({'status': 'ok', 'phase': update.load()['phase'], 'prompt_sha': PROMPT_SHA}))
    except BaseException as error:
        print(json.dumps({'status': 'failed', 'gate': str(error) if isinstance(error, GateError) else 'unexpected_failure'}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
