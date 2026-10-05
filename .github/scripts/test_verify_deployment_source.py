"""Focused regression for the reviewed same-release application allowlist."""
from __future__ import annotations

import importlib.util
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "verify_deployment_source", Path(__file__).with_name("verify_deployment_source.py"))
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class DeploymentSourceGuardTests(unittest.TestCase):
    def _verify(self, changes=None, head=None, status=b"", source=None, missing_previous=False):
        revision = "b" * 40
        changes = guard.APPLICATION_PATCH if changes is None else changes

        def git(command, **_kwargs):
            verb = command[3]
            if missing_previous and verb == 'merge-base' and guard.PREVIOUS_SHA in command:
                raise subprocess.CalledProcessError(1, command)
            values = {
                "rev-parse": ((head or revision) + "\n").encode(),
                "merge-base": b"",
                "diff": b"\0".join(path.encode() for path in sorted(changes)),
                "status": status,
            }
            return SimpleNamespace(stdout=values[verb])

        with patch.object(guard.subprocess, "run", side_effect=git):
            guard.verify(source or Path(__file__).resolve().parents[2], "a" * 40, revision)

    def test_reviewed_patch_with_workflow_delta_passes(self):
        self._verify(guard.APPLICATION_PATCH | {".github/workflows/build-release-image.yml"})

    def test_missing_patch_and_unrelated_application_delta_are_rejected(self):
        for changes in (guard.APPLICATION_PATCH - {"scripts/okx_public.py"},
                        guard.APPLICATION_PATCH | {"scripts/okx_rest.py"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(
                    AssertionError, "Missing retained patch or unreviewed application changes"):
                self._verify(changes)

    def test_wrong_commit_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, "Wrong fork source commit"):
            self._verify(head="c" * 40)

    def test_missing_current_baseline_ancestry_is_rejected(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self._verify(missing_previous=True)

    def test_dirty_build_input_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, "Source changed after revision verification"):
            self._verify(status=b" M scripts/okx_public.py\n")

    def test_all_62_reviewed_deadline_cases_are_required(self):
        self.assertEqual(sum(len(cases) for cases in guard.DEADLINE_REGRESSIONS.values()), 62)
        self.assertEqual(len(guard.DEADLINE_REGRESSIONS), 4)
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            for relative in (*guard.DEADLINE_REGRESSIONS, 'tests/ops/test_brain_dispatch.py'):
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / relative, target)
            self._verify(source=source)
            for relative, cases in guard.DEADLINE_REGRESSIONS.items():
                target = source / relative
                original = target.read_text(encoding='utf-8')
                case = sorted(cases)[0]
                target.write_text(original.replace('def ' + case + '(', 'def removed_regression(', 1), encoding='utf-8')
                with self.subTest(relative=relative), self.assertRaisesRegex(AssertionError, 'Incomplete deadline regression'):
                    self._verify(source=source)
                target.write_text(original, encoding='utf-8')
            (source / 'tests/llm/test_llm_deadline.py').unlink()
            with self.assertRaisesRegex(AssertionError, 'Required deadline regression missing'):
                self._verify(source=source)

    def test_deadline_and_review_documents_cannot_disappear_or_accept_other_paths(self):
        for path in ('astra_backend/deadline.py', '.trellis/spec/backend/astra-release-contract.md',
                     '.trellis/tasks/10-05-astra-release-preparation/validation.md'):
            self.assertIn(path, guard.APPLICATION_PATCH)
            with self.subTest(path=path), self.assertRaisesRegex(AssertionError, 'Missing retained patch'):
                self._verify(guard.APPLICATION_PATCH - {path})
        with self.assertRaisesRegex(AssertionError, 'unreviewed application changes'):
            self._verify(guard.APPLICATION_PATCH | {'.trellis/tasks/unreviewed/task.json'})

    def test_staging_requires_all_nine_independent_checks(self):
        import stage_release_bundle as staging
        checks = {key: 'passed' for key in staging.REQUIRED_RELEASE_CHECKS}
        with patch.object(staging, 'CHECKS', tuple(staging.REQUIRED_RELEASE_CHECKS)):
            staging.require_checks(checks)
            for key in staging.REQUIRED_RELEASE_CHECKS:
                for change in ({k: v for k, v in checks.items() if k != key},
                               {**checks, key: 'pending'}, {**checks, 'unknown_check': 'passed'}):
                    with self.subTest(key=key), self.assertRaisesRegex(Exception, 'checks_unverified'):
                        staging.require_checks(change)
        with patch.object(staging, 'CHECKS', ('state_preservation',)):
            with self.assertRaisesRegex(Exception, 'checks_unverified'):
                staging.require_checks(checks)

    def test_workflow_preserves_pins_labels_positive_discovery_and_immutable_smoke(self):
        import yaml
        root = Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load((root / '.github/workflows/build-release-image.yml').read_text(encoding='utf-8'))
        env = workflow['env']
        self.assertEqual(env['PREVIOUS_SHA'], '90f9f3a558bdbea0171b19a42c58e2fae7ed8e9d')
        self.assertEqual(env['UPSTREAM_SHA'], 'e0b29fef1818e0ff9c6b210eb73234620e276a02')
        self.assertEqual(env['SOURCE_SHA'], '${{ github.sha }}')
        self.assertEqual(env['SOURCE_VERSION'], 'v8.6.1')
        steps = {step['name']: step for step in workflow['jobs']['build']['steps']}
        regressions = steps['Require all cycle deadline regressions']['run']
        for name in guard.DEADLINE_REGRESSIONS:
            self.assertIn(name, regressions)
        self.assertIn('suite.countTestCases() >= minimum', regressions)
        self.assertIn('result.testsRun >= minimum', regressions)
        self.assertIn('assert not result.skipped', regressions)
        self.assertIn('result.testsRun > len(result.skipped)', regressions)
        for label in ('council-completion-v1', 'okx-public-domains-v1', 'cycle-deadline-v1'):
            self.assertIn(label, steps['Build candidate from corrected fork and reviewed release recipe']['with']['labels'])
        self.assertIn('test_linux_singleton_lock.py', steps['Verify Linux real singleton locks']['run'])
        self.assertIn('suite.countTestCases() >= 5', steps['Verify Linux real singleton locks']['run'])
        self.assertIn('result.testsRun >= 5 and not result.skipped', steps['Verify Linux real singleton locks']['run'])
        lock = steps['Verify candidate image real singleton locks']
        self.assertIn("docker image inspect --format '{{.Id}}' astraquant:ci", lock['run'])
        self.assertIn('docker run --rm --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=16m', lock['run'])
        self.assertIn('ASTRA_LOCK_TEST_SOURCE_ROOT=/app --entrypoint python "$candidate_id"', lock['run'])
        self.assertEqual(steps['Smoke test official Compose services and auth']['env']['EXPECTED_IMAGE_ID'],
                         '${{ steps.candidate_lock.outputs.image_id }}')
        self.assertEqual(steps['Rehearse cycle deadline upgrade and latest-state rollback']['run'],
                         'python ci/.github/scripts/rehearse_cycle_deadline_upgrade.py --source validation --previous previous')
        rehearsal = steps['Require real cycle deadline state rehearsal tests']
        self.assertEqual(rehearsal['env']['ASTRA_PREVIOUS_SOURCE'], '${{ github.workspace }}/previous')
        self.assertIn('test_rehearse_cycle_deadline_upgrade.py', rehearsal['run'])
        self.assertIn('suite.countTestCases() >= 4', rehearsal['run'])
        self.assertIn('result.testsRun >= 4 and not result.skipped', rehearsal['run'])
        self.assertLess(list(steps).index('Verify prior source pin'),
                        list(steps).index('Require real cycle deadline state rehearsal tests'))
        published = steps['Verify published immutable image']
        self.assertEqual(published['env']['EXPECTED_IMAGE_ID'], '${{ steps.candidate.outputs.image_id }}')
        self.assertIn('smoke_release_compose.py source "$IMAGE_NAME@$IMAGE_DIGEST" previous', published['run'])
        self.assertIn('$SOURCE_VERSION-cycle-deadline-fix-${SOURCE_SHA:0:12}',
                      steps['Publish the smoke-tested linux/amd64 image']['run'])
        self.assertEqual(steps['Upload verified raw deployment bundle']['with']['name'],
                         'astraquant-v8.6.1-cycle-deadline-deployment')

    def test_smoke_rejects_missing_patch_labels_and_different_published_image(self):
        import smoke_release_compose as smoke
        env = dict(SOURCE_SHA='a' * 40, SOURCE_VERSION='v8.6.1', SOURCE_REPOSITORY='Jonoka/astra-quant-agent',
                   UPSTREAM_SHA='b' * 40, BUILD_RECIPE_SHA256='c' * 64, EXPECTED_IMAGE_ID='candidate-id')
        labels = {
            'org.opencontainers.image.revision': env['SOURCE_SHA'],
            'org.opencontainers.image.version': env['SOURCE_VERSION'],
            'org.opencontainers.image.source': 'https://github.com/' + env['SOURCE_REPOSITORY'],
            'io.jonoka.astra.upstream-revision': env['UPSTREAM_SHA'],
            'io.jonoka.astra.council-completion-patch': 'council-completion-v1',
            'io.jonoka.astra.okx-public-domains-patch': 'okx-public-domains-v1',
            'io.jonoka.astra.cycle-deadline-patch': 'cycle-deadline-v1',
            'io.jonoka.astra.build-recipe-sha256': env['BUILD_RECIPE_SHA256'],
        }
        metadata = dict(Id='candidate-id', Os='linux', Architecture='amd64', Config={'Labels': labels})
        with patch.dict(os.environ, env):
            smoke.verify_image_metadata(metadata)
            for key in labels:
                bad = copy.deepcopy(metadata)
                bad['Config']['Labels'][key] = 'incorrect'
                with self.subTest(key=key), self.assertRaises(AssertionError):
                    smoke.verify_image_metadata(bad)
                del bad['Config']['Labels'][key]
                with self.subTest(missing=key), self.assertRaises(KeyError):
                    smoke.verify_image_metadata(bad)
            with self.assertRaisesRegex(AssertionError, 'Published image differs'):
                smoke.verify_image_metadata({**metadata, 'Id': 'different-published-id'})


if __name__ == "__main__":
    unittest.main()
