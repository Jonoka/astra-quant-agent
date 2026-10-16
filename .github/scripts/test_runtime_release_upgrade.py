"""Synthetic safety gates; execute on GitHub Actions, never the VPS."""
import copy
import errno
from contextlib import closing, contextmanager, nullcontext, redirect_stderr, redirect_stdout
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

# Synthetic subprocess role imports neither the application nor runtime helpers.
if not (len(sys.argv) > 1 and sys.argv[1] == '--synthetic-worker'):
    SPEC = importlib.util.spec_from_file_location("runtime_upgrade", Path(__file__).with_name("runtime_release_upgrade.py"))
    upgrade = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(upgrade)
    sys.path.insert(0, str(Path(__file__).parent))
    import stage_release_bundle as staging


LEGACY_REFERENCE_SHA = '90f9f3a558bdbea0171b19a42c58e2fae7ed8e9d'
LEGACY_REFERENCE_IMAGE = 'ghcr.io/jonoka/astra-quant-agent@sha256:8b471e834dbfe633d720dc5d0ad0c4249e922dce91689719c46ca0fb6575b43b'


class MetadataEntry:
    """Preserve DirEntry's interface while observing its real metadata reads."""
    def __init__(self, entry, before_stat):
        self.entry = entry
        self.before_stat = before_stat

    def __getattr__(self, name):
        return getattr(self.entry, name)

    def __fspath__(self):
        return os.fspath(self.entry)

    def stat(self, *, follow_symlinks=True):
        self.before_stat(self.entry, follow_symlinks)
        return self.entry.stat(follow_symlinks=follow_symlinks)


class ScandirSnapshot:
    """A real directory listing with deterministic list-to-stat race injection."""
    def __init__(self, iterator, before_entry, before_stat):
        self.iterator = iterator
        self.entries = iter(sorted(iterator, key=lambda entry: entry.name))
        self.before_entry = before_entry
        self.before_stat = before_stat
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        try:
            entry = next(self.entries)
        except StopIteration:
            self.close()
            raise
        self.before_entry(entry)
        return MetadataEntry(entry, self.before_stat)

    def close(self):
        self.iterator.close()
        self.closed = True


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
        # These 69 historical filesystem/SQLite/admission tests do not own real
        # containers. The new maintenance boundary is tested separately, while
        # its final admission call still executes each fixture's idle_window.
        obj.maintenance_drain = Mock()
        obj.maintenance_release_lock = Mock()
        obj.maintenance_clear_pause = Mock()
        obj.maintenance_idle_window = lambda: obj.idle_window()
        return obj

    @contextmanager
    def metadata_scans(self, *, before_open=None, before_entry=None, before_stat=None):
        """Only wrap scandir; every listed name and stat otherwise comes from disk."""
        scandir = os.scandir
        observed = {'opened': [], 'stats': [], 'iterators': []}
        def stat_entry(entry, follow_symlinks):
            observed['stats'].append((Path(entry.path), follow_symlinks))
            if before_stat is not None:
                before_stat(entry, follow_symlinks)
        def scan(path):
            path = Path(path)
            observed['opened'].append(path)
            if before_open is not None:
                before_open(path)
            iterator = ScandirSnapshot(scandir(path), before_entry or (lambda entry: None), stat_entry)
            observed['iterators'].append(iterator)
            return iterator
        with patch.object(upgrade.os, 'scandir', scan):
            yield observed
        self.assertTrue(all(iterator.closed for iterator in observed['iterators']))

    def cli(self, mode, operation):
        """Run real argument parsing/dispatch, isolating host-only admission."""
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = ['runtime_release_upgrade.py', mode, str(operation),
                     '--deployment-plan-sha256', 'a' * 64]
        with patch.object(sys, 'argv', arguments), \
                patch.object(upgrade, 'BACKUPS', self.root), \
                patch.object(upgrade.os, 'umask'), \
                patch.object(upgrade.os, 'geteuid', return_value=0), \
                patch.object(upgrade, 'operation_path', side_effect=lambda path: path), \
                patch.object(upgrade, 'upgrade_lock', return_value=nullcontext()), \
                patch.object(upgrade.signal, 'signal'), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = upgrade.main()
        return result, stdout.getvalue(), stderr.getvalue()

    def diagnostic(self, result, stdout, stderr, kind):
        self.assertEqual(result, 1)
        self.assertEqual(stderr, '')
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], 'FAIL: runtime_failure')
        self.assertTrue(lines[1].startswith('DIAGNOSTIC: '))
        report = upgrade.json.loads(lines[1].removeprefix('DIAGNOSTIC: '))
        self.assertEqual(set(report), {'type', 'frames'})
        self.assertEqual(report['type'], kind)
        self.assertTrue(report['frames'])
        for frame in report['frames']:
            self.assertEqual(set(frame), {'function', 'line'})
            self.assertIsInstance(frame['function'], str)
            self.assertIs(type(frame['line']), int)
            self.assertGreater(frame['line'], 0)
        self.assertNotIn(str(self.root), stdout)
        self.assertNotIn('synthetic-private', stdout)
        self.assertNotIn('Traceback', stdout)
        return report

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

    def test_capacity_is_metadata_only_and_preserves_strict_free_space_boundary(self):
        obj = self.operator()
        runtime = self.root / 'runtime'
        (runtime / 'data/empty').mkdir(parents=True)
        (runtime / '.env').write_bytes(b'private-synthetic-config')
        (runtime / 'data/cache.bin').write_bytes(b'cache-payload')
        size = len(b'private-synthetic-config') + len(b'cache-payload')
        boundary = 5 * size + 1024**3
        with patch.object(upgrade, 'ROOT', runtime), \
                patch.object(upgrade, 'tree_manifest', side_effect=AssertionError('content manifest used')), \
                patch.object(upgrade, 'digest', side_effect=AssertionError('content digest used')), \
                patch.object(Path, 'read_bytes', side_effect=AssertionError('content bytes read')), \
                patch.object(Path, 'open', side_effect=AssertionError('content file opened')), \
                self.metadata_scans() as observed:
            self.assertEqual(upgrade.capacity_bytes(runtime), size)
            for free in (boundary - 1, boundary, boundary + 1):
                with self.subTest(free=free), patch('shutil.disk_usage', return_value=Mock(free=free)):
                    if free <= boundary:
                        with self.assertRaisesRegex(upgrade.GateError, 'snapshot_recovery_capacity'):
                            obj.capacity()
                    else:
                        obj.capacity()
        self.assertTrue(observed['stats'])
        self.assertTrue(all(follow is False for _, follow in observed['stats']))

    def test_capacity_retries_entire_scan_after_real_atomic_cache_replace(self):
        def fixture(name):
            root = self.root / name
            cache = root / 'z-cache'
            cache.mkdir(parents=True)
            counted, current, pending = root / 'a-counted', cache / 'a-current', cache / 'z-pending.tmp'
            counted.write_bytes(b'old')
            current.write_bytes(b'prior')
            pending.write_bytes(b'new-cache-payload')
            return root, counted, current, pending
        runtime, counted, current, pending = fixture('runtime')
        replaced = []
        def replace_after_listing(entry):
            if Path(entry.path) == pending and not replaced:
                os.replace(pending, current)
                counted.write_bytes(b'larger-after-replacement')
                replaced.append(True)
        with self.metadata_scans(before_entry=replace_after_listing) as observed:
            self.assertEqual(upgrade.capacity_bytes(runtime),
                             len(b'larger-after-replacement') + len(b'new-cache-payload'))
        self.assertEqual(replaced, [True])
        self.assertEqual(observed['opened'].count(runtime), 2)
        self.assertEqual(sum(path == counted for path, _ in observed['stats']), 2)
        self.assertFalse(pending.exists())
        # The same fully compatible iterator reproduces the original failure in
        # the content-manifest path, rather than failing on a mocked interface.
        runtime, counted, current, pending = fixture('baseline')
        replaced.clear()
        with self.metadata_scans(before_entry=replace_after_listing), \
                self.assertRaises(FileNotFoundError) as caught:
            upgrade.tree_manifest(runtime)
        self.assertEqual(Path(caught.exception.filename), pending)
        self.assertEqual(replaced, [True])

    def test_capacity_persistent_disappearance_fails_after_three_complete_attempts(self):
        runtime = self.root / 'runtime'
        cache = runtime / 'z-cache'
        cache.mkdir(parents=True)
        counted, current, pending = runtime / 'a-counted', cache / 'a-current', cache / 'z-pending.tmp'
        counted.write_bytes(b'count-before-failure')
        current.write_bytes(b'prior')
        replacements = []
        def create_pending(path):
            if path == cache:
                pending.write_bytes(b'new-cache-payload')
        def replace_pending(entry):
            if Path(entry.path) == pending:
                os.replace(pending, current)
                replacements.append(True)
        with self.metadata_scans(before_open=create_pending, before_entry=replace_pending) as observed, \
                self.assertRaises(FileNotFoundError) as caught:
            upgrade.capacity_bytes(runtime)
        self.assertEqual(Path(caught.exception.filename), pending)
        self.assertEqual(len(replacements), 3)
        self.assertEqual(observed['opened'].count(runtime), 3)
        self.assertEqual(sum(path == counted for path, _ in observed['stats']), 3)

    def test_capacity_permission_and_other_io_errors_are_not_retried(self):
        (self.root / 'a-counted').write_bytes(b'counted')
        blocked = self.root / 'z-blocked'
        blocked.write_bytes(b'not-skippable')
        for kind, number in ((PermissionError, errno.EACCES), (OSError, errno.EIO)):
            failure = kind(number, 'synthetic-private-error', str(blocked))
            def fail_stat(entry, follow_symlinks):
                if Path(entry.path) == blocked:
                    raise failure
            with self.subTest(kind=kind.__name__), \
                    self.metadata_scans(before_stat=fail_stat) as observed, \
                    self.assertRaises(kind) as caught:
                upgrade.capacity_bytes(self.root)
            self.assertIs(caught.exception, failure)
            self.assertEqual(observed['opened'].count(self.root), 1)
            self.assertEqual(sum(path == blocked for path, _ in observed['stats']), 1)

    def test_capacity_rejects_missing_root_instead_of_returning_zero(self):
        missing = self.root / 'missing'
        with self.assertRaises((upgrade.GateError, FileNotFoundError)):
            upgrade.capacity_bytes(missing)
        self.assertFalse(missing.exists())

    def test_capacity_official_link_is_never_followed_or_double_counted(self):
        (self.root / 'frontend/public').mkdir(parents=True)
        (self.root / 'docs/images').mkdir(parents=True)
        (self.root / 'docs/images/asset').write_bytes(b'one-copy-only')
        link = self.root / 'frontend/public/images'
        link.symlink_to('../../docs/images', target_is_directory=True)
        with self.metadata_scans() as observed:
            self.assertEqual(upgrade.capacity_bytes(self.root), len(b'one-copy-only'))
            self.assertEqual(upgrade.capacity_bytes(self.root / 'frontend'), 0)
        self.assertEqual([follow for path, follow in observed['stats'] if path == link], [False, False])
        self.assertNotIn(link, observed['opened'])
        self.assertNotIn(link / 'asset', [path for path, _ in observed['stats']])

    def test_capacity_rejects_unknown_or_nonexact_official_symlinks(self):
        for name, relative, target in (
                ('unknown', 'alias', 'asset'),
                ('wrong-target', 'frontend/public/images', '../../docs/./images'),
                ('wrong-location', 'nested/frontend/public/images', '../../docs/images')):
            root = self.root / name
            (root / 'docs/images').mkdir(parents=True)
            (root / 'asset').write_bytes(b'synthetic')
            link = root / relative
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            with self.subTest(name=name), self.assertRaisesRegex(upgrade.GateError, 'tree_symlink_not_allowed'):
                upgrade.capacity_bytes(root)

    def test_capacity_rejects_official_link_escaping_through_docs_alias(self):
        runtime = self.root / 'runtime'
        outside = self.root / 'outside'
        (outside / 'images').mkdir(parents=True)
        (outside / 'images/asset').write_bytes(b'outside-payload')
        (runtime / 'frontend/public').mkdir(parents=True)
        (runtime / 'docs').symlink_to(outside, target_is_directory=True)
        (runtime / 'frontend/public/images').symlink_to('../../docs/images', target_is_directory=True)
        with self.assertRaisesRegex(upgrade.GateError, 'tree_symlink_escape'):
            upgrade.capacity_bytes(runtime / 'frontend')

    def test_capacity_rejects_special_files(self):
        (self.root / 'a-regular').write_bytes(b'counted')
        os.mkfifo(self.root / 'z-pipe')
        with self.assertRaisesRegex(upgrade.GateError, 'tree_special_file'):
            upgrade.capacity_bytes(self.root)

    def test_tree_manifest_stays_strict_when_listed_file_disappears(self):
        missing = self.root / 'listed-then-missing'
        missing.write_bytes(b'must-be-hashed')
        lstat = Path.lstat
        removed = []
        def remove_before_lstat(path, *args, **kwargs):
            if path == missing and not removed:
                path.unlink()
                removed.append(True)
            return lstat(path, *args, **kwargs)
        with patch.object(Path, 'lstat', remove_before_lstat), self.assertRaises(FileNotFoundError) as caught:
            upgrade.tree_manifest(self.root)
        self.assertEqual(Path(caught.exception.filename), missing)
        self.assertEqual(removed, [True])

    def test_cli_initialization_unexpected_errors_expose_only_type_and_helper_frames(self):
        operation = self.root / 'operation'
        operation.mkdir()
        plan = operation / 'deployment-plan.json'
        plan.write_bytes(b'{}')
        read_bytes = Path.read_bytes
        for failure in (FileNotFoundError(errno.ENOENT, 'synthetic-private-message', str(plan)),
                        PermissionError(errno.EACCES, 'synthetic-private-message', str(plan)),
                        ValueError('synthetic-private-message ' + str(plan))):
            # Deliberately share a helper function's name. Filtering must use
            # traceback filenames, rather than just accepting function names.
            def main(path):
                synthetic_private_local = 'synthetic-private-local'
                if path == plan:
                    raise failure
                return read_bytes(path)
            with self.subTest(kind=type(failure).__name__), patch.object(Path, 'read_bytes', main):
                result = self.cli('preflight', operation)
            report = self.diagnostic(*result, type(failure).__name__)
            self.assertEqual([frame['function'] for frame in report['frames']],
                             ['main', '__init__', 'deployment_plan', 'read_deployment_plan'])

    def test_cli_prestop_capacity_error_preserves_prepared_state_without_recovery(self):
        obj = self.operator()
        runtime = self.root / 'runtime'
        runtime.mkdir()
        blocked = runtime / 'private-cache'
        blocked.write_bytes(b'synthetic-cache')
        obj.state.update(phase='prepared', baseline={}, endpoints={})
        obj.save('state.json', obj.state)
        before_state = copy.deepcopy(obj.state)
        before_bytes = (obj.op / 'state.json').read_bytes()
        obj.check_drift = Mock()
        obj.stop = Mock()
        obj.rollback = Mock()
        def fail_stat(entry, follow_symlinks):
            if Path(entry.path) == blocked:
                raise PermissionError(errno.EACCES, 'synthetic-private-message', str(blocked))
        with patch.object(upgrade, 'ROOT', runtime), patch.object(upgrade, 'Upgrade', return_value=obj), \
                self.metadata_scans(before_stat=fail_stat) as observed:
            result = self.cli('execute', obj.op)
        report = self.diagnostic(*result, 'PermissionError')
        names = [frame['function'] for frame in report['frames']]
        self.assertEqual(names[:3], ['main', 'execute', 'capacity'])
        self.assertIn('capacity_bytes', names)
        self.assertNotIn('fail_stat', names)
        self.assertNotIn('stat_entry', names)
        self.assertEqual(observed['opened'].count(runtime), 1)
        self.assertEqual(obj.state, before_state)
        self.assertEqual((obj.op / 'state.json').read_bytes(), before_bytes)
        self.assertFalse((obj.op / 'failure.json').exists())
        obj.check_drift.assert_called_once_with()
        obj.stop.assert_not_called()
        obj.rollback.assert_not_called()

    def test_cli_known_gate_error_output_is_unchanged(self):
        operation = self.root / 'operation'
        operation.mkdir()
        with patch.object(upgrade.Upgrade, 'deployment_plan', side_effect=upgrade.GateError('synthetic_known_gate')):
            result, stdout, stderr = self.cli('preflight', operation)
        self.assertEqual(result, 1)
        self.assertEqual(stdout, 'FAIL: synthetic_known_gate\n')
        self.assertEqual(stderr, '')

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
        pin = upgrade.OFFICIAL_PROMPT_REFRESH
        source = Path(__file__).resolve().parents[2]
        old = subprocess.check_output(['git', '-C', str(source), 'show',
                                       pin['previous_source'] + ':' + upgrade.PROMPT])
        new = (source / upgrade.PROMPT).read_bytes()
        self.assertEqual(upgrade.sha(old), pin['previous_sha256'])
        self.assertEqual(upgrade.sha(new), pin['release_sha256'])
        obj.manifest = {'source_files': {'previous': {upgrade.PROMPT: upgrade.sha(old)},
                                        'release': {upgrade.PROMPT: upgrade.sha(new)}}}
        obj.deployment_plan = Mock(return_value={
            'previous_source': pin['previous_source'], 'previous_image': pin['previous_image'],
            'protected_config': {upgrade.PROMPT: upgrade.sha(old)}})
        obj.sources = Mock()  # Full raw source verification is in the plan integration suite.
        for name, value in (("source-previous", old), ("source-release", new), ("candidate", b"custom")):
            (obj.op / name / "data").mkdir(parents=True)
            (obj.op / name / upgrade.PROMPT).write_bytes(value)
        obj.prompt_refresh(obj.op / "candidate")
        self.assertEqual((obj.op / "candidate" / upgrade.PROMPT).read_bytes(), b"custom")
        self.assertFalse(obj.state["prompt_refreshed"])
        (obj.op / "candidate" / upgrade.PROMPT).write_bytes(old)
        obj.prompt_refresh(obj.op / "candidate")
        self.assertEqual((obj.op / "candidate" / upgrade.PROMPT).read_bytes(), new)
        obj.restore_prompt(obj.op / "candidate")
        self.assertEqual((obj.op / "candidate" / upgrade.PROMPT).read_bytes(), old)

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
        # Maintenance now verifies scheduler DB readability before changing any
        # admission state. Keep that real read-only gate in this WAL fixture.
        with closing(sqlite3.connect(live / upgrade.GATEWAY_DATABASE)) as database:
            database.execute('CREATE TABLE job_runs(id INTEGER PRIMARY KEY, status TEXT)')
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



# Real-process drain regressions below use only synthetic jobs and loopback HTTP.
import concurrent.futures
import fcntl
import http.client
import inspect
import json
import platform
import signal
import socket
import threading
import time
import types
from datetime import datetime, timedelta, timezone

JOB_NAMES = ("synthetic_a", "synthetic_b", "synthetic_c")
WAIT_SECONDS = 12.0


def emit(path: Path, actor: str, event: str, **data: object) -> None:
    # One append write, so concurrent processes/threads cannot interleave lines.
    row = {"t_ns": time.monotonic_ns(), "pid": os.getpid(),
           "actor": actor, "event": event, **data}
    raw = (json.dumps(row, sort_keys=True) + "\n").encode()
    if len(raw) >= 4096:
        raise AssertionError("oversized test event")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, raw) != len(raw):
            raise AssertionError("short event write")
    finally:
        os.close(fd)


def install_offline_guard(case: Path, synthetic_scripts: set[Path]) -> None:
    """Defense in depth: no sockets, no unapproved commands, scratch writes only."""
    case = case.resolve()
    allowed_scripts = {str(path.resolve()) for path in synthetic_scripts}
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def scratch_path(value: object) -> None:
        if isinstance(value, int):
            return
        path = Path(os.fsdecode(value)).resolve()
        if not path.is_relative_to(case):
            raise AssertionError(f"write outside isolated test directory: {path}")

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event.startswith("socket.") or event in {"os.system", "os.posix_spawn"}:
            raise AssertionError(f"forbidden external activity: {event}")
        if event == "open":
            path, mode, flags = args
            if (isinstance(flags, int) and flags & write_flags) or (
                isinstance(mode, str) and any(char in mode for char in "wax+")
            ):
                scratch_path(path)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod"}:
            scratch_path(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            scratch_path(args[0])
            scratch_path(args[1])
        elif event == "subprocess.Popen":
            executable, argv, cwd, _env = args
            if str(executable) != sys.executable or len(argv) != 2:
                raise AssertionError("only the synthetic Python scripts may run")
            if str(Path(argv[1]).resolve()) not in allowed_scripts:
                raise AssertionError("real job execution is forbidden")
            scratch_path(cwd)

    sys.addaudithook(audit)


def stub_module(name: str, **values: object) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(values)
    sys.modules[name] = module
    return module


def load_source_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load source: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def synthetic_worker(source_root: Path, case: Path, actor: str, mode: str) -> None:
    """Run in a separate OS process; never import the production dependency tree."""
    sys.dont_write_bytecode = True
    event_file = case / "events.jsonl"
    script_dir = case / "scripts"
    worker = None

    def record(event: str, **data: object) -> None:
        emit(event_file, actor, event, **data)

    def wait_gate(name: str) -> None:
        deadline = time.monotonic() + WAIT_SECONDS
        while not (case / name).exists():
            if time.monotonic() > deadline:
                raise AssertionError(f"test gate did not open: {name}")
            time.sleep(0.005)

    class SyntheticStore:
        def __init__(self, _path: Path):
            past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            self.states = {f"job.last.{name}": past for name in JOB_NAMES}
            self.lock = threading.RLock()
            record("store_created")

        def recover_processing(self):
            record("recover_processing")

        def recover_stale_job_runs(self):
            record("recover_stale_job_runs")
            return 0

        def prune_job_runs(self):
            return {"deleted": 0}

        def get_state(self, key):
            with self.lock:
                return self.states.get(key, "")

        def set_state(self, key, value):
            with self.lock:
                self.states[key] = value
            if mode == "mid_tick" and key == "job.last.synthetic_b":
                record("tick_paused_before_second_submit")
                wait_gate("release_tick")

        def begin_job(self, name, scheduled_at):
            record("begin_job", job=name, scheduled_at=scheduled_at)
            return name

        def finish_job(self, run_id, rc, detail, **_kwargs):
            record("finish_job", job=run_id, rc=rc, detail=detail)

        def claim_due(self, limit):
            assert limit == 1
            record("claim_due")
            # A deterministic signal window after the first tick. This replaces
            # only store I/O; real worker control flow and sleep are unmodified.
            deadline = time.monotonic() + WAIT_SECONDS
            while worker.RUNNING:
                if time.monotonic() > deadline:
                    raise AssertionError("test never delivered SIGTERM")
                time.sleep(0.005)
            return []

    class ForbiddenAdapter:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("delivery adapters must never run in this test")

    stub_module("astra_backend", __path__=[])
    stub_module("astra_gateway", __path__=[])
    stub_module("scripts", __path__=[])
    stub_module("astra_backend.time_utils", parse_beijing=datetime.fromisoformat)
    stub_module("astra_backend.schedule_store", load_schedule=lambda: {})
    stub_module("astra_backend.backup_store", list_jobs=lambda: [])
    stub_module("astra_gateway.collection_timeout", collection_timeout_summary=lambda *_: "")
    stub_module("astra_gateway.store", GatewayStore=SyntheticStore)
    stub_module("astra_gateway.channels", NotificationChannelAdapter=ForbiddenAdapter)
    stub_module("astra_gateway.publisher", DB_PATH=case / "unused.db")
    stub_module("astra_gateway.pidfile", PID_FILE=case / "owner.pid")
    stub_module("astra_gateway.cache_warmer", check_and_warmup_cache=lambda: None)
    stub_module("scripts.instrument_pool", POOL_FILE=case / "pool.exists",
                save_instruments=lambda _: None, DEFAULT_INSTRUMENTS=[])
    install_offline_guard(case, {script_dir / f"{name}.py" for name in JOB_NAMES})
    scheduler_module = load_source_module(
        "astra_gateway.scheduler", source_root / "astra_gateway" / "scheduler.py")
    scheduler_module.ROOT = case
    scheduler_module.SCRIPTS = script_dir
    scheduler_module.current_jobs = lambda: tuple(
        scheduler_module.JobSpec(name, f"{name}.py", 60, 20)
        for name in (() if mode == "idle" else JOB_NAMES)
    )

    class ObservedScheduler(scheduler_module.GatewayScheduler):
        def __init__(self, store):
            super().__init__(store, max_workers=1)
            real_submit = self.executor.submit

            def observed_submit(fn, spec, *args, **kwargs):
                future = real_submit(fn, spec, *args, **kwargs)
                record("submitted", job=spec.name)
                future.add_done_callback(lambda done: record(
                    "future_done", job=spec.name, cancelled=done.cancelled(),
                    error=repr(done.exception()) if not done.cancelled() else "cancelled"))
                return future

            self.executor.submit = observed_submit

        def _execute(self, spec, scheduled_at=None):
            record("thread_start", job=spec.name,
                   daemon=threading.current_thread().daemon)
            return super()._execute(spec, scheduled_at)

        def tick(self, now=None):
            record("tick_enter")
            result = super().tick(now)
            record("tick_return", launched=result)
            return result

        def shutdown(self):
            def statuses():
                return {name: {"done": f.done(), "cancelled": f.cancelled(),
                               "running": f.running()}
                        for name, f in self.running.items()}
            record("shutdown_enter", futures=statuses())
            super().shutdown()  # Exact production wait=False/cancel_futures=False.
            record("shutdown_return", futures=statuses())

    worker = load_source_module(
        "astra_gateway.worker", source_root / "astra_gateway" / "worker.py")
    worker.ROOT = case
    worker.LOCK_FILE = case / "owner.lock"
    worker.PID_FILE = case / "owner.pid"
    worker.LOG_FILE = case / "worker.log"
    worker.HEARTBEAT_FILE = case / "heartbeat"
    worker.DB_PATH = case / "unused.db"
    worker.GatewayScheduler = ObservedScheduler
    worker.RUNNING = True
    original_stop = worker.stop

    def observed_stop(*args):
        original_stop(*args)  # Real production function handles the OS signal.
        record("signal_handled", signum=args[0], running=worker.RUNNING)

    worker.stop = observed_stop
    record("before_run")
    worker.run()  # Real function; real signal.signal and flock registration.
    record("run_returned")
    # Returning out of __main__ must not terminate queued executor work. Python's
    # real interpreter-exit ThreadPoolExecutor join is part of this test.


SYNTHETIC_SCRIPT = r'''
import json, os, signal, sys, time
from pathlib import Path
root = Path(__file__).resolve().parents[1]
job = Path(__file__).stem

def no_network(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
        raise AssertionError("synthetic child is strictly offline")
sys.addaudithook(no_network)

def record(event, **data):
    row = {"t_ns": time.monotonic_ns(), "pid": os.getpid(), "actor": "child",
           "event": event, "job": job, "ppid": os.getppid(), **data}
    raw = (json.dumps(row, sort_keys=True) + "\n").encode()
    fd = os.open(root / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        assert os.write(fd, raw) == len(raw)
    finally:
        os.close(fd)

def interrupted(signum, _frame):
    record("child_interrupted", signum=signum)
    raise SystemExit(42)
signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
record("child_started", run_id=os.environ["ASTRA_JOB_RUN_ID"])
deadline = time.monotonic() + 20
while not (root / ("allow_" + job)).exists():
    if time.monotonic() > deadline:
        record("child_gate_timeout")
        raise SystemExit(43)
    time.sleep(0.005)
record("child_completed")
print("synthetic completion: " + job, flush=True)
'''


class RuntimeWorkerGracefulDrainTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.name, "posix", "real POSIX drain verification required")
        self.assertEqual(platform.python_implementation(), "CPython")
        self.source_root = Path(__file__).resolve().parents[2]
        for name in ("worker.py", "scheduler.py"):
            self.assertTrue((self.source_root / "astra_gateway" / name).is_file(),
                            "current source checkout must contain real worker and scheduler files")
        self.tmp = tempfile.TemporaryDirectory(prefix="astra-offline-drain-")
        self.case = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.processes = []
        self.addCleanup(self._stop_owned_processes)
        (self.case / "scripts").mkdir()
        (self.case / "pool.exists").touch()
        for name in JOB_NAMES:
            (self.case / "scripts" / f"{name}.py").write_text(SYNTHETIC_SCRIPT)
        self.events = self.case / "events.jsonl"

    def _rows(self, actor=None, event=None):
        rows = []
        if self.events.exists():
            for line in self.events.read_text().splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A producer may be appending the last line.
                if (actor is None or row["actor"] == actor) and (event is None or row["event"] == event):
                    rows.append(row)
        return rows

    def _wait(self, predicate, description):
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.005)
        diagnostics = {p.pid: (self.case / f"{actor}.stderr").read_text()
                       for p, actor in self.processes}
        self.fail(f"timed out waiting for {description}; events={self._rows()}; stderr={diagnostics}")

    def _event(self, actor, event):
        return self._wait(lambda: self._rows(actor, event), f"{actor}:{event}")[-1]

    def _start(self, actor, mode):
        # Deliberately do not propagate credentials, proxy URLs, model settings,
        # or any other caller environment into the worker or its job children.
        env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
               "LANG": "C.UTF-8"}
        with (self.case / f"{actor}.stdout").open("wb") as stdout, \
             (self.case / f"{actor}.stderr").open("wb") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--synthetic-worker",
                 str(self.source_root), str(self.case), actor, mode],
                cwd=self.case, env=env, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, start_new_session=True)
        self.processes.append((process, actor))
        return process

    def _stop_owned_processes(self):
        # Cleanup opens every gate first, allowing synthetic children to finish.
        # Signals are sent only to Popen objects created by this test.
        (self.case / "release_tick").touch()
        for name in JOB_NAMES:
            (self.case / f"allow_{name}").touch()
        for process, _actor in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4)
        artifact_dir = os.environ.get("ASTRA_TEST_ARTIFACT_DIR")
        if artifact_dir:
            target = Path(artifact_dir) / self._testMethodName
            target.mkdir(parents=True, exist_ok=True)
            for source in self.case.iterdir():
                if source.is_file():
                    (target / source.name).write_bytes(source.read_bytes())

    def _lock_is_free(self):
        with (self.case / "owner.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(handle, fcntl.LOCK_UN)
            return True

    def _assert_draining_gap(self, process, actor):
        returned = self._event(actor, "run_returned")
        self.assertIsNone(process.poll(), "OS process should still be draining jobs")
        self.assertTrue(self._lock_is_free(), "existing code releases local lock before interpreter joins")
        emit(self.events, "controller", "lock_free_while_old_process_alive", worker_pid=process.pid)
        self.assertFalse(self._rows("child", "child_completed"))
        self.assertFalse(self._rows("child", "child_interrupted"))
        status = self._event(actor, "shutdown_return")["futures"]
        self.assertEqual(set(status), set(JOB_NAMES))
        self.assertTrue(status[JOB_NAMES[0]]["running"])
        self.assertTrue(all(not value["done"] and not value["cancelled"] for value in status.values()))
        self.assertTrue(all(not status[name]["running"] for name in JOB_NAMES[1:]))
        self.assertEqual(len(self._rows(actor, "tick_enter")), 1)
        return returned

    def _drain(self, process, actor):
        for name in JOB_NAMES:
            self._wait(lambda: any(row["job"] == name for row in self._rows("child", "child_started")),
                       f"child {name} start")
            self.assertIsNone(process.poll())
            (self.case / f"allow_{name}").touch()
            self._wait(lambda: any(row["job"] == name for row in self._rows(actor, "finish_job")),
                       f"job {name} completion")
        self.assertEqual(process.wait(timeout=WAIT_SECONDS), 0)
        emit(self.events, "controller", "old_process_reaped", worker_pid=process.pid)
        submitted = self._rows(actor, "submitted")
        finished = self._rows(actor, "finish_job")
        completed = self._rows("child", "child_completed")
        self.assertEqual([row["job"] for row in submitted], list(JOB_NAMES))
        self.assertEqual([row["job"] for row in finished], list(JOB_NAMES))
        self.assertEqual([row["job"] for row in completed], list(JOB_NAMES))
        self.assertTrue(all(row["rc"] == 0 for row in finished))
        futures = self._rows(actor, "future_done")
        self.assertEqual(len(futures), 3)
        self.assertTrue(all(not row["cancelled"] and row["error"] == "None" for row in futures))
        self.assertTrue(all(not row["daemon"] for row in self._rows(actor, "thread_start")))
        self.assertFalse(self._rows("child", "child_interrupted"))
        self.assertEqual(len(self._rows(actor, "tick_enter")), 1)
        self.assertTrue(self._lock_is_free())
        return completed

    def test_sigterm_drains_running_and_queued_jobs_before_process_exit(self):
        old = self._start("old", "after_tick")
        self._event("old", "claim_due")
        self._event("child", "child_started")
        self.assertFalse(self._lock_is_free())
        self.assertEqual(len(self._rows("old", "submitted")), 3)
        old.send_signal(signal.SIGTERM)
        handled = self._event("old", "signal_handled")
        self.assertFalse(handled["running"])
        returned = self._assert_draining_gap(old, "old")
        completed = self._drain(old, "old")
        self.assertTrue(all(row["t_ns"] > returned["t_ns"] for row in completed))
        queued_starts = [row for row in self._rows("child", "child_started") if row["job"] != JOB_NAMES[0]]
        self.assertTrue(all(row["t_ns"] > returned["t_ns"] for row in queued_starts),
                        "queued work may create child processes after worker.run returns")

    def test_sigterm_during_tick_allows_last_batch_then_drains_it(self):
        old = self._start("old", "mid_tick")
        self._event("old", "tick_paused_before_second_submit")
        self._event("child", "child_started")
        self.assertEqual([row["job"] for row in self._rows("old", "submitted")], [JOB_NAMES[0]])
        old.send_signal(signal.SIGTERM)
        handled = self._event("old", "signal_handled")
        (self.case / "release_tick").touch()
        self._event("old", "tick_return")
        submissions = self._rows("old", "submitted")
        self.assertEqual([row["job"] for row in submissions], list(JOB_NAMES))
        self.assertTrue(all(row["t_ns"] > handled["t_ns"] for row in submissions[1:]),
                        "stop does not interrupt the already-entered tick for-loop")
        self._assert_draining_gap(old, "old")
        self._drain(old, "old")

    def test_restart_after_reaping_old_worker_has_one_owner(self):
        old = self._start("old", "after_tick")
        self._event("old", "claim_due")
        self._event("child", "child_started")
        contender = self._start("before_stop_contender", "idle")
        self.assertEqual(contender.wait(timeout=WAIT_SECONDS), 0)
        self.assertFalse(self._rows("before_stop_contender", "store_created"))
        self.assertFalse(self._lock_is_free())
        old.send_signal(signal.SIGTERM)
        self._assert_draining_gap(old, "old")
        self._drain(old, "old")
        successor = self._start("successor", "idle")
        self._event("successor", "claim_due")
        self.assertFalse(self._lock_is_free())
        self.assertEqual((self.case / "owner.pid").read_text(), str(successor.pid))
        duplicate = self._start("duplicate", "idle")
        self.assertEqual(duplicate.wait(timeout=WAIT_SECONDS), 0)
        self.assertFalse(self._rows("duplicate", "store_created"))
        self.assertFalse(self._rows("duplicate", "tick_enter"))
        self.assertEqual((self.case / "owner.pid").read_text(), str(successor.pid))
        self.assertFalse(self._lock_is_free())
        self.assertEqual(len(self._rows("successor", "recover_processing")), 1)
        self.assertEqual(len(self._rows("successor", "recover_stale_job_runs")), 1)
        successor.send_signal(signal.SIGTERM)
        self.assertEqual(successor.wait(timeout=WAIT_SECONDS), 0)
        self.assertTrue(self._lock_is_free())



# These fixture programs are written only into each test's TemporaryDirectory.
# They import no Astra app/store/model/trading implementation.
UVICORN_SYNTHETIC_APP = r'''
"""Isolated FastAPI fixture; imports no Astra, model, trading, or network client code."""
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

# Defense in depth: any accidental application egress is fail-closed.
def loopback_only(event, args):
    if event in {"socket.connect", "socket.sendto", "socket.bind"}:
        address = args[1]
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("non-loopback network is forbidden")
    if event == "socket.getaddrinfo" and args[0] not in {"127.0.0.1", "::1", "localhost"}:
        raise AssertionError("external DNS is forbidden")
sys.addaudithook(loopback_only)

from fastapi import BackgroundTasks, FastAPI

STATE = Path(os.environ["GRACEFUL_TEST_STATE"])
WORKER = Path(__file__).with_name("controlled_worker.py")


def mark(name, **extra):
    target = STATE / name
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "monotonic": time.monotonic(), **extra}))
    temporary.replace(target)


@asynccontextmanager
async def lifespan(app):
    mark("app-started.json")
    yield
    mark("app-shutdown.json")


app = FastAPI(lifespan=lifespan)


def blocking_job(label):
    mark(f"{label}.handler-started.json")
    child = subprocess.run(
        [sys.executable, str(WORKER), str(STATE), label],
        check=True, text=True, capture_output=True,
    )
    mark(f"{label}.handler-completed.json", child_returncode=child.returncode)
    return {"ok": True, "child_returncode": child.returncode}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/sync")
def sync_job():
    return blocking_job("sync")


@app.get("/background")
def background_job(tasks: BackgroundTasks):
    tasks.add_task(blocking_job, "background")
    return {"accepted": True}
'''
UVICORN_SYNTHETIC_CHILD = r'''
"""Harmless child: wait for a scratch-file release, then exit naturally."""
import json
import os
from pathlib import Path
import sys
import time

def no_external_activity(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
        raise AssertionError("synthetic child is offline")
sys.addaudithook(no_external_activity)

state = Path(sys.argv[1])
label = sys.argv[2]

def mark(name, **values):
    target = state / name
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(values))
    temporary.replace(target)

mark(f"{label}.child-started.json", pid=os.getpid(), ppid=os.getppid(), monotonic=time.monotonic())
deadline = time.monotonic() + 20
while not (state / f"{label}.release").exists():
    if time.monotonic() >= deadline:
        (state / f"{label}.child-timeout").write_text("test harness never released the child\n")
        sys.exit(77)
    time.sleep(0.02)
mark(f"{label}.child-completed.json", pid=os.getpid(), monotonic=time.monotonic())
print("controlled child completed", flush=True)
'''


def uvicorn_test_request(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=25)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def uvicorn_test_refused(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return False
    except ConnectionRefusedError:
        return True


def uvicorn_test_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class RuntimeUvicornGracefulDrainTests(unittest.TestCase):
    """Exact requirements versions; single SIGINT, TCP and harmless subprocesses."""

    def exercise(self, label):
        import fastapi
        import starlette
        import uvicorn

        source_root = Path(__file__).resolve().parents[2]
        requirements = (source_root / 'requirements.txt').read_text().splitlines()
        for name, module in (('uvicorn', uvicorn), ('fastapi', fastapi)):
            pins = [line.split('==', 1)[1].strip() for line in requirements
                    if line.startswith(name + '==')]
            self.assertEqual(len(pins), 1, f'{name} must have one exact requirements pin')
            self.assertEqual(module.__version__, pins[0],
                             f'real drain test must run the {name} requirements version')
        self.assertIsNone(inspect.signature(uvicorn.Config).parameters[
            'timeout_graceful_shutdown'].default)
        self.assertEqual(os.name, 'posix', 'real POSIX SIGTERM verification required')
        temporary = tempfile.TemporaryDirectory(prefix='astra-offline-api-drain-')
        root = Path(temporary.name)
        self.addCleanup(temporary.cleanup)
        state = root / label
        state.mkdir()
        (root / 'graceful_app.py').write_text(UVICORN_SYNTHETIC_APP)
        (root / 'controlled_worker.py').write_text(UVICORN_SYNTHETIC_CHILD)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        # Only dependency roots of the imported, version-verified packages are
        # propagated. Do not inherit credentials, proxies, UVICORN_* overrides,
        # model settings, or other caller environment into the server/children.
        dependency_roots = sorted({str(Path(module.__file__).resolve().parents[1])
                                   for module in (uvicorn, fastapi, starlette)})
        env = {'GRACEFUL_TEST_STATE': str(state), 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONUNBUFFERED': '1', 'LANG': 'C.UTF-8',
               'PYTHONPATH': os.pathsep.join(dependency_roots)}
        command = [sys.executable, '-B', '-m', 'uvicorn', 'graceful_app:app',
                   '--host', '127.0.0.1', '--port', str(port), '--log-level', 'info']
        info = {'case': label, 'uvicorn': uvicorn.__version__,
                'fastapi': fastapi.__version__, 'starlette': starlette.__version__,
                'configured_graceful_timeout': None, 'forced_cleanup': False}
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        process = None

        def wait_for(predicate, description, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = predicate()
                if value:
                    return value
                time.sleep(0.01)
            log = (state / 'uvicorn.log').read_text()
            self.fail(f'timed out waiting for {description}; synthetic server log: {log}')

        with (state / 'uvicorn.log').open('w') as server_log:
            try:
                process = subprocess.Popen(command, cwd=root, env=env,
                    stdin=subprocess.DEVNULL, stdout=server_log, stderr=subprocess.STDOUT,
                    start_new_session=True)
                info['backend_pid'] = process.pid
                wait_for(lambda: (state / 'app-started.json').exists(), 'app startup')

                def ready():
                    try:
                        return uvicorn_test_request(port, '/health')[0] == 200
                    except (ConnectionError, OSError):
                        return False

                wait_for(ready, 'loopback health')
                future = pool.submit(uvicorn_test_request, port, '/' + label)
                marker = state / (label + '.child-started.json')
                wait_for(marker.exists, 'synthetic child startup')
                child = json.loads(marker.read_text())
                child_pid = child['pid']
                info['child_pid'] = child_pid
                self.assertEqual(child['ppid'], process.pid)
                self.assertTrue(uvicorn_test_alive(child_pid))
                if label == 'background':
                    self.assertEqual(future.result(timeout=3), (200, {'accepted': True}))
                    info['response_before_sigint'] = True
                else:
                    self.assertFalse(future.done())

                info['sigint_monotonic'] = time.monotonic()
                # Precisely one signal to this owned PID, never its process group.
                process.send_signal(signal.SIGINT)
                wait_for(lambda: uvicorn_test_refused(port), 'closed listener', timeout=5)
                self.assertIsNone(process.poll(), 'backend exited before child release')
                self.assertTrue(uvicorn_test_alive(child_pid))
                self.assertFalse((state / (label + '.child-completed.json')).exists())
                self.assertFalse((state / 'app-shutdown.json').exists())
                if label == 'sync':
                    self.assertFalse(future.done())
                info['backend_and_child_alive_while_listener_closed'] = True
                info['release_monotonic'] = time.monotonic()
                (state / (label + '.release')).write_text('finish naturally\n')
                expected = ((200, {'ok': True, 'child_returncode': 0}) if label == 'sync'
                            else (200, {'accepted': True}))
                self.assertEqual(future.result(timeout=10), expected)
                returncode = process.wait(timeout=10)
                info['backend_exit_observed_monotonic'] = time.monotonic()
                info['returncode'] = returncode
                # Uvicorn restores/re-raises the single SIGINT after normal drain.
                # Exit status alone proves neither success nor task completion.
                self.assertIn(returncode, (0, 130, -signal.SIGINT))
                self.assertFalse(uvicorn_test_alive(child_pid))
                self.assertFalse((state / (label + '.child-timeout')).exists())
                done = json.loads((state / (label + '.child-completed.json')).read_text())
                handler = json.loads((state / (label + '.handler-completed.json')).read_text())
                shutdown = json.loads((state / 'app-shutdown.json').read_text())
                self.assertEqual(handler['child_returncode'], 0)
                self.assertLess(info['release_monotonic'], done['monotonic'])
                self.assertLessEqual(done['monotonic'], handler['monotonic'])
                self.assertLessEqual(handler['monotonic'], shutdown['monotonic'])
                self.assertLessEqual(shutdown['monotonic'], info['backend_exit_observed_monotonic'])
                log = (state / 'uvicorn.log').read_text()
                self.assertIn('Application shutdown complete.', log)
                self.assertIn('Finished server process', log)
                self.assertNotIn('timeout graceful shutdown exceeded', log)
                self.assertNotIn('Traceback', log)
                self.assertTrue(uvicorn_test_refused(port))
                info['natural_child_exit_and_ordered_lifespan_shutdown'] = True
                info['drain_passed'] = True
            finally:
                (state / (label + '.release')).touch()
                if process is not None and process.poll() is None:
                    info['forced_cleanup'] = True
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                pool.shutdown(wait=True)
                (state / 'result.json').write_text(json.dumps(info, indent=2) + '\n')
                artifact_dir = os.environ.get('ASTRA_TEST_ARTIFACT_DIR')
                if artifact_dir:
                    target = Path(artifact_dir) / self._testMethodName
                    target.mkdir(parents=True, exist_ok=True)
                    for path in state.iterdir():
                        if path.is_file():
                            (target / path.name).write_bytes(path.read_bytes())
        self.assertFalse(info['forced_cleanup'])

    def test_sync_http_subprocess_drains(self):
        self.exercise('sync')

    def test_http_background_task_drains(self):
        self.exercise('background')


# Detached executor work is outside the HTTP/ASGI task graph. These real
# counterexamples are why backend admission uses exactly one SIGINT and an
# uncertain send must never be retried automatically. No real model is called.
DETACHED_TPE_SYNTHETIC_APP = r'''
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import json, os, signal, subprocess, sys, threading, time
from pathlib import Path
from fastapi import FastAPI
STATE = Path(os.environ['SYNTHETIC_STATE'])

def offline(event, args):
    if event in {'socket.connect','socket.bind','socket.sendto'}:
        addr=args[1]
        if isinstance(addr,tuple) and addr[0] not in {'127.0.0.1','::1'}:
            raise AssertionError('synthetic app cannot contact external network')
    if event=='socket.getaddrinfo' and args[0] not in {'127.0.0.1','::1','localhost'}:
        raise AssertionError('synthetic app cannot use external DNS')
sys.addaudithook(offline)

def mark(name, **data):
    target = STATE / (name + '.json')
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps({'pid':os.getpid(),'time':time.monotonic(),**data}))
    tmp.replace(target)

@asynccontextmanager
async def lifespan(app):
    mark('app_started')
    yield
    mark('app_shutdown')

app=FastAPI(lifespan=lifespan)

def thread_work():
    mark('thread_started',daemon=threading.current_thread().daemon)
    deadline=time.monotonic()+25
    while not (STATE/'release').exists():
        if time.monotonic()>deadline:
            mark('thread_timed_out')
            raise RuntimeError('test gate never released')
        time.sleep(.005)
    mark('thread_completed')

def child_work():
    mark('child_thread_started',daemon=threading.current_thread().daemon)
    result=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('child.py')),str(STATE)],
                          cwd=STATE,text=True,capture_output=True,timeout=30)
    mark('child_thread_completed',returncode=result.returncode)

@app.get('/health')
def health(): return {'ok':True}

@app.get('/detach')
def detach():
    pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='synthetic-model')
    futures=[pool.submit(thread_work),pool.submit(child_work)]
    deadline=time.monotonic()+10
    while not all((STATE/(n+'.json')).exists() for n in ['thread_started','child_started']):
        if time.monotonic()>deadline: raise RuntimeError('tasks failed to start')
        time.sleep(.005)
    cancelled=[f.cancel() for f in futures]
    pool.shutdown(wait=False,cancel_futures=True)
    mark('http_handler_returned',cancelled=cancelled,running=[f.running() for f in futures])
    return {'returned':True,'cancelled':cancelled,'running':[f.running() for f in futures]}
'''

DETACHED_TPE_SYNTHETIC_CHILD = r'''
import json,os,sys,time
from pathlib import Path
state=Path(sys.argv[1])
def offline(event,args):
    if event.startswith('socket.') or event in {'subprocess.Popen','os.system','os.posix_spawn'}:
        raise AssertionError('synthetic child is offline')
sys.addaudithook(offline)
def mark(name,**data):
    p=state/(name+'.json');t=p.with_suffix('.tmp')
    t.write_text(json.dumps({'pid':os.getpid(),'ppid':os.getppid(),'time':time.monotonic(),**data}));t.replace(p)
mark('child_started')
deadline=time.monotonic()+25
while not (state/'release').exists():
    if time.monotonic()>deadline:
        mark('child_timed_out');sys.exit(77)
    time.sleep(.005)
mark('child_completed')
# Deliberately no stdout write: a killed backend must not create an artificial
# BrokenPipe error in this independent natural-exit probe.
'''

DETACHED_TPE_SYNTHETIC_LAUNCHER = r'''
import json,os,signal,sys,time
from pathlib import Path
state=Path(os.environ['SYNTHETIC_STATE'])
if sys.argv[1]=='ignore': signal.signal(signal.SIGINT,signal.SIG_IGN)
(state/'inherited_signal.json').write_text(json.dumps({'handler':str(signal.getsignal(signal.SIGINT)),'time':time.monotonic()}))
os.execv(sys.executable,[sys.executable,'-B','-m','uvicorn','app:app','--host','127.0.0.1','--port',sys.argv[2],'--log-level','info'])
'''

SECOND_SIGINT_SYNTHETIC_SERVER = r'''
import concurrent.futures, faulthandler, os, pathlib, signal, sys, threading, time
import uvicorn
D = pathlib.Path(sys.argv[1])
signal.signal(signal.SIGINT, signal.default_int_handler)
faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='synthetic-owned-pool')
def worker():
    (D/'worker_started').write_text(str(os.getpid()))
    end = time.monotonic()+20
    while not (D/'release').exists() and time.monotonic()<end:
        time.sleep(0.02)
    if not (D/'release').exists():
        raise AssertionError('synthetic gate timed out')
    (D/'worker_finished').write_text('finished normally')
async def app(scope, receive, send):
    assert scope['type']=='http'
    pool.submit(worker)
    await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/plain')]})
    await send({'type':'http.response.body','body':b'{"accepted": true}'})
class Server(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets)
        (D/'port').write_text(str(self.servers[0].sockets[0].getsockname()[1]))
try:
    Server(uvicorn.Config(app, host='127.0.0.1', port=0, loop='asyncio', lifespan='off', log_level='info')).run()
except KeyboardInterrupt:
    (D/'caught_keyboard_interrupt').write_text('caught')
finally:
    (D/'run_returned').write_text(repr(signal.getsignal(signal.SIGINT)))
'''


class RuntimeDetachedThreadDrainTests(unittest.TestCase):
    """HTTP already returned; custom executor jobs must still drain naturally."""

    def _dependencies(self):
        import fastapi
        import uvicorn
        self.assertTrue(sys.platform.startswith('linux'), 'real Linux process semantics required')
        requirements = (Path(__file__).resolve().parents[2] / 'requirements.txt').read_text().splitlines()
        for name, module in (('uvicorn', uvicorn), ('fastapi', fastapi)):
            pins = [line.split('==', 1)[1].strip() for line in requirements
                    if line.startswith(name + '==')]
            self.assertEqual(pins, [module.__version__])
        # Derive only trusted, imported package roots; no arbitrary environment
        # variables, proxy URLs, credentials, or UVICORN_* overrides are copied.
        return {'PYTHONPATH': os.pathsep.join(sorted({
            str(Path(module.__file__).resolve().parents[1]) for module in (uvicorn, fastapi)})),
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1', 'LANG': 'C.UTF-8'}

    def _wait(self, predicate, description, log, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.005)
        self.fail(f'timed out waiting for {description}; synthetic server log: {log.read_text()}')

    def _archive(self, state):
        artifact_dir = os.environ.get('ASTRA_TEST_ARTIFACT_DIR')
        if artifact_dir:
            target = Path(artifact_dir) / self._testMethodName
            target.mkdir(parents=True, exist_ok=True)
            for source in state.iterdir():
                if source.is_file():
                    (target / source.name).write_bytes(source.read_bytes())

    def _exercise_detached(self, signum, inherited):
        import ctypes
        environment = self._dependencies()
        temporary = tempfile.TemporaryDirectory(prefix='astra-detached-tpe-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        state = root / 'state'
        state.mkdir()
        for filename, program in (('app.py', DETACHED_TPE_SYNTHETIC_APP),
                                  ('child.py', DETACHED_TPE_SYNTHETIC_CHILD),
                                  ('launcher.py', DETACHED_TPE_SYNTHETIC_LAUNCHER)):
            (root / filename).write_text(program)
        environment['SYNTHETIC_STATE'] = str(state)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        # This flag affects only this test-runner process and is restored below.
        # It allows the deliberate TERM counterexample's own orphan to be
        # reaped after natural exit, rather than leaving a zombie for PID 1.
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        process = None
        child_pid = None
        orphan_reaped = False
        info = {'signal': int(signum), 'inherited_sigint': inherited, 'forced_cleanup': False}
        log_path = state / 'server.log'
        read = lambda name: json.loads((state / (name + '.json')).read_text())
        with log_path.open('w') as log:
            try:
                process = subprocess.Popen([sys.executable, '-B', str(root / 'launcher.py'), inherited, str(port)],
                    cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
                info['backend_pid'] = process.pid
                self._wait(lambda: (state / 'app_started.json').exists(), 'app startup', log_path)

                def ready():
                    try:
                        return uvicorn_test_request(port, '/health')[0] == 200
                    except OSError:
                        return False

                self._wait(ready, 'listener startup', log_path)
                self.assertEqual(uvicorn_test_request(port, '/detach'),
                    (200, {'returned': True, 'cancelled': [False, False], 'running': [True, True]}))
                info['http_200_before_signal'] = True
                child = read('child_started')
                child_pid = child['pid']
                info['child_pid'] = child_pid
                self.assertEqual(child['ppid'], process.pid)
                self.assertFalse(read('thread_started')['daemon'])
                self.assertFalse(read('child_thread_started')['daemon'])
                inherited_handler = read('inherited_signal')['handler']
                if inherited == 'ignore':
                    self.assertEqual(inherited_handler, str(signal.SIG_IGN))
                else:
                    self.assertIn('default_int_handler', inherited_handler)
                self.assertFalse((state / 'thread_completed.json').exists())
                self.assertFalse((state / 'child_thread_completed.json').exists())
                process.send_signal(signum)  # Exact owned PID, never process group.
                self._wait(lambda: uvicorn_test_refused(port), 'listener closure', log_path)
                self._wait(lambda: (state / 'app_shutdown.json').exists(), 'lifespan shutdown', log_path)
                self.assertTrue(uvicorn_test_alive(child_pid))
                self.assertFalse((state / 'thread_completed.json').exists())
                self.assertFalse((state / 'child_thread_completed.json').exists())
                if signum == signal.SIGTERM:
                    info['backend_returncode'] = process.wait(timeout=5)
                    self.assertEqual(info['backend_returncode'], -signal.SIGTERM)
                    self.assertTrue(uvicorn_test_alive(child_pid))
                    self.assertFalse((state / 'thread_completed.json').exists())
                    info['detached_thread_completion_lost'] = True
                    (state / 'release').touch()
                    self._wait(lambda: (state / 'child_completed.json').exists(), 'orphan natural completion', log_path)
                    got, status = os.waitpid(child_pid, 0)
                    orphan_reaped = True
                    self.assertEqual(got, child_pid)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                    info['orphan_child_natural_returncode'] = 0
                    self.assertFalse((state / 'child_thread_completed.json').exists())
                else:
                    # Uvicorn's application shutdown is already complete, but
                    # CPython must remain alive while joining the custom pool.
                    time.sleep(0.20)
                    self.assertIsNone(process.poll(), 'single SIGINT lost detached thread work')
                    self.assertTrue(uvicorn_test_alive(child_pid))
                    info['backend_alive_after_lifespan_while_detached_work_pending'] = True
                    info['release_time'] = time.monotonic()
                    (state / 'release').touch()
                    self._wait(lambda: (state / 'thread_completed.json').exists(), 'detached thread completion', log_path)
                    self._wait(lambda: (state / 'child_thread_completed.json').exists(), 'subprocess wait completion', log_path)
                    self.assertEqual(read('child_thread_completed')['returncode'], 0)
                    info['backend_returncode'] = process.wait(timeout=10)
                    self.assertIn(info['backend_returncode'], (0, 130, -signal.SIGINT))
                    self.assertFalse(uvicorn_test_alive(child_pid))
                    self.assertFalse((state / 'thread_timed_out.json').exists())
                    self.assertFalse((state / 'child_timed_out.json').exists())
                    self.assertGreater(read('thread_completed')['time'], info['release_time'])
                    info['detached_threads_completed'] = True
                    info['child_natural_returncode'] = 0
                info['passed'] = True
            finally:
                (state / 'release').touch()
                if process is not None and process.poll() is None:
                    info['forced_cleanup'] = True
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if child_pid is not None and not orphan_reaped:
                    try:
                        os.waitpid(child_pid, 0)
                    except ChildProcessError:
                        pass  # The backend normally reaped its own child.
                self.assertEqual(libc.prctl(36, previous.value, 0, 0, 0), 0)
                (state / 'result.json').write_text(json.dumps(info, indent=2) + '\n')
                self._archive(state)
        self.assertFalse(info['forced_cleanup'])

    def test_sigterm_default_loses_detached_executor_threads(self):
        self._exercise_detached(signal.SIGTERM, 'default')

    def test_sigterm_inherited_ignore_loses_detached_executor_threads(self):
        self._exercise_detached(signal.SIGTERM, 'ignore')

    def test_single_sigint_default_drains_detached_executor_threads(self):
        self._exercise_detached(signal.SIGINT, 'default')

    def test_single_sigint_inherited_ignore_drains_detached_executor_threads(self):
        self._exercise_detached(signal.SIGINT, 'ignore')

    def test_second_sigint_can_interrupt_interpreter_join_and_lose_work(self):
        """Negative regression: a signal with uncertain delivery must not retry."""
        environment = self._dependencies()
        temporary = tempfile.TemporaryDirectory(prefix='astra-second-sigint-')
        self.addCleanup(temporary.cleanup)
        state = Path(temporary.name)
        script = state / 'synthetic_server.py'
        script.write_text(SECOND_SIGINT_SYNTHETIC_SERVER)
        log_path = state / 'server.log'
        process = None
        info = {'forced_cleanup': False}
        with log_path.open('w') as log:
            try:
                process = subprocess.Popen([sys.executable, '-B', str(script), str(state)],
                    cwd=state, env=environment, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
                self._wait(lambda: (state / 'port').exists(), 'loopback port', log_path)
                self.assertEqual(uvicorn_test_request(int((state / 'port').read_text()), '/'),
                                 (200, {'accepted': True}))
                self._wait(lambda: (state / 'worker_started').exists(), 'custom worker', log_path)
                process.send_signal(signal.SIGINT)
                self._wait(lambda: (state / 'run_returned').exists(), 'server return', log_path)
                self.assertIn('default_int_handler', (state / 'run_returned').read_text())
                self.assertIsNone(process.poll())
                self.assertFalse((state / 'worker_finished').exists())
                # Capture the real blocking stack instead of racing a guessed
                # sleep. SIGUSR1 only asks faulthandler for diagnostics here.
                last_diagnostic = [0.0]
                def interpreter_join_seen():
                    if '_python_exit' in log_path.read_text():
                        return True
                    now = time.monotonic()
                    if now - last_diagnostic[0] >= 0.1:
                        process.send_signal(signal.SIGUSR1)
                        last_diagnostic[0] = now
                    return False
                self._wait(interpreter_join_seen, 'interpreter pool join', log_path)
                stack = log_path.read_text()
                self.assertIn('_shutdown', stack)
                self.assertIn('join', stack)
                self.assertFalse((state / 'worker_finished').exists())
                process.send_signal(signal.SIGINT)  # Deliberate unsafe counterexample, never helper policy.
                info['returncode'] = process.wait(timeout=5)
                self.assertFalse((state / 'worker_finished').exists())
                self.assertIn('KeyboardInterrupt', log_path.read_text())
                info['interpreter_join_confirmed'] = True
                info['completion_lost_after_second_sigint'] = True
                info['passed'] = True
            finally:
                (state / 'release').touch()
                if process is not None and process.poll() is None:
                    info['forced_cleanup'] = True
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                (state / 'result.json').write_text(json.dumps(info, indent=2) + '\n')
                self._archive(state)
        self.assertFalse(info['forced_cleanup'])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--synthetic-worker':
        synthetic_worker(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5])
    else:
        unittest.main()
