"""Portable real SQLite rehearsal; verified previous sources remain external input."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('cycle_rehearsal', Path(__file__).with_name('rehearse_cycle_deadline_upgrade.py'))
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)
SOURCE = Path(__file__).resolve().parents[2]
PREVIOUS = Path(os.environ.get('ASTRA_PREVIOUS_SOURCE', str(SOURCE.parent / 'release-fixtures/previous-90f9f3a')))


class CycleStateRehearsalTests(unittest.TestCase):
    def test_external_action_adapter_fails_closed(self):
        with self.assertRaisesRegex(AssertionError, 'forbidden'):
            rehearsal.denied('synthetic-command')

    def test_missing_previous_entry_point_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(AssertionError, 'missing'):
                rehearsal.rehearse(SOURCE, Path(root))

    @unittest.skipUnless((PREVIOUS / 'astra_gateway/store.py').is_file(), 'verified previous archive not supplied')
    def test_actual_previous_forward_old_writer_latest_rollback(self):
        result = rehearsal.rehearse(SOURCE, PREVIOUS)
        self.assertTrue(all(value == 'passed' for value in result['portable_checks'].values()))
        self.assertEqual(result['external_commands'], 'disabled')
        self.assertEqual(len(result['pending']), 2)

    @unittest.skipUnless((PREVIOUS / 'astra_gateway/store.py').is_file(), 'verified previous archive not supplied')
    def test_source_drift_after_probes_and_freeze_is_still_rejected(self):
        load = rehearsal.load_module
        def inject(path, name):
            module = load(path, name)
            if name == 'deadline_runtime_rehearsal':
                carry = module.Upgrade.carry
                def changed_carry(obj, source, target, prefix):
                    carry(obj, source, target, prefix)
                    (source / 'data/orders.json').write_text('{"synthetic":"changed-during-copy"}', encoding='utf-8')
                module.Upgrade.carry = changed_carry
            return module
        with patch.object(rehearsal, 'load_module', inject):
            with self.assertRaisesRegex(RuntimeError, 'poststart_state_changed_during_recovery'):
                rehearsal.rehearse(SOURCE, PREVIOUS)


if __name__ == '__main__':
    unittest.main()
