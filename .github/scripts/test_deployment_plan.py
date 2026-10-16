"""Linux synthetic plan, native-pool and pre-stop/recovery safety acceptance."""
import copy
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import runtime_release_upgrade as M
import prepare_deployment_plan as preparing

SOURCE = Path(__file__).resolve().parents[2]
NAMES = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'ARB', 'LINK', 'SUI', 'BNB', 'AVAX', 'OP']


def pool_bytes(names=NAMES):
    return (json.dumps({'instruments': [{'name': n, 'instId': n + '-USDT-SWAP', 'ctVal': 1.0,
        'tier': 'tier_1_bluechip' if n in ('BTC', 'ETH') else 'tier_2_momentum',
        'max_leverage': 3} for n in names]}, sort_keys=True) + '\n').encode()


class DeploymentPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='astra-plan-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.op = self.root / 'operation'
        self.op.mkdir(mode=0o700)
        self.live = self.root / 'live'
        (self.live / 'data').mkdir(parents=True)
        self.pool = self.live / 'data/instrument_pool.json'
        self.pool.write_bytes(pool_bytes())
        self.pool.chmod(0o600)
        (self.live / '.env').write_bytes(b'synthetic-private-config\n')
        (self.live / '.env').chmod(0o600)
        (self.op / 'provenance.json').write_bytes(b'{"synthetic":true}\n')
        self.obj = M.Upgrade.__new__(M.Upgrade)
        self.obj.op = self.op
        self.obj.session = None
        self.obj.previous = 'f' * 40
        self.obj.release = 'a' * 40
        self.obj.previous_image = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'b' * 64
        self.obj.image = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'c' * 64
        self.obj.manifest_sha = 'fixture'
        self.obj.state = {'manifest_sha': 'fixture', 'extras': [], 'prompt_refreshed': False}
        (self.live / 'source-marker').write_bytes(b'previous-source')
        config = {n: M.digest(self.live / n) if (self.live / n).is_file() else None for n in M.CONFIG}
        self.plan = {'schema': 1, 'previous_source': self.obj.previous,
            'previous_image': self.obj.previous_image, 'release_source': self.obj.release,
            'image': self.obj.image, 'helper_sha256': M.digest(Path(M.__file__)),
            'provenance_sha256': M.digest(self.op / 'provenance.json'),
            'protected_config': config,
            'pool': {'relative_path': 'data/instrument_pool.json', 'names': NAMES.copy(),
                     'sha256': M.digest(self.pool), 'size': self.pool.stat().st_size,
                     'uid': 0, 'gid': 0, 'mode': 0o600},
            'runtime': {'project': M.PROJECT, 'services': list(M.SERVICES), 'demo': True,
                        'schedule_seconds': 900, 'minimum_idle_window_seconds': 480}}
        self.repin()
        root_patch = patch.object(M, 'ROOT', self.live)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def repin(self):
        (self.op / 'deployment-plan.json').write_text(json.dumps(self.plan, sort_keys=True))
        self.obj.deployment_plan_sha256 = M.digest(self.op / 'deployment-plan.json')

    def gates(self):
        for name in ('sources', 'capacity', 'runtime', 'endpoints', 'validate_compose',
                     'image_metadata', 'stop', 'compose'):
            setattr(self.obj, name, Mock(return_value={}))

    def prepare(self):
        self.gates()
        candidate = self.op / 'candidate'
        candidate.mkdir()
        (candidate / 'source-marker').write_bytes(b'candidate-source')
        self.obj.state.update(phase='prepared', guard=self.obj.guard(self.live),
            candidate=M.tree_manifest(candidate), baseline={}, endpoints={},
            deployment_sources={'previous': M.deployment_source(self.live),
                                'release': M.deployment_source(candidate)})

    def database(self, running=False):
        with closing(sqlite3.connect(self.live / M.GATEWAY_DATABASE)) as db:
            db.execute('CREATE TABLE job_runs(status TEXT)')
            if running:
                db.execute("INSERT INTO job_runs VALUES('running')")
            db.commit()

    def test_approved_eleven_pool_keeps_exact_bytes_and_metadata(self):
        before = M.tree_manifest(self.live)
        self.assertEqual(self.obj.pool_guard()['names'], NAMES)
        self.assertEqual(M.tree_manifest(self.live), before)

    def test_approved_future_collection_is_not_restricted_to_ten_or_eleven(self):
        names = NAMES + ['UNI']
        self.pool.write_bytes(pool_bytes(names))
        self.plan['pool'].update(names=names, sha256=M.digest(self.pool), size=self.pool.stat().st_size)
        self.plan['protected_config']['data/instrument_pool.json'] = M.digest(self.pool)
        self.repin()
        self.assertEqual(len(self.obj.pool_guard()['names']), 12)

    def test_raw_pool_drift_with_unchanged_names_is_rejected(self):
        self.pool.write_bytes(self.pool.read_bytes() + b' ')
        with self.assertRaisesRegex(M.GateError, 'pool_config_drift'):
            self.obj.pool_guard()

    def test_missing_pool_is_rejected(self):
        self.pool.unlink()
        with self.assertRaisesRegex(M.GateError, 'pool_config_missing'):
            self.obj.pool_guard()

    def test_symlinked_pool_is_rejected(self):
        moved = self.root / 'other.json'
        self.pool.rename(moved)
        self.pool.symlink_to(moved)
        with self.assertRaisesRegex(M.GateError, 'symlink_path'):
            self.obj.pool_guard()

    def test_permissions_and_owner_drift_are_rejected(self):
        self.pool.chmod(0o640)
        with self.assertRaisesRegex(M.GateError, 'pool_config_drift'):
            self.obj.pool_guard()
        self.pool.chmod(0o600)
        os.chown(self.pool, 1, 1)
        with self.assertRaisesRegex(M.GateError, 'pool_config_drift'):
            self.obj.pool_guard()

    def test_absent_new_configuration_cannot_appear_without_approval(self):
        (self.live / 'data/risk_config.json').write_bytes(b'{}')
        with self.assertRaisesRegex(M.GateError, 'approved_configuration_drift'):
            self.obj.pool_guard()

    def test_configuration_directory_cannot_be_accepted_as_absence(self):
        (self.live / 'data/risk_config.json').mkdir()
        with self.assertRaisesRegex(M.GateError, 'configuration_file_type'):
            self.obj.pool_guard()

    def test_runtime_scope_requires_strict_boolean_and_integer_types(self):
        for key, value in (('demo', 1), ('schedule_seconds', 900.0),
                           ('minimum_idle_window_seconds', 480.0)):
            plan = copy.deepcopy(self.plan)
            plan['runtime'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(M.GateError, 'deployment_runtime_scope'):
                M.validate_deployment_plan(plan)

    def test_existing_configuration_drift_is_rejected_without_printing_it(self):
        (self.live / '.env').write_bytes(b'new-synthetic-secret')
        with self.assertRaisesRegex(M.GateError, '^approved_configuration_drift$'):
            self.obj.pool_guard()

    def test_plan_external_pin_cannot_be_omitted_or_wrong(self):
        for pin in (None, '', 'a' * 64):
            self.obj.deployment_plan_sha256 = pin
            with self.subTest(pin=pin), self.assertRaisesRegex(M.GateError, 'deployment_plan_external_pin'):
                self.obj.pool_guard()

    def test_changed_plan_is_not_automatically_reapproved(self):
        (self.op / 'deployment-plan.json').write_bytes(b'{}')
        with self.assertRaisesRegex(M.GateError, 'deployment_plan_external_pin'):
            self.obj.pool_guard()

    def test_duplicate_plan_fields_are_rejected_even_with_matching_pin(self):
        path = self.op / 'deployment-plan.json'
        path.write_bytes(b'{"schema":1,"schema":1}')
        self.obj.deployment_plan_sha256 = M.digest(path)
        with self.assertRaisesRegex(M.GateError, 'deployment_plan_duplicate_field'):
            self.obj.pool_guard()

    def test_executed_helper_is_bound_to_approved_plan(self):
        changed = self.op / 'altered-helper.py'
        changed.write_bytes(Path(M.__file__).read_bytes() + b'\n# change\n')
        with patch.object(M, '__file__', str(changed)), self.assertRaisesRegex(M.GateError, 'deployment_helper_identity'):
            self.obj.pool_guard()

    def test_provenance_is_bound_to_approved_plan(self):
        (self.op / 'provenance.json').write_bytes(b'{}')
        with self.assertRaisesRegex(M.GateError, 'deployment_provenance_identity'):
            self.obj.pool_guard()

    def test_invalid_identity_scope_and_duplicate_assets_are_rejected(self):
        for field, wrong in (('previous_source', 'tag'), ('previous_image', 'latest'),
                             ('release_source', M.UPSTREAM), ('schema', True)):
            bad = copy.deepcopy(self.plan)
            bad[field] = wrong
            with self.subTest(field=field), self.assertRaises(M.GateError):
                M.validate_deployment_plan(bad)
        for names in ([], ['BTC', 'BTC'], ['BTC', 'bad-name']):
            bad = copy.deepcopy(self.plan)
            bad['pool']['names'] = names
            with self.subTest(names=names), self.assertRaises(M.GateError):
                M.validate_deployment_plan(bad)
        bad = copy.deepcopy(self.plan)
        bad['runtime']['demo'] = False
        with self.assertRaises(M.GateError):
            M.validate_deployment_plan(bad)

    def test_prestop_pool_drift_never_stops_or_recovers(self):
        self.prepare()
        self.obj.rollback = Mock()
        self.pool.write_bytes(self.pool.read_bytes() + b' ')
        with self.assertRaisesRegex(M.GateError, 'pool_config_drift'):
            self.obj.execute()
        self.obj.stop.assert_not_called()
        self.obj.rollback.assert_not_called()

    def test_idle_guard_requires_zero_running_jobs_without_live_sqlite_sidecars(self):
        self.database(running=True)
        before = M.tree_manifest(self.live)
        with patch.object(M.time, 'time', return_value=1800), self.assertRaisesRegex(M.GateError, 'active_scheduled_job'):
            self.obj.idle_window()
        self.assertEqual(M.tree_manifest(self.live), before)

    def test_idle_guard_rejects_malformed_wal_or_short_schedule_window(self):
        self.database()
        with patch.object(M.time, 'time', return_value=2600), self.assertRaisesRegex(M.GateError, 'insufficient_idle_window'):
            self.obj.idle_window()
        Path(str(self.live / M.GATEWAY_DATABASE) + '-wal').write_bytes(b'pending')
        with patch.object(M.time, 'time', return_value=1800), self.assertRaisesRegex(M.GateError, 'sqlite_snapshot_invalid'):
            self.obj.idle_window()

    def test_idle_window_acceptance_reads_only_memory_copy(self):
        self.database()
        before = M.tree_manifest(self.live)
        with patch.object(M.time, 'time', return_value=1800):
            self.obj.idle_window()
        self.assertEqual(M.tree_manifest(self.live), before)

    def test_active_trader_process_refuses_cutover_even_without_job_row(self):
        self.database()
        proc = self.root / '1234'
        proc.mkdir()
        (proc / 'cmdline').write_bytes(b'python3\0/app/scripts/ai_brain_trader.py\0')
        original = Path.iterdir
        def directories(path):
            return iter([proc]) if path == Path('/proc') else original(path)
        with patch.object(Path, 'iterdir', directories), patch.object(M.time, 'time', return_value=1800), \
                self.assertRaisesRegex(M.GateError, 'active_trader_or_brain'):
            self.obj.idle_window()

    def test_capture_cannot_bind_unapproved_current_source_or_image(self):
        evidence = {'SOURCE_SHA': self.obj.release, 'GITHUB_SHA': self.obj.release,
            'PREVIOUS_SHA': self.obj.previous, 'PREVIOUS_IMAGE': self.obj.previous_image,
            'UPSTREAM_SHA': M.UPSTREAM, 'SOURCE_REPOSITORY': 'Jonoka/astra-quant-agent',
            'UPSTREAM_REPOSITORY': '0xethanq/astra-quant-agent', 'SOURCE_VERSION': 'v8.6.1',
            'platform': 'linux/amd64', 'IMAGE_NAME': 'ghcr.io/jonoka/astra-quant-agent',
            'IMAGE_DIGEST': 'sha256:' + 'c' * 64}
        (self.op / 'provenance.json').write_text(json.dumps(evidence))
        for source, image in (('d' * 40, self.obj.previous_image),
                              (self.obj.previous, 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'e' * 64)):
            with self.subTest(source=source), self.assertRaisesRegex(M.GateError, 'approved_previous_identity'):
                preparing.capture(self.op, source, image, NAMES)

    def test_constructor_binds_manifest_to_current_and_target_plan(self):
        manifest = {'schema': 2, 'previous_source': self.obj.previous,
            'previous_image': self.obj.previous_image, 'release_source': self.obj.release,
            'image': self.obj.image, 'deployment_plan_sha256': self.obj.deployment_plan_sha256,
            'upstream_source': M.UPSTREAM}
        path = self.op / 'manifest.json'
        path.write_text(json.dumps(manifest))
        with patch.object(M, 'operation_path', return_value=self.op):
            obj = M.Upgrade(self.op, None, self.obj.deployment_plan_sha256)
            self.assertEqual(obj.previous, self.obj.previous)
            self.assertEqual(obj.previous_image, self.obj.previous_image)
            for field in ('previous_source', 'previous_image', 'release_source', 'image',
                          'deployment_plan_sha256', 'schema'):
                bad = dict(manifest)
                bad[field] = 1 if field == 'schema' else 'incorrect'
                path.write_text(json.dumps(bad))
                with self.subTest(field=field), self.assertRaises(M.GateError):
                    M.Upgrade(self.op, None, self.obj.deployment_plan_sha256)

    def test_capture_native_collection_must_match_approved_names_and_raw_hash(self):
        evidence = {'SOURCE_SHA': self.obj.release, 'GITHUB_SHA': self.obj.release,
            'PREVIOUS_SHA': self.obj.previous, 'PREVIOUS_IMAGE': self.obj.previous_image,
            'UPSTREAM_SHA': M.UPSTREAM, 'SOURCE_REPOSITORY': 'Jonoka/astra-quant-agent',
            'UPSTREAM_REPOSITORY': '0xethanq/astra-quant-agent', 'SOURCE_VERSION': 'v8.6.1',
            'platform': 'linux/amd64', 'IMAGE_NAME': 'ghcr.io/jonoka/astra-quant-agent',
            'IMAGE_DIGEST': 'sha256:' + 'c' * 64}
        (self.op / 'provenance.json').write_text(json.dumps(evidence))
        shutil.copy2(Path(M.__file__), self.op / 'runtime_release_upgrade.py')
        image_labels = {'org.opencontainers.image.revision': self.obj.previous,
            'org.opencontainers.image.version': 'v8.6.1',
            'org.opencontainers.image.source': 'https://github.com/Jonoka/astra-quant-agent',
            'io.jonoka.astra.upstream-revision': M.UPSTREAM,
            'io.jonoka.astra.council-completion-patch': M.PATCH_ID,
            'io.jonoka.astra.okx-public-domains-patch': M.OKX_PATCH_ID,
            'io.jonoka.astra.cycle-deadline-patch': M.DEADLINE_PATCH_ID}
        metadata = {'Id': 'current-image-id', 'RepoDigests': [self.obj.previous_image],
            'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Labels': image_labels}}
        native = {'names': NAMES, 'sha256': M.digest(self.pool)}
        def command(*args, **kwargs):
            if args[:2] == ('docker', 'inspect'):
                return json.dumps([{'Config': {'Image': self.obj.previous_image,
                    'Labels': {'com.docker.compose.project': M.PROJECT,
                               'com.docker.compose.service': args[2].removeprefix('astraquant-')}},
                    'State': {'Running': True, 'Health': {'Status': 'healthy'}},
                    'RestartCount': 0, 'Image': 'current-image-id'}]).encode()
            if args[:3] == ('docker', 'image', 'inspect'):
                return json.dumps([metadata]).encode()
            self.assertEqual(args[:3], ('docker', 'exec', M.NAMES[0]))
            return json.dumps(native).encode()
        with patch.object(preparing, 'ROOT', self.live), patch.object(preparing, 'env_gate'), \
                patch.object(preparing, 'run', side_effect=command):
            plan = preparing.capture(self.op, self.obj.previous, self.obj.previous_image, NAMES)
            self.assertEqual(plan['pool']['sha256'], M.digest(self.pool))
            native['names'] = list(reversed(NAMES))
            with self.assertRaisesRegex(M.GateError, 'approved_native_pool'):
                preparing.capture(self.op, self.obj.previous, self.obj.previous_image, NAMES)
            native['names'] = NAMES
            metadata['Config']['Labels']['org.opencontainers.image.revision'] = 'd' * 40
            with self.assertRaisesRegex(M.GateError, 'current_source_image_identity'):
                preparing.capture(self.op, self.obj.previous, self.obj.previous_image, NAMES)

    def test_idle_failure_does_not_enter_stop_or_recovery(self):
        self.prepare()
        self.database(running=True)
        self.obj.rollback = Mock()
        with patch.object(M.time, 'time', return_value=1800), self.assertRaisesRegex(M.GateError, 'active_scheduled_job'):
            self.obj.execute()
        self.obj.stop.assert_not_called()
        self.obj.rollback.assert_not_called()

    def test_poststop_pool_drift_preserves_latest_bytes_and_refuses_restart(self):
        self.prepare()
        self.database()
        changed = self.pool.read_bytes() + b' '
        self.obj.stop.side_effect = lambda: self.pool.write_bytes(changed)
        with patch.object(M.time, 'time', return_value=1800), patch.object(M, 'containers', return_value={}), \
                self.assertRaisesRegex(M.GateError, 'upgrade_failed_recovery_incomplete'):
            self.obj.execute()
        self.assertEqual(self.pool.read_bytes(), changed)
        self.obj.compose.assert_not_called()
        self.obj.stop.assert_called_once()

    def test_latest_recovery_retains_new_bytes_and_deletions_before_refusing_unapproved_pool(self):
        self.gates()
        with closing(sqlite3.connect(self.live / 'data/astra_admin.db')) as db:
            db.executescript("CREATE TABLE admin_users(id INTEGER,username TEXT,password_hash TEXT,salt TEXT,iterations INTEGER,role TEXT,enabled INTEGER,created_at TEXT); INSERT INTO admin_users VALUES(1,'synthetic','hash','salt',1,'superadmin',1,'synthetic');")
        (self.live / 'data/retained.json').write_bytes(b'old-record')
        (self.live / 'data/deleted.json').write_bytes(b'intentionally-delete-later')
        (self.live / 'source-marker').write_bytes(b'previous-source')
        original = self.op / 'original-deployment'
        shutil.copytree(self.live, original)
        snapshot = M.tree_manifest(original)
        shutil.copytree(original, self.op / 'stopped-snapshot')
        self.obj.state.update(phase='candidate-active', snapshot=snapshot, databases=M.db_state(original))
        (self.live / 'source-marker').write_bytes(b'candidate-source')
        self.obj.state['deployment_sources'] = {'previous': M.deployment_source(original),
                                              'release': M.deployment_source(self.live)}
        (self.live / 'data/retained.json').write_bytes(b'new-record')
        (self.live / 'data/deleted.json').unlink()
        changed = self.pool.read_bytes() + b' '
        self.pool.write_bytes(changed)
        with patch.object(M, 'env_gate'), patch.object(M, 'containers', return_value={}), \
                self.assertRaisesRegex(M.GateError, 'pool_config_drift'):
            self.obj.rollback()
        self.assertEqual(self.pool.read_bytes(), changed)
        self.assertEqual((self.live / 'data/retained.json').read_bytes(), b'new-record')
        self.assertFalse((self.live / 'data/deleted.json').exists())
        self.assertEqual((self.live / 'source-marker').read_bytes(), b'previous-source')
        self.assertEqual((original / 'data/retained.json').read_bytes(), b'old-record')
        self.obj.compose.assert_not_called()

    def test_already_deployed_gateway_contract_requires_identical_code(self):
        old = self.root / 'old.py'
        new = self.root / 'new.py'
        code = 'SCHEMA = ' + repr('original;\n' + M.DEADLINE_SCHEMA_ADDITION) + '\nMIGRATION_COLUMNS = ()\n'
        old.write_text(code)
        new.write_text(code)
        M.gateway_source_compatibility(old, new)
        new.write_text(code + '\n# unrelated change\n')
        with self.assertRaisesRegex(M.GateError, 'gateway_schema_changed'):
            M.gateway_source_compatibility(old, new)

    def test_previous_and_release_native_readers_preserve_eleven_asset_fixture(self):
        code = '''import json,sys,hashlib
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from scripts import instrument_pool as pool
pool.POOL_FILE=Path(sys.argv[2])
def deny_write(*args, **kwargs): raise RuntimeError('write prohibited')
pool._write_pool_file=deny_write; pool._write_json_atomic=deny_write
before=pool.POOL_FILE.read_bytes()
items=pool.load_instruments()
assert pool.pool_is_trustworthy() and before==pool.POOL_FILE.read_bytes()
print(json.dumps({'names':[i['name'] for i in items],'sha256':hashlib.sha256(before).hexdigest()}))
'''
        previous = Path(os.environ['ASTRA_PREVIOUS_SOURCE'])
        sources = [previous, SOURCE]
        if os.environ.get('ASTRA_DEPLOYED_SOURCE'):
            sources.append(Path(os.environ['ASTRA_DEPLOYED_SOURCE']))
        for source in sources:
            with self.subTest(source=source.name):
                result = subprocess.run([sys.executable, '-c', code, str(source), str(self.pool)],
                    check=True, capture_output=True, text=True)
                self.assertEqual(json.loads(result.stdout.splitlines()[-1]),
                                 {'names': NAMES, 'sha256': M.sha(pool_bytes())})


# These constants are an independent regression oracle, not imported from the
# implementation being tested. The old bytes come from the actual Git object.
REFRESH_PREVIOUS = '1bd3c016bf4375114d34022c1528d119d90cd6ad'
REFRESH_IMAGE = 'ghcr.io/jonoka/astra-quant-agent@sha256:7bfc6bb0b75c6a63393f1c717c771cc7097059b2b2c4a6ddca2de86049005bee'
REFRESH_OLD = 'c032575fc4f3af3fc32fcb9ec410770468eecdc83a7e5f7fd6810096a3cf7573'
REFRESH_NEW = 'ea9ae97ee05637664b71cfb86a9f32aeea946893e35591f745cb0cccb378ce63'
REFRESH_NAMES = NAMES + ['UNI', 'ADA']


class ExactPromptRefreshTests(unittest.TestCase):
    """Real pinned-plan/file/guard lifecycle with narrow external fixtures.

    Source fixtures contain the real Git/current files needed by sources() and
    native prompt/pool/risk readers, rather than synthetic hash-only manifests.
    Docker inventory/Compose, HTTP and elapsed time are simulated. The reader
    subprocess runs the production reader code, loading the real schema literal
    without importing unrelated exchange/trading execution dependencies.

    On a non-root workstation ONLY .env/pool stat ownership is adapted to root
    for these fixtures; raw os.stat/lstat metadata, modes, bytes and all guards
    remain checked. This does NOT certify root privilege or owner enforcement.
    Hosted root runs use real ownership with no stat adaptation. The original
    root-only DeploymentPlanTests suite above is deliberately left unchanged.
    """
    SOURCE_FILES = (
        'Dockerfile', 'docker-compose.yml', M.PROMPT,
        'astra_backend/__init__.py', 'astra_backend/version.py',
        'astra_backend/admin_auth.py', 'astra_backend/risk_reservation.py',
        'astra_backend/config.py', 'astra_gateway/__init__.py', 'astra_gateway/store.py',
        'scripts/db_manager.py', 'scripts/prompt_library.py',
        'scripts/prompt_templates.py', 'scripts/local_lock.py',
        'scripts/instrument_pool.py', 'scripts/risk_constants.py',
        'scripts/okx_runtime.py', 'scripts/ai_brain_trader.py',
    )

    @classmethod
    def setUpClass(cls):
        cls.source_bytes = {'previous': {}, 'release': {}}
        # Full source checkouts in the hosted gate contain the historical object.
        # ASTRA_DEPLOYED_SOURCE also supports its independently pinned shallow
        # checkout, without accepting an arbitrary directory as historical proof.
        previous = Path(os.environ.get('ASTRA_DEPLOYED_SOURCE', SOURCE))
        for name in cls.SOURCE_FILES:
            result = subprocess.run(['git', '-C', str(SOURCE), 'show',
                                     REFRESH_PREVIOUS + ':' + name], capture_output=True)
            if result.returncode:
                identity = subprocess.run(['git', '-C', str(previous), 'rev-parse', 'HEAD'],
                                          check=True, capture_output=True, text=True).stdout.strip()
                if identity != REFRESH_PREVIOUS:
                    raise AssertionError('Exact historical prompt source unavailable')
                result = subprocess.run(['git', '-C', str(previous), 'show',
                                         'HEAD:' + name], check=True, capture_output=True)
            cls.source_bytes['previous'][name] = result.stdout
            cls.source_bytes['release'][name] = (SOURCE / name).read_bytes()
        if M.sha(cls.source_bytes['previous'][M.PROMPT]) != REFRESH_OLD:
            raise AssertionError('Historical prompt is not the independently reviewed bytes')
        if M.sha(cls.source_bytes['release'][M.PROMPT]) != REFRESH_NEW:
            raise AssertionError('Release prompt is not the independently reviewed bytes')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='astra-exact-prompt-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.op = self.root / 'operation'
        self.op.mkdir(mode=0o700)
        self.live = self.root / 'live'
        for label, files in self.source_bytes.items():
            source = self.op / ('source-' + label)
            for name, raw in files.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            (source / 'frontend/public').mkdir(parents=True)
            (source / 'docs/images').mkdir(parents=True)
            (source / 'frontend/public/images').symlink_to('../../docs/images')
        shutil.copytree(self.op / 'source-previous', self.live, symlinks=True)
        self.pool = self.live / 'data/instrument_pool.json'
        self.pool.write_bytes(pool_bytes(REFRESH_NAMES))
        self.pool.chmod(0o600)
        (self.live / M.PROMPT).chmod(0o640)
        (self.live / '.env').write_text(
            'ASTRA_STANDALONE_GATEWAY=true\nASTRA_OKX_ENV=demo\nOKX_IS_SIMULATED=1\n'
            'ASTRA_MIN_ENTRY_CONFIDENCE=75\nASTRA_MIN_RISK_REWARD=1.6\n'
            'ASTRA_STOP_COOLDOWN_MINUTES=15\nASTRA_SCALE_OUT_TRIGGER_ATR=2.2\n'
            'ASTRA_SCALE_OUT_RATIO=0.4\nASTRA_MAX_SAME_DIRECTION_POSITIONS=3\n')
        (self.live / '.env').chmod(0o600)
        (self.live / 'data/prompt_library.local.json').write_bytes(
            b'{"version":2,"active_profile_id":"allpattern_swing","profiles":{},"revisions":[]}\n')
        (self.live / 'data/risk_config.json').write_bytes(b'{"fixture":"retained"}\n')
        (self.live / 'logs').mkdir()
        (self.live / 'backups').mkdir()
        (self.live / 'plugins').mkdir()
        (self.live / 'plugins/custom.py').write_bytes(b'# retained plugin\n')
        (self.live / M.OVERRIDE).write_text('services:\n' + ''.join(
            '  ' + service + ':\n    image: ' + REFRESH_IMAGE + '\n' for service in M.SERVICES))
        with closing(sqlite3.connect(self.live / 'data/astra_admin.db')) as db:
            db.executescript("CREATE TABLE admin_users(id INTEGER,username TEXT,password_hash TEXT,salt TEXT,iterations INTEGER,role TEXT,enabled INTEGER,created_at TEXT); INSERT INTO admin_users VALUES(1,'fixture','hash','salt',1,'superadmin',1,'fixture');")
        with closing(sqlite3.connect(self.live / M.GATEWAY_DATABASE)) as db:
            db.execute('CREATE TABLE job_runs(status TEXT)')
        with closing(sqlite3.connect(self.live / 'data/astra_quant.db')) as db:
            db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)')
            db.execute("INSERT INTO records VALUES(1, 'before')")
            db.commit()
        (self.op / 'provenance.json').write_bytes(b'{"fixture":"hosted-evidence"}\n')
        (self.op / 'Dockerfile.release').write_bytes(b'# fixture image recipe\n')
        self.obj = M.Upgrade.__new__(M.Upgrade)
        self.obj.op, self.obj.session = self.op, None
        self.obj.previous, self.obj.previous_image = REFRESH_PREVIOUS, REFRESH_IMAGE
        self.obj.release = 'a' * 40
        self.obj.image = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'c' * 64
        self.obj.state = {}
        self.plan = {'schema': 1, 'previous_source': self.obj.previous,
            'previous_image': self.obj.previous_image, 'release_source': self.obj.release,
            'image': self.obj.image, 'helper_sha256': M.digest(Path(M.__file__)),
            'provenance_sha256': M.digest(self.op / 'provenance.json'),
            'protected_config': {n: M.configuration_hash(self.live / n) for n in M.CONFIG},
            'pool': {'relative_path': 'data/instrument_pool.json', 'names': REFRESH_NAMES.copy(),
                     'sha256': M.digest(self.pool), 'size': self.pool.stat().st_size,
                     'uid': 0, 'gid': 0, 'mode': 0o600},
            'runtime': {'project': M.PROJECT, 'services': list(M.SERVICES), 'demo': True,
                        'schedule_seconds': 900, 'minimum_idle_window_seconds': 480}}
        self.repin()
        self.obj.manifest = {'schema': 2, 'previous_source': self.obj.previous,
            'previous_image': self.obj.previous_image, 'release_source': self.obj.release,
            'image': self.obj.image, 'upstream_source': M.UPSTREAM,
            'deployment_plan_sha256': self.obj.deployment_plan_sha256,
            'source_files': {label: M.files_manifest(self.op / ('source-' + label))
                             for label in ('previous', 'release')},
            'compatibility': {'previous_source': self.obj.previous,
                'previous_image': self.obj.previous_image, 'release_source': self.obj.release,
                'upstream_source': M.UPSTREAM, 'image': self.obj.image,
                'rollback_preserves_new_records': True,
                'hosted_run': 'https://github.com/Jonoka/astra-quant-agent/actions/runs/123',
                'checks': dict.fromkeys(M.CHECKS, 'passed'),
                'reviewed_formats': ['sqlite', 'encrypted_credentials', 'settings', 'prompts', 'trading_records']}}
        self.pin_manifest()
        self.commands = []
        self.native_reads = []
        self.inventory = self.runtime_inventory(previous=True)
        self.real_run = M.run
        self.patching(M, 'ROOT', self.live)
        self.patching(M, 'containers', side_effect=lambda: copy.deepcopy(self.inventory))
        self.patching(M, 'run', side_effect=self.command)
        self.patching(M, 'request', side_effect=self.http)
        self.patching(M.time, 'sleep')
        self.patching(M.time, 'time', return_value=1800)
        if os.geteuid() != 0:
            original_stat = Path.stat
            root = self.root
            class FixtureOwnership:
                def __init__(self, actual):
                    self.actual = actual
                    self.st_uid = self.st_gid = 0
                def __getattr__(self, name):
                    return getattr(self.actual, name)
            def fixture_stat(path, *, follow_symlinks=True):
                actual = original_stat(path, follow_symlinks=follow_symlinks)
                if (follow_symlinks and root in path.parents and
                        path.name in ('.env', 'instrument_pool.json')):
                    return FixtureOwnership(actual)
                return actual
            self.patching(Path, 'stat', fixture_stat)

    def patching(self, target, name, *args, **kwargs):
        pending = patch.object(target, name, *args, **kwargs)
        value = pending.start()
        self.addCleanup(pending.stop)
        return value

    def repin(self):
        (self.op / 'deployment-plan.json').write_text(json.dumps(self.plan, sort_keys=True))
        self.obj.deployment_plan_sha256 = M.digest(self.op / 'deployment-plan.json')

    def pin_manifest(self):
        (self.op / 'manifest.json').write_text(json.dumps(self.obj.manifest, sort_keys=True))
        self.obj.manifest_sha = M.digest(self.op / 'manifest.json')

    def change_source(self, label, raw):
        (self.op / ('source-' + label) / M.PROMPT).write_bytes(raw)
        self.obj.manifest['source_files'][label][M.PROMPT] = M.sha(raw)
        self.pin_manifest()

    def runtime_inventory(self, previous):
        return {name: {'id': ('previous-' if previous else 'release-') + service,
            'ref': self.obj.previous_image if previous else self.obj.image,
            'image': 'previous-image-id' if previous else 'release-image-id',
            'project': M.PROJECT, 'service': service, 'state': 'running',
            'health': 'healthy', 'restart': 0, 'ports': {},
            'healthcheck': {'Test': ['CMD-SHELL', 'fixture-health']},
            'mounts': [{'Type': 'bind', 'Source': str(self.live / n),
                        'Destination': '/app/' + n, 'RW': True}
                       for n in ('.env', 'data', 'logs', 'backups')]}
            for service, name in zip(M.SERVICES, M.NAMES)}

    def command(self, *args, **kwargs):
        args = tuple(str(a) for a in args)
        if not args or args[0] != 'docker':
            return self.real_run(*args, **kwargs)
        self.commands.append(args)
        if args[1:3] == ('image', 'inspect'):
            previous = args[3] == self.obj.previous_image
            labels = {'org.opencontainers.image.revision': self.obj.previous if previous else self.obj.release,
                'org.opencontainers.image.version': 'v8.6.1',
                'org.opencontainers.image.source': 'https://github.com/Jonoka/astra-quant-agent',
                'io.jonoka.astra.upstream-revision': M.UPSTREAM,
                'io.jonoka.astra.council-completion-patch': M.PATCH_ID,
                'io.jonoka.astra.okx-public-domains-patch': M.OKX_PATCH_ID,
                'io.jonoka.astra.cycle-deadline-patch': M.DEADLINE_PATCH_ID,
                'io.jonoka.astra.build-recipe-sha256': M.digest(self.op / 'Dockerfile.release')}
            return json.dumps([{'Id': 'previous-image-id' if previous else 'release-image-id',
                'RepoDigests': [args[3]], 'Os': 'linux', 'Architecture': 'amd64',
                'Config': {'Labels': labels}}]).encode()
        if args[1] == 'stop':
            for current in self.inventory.values():
                if current['id'] in args[4:]:
                    current['state'] = 'exited'
            return b''
        if args[1] == 'logs':
            return b''
        if args[1] == 'compose':
            files = [Path(args[index + 1]) for index, arg in enumerate(args) if arg == '-f']
            spec = M.yaml.safe_load(files[-1].read_bytes())
            image = spec['services']['backend']['image']
            if 'up' in args:
                self.inventory = self.runtime_inventory(previous=image == self.obj.previous_image)
            if '--format' in args:
                return json.dumps({'services': {service: {'image': image,
                    'volumes': [{'type': 'bind', 'source': str(self.live / n), 'target': '/app/' + n}
                                for n in ('.env', 'data', 'logs', 'backups')]}
                    for service in M.SERVICES}}).encode()
            return b''
        if args[1] == 'exec':
            code = args[-1]
            if 'read-only strategy preflight' in code:
                return self.native_strategy(code)
            if 'workers, backends' in code:
                gateway = args[2] == 'astraquant-gateway'
                return json.dumps({'workers': [11] if gateway else [],
                    'backends': [] if gateway else [12], 'owner': 11 if gateway else None,
                    'held': gateway}).encode()
        self.fail('Unexpected external command: ' + repr(args[:4]))

    def native_strategy(self, code):
        # Import actual readers from the deployed fixture. Only the immutable
        # schema export is loaded by AST, avoiding market/exchange dependencies.
        bootstrap = '''import ast, os, sys, types
from pathlib import Path
root = Path(sys.argv[1]).resolve()
assert Path.cwd() == root and sys.flags.isolated
sys.path.insert(0, str(root))
def refuse_external_private_files(event, args):
 if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
  path = Path(os.fsdecode(args[0])).resolve()
  if path.name in ('.env', 'astra_secrets.enc', '.astra_secret_key', 'astra_backup_secrets.enc', '.astra_backup_secret_key'):
   assert root in path.parents, 'reader attempted external configuration access'
sys.addaudithook(refuse_external_private_files)
for line in (root / '.env').read_text().splitlines():
 if '=' in line:
  key, value = line.split('=', 1); os.environ[key] = value
schema = next(ast.literal_eval(node.value) for node in ast.parse((root / 'scripts/ai_brain_trader.py').read_text()).body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'READONLY_OUTPUT_SCHEMA' for target in node.targets))
module = types.ModuleType('scripts.ai_brain_trader')
module.__file__ = str(root / 'scripts/ai_brain_trader.py')
module.READONLY_OUTPUT_SCHEMA = schema
sys.modules['scripts.ai_brain_trader'] = module
from astra_backend import config, version
from scripts import risk_constants, instrument_pool, prompt_library, prompt_templates, local_lock, okx_runtime
import astra_backend, astra_gateway
assert config.ROOT == root and config.settings.root == root
assert instrument_pool.ROOT == root and prompt_library.ROOT == root and okx_runtime.ROOT == root
for imported in (astra_backend, astra_gateway, config, version, risk_constants, instrument_pool, prompt_library, prompt_templates, local_lock, okx_runtime, module):
 assert root in Path(imported.__file__).resolve().parents, 'reader imported outside fixture'
assert 'astra_gateway.secrets' not in sys.modules
'''
        # The only translated container path is its bind mount; assertions and
        # the production reader body below are untouched.
        translated = code.replace("Path('/app/data')", "Path(" + repr(str(self.live / 'data')) + ")")
        # Isolated mode ignores PYTHONPATH/user site packages; the only project
        # path is this fixture. Do not inherit credentials or runtime settings.
        env = {'LANG': 'C.UTF-8'}
        result = subprocess.run([sys.executable, '-I', '-B', '-c', bootstrap + translated, str(self.live)],
                                check=True, capture_output=True, env=env, cwd=self.live)
        value = json.loads(result.stdout)
        self.native_reads.append(value)
        return result.stdout

    def http(self, base, path, token=None, expected=200):
        if path.endswith('/health'):
            return {'version': '8.6.1', 'status': 'ok', 'credentials': {'fixture': True}}
        if path.endswith('/auth/status'):
            return {'initialized': True}
        if path.endswith('/auth/me'):
            self.assertEqual(expected, 401)
            return {}
        return {'version': '8.6.1'}

    def physical_file(self, path):
        info = os.stat(path)
        return (path.read_bytes(), info.st_uid, info.st_gid, info.st_mode & 0o7777)

    def activate(self):
        self.obj.preflight()
        self.obj.execute()
        self.assertEqual(self.obj.state['phase'], 'accepted')

    def assert_no_stop(self):
        self.assertFalse(any(c[1] == 'stop' or c[1] == 'compose' and 'up' in c for c in self.commands))
        self.assertFalse((self.op / 'failure.json').exists())

    def test_exact_reviewed_transition_and_actual_source_hashes(self):
        self.assertEqual(M.OFFICIAL_PROMPT_REFRESH, {'previous_source': REFRESH_PREVIOUS,
            'previous_image': REFRESH_IMAGE, 'previous_sha256': REFRESH_OLD, 'release_sha256': REFRESH_NEW})
        self.obj.sources()
        plan_before = (self.op / 'deployment-plan.json').read_bytes()
        self.assertEqual(self.obj.approved_prompt_hash(self.obj.deployment_plan()), REFRESH_NEW)
        self.assertEqual((self.op / 'deployment-plan.json').read_bytes(), plan_before)
        self.assertEqual(M.digest(self.live / M.PROMPT), REFRESH_OLD)

    def test_real_preflight_execute_guards_accept_and_restore_exactly(self):
        pool_before = self.physical_file(self.pool)
        prompt_before = self.physical_file(self.live / M.PROMPT)
        protected = {n: self.physical_file(self.live / n) if (self.live / n).exists() else None
                     for n in M.CONFIG if n != M.PROMPT}
        plan_before = (self.op / 'deployment-plan.json').read_bytes()
        self.activate()
        self.assertTrue(self.obj.state['prompt_refreshed'])
        self.assertEqual(M.digest(self.live / M.PROMPT), REFRESH_NEW)
        self.assertEqual(self.physical_file(self.live / M.PROMPT)[1:], prompt_before[1:])
        self.assertEqual(self.physical_file(self.pool), pool_before)
        self.assertEqual(self.obj.pool_guard()['names'], REFRESH_NAMES)
        self.assertEqual(len(self.obj.read('upgrade-observation.json')), 7)
        self.assertEqual(len(self.native_reads), 2)
        self.assertTrue(all(v['pool'] == REFRESH_NAMES for v in self.native_reads))
        self.assertNotEqual(self.native_reads[0]['rendered_sha256'], self.native_reads[1]['rendered_sha256'])
        self.assertEqual(self.native_reads[0]['schema_sha256'], self.native_reads[1]['schema_sha256'])
        for name, before in protected.items():
            self.assertEqual(self.physical_file(self.live / name) if (self.live / name).exists() else None, before)
        with closing(sqlite3.connect(self.live / 'data/astra_quant.db')) as db:
            db.execute("INSERT INTO records VALUES(2, 'after')")
            db.commit()
        self.obj.rollback()
        self.assertEqual(self.obj.state['phase'], 'rolled-back')
        self.assertEqual(self.physical_file(self.live / M.PROMPT), prompt_before)
        self.assertEqual(self.physical_file(self.pool), pool_before)
        self.assertEqual(self.obj.pool_guard()['names'], REFRESH_NAMES)
        self.assertEqual(len(self.obj.read('rollback-observation.json')), 7)
        with closing(sqlite3.connect(self.live / 'data/astra_quant.db')) as db:
            self.assertEqual(db.execute('SELECT * FROM records ORDER BY id').fetchall(), [(1, 'before'), (2, 'after')])
        self.assertEqual((self.op / 'deployment-plan.json').read_bytes(), plan_before)
        self.assertEqual(M.digest(self.op / 'original-deployment' / M.PROMPT), REFRESH_OLD)
        self.assertEqual(M.digest(self.op / 'failed-candidate' / M.PROMPT), REFRESH_NEW)

    def test_wrong_source_image_or_either_hash_is_not_authorized(self):
        original = copy.deepcopy(self.plan)
        for key, value in (('previous_source', 'f' * 40),
                           ('previous_image', 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'd' * 64)):
            bad = copy.deepcopy(original)
            bad[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(M.GateError, '^unapproved_official_prompt_refresh$'):
                self.obj.approved_prompt_hash(bad)
        for label in ('previous', 'release'):
            old = self.obj.manifest['source_files'][label][M.PROMPT]
            self.obj.manifest['source_files'][label][M.PROMPT] = '0' * 64
            with self.subTest(label=label), self.assertRaisesRegex(M.GateError, '^unapproved_official_prompt_refresh$'):
                self.obj.approved_prompt_hash(original)
            self.obj.manifest['source_files'][label][M.PROMPT] = old
        original['protected_config'][M.PROMPT] = 'e' * 64
        with self.assertRaisesRegex(M.GateError, '^unapproved_official_prompt_refresh$'):
            self.obj.approved_prompt_hash(original)

    def test_unapproved_factory_change_fails_real_preflight_before_stop(self):
        self.change_source('release', self.source_bytes['release'][M.PROMPT] + b'\n')
        with self.assertRaisesRegex(M.GateError, '^unapproved_official_prompt_refresh$'):
            self.obj.preflight()
        self.assertFalse((self.op / 'candidate').exists())
        self.assert_no_stop()

    def test_custom_prompt_is_preserved_by_preflight_and_refresh(self):
        custom = self.source_bytes['previous'][M.PROMPT] + b'\n '
        (self.live / M.PROMPT).write_bytes(custom)
        self.plan['protected_config'][M.PROMPT] = M.sha(custom)
        self.repin()
        self.activate()
        self.assertFalse(self.obj.state['prompt_refreshed'])
        self.assertEqual((self.live / M.PROMPT).read_bytes(), custom)
        self.obj.rollback()
        self.assertEqual((self.live / M.PROMPT).read_bytes(), custom)

    def test_missing_prompt_preserves_intentional_deletion(self):
        # Missing factory bytes need not pass the strategy profile assertions;
        # isolate that external reader response while leaving every plan/source
        # and lifecycle guard real for the intentional-deletion case.
        (self.live / M.PROMPT).unlink()
        self.plan['protected_config'][M.PROMPT] = None
        self.repin()
        reader = self.patching(self.obj, 'strategy_reader', wraps=self.obj.strategy_reader)
        original_native = self.native_strategy
        self.native_strategy = lambda code: json.dumps({'fixture': 'missing factory'}).encode()
        self.activate()
        self.assertGreater(reader.call_count, 0)
        self.assertFalse((self.live / M.PROMPT).exists())
        self.assertFalse(self.obj.state['prompt_refreshed'])
        self.native_strategy = original_native

    def test_identical_official_hashes_need_no_special_transition(self):
        self.change_source('release', self.source_bytes['previous'][M.PROMPT])
        before = self.physical_file(self.live / M.PROMPT)
        self.activate()
        self.assertEqual(self.physical_file(self.live / M.PROMPT), before)
        self.assertEqual(self.obj.approved_prompt_hash(self.obj.deployment_plan()), REFRESH_OLD)

    def test_pre_stop_new_prompt_is_rejected_without_stop_or_recovery(self):
        self.obj.preflight()
        (self.live / M.PROMPT).write_bytes(self.source_bytes['release'][M.PROMPT])
        rollback = self.patching(self.obj, 'rollback', wraps=self.obj.rollback)
        with self.assertRaisesRegex(M.GateError, '^approved_configuration_drift$'):
            self.obj.execute()
        rollback.assert_not_called()
        self.assert_no_stop()

    def test_refresh_flag_alone_does_not_authorize_old_root_new_prompt(self):
        self.obj.preflight()
        self.obj.state['prompt_refreshed'] = True
        (self.live / M.PROMPT).write_bytes(self.source_bytes['release'][M.PROMPT])
        with self.assertRaisesRegex(M.GateError, '^approved_configuration_drift$'):
            self.obj.pool_guard()
        (self.op / 'original-deployment').mkdir()
        self.obj.state['phase'] = 'candidate-active'
        with self.assertRaisesRegex(M.GateError, '^foreign_deployment_source$'):
            self.obj.pool_guard()

    def test_new_root_requires_refresh_flag_and_original_directory(self):
        self.activate()
        self.obj.state['prompt_refreshed'] = False
        with self.assertRaisesRegex(M.GateError, '^approved_configuration_drift$'):
            self.obj.pool_guard()
        self.obj.state['prompt_refreshed'] = True
        (self.op / 'original-deployment').rename(self.op / 'saved-original')
        with self.assertRaisesRegex(M.GateError, '^approved_configuration_drift$'):
            self.obj.pool_guard()

    def test_new_root_with_old_prompt_is_rejected(self):
        self.activate()
        (self.live / M.PROMPT).write_bytes(self.source_bytes['previous'][M.PROMPT])
        with self.assertRaisesRegex(M.GateError, '^approved_configuration_drift$'):
            self.obj.pool_guard()

    def test_candidate_phase_and_source_identity_cannot_be_forged(self):
        self.activate()
        self.obj.state['phase'] = 'prepared'
        with self.assertRaisesRegex(M.GateError, '^deployment_candidate_phase$'):
            self.obj.pool_guard()
        self.obj.state['phase'] = 'accepted'
        source = self.live / 'scripts/prompt_library.py'
        source.write_bytes(source.read_bytes() + b'\n# foreign source\n')
        with self.assertRaisesRegex(M.GateError, '^foreign_deployment_source$'):
            self.obj.pool_guard()

    def test_failed_candidate_directory_disables_release_prompt_exception(self):
        self.activate()
        (self.op / 'failed-candidate').mkdir()
        self.obj.state['phase'] = 'recovery-active'
        with self.assertRaisesRegex(M.GateError, '^approved_configuration_drift$'):
            self.obj.pool_guard()

    def test_rollback_cannot_restart_if_prompt_was_not_restored(self):
        self.activate()
        self.commands.clear()
        restore = self.patching(self.obj, 'restore_prompt', return_value=None)
        with self.assertRaisesRegex(M.GateError, '^approved_configuration_drift$'):
            self.obj.rollback()
        restore.assert_called_once()
        self.assertFalse(any(c[1] == 'compose' and 'up' in c for c in self.commands))
        self.assertEqual(self.obj.state['phase'], 'recovery-active')
        self.assertEqual(M.digest(self.live / M.PROMPT), REFRESH_NEW)

    def test_local_risk_env_pool_and_custom_prompt_drift_still_reject(self):
        self.activate()
        for name in ('data/prompt_library.local.json', 'data/risk_config.json', '.env',
                     'data/instrument_pool.json', M.PROMPT):
            path = self.live / name
            before = path.read_bytes()
            path.write_bytes(before + b' ')
            gate = '^pool_config_drift$' if name == 'data/instrument_pool.json' else '^approved_configuration_drift$'
            with self.subTest(name=name), self.assertRaisesRegex(M.GateError, gate):
                self.obj.pool_guard()
            path.write_bytes(before)
        self.pool.chmod(0o640)
        with self.assertRaisesRegex(M.GateError, '^pool_config_drift$'):
            self.obj.pool_guard()

    def test_protected_config_metadata_drift_reaches_continuity_gate(self):
        self.activate()
        path = self.live / 'data/prompt_library.local.json'
        path.chmod(0o600 if path.stat().st_mode & 0o777 != 0o600 else 0o640)
        self.obj.pool_guard()
        with self.assertRaisesRegex(M.GateError, '^configuration_credential_drift$'):
            self.obj.continuity()

    def test_source_bytes_tampering_before_refresh_never_writes_prompt(self):
        self.obj.preflight()
        target = self.op / 'candidate'
        self.obj.carry(self.live, target, 'test-seed')
        before = self.physical_file(target / M.PROMPT)
        source = self.op / 'source-release' / M.PROMPT
        source.write_bytes(source.read_bytes() + b' ')
        with self.assertRaisesRegex(M.GateError, '^raw_source_drift$'):
            self.obj.prompt_refresh(target)
        self.assertEqual(self.physical_file(target / M.PROMPT), before)

    def test_reapproved_manifest_cannot_authorize_different_factory_bytes(self):
        self.obj.preflight()
        target = self.op / 'candidate'
        self.obj.carry(self.live, target, 'test-seed')
        before = self.physical_file(target / M.PROMPT)
        self.change_source('release', self.source_bytes['release'][M.PROMPT] + b' ')
        with self.assertRaisesRegex(M.GateError, '^unapproved_official_prompt_refresh$'):
            self.obj.prompt_refresh(target)
        self.assertEqual(self.physical_file(target / M.PROMPT), before)

    def test_source_bytes_tampering_before_restore_never_writes_prompt(self):
        self.activate()
        before = self.physical_file(self.live / M.PROMPT)
        source = self.op / 'source-previous' / M.PROMPT
        source.write_bytes(source.read_bytes() + b' ')
        with self.assertRaisesRegex(M.GateError, '^raw_source_drift$'):
            self.obj.restore_prompt(self.live)
        self.assertEqual(self.physical_file(self.live / M.PROMPT), before)

    def test_restore_preserves_post_upgrade_custom_or_deleted_prompt(self):
        self.activate()
        path = self.live / M.PROMPT
        custom = path.read_bytes() + b'\n '
        path.write_bytes(custom)
        self.obj.restore_prompt(self.live)
        self.assertEqual(path.read_bytes(), custom)
        path.unlink()
        self.obj.restore_prompt(self.live)
        self.assertFalse(path.exists())


    def reload(self):
        # The operation-directory location is a fixture; constructor, external
        # plan/helper/provenance pins, manifest and saved state are read afresh.
        with patch.object(M, 'operation_path', return_value=self.op):
            return M.Upgrade(self.op, None, self.obj.deployment_plan_sha256)

    def test_reloaded_post_cutover_rollback_verifies_without_mutation(self):
        self.activate()
        self.obj.rollback()
        restored = self.reload()
        before = M.tree_manifest(self.live)
        self.commands.clear()
        restored.verify(previous=True)
        self.assertEqual(restored.state['phase'], 'rolled-back')
        self.assertEqual(M.tree_manifest(self.live), before)
        self.assertFalse(any(c[1] == 'stop' or c[1] == 'compose' and 'up' in c for c in self.commands))
        with self.assertRaisesRegex(M.GateError, '^rollback_phase$'):
            restored.rollback()

    def test_reloaded_early_rollback_verifies_without_original_tree(self):
        self.obj.preflight()
        self.obj.phase('stopping')
        self.obj.rollback()
        self.assertFalse((self.op / 'original-deployment').exists())
        self.assertFalse((self.op / 'failed-candidate').exists())
        restored = self.reload()
        before = M.tree_manifest(self.live)
        restored.verify(previous=True)
        self.assertEqual(restored.state['phase'], 'rolled-back')
        self.assertEqual(M.tree_manifest(self.live), before)

    def test_reloaded_rollback_still_rejects_foreign_source_image_and_container(self):
        self.activate()
        self.obj.rollback()
        restored = self.reload()
        source = self.live / 'scripts/prompt_library.py'
        raw = source.read_bytes()
        source.write_bytes(raw + b'\n# foreign deployment\n')
        with self.assertRaisesRegex(M.GateError, '^foreign_deployment_source$'):
            restored.verify(previous=True)
        source.write_bytes(raw)
        backend = self.inventory[M.NAMES[0]]
        for field, value, gate in (
                ('ref', 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'e' * 64, 'foreign_deployment_image'),
                ('image', 'foreign-image-id', 'foreign_deployment_image'),
                ('id', 'foreign-container-id', 'foreign_deployment_container')):
            before = backend[field]
            backend[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(M.GateError, '^' + gate + '$'):
                restored.verify(previous=True)
            backend[field] = before

    def test_identical_factory_hash_allows_noop_outside_special_tuple(self):
        self.change_source('release', self.source_bytes['previous'][M.PROMPT])
        different = copy.deepcopy(self.obj.deployment_plan())
        different['previous_source'] = 'f' * 40
        different['previous_image'] = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'd' * 64
        M.validate_deployment_plan(different)
        self.assertEqual(self.obj.approved_prompt_hash(different), REFRESH_OLD)

    def test_wrong_previous_identity_fails_real_preflight_without_stop(self):
        for key, wrong in (('previous_source', 'f' * 40),
                           ('previous_image', 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'd' * 64)):
            original = self.plan[key]
            self.plan[key] = wrong
            attribute = 'previous' if key == 'previous_source' else key
            setattr(self.obj, attribute, wrong)
            self.obj.manifest['compatibility'][key] = wrong
            self.repin()
            self.pin_manifest()
            with self.subTest(key=key), self.assertRaisesRegex(M.GateError, '^unapproved_official_prompt_refresh$'):
                self.obj.preflight()
            self.assert_no_stop()
            self.plan[key] = original
            setattr(self.obj, attribute, original)
            self.obj.manifest['compatibility'][key] = original
            self.repin()
            self.pin_manifest()


    def prepare_recovery_records(self):
        (self.live / 'data/retained.json').write_bytes(b'before-start')
        (self.live / 'data/deleted.json').write_bytes(b'delete-after-start')
        self.obj.preflight()

    def add_poststart_records(self):
        self.assertEqual(M.digest(self.live / M.PROMPT), REFRESH_NEW)
        with closing(sqlite3.connect(self.live / 'data/astra_quant.db')) as db:
            db.execute("INSERT INTO records VALUES(2, 'after-start')")
            db.commit()
        (self.live / 'data/retained.json').write_bytes(b'latest-after-start')
        (self.live / 'data/deleted.json').unlink()

    def assert_latest_recovery(self):
        self.assertEqual(self.obj.state['phase'], 'rolled-back')
        self.assertEqual(M.digest(self.live / M.PROMPT), REFRESH_OLD)
        self.assertEqual(self.obj.pool_guard()['names'], REFRESH_NAMES)
        self.assertEqual((self.live / 'data/retained.json').read_bytes(), b'latest-after-start')
        self.assertFalse((self.live / 'data/deleted.json').exists())
        self.assertTrue((self.op / 'original-deployment/data/deleted.json').exists())
        with closing(sqlite3.connect(self.live / 'data/astra_quant.db')) as db:
            self.assertEqual(db.execute('SELECT * FROM records ORDER BY id').fetchall(),
                             [(1, 'before'), (2, 'after-start')])
        self.reload().verify(previous=True)

    def test_post_start_failure_automatically_restores_prompt_and_latest_state(self):
        self.prepare_recovery_records()
        original_start = self.obj.start
        def start(previous):
            original_start(previous)
            if not previous:
                self.add_poststart_records()
                raise M.GateError('injected_post_start_failure')
        self.patching(self.obj, 'start', side_effect=start)
        with self.assertRaisesRegex(M.GateError, '^upgrade_failed_previous_runtime_recovered$'):
            self.obj.execute()
        self.assertEqual(self.obj.read('failure.json')['gate'], 'injected_post_start_failure')
        self.assert_latest_recovery()

    def test_verify_failure_automatically_restores_prompt_and_latest_state(self):
        self.prepare_recovery_records()
        original_logs = self.obj.log_check
        injected = []
        def log_check():
            if not injected:
                injected.append(True)
                self.add_poststart_records()
                raise M.GateError('injected_observation_failure')
            return original_logs()
        self.patching(self.obj, 'log_check', side_effect=log_check)
        with self.assertRaisesRegex(M.GateError, '^upgrade_failed_previous_runtime_recovered$'):
            self.obj.execute()
        self.assertEqual(self.obj.read('failure.json')['gate'], 'injected_observation_failure')
        self.assert_latest_recovery()

    def interrupted_rename(self, cut):
        self.obj.preflight()
        pool_before = self.physical_file(self.pool)
        old_prompt = self.physical_file(self.live / M.PROMPT)
        original_move = self.obj.move
        injected = []
        def move(source, target):
            original_move(source, target)
            matched = (source == self.live and target == self.op / 'original-deployment'
                       if cut == 'first' else source == self.op / 'candidate' and target == self.live)
            if matched and not injected:
                injected.append(True)
                raise M.GateError('injected_' + cut + '_rename_interruption')
        self.patching(self.obj, 'move', side_effect=move)
        with self.assertRaisesRegex(M.GateError, '^upgrade_failed_previous_runtime_recovered$'):
            self.obj.execute()
        self.assertEqual(injected, [True])
        self.assertEqual(self.obj.state['phase'], 'rolled-back')
        self.assertEqual(self.physical_file(self.live / M.PROMPT), old_prompt)
        self.assertEqual(self.physical_file(self.pool), pool_before)
        self.reload().verify(previous=True)

    def test_first_cutover_rename_interruption_recovers_original_prompt(self):
        self.interrupted_rename('first')

    def test_second_cutover_rename_interruption_recovers_original_prompt(self):
        self.interrupted_rename('second')


    def test_absent_local_overlay_survives_upgrade_reload_verify_and_rollback(self):
        local = self.live / 'data/prompt_library.local.json'
        local.unlink()
        self.plan['protected_config']['data/prompt_library.local.json'] = None
        self.repin()
        self.obj.manifest['deployment_plan_sha256'] = self.obj.deployment_plan_sha256
        self.pin_manifest()
        pool_before = self.physical_file(self.pool)
        prompt_before = self.physical_file(self.live / M.PROMPT)
        self.activate()
        self.assertFalse(local.exists())
        self.reload().verify()
        self.assertFalse(local.exists())
        self.assertEqual(self.physical_file(self.pool), pool_before)
        self.obj.rollback()
        self.reload().verify(previous=True)
        self.assertFalse(local.exists())
        self.assertEqual(self.physical_file(self.live / M.PROMPT), prompt_before)
        self.assertEqual(self.physical_file(self.pool), pool_before)
        self.assertTrue(all(value['pool'] == REFRESH_NAMES for value in self.native_reads))


if __name__ == '__main__':
    unittest.main()
