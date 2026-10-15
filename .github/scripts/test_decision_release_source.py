"""Decision-release refusal tests. Mocked history successes are not Git proof.

Real production ancestry and legacy source acceptance must additionally pass the
unmocked source guard on the exact committed release checkout.
"""
from __future__ import annotations

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

    def test_each_of_the_nine_release_paths_is_required(self):
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
        self.assertEqual(len(guard.RELEASE_INCREMENT_PATHS), 9)

    def test_wal_increment_is_exactly_three_fixed_tool_paths_with_both_hashes(self):
        prior = '16e12871e51e7cfb13823a370dcbf9bdec9f0804'
        old = json.loads(guard.git(ROOT, 'show', prior + ':' + guard.CONTRACT_PATH))
        added = {'.github/scripts/runtime_release_upgrade.py',
                 '.github/scripts/test_runtime_release_upgrade.py',
                 '.github/scripts/test_deployment_plan.py'}
        self.assertEqual(set(self.contract['release_paths']) - set(old['release_paths']), added)
        self.assertEqual(self.contract['reviewed_paths'], old['reviewed_paths'])
        self.assertEqual(self.contract['self_hash_exemptions'], old['self_hash_exemptions'])
        for relative in added:
            pin = self.contract['release_paths'][relative]
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
    def invoke(self, *, fail=None, verifier_fail=None, pr_only=False, deployed=None, environment=None, uid=0):
        import run_decision_release_preflight as preflight
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if fail and fail(command):
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=0, stdout=((deployed or guard.BASELINE) + '\n').encode())

        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request', 'ASTRA_DATA_DIR': '/production',
                                    'GITHUB_TOKEN': 'synthetic', 'PROVIDER_API_KEY': 'synthetic',
                                    **(environment or {})}, clear=True), \
                patch.object(preflight, 'verify_decision_release_source', side_effect=verifier_fail) as verify, \
                patch.object(preflight, 'verify_fixture'), \
                patch.object(preflight.subprocess, 'run', side_effect=run), patch.object(preflight, 'extract') as extract, \
                patch.object(preflight.os, 'geteuid', return_value=uid), patch('sys.stdout', new_callable=io.StringIO) as output:
            if deployed:
                os.environ['ASTRA_DEPLOYED_SOURCE'] = '/synthetic-deployed'
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
        _, verify, _, output = self.invoke()
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
        calls, _, _, _ = self.invoke(environment=injected, deployed=guard.BASELINE)
        self.assertEqual(calls[0][0], ['git', '-C', '/synthetic-deployed', 'rev-parse', 'HEAD'])
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

    def test_historical_preflight_failure_propagates_and_cleans_worktree(self):
        import run_decision_release_preflight as preflight
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[-1] == '--release-pr':
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=0)

        with patch.dict(os.environ, {}, clear=True), patch.object(preflight, 'verify_decision_release_source') as verify, \
                patch.object(preflight, 'verify_fixture'), \
                patch.object(preflight.subprocess, 'run', side_effect=run), self.assertRaises(subprocess.CalledProcessError):
            preflight.run_preflight(ROOT, 'b' * 40)
        self.assertEqual(calls[-1][3:6], ['worktree', 'remove', '--force'])
        self.assertEqual(verify.call_count, 1)

    def test_candidate_helper_and_product_failures_are_never_ignored(self):
        for filename in ('verify_pr_candidate.py', 'test_pr_candidate_contract.py', 'test_runtime_release_upgrade.py',
                         'test_decision_release_source.py', 'run_decision_contract_regressions.py'):
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
