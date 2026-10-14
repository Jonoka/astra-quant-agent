"""Exact decision-release source guard; publication still needs separate approval.

The two new trust roots cannot certify their own approval. Independently review
and freeze their final commit, then verify remote readback. This guard never
creates approval hashes, and an ordinary PR commit is not a release candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

UPSTREAM = UPSTREAM_SHA = 'e0b29fef1818e0ff9c6b210eb73234620e276a02'
BASELINE = '60133ce82d4a0b91f94bc0c613a07e24aff049f5'
BASELINE_TREE = '64059d6490d5734a3e764c5b742cf4fad2f8016a'
REVIEWED_COMMIT = '71ff89f554b797558c721cc3f93051624f911e18'
REVIEWED_TREE = '410a2e8c9c253304add6d540f33c8c2287032995'
CONTRACT_PATH = '.github/scripts/decision_release_contract.json'
VERIFIER_PATH = '.github/scripts/verify_decision_release_source.py'
CANDIDATE_CONTRACT_PATH = '.github/scripts/pr_candidate_contract.json'
HISTORICAL_VERIFIER_PATH = '.github/scripts/verify_deployment_source.py'
SELF_HASH_EXEMPTIONS = frozenset({CONTRACT_PATH, VERIFIER_PATH})
RELEASE_INCREMENT_PATHS = frozenset({
    CONTRACT_PATH, VERIFIER_PATH,
    '.github/scripts/test_decision_release_source.py',
    '.github/scripts/run_decision_release_preflight.py',
    '.github/workflows/test-taker-parsing.yml',
    '.github/workflows/build-release-image.yml',
})
# Fixed reviewed scope, not an allowlist derived from the current PR manifest.
REVIEWED_PATHS = frozenset({
    '.github/scripts/pr_candidate_contract.json',
    '.github/scripts/run_decision_contract_regressions.py',
    '.github/scripts/run_deployment_preflight.py',
    '.github/scripts/test_decision_regression_gate.py',
    '.github/scripts/test_pr_candidate_contract.py',
    '.github/scripts/verify_pr_candidate.py',
    '.github/workflows/test-taker-parsing.yml',
    'astra_backend/council/consensus.py',
    'astra_backend/council/debate.py',
    'docs/DECISION_CONTRACT_REPAIR.md',
    'scripts/brain/dispatch.py',
    'scripts/brain/packages.py',
    'scripts/brain/prompt.py',
    'scripts/factors/candles_15m.py',
    'scripts/replay_proposal_contract.py',
    'scripts/trader/factors.py',
    'scripts/trader/signal_snapshot.py',
    'tests/core/test_council_deadline.py',
    'tests/core/test_council_json_contract.py',
    'tests/core/test_debate_single_trader_paths.py',
    'tests/core/test_proposal_replay.py',
    'tests/core/test_proposal_semantic_contract.py',
    'tests/data/proposal_contract_synthetic.json',
    'tests/extraction/test_brain_package_extraction.py',
    'tests/extraction/test_factors_candles_15m_extraction.py',
    'tests/llm/test_closed_candle_contract.py',
})
_EXECUTING_VERIFIER_BYTES = Path(__file__).read_bytes()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def is_hash(value, length=64):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{' + str(length) + '}', value) is not None


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate decision release contract field: ' + key)
        result[key] = value
    return result


def decision_release_contract(path: Path):
    contract = json.loads(path.read_bytes(), object_pairs_hook=unique_fields)
    require(isinstance(contract, dict) and set(contract) == {
        'schema', 'upstream', 'baseline', 'baseline_tree', 'reviewed_commit',
        'reviewed_tree', 'reviewed_paths', 'release_paths', 'self_hash_exemptions'},
        'Invalid decision release contract fields')
    require(type(contract['schema']) is int and contract['schema'] == 1
            and contract['upstream'] == UPSTREAM, 'Wrong decision release schema or upstream')
    require(contract['baseline'] == BASELINE and contract['baseline_tree'] == BASELINE_TREE,
            'Wrong decision release baseline or tree')
    require(contract['reviewed_commit'] == REVIEWED_COMMIT and contract['reviewed_tree'] == REVIEWED_TREE,
            'Wrong reviewed decision anchor or tree')
    exemptions = contract['self_hash_exemptions']
    require(isinstance(exemptions, list) and len(exemptions) == 2
            and all(isinstance(value, str) for value in exemptions)
            and set(exemptions) == SELF_HASH_EXEMPTIONS, 'Unreviewed decision release trust root')
    for field, expected, before in (
        ('reviewed_paths', REVIEWED_PATHS, 'base_sha256'),
        ('release_paths', RELEASE_INCREMENT_PATHS - SELF_HASH_EXEMPTIONS, 'reviewed_sha256'),
    ):
        paths = contract[field]
        require(isinstance(paths, dict) and set(paths) == expected,
                'Missing or unreviewed decision ' + field)
        for relative, pin in paths.items():
            require(isinstance(pin, dict) and set(pin) == {before, 'sha256', 'mode'},
                    'Invalid decision release pin fields: ' + relative)
            require(pin['mode'] == '100644' and is_hash(pin['sha256']),
                    'Invalid decision release hash or mode: ' + relative)
            require(pin[before] is None or is_hash(pin[before]),
                    'Invalid decision release predecessor hash: ' + relative)
    for relative, pin in contract['release_paths'].items():
        if relative in REVIEWED_PATHS:
            require(pin['reviewed_sha256'] == contract['reviewed_paths'][relative]['sha256'],
                    'Decision release predecessor differs from reviewed pin: ' + relative)
    return contract


def clean_environment():
    # No production settings, credentials, Git overrides or Python injection.
    env = {key: value for key, value in os.environ.items()
           if key in {'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT'}}
    env.update(PYTHONDONTWRITEBYTECODE='1', OFFLINE_SUITE_RUNNING='1',
               ASTRA_LEDGER_SYNC_DISABLED='1', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1')
    return env


def git(source, *args, input=None):
    return subprocess.run(['git', '--no-replace-objects', '-c', 'core.fsmonitor=false',
                           '-c', 'core.hooksPath=' + os.devnull, '-C', str(source), *args],
                          check=True, capture_output=True, input=input,
                          env=clean_environment(), timeout=60).stdout


def changes(source, before, after):
    return {path.decode() for path in git(source, 'diff', '--no-ext-diff', '--no-renames',
                                         '--name-only', '-z', before, after).split(b'\0') if path}


def require_visible_index(source):
    entries = [entry for entry in git(source, 'ls-files', '-v', '-z').split(b'\0') if entry]
    require(entries and all(entry.startswith(b'H ') for entry in entries),
            'Decision release index hides tracked working changes')


def tree_entries(source, revision, relative=None):
    args = ('ls-tree', '-z', revision, '--', relative) if relative is not None else ('ls-tree', '-r', '-z', revision)
    entries = {}
    for record in filter(None, git(source, *args).split(b'\0')):
        metadata, separator, name = record.partition(b'\t')
        fields = metadata.split()
        require(separator and len(fields) == 3 and fields[1] == b'blob'
                and fields[0] in (b'100644', b'100755', b'120000') and is_hash(fields[2].decode(), 40),
                'Unreviewed decision release Git mode or type')
        path = name.decode()
        require(path not in entries and not path.startswith('/') and '..' not in Path(path).parts,
                'Invalid decision release Git path')
        entries[path] = (fields[0].decode(), fields[2].decode())
    if relative is not None:
        require(not entries or set(entries) == {relative}, 'Ambiguous decision release tree entry: ' + relative)
        require(not entries or entries[relative][0] == '100644',
                'Unreviewed decision release Git mode or type: ' + relative)
    return entries


def regular_file(source, relative):
    path = source / relative
    require(path.is_file() and not path.is_symlink(), 'Required decision release regular file missing: ' + relative)
    require(stat.S_ISREG(path.stat().st_mode) and not path.stat().st_mode & 0o111,
            'Unreviewed decision release working mode: ' + relative)
    require(all(not parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents),
            'Symlinked decision release source directory: ' + relative)
    return path.read_bytes()


def check_pin(source, revision, relative, expected):
    entries = tree_entries(source, revision, relative)
    if expected is None:
        require(not entries, 'New decision release path already exists in predecessor: ' + relative)
        return
    require(entries, 'Decision release predecessor or required path missing: ' + relative)
    raw = git(source, 'show', revision + ':' + relative)
    require(hashlib.sha256(raw).hexdigest() == expected, 'Unreviewed decision release bytes: ' + relative)


def verify_checkout(source, revision):
    """Compare every tracked raw byte and mode, including unchanged files."""
    require_visible_index(source)
    require(not git(source, 'status', '--porcelain', '--untracked-files=all'),
            'Decision release source is dirty or untracked')
    entries = tree_entries(source, revision)
    require(entries, 'Empty decision release tree')
    objects = list(dict.fromkeys(oid for _, oid in entries.values()))
    batch = git(source, 'cat-file', '--batch', input=('\n'.join(objects) + '\n').encode())
    blobs, offset = {}, 0
    for oid in objects:
        end = batch.find(b'\n', offset)
        require(end >= offset, 'Truncated decision release Git blob response')
        header = batch[offset:end].split()
        require(len(header) == 3 and header[:2] == [oid.encode(), b'blob'] and header[2].isdigit(),
                'Invalid decision release Git blob response')
        size = int(header[2]); offset = end + 1
        require(offset + size < len(batch) and batch[offset + size:offset + size + 1] == b'\n',
                'Truncated decision release Git blob bytes')
        blobs[oid] = batch[offset:offset + size]; offset += size + 1
    require(offset == len(batch), 'Unexpected decision release Git blob response')
    for relative, (mode, oid) in entries.items():
        path = source / relative
        require(all(not parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents),
                'Symlinked decision release source directory: ' + relative)
        if mode == '120000':
            require(path.is_symlink(), 'Decision release working symlink mode changed: ' + relative)
            working = os.fsencode(os.readlink(path))
        elif mode == '100644':
            working = regular_file(source, relative)
        else:
            require(path.is_file() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)
                    and path.stat().st_mode & 0o111, 'Decision release working executable mode changed: ' + relative)
            working = path.read_bytes()
        require(working == blobs[oid], 'Decision release working bytes differ from commit: ' + relative)
    require_visible_index(source)
    require(not git(source, 'status', '--porcelain', '--untracked-files=all'),
            'Decision release source changed during verification')


def verify_current_source(source: Path, upstream: str, revision: str, contract_path: Path | None = None):
    """Validate current bytes only. This is not the complete release-source gate."""
    source = source.resolve()
    require(upstream == UPSTREAM, 'Wrong decision release upstream')
    require(is_hash(revision, 40) and revision not in {BASELINE, REVIEWED_COMMIT},
            'Unpinned revision or ordinary PR alone is not a decision release')
    path = source / CONTRACT_PATH if contract_path is None else contract_path.absolute()
    require(path == source / CONTRACT_PATH, 'Wrong explicit decision release contract path')
    contract = decision_release_contract(path)
    require(git(source, 'rev-parse', '--show-toplevel').decode().strip() == str(source),
            'Decision release source is not the Git checkout root')
    require(git(source, 'rev-parse', 'HEAD').decode().strip() == revision, 'Wrong decision release source commit')
    for anchor, tree in ((BASELINE, BASELINE_TREE), (REVIEWED_COMMIT, REVIEWED_TREE)):
        require(git(source, 'rev-parse', anchor + '^{tree}').decode().strip() == tree,
                'Wrong decision release anchor tree')
    for before, after in ((upstream, BASELINE), (BASELINE, REVIEWED_COMMIT), (REVIEWED_COMMIT, revision)):
        git(source, 'merge-base', '--is-ancestor', before, after)
    require(changes(source, BASELINE, REVIEWED_COMMIT) == REVIEWED_PATHS,
            'Missing or unreviewed decision anchor delta paths')
    require(changes(source, REVIEWED_COMMIT, revision) == RELEASE_INCREMENT_PATHS,
            'Missing or unreviewed decision release increment paths')
    require(changes(source, BASELINE, revision) == REVIEWED_PATHS | RELEASE_INCREMENT_PATHS,
            'Missing or unreviewed complete decision release delta paths')
    # Read the PR declaration only at the fixed historical anchor. It cannot
    # authorize new paths or hashes from a mutable candidate checkout.
    candidate = json.loads(git(source, 'show', REVIEWED_COMMIT + ':' + CANDIDATE_CONTRACT_PATH),
                           object_pairs_hook=unique_fields)
    require(candidate.get('baseline') == BASELINE and candidate.get('baseline_tree') == BASELINE_TREE
            and set(candidate.get('candidate_paths', {})) == REVIEWED_PATHS,
            'Historical PR declaration differs from fixed decision scope')
    for relative, pin in contract['reviewed_paths'].items():
        old = candidate['candidate_paths'][relative]
        require(pin['base_sha256'] == old['base_sha256'] and pin['mode'] == old['mode']
                and (old['sha256'] is None or pin['sha256'] == old['sha256']),
                'Historical PR pins differ from decision review: ' + relative)
        check_pin(source, BASELINE, relative, pin['base_sha256'])
        check_pin(source, REVIEWED_COMMIT, relative, pin['sha256'])
    for relative, pin in contract['release_paths'].items():
        check_pin(source, REVIEWED_COMMIT, relative, pin['reviewed_sha256'])
        check_pin(source, revision, relative, pin['sha256'])
    for relative in SELF_HASH_EXEMPTIONS:
        check_pin(source, REVIEWED_COMMIT, relative, None)
        require(tree_entries(source, revision, relative), 'Decision release trust root missing: ' + relative)
        committed = git(source, 'show', revision + ':' + relative)
        working = regular_file(source, relative)
        require(working == committed, 'Decision release trust-root working bytes changed: ' + relative)
        executing = (_EXECUTING_VERIFIER_BYTES if relative == VERIFIER_PATH
                     else Path(__file__).with_name(Path(relative).name).read_bytes())
        require(working == executing, 'Executing decision release trust root differs from source: ' + relative)
    for relative in REVIEWED_PATHS - RELEASE_INCREMENT_PATHS:
        check_pin(source, revision, relative, contract['reviewed_paths'][relative]['sha256'])
    verify_checkout(source, revision)
    require(git(source, 'rev-parse', 'HEAD').decode().strip() == revision,
            'Decision release HEAD changed during verification')


def verify_historical_baseline(source: Path, upstream: str = UPSTREAM):
    """Run the original complete release guard from an exact baseline worktree."""
    source = source.resolve()
    require(upstream == UPSTREAM, 'Wrong historical decision release upstream')
    require(git(source, 'rev-parse', BASELINE + '^{tree}').decode().strip() == BASELINE_TREE,
            'Wrong historical decision release baseline tree')
    with tempfile.TemporaryDirectory(prefix='astra-decision-baseline-') as temporary:
        baseline = Path(temporary) / 'source'
        git(source, 'worktree', 'add', '--detach', str(baseline), BASELINE)
        try:
            require(git(baseline, 'rev-parse', 'HEAD').decode().strip() == BASELINE,
                    'Wrong historical decision release checkout')
            verify_checkout(baseline, BASELINE)
            subprocess.run([sys.executable, '-I', '-B', str(baseline / HISTORICAL_VERIFIER_PATH),
                            str(baseline), upstream, BASELINE], check=True, env=clean_environment(),
                           timeout=180, cwd=baseline)
            verify_checkout(baseline, BASELINE)
            require(git(baseline, 'rev-parse', 'HEAD').decode().strip() == BASELINE,
                    'Historical decision release HEAD changed during verification')
        finally:
            git(source, 'worktree', 'remove', '--force', str(baseline))


def verify_decision_release_source(source: Path, upstream: str, revision: str, contract_path: Path | None = None):
    verify_current_source(source, upstream, revision, contract_path)
    verify_historical_baseline(source, upstream)
    verify_current_source(source, upstream, revision, contract_path)
    print('DECISION_RELEASE_SOURCE_PASS head=' + revision + ' reviewed_decision=' + REVIEWED_COMMIT
          + ' deployment_authorization=not_granted', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('upstream')
    parser.add_argument('revision')
    parser.add_argument('--contract', type=Path)
    args = parser.parse_args()
    verify_decision_release_source(args.source, args.upstream, args.revision, args.contract)


if __name__ == '__main__':
    main()
