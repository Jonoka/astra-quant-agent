"""Crash real recovery renames/journal writes, reload disk, then verify retries."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import runtime_release_upgrade as M
import test_deployment_identity as identity


class SimulatedProcessLoss(RuntimeError):
    pass


class RecoveryJournalTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        base = identity.DeploymentIdentityTests('test_accepted_owned_candidate_rolls_back_latest_records_and_deletions')
        base.setUp()
        try:
            (base.live / 'logs').mkdir()
            (base.live / 'logs/natural.log').write_bytes(b'latest-natural-round')
            base.obj.save('state.json', base.obj.state)
            yield base
        finally:
            base.doCleanups()

    def reload(self, base):
        obj = M.Upgrade.__new__(M.Upgrade)
        obj.__dict__.update(base.obj.__dict__)
        obj.state = json.loads((base.op / 'state.json').read_bytes())
        obj.start = Mock()
        obj.verify = Mock()
        base.obj = obj

    def crash(self, base, where):
        obj = base.obj
        actual_phase, actual_move = obj.phase, obj.move
        def phase(value):
            mapping = {'recovery-ready': 'ready', 'candidate-retained': 'retained',
                       'recovery-active': 'active'}
            name = mapping.get(value)
            if name and where == name + '-before':
                raise SimulatedProcessLoss(where)
            actual_phase(value)
            if name and where == name + '-after':
                raise SimulatedProcessLoss(where)
        def move(source, target):
            name = ('first' if source == base.live and target == base.op / 'failed-candidate'
                    else 'second' if source == base.op / 'recovery' and target == base.live else None)
            if name and where == name + '-before':
                raise SimulatedProcessLoss(where)
            actual_move(source, target)
            if name and where == name + '-after':
                raise SimulatedProcessLoss(where)
        actual_replace = M.os.replace
        def replace(source, target):
            selected = Path(target).name == 'state.json' and obj.state.get('phase') == 'recovery-active'
            if selected and where == 'replace-before':
                raise SimulatedProcessLoss(where)
            actual_replace(source, target)
            if selected and where == 'replace-after':
                raise SimulatedProcessLoss(where)
        with patch.object(obj, 'phase', side_effect=phase), patch.object(obj, 'move', side_effect=move), \
                patch.object(M.os, 'replace', side_effect=replace), self.assertRaises(SimulatedProcessLoss):
            obj.rollback()
        obj.start.assert_not_called()
        self.reload(base)

    def retry(self, where):
        with self.fixture() as base:
            self.crash(base, where)
            calls = base.docker.call_count
            base.obj.rollback()
            self.assertEqual(base.obj.state['phase'], 'rolled-back')
            self.assertEqual(json.loads((base.op / 'state.json').read_bytes())['phase'], 'rolled-back')
            self.assertEqual((base.live / 'source-marker').read_bytes(), b'previous')
            self.assertEqual((base.live / 'data/records.json').read_bytes(), b'latest')
            self.assertFalse((base.live / 'data/deleted.json').exists())
            self.assertEqual((base.live / 'logs/natural.log').read_bytes(), b'latest-natural-round')
            self.assertEqual(M.tree_manifest(base.original), base.obj.state['snapshot'])
            self.assertEqual(M.tree_manifest(base.op / 'stopped-snapshot'), base.obj.state['snapshot'])
            self.assertEqual(base.docker.call_count, calls, 'Retry must not stop again')
            base.obj.start.assert_called_once_with(True)

    def rejected(self, base, gate):
        root = M.tree_manifest(base.live)
        disk = (base.op / 'state.json').read_bytes()
        calls = base.docker.call_count
        with self.assertRaisesRegex(M.GateError, gate):
            base.obj.rollback()
        self.assertEqual(M.tree_manifest(base.live), root)
        self.assertEqual((base.op / 'state.json').read_bytes(), disk)
        self.assertEqual(base.docker.call_count, calls)
        base.obj.start.assert_not_called()

    def test_crash_before_ready_journal_without_trusted_manifest_fails_closed(self):
        with self.fixture() as base:
            self.crash(base, 'ready-before')
            self.assertNotIn('recovery_manifest', base.obj.state)
            self.rejected(base, 'partial_recovery_requires_review')

    def test_crash_after_ready_journal_resumes_before_first_rename(self):
        self.retry('ready-after')

    def test_crash_before_first_rename_resumes_verified_ready_tree(self):
        self.retry('first-before')

    def test_crash_after_first_rename_before_retained_journal_resumes(self):
        self.retry('first-after')

    def test_crash_before_retained_journal_resumes_with_root_absent(self):
        self.retry('retained-before')

    def test_crash_after_retained_journal_resumes_with_root_absent(self):
        self.retry('retained-after')

    def test_crash_before_second_rename_resumes_verified_recovery(self):
        self.retry('second-before')

    def test_crash_after_second_rename_before_active_journal_resumes(self):
        self.retry('second-after')

    def test_crash_before_active_journal_reconciles_complete_restored_tree(self):
        self.retry('active-before')

    def test_crash_after_active_journal_starts_without_recopying(self):
        self.retry('active-after')

    def test_crash_before_atomic_journal_replace_uses_durable_old_record(self):
        self.retry('replace-before')

    def test_crash_after_atomic_journal_replace_uses_durable_new_record(self):
        self.retry('replace-after')

    def test_changed_latest_data_is_not_reapproved_after_second_rename(self):
        with self.fixture() as base:
            self.crash(base, 'second-after')
            (base.live / 'data/records.json').write_bytes(b'unapproved-new-data')
            self.rejected(base, 'recovery_tree_drift')

    def test_external_image_after_second_rename_is_rejected_before_journal_or_start(self):
        with self.fixture() as base:
            self.crash(base, 'second-after')
            base.current[M.NAMES[0]].update(ref='foreign-image', image='foreign-id')
            self.rejected(base, 'foreign_deployment_image')

    def test_retained_candidate_source_must_still_belong_to_operation(self):
        with self.fixture() as base:
            self.crash(base, 'second-after')
            (base.op / 'failed-candidate/source-marker').write_bytes(b'foreign-source')
            self.rejected(base, 'foreign_failed_source')

    def test_missing_full_recovery_hash_does_not_allow_stage_promotion(self):
        with self.fixture() as base:
            self.crash(base, 'second-after')
            del base.obj.state['recovery_manifest']
            base.obj.save('state.json', base.obj.state)
            self.rejected(base, 'recovery_tree_drift')

    def test_running_owned_candidate_after_second_rename_is_not_silently_restarted(self):
        with self.fixture() as base:
            self.crash(base, 'second-after')
            base.current[M.NAMES[0]]['state'] = 'running'
            self.rejected(base, 'candidate_container_still_active')

    def test_pre_first_rename_requires_complete_frozen_input_manifest(self):
        with self.fixture() as base:
            self.crash(base, 'first-before')
            del base.obj.state['recovery_input_manifest']
            base.obj.save('state.json', base.obj.state)
            self.rejected(base, 'recovery_input_drift')

    def test_changed_recovered_permissions_are_rejected_by_full_tree(self):
        with self.fixture() as base:
            self.crash(base, 'second-after')
            (base.live / '.env').chmod(0o640)
            self.rejected(base, 'recovery_tree_drift')

    def test_changed_stopped_snapshot_blocks_reconciliation(self):
        with self.fixture() as base:
            self.crash(base, 'second-after')
            (base.op / 'stopped-snapshot/data/records.json').write_bytes(b'changed-evidence')
            self.rejected(base, 'snapshot_evidence_drift')


if __name__ == '__main__':
    unittest.main()
