"""Actual temporary-file publication checks, with a synthetic slow fsync."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from astra_backend import deadline
from scripts import ai_brain_trader as brain


class BrainAtomicPublishDeadlineTests(unittest.TestCase):
    def test_fsync_expiry_preserves_existing_file_and_cleans_temporary_output(self):
        clock = [100.0]
        real_fsync = brain.os.fsync
        def slow_fsync(fd):
            real_fsync(fd)
            clock[0] += 2
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "synthetic.json"
            target.write_text('{"previous": true}', encoding="utf-8")
            with patch.object(deadline.time, "monotonic", lambda: clock[0]), \
                 patch.object(brain.os, "fsync", slow_fsync), deadline.deadline_scope(timeout=1):
                with self.assertRaises(deadline.DeadlineExceeded):
                    brain.atomic_write_json(str(target), {"late": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"previous": True})
            self.assertEqual([path.name for path in Path(temp).iterdir()], ["synthetic.json"])

    def test_without_deadline_keeps_real_atomic_publication_behavior(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "synthetic.json"
            target.write_text('{"previous": true}', encoding="utf-8")
            brain.atomic_write_json(str(target), {"new": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"new": True})
            self.assertEqual([path.name for path in Path(temp).iterdir()], ["synthetic.json"])

    def test_expired_failure_health_is_durable_without_renewing_decision_budget(self):
        clock = [100.0]
        with tempfile.TemporaryDirectory() as temp, patch.object(brain, "DATA_DIR", temp), \
             patch.object(deadline.time, "monotonic", lambda: clock[0]), deadline.deadline_scope(timeout=1) as boundary:
            clock[0] += 2
            brain._record_cycle_health("failed", "synthetic deadline")
            health = json.loads((Path(temp) / "ai_health.json").read_text(encoding="utf-8"))
            self.assertEqual((health["last_status"], health["last_error"]), ("failed", "synthetic deadline"))
            self.assertEqual(deadline.current_deadline(), boundary)
            with self.assertRaises(deadline.DeadlineExceeded):
                brain.atomic_write_json(str(Path(temp) / "decision.json"), {"late": True})
            self.assertFalse((Path(temp) / "decision.json").exists())

    def test_expired_success_health_cannot_replace_a_previous_failed_audit(self):
        clock = [100.0]
        with tempfile.TemporaryDirectory() as temp, patch.object(brain, "DATA_DIR", temp):
            brain._record_cycle_health("failed", "previous synthetic failure")
            target = Path(temp) / "ai_health.json"
            previous = target.read_bytes()
            with patch.object(deadline.time, "monotonic", lambda: clock[0]), deadline.deadline_scope(timeout=1):
                clock[0] += 2
                brain._record_cycle_health("ok")
            self.assertEqual(target.read_bytes(), previous)
