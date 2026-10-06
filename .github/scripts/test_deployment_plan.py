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

    def test_idle_guard_rejects_wal_or_short_schedule_window(self):
        self.database()
        with patch.object(M.time, 'time', return_value=2600), self.assertRaisesRegex(M.GateError, 'insufficient_idle_window'):
            self.obj.idle_window()
        Path(str(self.live / M.GATEWAY_DATABASE) + '-wal').write_bytes(b'pending')
        with patch.object(M.time, 'time', return_value=1800), self.assertRaisesRegex(M.GateError, 'scheduler_snapshot_unstable'):
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


if __name__ == '__main__':
    unittest.main()
