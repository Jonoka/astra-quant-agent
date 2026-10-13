"""Shared safety regressions; --collection-pr is explicitly not release approval."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

from runtime_release_upgrade import UPSTREAM
from verify_deployment_source import (COLLECTION_BASELINE, PREVIOUS_SHA, REVIEWED_COLLECTION_SHA,
                                      verify_release_source, verify_collection_source)
from stage_release_bundle import extract

SUITES = {
    'test_verify_deployment_source.py': 35,
    'test_runtime_release_upgrade.py': 39,
    'test_deployment_plan.py': 29,
    'test_deployment_identity.py': 19,
    'test_recovery_journal.py': 20,
    'test_linux_singleton_lock.py': 7,
    'test_rehearse_cycle_deadline_upgrade.py': 4,
    'test_taker_regression_gate.py': 7,
    'test_collection_regression_gate.py': 10,
    'test_collection_status_compatibility.py': 17,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--collection-pr', action='store_true')
    mode.add_argument('--release-pr', action='store_true', help='Exact release-source checks without publication or deployment approval')
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Shared deployment preflight requires Linux')
    source = Path(__file__).resolve().parents[2]
    revision = os.environ['ASTRA_CI_EXPECTED_SHA']
    def source_verifier(source, upstream, revision):
        if args.collection_pr:
            verify_collection_source(source, upstream, revision)
        else:
            verify_release_source(source, upstream, revision)
    source_verifier(source, UPSTREAM, revision)
    suites = dict(SUITES)
    if not args.collection_pr:
        suites['test_release_source_contract.py'] = 33
    env = dict(os.environ)
    for key in list(env):
        if key in ('GITHUB_TOKEN', 'GH_TOKEN') or key.endswith(('API_KEY', 'ACCESS_TOKEN', 'SECRET_KEY')):
            env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix='astra-preflight-') as temporary:
        archive = Path(temporary) / 'legacy.tar'
        with archive.open('wb') as output:
            subprocess.run(['git', '-C', str(source), 'archive', '--format=tar', PREVIOUS_SHA],
                           stdout=output, check=True, timeout=60, env=env)
        previous = Path(temporary) / 'previous'
        extract(archive, previous, PREVIOUS_SHA)
        env['ASTRA_PREVIOUS_SOURCE'] = str(previous)
        # Both modes require the original collection baseline fixtures.
        base_archive = Path(temporary) / 'collection-baseline.tar'
        with base_archive.open('wb') as output:
            subprocess.run(['git', '-C', str(source), 'archive', '--format=tar', COLLECTION_BASELINE],
                           stdout=output, check=True, timeout=60, env=env)
        baseline = Path(temporary) / 'collection-baseline'
        extract(base_archive, baseline, COLLECTION_BASELINE)
        env['ASTRA_COLLECTION_BASELINE'] = str(baseline)
        if not args.collection_pr:
            reviewed_archive = Path(temporary) / 'reviewed-collection.tar'
            with reviewed_archive.open('wb') as output:
                subprocess.run(['git', '-C', str(source), 'archive', '--format=tar', REVIEWED_COLLECTION_SHA],
                               stdout=output, check=True, timeout=60, env=env)
            reviewed = Path(temporary) / 'reviewed-collection'
            extract(reviewed_archive, reviewed, REVIEWED_COLLECTION_SHA)
            env['ASTRA_COLLECTION_REVIEWED_SOURCE'] = str(reviewed)
        loader = '''import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1]).parent))
from run_taker_regressions import require_complete_result
p=Path(sys.argv[1]); minimum=int(sys.argv[2])
suite=unittest.defaultTestLoader.discover(str(p.parent),pattern=p.name)
assert suite.countTestCases()>=minimum, 'Incomplete deployment discovery'
result=unittest.TextTestRunner(verbosity=2).run(suite)
require_complete_result(result,minimum)
'''
        for name, minimum in suites.items():
            command = [sys.executable, '-c', loader, str(source / '.github/scripts' / name), str(minimum)]
            if name in {'test_deployment_plan.py', 'test_deployment_identity.py',
                        'test_recovery_journal.py'} and os.geteuid() != 0:
                # Real ownership refusal cases require root-owned disposable
                # fixtures. Escalate this synthetic suite only, on hosted CI.
                if env.get('GITHUB_ACTIONS') != 'true':
                    raise RuntimeError('Deployment ownership acceptance needs a root Linux sandbox')
                command = ['sudo', '-n', '--preserve-env=ASTRA_PREVIOUS_SOURCE,ASTRA_DEPLOYED_SOURCE,ASTRA_COLLECTION_BASELINE,ASTRA_COLLECTION_REVIEWED_SOURCE,PYTHONDONTWRITEBYTECODE',
                           *command]
            subprocess.run(command, check=True, env=env, timeout=180, cwd=source)
        subprocess.run([sys.executable, str(source / '.github/scripts/run_taker_regressions.py')],
                       check=True, env=env, timeout=360, cwd=source)
        subprocess.run([sys.executable, str(source / '.github/scripts/run_taker_regressions.py'), '--alpha'],
                       check=True, env=env, timeout=120, cwd=source)
        subprocess.run([sys.executable, str(source / '.github/scripts/run_collection_regressions.py')],
                       check=True, env=env, timeout=480, cwd=source)
        subprocess.run([sys.executable, str(source / '.github/scripts/run_collection_regressions.py'), '--retained'],
                       check=True, env=env, timeout=480, cwd=source)
    source_verifier(source, UPSTREAM, revision)
    label = ('COLLECTION_PR_PREFLIGHT_PASS' if args.collection_pr else
             'RELEASE_PR_PREFLIGHT_PASS' if args.release_pr else 'DEPLOYMENT_PREFLIGHT_PASS')
    print(label + ' source=' + revision + (' release_eligibility=not_granted' if args.collection_pr or args.release_pr else ''), flush=True)


if __name__ == '__main__':
    main()
