"""Run the affected regression suites in a disposable, offline Linux checkout."""
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.request


SUITES = {
    "tests.trading.test_okx_taker_decimal": 13,
    "tests.trading.test_okx_taker_consistency": 22,
    "tests.ops.test_brain_packages": 47,
    "tests.ops.test_factors_smart_money": 27,
    "tests.ops.test_factor_library": 73,
    "tests.trading.test_okx_quant_factors": 62,
    "tests.extraction.test_brain_package_extraction": 11,
    "tests.extraction.test_factors_smart_money_extraction": 4,
    "tests.ops.test_brain_decisions": 61,
    "tests.llm.test_brain_prompt_module": 33,
    "tests.llm.test_prompt_math_foundations": 17,
    "tests.core.test_factor_assembly_fields": 7,
    "tests.core.test_factor_assembly_semantics": 10,
    "tests.core.test_factor_assembly_local_files": 4,
    "tests.ui.test_dashboard_factors_view_tails": 2,
    "tests.extraction.test_brain_prompt_extraction": 10,
    "tests.extraction.test_brain_decisions_extraction": 8,
}

ALPHA_SUITES = {"tests.llm.test_alpha_transport_contract": 12}


def require_complete_result(result, minimum):
    if (not result.wasSuccessful() or result.testsRun < minimum
            or result.skipped or result.expectedFailures):
        raise RuntimeError("Regression gate failed, incomplete, skipped or expected-failed")


def deny_network(*args, **kwargs):
    raise AssertionError("Offline regression gate prohibits live network")


def copy_source(source, destination):
    def ignore(directory, names):
        excluded = {"__pycache__", "node_modules", ".venv", "venv"}
        if Path(directory) == source:
            excluded.update({".git", "data", "logs", ".aws", ".codex", ".agents"})
        return [name for name in names if name in excluded
                or name.startswith(".env") or name.endswith(".pyc")]

    shutil.copytree(source, destination, ignore=ignore)
    # Seed the repository's tracked prompt template, never runtime databases/logs.
    fixture = source / "data" / "prompt_library.json"
    (destination / "data").mkdir()
    shutil.copyfile(fixture, destination / "data" / fixture.name)


def main():
    if sys.argv[1:] not in ([], ["--alpha"]):
        raise RuntimeError("Expected no arguments or --alpha")
    alpha = sys.argv[1:] == ["--alpha"]
    suites = ALPHA_SUITES if alpha else SUITES
    label = "ALPHA" if alpha else "TAKER"
    if not sys.platform.startswith("linux"):
        raise RuntimeError("The regression gate requires Linux")
    source = Path(__file__).resolve().parents[2]
    expected_sha = os.environ.get("ASTRA_CI_EXPECTED_SHA", "local-uncommitted-validation")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="astra-taker-ci-") as temporary:
        scratch = Path(temporary) / "source"
        copy_source(source, scratch)
        for key in list(os.environ):
            if (key.startswith("ASTRA_") or key in ("GITHUB_TOKEN", "GH_TOKEN")
                    or key.endswith(("API_KEY", "ACCESS_TOKEN", "SECRET_KEY"))):
                os.environ.pop(key, None)
        os.environ["ASTRA_DATA_DIR"] = str(scratch / "data")
        os.environ["ASTRA_LEDGER_SYNC_DISABLED"] = "1"
        sys.path[:0] = [str(scratch), str(scratch / "scripts"),
                        str(scratch / ".github" / "scripts")]
        os.chdir(scratch)
        urllib.request.urlopen = deny_network
        socket.create_connection = deny_network
        socket.socket.connect = deny_network
        socket.socket.connect_ex = deny_network
        import requests
        requests.sessions.Session.request = deny_network
        try:
            suite = unittest.TestSuite()
            for module, minimum in suites.items():
                loaded = unittest.defaultTestLoader.loadTestsFromName(module)
                if loaded.countTestCases() < minimum:
                    raise RuntimeError(f"Incomplete discovery: {module} requires {minimum} tests")
                suite.addTests(loaded)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            print(f"{label}_CI_RESULT sha={expected_sha} modules={len(suites)} "
                  f"tests={result.testsRun} failures={len(result.failures)} "
                  f"errors={len(result.errors)} skips={len(result.skipped)}", flush=True)
            require_complete_result(result, sum(suites.values()))
            if summary:
                with Path(summary).open("a", encoding="utf-8") as output:
                    output.write(f"Offline {label.lower()} regressions: **{result.testsRun} passed**, "
                                 f"{len(suites)} modules, zero skips.\n\nTested PR head: `{expected_sha}`\n")
        finally:
            os.chdir(original_cwd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
