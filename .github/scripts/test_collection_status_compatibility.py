"""Exact-baseline collection storage checks; temporary SQLite, no live services.

CI supplies independently archived baseline/historical sources. Missing fixtures
are failures, never skips. The original helper gates run during the rollback
rehearsal; no source/schema acceptance function is mocked or bypassed.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(Path(__file__).parent))
import runtime_release_upgrade as upgrade
import rehearse_cycle_deadline_upgrade as rehearsal

SLOT = "2026-10-05T12:00:00+08:00"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def schema(path):
    with closing(sqlite3.connect(path)) as db:
        tables = {}
        for name, declaration in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = {"columns": [list(row[1:]) for row in db.execute("PRAGMA table_info(" + quoted + ")")],
                            "rows": db.execute("SELECT COUNT(*) FROM " + quoted).fetchone()[0], "sql": declaration}
        indexes = [list(row) for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('index','trigger','view') ORDER BY type,name")]
    return {upgrade.GATEWAY_DATABASE: {"tables": tables, "indexes": indexes}}


class CollectionStatusCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = Path(os.environ.get("ASTRA_COLLECTION_BASELINE", str(SOURCE.parent / "baseline")))
        historical_root = Path(os.environ.get("ASTRA_PREVIOUS_SOURCE", str(SOURCE.parent / "release-fixtures/previous-90f9f3a")))
        cls.historical_path = Path(os.environ.get("ASTRA_HISTORICAL_GATEWAY", str(historical_root / "astra_gateway/store.py")))
        cls.baseline_path = cls.baseline / "astra_gateway/store.py"
        cls.current_path = SOURCE / "astra_gateway/store.py"
        for path, expected in (
                (cls.baseline_path, upgrade.COLLECTION_BASELINE_GATEWAY_SHA256),
                (cls.historical_path, upgrade.COLLECTION_HISTORICAL_GATEWAY_SHA256),
                (cls.current_path, upgrade.COLLECTION_GATEWAY_SHA256)):
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise AssertionError("independently verified storage fixture missing or changed: " + str(path))
        cls.old = load(cls.baseline_path, "collection_baseline_gateway")
        cls.historical = load(cls.historical_path, "collection_historical_gateway")
        cls.new = load(cls.current_path, "collection_current_gateway")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astra-collection-storage-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "gateway.db"
        self.prior = self.old.GatewayStore(self.path)
        self.prior.set_state("unrelated-state", "retain-exactly")
        self.legacy_id = self.prior.begin_job("trader", SLOT)
        self.prior.finish_job(self.legacy_id, 0, "legacy process output")

    def finished(self, store, status="failed", code=0):
        run_id = store.begin_job("trader", SLOT)
        result = {"job_run_id": run_id, "scheduled_at": SLOT, "last_status": status,
                  "last_at": "2026-10-05T12:00:01+08:00", "last_error": "synthetic collection deadline"}
        store.finish_job(run_id, code, "inspection output", result)
        return next(row for row in store.job_runs() if row["id"] == run_id)

    def test_exact_baseline_forward_preserves_original_rows(self):
        before = schema(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            rows = {name: db.execute('SELECT * FROM "' + name + '"').fetchall()
                    for name in before[upgrade.GATEWAY_DATABASE]["tables"]}
        current = self.new.GatewayStore(self.path)
        after = schema(self.path)
        upgrade.compatible_databases(before, after)
        old_table = before[upgrade.GATEWAY_DATABASE]["tables"]["job_runs"]
        new_table = after[upgrade.GATEWAY_DATABASE]["tables"]["job_runs"]
        self.assertEqual(new_table["columns"], old_table["columns"] + upgrade.COLLECTION_JOB_COLUMNS)
        with closing(sqlite3.connect(self.path)) as db:
            for name, values in rows.items():
                columns = before[upgrade.GATEWAY_DATABASE]["tables"][name]["columns"]
                names = ",".join('"' + column[0] + '"' for column in columns)
                self.assertEqual(db.execute('SELECT ' + names + ' FROM "' + name + '"').fetchall(), values)
        row = current.job_runs()[0]
        self.assertEqual((row["id"], row["detail"], row["process_status"], row["brain_status"]),
                         (self.legacy_id, "legacy process output", "success", "unknown"))

    def test_historical_forward_uses_only_exact_reviewed_additions(self):
        path = self.root / "historical.db"
        old = self.historical.GatewayStore(path)
        run = old.begin_job("trader")
        old.finish_job(run, 0, "historical")
        before = schema(path)
        current = self.new.GatewayStore(path)
        upgrade.compatible_databases(before, schema(path))
        self.assertEqual(current.job_runs()[0]["brain_status"], "unknown")
        self.assertEqual(current.job_runs()[0]["detail"], "historical")

    def test_initialization_is_idempotent(self):
        current = self.new.GatewayStore(self.path)
        outcome = self.finished(current)
        before = schema(self.path)
        for _ in range(3):
            self.assertEqual(self.new.GatewayStore(self.path).job_runs()[0], outcome)
            self.assertEqual(schema(self.path), before)

    def test_concurrent_initializers_serialize_exact_additions(self):
        before = schema(self.path)
        barrier = threading.Barrier(8)
        def initialize(_):
            barrier.wait(timeout=10)
            return self.new.GatewayStore(self.path).job_runs()[0]["id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(initialize, range(8))), [self.legacy_id] * 8)
        after = schema(self.path)
        upgrade.compatible_databases(before, after)
        names = [column[0] for column in after[upgrade.GATEWAY_DATABASE]["tables"]["job_runs"]["columns"]]
        self.assertEqual(len(names), len(set(names)))

    def test_old_reader_writer_preserves_new_terminal_brain_evidence(self):
        current = self.new.GatewayStore(self.path)
        outcome = self.finished(current)
        old = self.old.GatewayStore(self.path)
        self.assertEqual(old.job_runs()[0], outcome)
        run = old.begin_job("trader", "2026-10-05T12:15:00+08:00")
        old.finish_job(run, 0, "old writer output")
        old.set_state("old-writer", "latest")
        latest = current.job_runs()[0]
        self.assertEqual((latest["process_status"], latest["brain_status"]), ("success", "unknown"))
        self.assertEqual(current.job_runs()[1], outcome)
        self.assertEqual(current.get_state("unrelated-state"), "retain-exactly")

    def test_old_finisher_does_not_leave_new_run_projected_running(self):
        current = self.new.GatewayStore(self.path)
        for code in (0, 7):
            with self.subTest(code=code):
                run = current.begin_job("trader", SLOT)
                self.prior.finish_job(run, code, "previous version finished")
                row = current.job_runs()[0]
                self.assertEqual(row["process_status"], "success" if code == 0 else "failed")
                self.assertEqual(row["brain_status"], "unknown")

    def test_old_recovery_does_not_leave_new_run_projected_pending(self):
        current = self.new.GatewayStore(self.path)
        current.begin_job("trader", SLOT)
        self.prior.recover_stale_job_runs()
        row = current.job_runs()[0]
        self.assertEqual((row["status"], row["process_status"], row["brain_status"]),
                         ("interrupted", "interrupted", "unknown"))

    def test_old_process_update_preserves_confirmed_brain_result(self):
        current = self.new.GatewayStore(self.path)
        outcome = self.finished(current, "ok")
        self.prior.finish_job(outcome["id"], 7, "process failed after brain success")
        row = current.job_runs()[0]
        self.assertEqual((row["process_status"], row["brain_status"]), ("failed", "ok"))
        self.assertEqual(row["brain_result_at"], outcome["brain_result_at"])
        self.assertEqual(row["brain_detail"], outcome["brain_detail"])

    def test_fresh_and_upgraded_layouts_are_stable_not_rebuilt(self):
        migrated = self.new.GatewayStore(self.path)
        fresh_path = self.root / "fresh.db"
        fresh = self.new.GatewayStore(fresh_path)
        migrated_columns = schema(self.path)[upgrade.GATEWAY_DATABASE]["tables"]["job_runs"]["columns"]
        fresh_before = schema(fresh_path)
        fresh_columns = fresh_before[upgrade.GATEWAY_DATABASE]["tables"]["job_runs"]["columns"]
        self.assertNotEqual(migrated_columns, fresh_columns)
        self.assertEqual(sorted(migrated_columns), sorted(fresh_columns))
        self.finished(fresh)
        self.new.GatewayStore(fresh_path)
        upgrade.compatible_databases(fresh_before, schema(fresh_path))
        self.assertEqual(migrated.job_runs()[0]["id"], self.legacy_id)

    def test_source_contract_accepts_only_fixed_collection_versions(self):
        for previous in (self.baseline_path, self.historical_path, self.current_path):
            upgrade.gateway_source_compatibility(previous, self.current_path)

    def test_source_contract_rejects_unreviewed_old_version(self):
        altered = self.root / "old.py"
        altered.write_bytes(self.baseline_path.read_bytes() + b"\n# unreviewed source\n")
        with self.assertRaises(upgrade.GateError):
            upgrade.gateway_source_compatibility(altered, self.current_path)

    def test_source_contract_rejects_changed_schema_sql_or_other_code(self):
        original = self.current_path.read_text(encoding="utf-8")
        changes = (original.replace("brain_detail TEXT NOT NULL DEFAULT ''", "brain_detail TEXT"),
                   original.replace("SELECT * FROM job_runs WHERE id=?", "SELECT * FROM job_runs"),
                   original + "\n# additional unreviewed code\n")
        altered = self.root / "new.py"
        for content in changes:
            with self.subTest(content=hashlib.sha256(content.encode()).hexdigest()):
                self.assertNotEqual(content, original)
                altered.write_text(content, encoding="utf-8")
                with self.assertRaises(upgrade.GateError):
                    upgrade.gateway_source_compatibility(self.baseline_path, altered)

    def test_unknown_status_migrations_fail_closed(self):
        before = schema(self.path)
        self.new.GatewayStore(self.path)
        approved = schema(self.path)
        for mutation in ("extra", "partial", "type", "nullability", "default", "order", "sql", "constraint"):
            after = copy.deepcopy(approved)
            table = after[upgrade.GATEWAY_DATABASE]["tables"]["job_runs"]
            columns = table["columns"]
            if mutation == "extra": columns.append(["unknown", "TEXT", 1, "''", 0])
            elif mutation == "partial": columns.pop()
            elif mutation == "type": columns[-1][1] = "BLOB"
            elif mutation == "nullability": columns[-1][2] = 0
            elif mutation == "default": columns[-1][3] = "'unknown'"
            elif mutation == "order": columns[-1], columns[-2] = columns[-2], columns[-1]
            elif mutation == "sql": table["sql"] += " /* changed */"
            else: table["sql"] = table["sql"].replace("AUTOINCREMENT", "")
            with self.subTest(mutation=mutation), self.assertRaises(upgrade.GateError):
                upgrade.compatible_databases(before, after)

    def test_status_columns_cannot_upgrade_unknown_prior_schema(self):
        before = schema(self.path)
        self.new.GatewayStore(self.path)
        after = schema(self.path)
        for state in (before, after):
            table = state[upgrade.GATEWAY_DATABASE]["tables"]["job_runs"]
            table["columns"][1][2] = 0
            table["sql"] = table["sql"].replace("job_name TEXT NOT NULL", "job_name TEXT")
        with self.assertRaisesRegex(upgrade.GateError, "unknown_database_migration"):
            upgrade.compatible_databases(before, after)

    def test_status_columns_cannot_be_added_to_another_database(self):
        before = schema(self.path)
        self.new.GatewayStore(self.path)
        after = schema(self.path)
        for state in (before, after):
            state["data/astra_admin.db"] = state.pop(upgrade.GATEWAY_DATABASE)
        with self.assertRaisesRegex(upgrade.GateError, "unknown_database_migration"):
            upgrade.compatible_databases(before, after)

    def test_new_columns_cannot_be_removed_or_reordered_on_rollback(self):
        before = schema(self.path)
        self.new.GatewayStore(self.path)
        upgraded = schema(self.path)
        with self.assertRaises(upgrade.GateError):
            upgrade.compatible_databases(upgraded, before)
        changed = copy.deepcopy(upgraded)
        changed[upgrade.GATEWAY_DATABASE]["tables"]["job_runs"]["columns"].reverse()
        with self.assertRaises(upgrade.GateError):
            upgrade.compatible_databases(upgraded, changed)

    def test_actual_helper_rollback_carries_latest_brain_results(self):
        original_load = rehearsal.load_module
        evidence = []
        audited = []
        def instrument(path, name):
            module = original_load(path, name)
            if name == "deadline_new_gateway":
                finish = module.GatewayStore.finish_job
                def finish_with_result(store, run_id, code, detail, brain_result=None):
                    finish(store, run_id, code, detail, brain_result)
                    with store.connect() as db:
                        job = db.execute("SELECT job_name FROM job_runs WHERE id=?", (run_id,)).fetchone()[0]
                    if job == "synthetic-candidate":
                        for status in ("ok", "failed", "skipped"):
                            extra = store.begin_job("trader", SLOT)
                            finish(store, extra, 0, "latest " + status,
                                   {"job_run_id": extra, "scheduled_at": SLOT, "last_status": status,
                                    "last_at": SLOT, "last_error": "retained " + status})
                            evidence.append(next(row for row in store.job_runs() if row["id"] == extra))
                module.GatewayStore.finish_job = finish_with_result
            elif name == "deadline_runtime_rehearsal":
                rollback = module.Upgrade.rollback
                def audited_rollback(operator):
                    rollback(operator)
                    path = module.ROOT / "data/astra_gateway.db"
                    old = self.old.GatewayStore(path)
                    by_id = {row["id"]: row for row in old.job_runs(200)}
                    for expected in evidence:
                        self.assertEqual(by_id[expected["id"]], expected)
                    reopened = self.new.GatewayStore(path)
                    by_id = {row["id"]: row for row in reopened.job_runs(200)}
                    for expected in evidence:
                        self.assertEqual(by_id[expected["id"]], expected)
                    self.assertEqual(old.get_state("old-writer"), "latest")
                    audited.append(True)
                module.Upgrade.rollback = audited_rollback
            return module
        with patch.object(rehearsal, "load_module", instrument):
            result = rehearsal.rehearse(SOURCE, self.baseline)
        self.assertEqual(len(evidence), 3)
        self.assertEqual(audited, [True])
        self.assertTrue(all(value == "passed" for value in result["portable_checks"].values()))
        self.assertEqual(result["external_commands"], "disabled")


if __name__ == "__main__":
    unittest.main()
