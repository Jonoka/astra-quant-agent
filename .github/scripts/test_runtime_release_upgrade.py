"""Synthetic safety gates; execute on GitHub Actions, never the VPS."""
import copy
from contextlib import closing, contextmanager
import importlib.util
import itertools
import os
import sqlite3
import sys
import io
import tarfile
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("runtime_upgrade", Path(__file__).with_name("runtime_release_upgrade.py"))
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)
sys.path.insert(0, str(Path(__file__).parent))
import stage_release_bundle as staging


LEGACY_REFERENCE_SHA = '90f9f3a558bdbea0171b19a42c58e2fae7ed8e9d'
LEGACY_REFERENCE_IMAGE = 'ghcr.io/jonoka/astra-quant-agent@sha256:8b471e834dbfe633d720dc5d0ad0c4249e922dce91689719c46ca0fb6575b43b'


class RuntimeUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def operator(self):
        obj = upgrade.Upgrade.__new__(upgrade.Upgrade)
        obj.op = self.root / "operation"
        obj.op.mkdir()
        obj.state = {"extras": []}
        obj.previous = LEGACY_REFERENCE_SHA
        obj.previous_image = LEGACY_REFERENCE_IMAGE
        # These fixtures exercise the original unrelated lifecycle gates. The
        # pinned-plan/native-pool guards have their own real filesystem suite.
        obj.pool_guard = Mock()
        obj.deployment_guard = Mock()
        obj.remember_started_containers = Mock()
        return obj

    def schema_fixture(self, *, additive=False):
        with sqlite3.connect(':memory:') as db:
            db.executescript("CREATE TABLE job_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, job_name TEXT NOT NULL);"
                             "CREATE TABLE model_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL);")
            if additive:
                db.execute(upgrade.JOB_SCHEDULED_ALTER)
                db.executescript(upgrade.DEADLINE_SCHEMA_ADDITION)
            return self.schema_state(db)

    @staticmethod
    def schema_state(db):
        tables = {}
        for name, declaration in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"):
            tables[name] = {'columns': [list(row[1:]) for row in db.execute('PRAGMA table_info("' + name + '")')],
                            'rows': db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0], 'sql': declaration}
        indexes = [list(row) for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('index','trigger','view') ORDER BY type,name")]
        return {upgrade.GATEWAY_DATABASE: {'tables': tables, 'indexes': indexes}}

    def test_only_exact_additive_deadline_schema_is_accepted_and_idempotent(self):
        before, after = self.schema_fixture(), self.schema_fixture(additive=True)
        upgrade.compatible_databases(before, after)
        upgrade.compatible_databases(after, copy.deepcopy(after))

    def test_deadline_gate_rejects_modified_scheduled_column_or_old_table_sql(self):
        before, after = self.schema_fixture(), self.schema_fixture(additive=True)
        for mutation in ('default', 'old_sql', 'order'):
            changed = copy.deepcopy(after)
            table = changed[upgrade.GATEWAY_DATABASE]['tables']['job_runs']
            if mutation == 'default':
                table['columns'][-1][3] = "'unknown'"
            elif mutation == 'old_sql':
                table['sql'] = table['sql'].replace('job_name TEXT NOT NULL', 'job_name TEXT')
            else:
                table['columns'].reverse()
            with self.subTest(mutation=mutation), self.assertRaises(upgrade.GateError):
                upgrade.compatible_databases(before, changed)

    def test_request_unique_constraint_indexes_and_unknown_objects_are_fail_closed(self):
        before, after = self.schema_fixture(), self.schema_fixture(additive=True)
        for mutation in ('unique', 'extra_index', 'missing_index', 'table_sql', 'extra_table'):
            changed = copy.deepcopy(after)
            gateway = changed[upgrade.GATEWAY_DATABASE]
            if mutation == 'unique':
                gateway['indexes'] = [row for row in gateway['indexes'] if not row[1].startswith('sqlite_autoindex_model_requests')]
            elif mutation == 'extra_index':
                gateway['indexes'].append(['index', 'unreviewed', 'model_requests', 'CREATE INDEX unreviewed ON model_requests(status)'])
            elif mutation == 'missing_index':
                gateway['indexes'] = [row for row in gateway['indexes'] if row[1] != 'idx_model_requests_job']
            elif mutation == 'table_sql':
                gateway['tables']['model_requests']['sql'] += ' /* unknown constraint */'
            else:
                gateway['tables']['unreviewed'] = {'columns': [], 'rows': 0}
            with self.subTest(mutation=mutation), self.assertRaises(upgrade.GateError):
                upgrade.compatible_databases(before, changed)

    def test_model_request_rows_cannot_be_dropped_on_rollback(self):
        before = self.schema_fixture(additive=True)
        before[upgrade.GATEWAY_DATABASE]['tables']['model_requests']['rows'] = 2
        after = copy.deepcopy(before)
        after[upgrade.GATEWAY_DATABASE]['tables']['model_requests']['rows'] = 1
        with self.assertRaisesRegex(upgrade.GateError, 'model_request_rows_lost'):
            upgrade.compatible_databases(before, after)

    def test_deadline_additions_cannot_be_added_to_other_database(self):
        before, after = self.schema_fixture(), self.schema_fixture(additive=True)
        before['data/astra_admin.db'] = before.pop(upgrade.GATEWAY_DATABASE)
        after['data/astra_admin.db'] = after.pop(upgrade.GATEWAY_DATABASE)
        with self.assertRaisesRegex(upgrade.GateError, 'unknown_database_migration'):
            upgrade.compatible_databases(before, after)

    def test_source_ast_rejects_extra_migration_query_or_rewritten_old_ensure(self):
        old, new = self.root / 'before.py', self.root / 'after.py'
        schema = 'CREATE TABLE model_calls(id INTEGER);\n'
        prefix = 'SCHEMA = ' + repr(schema) + '\nMIGRATION_COLUMNS = ()\n'
        body = 'class GatewayStore:\n    @staticmethod\n    def _ensure_columns(connection):\n        connection.execute("PRAGMA table_info(model_calls)")\n'
        old.write_text(prefix + body, encoding='utf-8')
        known = '\n'.join('        ' + line if line else '' for line in upgrade.APPROVED_ENSURE_ADDITION.splitlines()) + '\n'
        candidate = 'SCHEMA = ' + repr(schema + upgrade.DEADLINE_SCHEMA_ADDITION) + '\nMIGRATION_COLUMNS = ()\n' + body + known
        new.write_text(candidate, encoding='utf-8')
        upgrade.gateway_source_compatibility(old, new)
        for altered in (candidate + '\nconnection.execute("DROP TABLE model_calls")\n',
                        candidate + '\nconnection.execute("SELECT * FROM unreviewed")\n',
                        candidate.replace('TEXT NOT NULL DEFAULT \'\'\")', 'TEXT DEFAULT NULL\")'),
                        candidate.replace('PRAGMA table_info(model_calls)', 'PRAGMA table_info(events)')):
            new.write_text(altered, encoding='utf-8')
            with self.assertRaises(upgrade.GateError):
                upgrade.gateway_source_compatibility(old, new)

    def test_same_release_gateway_schema_must_remain_exact(self):
        before = {'data/astra_gateway.db': {'tables': {'model_calls': {
            'columns': [['id', 'INTEGER', 0, None, 1]], 'rows': 1}}, 'indexes': []}}
        after = copy.deepcopy(before)
        upgrade.compatible_databases(before, after)
        columns = after['data/astra_gateway.db']['tables']['model_calls']['columns']
        columns.append(['reasoning_tokens', 'INTEGER', 0, '0', 0])
        with self.assertRaisesRegex(upgrade.GateError, 'unknown_database_migration'):
            upgrade.compatible_databases(before, after)

    def test_collection_status_additions_require_exact_prior_table(self):
        # Matching four column names cannot authorize migration of an unrelated
        # or altered job_runs table. Its original full contract is also pinned.
        before = self.schema_fixture(additive=True)
        after = copy.deepcopy(before)
        after[upgrade.GATEWAY_DATABASE]['tables']['job_runs']['columns'].extend(
            copy.deepcopy(upgrade.COLLECTION_JOB_COLUMNS))
        with self.assertRaisesRegex(upgrade.GateError, 'unknown_database_migration'):
            upgrade.compatible_databases(before, after)

    def test_same_collection_source_requires_fixed_reviewed_bytes(self):
        source = self.root / 'unreviewed.py'
        source.write_text('SCHEMA = ' + repr('process_status TEXT;\n' + upgrade.DEADLINE_SCHEMA_ADDITION)
                          + '\nMIGRATION_COLUMNS = ()\n', encoding='utf-8')
        with self.assertRaisesRegex(upgrade.GateError, 'collection_gateway_source_changed'):
            upgrade.gateway_source_compatibility(source, source)

    def test_source_contract_rejects_shadowed_literal_and_migration_method(self):
        old, new = self.root / 'old.py', self.root / 'new.py'
        prefix = 'SCHEMA = ""\nMIGRATION_COLUMNS: tuple = ()\n'
        body = 'class GatewayStore:\n    def _ensure_columns(connection):\n        pass\n'
        known = '\n'.join('        ' + line if line else '' for line in upgrade.APPROVED_ENSURE_ADDITION.splitlines()) + '\n'
        old.write_text(prefix + body, encoding='utf-8')
        candidate = 'SCHEMA = ' + repr(upgrade.DEADLINE_SCHEMA_ADDITION) + '\nMIGRATION_COLUMNS: tuple = ()\n' + body + known
        new.write_text(candidate, encoding='utf-8')
        upgrade.gateway_source_compatibility(old, new)
        for addition in ('SCHEMA = ""', 'SCHEMA: str = ""', 'SCHEMA += ""', '(SCHEMA := "")',
                         'def SCHEMA(): pass', 'async def SCHEMA(): pass', 'class SCHEMA: pass',
                         'import replacement as SCHEMA', 'from replacement import SCHEMA',
                         'from replacement import *', 'del SCHEMA',
                         'try: pass\nexcept Exception as SCHEMA: pass',
                         'match "synthetic":\n    case SCHEMA: pass',
                         'def replacement(SCHEMA): pass',
                         'MIGRATION_COLUMNS = ()', 'MIGRATION_COLUMNS: tuple = ()',
                         'MIGRATION_COLUMNS += ()', '(MIGRATION_COLUMNS := ())',
                         'class MIGRATION_COLUMNS: pass', 'import replacement as MIGRATION_COLUMNS',
                         'class _ensure_columns: pass', 'import replacement as _ensure_columns',
                         'del GatewayStore._ensure_columns',
                         'def _ensure_columns(connection):\n    pass',
                         'GatewayStore._ensure_columns = lambda connection: None'):
            with self.subTest(addition=addition):
                new.write_text(candidate + '\n' + addition + '\n', encoding='utf-8')
                with self.assertRaises(upgrade.GateError):
                    upgrade.gateway_source_compatibility(old, new)

    def test_real_wal_database_probes_close_every_connection(self):
        data = self.root / 'data'
        data.mkdir()
        db = sqlite3.connect(data / 'astra_admin.db')
        try:
            db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE admin_users(id INTEGER,username TEXT,password_hash TEXT,"
                             "salt TEXT,iterations INTEGER,role TEXT,enabled INTEGER,created_at TEXT);"
                             "INSERT INTO admin_users VALUES(1,'synthetic','hash','salt',1,'superadmin',1,'synthetic');")
        finally:
            db.close()
        connections = []
        connect = sqlite3.connect
        def track(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch.object(upgrade.sqlite3, 'connect', track):
            upgrade.db_state(self.root)
            upgrade.admin_identity(self.root)
        self.assertEqual(len(connections), 2)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    def test_database_report_does_not_mix_concurrent_wal_commits(self):
        data = self.root / 'data'
        data.mkdir()
        path = data / 'astra_admin.db'
        connect = sqlite3.connect
        with closing(connect(path)) as writer:
            writer.executescript('PRAGMA journal_mode=WAL; CREATE TABLE records(id INTEGER);'
                                 'INSERT INTO records VALUES(1);')
            changed = []
            class ConcurrentCommit(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    result = super().execute(sql, *args, **kwargs)
                    if sql == 'PRAGMA integrity_check' and not changed:
                        writer.executescript('INSERT INTO records VALUES(2);'
                                             'CREATE TABLE later_commit(id INTEGER);')
                        changed.append(True)
                    return result
            with patch.object(upgrade.sqlite3, 'connect',
                              side_effect=lambda *a, **kw: connect(*a, factory=ConcurrentCommit, **kw)):
                report = upgrade.db_state(self.root)['data/astra_admin.db']
            self.assertEqual(report['tables']['records']['rows'], 1)
            self.assertNotIn('later_commit', report['tables'])
            self.assertEqual(writer.execute('SELECT COUNT(*) FROM records').fetchone()[0], 2)
            self.assertEqual(changed, [True])

    def wal_gateway(self, status='finished'):
        """Keep a real backend connection open, with schema in the main file."""
        path = self.root / upgrade.GATEWAY_DATABASE
        path.parent.mkdir(exist_ok=True)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.executescript('PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; '
                         'CREATE TABLE job_runs(id INTEGER PRIMARY KEY, status TEXT, detail BLOB);')
        db.execute('INSERT INTO job_runs VALUES(1,?,?)', (status, b'original\x00\xff'))
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        self.assertEqual(Path(str(path) + '-wal').stat().st_size, 0)
        self.assertTrue(Path(str(path) + '-shm').is_file())
        return path, db

    @staticmethod
    def sqlite_payload(path):
        # SHM is intentionally excluded: normal SQLite reader coordination can
        # change it even when both SQL payload files are strictly read-only.
        result = {}
        for candidate in (path, Path(str(path) + '-wal')):
            info = candidate.stat()
            result[candidate.name] = (candidate.read_bytes(), info.st_ino,
                                      info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return result

    @contextmanager
    def idle_fixture(self, obj, *, clock=1800, processes=()):
        obj.deployment_plan = Mock(return_value={'runtime': {
            'schedule_seconds': 900, 'minimum_idle_window_seconds': 480}})
        original_iterdir = Path.iterdir
        def iterdir(path):
            return iter(processes) if path == Path('/proc') else original_iterdir(path)
        with patch.object(upgrade, 'ROOT', self.root), \
                patch.object(upgrade.time, 'time', return_value=clock) as wall_clock, \
                patch.object(Path, 'iterdir', iterdir):
            yield wall_clock

    def test_idle_window_rejects_running_commit_only_present_in_wal(self):
        obj = self.operator()
        path, backend = self.wal_gateway()
        backend.execute("UPDATE job_runs SET status='running' WHERE id=1")
        backend.commit()
        with closing(sqlite3.connect(path.as_uri() + '?immutable=1', uri=True)) as main_only:
            self.assertEqual(main_only.execute('SELECT status FROM job_runs').fetchone(), ('finished',))
        before = self.sqlite_payload(path)
        with self.idle_fixture(obj), self.assertRaisesRegex(upgrade.GateError, 'active_scheduled_job'):
            obj.idle_window()
        self.assertEqual(self.sqlite_payload(path), before)
        obj.pool_guard.assert_not_called()

    def test_idle_window_accepts_finished_commit_only_present_in_wal(self):
        obj = self.operator()
        path, backend = self.wal_gateway(status='running')
        backend.execute("UPDATE job_runs SET status='finished', detail=? WHERE id=1", (b'latest\x00\xff',))
        backend.commit()
        with closing(sqlite3.connect(path.as_uri() + '?immutable=1', uri=True)) as main_only:
            self.assertEqual(main_only.execute('SELECT status FROM job_runs').fetchone(), ('running',))
        before = self.sqlite_payload(path)
        with self.idle_fixture(obj):
            obj.idle_window()
        self.assertEqual(self.sqlite_payload(path), before)
        obj.pool_guard.assert_called_once_with()

    def test_idle_window_accepts_open_backend_zero_byte_wal_and_shm(self):
        obj = self.operator()
        path, backend = self.wal_gateway()
        before = self.sqlite_payload(path)
        with self.idle_fixture(obj):
            obj.idle_window()
        self.assertEqual(self.sqlite_payload(path), before)
        self.assertEqual(backend.execute('SELECT COUNT(*) FROM job_runs').fetchone(), (1,))
        obj.pool_guard.assert_called_once_with()

    def test_sqlite_snapshot_excludes_uncommitted_writes_and_is_query_only(self):
        path, backend = self.wal_gateway()
        backend.execute("UPDATE job_runs SET status='running' WHERE id=1")
        before = self.sqlite_payload(path)
        with upgrade.sqlite_snapshot(path) as snapshot:
            self.assertEqual(snapshot.execute('SELECT status,detail FROM job_runs').fetchall(),
                             [('finished', b'original\x00\xff')])
            self.assertEqual(snapshot.execute('PRAGMA query_only').fetchone(), (1,))
            with self.assertRaises(sqlite3.OperationalError):
                snapshot.execute("UPDATE job_runs SET status='changed'")
        self.assertEqual(self.sqlite_payload(path), before)
        self.assertTrue(backend.in_transaction)
        backend.rollback()

    def test_sqlite_snapshot_closes_readonly_source_and_memory_copy_on_every_exit(self):
        path, _ = self.wal_gateway()
        connect = sqlite3.connect
        for consumer_fails in (False, True):
            connections, calls = [], []
            def track(*args, **kwargs):
                calls.append((args, kwargs))
                connection = connect(*args, **kwargs)
                connections.append(connection)
                return connection
            with self.subTest(consumer_fails=consumer_fails), patch.object(upgrade.sqlite3, 'connect', track):
                try:
                    with upgrade.sqlite_snapshot(path) as snapshot:
                        self.assertEqual(snapshot.execute('SELECT COUNT(*) FROM job_runs').fetchone(), (1,))
                        if consumer_fails:
                            raise RuntimeError('synthetic consumer failure')
                except RuntimeError:
                    self.assertTrue(consumer_fails)
            self.assertEqual(len(connections), 2)
            self.assertTrue(calls[0][1]['uri'])
            self.assertIn('mode=ro', calls[0][0][0])
            self.assertNotIn('immutable', calls[0][0][0])
            self.assertEqual(calls[1][0][0], ':memory:')
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')

    def test_sqlite_snapshot_busy_progress_is_deadline_bounded_and_closes_connections(self):
        path, _ = self.wal_gateway()
        connect, connections, statuses = sqlite3.connect, [], []
        class BusyConnection(sqlite3.Connection):
            def backup(self, target, *, pages, progress, sleep):
                self_test.assertGreater(pages, 0)
                self_test.assertLessEqual(sleep, 0.1)
                for status in itertools.islice(itertools.cycle((sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)), 100):
                    statuses.append(status)
                    progress(status, 1, 1)
                self_test.fail('backup progress did not enforce its deadline')
        self_test = self
        def track(*args, **kwargs):
            if args[0] != ':memory:':
                kwargs['factory'] = BusyConnection
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection
        before = self.sqlite_payload(path)
        with patch.object(upgrade.sqlite3, 'connect', track), \
                patch.object(upgrade.time, 'monotonic', side_effect=itertools.count(step=0.1)), \
                self.assertRaisesRegex(upgrade.GateError, 'sqlite_snapshot_timeout'):
            with upgrade.sqlite_snapshot(path, timeout_seconds=0.5):
                self.fail('busy backup yielded a snapshot')
        self.assertIn(sqlite3.SQLITE_BUSY, statuses)
        self.assertIn(sqlite3.SQLITE_LOCKED, statuses)
        self.assertLess(len(statuses), 10)
        self.assertEqual(self.sqlite_payload(path), before)
        self.assertEqual(len(connections), 2)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    def test_sqlite_snapshot_rejects_corrupt_missing_and_symlink_inputs(self):
        valid = self.root / 'valid.db'
        with closing(sqlite3.connect(valid)) as db:
            db.execute('CREATE TABLE sample(id INTEGER)')
        corrupt = self.root / 'corrupt.db'
        corrupt.write_bytes(b'SQLite format 3\x00' + b'not a database' * 100)
        link = self.root / 'alias.db'
        link.symlink_to(valid)
        directory = self.root / 'directory.db'
        directory.mkdir()
        for path in (corrupt, self.root / 'missing.db', link, directory):
            with self.subTest(path=path.name), self.assertRaises(upgrade.GateError):
                with upgrade.sqlite_snapshot(path):
                    self.fail('unsafe input yielded a snapshot')
        self.assertFalse((self.root / 'missing.db').exists())
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(valid) + suffix)
            sidecar.symlink_to(corrupt)
            with self.subTest(suffix=suffix), self.assertRaisesRegex(upgrade.GateError, 'symlink_path'):
                with upgrade.sqlite_snapshot(valid):
                    self.fail('symlink sidecar yielded a snapshot')
            sidecar.unlink()

    def test_sqlite_snapshot_rejects_unrecognized_wal_instead_of_ignoring_it(self):
        path = self.root / 'malformed-wal.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE sample(id INTEGER)')
        wal = Path(str(path) + '-wal')
        wal.write_bytes(b'unrecognized nonempty WAL payload')
        before = self.sqlite_payload(path)
        with self.assertRaisesRegex(upgrade.GateError, 'sqlite_snapshot_invalid'):
            with upgrade.sqlite_snapshot(path):
                self.fail('unrecognized WAL was silently ignored')
        self.assertEqual(self.sqlite_payload(path), before)

    def test_sqlite_snapshot_detects_main_inode_replacement(self):
        path = self.root / 'replace.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE sample(id INTEGER)')
        replacement = self.root / 'replacement.db'
        replacement.write_bytes(path.read_bytes())
        connect = sqlite3.connect
        class ReplacingConnection(sqlite3.Connection):
            def backup(self, *args, **kwargs):
                super().backup(*args, **kwargs)
                os.replace(replacement, path)
        def track(*args, **kwargs):
            if args[0] != ':memory:':
                kwargs['factory'] = ReplacingConnection
            return connect(*args, **kwargs)
        with patch.object(upgrade.sqlite3, 'connect', track), \
                self.assertRaisesRegex(upgrade.GateError, 'sqlite_snapshot_identity_changed'):
            with upgrade.sqlite_snapshot(path):
                self.fail('replacement database accepted')

    def test_idle_window_preserves_process_and_configuration_gates(self):
        obj = self.operator()
        self.wal_gateway()
        process = self.root / '12345'
        process.mkdir()
        for executable in ('ai_factor_trader.py', 'ai_brain_trader.py', 'trading_brain.py', 'self_evolution.py'):
            (process / 'cmdline').write_bytes(b'python\x00/app/' + executable.encode() + b'\x00')
            with self.subTest(executable=executable), self.idle_fixture(obj, processes=(process,)), \
                    self.assertRaisesRegex(upgrade.GateError, 'active_trader_or_brain'):
                obj.idle_window()
        obj.pool_guard.assert_not_called()
        obj.pool_guard.side_effect = upgrade.GateError('pool_config_drift')
        with self.idle_fixture(obj), self.assertRaisesRegex(upgrade.GateError, 'pool_config_drift'):
            obj.idle_window()

    def test_idle_window_enforces_480_seconds_before_and_after_snapshot(self):
        obj = self.operator()
        self.wal_gateway()
        with self.idle_fixture(obj, clock=2220):  # Exactly 480 seconds remain.
            obj.idle_window()
        obj.pool_guard.reset_mock()
        with self.idle_fixture(obj, clock=2221), \
                patch.object(upgrade, 'sqlite_snapshot') as snapshot, \
                self.assertRaisesRegex(upgrade.GateError, 'insufficient_idle_window'):
            obj.idle_window()
        snapshot.assert_not_called()
        obj.pool_guard.assert_not_called()
        with self.idle_fixture(obj) as wall_clock:
            wall_clock.side_effect = [1800, 2221]
            with self.assertRaisesRegex(upgrade.GateError, 'insufficient_idle_window'):
                obj.idle_window()

    def test_corrupt_scheduler_database_aborts_before_stopped_copy(self):
        obj = self.operator()
        path = self.root / upgrade.GATEWAY_DATABASE
        path.parent.mkdir()
        path.write_bytes(b'SQLite format 3\x00' + b'corrupt' * 100)
        obj.state.update(phase='prepared', baseline={}, endpoints={})
        for method in ('check_drift', 'capacity', 'runtime', 'validate_compose', 'image_metadata'):
            setattr(obj, method, Mock())
        obj.endpoints = Mock(return_value={})
        obj.stop = Mock()
        obj.copy = Mock()
        obj.rollback = Mock()
        with self.idle_fixture(obj), self.assertRaisesRegex(upgrade.GateError, 'sqlite_snapshot_invalid'):
            obj.execute()
        obj.stop.assert_not_called()
        obj.copy.assert_not_called()
        obj.rollback.assert_not_called()
        self.assertFalse((obj.op / 'stopped-snapshot').exists())

    def test_gateway_source_contract_rejects_other_sql_or_migrations(self):
        old, new = self.root / 'old.py', self.root / 'new.py'
        schema = 'CREATE TABLE model_calls (\n  output_tokens INTEGER,\n  total_tokens INTEGER\n);'
        migrations = (('cached_tokens', 'INTEGER'),)
        old.write_text('SCHEMA = ' + repr(schema) + '\nMIGRATION_COLUMNS = ' + repr(migrations))
        new.write_text('SCHEMA = ' + repr(schema + upgrade.DEADLINE_SCHEMA_ADDITION) + '\nMIGRATION_COLUMNS = ' + repr(migrations))
        upgrade.gateway_source_compatibility(old, new)
        new.write_text('SCHEMA = ' + repr(schema + '\n-- changed') + '\nMIGRATION_COLUMNS = ' + repr(migrations))
        with self.assertRaisesRegex(upgrade.GateError, 'gateway_schema_changed'):
            upgrade.gateway_source_compatibility(old, new)

    def test_stage_identity_uses_corrected_fork_sha_and_pinned_ancestor(self):
        evidence = {'SOURCE_SHA': 'a' * 40, 'GITHUB_SHA': 'a' * 40, 'PREVIOUS_SHA': LEGACY_REFERENCE_SHA,
                    'UPSTREAM_SHA': upgrade.UPSTREAM, 'SOURCE_REPOSITORY': 'Jonoka/astra-quant-agent',
                    'UPSTREAM_REPOSITORY': '0xethanq/astra-quant-agent', 'SOURCE_VERSION': 'v8.6.1',
                    'platform': 'linux/amd64'}
        self.assertEqual(staging.source_identity(evidence), 'a' * 40)
        for key, bad in (('SOURCE_SHA', upgrade.UPSTREAM), ('GITHUB_SHA', 'b' * 40),
                         ('UPSTREAM_SHA', 'b' * 40),
                         ('SOURCE_REPOSITORY', '0xethanq/astra-quant-agent')):
            changed = {**evidence, key: bad}
            with self.assertRaisesRegex(Exception, 'source_identity'):
                staging.source_identity(changed)

    def test_candidate_image_requires_corrected_fork_upstream_patch_and_recipe(self):
        obj = self.operator()
        obj.image = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'a' * 64
        obj.release = 'b' * 40
        (obj.op / 'Dockerfile.release').write_bytes(b'reviewed-recipe')
        labels = {'org.opencontainers.image.revision': obj.release,
                  'org.opencontainers.image.version': 'v8.6.1',
                  'org.opencontainers.image.source': 'https://github.com/Jonoka/astra-quant-agent',
                  'io.jonoka.astra.upstream-revision': upgrade.UPSTREAM,
                  'io.jonoka.astra.council-completion-patch': upgrade.PATCH_ID,
                  'io.jonoka.astra.okx-public-domains-patch': upgrade.OKX_PATCH_ID,
                  'io.jonoka.astra.cycle-deadline-patch': upgrade.DEADLINE_PATCH_ID,
                  'io.jonoka.astra.build-recipe-sha256': upgrade.sha(b'reviewed-recipe')}
        metadata = {'Config': {'Labels': labels}, 'RepoDigests': [obj.image],
                    'Os': 'linux', 'Architecture': 'amd64', 'Id': 'image-id'}
        with patch.object(upgrade, 'run', return_value=upgrade.json.dumps([metadata]).encode()):
            self.assertEqual(obj.image_metadata(), 'image-id')
        for key in ('io.jonoka.astra.upstream-revision', 'io.jonoka.astra.council-completion-patch',
                    'io.jonoka.astra.okx-public-domains-patch', 'io.jonoka.astra.cycle-deadline-patch',
                    'io.jonoka.astra.build-recipe-sha256'):
            changed = copy.deepcopy(metadata)
            changed['Config']['Labels'][key] = 'incorrect'
            with patch.object(upgrade, 'run', return_value=upgrade.json.dumps([changed]).encode()), \
                    self.assertRaisesRegex(upgrade.GateError, 'image_patch_provenance'):
                obj.image_metadata()

    def test_previous_image_metadata_is_deployed_fork_same_release(self):
        obj = self.operator()
        obj.image = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'b' * 64
        obj.release = 'c' * 40
        labels = {
            'org.opencontainers.image.revision': LEGACY_REFERENCE_SHA,
            'org.opencontainers.image.version': 'v8.6.1',
            'org.opencontainers.image.source': 'https://github.com/Jonoka/astra-quant-agent',
        }
        metadata = {'Config': {'Labels': labels}, 'RepoDigests': [LEGACY_REFERENCE_IMAGE],
                    'Os': 'linux', 'Architecture': 'amd64', 'Id': 'old-image-id'}
        with patch.object(upgrade, 'run', return_value=upgrade.json.dumps([metadata]).encode()):
            self.assertEqual(obj.image_metadata(previous=True), 'old-image-id')

    def test_stage_validates_archive_pin_and_traversal_before_writing(self):
        archive = self.root / 'source.tar'
        def bundle(name, pin):
            with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT,
                              pax_headers={'comment': pin}) as tar:
                item = tarfile.TarInfo(name)
                item.size = 4
                tar.addfile(item, io.BytesIO(b'data'))
                link = tarfile.TarInfo(staging.LINK)
                link.type = tarfile.SYMTYPE
                link.linkname = '../../docs/images'
                tar.addfile(link)
        bundle('../escape', 'a' * 40)
        destination = self.root / 'extracted'
        with self.assertRaisesRegex(Exception, 'archive_path'):
            staging.extract(archive, destination, 'a' * 40)
        self.assertFalse(destination.exists())
        bundle('docs/images/fixture', 'b' * 40)
        with self.assertRaisesRegex(Exception, 'git_archive_source_pin'):
            staging.extract(archive, destination, 'a' * 40)
        bundle('docs/images/fixture', 'a' * 40)
        manifest = staging.extract(archive, destination, 'a' * 40)
        self.assertEqual(manifest['docs/images/fixture'], upgrade.sha(b'data'))

    def test_official_symlink_preserved_without_traversal(self):
        (self.root / "frontend/public").mkdir(parents=True)
        (self.root / "docs/images").mkdir(parents=True)
        (self.root / "docs/images/asset").write_bytes(b"image")
        link = self.root / "frontend/public/images"
        link.symlink_to("../../docs/images", target_is_directory=True)
        manifest = upgrade.tree_manifest(self.root)
        self.assertEqual(manifest["frontend/public/images"]["symlink"], "../../docs/images")
        self.assertNotIn("frontend/public/images/asset", manifest)
        self.assertEqual(upgrade.tree_manifest(self.root / "frontend")["public/images"]["symlink"], "../../docs/images")

    def test_unknown_and_escaping_links_rejected(self):
        link = self.root / "link"
        link.symlink_to("/etc/passwd")
        with self.assertRaisesRegex(upgrade.GateError, "tree_symlink_not_allowed"):
            upgrade.tree_manifest(self.root)

    def test_official_link_cannot_escape_through_docs_alias(self):
        (self.root / "frontend/public").mkdir(parents=True)
        (self.root / "docs").symlink_to(self.root.parent, target_is_directory=True)
        (self.root / "frontend/public/images").symlink_to("../../docs/images", target_is_directory=True)
        with self.assertRaises(upgrade.GateError):
            upgrade.tree_manifest(self.root)

    def test_special_file_rejected(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaisesRegex(upgrade.GateError, "tree_special_file"):
            upgrade.tree_manifest(self.root)

    def test_relative_paths_reject_alias_and_traversal(self):
        for value in ("../data", "/data", "data/../env", "data\\env", "./data", "data//env", ""):
            with self.subTest(value=value), self.assertRaises(upgrade.GateError):
                upgrade.relative_path(value)

    def test_source_checkout_crlf_proof_is_exact_and_never_normalizes_inputs(self):
        raw = '绛栫暐\nsecond\n'.encode()
        crlf = raw.replace(b'\n', b'\r\n')
        self.assertEqual(upgrade.source_representation(raw, raw), 'raw')
        self.assertEqual(upgrade.source_representation(crlf, raw), 'proven_utf8_crlf')
        for changed, reference in ((crlf + b'edit', raw), (raw.replace(b'\n', b'\r\n', 1), raw),
                                   (b'a\x00\r\n', b'a\x00\n'), (b'\xff\r\n', b'\xff\n'),
                                   (b'a\r\r\n', b'a\r\n')):
            with self.subTest(changed=changed), self.assertRaises(upgrade.GateError):
                upgrade.source_representation(changed, reference)
        self.assertEqual(crlf, raw.replace(b'\n', b'\r\n'))

    def test_equal_schema_rejects_new_gateway_telemetry_and_cache_tables(self):
        before = {'data/astra_gateway.db': {'tables': {'model_calls': {
            'columns': [['id', 'INTEGER', 0, None, 1]], 'rows': 1}}, 'indexes': []}}
        after = copy.deepcopy(before)
        after['data/astra_gateway.db']['tables']['model_calls']['columns'].append(
            ['cached_tokens', 'INTEGER', 0, None, 0])
        with self.assertRaisesRegex(upgrade.GateError, 'unknown_database_migration'):
            upgrade.compatible_databases(before, after)
        after = copy.deepcopy(before)
        after['data/astra_gateway.db']['tables']['llm_query_cache'] = {'columns': [], 'rows': 0}
        with self.assertRaisesRegex(upgrade.GateError, 'unknown_database_table'):
            upgrade.compatible_databases(before, after)

    def test_compose_requires_four_unchanged_mounts_and_only_image_change(self):
        obj = self.operator()
        obj.image = 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'a' * 64
        old = {'services': {s: {'image': LEGACY_REFERENCE_IMAGE, 'volumes': [
            {'target': '/app/' + n, 'source': str(upgrade.ROOT / n), 'type': 'bind'}
            for n in ('.env', 'data', 'logs', 'backups')]} for s in upgrade.SERVICES}}
        new = copy.deepcopy(old)
        for service in upgrade.SERVICES:
            new['services'][service]['image'] = obj.image
        def invoke(candidate):
            with patch.object(obj, 'compose', side_effect=[b'', b'',
                    upgrade.json.dumps(old).encode(), upgrade.json.dumps(candidate).encode()]):
                obj.validate_compose(obj.op / 'candidate')
        invoke(new)
        broken = copy.deepcopy(new)
        broken['services']['backend']['volumes'].pop()
        with self.assertRaisesRegex(upgrade.GateError, 'compose_unexpected_change'):
            invoke(broken)
        old['services']['backend']['volumes'].append(
            {'target': '/app/plugins', 'source': str(upgrade.ROOT / 'plugins'), 'type': 'bind'})
        with self.assertRaisesRegex(upgrade.GateError, 'compose_mount_set'):
            invoke(new)

    def test_override_patch_preserves_all_non_image_bytes(self):
        path = self.root / "override.yml"
        before = ("# retained\r\nservices:\r\n  backend:\r\n    image: '" + LEGACY_REFERENCE_IMAGE +
                  "' # backend\r\n  gateway:\r\n    image: \"" + LEGACY_REFERENCE_IMAGE + "\"\r\n").encode()
        path.write_bytes(before)
        image = "ghcr.io/jonoka/astra-quant-agent@sha256:" + "a" * 64
        upgrade.patch_override(path, LEGACY_REFERENCE_IMAGE, image)
        self.assertEqual(path.read_bytes(), before.replace(LEGACY_REFERENCE_IMAGE.encode(), image.encode()))

    def test_override_extra_scope_rejected_before_write(self):
        path = self.root / "override.yml"
        before = b"services:\n  backend:\n    image: x\n    privileged: true\n"
        path.write_bytes(before)
        with self.assertRaises(upgrade.GateError):
            upgrade.patch_override(path, "x", "y")
        self.assertEqual(path.read_bytes(), before)

    def test_custom_prompt_preserved_and_pristine_prompt_refreshed(self):
        obj = self.operator()
        for name, value in (("source-previous", b"old"), ("source-release", b"new"), ("candidate", b"custom")):
            (obj.op / name / "data").mkdir(parents=True)
            (obj.op / name / upgrade.PROMPT).write_bytes(value)
        obj.prompt_refresh(obj.op / "candidate")
        self.assertEqual((obj.op / "candidate" / upgrade.PROMPT).read_bytes(), b"custom")
        self.assertFalse(obj.state["prompt_refreshed"])
        (obj.op / "candidate" / upgrade.PROMPT).write_bytes(b"old")
        obj.prompt_refresh(obj.op / "candidate")
        self.assertEqual((obj.op / "candidate" / upgrade.PROMPT).read_bytes(), b"new")
        obj.restore_prompt(obj.op / "candidate")
        self.assertEqual((obj.op / "candidate" / upgrade.PROMPT).read_bytes(), b"old")

    def test_restore_prompt_keeps_poststart_edit(self):
        obj = self.operator()
        for name in ("source-previous", "source-release", "recovery"):
            (obj.op / name / "data").mkdir(parents=True)
            (obj.op / name / upgrade.PROMPT).write_bytes(name.encode())
        obj.state["prompt_refreshed"] = True
        obj.restore_prompt(obj.op / "recovery")
        self.assertEqual((obj.op / "recovery" / upgrade.PROMPT).read_bytes(), b"recovery")

    def test_recovery_carry_uses_latest_records_and_preserves_deletions(self):
        obj = self.operator()
        latest, recovery = obj.op / "latest", obj.op / "recovery"
        for root in (latest, recovery):
            (root / "data").mkdir(parents=True)
        (latest / "data/orders.json").write_bytes(b"new-order")
        (recovery / "data/orders.json").write_bytes(b"stale-order")
        (recovery / ".env").write_bytes(b"old-secret")
        obj.carry(latest, recovery, "recovery-seed")
        self.assertEqual((recovery / "data/orders.json").read_bytes(), b"new-order")
        self.assertFalse((recovery / ".env").exists())
        self.assertEqual((obj.op / "recovery-seed-data/orders.json").read_bytes(), b"stale-order")
        self.assertEqual((obj.op / "recovery-seed-.env").read_bytes(), b"old-secret")

    def test_recovery_retains_candidate_sqlite_writes_config_and_overlay_metadata(self):
        obj = self.operator()
        latest, recovery = obj.op / 'latest', obj.op / 'recovery'
        for root in (latest, recovery):
            (root / 'data').mkdir(parents=True)
            with sqlite3.connect(root / 'data/orders.db') as db:
                db.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT)')
                db.execute("INSERT INTO orders VALUES(1,'prior')")
        with sqlite3.connect(latest / 'data/orders.db') as db:
            db.execute("INSERT INTO orders VALUES(2,'candidate')")
        (latest / '.env').write_bytes(b'latest-config\r\n')
        (latest / '.env').chmod(0o600)
        overlay = latest / 'data/prompt_library.local.json'
        overlay.write_bytes(b'latest-custom-overlay')
        overlay.chmod(0o640)
        expected = upgrade.tree_manifest(latest / 'data')
        obj.carry(latest, recovery, 'recovery-seed')
        obj.state['prompt_refreshed'] = True
        obj.restore_prompt(recovery)  # No baseline exists; preserve custom overlay.
        self.assertEqual(upgrade.tree_manifest(recovery / 'data'), expected)
        self.assertEqual((recovery / '.env').read_bytes(), b'latest-config\r\n')
        self.assertEqual((recovery / '.env').stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(recovery / 'data/orders.db') as db:
            self.assertEqual(db.execute('SELECT status FROM orders ORDER BY id').fetchall(),
                             [('prior',), ('candidate',)])

    def stopped_wal(self, path, *, append=False):
        """A terminated backend leaves genuine committed WAL and SHM bytes.

        Normal connection close would checkpoint/remove the sidecars, so use a
        tiny local process and exit after commit without SQLite's close hook.
        """
        script = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA wal_autocheckpoint=0')
if sys.argv[2] == 'create':
    db.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT, payload BLOB)')
    db.execute('INSERT INTO orders VALUES(1,?,?)', ('prior', b'prior\\x00\\xff'))
    db.commit()
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
    db.execute('INSERT INTO orders VALUES(2,?,?)', ('committed-wal', bytes(range(256))))
else:
    db.execute('INSERT INTO orders VALUES(3,?,?)', ('candidate-latest', b'latest\\x00\\xff' * 257))
db.commit()
os._exit(0)
"""
        subprocess.run([sys.executable, '-c', script, str(path), 'append' if append else 'create'],
                       check=True, capture_output=True, timeout=15)
        self.assertGreater(Path(str(path) + '-wal').stat().st_size, 32)
        self.assertGreater(Path(str(path) + '-shm').stat().st_size, 0)

    def rollback_wal_fixture(self):
        obj = self.operator()
        live = obj.op / 'live'
        (live / 'data').mkdir(parents=True)
        (live / 'source-marker').write_bytes(b'previous-source')
        (live / '.env').write_bytes(b'ASTRA_STANDALONE_GATEWAY=true\nASTRA_OKX_ENV=demo\nOKX_IS_SIMULATED=1\n')
        (live / '.env').chmod(0o600)
        (live / 'data/deleted.json').write_bytes(b'intentionally deleted by candidate')
        (live / 'data/retained.json').write_bytes(b'prior record')
        with closing(sqlite3.connect(live / 'data/astra_admin.db')) as db:
            db.executescript('CREATE TABLE admin_users(id INTEGER,username TEXT,password_hash TEXT,'
                             'salt TEXT,iterations INTEGER,role TEXT,enabled INTEGER,created_at TEXT);'
                             "INSERT INTO admin_users VALUES(1,'synthetic','hash','salt',1,'superadmin',1,'synthetic');")
        self.stopped_wal(live / 'data/astra_quant.db')
        baseline = upgrade.db_state(live)
        original = obj.op / 'original-deployment'
        obj.copy(live, original)
        obj.copy(live, obj.op / 'stopped-snapshot')
        snapshot = upgrade.tree_manifest(original)
        self.assertEqual(upgrade.tree_manifest(obj.op / 'stopped-snapshot'), snapshot)
        self.stopped_wal(live / 'data/astra_quant.db', append=True)
        (live / 'data/deleted.json').unlink()
        (live / 'data/retained.json').write_bytes(b'latest record\x00\xff')
        (live / 'data/retained.json').chmod(0o640)
        (live / '.env').write_bytes((live / '.env').read_bytes() + b'# latest configuration\n')
        (live / 'source-marker').write_bytes(b'candidate-source')
        obj.manifest_sha = 'synthetic-manifest'
        obj.state.update(phase='candidate-active', manifest_sha=obj.manifest_sha,
                         snapshot=snapshot, databases=baseline, deployment_sources={
                             'previous': upgrade.deployment_source(original),
                             'release': upgrade.deployment_source(live)})
        # These tests exercise filesystem/SQLite recovery. Host ownership,
        # container identity, capacity, image, and health gates have separate
        # deployment tests and cannot be exercised as root by this fixture.
        for method in ('sources', 'image_metadata', 'capacity', 'stop', 'start', 'verify'):
            setattr(obj, method, Mock())
        return obj, live, original, snapshot

    def test_rollback_carries_latest_committed_wal_rows_and_preserves_original_trio(self):
        obj, live, original, snapshot = self.rollback_wal_fixture()
        quant = live / 'data/astra_quant.db'
        latest_payload = {p.name: p.read_bytes() for p in (quant, Path(str(quant) + '-wal'))}
        with patch.object(upgrade, 'ROOT', live), patch.object(upgrade, 'env_gate'):
            obj.rollback()
        self.assertEqual(obj.state['phase'], 'rolled-back')
        obj.stop.assert_called_once_with()
        obj.start.assert_called_once_with(True)
        obj.verify.assert_called_once_with(previous=True)
        self.assertEqual(upgrade.tree_manifest(original), snapshot)
        self.assertEqual(upgrade.tree_manifest(obj.op / 'stopped-snapshot'), snapshot)
        self.assertEqual(upgrade.tree_manifest(obj.op / 'recovery-seed-data'),
                         upgrade.tree_manifest(original / 'data'))
        for root in (live, obj.op / 'failed-candidate'):
            for name, payload in latest_payload.items():
                self.assertEqual((root / 'data' / name).read_bytes(), payload)
            self.assertEqual((root / 'data/retained.json').read_bytes(), b'latest record\x00\xff')
            self.assertEqual((root / 'data/retained.json').stat().st_mode & 0o777, 0o640)
            self.assertFalse((root / 'data/deleted.json').exists())
            self.assertTrue((root / '.env').read_bytes().endswith(b'# latest configuration\n'))
            self.assertEqual((root / '.env').stat().st_mode & 0o777, 0o600)
        self.assertEqual((live / 'source-marker').read_bytes(), b'previous-source')
        self.assertEqual((obj.op / 'failed-candidate/source-marker').read_bytes(), b'candidate-source')
        with upgrade.sqlite_snapshot(quant) as db:
            self.assertEqual(db.execute('SELECT id,status,payload FROM orders ORDER BY id').fetchall(), [
                (1, 'prior', b'prior\x00\xff'), (2, 'committed-wal', bytes(range(256))),
                (3, 'candidate-latest', b'latest\x00\xff' * 257)])
        self.assertEqual(upgrade.tree_manifest(original), snapshot)
        self.assertEqual(upgrade.tree_manifest(obj.op / 'stopped-snapshot'), snapshot)

    def test_rollback_copy_failure_retains_latest_original_and_owned_wal_evidence(self):
        obj, live, original, snapshot = self.rollback_wal_fixture()
        quant = live / 'data/astra_quant.db'
        latest_payload = self.sqlite_payload(quant)
        run = upgrade.run
        def fail_recovery_data_copy(*args, **kwargs):
            if args[:3] == ('cp', '-a', '--') and args[3] == live / 'data':
                raise upgrade.GateError('synthetic_copy_failure')
            return run(*args, **kwargs)
        with patch.object(upgrade, 'ROOT', live), patch.object(upgrade, 'env_gate'), \
                patch.object(upgrade, 'run', side_effect=fail_recovery_data_copy), \
                self.assertRaisesRegex(upgrade.GateError, 'synthetic_copy_failure'):
            obj.rollback()
        self.assertEqual(self.sqlite_payload(quant), latest_payload)
        self.assertEqual(upgrade.tree_manifest(original), snapshot)
        self.assertEqual(upgrade.tree_manifest(obj.op / 'stopped-snapshot'), snapshot)
        self.assertEqual(upgrade.tree_manifest(obj.op / 'recovery-seed-data'),
                         upgrade.tree_manifest(original / 'data'))
        self.assertEqual((live / 'source-marker').read_bytes(), b'candidate-source')
        self.assertTrue((obj.op / 'recovery').is_dir())
        self.assertFalse((obj.op / 'failed-candidate').exists())
        obj.start.assert_not_called()
        obj.verify.assert_not_called()

    def execute_wal_fixture(self):
        obj = self.operator()
        live, candidate = obj.op / 'live', obj.op / 'candidate'
        for root, marker in ((live, b'previous-source'), (candidate, b'candidate-source')):
            (root / 'data').mkdir(parents=True)
            (root / 'source-marker').write_bytes(marker)
        with closing(sqlite3.connect(live / 'data/astra_admin.db')) as db:
            db.executescript('CREATE TABLE admin_users(id,username,password_hash,salt,iterations,role,enabled,created_at);'
                             "INSERT INTO admin_users VALUES(1,'synthetic','hash','salt',1,'superadmin',1,'synthetic');")
        self.stopped_wal(live / 'data/astra_quant.db')
        obj.state.update(phase='prepared', baseline={}, endpoints={}, guard=obj.guard(live))
        # Host/image/ownership and admission are separate boundaries. Keep the
        # actual post-stop DB probes, full copies, carry, renames and recovery.
        for method in ('check_drift', 'capacity', 'runtime', 'validate_compose', 'image_metadata',
                       'idle_window', 'prompt_refresh', 'sources', 'start', 'verify'):
            setattr(obj, method, Mock())
        obj.endpoints = Mock(return_value={})
        obj.stop = Mock(side_effect=lambda: self.stopped_wal(live / 'data/astra_quant.db', append=True))
        obj.manifest_sha = 'synthetic'
        obj.state['manifest_sha'] = obj.manifest_sha
        return obj, live

    def test_execute_freezes_final_wal_commit_written_during_stop(self):
        obj, live = self.execute_wal_fixture()
        with patch.object(upgrade, 'ROOT', live):
            obj.execute()
            probe = obj.op / 'disposable-original-probe'
            obj.copy(obj.op / 'original-deployment', probe)
        self.assertEqual(obj.state['phase'], 'accepted')
        self.assertEqual(obj.state['databases']['data/astra_quant.db']['tables']['orders']['rows'], 3)
        obj.stop.assert_called_once_with()
        obj.start.assert_called_once_with(False)
        self.assertEqual((live / 'source-marker').read_bytes(), b'candidate-source')
        for root in (obj.op / 'original-deployment', obj.op / 'stopped-snapshot'):
            self.assertEqual(upgrade.tree_manifest(root), obj.state['snapshot'])
        for root in (live, probe):
            with upgrade.sqlite_snapshot(root / 'data/astra_quant.db') as db:
                self.assertEqual(db.execute('SELECT id,status FROM orders ORDER BY id').fetchall(),
                                 [(1, 'prior'), (2, 'committed-wal'), (3, 'candidate-latest')])
        self.assertEqual(upgrade.tree_manifest(obj.op / 'original-deployment'), obj.state['snapshot'])
        self.assertEqual(upgrade.tree_manifest(obj.op / 'stopped-snapshot'), obj.state['snapshot'])

    def test_execute_failed_stopped_copy_restores_old_runtime_with_final_wal_commit(self):
        obj, live = self.execute_wal_fixture()
        run = upgrade.run
        def fail_stopped_copy(*args, **kwargs):
            if args[:3] == ('cp', '-a', '--') and args[-1] == obj.op / 'stopped-snapshot':
                raise upgrade.GateError('synthetic_stopped_copy_failure')
            return run(*args, **kwargs)
        with patch.object(upgrade, 'ROOT', live), patch.object(upgrade, 'run', side_effect=fail_stopped_copy), \
                self.assertRaisesRegex(upgrade.GateError, 'upgrade_failed_previous_runtime_recovered'):
            obj.execute()
        self.assertEqual(obj.state['phase'], 'rolled-back')
        obj.start.assert_called_once_with(True)
        obj.verify.assert_called_once_with(previous=True)
        self.assertEqual((live / 'source-marker').read_bytes(), b'previous-source')
        self.assertFalse((obj.op / 'original-deployment').exists())
        with upgrade.sqlite_snapshot(live / 'data/astra_quant.db') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 3)

    def test_database_rejects_lost_trading_rows_and_unknown_columns(self):
        before = {"data/astra_quant.db": {"tables": {"orders": {"columns": [["id", "INTEGER", 0, None, 1]], "rows": 2}}, "indexes": []}}
        after = copy.deepcopy(before)
        after["data/astra_quant.db"]["tables"]["orders"]["rows"] = 3
        upgrade.compatible_databases(before, after)
        after["data/astra_quant.db"]["tables"]["orders"]["rows"] = 1
        with self.assertRaisesRegex(upgrade.GateError, "trading_rows_lost"):
            upgrade.compatible_databases(before, after)
        after = copy.deepcopy(before)
        after["data/astra_quant.db"]["tables"]["orders"]["columns"].append(["unknown", "TEXT", 0, None, 0])
        with self.assertRaisesRegex(upgrade.GateError, "unknown_database_migration"):
            upgrade.compatible_databases(before, after)

    def test_lock_is_nonblocking_and_persistent(self):
        path = self.root / "lock"
        with upgrade.upgrade_lock(path):
            inode = path.stat().st_ino
            with self.assertRaisesRegex(upgrade.GateError, "upgrade_lock_busy"):
                with upgrade.upgrade_lock(path):
                    self.fail("second lock acquired")
        self.assertEqual(path.stat().st_ino, inode)

    def test_start_names_only_target_services_and_no_build(self):
        obj = self.operator()
        obj.log_marks = lambda: {}
        obj.phase = lambda phase: None
        with patch.object(obj, "compose") as compose:
            obj.start(False)
        args = compose.call_args.args
        self.assertEqual(args[-2:], upgrade.SERVICES)
        self.assertIn("--no-build", args)
        self.assertIn("--no-deps", args)

    def test_optional_session_does_not_read_stdin(self):
        with patch.object(upgrade.os, "read") as read:
            self.assertIsNone(upgrade.read_session(None))
        read.assert_not_called()
        with patch.object(upgrade.os, "read", return_value=b"a" * 40 + b"\n"):
            self.assertEqual(upgrade.read_session(3), "a" * 40)
        for fd in (-1, 1, 2):
            with self.assertRaisesRegex(upgrade.GateError, "session_descriptor"):
                upgrade.read_session(fd)

    def test_both_same_release_modes_check_v861_and_native_auth_protection(self):
        obj = self.operator()
        obj.session = None
        calls = []
        def response(base, path, token=None, expected=200):
            calls.append((base, path, token, expected))
            if path.endswith("/health"):
                return {"version": "8.6.1", "status": "ok", "credentials": {"okx_configured": True}}
            if path.endswith("/status") and "/admin/" not in path:
                return {"version": "8.6.1"}
            if path.endswith("/auth/status"):
                return {"initialized": True}
            self.assertEqual(expected, 401)
            return {}
        for previous in (False, True):
            with self.subTest(previous=previous), \
                    patch.object(upgrade, "request", side_effect=response):
                result = obj.endpoints(previous)
        self.assertEqual(result["authenticated_session_probe"], "unexercised_no_supplied_session")
        self.assertIsNone(result["authenticated_identity"])
        protected = [c for c in calls if c[1].endswith("/auth/me")]
        self.assertEqual(len(protected), 4)
        self.assertTrue(all(c[2] is None and c[3] == 401 for c in protected))

    def test_supplied_session_is_checked_but_never_persisted(self):
        obj = self.operator()
        obj.session = "synthetic-existing-session"
        def response(base, path, token=None, expected=200):
            if token:
                self.assertEqual(token, obj.session)
                return {"user": {"id": 1, "username": "operator", "role": "superadmin", "enabled": True}}
            if path.endswith("/health"):
                return {"version": "8.6.1", "status": "ok", "credentials": {}}
            if path.endswith("/auth/status"):
                return {"initialized": True}
            return {"version": "8.6.1"}
        with patch.object(upgrade, "request", side_effect=response):
            result = obj.endpoints(False)
        self.assertEqual(result["authenticated_session_probe"], "passed")
        self.assertIsNotNone(result["authenticated_identity"])
        self.assertNotIn(obj.session, repr(result))

    def test_config_drift_aborts_execute_before_stop_or_recovery(self):
        obj = self.operator()
        runtime = obj.op / "live"
        candidate = obj.op / "candidate"
        runtime.mkdir()
        candidate.mkdir()
        (runtime / ".env").write_bytes(b"original")
        obj.manifest_sha = "fixture"
        obj.sources = Mock()
        with patch.object(upgrade, "ROOT", runtime):
            obj.state.update(phase="prepared", manifest_sha="fixture", guard=obj.guard(runtime),
                             candidate=upgrade.tree_manifest(candidate))
            (runtime / ".env").write_bytes(b"user-edited")
            obj.stop = Mock()
            obj.rollback = Mock()
            with self.assertRaisesRegex(upgrade.GateError, "source_config_drift"):
                obj.execute()
        obj.stop.assert_not_called()
        obj.rollback.assert_not_called()
        self.assertEqual((runtime / ".env").read_bytes(), b"user-edited")

    def test_unrelated_inventory_reordering_and_real_changes(self):
        mount1 = {"Destination": "/data", "Source": "/host/data", "RW": True}
        mount2 = {"Destination": "/logs", "Source": "/host/logs", "RW": True}
        container = {"project": "other", "id": "id1", "image": "image1", "restart": 0,
                     "state": "running", "health": "healthy", "mounts": [mount1, mount2],
                     "ports": {"80/tcp": [{"HostIp": "::", "HostPort": "80"},
                                            {"HostIp": "0.0.0.0", "HostPort": "80"}]}}
        before = {"other": container}
        after = copy.deepcopy(before)
        after["other"]["mounts"].reverse()
        after["other"]["ports"]["80/tcp"].reverse()
        upgrade.unchanged_unrelated(before, after)
        for field, value in (("id", "id2"), ("image", "image2"), ("restart", 1)):
            changed = copy.deepcopy(after)
            changed["other"][field] = value
            with self.assertRaisesRegex(upgrade.GateError, "unrelated_container_drift"):
                upgrade.unchanged_unrelated(before, changed)
        changed = copy.deepcopy(after)
        changed["other"]["mounts"][0]["RW"] = False
        with self.assertRaises(upgrade.GateError):
            upgrade.unchanged_unrelated(before, changed)
        with self.assertRaises(upgrade.GateError):
            upgrade.unchanged_unrelated(before, {})

    def test_full_stopped_copy_retains_bytes_modes_empty_dirs_and_link(self):
        obj = self.operator()
        source = obj.op / "stopped"
        source.mkdir(mode=0o700)
        for name in ("data/empty", "logs", "backups", "plugins", "frontend/public", "docs/images"):
            (source / name).mkdir(parents=True)
        (source / ".env").write_bytes(b"synthetic-private-config\x00\xff")
        (source / ".env").chmod(0o600)
        self.stopped_wal(source / 'data/orders.db')
        for suffix in ('', '-wal', '-shm'):
            (source / ('data/orders.db' + suffix)).chmod(0o600)
        (source / "frontend/public/images").symlink_to("../../docs/images", target_is_directory=True)
        expected = upgrade.tree_manifest(source)
        snapshot = obj.op / "snapshot"
        obj.copy(source, snapshot)
        self.assertEqual(upgrade.tree_manifest(snapshot), expected)
        self.assertEqual(upgrade.tree_manifest(source), expected)
        probe = obj.op / 'disposable-snapshot-probe'
        obj.copy(snapshot, probe)
        with upgrade.sqlite_snapshot(probe / 'data/orders.db') as db:
            self.assertEqual(db.execute('SELECT id,status,payload FROM orders ORDER BY id').fetchall(),
                             [(1, 'prior', b'prior\x00\xff'), (2, 'committed-wal', bytes(range(256)))])
        self.assertEqual(upgrade.tree_manifest(source), expected)
        self.assertEqual(upgrade.tree_manifest(snapshot), expected)


if __name__ == "__main__":
    unittest.main()
