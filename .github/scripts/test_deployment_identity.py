"""Real Linux rollback/rename control flow with synthetic Docker inventories."""
from contextlib import closing
import copy
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import runtime_release_upgrade as M


class DeploymentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='astra-identity-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.live = self.root / 'live'
        (self.live / 'data').mkdir(parents=True)
        self.op = self.root / 'operation'
        self.op.mkdir()
        self.original = self.op / 'original-deployment'
        (self.live / '.env').write_bytes(b'ASTRA_STANDALONE_GATEWAY=true\nASTRA_OKX_ENV=demo\nOKX_IS_SIMULATED=1\n')
        (self.live / '.env').chmod(0o600)
        with closing(sqlite3.connect(self.live / 'data/astra_admin.db')) as db:
            db.executescript("CREATE TABLE admin_users(id,username,password_hash,salt,iterations,role,enabled,created_at); INSERT INTO admin_users VALUES(1,'synthetic','hash','salt',1,'superadmin',1,'today')")
        (self.live / 'source-marker').write_bytes(b'previous')
        (self.live / 'data/records.json').write_bytes(b'old')
        (self.live / 'data/deleted.json').write_bytes(b'old')
        shutil.copytree(self.live, self.original)
        shutil.copytree(self.original, self.op / 'stopped-snapshot')
        (self.live / 'source-marker').write_bytes(b'candidate')
        (self.live / 'data/records.json').write_bytes(b'latest')
        (self.live / 'data/deleted.json').unlink()
        self.obj = M.Upgrade.__new__(M.Upgrade)
        self.obj.op = self.op
        self.obj.previous_image = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'b' * 64
        self.obj.image = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'c' * 64
        self.obj.manifest_sha = 'synthetic'
        self.old = self.inventory(previous=True)
        self.current = self.inventory()
        self.obj.state = {'phase': 'accepted', 'manifest_sha': 'synthetic', 'extras': [],
            'prompt_refreshed': False, 'snapshot': M.tree_manifest(self.original),
            'databases': M.db_state(self.original), 'baseline': copy.deepcopy(self.old),
            'candidate_containers': {n: {'id': c['id']} for n, c in self.current.items()},
            'deployment_sources': {'previous': M.deployment_source(self.original),
                                   'release': M.deployment_source(self.live)}}
        self.obj.sources = Mock()
        self.obj.image_metadata = Mock(side_effect=lambda previous=False: 'previous-id' if previous else 'candidate-id')
        self.obj.capacity = Mock()
        self.obj.pool_guard = Mock()
        self.obj.start = Mock()
        self.obj.verify = Mock()
        # This suite tests actual filesystem/container ownership and rename
        # recovery. Only the separate protocol barrier is synthetic here.
        self.obj.deployment_plan = Mock(return_value={'schema': 2, 'maintenance': {'mode': 'normal'}})
        coordinator = Mock()
        coordinator.startup_environment.return_value = {'ASTRA_MAINTENANCE_ENABLED': '1'}
        self.obj.maintenance = Mock(return_value=coordinator)
        version_patch = patch('maintenance_deployment.check_protocol_versions')
        version_patch.start()
        self.addCleanup(version_patch.stop)
        self.docker = Mock()
        def natural_exit(*, recovery=False):
            self.obj.deployment_guard()
            self.docker('protocol-natural-exit', *(c['id'] for c in self.current.values()))
            for c in self.current.values():
                c['state'] = 'exited'
        coordinator.orderly_stop.side_effect = natural_exit
        actual_run = M.run
        def command(*args, **kwargs):
            if args[0] != 'docker':
                return actual_run(*args, **kwargs)
            self.docker(*args, **kwargs)
            raise AssertionError('schema2 lifecycle must never signal Docker stop/kill')
        for patcher in (patch.object(M, 'ROOT', self.live),
                        patch.object(M, 'containers', side_effect=lambda: copy.deepcopy(self.current)),
                        patch.object(M, 'run', side_effect=command)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def inventory(self, previous=False):
        return {n: {'id': ('old-' if previous else 'new-') + n, 'project': M.PROJECT,
            'service': n.removeprefix('astraquant-'),
            'ref': self.obj.previous_image if previous else self.obj.image,
            'image': 'previous-id' if previous else 'candidate-id', 'state': 'running'} for n in M.NAMES}

    def refuse(self, gate):
        before = M.tree_manifest(self.live) if self.live.exists() else None
        with self.assertRaisesRegex(M.GateError, gate):
            self.obj.rollback()
        self.docker.assert_not_called()
        self.obj.start.assert_not_called()
        self.assertEqual(M.tree_manifest(self.live) if self.live.exists() else None, before)
        self.assertTrue(list(self.op.glob('rollback-identity-rejection-*.json')))

    def early_previous(self):
        # Delete only this test's temporary original; no live/real workspace.
        shutil.rmtree(self.original)
        (self.live / 'source-marker').write_bytes(b'previous')
        self.obj.state['phase'] = 'stopping'
        self.current = copy.deepcopy(self.old)

    def test_old_operation_refuses_foreign_image_before_stop_or_rename(self):
        for c in self.current.values():
            c.update(ref='ghcr.io/jonoka/astra-quant-agent@sha256:' + 'd' * 64, image='foreign-id')
        self.refuse('foreign_deployment_image')

    def test_foreign_source_with_expected_image_is_refused_before_stop(self):
        (self.live / 'source-marker').write_bytes(b'foreign-release')
        self.refuse('foreign_deployment_source')

    def test_same_source_image_with_new_container_ids_is_not_old_operation(self):
        for c in self.current.values():
            c['id'] = 'later-' + c['id']
        self.refuse('foreign_deployment_container')

    def test_reserved_container_name_with_other_project_is_refused(self):
        self.current[M.NAMES[0]]['project'] = 'other'
        self.refuse('deployment_container_scope')

    def test_extra_project_service_is_refused(self):
        self.current['unexpected-service'] = dict(self.current[M.NAMES[0]])
        self.refuse('deployment_container_scope')

    def test_exact_reference_with_wrong_image_id_is_refused(self):
        self.current[M.NAMES[0]]['image'] = 'foreign-config-id'
        self.refuse('foreign_deployment_image')

    def test_source_drift_after_initial_guard_is_rechecked_by_actual_stop(self):
        self.obj.capacity.side_effect = lambda: (self.live / 'source-marker').write_bytes(b'foreign-release')
        with self.assertRaisesRegex(M.GateError, 'foreign_deployment_source'):
            self.obj.rollback()
        self.docker.assert_not_called()
        self.obj.start.assert_not_called()
        self.assertEqual((self.live / 'source-marker').read_bytes(), b'foreign-release')

    def test_foreign_image_appearing_after_stop_is_refused_before_rename(self):
        actual_phase = self.obj.phase
        def phase(value):
            actual_phase(value)
            if value == 'recovery-ready':
                self.current[M.NAMES[0]].update(ref='foreign', image='foreign', id='foreign')
        self.obj.phase = phase
        with self.assertRaisesRegex(M.GateError, 'foreign_deployment_image'):
            self.obj.rollback()
        self.assertEqual(self.docker.call_count, 1)
        self.assertFalse((self.op / 'failed-candidate').exists())
        self.obj.start.assert_not_called()
        self.assertEqual((self.live / 'data/records.json').read_bytes(), b'latest')

    def test_accepted_owned_candidate_rolls_back_latest_records_and_deletions(self):
        self.obj.state['phase'] = 'accepted-paused'
        self.obj.rollback()
        self.assertEqual((self.live / 'source-marker').read_bytes(), b'previous')
        self.assertEqual((self.live / 'data/records.json').read_bytes(), b'latest')
        self.assertFalse((self.live / 'data/deleted.json').exists())
        self.assertEqual(M.tree_manifest(self.original), self.obj.state['snapshot'])
        self.obj.start.assert_called_once_with(True)
        self.assertEqual(self.obj.state['phase'], 'rolled-back-paused')

    def test_unhealthy_and_exited_owned_candidate_remains_recoverable(self):
        self.obj.state['phase'] = 'starting'
        self.current[M.NAMES[0]]['health'] = 'unhealthy'
        self.current[M.NAMES[1]]['state'] = 'exited'
        self.obj.rollback()
        self.obj.start.assert_called_once_with(True)

    def test_partial_start_with_candidate_and_stopped_previous_is_recoverable(self):
        self.obj.state['phase'] = 'starting'
        self.current[M.NAMES[1]] = dict(self.old[M.NAMES[1]], state='exited')
        self.obj.rollback()
        self.obj.start.assert_called_once_with(True)

    def test_early_stop_failure_accepts_only_owned_previous(self):
        self.early_previous()
        self.current[M.NAMES[1]]['state'] = 'exited'
        self.obj.rollback()
        self.docker.assert_not_called()
        self.obj.start.assert_called_once_with(True)

    def test_missing_root_after_first_rename_recovers_owned_previous(self):
        self.live.rename(self.op / 'candidate')
        self.obj.state['phase'] = 'root-moved'
        self.current = {n: dict(c, state='exited') for n, c in self.old.items()}
        self.obj.rollback()
        self.assertEqual((self.live / 'source-marker').read_bytes(), b'previous')
        self.obj.start.assert_called_once_with(True)

    def test_missing_root_foreign_container_is_refused_before_restore(self):
        self.live.rename(self.op / 'candidate')
        self.obj.state['phase'] = 'root-moved'
        self.current[M.NAMES[0]].update(ref='foreign', image='foreign')
        self.refuse('foreign_deployment_image')
        self.assertTrue(self.original.exists())

    def prepare_recovery_retry(self, missing=False):
        self.live.rename(self.op / 'failed-candidate')
        shutil.copytree(self.original, self.op / 'recovery')
        (self.op / 'recovery/data/records.json').write_bytes(b'latest')
        (self.op / 'recovery/data/deleted.json').unlink()
        self.obj.state['recovery_manifest'] = M.tree_manifest(self.op / 'recovery')
        self.obj.state['phase'] = 'candidate-retained'
        self.current = {n: dict(c, state='exited') for n, c in self.current.items()}
        if not missing:
            (self.op / 'recovery').rename(self.live)
            self.obj.state['phase'] = 'recovery-starting'
            self.current = self.inventory(previous=True)
            for c in self.current.values():
                c['id'] = 'recovery-' + c['id']
            self.obj.state['recovery_containers'] = {n: {'id': c['id']} for n, c in self.current.items()}

    def test_retry_between_recovery_renames_uses_verified_latest_recovery(self):
        self.prepare_recovery_retry(missing=True)
        self.obj.rollback()
        self.assertEqual((self.live / 'data/records.json').read_bytes(), b'latest')
        self.assertFalse((self.live / 'data/deleted.json').exists())
        self.obj.start.assert_called_once_with(True)

    def test_recovery_start_retry_accepts_recorded_recreated_previous_ids(self):
        self.prepare_recovery_retry()
        self.obj.rollback()
        self.docker.assert_not_called()
        self.obj.start.assert_called_once_with(True)

    def test_paused_and_resumed_recovery_phases_keep_exact_ownership_gates(self):
        self.prepare_recovery_retry()
        for phase in ('rolled-back-paused', 'rolled-back'):
            self.obj.state['phase'] = phase
            with self.subTest(phase=phase):
                self.obj.deployment_guard()
                before = self.current[M.NAMES[0]]['id']
                self.current[M.NAMES[0]]['id'] = 'external-replacement'
                try:
                    with self.assertRaisesRegex(M.GateError, 'foreign_deployment_container'):
                        self.obj.deployment_guard()
                finally:
                    self.current[M.NAMES[0]]['id'] = before
        self.docker.assert_not_called()

    def test_rename_never_moves_source_under_running_owned_containers(self):
        with self.assertRaisesRegex(M.GateError, 'deployment_rename_running_container'):
            self.obj.move(self.live, self.op / 'failed-candidate')
        self.assertTrue(self.live.exists())
        self.assertFalse((self.op / 'failed-candidate').exists())

    def test_pre_stop_failure_refuses_external_previous_container_ids(self):
        self.early_previous()
        self.current[M.NAMES[0]]['id'] = 'external-previous'
        self.refuse('foreign_deployment_container')

    def test_partial_compose_failure_records_ids_then_allows_owned_recovery(self):
        self.obj.state['phase'] = 'candidate-active'
        self.current = {n: dict(c, state='exited') for n, c in self.old.items()}
        self.obj.log_marks = Mock(return_value={})
        actual_start = M.Upgrade.start.__get__(self.obj, M.Upgrade)
        def compose(source, *args):
            if args[0] == 'up':
                self.current[M.NAMES[0]] = self.inventory()[M.NAMES[0]]
                raise M.GateError('synthetic_partial_start')
            return b''
        self.obj.compose = Mock(side_effect=compose)
        with self.assertRaisesRegex(M.GateError, 'synthetic_partial_start'):
            actual_start(False)
        self.assertEqual(self.obj.state['candidate_containers'], {M.NAMES[0]: {'id': self.current[M.NAMES[0]]['id']}})
        self.obj.rollback()
        self.obj.start.assert_called_once_with(True)


if __name__ == '__main__':
    unittest.main()
