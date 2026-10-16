"""Bounded release preparation for the independently reviewed PR #9 source.

Historical contracts are exercised on their exact historical checkouts. Current
runtime helpers and application regressions run on the release checkout itself.
This preflight neither publishes an image nor grants deployment authorization.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

from run_deployment_preflight import SUITES
from stage_release_bundle import extract
from verify_deployment_source import COLLECTION_BASELINE, PREVIOUS_SHA, REVIEWED_COLLECTION_SHA
from verify_decision_release_source import (
    BASELINE, REVIEWED_COMMIT, UPSTREAM, clean_environment,
    git as source_git, verify_checkout, verify_decision_release_source,
)

# This release's verified deployment predecessor is separate from the frozen
# historical review fixtures. Updating it requires another exact-source review.
DEPLOYMENT_PREDECESSOR = {
    'source': '1bd3c016bf4375114d34022c1528d119d90cd6ad',
    'tree': '47d98d0091a86a513ce1ccc20ced1096fce9d93d',
    'image': 'ghcr.io/jonoka/astra-quant-agent@sha256:7bfc6bb0b75c6a63393f1c717c771cc7097059b2b2c4a6ddca2de86049005bee',
}

# The old source-contract positive fixtures assert exact historical bytes, not
# current application behavior. Their 35 + 33 cases still run on BASELINE below.
# All other original helper cases must additionally pass on the release source.
CURRENT_HELPER_SUITES = {name: minimum for name, minimum in SUITES.items()
                         if name != 'test_verify_deployment_source.py'}
# The historical minimum remains 39 on its original source. This release adds
# 17 real-WAL/rollback and 13 capacity/diagnostic cases, all mandatory on the
# same candidate as the 68 unchanged ownership/recovery cases; discovery must
# not silently lose them.
CURRENT_HELPER_SUITES['test_runtime_release_upgrade.py'] = 69
DECISION_RELEASE_SUITES = {
    'test_decision_release_source.py': 105,
    'test_decision_regression_gate.py': 13,
}
HISTORICAL_CANDIDATE_SUITES = {'test_pr_candidate_contract.py': 46}
OWNERSHIP_SUITES = frozenset({
    'test_deployment_plan.py', 'test_deployment_identity.py', 'test_recovery_journal.py',
})
PRODUCT_GATES = (
    ('run_taker_regressions.py', (), 360),
    ('run_taker_regressions.py', ('--alpha',), 120),
    ('run_collection_regressions.py', (), 480),
    ('run_collection_regressions.py', ('--retained',), 480),
    ('run_decision_contract_regressions.py', (), 480),
)

LOADER = '''import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1]).parent))
from run_taker_regressions import require_complete_result
p=Path(sys.argv[1]); minimum=int(sys.argv[2]); loader=unittest.TestLoader()
suite=loader.discover(str(p.parent),pattern=p.name)
discovered=suite.countTestCases()
if loader.errors or discovered<minimum: raise RuntimeError('Incomplete decision release gate discovery')
result=unittest.TextTestRunner(verbosity=2).run(suite)
require_complete_result(result,max(minimum,discovered))
'''


def preflight_environment():
    """Do not let Git/Python startup overrides redefine a historical fixture."""
    env = clean_environment()
    for name in ('GITHUB_ACTIONS', 'GITHUB_EVENT_NAME', 'GITHUB_STEP_SUMMARY'):
        if name in os.environ:
            env[name] = os.environ[name]
    # Fixed overrides apply to every Git subprocess, including the unchanged
    # historical preflight's worktree/archive calls. Never copy caller-supplied
    # GIT_CONFIG_COUNT/KEY/VALUE, hooks, fsmonitor or replace-object settings.
    env.update(GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='core.hooksPath',
               GIT_CONFIG_VALUE_0=os.devnull, GIT_CONFIG_KEY_1='core.fsmonitor',
               GIT_CONFIG_VALUE_1='false')
    return env


def verify_fixture(destination, revision):
    if source_git(destination, 'rev-parse', 'HEAD').decode().strip() != revision:
        raise RuntimeError('Historical preflight fixture HEAD differs from approved revision')
    # Git status alone cannot detect smudge/clean transformations or hidden
    # index flags. Verify raw bytes before executing a historical script.
    verify_checkout(destination, revision)


def verify_deployed_predecessor(source, deployed, revision, env):
    """Bind real workflow inputs and checkout to this release's exact prior state."""
    pin = DEPLOYMENT_PREDECESSOR
    if (os.environ.get('PREVIOUS_SHA') != pin['source']
            or os.environ.get('PREVIOUS_IMAGE') != pin['image']):
        raise RuntimeError('Decision release workflow inputs differ from pinned deployment predecessor')
    if revision == pin['source']:
        raise RuntimeError('Decision release candidate equals deployment predecessor')
    deployed = Path(deployed).resolve()

    def require_identity():
        for expression, expected in (('--show-toplevel', str(deployed)),
                                     ('HEAD', pin['source']), ('HEAD^{tree}', pin['tree'])):
            actual = subprocess.run(['git', '-C', str(deployed), 'rev-parse', expression],
                                    check=True, capture_output=True, env=env,
                                    timeout=60).stdout.decode().strip()
            if actual != expected:
                raise RuntimeError('Decision release deployed source differs from pinned deployment predecessor')

    require_identity()
    subprocess.run(['git', '-C', str(source), 'merge-base', '--is-ancestor', pin['source'], revision],
                   check=True, capture_output=True, env=env, timeout=60)
    verify_checkout(deployed, pin['source'])
    require_identity()
    return deployed


@contextmanager
def historical_worktree(source, destination, revision, env):
    subprocess.run(['git', '-C', str(source), 'worktree', 'add', '--detach', str(destination), revision],
                   check=True, env=env, timeout=60)
    try:
        verify_fixture(destination, revision)
        yield destination
        verify_fixture(destination, revision)
    finally:
        subprocess.run(['git', '-C', str(source), 'worktree', 'remove', '--force', str(destination)],
                       check=True, env=env, timeout=60)


def run_suites(source, specification, env):
    for name, minimum in specification.items():
        command = [sys.executable, '-c', LOADER, str(source / '.github/scripts' / name), str(minimum)]
        if name in OWNERSHIP_SUITES and os.geteuid() != 0:
            # Preserve the existing root-owned disposable-fixture requirement.
            # Never fake root or skip these tests on an unprivileged workstation.
            if env.get('GITHUB_ACTIONS') != 'true':
                raise RuntimeError('Deployment ownership acceptance needs a root Linux sandbox')
            command = ['sudo', '-n', '--preserve-env=ASTRA_PREVIOUS_SOURCE,ASTRA_DEPLOYED_SOURCE,ASTRA_COLLECTION_BASELINE,ASTRA_COLLECTION_REVIEWED_SOURCE,PYTHONDONTWRITEBYTECODE',
                       *command]
        subprocess.run(command, check=True, env=env, timeout=240, cwd=source)


def run_preflight(source, revision, pr_only=False):
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Decision release preflight requires Linux')
    if pr_only and os.environ.get('GITHUB_EVENT_NAME') != 'pull_request':
        raise RuntimeError('Release PR preflight requires a pull_request event')
    supplied = [bool(os.environ.get(name)) for name in
                ('ASTRA_DEPLOYED_SOURCE', 'PREVIOUS_SHA', 'PREVIOUS_IMAGE')]
    if (not pr_only or any(supplied)) and not all(supplied):
        raise RuntimeError('Decision release requires complete pinned deployment predecessor inputs')
    source = source.resolve()
    verify_decision_release_source(source, UPSTREAM, revision)
    env = preflight_environment()
    env['ASTRA_CI_EXPECTED_SHA'] = revision
    deployed = os.environ.get('ASTRA_DEPLOYED_SOURCE')
    if deployed:
        deployed = verify_deployed_predecessor(source, deployed, revision, env)
        env['ASTRA_DEPLOYED_SOURCE'] = str(deployed)

    with tempfile.TemporaryDirectory(prefix='astra-decision-release-') as temporary:
        root = Path(temporary)
        # Original release source/helper proofs stay bound to the exact deployed
        # release; changed candidate files never become their expected fixtures.
        with historical_worktree(source, root / 'baseline', BASELINE, env) as baseline:
            historical = dict(env, ASTRA_CI_EXPECTED_SHA=BASELINE)
            subprocess.run([sys.executable, str(baseline / '.github/scripts/run_deployment_preflight.py'), '--release-pr'],
                           check=True, env=historical, timeout=1200, cwd=baseline)

        # Preserve ordinary-PR separation regressions on the exact reviewed PR
        # tree. In particular, these tests still prove its old release rejection.
        with historical_worktree(source, root / 'reviewed', REVIEWED_COMMIT, env) as reviewed:
            historical = dict(env, ASTRA_CI_EXPECTED_SHA=REVIEWED_COMMIT)
            subprocess.run([sys.executable, str(reviewed / '.github/scripts/verify_pr_candidate.py'),
                            str(reviewed), REVIEWED_COMMIT, '--contract',
                            str(reviewed / '.github/scripts/pr_candidate_contract.json')],
                           check=True, env=historical, timeout=180, cwd=reviewed)
            run_suites(reviewed, HISTORICAL_CANDIDATE_SUITES, historical)

        # Current helper tests receive the unchanged historical compatibility
        # archives, while their implementation is imported from the release HEAD.
        for label, pin, variable in (
            ('previous', PREVIOUS_SHA, 'ASTRA_PREVIOUS_SOURCE'),
            ('collection-baseline', COLLECTION_BASELINE, 'ASTRA_COLLECTION_BASELINE'),
            ('collection-reviewed', REVIEWED_COLLECTION_SHA, 'ASTRA_COLLECTION_REVIEWED_SOURCE'),
        ):
            archive = root / (label + '.tar')
            with archive.open('wb') as output:
                subprocess.run(['git', '-C', str(source), 'archive', '--format=tar', pin],
                               check=True, stdout=output, env=env, timeout=60)
            fixture = root / label
            extract(archive, fixture, pin)
            env[variable] = str(fixture)
        run_suites(source, CURRENT_HELPER_SUITES, env)
        run_suites(source, DECISION_RELEASE_SUITES, env)
        for name, arguments, timeout in PRODUCT_GATES:
            subprocess.run([sys.executable, str(source / '.github/scripts' / name), *arguments],
                           check=True, env=env, timeout=timeout, cwd=source)

    if deployed:
        verify_deployed_predecessor(source, deployed, revision, env)
    verify_decision_release_source(source, UPSTREAM, revision)
    label = 'DECISION_RELEASE_PR_PREFLIGHT_PASS' if pr_only else 'DECISION_DEPLOYMENT_PREFLIGHT_PASS'
    print(label + ' source=' + revision + ' publication_authorization=not_granted', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-pr', action='store_true', help='Release preparation PR checks only; no image or deployment')
    args = parser.parse_args()
    run_preflight(Path(__file__).resolve().parents[2], os.environ['ASTRA_CI_EXPECTED_SHA'], args.release_pr)


if __name__ == '__main__':
    main()
