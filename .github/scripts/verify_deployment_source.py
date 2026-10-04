"""Reject builds that bypass the retained council completion correction."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import subprocess
import sys

APPLICATION_PATCH = {"scripts/brain/dispatch.py", "tests/ops/test_brain_dispatch.py"}


def verify(source: Path, upstream: str, revision: str) -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", upstream), "Unpinned upstream source"
    assert re.fullmatch(r"[0-9a-f]{40}", revision), "Unpinned corrected source"

    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(source), *args], check=True,
                              capture_output=True, timeout=45).stdout

    assert git("rev-parse", "HEAD").decode().strip() == revision, "Wrong fork source commit"
    git("merge-base", "--is-ancestor", upstream, revision)
    changes = {path.decode() for path in git("diff", "--name-only", "-z", upstream, revision).split(b"\0") if path}
    application = {path for path in changes if not path.startswith(".github/")}
    assert application == APPLICATION_PATCH, "Missing retained patch or unreviewed application changes"
    assert not git("status", "--porcelain"), "Source changed after revision verification"
    suite_path = source / "tests/ops/test_brain_dispatch.py"
    tree = ast.parse(suite_path.read_text(encoding="utf-8"))
    suites = [node for node in tree.body if isinstance(node, ast.ClassDef)
              and node.name == "CouncilCompletionTests"]
    assert len(suites) == 1, "Required council completion regression missing"
    cases = {node.name for node in suites[0].body if isinstance(node, ast.FunctionDef)
             and node.name.startswith("test_")}
    required = {
        "test_council_success_returns_the_exact_persisted_fresh_cache",
        "test_council_success_records_ok_health_and_truthful_output_without_fake_usage",
        "test_successful_council_cache_reaches_real_downstream_management_gate",
        "test_council_persistence_failure_never_returns_cache_or_marks_success",
    }
    assert required <= cases and len(cases) >= 9, "Incomplete council completion regression"
    print("PASS: exact corrected fork commit, upstream ancestry, reviewed delta and required regression")


if __name__ == "__main__":
    verify(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3])
