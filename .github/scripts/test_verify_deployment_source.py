"""Focused regression for the reviewed same-release application allowlist."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "verify_deployment_source", Path(__file__).with_name("verify_deployment_source.py"))
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class DeploymentSourceGuardTests(unittest.TestCase):
    def _verify(self, changes=None, head=None, status=b""):
        revision = "b" * 40
        changes = guard.APPLICATION_PATCH if changes is None else changes

        def git(command, **_kwargs):
            verb = command[3]
            values = {
                "rev-parse": ((head or revision) + "\n").encode(),
                "merge-base": b"",
                "diff": b"\0".join(path.encode() for path in sorted(changes)),
                "status": status,
            }
            return SimpleNamespace(stdout=values[verb])

        with patch.object(guard.subprocess, "run", side_effect=git):
            guard.verify(Path(__file__).resolve().parents[2], "a" * 40, revision)

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

    def test_dirty_build_input_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, "Source changed after revision verification"):
            self._verify(status=b" M scripts/okx_public.py\n")


if __name__ == "__main__":
    unittest.main()
