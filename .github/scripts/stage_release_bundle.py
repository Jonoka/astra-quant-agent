"""Stage raw hosted archives only after every required hosted check passes."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile

from runtime_release_upgrade import (UPSTREAM, CHECKS, PATCH_ID, IMAGE_RE,
                                    digest, plain_path as plain, operation_path, require,
                                    read_deployment_plan)

LINK = 'frontend/public/images'
REQUIRED_RELEASE_CHECKS = frozenset({
    'state_preservation', 'backward_read_write', 'helper_tests',
    'published_compose_smoke', 'council_regression', 'patch_retention',
    'cycle_deadline_regression', 'linux_singleton_lock',
    'cycle_deadline_state_rehearsal',
    'shared_deployment_preflight', 'deployment_plan_tests', 'current_baseline_rehearsal',
    'demo_maintenance_regression',
})


def require_checks(checks):
    require(set(CHECKS) == REQUIRED_RELEASE_CHECKS and
            set(checks) == REQUIRED_RELEASE_CHECKS and
            all(checks[key] == 'passed' for key in REQUIRED_RELEASE_CHECKS),
            'checks_unverified')


def save(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write('\n')


def extract(archive, destination, pin):
    plain(destination)
    require(not destination.exists(), 'destination_exists')
    staging = plain(destination.with_name(destination.name + '.extracting'))
    require(not staging.exists(), 'staging_exists')
    with tarfile.open(archive, 'r:') as bundle:
        require(bundle.pax_headers.get('comment') == pin, 'git_archive_source_pin')
        members = bundle.getmembers()
        seen = set()
        for member in members:
            name = PurePosixPath(member.name)
            require(member.name and not name.is_absolute() and '..' not in name.parts and
                    '\\' not in member.name and name.as_posix() == member.name.rstrip('/'), 'archive_path')
            canonical = name.as_posix()
            require(canonical not in seen, 'archive_duplicate')
            seen.add(canonical)
            require(member.isfile() or member.isdir() or member.issym(), 'archive_member_type')
            require(member.mode & 0o7000 == 0, 'archive_special_permissions')
            require(not canonical.startswith(LINK + '/'), 'archive_symlink_child')
            if member.issym():
                require(canonical == LINK and member.linkname == '../../docs/images', 'archive_symlink')
            else:
                require(canonical != LINK, 'archive_link_missing')
        require(LINK in seen, 'archive_link_missing')
        staging.mkdir(mode=0o700)
        for member in members:
            target = staging / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, target.open('xb') as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            if not member.issym():
                target.chmod(member.mode)
        require((staging / LINK).resolve() == staging / 'docs/images', 'extracted_link_escape')
    staging.rename(destination)
    return {p.relative_to(destination).as_posix(): digest(p)
            for p in sorted(destination.rglob('*')) if p.is_file() and not p.is_symlink()}


def source_identity(evidence):
    release = evidence['SOURCE_SHA']
    previous = evidence['PREVIOUS_SHA']
    require(re.fullmatch(r'[0-9a-f]{40}', release) and
            re.fullmatch(r'[0-9a-f]{40}', previous) and release not in (previous, UPSTREAM) and
            evidence.get('GITHUB_SHA') == release and
            evidence['UPSTREAM_SHA'] == UPSTREAM and
            evidence['SOURCE_REPOSITORY'] == 'Jonoka/astra-quant-agent' and
            evidence['UPSTREAM_REPOSITORY'] == '0xethanq/astra-quant-agent' and
            evidence['SOURCE_VERSION'] == 'v8.6.1' and evidence['platform'] == 'linux/amd64',
            'source_identity')
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', type=Path)
    parser.add_argument('provenance', type=Path)
    parser.add_argument('--provenance-sha256', required=True)
    parser.add_argument('--run-url', required=True)
    parser.add_argument('--checks-json', type=Path, required=True)
    parser.add_argument('--deployment-plan-sha256', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    op = operation_path(args.operation)
    provenance = plain(args.provenance)
    require(provenance.parent == op and digest(provenance) == args.provenance_sha256, 'provenance_hash')
    evidence = json.loads(provenance.read_bytes())
    release = source_identity(evidence)
    plan = read_deployment_plan(op, args.deployment_plan_sha256)
    image = evidence['IMAGE_NAME'] + '@' + evidence['IMAGE_DIGEST']
    require(re.fullmatch(IMAGE_RE, image), 'image_identity')
    previous = evidence['PREVIOUS_SHA']
    require(plan['previous_source'] == previous and
            plan['previous_image'] == evidence.get('PREVIOUS_IMAGE') and
            plan['release_source'] == release and plan['image'] == image and
            plan['provenance_sha256'] == args.provenance_sha256 and
            plan['helper_sha256'] == digest(op / 'runtime_release_upgrade.py'),
            'deployment_plan_provenance')
    require(args.run_url == 'https://github.com/Jonoka/astra-quant-agent/actions/runs/' +
            str(evidence['GITHUB_RUN_ID']), 'hosted_run_identity')
    checks = json.loads(plain(args.checks_json).read_bytes())
    require_checks(checks)
    required = {'source-previous.tar', 'source-release.tar', 'runtime_release_upgrade.py',
                'stage_release_bundle.py', 'prepare_deployment_plan.py', 'Dockerfile.release',
                'maintenance_deployment.py', 'maintenance_protocol.py'}
    require(required <= set(evidence['sha256']), 'artifact_manifest_incomplete')
    for name, expected in evidence['sha256'].items():
        require(Path(name).name == name and re.fullmatch(r'[0-9a-f]{64}', expected), 'artifact_name_hash')
        path = plain(op / name)
        require(path.is_file() and digest(path) == expected, 'artifact_hash')
        require(path.stat().st_uid == 0 and not path.stat().st_mode & 0o077, 'artifact_permissions')
    sources = {label: extract(op / ('source-' + label + '.tar'), op / ('source-' + label), pin)
               for label, pin in (('previous', previous), ('release', release))}
    for helper in ('runtime_release_upgrade.py', 'stage_release_bundle.py', 'prepare_deployment_plan.py',
                   'maintenance_deployment.py'):
        require(digest(op / helper) == digest(op / 'source-release/.github/scripts' / helper),
                'helper_source_drift')
    require(digest(op / 'maintenance_protocol.py') ==
            digest(op / 'source-release/astra_backend/maintenance.py'), 'maintenance_protocol_source_drift')
    if plan['schema'] == 2:
        scope = plan['maintenance']
        require((op / 'source-previous/astra_backend/maintenance.py').is_file() and
                digest(op / 'source-previous/astra_backend/maintenance.py') == scope['previous_protocol_sha256'] and
                digest(op / 'maintenance_protocol.py') == scope['target_protocol_sha256'],
                'maintenance_legacy_or_protocol_artifact_refused')
    official = (op / 'source-release/Dockerfile').read_bytes()
    anchor = b'RUN rm -rf public/images && mkdir -p public/images\n'
    require(official.count(anchor) == 1, 'recipe_anchor_drift')
    recipe = official.replace(anchor, anchor + b'COPY docs/images/dashboard_preview.png ./public/images/dashboard_preview.png\n', 1)
    require((op / 'Dockerfile.release').read_bytes() == recipe and
            digest(op / 'Dockerfile.release') == evidence['BUILD_RECIPE_SHA256'], 'reviewed_recipe_drift')
    compatibility = {'previous_source': previous, 'previous_image': plan['previous_image'],
                     'release_source': release, 'upstream_source': UPSTREAM,
                     'image': image, 'hosted_run': args.run_url, 'rollback_preserves_new_records': True,
                     'checks': checks, 'reviewed_formats': ['sqlite', 'encrypted_credentials', 'settings',
                                                          'prompts', 'trading_records']}
    save(op / 'manifest.json', {'schema': 2, 'previous_source': previous,
         'previous_image': plan['previous_image'], 'deployment_plan_sha256': args.deployment_plan_sha256,
         'release_source': release,
         'upstream_source': UPSTREAM, 'image': image, 'source_files': sources, 'compatibility': compatibility})
    print('PASS: source bundle and independently attested hosted checks staged')


if __name__ == '__main__':
    main()
