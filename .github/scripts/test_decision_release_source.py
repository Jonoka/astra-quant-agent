"""Decision-release refusal tests. Mocked history successes are not Git proof.

Real production ancestry and legacy source acceptance must additionally pass the
unmocked source guard on the exact committed release checkout.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import verify_decision_release_source as guard

ROOT = Path(__file__).resolve().parents[2]
PRE_FACTOR_RELEASE = '26673ca6668b1df3f5e074e7f760cc593ae1a192'
PRE_PROMPT_RELEASE = '1bd3c016bf4375114d34022c1528d119d90cd6ad'
PROMPT_CONSISTENCY_RELEASE_PATHS = frozenset({
    '.github/scripts/run_decision_contract_regressions.py',
    '.github/scripts/test_decision_regression_gate.py',
    'data/prompt_library.json',
    'tests/llm/test_prompt_exhaustion_consistency.py',
})
FACTOR_COLLECTION_RELEASE_PATHS = frozenset({
    '.github/scripts/run_collection_regressions.py',
    '.github/scripts/test_collection_regression_gate.py',
    'astra_gateway/scheduler.py',
    'astra_gateway/collection_timeout.py',
    'scripts/market_data_service.py',
    'tests/ops/test_scheduler_timeout_diagnostics.py',
    'tests/venues/test_indicator_failure_dedup.py',
})


def object_id(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


class DecisionSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = guard.decision_release_contract(ROOT / guard.CONTRACT_PATH)
        cls.revision = 'b' * 40
        cls.reviewed = {relative: guard.git(ROOT, 'show', guard.REVIEWED_COMMIT + ':' + relative)
                        for relative in guard.REVIEWED_PATHS}
        cls.baseline = {relative: guard.git(ROOT, 'show', guard.BASELINE + ':' + relative)
                        for relative, pin in cls.contract['reviewed_paths'].items()
                        if pin['base_sha256'] is not None}
        for relative, pin in cls.contract['release_paths'].items():
            if pin['reviewed_sha256'] is not None and relative not in cls.reviewed:
                cls.reviewed[relative] = guard.git(ROOT, 'show', guard.REVIEWED_COMMIT + ':' + relative)
        cls.current = {relative: (ROOT / relative).read_bytes()
                       for relative in guard.REVIEWED_PATHS | guard.RELEASE_INCREMENT_PATHS}
        # Unchanged files outside either delta must also be raw-byte checked.
        for relative in ('Dockerfile', guard.HISTORICAL_VERIFIER_PATH,
                         '.github/scripts/collection_source_contract.json',
                         '.github/scripts/collection_release_contract.json'):
            cls.current[relative] = (ROOT / relative).read_bytes()

    def verify(self, *, revision=None, upstream=guard.UPSTREAM, head=None, trees=None,
               root=None, delta=None, dirty=b'', hidden=b'H', mode=None, missing=None,
               mutation=None, working_mutation=None, ancestry=None, new_exists=None,
               contract_path=None, executor=None):
        """Synthetic Git fixture only: it cannot prove real ancestry or release."""
        revision = self.revision if revision is None else revision
        snapshots = {guard.BASELINE: dict(self.baseline), guard.REVIEWED_COMMIT: dict(self.reviewed),
                     revision: dict(self.current)}
        if mutation:
            rev, relative = mutation
            snapshots[rev][relative] += b'\n# unreviewed\n'
        if new_exists:
            rev, relative = new_exists
            snapshots[rev][relative] = b'unreviewed predecessor'
        if missing:
            snapshots[missing[0]].pop(missing[1], None)
        dirty_calls = iter(dirty) if isinstance(dirty, list) else None
        head_calls = iter(head) if isinstance(head, list) else None

        def git(source, *args, input=None):
            verb, rest = args[0], args[1:]
            if verb == 'rev-parse':
                values = {'HEAD': (next(head_calls) if head_calls else head) or revision,
                          '--show-toplevel': root or str(ROOT),
                          guard.BASELINE + '^{tree}': guard.BASELINE_TREE,
                          guard.REVIEWED_COMMIT + '^{tree}': guard.REVIEWED_TREE}
                values.update(trees or {})
                return (values[rest[0]] + '\n').encode()
            if verb == 'merge-base':
                self.assertIn(rest, (('--is-ancestor', guard.UPSTREAM, guard.BASELINE),
                                     ('--is-ancestor', guard.BASELINE, guard.REVIEWED_COMMIT),
                                     ('--is-ancestor', guard.REVIEWED_COMMIT, revision)))
                if ancestry == rest[1:]:
                    raise subprocess.CalledProcessError(1, ['git', *args])
                return b''
            if verb == 'diff':
                self.assertEqual(rest[:4], ('--no-ext-diff', '--no-renames', '--name-only', '-z'))
                key = rest[-2:]
                expected = {(guard.BASELINE, guard.REVIEWED_COMMIT): guard.REVIEWED_PATHS,
                            (guard.REVIEWED_COMMIT, revision): guard.RELEASE_INCREMENT_PATHS,
                            (guard.BASELINE, revision): guard.REVIEWED_PATHS | guard.RELEASE_INCREMENT_PATHS}
                names = delta[1] if delta and delta[0] == key else expected[key]
                return b'\0'.join(name.encode() for name in sorted(names))
            if verb == 'status':
                self.assertEqual(rest, ('--porcelain', '--untracked-files=all'))
                return next(dirty_calls) if dirty_calls else dirty
            if verb == 'ls-files':
                self.assertEqual(rest, ('-v', '-z'))
                return b'\0'.join(hidden + b' ' + name.encode() for name in sorted(self.current))
            if verb == 'ls-tree':
                recursive = rest[0] == '-r'
                rev = rest[2] if recursive else rest[1]
                names = snapshots[rev] if recursive else [rest[-1]]
                result = []
                for relative in sorted(names):
                    if relative not in snapshots[rev]:
                        continue
                    file_mode, kind = ('100644', 'blob') if not mode or mode[:2] != (rev, relative) else mode[2:]
                    result.append((file_mode + ' ' + kind + ' ' + object_id(snapshots[rev][relative])
                                   + '\t' + relative).encode())
                return b'\0'.join(result)
            if verb == 'show':
                rev, relative = rest[0].split(':', 1)
                return snapshots[rev][relative]
            if verb == 'cat-file':
                self.assertEqual(rest, ('--batch',))
                blobs = {object_id(raw).encode(): raw for raw in snapshots[revision].values()}
                return b''.join(oid + b' blob ' + str(len(blobs[oid])).encode() + b'\n'
                                + blobs[oid] + b'\n' for oid in input.splitlines())
            raise AssertionError('Unexpected synthetic Git operation: ' + str(args))

        original_regular = guard.regular_file

        def regular(source, relative):
            raw = original_regular(source, relative)
            return raw + b'\n# unreviewed\n' if relative == working_mutation else raw

        with patch.object(guard, 'git', side_effect=git), patch.object(guard, 'regular_file', side_effect=regular), \
                patch.object(guard, '_EXECUTING_VERIFIER_BYTES', executor or guard._EXECUTING_VERIFIER_BYTES):
            guard.verify_current_source(ROOT, upstream, revision, contract_path)

    def reject_contract(self, changed, message='.'):
        with patch.object(Path, 'read_bytes', return_value=json.dumps(changed).encode()), \
                self.assertRaisesRegex(RuntimeError, message):
            guard.decision_release_contract(ROOT / guard.CONTRACT_PATH)

    def test_synthetic_current_fixture_passes_only_current_validation_not_historical_proof(self):
        with patch.object(guard, 'verify_historical_baseline') as historical:
            self.verify()
        historical.assert_not_called()

    def test_ordinary_pr_and_baseline_are_never_release_candidates(self):
        for revision in (guard.REVIEWED_COMMIT, guard.BASELINE):
            with self.subTest(revision=revision), self.assertRaisesRegex(RuntimeError, 'ordinary PR'):
                self.verify(revision=revision)

    def test_exact_lowercase_full_revision_is_required(self):
        for revision in ('HEAD', 'b' * 39, 'B' * 40):
            with self.subTest(revision=revision), self.assertRaisesRegex(RuntimeError, 'Unpinned revision'):
                self.verify(revision=revision)

    def test_wrong_upstream_and_checkout_head_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'upstream'):
            self.verify(upstream='c' * 40)
        with self.assertRaisesRegex(RuntimeError, 'source commit'):
            self.verify(head='c' * 40)

    def test_checkout_must_be_the_repository_root(self):
        with self.assertRaisesRegex(RuntimeError, 'checkout root'):
            self.verify(root=str(ROOT.parent))

    def test_final_head_cannot_change_during_verification(self):
        # rev-parse reads root, HEAD and two anchor trees, then final HEAD.
        with self.assertRaisesRegex(RuntimeError, 'HEAD changed'):
            self.verify(head=[self.revision] * 4 + ['c' * 40])

    def test_contract_path_cannot_select_a_different_manifest(self):
        with self.assertRaisesRegex(RuntimeError, 'explicit decision release contract'):
            self.verify(contract_path=ROOT / 'other.json')

    def test_each_historical_anchor_tree_is_fixed(self):
        for anchor in (guard.BASELINE, guard.REVIEWED_COMMIT):
            with self.subTest(anchor=anchor), self.assertRaisesRegex(RuntimeError, 'anchor tree'):
                self.verify(trees={anchor + '^{tree}': 'c' * 40})

    def test_every_historical_ancestry_edge_is_required(self):
        for edge in ((guard.UPSTREAM, guard.BASELINE), (guard.BASELINE, guard.REVIEWED_COMMIT),
                     (guard.REVIEWED_COMMIT, self.revision)):
            with self.subTest(edge=edge), self.assertRaises(subprocess.CalledProcessError):
                self.verify(ancestry=edge)

    def test_each_of_the_26_reviewed_paths_is_required(self):
        for relative in guard.REVIEWED_PATHS:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'anchor delta paths'):
                self.verify(delta=((guard.BASELINE, guard.REVIEWED_COMMIT), guard.REVIEWED_PATHS - {relative}))

    def test_each_of_the_20_release_paths_is_required(self):
        for relative in guard.RELEASE_INCREMENT_PATHS:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'release increment paths'):
                self.verify(delta=((guard.REVIEWED_COMMIT, self.revision), guard.RELEASE_INCREMENT_PATHS - {relative}))

    def test_unknown_application_and_ci_deltas_are_rejected(self):
        for relative in ('scripts/unknown.py', '.github/scripts/unknown.py', '.github/workflows/unknown.yml'):
            for edge, names in (((guard.BASELINE, guard.REVIEWED_COMMIT), guard.REVIEWED_PATHS),
                                ((guard.REVIEWED_COMMIT, self.revision), guard.RELEASE_INCREMENT_PATHS)):
                with self.subTest(relative=relative, edge=edge), self.assertRaisesRegex(RuntimeError, 'paths'):
                    self.verify(delta=(edge, names | {relative}))

    def test_reviewed_changes_cannot_be_cancelled_by_release_delta(self):
        with self.assertRaisesRegex(RuntimeError, 'complete decision release delta'):
            self.verify(delta=((guard.BASELINE, self.revision), guard.RELEASE_INCREMENT_PATHS))

    def test_staged_modified_and_untracked_checkout_is_rejected(self):
        for dirty in (b' M Dockerfile\n', b'M  Dockerfile\n', b'?? unknown.py\n'):
            with self.subTest(dirty=dirty), self.assertRaisesRegex(RuntimeError, 'dirty or untracked'):
                self.verify(dirty=dirty)

    def test_checkout_cannot_become_dirty_during_verification(self):
        with self.assertRaisesRegex(RuntimeError, 'changed during verification'):
            self.verify(dirty=[b'', b' M Dockerfile\n'])

    def test_assume_unchanged_and_skip_worktree_are_rejected(self):
        for hidden in (b'h', b'S', b's'):
            with self.subTest(hidden=hidden), self.assertRaisesRegex(RuntimeError, 'index hides'):
                self.verify(hidden=hidden)

    def test_all_existing_baseline_predecessor_hashes_are_checked(self):
        for relative in self.baseline:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'Unreviewed decision release bytes'):
                self.verify(mutation=(guard.BASELINE, relative))

    def test_baseline_predecessors_cannot_disappear(self):
        relative = next(iter(self.baseline))
        with self.assertRaisesRegex(RuntimeError, 'required path missing'):
            self.verify(missing=(guard.BASELINE, relative))

    def test_new_reviewed_paths_must_be_absent_at_baseline(self):
        for relative in guard.REVIEWED_PATHS - set(self.baseline):
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'already exists in predecessor'):
                self.verify(new_exists=(guard.BASELINE, relative))

    def test_all_reviewed_anchor_hashes_are_checked(self):
        for relative in guard.REVIEWED_PATHS - {guard.CANDIDATE_CONTRACT_PATH}:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'Unreviewed decision release bytes'):
                self.verify(mutation=(guard.REVIEWED_COMMIT, relative))

    def test_original_candidate_manifest_is_pinned_and_read_only_at_reviewed_anchor(self):
        with self.assertRaisesRegex(RuntimeError, 'Unreviewed decision release bytes'):
            self.verify(mutation=(self.revision, guard.CANDIDATE_CONTRACT_PATH),
                        working_mutation=guard.CANDIDATE_CONTRACT_PATH)
        changed = copy.deepcopy(self.contract)
        changed['reviewed_paths']['scripts/brain/packages.py']['sha256'] = 'c' * 64
        with patch.object(guard, 'decision_release_contract', return_value=changed), \
                self.assertRaisesRegex(RuntimeError, 'Historical PR pins'):
            self.verify()

    def test_all_release_predecessors_are_checked(self):
        for relative, pin in self.contract['release_paths'].items():
            if pin['reviewed_sha256'] is not None:
                with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'Unreviewed decision release bytes'):
                    self.verify(mutation=(guard.REVIEWED_COMMIT, relative))

    def test_new_release_paths_and_trust_roots_must_be_absent_at_anchor(self):
        names = {relative for relative, pin in self.contract['release_paths'].items()
                 if pin['reviewed_sha256'] is None} | guard.SELF_HASH_EXEMPTIONS
        for relative in names:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'already exists in predecessor'):
                self.verify(new_exists=(guard.REVIEWED_COMMIT, relative))

    def test_reviewed_and_release_files_cannot_be_deleted(self):
        for relative in guard.REVIEWED_PATHS | guard.RELEASE_INCREMENT_PATHS:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'missing'):
                self.verify(missing=(self.revision, relative))

    def test_new_and_reviewed_files_reject_executable_symlink_and_gitlink_modes(self):
        for relative in guard.REVIEWED_PATHS | guard.RELEASE_INCREMENT_PATHS:
            for mode, kind in (('100755', 'blob'), ('120000', 'blob'), ('160000', 'commit')):
                with self.subTest(relative=relative, mode=mode), self.assertRaisesRegex(RuntimeError, 'Git mode or type'):
                    self.verify(mode=(self.revision, relative, mode, kind))

    def test_both_historical_predecessor_modes_are_checked(self):
        for revision, relative in ((guard.BASELINE, next(iter(self.baseline))),
                                   (guard.REVIEWED_COMMIT, '.github/workflows/build-release-image.yml')):
            with self.subTest(revision=revision), self.assertRaisesRegex(RuntimeError, 'Git mode or type'):
                self.verify(mode=(revision, relative, '100755', 'blob'))

    def test_all_hashed_current_bytes_are_fixed(self):
        for relative in (guard.REVIEWED_PATHS | guard.RELEASE_INCREMENT_PATHS) - guard.SELF_HASH_EXEMPTIONS:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'Unreviewed decision release bytes'):
                self.verify(mutation=(self.revision, relative), working_mutation=relative)

    def test_every_working_file_must_match_committed_raw_bytes(self):
        for relative in self.current:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'working bytes'):
                self.verify(working_mutation=relative)

    def test_unchanged_legacy_contracts_and_verifier_are_also_byte_checked(self):
        for relative in (guard.HISTORICAL_VERIFIER_PATH, '.github/scripts/collection_source_contract.json',
                         '.github/scripts/collection_release_contract.json'):
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'working bytes differ'):
                self.verify(working_mutation=relative)

    def test_self_exempt_files_must_match_executing_trust_roots(self):
        for relative in guard.SELF_HASH_EXEMPTIONS:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'Executing decision release trust root'):
                self.verify(mutation=(self.revision, relative), working_mutation=relative)

    def test_imported_verifier_bytes_cannot_drift_from_disk(self):
        with self.assertRaisesRegex(RuntimeError, 'Executing decision release trust root'):
            self.verify(executor=b'different verifier loaded before verification')

    def test_schema_upstream_baseline_and_reviewed_anchors_are_strict(self):
        for field, value in (('schema', True), ('schema', 2), ('upstream', 'c' * 40),
                             ('baseline', 'c' * 40), ('baseline_tree', 'c' * 40),
                             ('reviewed_commit', 'c' * 40), ('reviewed_tree', 'c' * 40)):
            changed = copy.deepcopy(self.contract); changed[field] = value
            with self.subTest(field=field):
                self.reject_contract(changed)

    def test_current_head_self_pin_and_unknown_contract_fields_are_forbidden(self):
        for field in ('current_commit', 'current_tree', 'revision', 'generate_pins', 'allow_release'):
            changed = copy.deepcopy(self.contract); changed[field] = self.revision
            self.reject_contract(changed, 'contract fields')
        self.assertNotIn('ordinary_pull_request_only', self.contract)
        self.assertEqual(len(guard.REVIEWED_PATHS), 26)
        self.assertEqual(len(guard.RELEASE_INCREMENT_PATHS), 20)

    def test_wal_increment_is_exactly_three_fixed_tool_paths_with_both_hashes(self):
        prior = '16e12871e51e7cfb13823a370dcbf9bdec9f0804'
        old = json.loads(guard.git(ROOT, 'show', prior + ':' + guard.CONTRACT_PATH))
        before_factor = json.loads(guard.git(ROOT, 'show', PRE_FACTOR_RELEASE + ':' + guard.CONTRACT_PATH))
        added = {'.github/scripts/runtime_release_upgrade.py',
                 '.github/scripts/test_runtime_release_upgrade.py',
                 '.github/scripts/test_deployment_plan.py'}
        self.assertEqual(set(before_factor['release_paths']) - set(old['release_paths']), added)
        self.assertEqual(self.contract['reviewed_paths'], old['reviewed_paths'])
        self.assertEqual(self.contract['self_hash_exemptions'], old['self_hash_exemptions'])
        for relative in added:
            pin = self.contract['release_paths'][relative]
            self.assertEqual(pin, before_factor['release_paths'][relative])
            before = guard.git(ROOT, 'show', prior + ':' + relative)
            self.assertEqual(before, self.reviewed[relative])
            self.assertEqual(pin['reviewed_sha256'], hashlib.sha256(before).hexdigest())
            self.assertEqual(pin['sha256'], hashlib.sha256(self.current[relative]).hexdigest())
            self.assertEqual(pin['mode'], '100644')
            self.assertNotIn(relative, guard.SELF_HASH_EXEMPTIONS)

    def test_each_wal_tool_predecessor_current_bytes_and_modes_remain_pinned(self):
        for relative in ('.github/scripts/runtime_release_upgrade.py',
                         '.github/scripts/test_runtime_release_upgrade.py',
                         '.github/scripts/test_deployment_plan.py'):
            for revision in (guard.REVIEWED_COMMIT, self.revision):
                with self.subTest(relative=relative, revision=revision), self.assertRaises(RuntimeError):
                    self.verify(mutation=(revision, relative))
                with self.subTest(relative=relative, revision=revision, mode=True), self.assertRaises(RuntimeError):
                    self.verify(mode=(revision, relative, '100755', 'blob'))

    def test_factor_increment_is_exactly_seven_paths_with_existing_release_pins_preserved(self):
        old = json.loads(guard.git(ROOT, 'show', PRE_FACTOR_RELEASE + ':' + guard.CONTRACT_PATH))
        before_prompt = json.loads(guard.git(ROOT, 'show', PRE_PROMPT_RELEASE + ':' + guard.CONTRACT_PATH))
        self.assertEqual(set(before_prompt['release_paths']) - set(old['release_paths']),
                         FACTOR_COLLECTION_RELEASE_PATHS)
        self.assertEqual(set(before_prompt['release_paths']) | guard.SELF_HASH_EXEMPTIONS,
                         set(old['release_paths']) | guard.SELF_HASH_EXEMPTIONS | FACTOR_COLLECTION_RELEASE_PATHS)
        for field in ('schema', 'upstream', 'baseline', 'baseline_tree', 'reviewed_commit',
                      'reviewed_tree', 'reviewed_paths', 'self_hash_exemptions'):
            self.assertEqual(before_prompt[field], old[field])
        updated = {'.github/scripts/test_decision_release_source.py',
                   '.github/scripts/run_decision_release_preflight.py'}
        for relative, pin in old['release_paths'].items():
            with self.subTest(relative=relative):
                if relative in updated:
                    self.assertEqual(before_prompt['release_paths'][relative]['reviewed_sha256'],
                                     pin['reviewed_sha256'])
                    self.assertEqual(before_prompt['release_paths'][relative]['mode'], pin['mode'])
                else:
                    self.assertEqual(before_prompt['release_paths'][relative], pin)

    def test_prompt_increment_is_exactly_four_paths_with_existing_release_pins_preserved(self):
        old = json.loads(guard.git(ROOT, 'show', PRE_PROMPT_RELEASE + ':' + guard.CONTRACT_PATH))
        self.assertEqual(set(self.contract['release_paths']) - set(old['release_paths']),
                         PROMPT_CONSISTENCY_RELEASE_PATHS)
        self.assertEqual(guard.RELEASE_INCREMENT_PATHS,
                         set(old['release_paths']) | guard.SELF_HASH_EXEMPTIONS | PROMPT_CONSISTENCY_RELEASE_PATHS)
        for field in ('schema', 'upstream', 'baseline', 'baseline_tree', 'reviewed_commit',
                      'reviewed_tree', 'reviewed_paths', 'self_hash_exemptions'):
            self.assertEqual(self.contract[field], old[field])
        updated = {'.github/scripts/test_decision_release_source.py',
                   '.github/scripts/run_decision_release_preflight.py'}
        for relative, pin in old['release_paths'].items():
            with self.subTest(relative=relative):
                if relative in updated:
                    self.assertEqual(self.contract['release_paths'][relative]['reviewed_sha256'],
                                     pin['reviewed_sha256'])
                    self.assertEqual(self.contract['release_paths'][relative]['mode'], pin['mode'])
                else:
                    self.assertEqual(self.contract['release_paths'][relative], pin)

    def test_prompt_pins_bind_exact_original_predecessors_and_current_bytes(self):
        for relative in PROMPT_CONSISTENCY_RELEASE_PATHS:
            with self.subTest(relative=relative):
                pin = self.contract['release_paths'][relative]
                if relative == 'tests/llm/test_prompt_exhaustion_consistency.py':
                    self.assertIsNone(pin['reviewed_sha256'])
                    self.assertNotIn(relative, self.reviewed)
                else:
                    self.assertEqual(pin['reviewed_sha256'], hashlib.sha256(self.reviewed[relative]).hexdigest())
                self.assertEqual(pin['sha256'], hashlib.sha256(self.current[relative]).hexdigest())
                self.assertEqual(pin['mode'], '100644')
                self.assertNotIn(relative, guard.SELF_HASH_EXEMPTIONS)

    def test_factor_pins_bind_exact_original_predecessors_and_current_bytes(self):
        new = {'astra_gateway/collection_timeout.py',
               'tests/ops/test_scheduler_timeout_diagnostics.py',
               'tests/venues/test_indicator_failure_dedup.py'}
        for relative in FACTOR_COLLECTION_RELEASE_PATHS:
            with self.subTest(relative=relative):
                pin = self.contract['release_paths'][relative]
                if relative in new:
                    self.assertIsNone(pin['reviewed_sha256'])
                    self.assertNotIn(relative, self.reviewed)
                else:
                    self.assertEqual(pin['reviewed_sha256'], hashlib.sha256(self.reviewed[relative]).hexdigest())
                self.assertEqual(pin['sha256'], hashlib.sha256(self.current[relative]).hexdigest())
                self.assertEqual(pin['mode'], '100644')
                self.assertNotIn(relative, guard.SELF_HASH_EXEMPTIONS)

    def test_each_factor_manifest_path_is_mandatory(self):
        for relative in FACTOR_COLLECTION_RELEASE_PATHS:
            changed = copy.deepcopy(self.contract)
            del changed['release_paths'][relative]
            with self.subTest(relative=relative):
                self.reject_contract(changed, 'release_paths')

    def test_each_factor_pin_requires_all_exact_fields_and_valid_hashes(self):
        for relative in FACTOR_COLLECTION_RELEASE_PATHS:
            for field in ('reviewed_sha256', 'sha256', 'mode'):
                changed = copy.deepcopy(self.contract)
                del changed['release_paths'][relative][field]
                with self.subTest(relative=relative, missing=field):
                    self.reject_contract(changed, 'pin fields')
            for field, value in (('sha256', None), ('sha256', 'B' * 64),
                                 ('reviewed_sha256', 'invalid'), ('mode', '100755'), ('unknown', True)):
                changed = copy.deepcopy(self.contract)
                changed['release_paths'][relative][field] = value
                with self.subTest(relative=relative, field=field):
                    self.reject_contract(changed)

    def test_each_factor_predecessor_pin_cannot_be_replaced_by_a_valid_hash(self):
        for relative in FACTOR_COLLECTION_RELEASE_PATHS:
            changed = copy.deepcopy(self.contract)
            changed['release_paths'][relative]['reviewed_sha256'] = 'c' * 64
            with self.subTest(relative=relative), \
                    patch.object(guard, 'decision_release_contract', return_value=changed), \
                    self.assertRaisesRegex(RuntimeError, 'bytes|required path missing'):
                self.verify()

    def test_each_factor_current_pin_cannot_be_replaced_by_a_valid_hash(self):
        for relative in FACTOR_COLLECTION_RELEASE_PATHS:
            changed = copy.deepcopy(self.contract)
            changed['release_paths'][relative]['sha256'] = 'c' * 64
            with self.subTest(relative=relative), \
                    patch.object(guard, 'decision_release_contract', return_value=changed), \
                    self.assertRaisesRegex(RuntimeError, 'Unreviewed decision release bytes'):
                self.verify()

    def test_each_factor_path_must_exist_in_release_delta_and_committed_tree(self):
        for relative in FACTOR_COLLECTION_RELEASE_PATHS:
            with self.subTest(relative=relative, absent='delta'), \
                    self.assertRaisesRegex(RuntimeError, 'release increment paths'):
                self.verify(delta=((guard.REVIEWED_COMMIT, self.revision),
                                   guard.RELEASE_INCREMENT_PATHS - {relative}))
            with self.subTest(relative=relative, absent='tree'), \
                    self.assertRaisesRegex(RuntimeError, 'required path missing'):
                self.verify(missing=(self.revision, relative))

    def test_each_factor_path_rejects_changed_current_bytes_and_nonregular_modes(self):
        for relative in FACTOR_COLLECTION_RELEASE_PATHS:
            with self.subTest(relative=relative, changed='bytes'), \
                    self.assertRaisesRegex(RuntimeError, 'Unreviewed decision release bytes'):
                self.verify(mutation=(self.revision, relative), working_mutation=relative)
            for mode, kind in (('100755', 'blob'), ('120000', 'blob'), ('160000', 'commit')):
                with self.subTest(relative=relative, mode=mode), \
                        self.assertRaisesRegex(RuntimeError, 'Git mode or type'):
                    self.verify(mode=(self.revision, relative, mode, kind))

    def test_manifest_duplicate_fields_are_rejected(self):
        with patch.object(Path, 'read_bytes', return_value=b'{"schema":1,"schema":1}'), \
                self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            guard.decision_release_contract(ROOT / guard.CONTRACT_PATH)

    def test_manifest_path_sets_cannot_expand_or_shrink(self):
        for field in ('reviewed_paths', 'release_paths'):
            changed = copy.deepcopy(self.contract)
            changed[field]['../escape'] = next(iter(changed[field].values()))
            self.reject_contract(changed, field)
            changed = copy.deepcopy(self.contract); changed[field].pop(next(iter(changed[field])))
            self.reject_contract(changed, field)

    def test_only_the_two_new_roots_are_self_hash_exempt(self):
        self.assertEqual(set(self.contract['self_hash_exemptions']), guard.SELF_HASH_EXEMPTIONS)
        for exemptions in ([], list(guard.SELF_HASH_EXEMPTIONS) * 2,
                           [guard.CONTRACT_PATH, guard.HISTORICAL_VERIFIER_PATH]):
            changed = copy.deepcopy(self.contract); changed['self_hash_exemptions'] = exemptions
            self.reject_contract(changed, 'trust root')
        for relative in (guard.CANDIDATE_CONTRACT_PATH, '.github/scripts/verify_pr_candidate.py'):
            self.assertTrue(guard.is_hash(self.contract['reviewed_paths'][relative]['sha256']))

    def test_every_manifest_pin_has_strict_fields_hashes_and_mode(self):
        for group, predecessor in (('reviewed_paths', 'base_sha256'), ('release_paths', 'reviewed_sha256')):
            relative = next(iter(self.contract[group]))
            for field, value in (('sha256', None), ('sha256', 'B' * 64), ('sha256', 'c' * 63),
                                 ('mode', '100755'), (predecessor, 'invalid'), ('unknown', True)):
                changed = copy.deepcopy(self.contract); changed[group][relative][field] = value
                with self.subTest(group=group, field=field):
                    self.reject_contract(changed)

    def test_overlap_predecessor_hash_must_agree_with_reviewed_manifest(self):
        changed = copy.deepcopy(self.contract)
        changed['release_paths']['.github/workflows/test-taker-parsing.yml']['reviewed_sha256'] = 'c' * 64
        self.reject_contract(changed, 'predecessor differs')


class RawCheckoutRefusalTests(unittest.TestCase):
    def test_real_hidden_index_flags_are_rejected(self):
        for flag in ('--assume-unchanged', '--skip-worktree'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory(prefix='decision-index-') as temporary:
                root = Path(temporary); (root / 'file').write_bytes(b'original')
                guard.git(root, 'init', '--quiet'); guard.git(root, 'add', 'file')
                guard.require_visible_index(root)
                guard.git(root, 'update-index', flag, 'file')
                (root / 'file').write_bytes(b'hidden changed bytes')
                with self.assertRaisesRegex(RuntimeError, 'index hides'):
                    guard.require_visible_index(root)

    def test_real_working_regular_file_modes_and_symlinked_directories_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='decision-modes-') as temporary:
            root = Path(temporary); target = root / 'file'; target.write_bytes(b'fixture')
            target.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'working mode'):
                guard.regular_file(root, 'file')
            target.unlink(); target.symlink_to('missing')
            with self.assertRaisesRegex(RuntimeError, 'regular file missing'):
                guard.regular_file(root, 'file')
            target.unlink()
            with self.assertRaisesRegex(RuntimeError, 'regular file missing'):
                guard.regular_file(root, 'file')
            (root / 'real').mkdir(); (root / 'real/file').write_bytes(b'fixture')
            (root / 'link').symlink_to('real', target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'Symlinked'):
                guard.regular_file(root, 'link/file')

    def checkout_fixture(self, mutate=None):
        """Real Git blobs with mocked clean status, never a history proof."""
        with tempfile.TemporaryDirectory(prefix='decision-raw-checkout-') as temporary:
            root = Path(temporary)
            (root / 'file').write_bytes(b'raw\r\nbytes\n')
            (root / 'exec').write_bytes(b'#!/bin/sh\n'); (root / 'exec').chmod(0o755)
            (root / 'link').symlink_to('file')
            guard.git(root, 'init', '--quiet'); guard.git(root, 'add', '.')
            tree = guard.git(root, 'write-tree').decode().strip()
            if mutate:
                mutate(root)
            real_git = guard.git

            def git(source, *args, **kwargs):
                return b'' if args[0] == 'status' else real_git(source, *args, **kwargs)

            with patch.object(guard, 'git', side_effect=git):
                guard.verify_checkout(root, tree)

    def test_full_raw_checkout_accepts_existing_executable_and_symlink(self):
        self.checkout_fixture()

    def test_raw_byte_comparison_catches_changes_even_when_status_is_clean(self):
        with self.assertRaisesRegex(RuntimeError, 'working bytes differ'):
            self.checkout_fixture(lambda root: (root / 'file').write_bytes(b'raw\nbytes\n'))

    def test_existing_executable_mode_cannot_be_removed(self):
        with self.assertRaisesRegex(RuntimeError, 'working executable mode'):
            self.checkout_fixture(lambda root: (root / 'exec').chmod(0o644))

    def test_existing_symlink_target_cannot_change(self):
        def mutate(root):
            (root / 'link').unlink(); (root / 'link').symlink_to('elsewhere')
        with self.assertRaisesRegex(RuntimeError, 'working bytes differ'):
            self.checkout_fixture(mutate)

    def test_git_calls_disable_replace_objects_hooks_and_fsmonitor(self):
        with patch.object(guard.subprocess, 'run', return_value=SimpleNamespace(stdout=b'')) as run:
            guard.git(ROOT, 'status')
        command = run.call_args.args[0]
        self.assertIn('--no-replace-objects', command)
        self.assertIn('core.fsmonitor=false', command)
        self.assertIn('core.hooksPath=' + os.devnull, command)
        self.assertTrue(run.call_args.kwargs['check'])


class HistoricalGuardBoundaryTests(unittest.TestCase):
    def historical(self, *, fail=False, head=guard.BASELINE, tree=guard.BASELINE_TREE):
        calls = []

        def git(source, *args, **kwargs):
            calls.append((source, args))
            if args == ('rev-parse', guard.BASELINE + '^{tree}'):
                return (tree + '\n').encode()
            if args == ('rev-parse', 'HEAD'):
                return (head + '\n').encode()
            return b''

        def run(command, **kwargs):
            if fail:
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=0)

        with patch.object(guard, 'git', side_effect=git), patch.object(guard, 'verify_checkout') as checkout, \
                patch.object(guard.subprocess, 'run', side_effect=run) as execute:
            guard.verify_historical_baseline(ROOT)
        return calls, checkout, execute

    def test_mock_historical_success_uses_exact_independent_worktree_and_original_script(self):
        calls, checkout, execute = self.historical()
        add = calls[1][1]
        self.assertEqual(add[:3], ('worktree', 'add', '--detach'))
        self.assertEqual(add[-1], guard.BASELINE)
        baseline = Path(add[-2])
        self.assertNotEqual(baseline, ROOT)
        command = execute.call_args.args[0]
        self.assertEqual(command, [sys.executable, '-I', '-B', str(baseline / guard.HISTORICAL_VERIFIER_PATH),
                                   str(baseline), guard.UPSTREAM, guard.BASELINE])
        self.assertEqual(execute.call_args.kwargs['cwd'], baseline)
        self.assertTrue(execute.call_args.kwargs['check'])
        self.assertEqual(checkout.call_args_list[0].args, (baseline, guard.BASELINE))
        self.assertEqual(checkout.call_count, 2)
        self.assertEqual(calls[-1][1], ('worktree', 'remove', '--force', str(baseline)))

    def test_historical_failure_cannot_be_ignored_and_cleanup_is_attempted(self):
        calls = []

        def git(source, *args, **kwargs):
            calls.append(args)
            if args == ('rev-parse', guard.BASELINE + '^{tree}'):
                return (guard.BASELINE_TREE + '\n').encode()
            if args == ('rev-parse', 'HEAD'):
                return (guard.BASELINE + '\n').encode()
            return b''

        with patch.object(guard, 'git', side_effect=git), patch.object(guard, 'verify_checkout'), \
                patch.object(guard.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['historical'])), \
                self.assertRaises(subprocess.CalledProcessError):
            guard.verify_historical_baseline(ROOT)
        self.assertEqual(calls[-1][:3], ('worktree', 'remove', '--force'))

    def test_historical_wrong_head_and_tree_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'baseline tree'):
            self.historical(tree='c' * 40)
        with self.assertRaisesRegex(RuntimeError, 'historical decision release checkout'):
            self.historical(head='c' * 40)

    def test_historical_dirty_or_drifted_checkout_cannot_execute(self):
        with patch.object(guard, 'git', side_effect=[(guard.BASELINE_TREE + '\n').encode(), b'',
                                                  (guard.BASELINE + '\n').encode(), b'']), \
                patch.object(guard, 'verify_checkout', side_effect=RuntimeError('historical bytes drift')), \
                patch.object(guard.subprocess, 'run') as execute, self.assertRaisesRegex(RuntimeError, 'bytes drift'):
            guard.verify_historical_baseline(ROOT)
        execute.assert_not_called()

    def test_full_guard_always_checks_current_historical_then_current(self):
        calls = []
        with patch.object(guard, 'verify_current_source', side_effect=lambda *args: calls.append('current')), \
                patch.object(guard, 'verify_historical_baseline', side_effect=lambda *args: calls.append('historical')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            guard.verify_decision_release_source(ROOT, guard.UPSTREAM, 'b' * 40)
        self.assertEqual(calls, ['current', 'historical', 'current'])
        self.assertIn('DECISION_RELEASE_SOURCE_PASS', output.getvalue())
        self.assertIn('deployment_authorization=not_granted', output.getvalue())

    def test_full_guard_never_prints_pass_when_history_fails(self):
        with patch.object(guard, 'verify_current_source') as current, \
                patch.object(guard, 'verify_historical_baseline', side_effect=RuntimeError('historical failure')), \
                patch('sys.stdout', new_callable=io.StringIO) as output, \
                self.assertRaisesRegex(RuntimeError, 'historical failure'):
            guard.verify_decision_release_source(ROOT, guard.UPSTREAM, 'b' * 40)
        self.assertEqual(current.call_count, 1)
        self.assertNotIn('SOURCE_PASS', output.getvalue())

    def test_cli_is_full_guard_without_a_history_bypass_switch(self):
        with patch.object(sys, 'argv', ['verify', str(ROOT), guard.UPSTREAM, 'b' * 40]), \
                patch.object(guard, 'verify_decision_release_source') as complete:
            guard.main()
        complete.assert_called_once_with(ROOT, guard.UPSTREAM, 'b' * 40, None)
        with patch.object(sys, 'argv', ['verify', str(ROOT), guard.UPSTREAM, 'b' * 40, '--skip-history']), \
                patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            guard.main()

    def test_historical_environment_excludes_python_git_and_production_injection(self):
        with patch.dict(os.environ, {'PYTHONOPTIMIZE': '1', 'PYTHONPATH': '/untrusted', 'GIT_INDEX_FILE': '/untrusted',
                                    'GITHUB_TOKEN': 'synthetic', 'PROVIDER_API_KEY': 'synthetic', 'ASTRA_DATA_DIR': '/production'}):
            env = guard.clean_environment()
        for key in ('PYTHONOPTIMIZE', 'PYTHONPATH', 'GIT_INDEX_FILE', 'GITHUB_TOKEN', 'PROVIDER_API_KEY', 'ASTRA_DATA_DIR'):
            self.assertNotIn(key, env)
        self.assertEqual(env['OFFLINE_SUITE_RUNNING'], '1')
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')


class DecisionPreflightBoundaryTests(unittest.TestCase):
    def invoke(self, *, fail=None, verifier_fail=None, pr_only=True, deployed=None, environment=None, uid=0):
        import run_decision_release_preflight as preflight
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if fail and fail(command):
                raise subprocess.CalledProcessError(1, command)
            value = deployed or preflight.DEPLOYMENT_PREDECESSOR['source']
            if command[-1] == '--show-toplevel':
                value = '/synthetic-deployed'
            elif command[-1] == 'HEAD^{tree}':
                value = preflight.DEPLOYMENT_PREDECESSOR['tree']
            return SimpleNamespace(returncode=0, stdout=(value + '\n').encode())

        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request', 'ASTRA_DATA_DIR': '/production',
                                    'GITHUB_TOKEN': 'synthetic', 'PROVIDER_API_KEY': 'synthetic',
                                    **(environment or {})}, clear=True), \
                patch.object(preflight, 'verify_decision_release_source', side_effect=verifier_fail) as verify, \
                patch.object(preflight, 'verify_fixture'), \
                patch.object(preflight, 'verify_checkout'), \
                patch.object(preflight.subprocess, 'run', side_effect=run), patch.object(preflight, 'extract') as extract, \
                patch.object(preflight.os, 'geteuid', return_value=uid), patch('sys.stdout', new_callable=io.StringIO) as output:
            if deployed or not pr_only:
                os.environ['ASTRA_DEPLOYED_SOURCE'] = '/synthetic-deployed'
                os.environ.setdefault('PREVIOUS_SHA', preflight.DEPLOYMENT_PREDECESSOR['source'])
                os.environ.setdefault('PREVIOUS_IMAGE', preflight.DEPLOYMENT_PREDECESSOR['image'])
            preflight.run_preflight(ROOT, 'b' * 40, pr_only)
        return calls, verify, extract, output.getvalue()

    def test_release_pr_mode_rejects_non_pr_events_before_source_check(self):
        import run_decision_release_preflight as preflight
        for event in ('', 'push', 'workflow_dispatch', 'pull_request_target'):
            with self.subTest(event=event), patch.dict(os.environ, {'GITHUB_EVENT_NAME': event}), \
                    patch.object(preflight, 'verify_decision_release_source') as verify, \
                    self.assertRaisesRegex(RuntimeError, 'pull_request event'):
                preflight.run_preflight(ROOT, 'b' * 40, True)
            verify.assert_not_called()

    def test_release_preflight_is_linux_only(self):
        import run_decision_release_preflight as preflight
        with patch.object(preflight.sys, 'platform', 'darwin'), \
                patch.object(preflight, 'verify_decision_release_source') as verify, \
                self.assertRaisesRegex(RuntimeError, 'requires Linux'):
            preflight.run_preflight(ROOT, 'b' * 40)
        verify.assert_not_called()

    def test_preflight_uses_complete_guard_before_and_after_all_work(self):
        _, verify, _, output = self.invoke(pr_only=False)
        self.assertEqual(verify.call_count, 2)
        self.assertTrue(all(call.args == (ROOT, guard.UPSTREAM, 'b' * 40) for call in verify.call_args_list))
        self.assertIn('DECISION_DEPLOYMENT_PREFLIGHT_PASS', output)
        self.assertIn('publication_authorization=not_granted', output)

    def test_release_pr_preflight_never_grants_publication(self):
        _, _, _, output = self.invoke(pr_only=True)
        self.assertIn('DECISION_RELEASE_PR_PREFLIGHT_PASS', output)
        self.assertIn('publication_authorization=not_granted', output)

    def test_original_release_preflight_runs_only_on_exact_601_worktree(self):
        calls, _, _, _ = self.invoke()
        add = calls[0][0]
        self.assertEqual(add[:6], ['git', '-C', str(ROOT), 'worktree', 'add', '--detach'])
        self.assertEqual(add[-1], guard.BASELINE)
        command, options = calls[1]
        self.assertEqual(command, [sys.executable, str(Path(add[-2]) / '.github/scripts/run_deployment_preflight.py'), '--release-pr'])
        self.assertEqual(options['env']['ASTRA_CI_EXPECTED_SHA'], guard.BASELINE)
        self.assertEqual(options['cwd'], Path(add[-2]))
        self.assertEqual(calls[2][0][3:6], ['worktree', 'remove', '--force'])

    def test_original_candidate_guard_and_46_tests_run_on_exact_71_worktree(self):
        calls, _, _, _ = self.invoke()
        add = calls[3][0]
        self.assertEqual(add[-1], guard.REVIEWED_COMMIT)
        reviewed = Path(add[-2])
        command, options = calls[4]
        self.assertEqual(command, [sys.executable, str(reviewed / '.github/scripts/verify_pr_candidate.py'),
                                   str(reviewed), guard.REVIEWED_COMMIT, '--contract',
                                   str(reviewed / guard.CANDIDATE_CONTRACT_PATH)])
        self.assertEqual(options['env']['ASTRA_CI_EXPECTED_SHA'], guard.REVIEWED_COMMIT)
        self.assertEqual(calls[5][0][3:], [str(reviewed / '.github/scripts/test_pr_candidate_contract.py'), '46'])
        self.assertEqual(calls[5][1]['cwd'], reviewed)

    def test_current_helpers_and_decision_refusals_run_on_release_head(self):
        import run_decision_release_preflight as preflight
        calls, _, _, _ = self.invoke()
        current = [(command, options) for command, options in calls
                   if len(command) > 3 and command[1] == '-c' and options.get('cwd') == ROOT]
        expected = dict(preflight.CURRENT_HELPER_SUITES, **preflight.DECISION_RELEASE_SUITES)
        self.assertEqual({Path(command[3]).name for command, _ in current}, set(expected))
        self.assertEqual(sum(preflight.CURRENT_HELPER_SUITES.values()), 182)
        self.assertNotIn('test_verify_deployment_source.py', preflight.CURRENT_HELPER_SUITES)
        self.assertEqual(preflight.DECISION_RELEASE_SUITES['test_decision_regression_gate.py'], 13)
        for command, options in current:
            self.assertEqual(Path(command[3]).parent, ROOT / '.github/scripts')
            self.assertEqual(int(command[4]), expected[Path(command[3]).name])
            self.assertEqual(options['env']['ASTRA_CI_EXPECTED_SHA'], 'b' * 40)

    def test_helper_114_and_root_68_cases_run_on_same_candidate_without_skips(self):
        import run_decision_release_preflight as preflight
        self.assertEqual(preflight.CURRENT_HELPER_SUITES['test_runtime_release_upgrade.py'], 69)
        self.assertEqual(preflight.SUITES['test_runtime_release_upgrade.py'], 39)
        self.assertEqual(sum(n for k, n in preflight.CURRENT_HELPER_SUITES.items()
                             if k not in preflight.OWNERSHIP_SUITES), 114)
        self.assertEqual({k: preflight.CURRENT_HELPER_SUITES[k] for k in preflight.OWNERSHIP_SUITES},
                         {'test_deployment_plan.py': 29, 'test_deployment_identity.py': 19,
                          'test_recovery_journal.py': 20})
        calls, _, _, _ = self.invoke(uid=1000, environment={'GITHUB_ACTIONS': 'true'})
        observed = {}
        for command, options in calls:
            if options.get('cwd') != ROOT or preflight.LOADER not in command:
                continue
            loader = command.index(preflight.LOADER)
            path = Path(command[loader + 1]); minimum = int(command[loader + 2])
            if path.name not in preflight.CURRENT_HELPER_SUITES:
                continue
            self.assertEqual(path.parent, ROOT / '.github/scripts')
            self.assertEqual(options['env']['ASTRA_CI_EXPECTED_SHA'], 'b' * 40)
            self.assertEqual(command[:2] == ['sudo', '-n'], path.name in preflight.OWNERSHIP_SUITES)
            self.assertTrue(options['check'])
            observed[path.name] = minimum
        self.assertEqual(observed, preflight.CURRENT_HELPER_SUITES)

    def test_all_retained_product_gates_and_decision_regressions_run_on_current_head(self):
        calls, _, _, _ = self.invoke()
        self.assertEqual([(Path(command[1]).name, command[2:]) for command, _ in calls[-5:]],
                         [('run_taker_regressions.py', []), ('run_taker_regressions.py', ['--alpha']),
                          ('run_collection_regressions.py', []), ('run_collection_regressions.py', ['--retained']),
                          ('run_decision_contract_regressions.py', [])])
        self.assertTrue(all(options['cwd'] == ROOT for _, options in calls[-5:]))

    def test_factor_collection_gate_preserves_all_original_minimums_and_requires_both_new_suites(self):
        import run_collection_regressions as collection
        import run_decision_release_preflight as preflight
        raw = guard.git(ROOT, 'show', PRE_FACTOR_RELEASE + ':.github/scripts/run_collection_regressions.py')
        assignment = next(node for node in ast.parse(raw).body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'COLLECTION_SUITES'
                                  for target in node.targets))
        original = ast.literal_eval(assignment.value)
        added = {'tests.ops.test_scheduler_timeout_diagnostics': 25,
                 'tests.venues.test_indicator_failure_dedup': 25}
        self.assertEqual(len(original), 30)
        self.assertEqual(sum(original.values()), 1036)
        self.assertEqual(collection.COLLECTION_SUITES, dict(original, **added))
        self.assertEqual(sum(collection.COLLECTION_SUITES.values()), 1086)
        self.assertEqual(preflight.CURRENT_HELPER_SUITES['test_collection_regression_gate.py'], 10)
        self.assertEqual(preflight.DECISION_RELEASE_SUITES['test_decision_release_source.py'], 105)
        calls, _, _, _ = self.invoke()
        gate = next((command, options) for command, options in calls
                    if command[-1].endswith('/run_collection_regressions.py'))
        self.assertEqual(gate[1]['cwd'], ROOT)
        self.assertTrue(gate[1]['check'])

    def test_prompt_gate_preserves_all_original_minimums_and_requires_new_suite(self):
        import run_decision_contract_regressions as decision
        raw = guard.git(ROOT, 'show', PRE_PROMPT_RELEASE + ':.github/scripts/run_decision_contract_regressions.py')
        assignment = next(node for node in ast.parse(raw).body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'TEST_SUITES'
                                  for target in node.targets))
        original = ast.literal_eval(assignment.value)
        self.assertEqual(len(original), 28)
        self.assertEqual(sum(original.values()), 474)
        self.assertEqual(decision.TEST_SUITES, dict(original, **{'tests.llm.test_prompt_exhaustion_consistency': 14}))
        self.assertEqual(sum(decision.TEST_SUITES.values()), 488)
        calls, _, _, _ = self.invoke()
        gate = next((command, options) for command, options in calls
                    if command[-1].endswith('/run_decision_contract_regressions.py'))
        self.assertEqual(gate[1]['cwd'], ROOT)
        self.assertTrue(gate[1]['check'])

    def test_current_helpers_receive_original_compatibility_archive_pins(self):
        import run_decision_release_preflight as preflight
        calls, _, extract, _ = self.invoke()
        archives = [command for command, _ in calls if 'archive' in command]
        self.assertEqual([command[-1] for command in archives],
                         [preflight.PREVIOUS_SHA, preflight.COLLECTION_BASELINE, preflight.REVIEWED_COLLECTION_SHA])
        self.assertEqual(extract.call_count, 3)
        for _, options in calls[-5:]:
            for key in ('ASTRA_PREVIOUS_SOURCE', 'ASTRA_COLLECTION_BASELINE', 'ASTRA_COLLECTION_REVIEWED_SOURCE'):
                self.assertIn(key, options['env'])

    def test_every_child_checks_returncode_and_removes_runtime_secrets(self):
        calls, _, _, _ = self.invoke()
        for _, options in calls:
            self.assertTrue(options['check'])
            self.assertNotIn('ASTRA_DATA_DIR', options['env'])
            self.assertNotIn('GITHUB_TOKEN', options['env'])
            self.assertNotIn('PROVIDER_API_KEY', options['env'])
            self.assertEqual(options['env']['OFFLINE_SUITE_RUNNING'], '1')

    def test_preflight_environment_has_only_whitelisted_values_and_fixed_git_overrides(self):
        import run_decision_release_preflight as preflight
        incoming = {
            'PATH': '/synthetic/bin', 'HOME': '/synthetic/home', 'LANG': 'C',
            'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'pull_request',
            'GITHUB_STEP_SUMMARY': '/synthetic/summary', 'GITHUB_TOKEN': 'synthetic-secret',
            'GIT_DIR': '/untrusted/git', 'GIT_WORK_TREE': '/untrusted/tree',
            'GIT_CONFIG_PARAMETERS': "'core.hooksPath=/untrusted/hooks'",
            'GIT_CONFIG_COUNT': '3', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
            'GIT_CONFIG_VALUE_0': '/untrusted/hooks', 'GIT_CONFIG_KEY_2': 'alias.status',
            'GIT_CONFIG_VALUE_2': '!untrusted-command', 'GIT_CONFIG_GLOBAL': '/untrusted/config',
            'GIT_NO_REPLACE_OBJECTS': '0', 'GIT_CONFIG_NOSYSTEM': '0',
            'PYTHONPATH': '/untrusted/python', 'PYTHONHOME': '/untrusted/pythonhome',
            'PYTHONOPTIMIZE': '2', 'PYTHONSTARTUP': '/untrusted/startup',
            'PYTHONDONTWRITEBYTECODE': '0', 'ASTRA_DATA_DIR': '/production',
            'UNRELATED_SETTING': 'must-not-survive',
        }
        with patch.dict(os.environ, incoming, clear=True):
            env = preflight.preflight_environment()
        self.assertEqual(env, {
            'PATH': '/synthetic/bin', 'HOME': '/synthetic/home', 'LANG': 'C',
            'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'pull_request',
            'GITHUB_STEP_SUMMARY': '/synthetic/summary', 'PYTHONDONTWRITEBYTECODE': '1',
            'OFFLINE_SUITE_RUNNING': '1', 'ASTRA_LEDGER_SYNC_DISABLED': '1',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1',
            'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
            'GIT_CONFIG_VALUE_0': os.devnull, 'GIT_CONFIG_KEY_1': 'core.fsmonitor',
            'GIT_CONFIG_VALUE_1': 'false',
        })

    def test_all_preflight_children_including_deployed_probe_use_fixed_safe_environment(self):
        injected = {
            'GIT_DIR': '/untrusted/git', 'GIT_WORK_TREE': '/untrusted/tree',
            'GIT_CONFIG_PARAMETERS': "'core.hooksPath=/untrusted/hooks'",
            'GIT_CONFIG_COUNT': '99', 'GIT_CONFIG_KEY_0': 'alias.status',
            'GIT_CONFIG_VALUE_0': '!untrusted-command', 'GIT_CONFIG_KEY_98': 'core.hooksPath',
            'GIT_CONFIG_VALUE_98': '/untrusted/hooks', 'PYTHONPATH': '/untrusted/python',
            'PYTHONOPTIMIZE': '1', 'PYTHONSTARTUP': '/untrusted/startup',
        }
        import run_decision_release_preflight as preflight
        calls, _, _, _ = self.invoke(environment=injected, deployed=preflight.DEPLOYMENT_PREDECESSOR['source'])
        self.assertEqual(calls[0][0], ['git', '-C', '/synthetic-deployed', 'rev-parse', '--show-toplevel'])
        for command, options in calls:
            with self.subTest(command=command):
                env = options['env']
                expected = {'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
                            'GIT_CONFIG_VALUE_0': os.devnull, 'GIT_CONFIG_KEY_1': 'core.fsmonitor',
                            'GIT_CONFIG_VALUE_1': 'false', 'GIT_NO_REPLACE_OBJECTS': '1',
                            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                            'GIT_TERMINAL_PROMPT': '0'}
                self.assertEqual({key: value for key, value in env.items() if key.startswith('GIT_')}, expected)
                self.assertEqual({key: value for key, value in env.items() if key.startswith('PYTHON')},
                                 {'PYTHONDONTWRITEBYTECODE': '1'})
                self.assertTrue(options['check'])

    def test_real_synthetic_worktree_cannot_run_local_post_checkout_hook(self):
        import run_decision_release_preflight as preflight
        with tempfile.TemporaryDirectory(prefix='decision-hook-refusal-') as temporary:
            root = Path(temporary); source = root / 'source'; source.mkdir()
            (source / 'file').write_bytes(b'synthetic hook fixture, not production history')
            guard.git(source, 'init', '--quiet'); guard.git(source, 'add', 'file')
            tree = guard.git(source, 'write-tree').decode().strip()
            revision = guard.git(source, '-c', 'user.name=Decision test fixture',
                                 '-c', 'user.email=fixture@example.invalid', 'commit-tree', tree,
                                 input=b'Synthetic hook fixture\n').decode().strip()
            marker = root / 'hook-ran'
            hook = source / '.git/hooks/post-checkout'
            hook.write_text('#!/bin/sh\nprintf hook-ran > ' + shlex.quote(str(marker)) + '\n')
            hook.chmod(0o755)
            env = preflight.preflight_environment()
            control = dict(env)
            for key in ('GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0',
                        'GIT_CONFIG_KEY_1', 'GIT_CONFIG_VALUE_1'):
                control.pop(key)
            # Positive control proves this Git installation would execute the
            # repository-local hook without the fixed child configuration.
            unprotected = root / 'unprotected'
            subprocess.run(['git', '-C', str(source), 'worktree', 'add', '--detach',
                            str(unprotected), revision], check=True, env=control,
                           capture_output=True, timeout=60)
            self.assertEqual(marker.read_bytes(), b'hook-ran')
            guard.git(source, 'worktree', 'remove', '--force', str(unprotected))
            marker.unlink()
            with patch.object(preflight, 'verify_fixture', wraps=preflight.verify_fixture) as verify:
                with preflight.historical_worktree(source, root / 'protected', revision, env) as protected:
                    self.assertEqual((protected / 'file').read_bytes(), (source / 'file').read_bytes())
                    self.assertFalse(marker.exists(), 'Local post-checkout hook ran despite fixed child environment')
                self.assertEqual(verify.call_count, 2)
                self.assertTrue(all(call.args == (root / 'protected', revision) for call in verify.call_args_list))
            self.assertFalse(marker.exists())
            self.assertFalse((root / 'protected').exists())

    def test_historical_worktree_rejects_initial_drift_before_yield_and_cleans_up(self):
        import run_decision_release_preflight as preflight
        destination = ROOT / 'synthetic-mocked-worktree'
        executed = False
        with patch.object(preflight.subprocess, 'run') as run, \
                patch.object(preflight, 'verify_fixture', side_effect=RuntimeError('initial fixture drift')) as verify, \
                self.assertRaisesRegex(RuntimeError, 'initial fixture drift'):
            with preflight.historical_worktree(ROOT, destination, guard.BASELINE, {}):
                executed = True
        self.assertFalse(executed)
        verify.assert_called_once_with(destination, guard.BASELINE)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][3:], ['worktree', 'remove', '--force', str(destination)])
        self.assertTrue(run.call_args.kwargs['check'])

    def test_historical_worktree_rejects_post_execution_drift_and_cleans_up(self):
        import run_decision_release_preflight as preflight
        destination = ROOT / 'synthetic-mocked-worktree'
        executed = False
        with patch.object(preflight.subprocess, 'run') as run, \
                patch.object(preflight, 'verify_fixture', side_effect=[None, RuntimeError('final fixture drift')]) as verify, \
                self.assertRaisesRegex(RuntimeError, 'final fixture drift'):
            with preflight.historical_worktree(ROOT, destination, guard.REVIEWED_COMMIT, {}):
                executed = True
        self.assertTrue(executed)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(run.call_args.args[0][3:], ['worktree', 'remove', '--force', str(destination)])
        self.assertTrue(run.call_args.kwargs['check'])

    def test_real_clean_status_smudged_history_is_rejected_before_old_script_execution(self):
        import run_decision_release_preflight as preflight
        with tempfile.TemporaryDirectory(prefix='decision-smudge-refusal-') as temporary:
            root = Path(temporary); source = root / 'source'; source.mkdir()
            (source / 'historical.py').write_bytes(b"print('ORIGINAL')\n")
            guard.git(source, 'init', '--quiet'); guard.git(source, 'add', 'historical.py')
            tree = guard.git(source, 'write-tree').decode().strip()
            revision = guard.git(source, '-c', 'user.name=Decision test fixture',
                                 '-c', 'user.email=fixture@example.invalid', 'commit-tree', tree,
                                 input=b'Synthetic smudge fixture\n').decode().strip()
            (source / '.git/info/attributes').write_text('historical.py filter=decision-test\n')
            guard.git(source, 'config', 'filter.decision-test.smudge', 'sed s/ORIGINAL/DRIFTED/g')
            guard.git(source, 'config', 'filter.decision-test.clean', 'sed s/DRIFTED/ORIGINAL/g')
            original_verify = preflight.verify_fixture
            observed_clean_drift = []

            def verify(destination, expected):
                self.assertEqual(guard.git(destination, 'status', '--porcelain', '--untracked-files=all'), b'')
                self.assertEqual((destination / 'historical.py').read_bytes(), b"print('DRIFTED')\n")
                observed_clean_drift.append(destination)
                original_verify(destination, expected)

            executed = False
            with patch.object(preflight, 'verify_fixture', side_effect=verify), \
                    self.assertRaisesRegex(RuntimeError, 'working bytes differ from commit'):
                with preflight.historical_worktree(source, root / 'fixture', revision,
                                                  preflight.preflight_environment()):
                    executed = True
            self.assertFalse(executed)
            self.assertEqual(observed_clean_drift, [root / 'fixture'])
            self.assertFalse((root / 'fixture').exists())

    def test_wrong_deployed_source_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'deployed source differs'):
            self.invoke(deployed='c' * 40)

    def test_deployed_predecessor_is_rechecked_after_all_acceptance(self):
        import run_decision_release_preflight as preflight
        with patch.object(preflight, 'verify_deployed_predecessor',
                          side_effect=[Path('/synthetic-deployed'), RuntimeError('late deployed drift')]) as verify, \
                self.assertRaisesRegex(RuntimeError, 'late deployed drift'):
            self.invoke(pr_only=False)
        self.assertEqual(verify.call_count, 2)

    def test_historical_preflight_failure_propagates_and_cleans_worktree(self):
        import run_decision_release_preflight as preflight
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[-1] == '--release-pr':
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=0)

        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request'}, clear=True), \
                patch.object(preflight, 'verify_decision_release_source') as verify, \
                patch.object(preflight, 'verify_fixture'), \
                patch.object(preflight.subprocess, 'run', side_effect=run), self.assertRaises(subprocess.CalledProcessError):
            preflight.run_preflight(ROOT, 'b' * 40, pr_only=True)
        self.assertEqual(calls[-1][3:6], ['worktree', 'remove', '--force'])
        self.assertEqual(verify.call_count, 1)

    def test_candidate_helper_and_product_failures_are_never_ignored(self):
        for filename in ('verify_pr_candidate.py', 'test_pr_candidate_contract.py', 'test_runtime_release_upgrade.py',
                         'test_decision_release_source.py', 'test_collection_regression_gate.py',
                         'run_collection_regressions.py', 'run_decision_contract_regressions.py'):
            with self.subTest(filename=filename), self.assertRaises(subprocess.CalledProcessError):
                self.invoke(fail=lambda command: any(str(value).endswith(filename) for value in command))

    def test_initial_and_final_source_failures_propagate(self):
        for failure in (RuntimeError('initial failure'), [None, RuntimeError('final failure')]):
            with self.subTest(failure=str(failure)), self.assertRaises(RuntimeError):
                self.invoke(verifier_fail=failure)

    def test_suite_loader_enforces_discovery_and_complete_results(self):
        import run_decision_release_preflight as preflight
        self.assertIn('loader.errors or discovered<minimum', preflight.LOADER)
        self.assertIn('require_complete_result(result,max(minimum,discovered))', preflight.LOADER)

    def test_ownership_cases_cannot_silently_skip_or_fake_root(self):
        import run_decision_release_preflight as preflight
        with patch.object(preflight.os, 'geteuid', return_value=1000), \
                patch.object(preflight.subprocess, 'run') as run, self.assertRaisesRegex(RuntimeError, 'root Linux sandbox'):
            preflight.run_suites(ROOT, {'test_deployment_plan.py': 29}, {})
        run.assert_not_called()
        with patch.object(preflight.os, 'geteuid', return_value=1000), \
                patch.object(preflight.subprocess, 'run') as run:
            preflight.run_suites(ROOT, {'test_deployment_plan.py': 29}, {'GITHUB_ACTIONS': 'true'})
        self.assertEqual(run.call_args.args[0][:2], ['sudo', '-n'])
        self.assertTrue(run.call_args.kwargs['check'])

    def test_cli_keeps_only_explicit_release_pr_option(self):
        import run_decision_release_preflight as preflight
        for flags, pr_only in (([], False), (['--release-pr'], True)):
            with patch.object(sys, 'argv', ['preflight', *flags]), \
                    patch.dict(os.environ, {'ASTRA_CI_EXPECTED_SHA': 'b' * 40}), \
                    patch.object(preflight, 'run_preflight') as run:
                preflight.main()
            run.assert_called_once_with(ROOT, 'b' * 40, pr_only)
        with patch.object(sys, 'argv', ['preflight', '--candidate-pr']), \
                patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            preflight.main()


class DeploymentPredecessorBoundaryTests(unittest.TestCase):
    """Real predecessor Git/byte checks; later acceptance is separately tested."""
    @classmethod
    def setUpClass(cls):
        import run_decision_release_preflight as preflight
        cls.preflight = preflight
        cls.temporary = tempfile.TemporaryDirectory(prefix='decision-deployed-predecessor-')
        cls.deployed = Path(cls.temporary.name) / 'deployed'
        cls.pin = dict(preflight.DEPLOYMENT_PREDECESSOR)
        cls.revision = guard.git(ROOT, 'rev-parse', 'HEAD').decode().strip()
        guard.git(ROOT, 'worktree', 'add', '--detach', str(cls.deployed), cls.pin['source'])

    @classmethod
    def tearDownClass(cls):
        try:
            guard.git(ROOT, 'worktree', 'remove', '--force', str(cls.deployed))
        finally:
            cls.temporary.cleanup()

    def inputs(self):
        return {'GITHUB_EVENT_NAME': 'workflow_dispatch',
                'ASTRA_DEPLOYED_SOURCE': str(self.deployed),
                'PREVIOUS_SHA': self.pin['source'], 'PREVIOUS_IMAGE': self.pin['image']}

    def verify(self, *, deployed=None, revision=None, inputs=None):
        with patch.dict(os.environ, self.inputs() if inputs is None else inputs, clear=True):
            return self.preflight.verify_deployed_predecessor(
                ROOT, self.deployed if deployed is None else deployed,
                self.revision if revision is None else revision,
                self.preflight.preflight_environment())

    def test_exact_current_predecessor_is_separate_from_historical_review_anchors(self):
        self.assertEqual(self.pin, {
            'source': '1bd3c016bf4375114d34022c1528d119d90cd6ad',
            'tree': '47d98d0091a86a513ce1ccc20ced1096fce9d93d',
            'image': 'ghcr.io/jonoka/astra-quant-agent@sha256:7bfc6bb0b75c6a63393f1c717c771cc7097059b2b2c4a6ddca2de86049005bee',
        })
        self.assertEqual(guard.BASELINE, '60133ce82d4a0b91f94bc0c613a07e24aff049f5')
        self.assertEqual(guard.REVIEWED_COMMIT, '71ff89f554b797558c721cc3f93051624f911e18')
        self.assertEqual(self.verify(), self.deployed)

    def test_actual_workflow_inputs_reach_historical_stage_after_real_predecessor_checks(self):
        import yaml
        workflow = yaml.safe_load((ROOT / '.github/workflows/build-release-image.yml').read_text())
        self.assertEqual(workflow['env']['PREVIOUS_SHA'], '${{ inputs.previous_source }}')
        self.assertEqual(workflow['env']['PREVIOUS_IMAGE'], '${{ inputs.previous_image }}')
        steps = workflow['jobs']['build']['steps']
        checkout = next(step for step in steps if step.get('with', {}).get('path') == 'deployed')
        self.assertEqual(checkout['with']['ref'], '${{ env.PREVIOUS_SHA }}')
        step = next(step for step in steps if step.get('run') == 'python .github/scripts/run_decision_release_preflight.py')
        self.assertEqual(step['env']['ASTRA_DEPLOYED_SOURCE'], '${{ github.workspace }}/deployed')
        # Exercise the formal input path with real HEAD/tree/ancestry/raw bytes.
        # The initial candidate guard has its own unmocked proof; stop before
        # expensive historical/application acceptance, never claim those ran here.
        with patch.dict(os.environ, self.inputs(), clear=True), \
                patch.object(self.preflight, 'verify_decision_release_source'), \
                patch.object(self.preflight, 'historical_worktree', side_effect=RuntimeError('historical boundary reached')) as historical, \
                patch.object(self.preflight, 'verify_checkout', wraps=self.preflight.verify_checkout) as raw, \
                self.assertRaisesRegex(RuntimeError, 'historical boundary reached'):
            self.preflight.run_preflight(ROOT, self.revision)
        raw.assert_called_once_with(self.deployed, self.pin['source'])
        self.assertEqual(historical.call_args.args[2], guard.BASELINE)

    def test_formal_mode_and_partial_pr_inputs_cannot_skip_predecessor_verification(self):
        fields = ('ASTRA_DEPLOYED_SOURCE', 'PREVIOUS_SHA', 'PREVIOUS_IMAGE')
        for mask in range(7):
            values = {name: self.inputs()[name] for index, name in enumerate(fields) if mask & (1 << index)}
            values['GITHUB_EVENT_NAME'] = 'pull_request'
            for pr_only in (False, True):
                if pr_only and mask == 0:
                    continue  # An ordinary PR has no deployment identity.
                with self.subTest(mask=mask, pr_only=pr_only), patch.dict(os.environ, values, clear=True), \
                        patch.object(self.preflight, 'verify_decision_release_source') as source, \
                        self.assertRaisesRegex(RuntimeError, 'complete pinned deployment predecessor inputs'):
                    self.preflight.run_preflight(ROOT, self.revision, pr_only)
                source.assert_not_called()

    def test_wrong_missing_old_and_arbitrary_well_formed_workflow_identity_are_rejected(self):
        variants = [dict(self.inputs(), PREVIOUS_SHA=value) for value in ('', guard.BASELINE, 'c' * 40)]
        variants += [dict(self.inputs(), PREVIOUS_IMAGE=value) for value in (
            '', 'ghcr.io/jonoka/astra-quant-agent@sha256:' + 'c' * 64,
            'ghcr.io/jonoka/astra-quant-agent@sha256:9247ceff7e1338c44e2d1592aa8c34e927e6f08784bfc1f0a622ce0b271f7ca6')]
        for values in variants:
            with self.subTest(values=values), self.assertRaisesRegex(RuntimeError, 'workflow inputs differ'):
                self.verify(inputs=values)

    def test_wrong_checkout_head_nested_root_and_tree_are_rejected(self):
        for path in (ROOT, self.deployed / '.github'):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'deployed source differs'):
                self.verify(deployed=path)
        with patch.dict(self.preflight.DEPLOYMENT_PREDECESSOR, {'tree': 'c' * 40}), \
                self.assertRaisesRegex(RuntimeError, 'deployed source differs'):
            self.verify()

    def test_dirty_hidden_nonregular_and_mode_drift_are_rejected_with_real_git(self):
        path = self.deployed / 'Dockerfile'
        original = path.read_bytes()
        mutations = ('bytes', 'assume-unchanged', 'skip-worktree', 'executable', 'symlink')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                try:
                    if mutation in ('assume-unchanged', 'skip-worktree'):
                        guard.git(self.deployed, 'update-index', '--' + mutation, 'Dockerfile')
                    if mutation in ('bytes', 'assume-unchanged', 'skip-worktree'):
                        path.write_bytes(original + b'\n# changed predecessor\n')
                    elif mutation == 'executable':
                        path.chmod(0o755)
                    else:
                        path.unlink(); path.symlink_to('README.md')
                    with self.assertRaises(RuntimeError):
                        self.verify()
                finally:
                    guard.git(self.deployed, 'update-index', '--no-assume-unchanged', 'Dockerfile')
                    guard.git(self.deployed, 'update-index', '--no-skip-worktree', 'Dockerfile')
                    if path.is_symlink():
                        path.unlink()
                    path.write_bytes(original); path.chmod(0o644)
        self.assertEqual(self.verify(), self.deployed)

    def test_candidate_must_strictly_descend_from_the_current_deployed_source(self):
        with self.assertRaisesRegex(RuntimeError, 'candidate equals'):
            self.verify(revision=self.pin['source'])
        with self.assertRaises(subprocess.CalledProcessError):
            self.verify(revision=guard.BASELINE)

    def test_head_drift_during_raw_scan_is_rejected_even_with_identical_tree(self):
        alias = guard.git(self.deployed, '-c', 'user.name=Decision test fixture',
                          '-c', 'user.email=fixture@example.invalid', 'commit-tree', self.pin['tree'],
                          '-p', self.pin['source'], input=b'Synthetic same-tree identity drift\n').decode().strip()
        original_verify = self.preflight.verify_checkout

        def drift_after_scan(source, revision):
            original_verify(source, revision)
            guard.git(source, 'update-ref', 'HEAD', alias)

        try:
            with patch.object(self.preflight, 'verify_checkout', side_effect=drift_after_scan), \
                    self.assertRaisesRegex(RuntimeError, 'deployed source differs'):
                self.verify()
        finally:
            guard.git(self.deployed, 'update-ref', 'HEAD', self.pin['source'])
        self.assertEqual(self.verify(), self.deployed)


class DecisionWorkflowBoundaryTests(unittest.TestCase):
    def test_pr_workflow_remains_pr_only_read_only_and_keeps_original_modes(self):
        import yaml
        raw = (ROOT / '.github/workflows/test-taker-parsing.yml').read_text()
        workflow = yaml.safe_load(raw)
        self.assertEqual(set(workflow.get('on', workflow.get(True))), {'pull_request'})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        steps = workflow['jobs']['taker-regressions']['steps']
        preflight = next(step for step in steps if step.get('id') == 'preflight')
        self.assertIn('decision_release_contract.json', preflight['run'])
        self.assertIn('run_decision_release_preflight.py --release-pr', preflight['run'])
        for original in ('--candidate-pr', '--release-pr', '--collection-pr'):
            self.assertIn('run_deployment_preflight.py ' + original, preflight['run'])
        for forbidden in ('continue-on-error', 'docker push', 'workflow_dispatch', 'secrets.', 'packages: write'):
            self.assertNotIn(forbidden, raw)

    def test_final_pr_verifier_reuses_original_mode_without_downgrade(self):
        import yaml
        workflow = yaml.safe_load((ROOT / '.github/workflows/test-taker-parsing.yml').read_text())
        final = workflow['jobs']['taker-regressions']['steps'][-1]
        self.assertEqual(final['env']['ASTRA_PR_MODE'], '${{ steps.preflight.outputs.mode }}')
        self.assertIn("mode = os.environ['ASTRA_PR_MODE']", final['run'])
        self.assertIn("mode == 'decision-release'", final['run'])
        self.assertIn("GITHUB_EVENT_NAME') != 'pull_request'", final['run'])
        self.assertIn('verify_decision_release_source(Path.cwd(), UPSTREAM_SHA', final['run'])
        self.assertNotIn('.is_file()', final['run'])
        self.assertIn('Missing or invalid original PR verification mode', final['run'])

    def test_build_workflow_requires_new_source_guard_at_all_three_existing_boundaries(self):
        raw = (ROOT / '.github/workflows/build-release-image.yml').read_text()
        self.assertEqual(raw.count('python ../ci/.github/scripts/verify_decision_release_source.py . '), 1)
        self.assertEqual(raw.count('python ci/.github/scripts/verify_decision_release_source.py validation '), 1)
        self.assertEqual(raw.count('python ci/.github/scripts/verify_decision_release_source.py source '), 1)
        self.assertIn('run: python .github/scripts/run_decision_release_preflight.py', raw)
        self.assertNotIn('run: python .github/scripts/run_deployment_preflight.py', raw)
        self.assertNotIn('continue-on-error', raw)

    def test_build_keeps_old_source_tests_on_exact_601_fixture(self):
        raw = (ROOT / '.github/workflows/build-release-image.yml').read_text()
        self.assertIn('worktree add --detach "$GITHUB_WORKSPACE/collection-release" ' + guard.BASELINE, raw)
        self.assertIn('for target in .github/scripts/test_verify_deployment_source.py ', raw)
        self.assertIn('target = Path("../collection-release") / target', raw)
        self.assertIn('assert suite.countTestCases() > 0', raw)
        self.assertIn('assert result.wasSuccessful()', raw)

    def test_old_contracts_verifiers_preflights_and_tests_remain_original_bytes(self):
        preserved = ('.github/scripts/collection_source_contract.json', '.github/scripts/collection_release_contract.json',
                     guard.HISTORICAL_VERIFIER_PATH, '.github/scripts/test_verify_deployment_source.py',
                     '.github/scripts/test_release_source_contract.py', guard.CANDIDATE_CONTRACT_PATH,
                     '.github/scripts/verify_pr_candidate.py', '.github/scripts/run_deployment_preflight.py',
                     '.github/scripts/test_pr_candidate_contract.py')
        for relative in preserved:
            with self.subTest(relative=relative):
                self.assertEqual((ROOT / relative).read_bytes(), guard.git(ROOT, 'show', guard.REVIEWED_COMMIT + ':' + relative))


if __name__ == '__main__':
    unittest.main()
