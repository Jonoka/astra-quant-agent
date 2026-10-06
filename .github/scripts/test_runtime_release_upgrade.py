"""Synthetic safety gates; execute on GitHub Actions, never the VPS."""
import copy
import importlib.util
import os
import sqlite3
import sys
import io
import tarfile
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
        (source / "data/orders.db-wal").write_bytes(b"committed-wal-fixture")
        (source / "frontend/public/images").symlink_to("../../docs/images", target_is_directory=True)
        expected = upgrade.tree_manifest(source)
        snapshot = obj.op / "snapshot"
        obj.copy(source, snapshot)
        self.assertEqual(upgrade.tree_manifest(snapshot), expected)
        self.assertEqual(upgrade.tree_manifest(source), expected)


if __name__ == "__main__":
    unittest.main()
