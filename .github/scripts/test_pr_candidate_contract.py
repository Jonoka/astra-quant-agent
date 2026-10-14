"""Offline candidate contract refusals; synthetic Git is not history proof."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import verify_pr_candidate as guard

ROOT = Path(__file__).resolve().parents[2]


class CandidateSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = guard.candidate_contract(ROOT / guard.CONTRACT_PATH)
        cls.revision = 'b' * 40
        cls.current = {name: (ROOT / name).read_bytes() for name in cls.contract['candidate_paths']}
        cls.previous = {}
        for name in cls.current:
            if guard.git(ROOT, 'ls-tree', '--name-only', guard.BASELINE, '--', name):
                cls.previous[name] = guard.git(ROOT, 'show', guard.BASELINE + ':' + name)

    def verify(self, *, revision=None, head=None, tree=None, root=None, delta=None,
               dirty=b'', mode=None, missing=None, mutation=None, working_mutation=None,
               ancestry=True, new_exists=None, contract_path=None):
        revision = self.revision if revision is None else revision

        def git(source, *args):
            verb, rest = args[0], args[1:]
            if verb == 'rev-parse':
                result = {'HEAD': head or revision, '--show-toplevel': root or str(ROOT),
                          guard.BASELINE + '^{tree}': tree or guard.BASELINE_TREE}[rest[0]]
                return (result + '\n').encode()
            if verb == 'merge-base':
                self.assertEqual(rest, ('--is-ancestor', guard.BASELINE, revision))
                if not ancestry:
                    raise subprocess.CalledProcessError(1, ['git', *args])
                return b''
            if verb == 'diff':
                self.assertEqual(rest, ('--no-renames', '--name-only', '-z', guard.BASELINE, revision))
                return b'\0'.join(name.encode() for name in sorted(self.current if delta is None else delta))
            if verb == 'status':
                self.assertEqual(rest, ('--porcelain', '--untracked-files=all'))
                return dirty
            if verb == 'ls-files':
                self.assertEqual(rest, ('-v', '-z'))
                return b'\0'.join(b'H ' + name.encode() for name in sorted(self.current))
            if verb == 'ls-tree':
                rev, relative = rest[1], rest[-1]
                if missing == (rev, relative):
                    return b''
                if rev == guard.BASELINE and relative not in self.previous and relative != new_exists:
                    return b''
                file_mode, kind = ('100644', 'blob') if mode is None or mode[:2] != (rev, relative) else mode[2:]
                return (file_mode + ' ' + kind + ' ' + 'd' * 40 + '\t' + relative + '\0').encode()
            if verb == 'show':
                rev, relative = rest[0].split(':', 1)
                raw = (self.previous if rev == guard.BASELINE else self.current)[relative]
                return raw + b'\n# unreviewed\n' if mutation == (rev, relative) else raw
            raise AssertionError('Unexpected synthetic Git operation: ' + str(args))

        original_regular = guard.regular_file

        def regular(source, relative):
            raw = original_regular(source, relative)
            return raw + b'\n# unreviewed\n' if relative == working_mutation else raw

        with patch.object(guard, 'git', side_effect=git), patch.object(guard, 'regular_file', side_effect=regular), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            guard.verify_pr_candidate(ROOT, contract_path or ROOT / guard.CONTRACT_PATH, revision)
        return output.getvalue()

    def reject_contract(self, contract, message=None):
        with patch.object(Path, 'read_bytes', return_value=json.dumps(contract).encode()):
            with self.assertRaisesRegex(RuntimeError, message or '.'):
                guard.candidate_contract(ROOT / guard.CONTRACT_PATH)

    def test_exact_synthetic_candidate_fixture_has_no_release_eligibility(self):
        self.assertIn('release_eligibility=not_granted', self.verify())

    def test_revision_must_be_exact_nonbaseline_commit(self):
        for value in ('HEAD', 'b' * 39, 'B' * 40, guard.BASELINE):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'revision'):
                self.verify(revision=value)

    def test_wrong_checked_out_head_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'source commit'):
            self.verify(head='c' * 40)

    def test_wrong_baseline_tree_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'baseline tree'):
            self.verify(tree='c' * 40)

    def test_baseline_must_be_actual_ancestor(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.verify(ancestry=False)

    def test_nested_or_different_repository_root_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'checkout root'):
            self.verify(root=str(ROOT.parent))

    def test_explicit_contract_cannot_be_replaced_by_another_path(self):
        with self.assertRaisesRegex(RuntimeError, 'explicit candidate contract'):
            self.verify(contract_path=ROOT / 'other.json')

    def test_every_candidate_path_is_required(self):
        for relative in self.current:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'delta paths'):
                self.verify(delta=set(self.current) - {relative})

    def test_unknown_application_and_github_paths_are_rejected(self):
        for relative in ('scripts/unreviewed.py', '.github/scripts/unreviewed.py',
                         '.github/workflows/unreviewed.yml', '.github/workflows/build-release-image.yml'):
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'delta paths'):
                self.verify(delta=set(self.current) | {relative})

    def test_modified_staged_and_untracked_checkout_is_rejected(self):
        for dirty in (b' M Dockerfile\n', b'M  Dockerfile\n', b'?? unknown.py\n'):
            with self.subTest(dirty=dirty), self.assertRaisesRegex(RuntimeError, 'dirty or untracked'):
                self.verify(dirty=dirty)

    def reject_real_hidden_index(self, flag):
        with tempfile.TemporaryDirectory(prefix='candidate-hidden-index-') as temporary:
            root = Path(temporary)
            guard.git(root, 'init', '--quiet')
            target = root / 'unrelated-tracked-file.txt'
            target.write_bytes(b'original fixture bytes')
            guard.git(root, 'add', target.name)
            guard.require_visible_index(root)
            guard.git(root, 'update-index', flag, target.name)
            target.write_bytes(b'hidden unreviewed bytes')
            with self.assertRaisesRegex(RuntimeError, 'index hides tracked working changes'):
                guard.require_visible_index(root)

    def test_real_assume_unchanged_cannot_hide_other_tracked_bytes(self):
        self.reject_real_hidden_index('--assume-unchanged')

    def test_real_skip_worktree_cannot_hide_other_tracked_bytes(self):
        self.reject_real_hidden_index('--skip-worktree')

    def test_all_existing_predecessor_hashes_are_checked(self):
        for relative in self.previous:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'predecessor bytes'):
                self.verify(mutation=(guard.BASELINE, relative))

    def test_existing_predecessor_cannot_disappear(self):
        relative = next(iter(self.previous))
        with self.assertRaisesRegex(RuntimeError, 'predecessor path missing'):
            self.verify(missing=(guard.BASELINE, relative))

    def test_new_paths_must_not_exist_at_baseline(self):
        for relative in set(self.current) - set(self.previous):
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'exists in baseline'):
                self.verify(new_exists=relative)

    def test_all_candidate_paths_refuse_deletion(self):
        for relative in self.current:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'path deleted'):
                self.verify(missing=(self.revision, relative))

    def test_candidate_git_executable_symlink_and_gitlink_modes_are_rejected(self):
        for relative in self.current:
            for mode, kind in (('100755', 'blob'), ('120000', 'blob'), ('160000', 'commit')):
                with self.subTest(relative=relative, mode=mode), self.assertRaisesRegex(RuntimeError, 'Git mode or type'):
                    self.verify(mode=(self.revision, relative, mode, kind))

    def test_baseline_modes_are_checked_separately(self):
        for relative in self.previous:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'Git mode or type'):
                self.verify(mode=(guard.BASELINE, relative, '100755', 'blob'))

    def test_all_committed_candidate_bytes_must_match_checkout(self):
        for relative in self.current:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'working bytes differ'):
                self.verify(mutation=(self.revision, relative))

    def test_all_working_candidate_bytes_must_match_commit(self):
        for relative in self.current:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'working bytes differ'):
                self.verify(working_mutation=relative)

    def test_committed_and_working_tamper_cannot_repin_candidate(self):
        for relative in set(self.current) - guard.SELF_HASH_EXEMPTIONS:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'Unreviewed candidate bytes'):
                self.verify(mutation=(self.revision, relative), working_mutation=relative)

    def test_exempt_roots_must_match_independently_executing_bytes(self):
        for relative in guard.SELF_HASH_EXEMPTIONS:
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, 'Executing candidate trust root'):
                self.verify(mutation=(self.revision, relative), working_mutation=relative)

    def test_only_two_explicit_self_hash_exemptions_exist(self):
        self.assertEqual(set(self.contract['self_hash_exemptions']), {guard.CONTRACT_PATH, guard.VERIFIER_PATH})
        self.assertEqual({path for path, pin in self.contract['candidate_paths'].items() if pin['sha256'] is None},
                         guard.SELF_HASH_EXEMPTIONS)

    def test_anchor_schema_and_purpose_cannot_change(self):
        for field, value in (('schema', 2), ('schema', True), ('purpose', 'release'),
                             ('baseline', 'c' * 40), ('baseline_tree', 'c' * 40)):
            changed = copy.deepcopy(self.contract); changed[field] = value
            with self.subTest(field=field):
                self.reject_contract(changed)

    def test_unknown_contract_fields_are_rejected(self):
        changed = copy.deepcopy(self.contract); changed['allow_release'] = True
        self.reject_contract(changed, 'contract fields')

    def test_duplicate_json_fields_are_rejected(self):
        with patch.object(Path, 'read_bytes', return_value=b'{"schema":1,"schema":1}'):
            with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
                guard.candidate_contract(ROOT / guard.CONTRACT_PATH)

    def test_contract_paths_are_fixed_application_and_exact_seven_ci_files(self):
        self.assertEqual(len(guard.CI_PATHS), 7)
        self.assertEqual(len(guard.APPLICATION_PATHS), 19)
        for relative in ('../escape', '.github/workflows/build-release-image.yml', '.github/scripts/unreviewed.py'):
            changed = copy.deepcopy(self.contract)
            changed['candidate_paths'][relative] = next(iter(changed['candidate_paths'].values()))
            self.reject_contract(changed, 'contract paths')

    def test_exemptions_cannot_be_added_duplicated_or_removed(self):
        for exemptions in ([], list(guard.SELF_HASH_EXEMPTIONS) * 2,
                           [guard.CONTRACT_PATH, '.github/scripts/run_deployment_preflight.py']):
            changed = copy.deepcopy(self.contract); changed['self_hash_exemptions'] = exemptions
            self.reject_contract(changed, 'trust root')

    def test_pin_fields_hashes_and_modes_are_strict(self):
        relative = 'scripts/brain/packages.py'
        for field, value in (('sha256', None), ('sha256', 'c' * 63), ('mode', '100755'),
                             ('base_mode', '100755'), ('base_sha256', 'wrong')):
            changed = copy.deepcopy(self.contract); changed['candidate_paths'][relative][field] = value
            self.reject_contract(changed)
        changed = copy.deepcopy(self.contract); changed['candidate_paths'][relative]['unknown'] = True
        self.reject_contract(changed, 'pin fields')

    def test_absent_predecessor_hash_and_mode_must_agree(self):
        for field, value in (('base_sha256', 'c' * 64), ('base_mode', '100644')):
            changed = copy.deepcopy(self.contract); changed['candidate_paths'][guard.CONTRACT_PATH][field] = value
            self.reject_contract(changed)

    def test_working_files_reject_real_executable_symlink_and_missing(self):
        with tempfile.TemporaryDirectory(prefix='candidate-mode-refusal-') as temporary:
            root = Path(temporary); path = root / 'fixture'
            path.write_bytes(b'synthetic')
            path.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'working mode'):
                guard.regular_file(root, 'fixture')
            path.unlink(); path.symlink_to('missing')
            with self.assertRaisesRegex(RuntimeError, 'regular file'):
                guard.regular_file(root, 'fixture')
            path.unlink()
            with self.assertRaisesRegex(RuntimeError, 'regular file'):
                guard.regular_file(root, 'fixture')

    def test_working_parent_directory_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='candidate-parent-refusal-') as temporary:
            root = Path(temporary); (root / 'real').mkdir(); (root / 'real/file').write_bytes(b'synthetic')
            (root / 'link').symlink_to(root / 'real', target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'Symlinked candidate'):
                guard.regular_file(root, 'link/file')

    def test_historical_contracts_verifier_and_release_workflow_are_unchanged(self):
        for relative in ('.github/scripts/collection_source_contract.json', '.github/scripts/collection_release_contract.json',
                         '.github/scripts/verify_deployment_source.py', '.github/workflows/build-release-image.yml',
                         '.github/scripts/test_verify_deployment_source.py', '.github/scripts/test_release_source_contract.py'):
            with self.subTest(relative=relative):
                self.assertEqual((ROOT / relative).read_bytes(), guard.git(ROOT, 'show', guard.BASELINE + ':' + relative))

    def test_unchanged_release_gate_rejects_real_candidate_history(self):
        from verify_deployment_source import UPSTREAM_SHA, verify_release_source
        revision = guard.git(ROOT, 'rev-parse', 'HEAD').decode().strip()
        with self.assertRaisesRegex(AssertionError, 'Missing or unreviewed release increment'):
            verify_release_source(ROOT, UPSTREAM_SHA, revision)


class CandidatePreflightBoundaryTests(unittest.TestCase):
    def invoke(self, *, fail=None, verifier_fail=None):
        import run_deployment_preflight as preflight
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if fail and fail(command):
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=0)

        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request', 'ASTRA_DATA_DIR': '/production',
                                    'GITHUB_TOKEN': 'synthetic-secret', 'PROVIDER_API_KEY': 'synthetic-secret'}), \
                patch.object(guard, 'verify_pr_candidate', side_effect=verifier_fail) as verify, \
                patch.object(preflight.subprocess, 'run', side_effect=run), patch('sys.stdout', new_callable=io.StringIO) as output:
            preflight.run_candidate_preflight(ROOT, 'b' * 40)
        return calls, verify, output.getvalue()

    def test_candidate_mode_rejects_non_pr_and_missing_event_before_verification(self):
        import run_deployment_preflight as preflight
        for event in ('', 'push', 'workflow_dispatch', 'pull_request_target'):
            with self.subTest(event=event), patch.dict(os.environ, {'GITHUB_EVENT_NAME': event}), \
                    patch.object(guard, 'verify_pr_candidate') as verify:
                with self.assertRaisesRegex(RuntimeError, 'restricted to pull_request'):
                    preflight.run_candidate_preflight(ROOT, 'b' * 40)
                verify.assert_not_called()

    def test_candidate_and_release_flags_are_mutually_exclusive(self):
        import run_deployment_preflight as preflight
        for other in ('--release-pr', '--collection-pr'):
            with patch.object(sys, 'argv', ['preflight', '--candidate-pr', other]), \
                    patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                preflight.main()

    def test_default_and_release_modes_still_require_historical_release_gate(self):
        import run_deployment_preflight as preflight
        for args in ([], ['--release-pr']):
            with patch.object(sys, 'argv', ['preflight', *args]), patch.dict(os.environ, {'ASTRA_CI_EXPECTED_SHA': 'b' * 40}), \
                    patch.object(preflight, 'verify_release_source', side_effect=RuntimeError('historical release required')) as release, \
                    patch.object(preflight, 'run_candidate_preflight') as candidate:
                with self.assertRaisesRegex(RuntimeError, 'historical release required'):
                    preflight.main()
                release.assert_called_once(); candidate.assert_not_called()

    def test_collection_mode_still_requires_historical_collection_gate(self):
        import run_deployment_preflight as preflight
        with patch.object(sys, 'argv', ['preflight', '--collection-pr']), patch.dict(os.environ, {'ASTRA_CI_EXPECTED_SHA': 'b' * 40}), \
                patch.object(preflight, 'verify_collection_source', side_effect=RuntimeError('historical collection required')) as collection, \
                patch.object(preflight, 'run_candidate_preflight') as candidate:
            with self.assertRaisesRegex(RuntimeError, 'historical collection required'):
                preflight.main()
            collection.assert_called_once(); candidate.assert_not_called()

    def test_baseline_worktree_runs_unchanged_release_preflight_with_exact_revision(self):
        calls, verify, output = self.invoke()
        add = calls[0][0]
        self.assertEqual(add[:6], ['git', '-C', str(ROOT), 'worktree', 'add', '--detach'])
        self.assertEqual(add[-1], guard.BASELINE)
        command, options = calls[1]
        self.assertEqual(command[-1], '--release-pr')
        self.assertEqual(command[1], str(Path(add[-2]) / '.github/scripts/run_deployment_preflight.py'))
        self.assertEqual(options['env']['ASTRA_CI_EXPECTED_SHA'], guard.BASELINE)
        self.assertEqual(calls[2][0][3:6], ['worktree', 'remove', '--force'])
        self.assertEqual(verify.call_count, 2)
        self.assertIn('release_eligibility=not_granted', output)

    def test_candidate_runs_all_four_retained_application_gates_plus_decisions(self):
        calls, _, _ = self.invoke()
        actual = [(Path(command[1]).name, command[2:]) for command, _ in calls[-5:]]
        self.assertEqual(actual, [('run_taker_regressions.py', []), ('run_taker_regressions.py', ['--alpha']),
                                  ('run_collection_regressions.py', []), ('run_collection_regressions.py', ['--retained']),
                                  ('run_decision_contract_regressions.py', [])])

    def test_candidate_refusal_suites_are_mandatory_before_application_tests(self):
        import run_deployment_preflight as preflight
        calls, _, _ = self.invoke()
        self.assertEqual({Path(command[3]).name for command, _ in calls[3:-5]}, set(preflight.CANDIDATE_SUITES))
        self.assertEqual(set(preflight.CANDIDATE_SUITES), {'test_pr_candidate_contract.py', 'test_decision_regression_gate.py'})
        self.assertTrue(all(options['check'] for _, options in calls))

    def test_child_environment_removes_production_settings_and_credentials(self):
        calls, _, _ = self.invoke()
        for _, options in calls:
            env = options['env']
            self.assertNotIn('ASTRA_DATA_DIR', env)
            self.assertNotIn('GITHUB_TOKEN', env)
            self.assertNotIn('PROVIDER_API_KEY', env)
            self.assertEqual(env['OFFLINE_SUITE_RUNNING'], '1')
            self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')

    def test_baseline_failure_is_propagated_and_worktree_cleanup_is_attempted(self):
        import run_deployment_preflight as preflight
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[-1] == '--release-pr':
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=0)

        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request'}), patch.object(guard, 'verify_pr_candidate') as verify, \
                patch.object(preflight.subprocess, 'run', side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                preflight.run_candidate_preflight(ROOT, 'b' * 40)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1][3:6], ['worktree', 'remove', '--force'])
        self.assertEqual(verify.call_count, 1)

    def test_candidate_application_failure_is_not_ignored(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke(fail=lambda command: len(command) > 1 and command[1].endswith('run_decision_contract_regressions.py'))

    def test_before_and_after_verifier_failures_are_not_ignored(self):
        for failure in (RuntimeError('before failure'), [None, RuntimeError('after failure')]):
            with self.subTest(failure=str(failure)), self.assertRaises(RuntimeError):
                self.invoke(verifier_fail=failure)

    def test_workflow_is_pr_only_and_reuses_original_mode_for_final_verification(self):
        import yaml
        workflow = yaml.safe_load((ROOT / '.github/workflows/test-taker-parsing.yml').read_text())
        self.assertEqual(set(workflow.get('on', workflow.get(True))), {'pull_request'})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        steps = workflow['jobs']['taker-regressions']['steps']
        first = next(step for step in steps if step.get('id') == 'preflight')
        self.assertIn('pr_candidate_contract.json', first['run'])
        self.assertIn('run_deployment_preflight.py --candidate-pr', first['run'])
        self.assertIn('run_deployment_preflight.py --release-pr', first['run'])
        self.assertIn('run_deployment_preflight.py --collection-pr', first['run'])
        final = steps[-1]
        self.assertEqual(final['env']['ASTRA_PR_MODE'], '${{ steps.preflight.outputs.mode }}')
        self.assertIn("mode = os.environ['ASTRA_PR_MODE']", final['run'])
        self.assertNotIn('.is_file()', final['run'])
        self.assertIn("GITHUB_EVENT_NAME') != 'pull_request'", final['run'])
        for forbidden in ('continue-on-error', 'docker push', 'workflow_dispatch', 'secrets.', 'packages: write'):
            self.assertNotIn(forbidden, str(workflow))


if __name__ == '__main__':
    unittest.main()
