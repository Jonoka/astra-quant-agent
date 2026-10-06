"""Capture an explicitly approved live baseline without changing runtime state.

Run only in a new restricted operation directory. The printed SHA must be supplied
externally to staging and every runtime action; never derive it inside a guard.
Only hashes, source/image identities and asset names are retained, never secrets.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat

from runtime_release_upgrade import (
    ROOT, PROJECT, SERVICES, NAMES, CONFIG, UPSTREAM, IMAGE_RE, PATCH_ID,
    OKX_PATCH_ID, DEADLINE_PATCH_ID, digest, sha, plain_path, operation_path,
    require, run, env_gate, validate_deployment_plan, configuration_hash,
)
from stage_release_bundle import source_identity, save


def capture(operation, expected_source, expected_image, approved_names):
    provenance = plain_path(operation / 'provenance.json')
    evidence = json.loads(provenance.read_bytes())
    release = source_identity(evidence)
    image = evidence['IMAGE_NAME'] + '@' + evidence['IMAGE_DIGEST']
    require(evidence['PREVIOUS_SHA'] == expected_source and
            evidence.get('PREVIOUS_IMAGE') == expected_image, 'approved_previous_identity')
    env_gate(ROOT)
    pool_path = plain_path(ROOT / 'data/instrument_pool.json')
    before = pool_path.stat()
    raw = pool_path.read_bytes()
    pool = {'relative_path': 'data/instrument_pool.json', 'names': approved_names,
            'sha256': sha(raw), 'size': before.st_size, 'uid': before.st_uid,
            'gid': before.st_gid, 'mode': stat.S_IMODE(before.st_mode)}
    config = {}
    for name in CONFIG:
        config[name] = configuration_hash(ROOT / name)
    plan = {'schema': 1, 'previous_source': expected_source, 'previous_image': expected_image,
            'release_source': release, 'image': image,
            'helper_sha256': digest(operation / 'runtime_release_upgrade.py'),
            'provenance_sha256': digest(provenance), 'pool': pool,
            'protected_config': config,
            'runtime': {'project': PROJECT, 'services': list(SERVICES), 'demo': True,
                        'schedule_seconds': 900, 'minimum_idle_window_seconds': 480}}
    validate_deployment_plan(plan)
    # Inspect each current container independently. A tag cannot become rollback
    # authority; exact immutable reference and source/version labels must agree.
    for name in NAMES:
        current = json.loads(run('docker', 'inspect', name))[0]
        labels = current['Config']['Labels']
        require(current['Config']['Image'] == expected_image and
                labels.get('com.docker.compose.project') == PROJECT and
                labels.get('com.docker.compose.service') == name.removeprefix('astraquant-') and
                current['State']['Running'] and current['State']['Health']['Status'] == 'healthy' and
                current['RestartCount'] == 0, 'current_runtime_identity')
        metadata = json.loads(run('docker', 'image', 'inspect', expected_image))[0]
        image_labels = metadata['Config']['Labels']
        require(metadata['Id'] == current['Image'] and expected_image in metadata['RepoDigests'] and
                metadata['Os'] == 'linux' and metadata['Architecture'] == 'amd64' and
                image_labels.get('org.opencontainers.image.revision') == expected_source and
                image_labels.get('org.opencontainers.image.version') == 'v8.6.1' and
                image_labels.get('org.opencontainers.image.source') ==
                'https://github.com/Jonoka/astra-quant-agent' and
                image_labels.get('io.jonoka.astra.upstream-revision') == UPSTREAM and
                image_labels.get('io.jonoka.astra.council-completion-patch') == PATCH_ID and
                image_labels.get('io.jonoka.astra.okx-public-domains-patch') == OKX_PATCH_ID and
                image_labels.get('io.jonoka.astra.cycle-deadline-patch') == DEADLINE_PATCH_ID,
                'current_source_image_identity')
    code = r'''import json, hashlib
from pathlib import Path
from scripts import instrument_pool as pool
def refuse_write(*args, **kwargs):
 raise RuntimeError('read-only plan capture refused a pool write')
pool._write_pool_file = refuse_write
pool._write_json_atomic = refuse_write
before = Path('/app/data/instrument_pool.json').read_bytes()
items = pool.load_instruments()
assert pool.pool_is_trustworthy()
assert before == Path('/app/data/instrument_pool.json').read_bytes()
print(json.dumps({'names':[i['name'] for i in items],
 'sha256':hashlib.sha256(before).hexdigest()}))
'''
    actual = json.loads(run('docker', 'exec', NAMES[0], 'python3', '-c', code))
    require(actual == {'names': approved_names, 'sha256': pool['sha256']}, 'approved_native_pool')
    after = pool_path.stat()
    require(raw == pool_path.read_bytes() and
            (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) and
            all(configuration_hash(ROOT / name) == value
                for name, value in config.items()), 'baseline_capture_drift')
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', type=Path)
    parser.add_argument('--expected-previous-source', required=True)
    parser.add_argument('--expected-previous-image', required=True)
    parser.add_argument('--approved-assets', nargs='+', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    operation = operation_path(args.operation)
    plan = capture(operation, args.expected_previous_source,
                   args.expected_previous_image, args.approved_assets)
    path = operation / 'deployment-plan.json'
    save(path, plan)
    print('DEPLOYMENT_PLAN_SHA256=' + digest(path))


if __name__ == '__main__':
    main()
