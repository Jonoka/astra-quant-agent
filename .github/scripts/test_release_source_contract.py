"""Offline release contract refusal tests; mocked history is not real Git proof."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import verify_deployment_source as guard

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(os.environ.get('ASTRA_COLLECTION_BASELINE', str(ROOT)))
REVIEWED = Path(os.environ.get('ASTRA_COLLECTION_REVIEWED_SOURCE', str(ROOT)))


class ReleaseSourceContractTests(unittest.TestCase):
    """Git responses are synthetic; bytes are the preserved source snapshots."""
    @classmethod
    def setUpClass(cls):
        cls.raw = (ROOT / guard.RELEASE_CONTRACT_PATH).read_bytes()
        cls.contract = json.loads(cls.raw)
        cls.revision = 'b' * 40
        cls.collection = guard.collection_contract()
        cls.cache = {}

    def verify(self, *, contract=None, revision=None, head=None, trees=None,
               missing_ancestor=None, baseline_delta=None, reviewed_delta=None, release_delta=None,
               application_delta=None, dirty=b'', bad_mode=None, mutation=None, source=None,
               existing_new=None, missing_predecessor=None, upstream=guard.UPSTREAM_SHA):
        revision = self.revision if revision is None else revision
        trees = trees or {}
        def git(command, **_kwargs):
            verb, args = command[3], command[4:]
            if verb == 'rev-parse':
                value = head or revision
                if args != ['HEAD']:
                    value = trees.get(args[0], {guard.COLLECTION_BASELINE + '^{tree}': guard.COLLECTION_BASELINE_TREE,
                                              guard.REVIEWED_COLLECTION_SHA + '^{tree}': guard.REVIEWED_COLLECTION_TREE}[args[0]])
                return SimpleNamespace(stdout=(value + '\n').encode())
            if verb == 'merge-base':
                if missing_ancestor == tuple(args[-2:]):
                    raise subprocess.CalledProcessError(1, command)
                return SimpleNamespace(stdout=b'')
            if verb == 'diff':
                pair = args[-2:]
                if pair == [guard.UPSTREAM_SHA, guard.COLLECTION_BASELINE]:
                    result = guard.APPLICATION_PATCH if baseline_delta is None else baseline_delta
                elif pair == [guard.COLLECTION_BASELINE, guard.REVIEWED_COLLECTION_SHA]:
                    result = set(self.contract['reviewed_paths']) if reviewed_delta is None else reviewed_delta
                elif pair == [guard.REVIEWED_COLLECTION_SHA, revision]:
                    result = guard.RELEASE_INCREMENT_PATHS if release_delta is None else release_delta
                else:
                    result = guard.APPLICATION_PATCH | set(self.collection['application']) if application_delta is None else application_delta
                return SimpleNamespace(stdout=b'\0'.join(x.encode() for x in sorted(result)))
            if verb == 'show':
                rev, relative = args[0].split(':', 1)
                location = {guard.COLLECTION_BASELINE: BASE, guard.REVIEWED_COLLECTION_SHA: REVIEWED, revision: ROOT}[rev]
                key = (str(location), relative)
                if key not in self.cache:
                    self.cache[key] = (location / relative).read_bytes()
                raw = self.cache[key]
                if mutation == (rev, relative):
                    raw += b'\n# synthetic unreviewed bytes\n'
                return SimpleNamespace(stdout=raw)
            if verb == 'ls-tree':
                relative = args[-1]
                if args[0] == '--name-only':
                    location = BASE if args[1] == guard.COLLECTION_BASELINE else REVIEWED
                    present = (location / relative).exists()
                    if relative == existing_new:
                        present = True
                    if relative == missing_predecessor:
                        present = False
                    return SimpleNamespace(stdout=(relative + '\n').encode() if present else b'')
                mode = '100755' if bad_mode == (args[1], relative) else '100644'
                return SimpleNamespace(stdout=(mode + ' blob ' + 'd' * 40 + '\t' + relative + '\0').encode())
            if verb == 'status':
                return SimpleNamespace(stdout=dirty)
            raise AssertionError('Unexpected synthetic git call: ' + str(command))
        with patch.object(guard.subprocess, 'run', side_effect=git), patch('sys.stdout', new_callable=io.StringIO):
            if contract is None:
                guard.verify_release_source(source or ROOT, upstream, revision)
            else:
                with patch.object(guard, 'release_contract', return_value=contract):
                    guard.verify_release_source(source or ROOT, upstream, revision)

    def test_exact_release_fixture_passes_without_claiming_history_proof(self):
        self.verify()

    def test_missing_release_contract_is_not_silently_accepted(self):
        with patch.object(Path, 'read_bytes', side_effect=FileNotFoundError('fixture contract absent')):
            with self.assertRaises(FileNotFoundError):
                guard.release_contract()

    def test_contract_removal_cannot_downgrade_release_delta_to_collection_pr(self):
        # The old PR path remains strictly bound to 61 paths. Even with the new
        # manifest removed, the release tests/workflow are unreviewed CI deltas.
        from test_verify_deployment_source import CollectionSourceGuardTests
        old = CollectionSourceGuardTests(); old.setUp()
        downgraded = old.delta | (guard.RELEASE_INCREMENT_PATHS - {guard.RELEASE_CONTRACT_PATH})
        with self.assertRaisesRegex(AssertionError, 'collection CI changes'):
            old.verify(delta=downgraded)

    def test_reviewed_pr_is_not_release(self):
        with self.assertRaisesRegex(AssertionError, 'PR alone is not a release'):
            self.verify(revision=guard.REVIEWED_COLLECTION_SHA)

    def test_wrong_upstream_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'Wrong release upstream'):
            self.verify(upstream='c' * 40)

    def test_unpinned_revision_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'Unpinned release source'):
            self.verify(revision='HEAD')

    def test_wrong_checked_out_head_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'Wrong release source commit'):
            self.verify(head='c' * 40)

    def test_both_exact_anchor_trees_are_required(self):
        for rev in (guard.COLLECTION_BASELINE, guard.REVIEWED_COLLECTION_SHA):
            with self.subTest(rev=rev), self.assertRaisesRegex(AssertionError, 'Wrong release anchor tree'):
                self.verify(trees={rev + '^{tree}': 'c' * 40})

    def test_all_four_ancestry_relations_are_required(self):
        pairs = ((guard.UPSTREAM_SHA, guard.COLLECTION_BASELINE), (guard.PREVIOUS_SHA, guard.COLLECTION_BASELINE),
                 (guard.COLLECTION_BASELINE, guard.REVIEWED_COLLECTION_SHA), (guard.REVIEWED_COLLECTION_SHA, self.revision))
        for pair in pairs:
            with self.subTest(pair=pair), self.assertRaises(subprocess.CalledProcessError):
                self.verify(missing_ancestor=pair)

    def test_baseline_retained_paths_are_unchanged(self):
        with self.assertRaisesRegex(AssertionError, 'baseline lost retained patch'):
            self.verify(baseline_delta=guard.APPLICATION_PATCH - {'scripts/okx_taker.py'})

    def test_every_legacy_pin_remains_checked_at_original_baseline(self):
        for relative in {**guard.TAKER_PATCH_SHA256, **guard.ALPHA_PATCH_SHA256}:
            with self.subTest(relative=relative), self.assertRaisesRegex(AssertionError, 'Baseline reviewed source changed'):
                self.verify(mutation=(guard.COLLECTION_BASELINE, relative))

    def test_original_application_baseline_bytes_are_checked(self):
        with self.assertRaisesRegex(AssertionError, 'Collection baseline source changed'):
            self.verify(mutation=(guard.COLLECTION_BASELINE, 'astra_gateway/store.py'))

    def test_exact_reviewed_61_path_set_is_required(self):
        paths = set(self.contract['reviewed_paths'])
        for delta in (paths - {'astra_backend/collection.py'}, paths | {'unreviewed.py'}):
            with self.subTest(delta=delta), self.assertRaisesRegex(AssertionError, 'anchor delta changed'):
                self.verify(reviewed_delta=delta)

    def test_all_61_reviewed_anchor_bytes_are_required(self):
        for relative in self.contract['reviewed_paths']:
            with self.subTest(relative=relative), self.assertRaisesRegex(AssertionError, 'Reviewed anchor bytes changed'):
                self.verify(mutation=(guard.REVIEWED_COLLECTION_SHA, relative))

    def test_anchor_modes_are_required(self):
        for relative in self.contract['reviewed_paths']:
            with self.subTest(relative=relative), self.assertRaisesRegex(AssertionError, 'Git mode or type'):
                self.verify(bad_mode=(guard.REVIEWED_COLLECTION_SHA, relative))

    def test_every_release_increment_path_is_required(self):
        for relative in guard.RELEASE_INCREMENT_PATHS:
            with self.subTest(relative=relative), self.assertRaisesRegex(AssertionError, 'release increment'):
                self.verify(release_delta=guard.RELEASE_INCREMENT_PATHS - {relative})

    def test_extra_release_paths_are_rejected(self):
        for relative in ('scripts/unreviewed.py', '.github/scripts/unsafe.py', '.github/workflows/unreviewed.yml'):
            with self.subTest(relative=relative), self.assertRaisesRegex(AssertionError, 'release increment'):
                self.verify(release_delta=guard.RELEASE_INCREMENT_PATHS | {relative})

    def test_missing_retained_application_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'Missing retained patch'):
            self.verify(application_delta=(guard.APPLICATION_PATCH | set(self.collection['application'])) - {'scripts/okx_taker.py'})

    def test_release_modes_are_required_for_all_bound_paths(self):
        for relative in set(self.contract['reviewed_paths']) | guard.RELEASE_INCREMENT_PATHS:
            with self.subTest(relative=relative), self.assertRaisesRegex(AssertionError, 'Git mode or type'):
                self.verify(bad_mode=(self.revision, relative))

    def test_dirty_and_untracked_release_source_is_rejected(self):
        for dirty in (b' M Dockerfile\n', b'?? unreviewed.py\n'):
            with self.subTest(dirty=dirty), self.assertRaisesRegex(AssertionError, 'source changed'):
                self.verify(dirty=dirty)

    def test_all_final_tree_bytes_are_bound_including_contract_and_verifier(self):
        for relative in set(self.contract['reviewed_paths']) | guard.RELEASE_INCREMENT_PATHS:
            with self.subTest(relative=relative), self.assertRaisesRegex(AssertionError, '(Unreviewed release tree bytes|Release trust-root working bytes changed)'):
                self.verify(mutation=(self.revision, relative))

    def test_changed_release_predecessor_is_rejected(self):
        relative = '.github/workflows/build-release-image.yml'
        with self.assertRaisesRegex(AssertionError, 'Release predecessor bytes changed'):
            self.verify(mutation=(guard.REVIEWED_COLLECTION_SHA, relative))

    def test_new_release_path_must_be_absent_from_anchor(self):
        for relative in (guard.RELEASE_CONTRACT_PATH, '.github/scripts/test_release_source_contract.py'):
            with self.subTest(relative=relative), self.assertRaisesRegex(AssertionError, '(exists|already exists)'):
                self.verify(existing_new=relative)

    def test_release_predecessor_cannot_disappear(self):
        with self.assertRaisesRegex(AssertionError, 'Release predecessor path missing'):
            self.verify(missing_predecessor='.github/workflows/build-release-image.yml')

    def test_only_two_reviewed_roots_have_self_hash_exemptions(self):
        self.assertEqual(set(self.contract['self_hash_exemptions']), {guard.RELEASE_CONTRACT_PATH, ".github/scripts/verify_deployment_source.py"})
        self.assertNotIn('.github/scripts/verify_deployment_source.py', self.contract['release_paths'])
        self.assertIn('.github/workflows/build-release-image.yml', self.contract['release_paths'])

    def test_contract_rejects_new_roots_paths_modes_and_anchor_changes(self):
        mutations = []
        for key in ('upstream', 'baseline', 'baseline_tree', 'reviewed_commit', 'reviewed_tree'):
            changed = copy.deepcopy(self.contract); changed[key] = '0' * 40; mutations.append(changed)
        changed = copy.deepcopy(self.contract); changed['self_hash_exemptions'].append('.github/scripts/run_deployment_preflight.py'); mutations.append(changed)
        changed = copy.deepcopy(self.contract); changed['release_paths']['.github/unreviewed.py'] = next(iter(changed['release_paths'].values())); mutations.append(changed)
        changed = copy.deepcopy(self.contract); changed['reviewed_paths']['astra_backend/collection.py']['mode'] = '100755'; mutations.append(changed)
        for changed in mutations:
            raw = json.dumps(changed).encode()
            with self.subTest(changed=changed), patch.object(Path, 'read_bytes', return_value=raw), self.assertRaises(AssertionError):
                guard.release_contract()

    def test_original_collection_contract_is_byte_identical_to_reviewed_anchor(self):
        relative = '.github/scripts/collection_source_contract.json'
        self.assertEqual((ROOT / relative).read_bytes(), (REVIEWED / relative).read_bytes())

    def test_release_preflight_makes_collection_suites_mandatory(self):
        import run_deployment_preflight as preflight
        self.assertEqual(preflight.SUITES['test_collection_status_compatibility.py'], 17)
        self.assertEqual(preflight.SUITES['test_collection_regression_gate.py'], 10)
        source = (ROOT / '.github/scripts/run_deployment_preflight.py').read_text()
        self.assertIn('verify_release_source(source, upstream, revision)', source)
        self.assertIn("run_collection_regressions.py'), '--retained'", source)
        self.assertNotIn('else verify\n', source)
        import run_collection_regressions as regressions
        self.assertEqual(sum(regressions.COLLECTION_SUITES.values()), 1036)
        self.assertEqual(sum(regressions.RETAINED_SUITES.values()), 282)

    def test_release_workflow_requires_frontend_and_preserves_existing_authority(self):
        import yaml
        workflow = yaml.safe_load((ROOT / '.github/workflows/build-release-image.yml').read_text())
        self.assertNotIn('ASTRA_RELEASE_CONTRACT_SHA256', workflow['env'])
        self.assertEqual(workflow['env']['PREVIOUS_SHA'], '${{ inputs.previous_source }}')
        self.assertEqual(workflow['env']['PREVIOUS_IMAGE'], '${{ inputs.previous_image }}')
        steps = workflow['jobs']['build']['steps']
        names = [step['name'] for step in steps]
        for name in ('Require frontend gate refusal cases for release', 'Require 23 collection frontend tests, types and build for release'):
            self.assertLess(names.index(name), names.index('Build candidate from corrected fork and reviewed release recipe'))
            self.assertNotIn('continue-on-error', steps[names.index(name)])
        package = steps[names.index('Package raw pinned source and reviewed deployment provenance')]['run']
        self.assertIn('git -C source archive --format=tar', package)
        self.assertNotIn('RELEASE_CONTRACT_SHA256', package)
        ancestry = steps[names.index('Verify official release ancestry and retained application repair')]['run']
        self.assertIn('refs/heads/codex/deploy|refs/heads/codex/okx-public-domains', ancestry)
        self.assertIn('git merge-base --is-ancestor "$PREVIOUS_SHA" "$SOURCE_SHA"', ancestry)
        self.assertIn('test "$PREVIOUS_SHA" != "$SOURCE_SHA"', ancestry)

    def test_pr_workflow_selects_strict_release_gate_without_new_approval_mechanism(self):
        text = (ROOT / '.github/workflows/test-taker-parsing.yml').read_text()
        self.assertIn('if test -f .github/scripts/collection_release_contract.json; then', text)
        self.assertIn('run_deployment_preflight.py --release-pr', text)
        self.assertIn('run_deployment_preflight.py --collection-pr', text)
        self.assertNotIn('vars.', text)
        self.assertNotIn('release_contract_sha256', text)
        self.assertIn('verifier = verify_release_source if', text)
        self.assertIn('no image, production migration or release approval', text)

    def test_only_confirmed_status_css_paths_extend_release_application(self):
        expected = {'frontend/src/views/admin/GatewayPage.vue',
                    'frontend/src/views/admin/AgentsPage.vue'}
        self.assertEqual({p for p in guard.RELEASE_INCREMENT_PATHS if not p.startswith('.github/')}, expected)
        for relative in expected:
            pin = self.contract['release_paths'][relative]
            self.assertEqual(pin['reviewed_sha256'], self.contract['reviewed_paths'][relative]['sha256'])
            self.assertNotEqual(pin['sha256'], pin['reviewed_sha256'])

    def test_css_fix_requires_exact_after_bytes_and_cannot_expand_application_scope(self):
        for relative in ('frontend/src/views/admin/GatewayPage.vue',
                         'frontend/src/views/admin/AgentsPage.vue'):
            changed = copy.deepcopy(self.contract)
            changed['release_paths'][relative]['sha256'] = changed['release_paths'][relative]['reviewed_sha256']
            with self.subTest(relative=relative), self.assertRaisesRegex(AssertionError, 'Unreviewed release tree bytes'):
                self.verify(contract=changed)
        with self.assertRaisesRegex(AssertionError, 'Missing or unreviewed release increment'):
            self.verify(release_delta=guard.RELEASE_INCREMENT_PATHS | {'frontend/src/views/dashboard/RadarView.vue'})

    def test_staging_retains_all_twelve_checks(self):
        import stage_release_bundle as stage
        from runtime_release_upgrade import CHECKS
        self.assertEqual(len(CHECKS), 12)
        self.assertEqual(set(CHECKS), stage.REQUIRED_RELEASE_CHECKS)


if __name__ == '__main__':
    unittest.main()
