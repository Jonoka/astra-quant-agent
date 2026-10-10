"""One exact-source, deployment-helper and offline taker gate for PR and release."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

from runtime_release_upgrade import UPSTREAM
from verify_deployment_source import PREVIOUS_SHA, verify
from stage_release_bundle import extract

SUITES = {
    'test_verify_deployment_source.py': 18,
    'test_runtime_release_upgrade.py': 37,
    'test_deployment_plan.py': 29,
    'test_deployment_identity.py': 20,
    'test_recovery_journal.py': 20,
    'test_linux_singleton_lock.py': 7,
    'test_rehearse_cycle_deadline_upgrade.py': 4,
    'test_taker_regression_gate.py': 7,
}


def main():
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Shared deployment preflight requires Linux')
    source = Path(__file__).resolve().parents[2]
    revision = os.environ['ASTRA_CI_EXPECTED_SHA']
    verify(source, UPSTREAM, revision)
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
        for name, minimum in SUITES.items():
            command = [sys.executable, '-c', loader, str(source / '.github/scripts' / name), str(minimum)]
            if name in {'test_deployment_plan.py', 'test_deployment_identity.py',
                        'test_recovery_journal.py'} and os.geteuid() != 0:
                # Real ownership refusal cases require root-owned disposable
                # fixtures. Escalate this synthetic suite only, on hosted CI.
                if env.get('GITHUB_ACTIONS') != 'true':
                    raise RuntimeError('Deployment ownership acceptance needs a root Linux sandbox')
                command = ['sudo', '-n', '--preserve-env=ASTRA_PREVIOUS_SOURCE,ASTRA_DEPLOYED_SOURCE,PYTHONDONTWRITEBYTECODE',
                           *command]
            subprocess.run(command, check=True, env=env, timeout=180, cwd=source)
        subprocess.run([sys.executable, str(source / '.github/scripts/run_taker_regressions.py')],
                       check=True, env=env, timeout=360, cwd=source)
        subprocess.run([sys.executable, str(source / '.github/scripts/run_taker_regressions.py'), '--alpha'],
                       check=True, env=env, timeout=120, cwd=source)
        subprocess.run([sys.executable, str(source / '.github/scripts/run_maintenance_tests.py'), '--require-linux'],
                       check=True, env=env, timeout=180, cwd=source)
        # Partial actual local integration only; not complete framework/field acceptance.
        subprocess.run([sys.executable, str(source / '.github/scripts/run_actual_integration.py')],
                       check=True, env=env, timeout=120, cwd=source)
        subprocess.run([sys.executable, str(source / 'tests/maintenance_integration/test_endpoint_isolation.py')],
                       check=True, env=env, timeout=60, cwd=source)
    verify(source, UPSTREAM, revision)
    print('DEPLOYMENT_PREFLIGHT_PASS source=' + revision, flush=True)


if __name__ == '__main__':
    main()
