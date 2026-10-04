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
        return obj

    def test_only_reviewed_gateway_reasoning_column_allowed(self):
        before = {'data/astra_gateway.db': {'tables': {'model_calls': {
            'columns': [['id', 'INTEGER', 0, None, 1]], 'rows': 1}}, 'indexes': []}}
        after = copy.deepcopy(before)
        columns = after['data/astra_gateway.db']['tables']['model_calls']['columns']
        columns.append(['reasoning_tokens', 'INTEGER', 0, '0', 0])
        upgrade.compatible_databases(before, after)
        upgrade.compatible_databases(after, after)  # Previous-version recovery keeps new column.
        for bad in (['reasoning_tokens', 'INTEGER', 1, '0', 0],
                    ['reasoning_tokens', 'TEXT', 0, '0', 0],
                    ['reasoning_tokens', 'INTEGER', 0, None, 0]):
            columns[-1] = bad
            with self.assertRaisesRegex(upgrade.GateError, 'unknown_database_migration'):
                upgrade.compatible_databases(before, after)

    def test_gateway_source_contract_rejects_other_sql_or_migrations(self):
        old, new = self.root / 'old.py', self.root / 'new.py'
        schema = 'CREATE TABLE model_calls (\n  output_tokens INTEGER,\n  total_tokens INTEGER\n);'
        migrations = (('cached_tokens', 'INTEGER'),)
        old.write_text('SCHEMA = ' + repr(schema) + '\nMIGRATION_COLUMNS = ' + repr(migrations))
        reviewed = schema.replace('  output_tokens INTEGER,\n',
                                  '  output_tokens INTEGER,\n  reasoning_tokens INTEGER DEFAULT 0,\n')
        new.write_text('SCHEMA = ' + repr(reviewed) + '\nMIGRATION_COLUMNS = ' +
                       repr((('reasoning_tokens', 'INTEGER DEFAULT 0'), *migrations)))
        upgrade.gateway_source_compatibility(old, new)
        new.write_text(new.read_text().replace('DEFAULT 0', 'DEFAULT 1'))
        with self.assertRaisesRegex(upgrade.GateError, 'gateway_schema_changed'):
            upgrade.gateway_source_compatibility(old, new)

    def test_stage_identity_uses_corrected_fork_sha_and_pinned_ancestor(self):
        evidence = {'SOURCE_SHA': 'a' * 40, 'PREVIOUS_SHA': upgrade.PREVIOUS,
                    'UPSTREAM_SHA': upgrade.UPSTREAM, 'SOURCE_REPOSITORY': 'Jonoka/astra-quant-agent',
                    'UPSTREAM_REPOSITORY': '0xethanq/astra-quant-agent', 'SOURCE_VERSION': 'v8.6.1',
                    'platform': 'linux/amd64'}
        self.assertEqual(staging.source_identity(evidence), 'a' * 40)
        for key, bad in (('SOURCE_SHA', upgrade.UPSTREAM), ('UPSTREAM_SHA', 'b' * 40),
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
                  'io.jonoka.astra.build-recipe-sha256': upgrade.sha(b'reviewed-recipe')}
        metadata = {'Config': {'Labels': labels}, 'RepoDigests': [obj.image],
                    'Os': 'linux', 'Architecture': 'amd64', 'Id': 'image-id'}
        with patch.object(upgrade, 'run', return_value=upgrade.json.dumps([metadata]).encode()):
            self.assertEqual(obj.image_metadata(), 'image-id')
        for key in ('io.jonoka.astra.upstream-revision', 'io.jonoka.astra.council-completion-patch',
                    'io.jonoka.astra.build-recipe-sha256'):
            changed = copy.deepcopy(metadata)
            changed['Config']['Labels'][key] = 'incorrect'
            with patch.object(upgrade, 'run', return_value=upgrade.json.dumps([changed]).encode()), \
                    self.assertRaisesRegex(upgrade.GateError, 'image_patch_provenance'):
                obj.image_metadata()

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
        raw = '策略\nsecond\n'.encode()
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
        old = {'services': {s: {'image': upgrade.OLD_IMAGE, 'volumes': [
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
        before = ("# retained\r\nservices:\r\n  backend:\r\n    image: '" + upgrade.OLD_IMAGE +
                  "' # backend\r\n  gateway:\r\n    image: \"" + upgrade.OLD_IMAGE + "\"\r\n").encode()
        path.write_bytes(before)
        image = "ghcr.io/jonoka/astra-quant-agent@sha256:" + "a" * 64
        upgrade.patch_override(path, upgrade.OLD_IMAGE, image)
        self.assertEqual(path.read_bytes(), before.replace(upgrade.OLD_IMAGE.encode(), image.encode()))

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

    def test_no_session_still_checks_local_and_public_auth_protection(self):
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
        with patch.object(upgrade, "request", side_effect=response):
            result = obj.endpoints(False)
        self.assertEqual(result["authenticated_session_probe"], "unexercised_no_supplied_session")
        self.assertIsNone(result["authenticated_identity"])
        protected = [c for c in calls if c[1].endswith("/auth/me")]
        self.assertEqual(len(protected), 2)
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
