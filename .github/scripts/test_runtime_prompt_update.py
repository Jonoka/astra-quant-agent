"""Hosted synthetic fixtures; never run this suite on production."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import runtime_prompt_update as h


def env():
    return b'SECRET=opaque-$`value\r\n# preserve\r\n' + b''.join(
        k.encode() + b' = ' + v[0].encode() + b'  # unchanged comment\r\n'
        for k, v in h.CHANGES.items()) + b'OTHER=unchanged'


class PatchTests(unittest.TestCase):
    def test_exact_six_preserve_secret_and_crlf(self):
        before = env(); after = h.patch_env(before)
        expected = before
        for k, (old, new) in h.CHANGES.items():
            expected = expected.replace(k.encode() + b' = ' + old.encode(), k.encode() + b' = ' + new.encode())
        self.assertEqual(after, expected)

    def test_missing_duplicate_and_changed_rejected(self):
        for value in (b'', env() + b'\nASTRA_TIME_STOP_HOURS=8',
                      env() + b'\n  export ASTRA_TIME_STOP_HOURS=8',
                      env().replace(b'ASTRA_TIME_STOP_HOURS = 8', b'ASTRA_TIME_STOP_HOURS = 9')):
            with self.assertRaises(h.GateError): h.patch_env(value)

    def test_lock_contention(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lock'
            with h.lock(path):
                with self.assertRaises(h.GateError):
                    with h.lock(path): pass

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'link'; p.symlink_to('/tmp')
            with self.assertRaises(h.GateError): h.plain(p)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / 'root'; self.root.mkdir()
        self.backups = base / 'backups'; self.backups.mkdir(mode=0o700)
        (self.root / 'data').mkdir(); (self.root / 'scripts').mkdir()
        self.old = b'{"old":true}'
        self.new = json.dumps({'active_profile_id':'allpattern_swing', 'profiles':{'allpattern_swing':{}}}).encode()
        for p in h.GUARDS: (self.root / p).write_bytes(b'immutable')
        (self.root / '.env').write_bytes(env()); (self.root / '.env').chmod(0o600)
        (self.root / h.FILES[1]).write_bytes(self.old)
        (self.root / 'data/orders.db').write_bytes(b'new-order-state')
        self.prompt = base / 'prompt'; self.prompt.write_bytes(self.new)
        self.patches = [patch.object(h, 'ROOT', self.root), patch.object(h, 'BACKUPS', self.backups),
                        patch.object(h, 'OLD_SHA', h.sha(self.old)), patch.object(h, 'PROMPT_SHA', h.sha(self.new))]
        for p in self.patches: p.start()
        self.u = h.Update(self.backups / 'prompts-v8.5.1-test')
        self.calls = []
        self.u.runtime = lambda *a: None
        self.u.compose = lambda *a: self.calls.append(a)
        self.u.stop = lambda: self.calls.append(('stop',))
        self.u.start = lambda: self.calls.append(('start',))
        self.u.smoke = lambda *a: None
        self.u.prepare(self.prompt)

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def test_apply_and_exact_rollback_without_data_restore(self):
        self.u.execute()
        self.assertEqual((self.root / '.env').read_bytes(), h.patch_env(env()))
        self.assertEqual((self.root / h.FILES[1]).read_bytes(), self.new)
        (self.root / 'data/orders.db').write_bytes(b'newer-order')
        self.u.execute(True)
        self.assertEqual((self.root / '.env').read_bytes(), env())
        self.assertEqual((self.root / h.FILES[1]).read_bytes(), self.old)
        self.assertEqual((self.root / 'data/orders.db').read_bytes(), b'newer-order')
        self.assertEqual(h.metadata(self.root / '.env')['mode'], 0o600)

    def test_pre_stop_drift_and_overlay_rejected(self):
        (self.root / '.env').write_bytes(env() + b'\nNEW=user-edit')
        with self.assertRaises(h.GateError): self.u.execute()
        self.assertNotIn(('stop',), self.calls)

    def test_rollback_refuses_new_edits(self):
        self.u.execute()
        (self.root / '.env').write_bytes(h.patch_env(env()) + b'\nNEW=user-edit')
        before = len(self.calls)
        with self.assertRaises(h.GateError): self.u.execute(True)
        self.assertNotIn(('stop',), self.calls[before:])

    def test_overlay_rejected(self):
        (self.root / 'data/prompt_library.local.json').write_bytes(b'{}')
        with self.assertRaises(h.GateError): self.u.execute()

    def test_smoke_failure_recovers_exact_config(self):
        def fail(updated):
            if updated: raise h.GateError('synthetic_smoke_failure')
        self.u.smoke = fail
        with self.assertRaises(h.GateError): self.u.execute()
        self.assertEqual((self.root / '.env').read_bytes(), env())
        self.assertEqual(self.u.load()['phase'], 'rolled_back')

    def test_smoke_failure_stops_before_restoring(self):
        def fail(updated):
            self.calls.append(('smoke', updated))
            if updated: raise h.GateError('synthetic_smoke_failure')
        self.u.smoke = fail
        with self.assertRaises(h.GateError): self.u.execute()
        failure = self.calls.index(('smoke', True))
        self.assertEqual(self.calls[failure + 1:failure + 3], [('stop',), ('start',)])

    def test_backup_or_candidate_permissions_rejected_before_stop(self):
        for filename in ('original-0', 'installed-1'):
            p = self.u.op / filename
            p.chmod(0o644)
            before = len(self.calls)
            with self.assertRaises(h.GateError): self.u.execute()
            self.assertNotIn(('stop',), self.calls[before:])
            p.chmod(0o600)

    def test_post_stop_drift_never_overwritten(self):
        newer = env() + b'\nNEW=user-edit'
        def stop():
            self.calls.append(('stop',))
            (self.root / '.env').write_bytes(newer)
        self.u.stop = stop
        with self.assertRaises(h.GateError): self.u.execute()
        self.assertEqual((self.root / '.env').read_bytes(), newer)
        self.assertNotIn(('start',), self.calls)

    def test_start_recreates_only_named_services_without_build(self):
        h.Update.start(self.u)
        self.assertIn(('up', '-d', '--no-build', '--no-deps', '--force-recreate',
                       '--wait', '--wait-timeout', '180', 'backend', 'gateway'), self.calls)


class OfficialFixture(unittest.TestCase):
    def test_legacy_rollback_reader_on_new_source(self):
        source = Path(os.environ['OFFICIAL_SOURCE']).resolve()
        baseline = Path(os.environ['LEGACY_SOURCE']).resolve() / 'data/prompt_library.json'
        self.assertEqual(h.sha(baseline.read_bytes()), h.OLD_SHA)
        # Real v8.5.1 reader with raw v8.3.1 baseline and original risk values.
        # Rollback restores existing configuration, not a new UI configuration write.
        h.run('python', '-c', '''import os, sys, json
from pathlib import Path
sys.path.insert(0, sys.argv[1])
values=json.loads(sys.argv[3])
os.environ.update(values)
from scripts import prompt_library as p
p.BASELINE_FILE=Path(sys.argv[2])
p.LOCAL_FILE=Path(sys.argv[2]).with_name('absent-rollback-overlay.json')
from astra_backend.risk_config import process_values, normalize
assert all(float(process_values()[k]) == float(v) for k,v in values.items())
try: normalize(values)
except ValueError: pass
else: raise AssertionError('legacy below-stop TP1 unexpectedly accepted for new writes')
assert p.load_library()['active_profile_id']=='stable'
profile=p.active_profile()
assert profile['trading_system'] and profile['trading_user']
assert p.render_variables(profile['trading_system'], {'risk_budget':'synthetic-risk'})
base=p.base_template_text('trading_system')
assert base.strip()
rendered=p.apply_module_layout(base, profile, 'trading_system', 'rollback fixture')
assert base.strip() in rendered
print('legacy_rollback_reader_ok')
''', source, baseline, json.dumps({k:v[0] for k,v in h.CHANGES.items()}))

    def test_pinned_reader_validator_and_render(self):
        source = Path(os.environ['OFFICIAL_SOURCE']).resolve()
        self.assertEqual(h.sha((source / 'data/prompt_library.json').read_bytes()), h.PROMPT_SHA)
        # Isolated process avoids module/path coupling with helper tests.
        h.run('python', '-c', '''import os, sys
sys.path.insert(0, sys.argv[1])
from scripts import prompt_library as p
from astra_backend.risk_config import normalize, process_values
import json
values=json.loads(sys.argv[2])
assert set(normalize(values)) == set(values)
assert all(float(process_values()[k]) == float(v) for k,v in values.items())
assert p.load_library()['active_profile_id']=='allpattern_swing'
profile=p.active_profile()
assert p.validate_profile(profile)['valid']
assert profile['trading_system'] and profile['trading_user']
assert p.render_variables(profile['trading_system'], {'risk_budget':'synthetic-risk'})
print('official_fixture_ok')
''', source, json.dumps({k:v[1] for k,v in h.CHANGES.items()}))


if __name__ == '__main__': unittest.main()
