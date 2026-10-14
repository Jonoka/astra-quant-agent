"""Exact ordinary-PR candidate source gate; never grants release eligibility.

The contract and this verifier are the only self-hash exemptions. Their review
requires an independent final commit freeze and remote readback; this gate is
not proof of its own approval and never derives approval hashes at runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess

BASELINE = '60133ce82d4a0b91f94bc0c613a07e24aff049f5'
BASELINE_TREE = '64059d6490d5734a3e764c5b742cf4fad2f8016a'
CONTRACT_PATH = '.github/scripts/pr_candidate_contract.json'
VERIFIER_PATH = '.github/scripts/verify_pr_candidate.py'
SELF_HASH_EXEMPTIONS = frozenset({CONTRACT_PATH, VERIFIER_PATH})
CI_PATHS = frozenset({
    CONTRACT_PATH, VERIFIER_PATH,
    '.github/scripts/test_pr_candidate_contract.py',
    '.github/scripts/run_deployment_preflight.py',
    '.github/scripts/run_decision_contract_regressions.py',
    '.github/scripts/test_decision_regression_gate.py',
    '.github/workflows/test-taker-parsing.yml',
})
APPLICATION_PATHS = frozenset({
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


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate candidate contract field: ' + key)
        result[key] = value
    return result


def is_hash(value, length=64):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{' + str(length) + '}', value) is not None


def candidate_contract(path: Path):
    """Read fixed approval pins, rejecting additional fields, paths and roots."""
    contract = json.loads(path.read_bytes(), object_pairs_hook=unique_fields)
    require(isinstance(contract, dict) and set(contract) == {
        'schema', 'purpose', 'baseline', 'baseline_tree', 'candidate_paths',
        'self_hash_exemptions'}, 'Invalid candidate contract fields')
    require(type(contract['schema']) is int and contract['schema'] == 1
            and contract['purpose'] == 'ordinary_pull_request_only', 'Wrong candidate schema or purpose')
    require(contract['baseline'] == BASELINE and contract['baseline_tree'] == BASELINE_TREE,
            'Wrong candidate baseline or tree')
    exemptions = contract['self_hash_exemptions']
    require(isinstance(exemptions, list) and len(exemptions) == 2
            and all(isinstance(value, str) for value in exemptions)
            and set(exemptions) == SELF_HASH_EXEMPTIONS, 'Unreviewed candidate trust root')
    paths = contract['candidate_paths']
    require(isinstance(paths, dict) and set(paths) == APPLICATION_PATHS | CI_PATHS,
            'Missing or unreviewed candidate contract paths')
    for relative, pin in paths.items():
        require(isinstance(pin, dict) and set(pin) == {
            'base_sha256', 'sha256', 'base_mode', 'mode'}, 'Invalid candidate pin fields: ' + relative)
        require(pin['mode'] == '100644', 'Invalid candidate mode: ' + relative)
        require((pin['base_sha256'] is None and pin['base_mode'] is None)
                or (is_hash(pin['base_sha256']) and pin['base_mode'] == '100644'),
                'Invalid candidate predecessor hash or mode: ' + relative)
        require(pin['sha256'] is None if relative in SELF_HASH_EXEMPTIONS else is_hash(pin['sha256']),
                'Invalid candidate hash or self-hash exemption: ' + relative)
        if relative in SELF_HASH_EXEMPTIONS:
            require(pin['base_sha256'] is None, 'Candidate trust root already exists in baseline: ' + relative)
    return contract


def git(source, *args):
    return subprocess.run(['git', '-C', str(source), *args], check=True,
                          capture_output=True, timeout=60).stdout


def require_visible_index(source):
    # A clean status is insufficient when tracked working bytes are hidden by
    # assume-unchanged or skip-worktree. Fresh CI checkouts need neither flag.
    entries = [entry for entry in git(source, 'ls-files', '-v', '-z').split(b'\0') if entry]
    require(entries and all(entry.startswith(b'H ') for entry in entries),
            'Candidate index hides tracked working changes')


def tree_entry(source, revision, relative):
    records = [record for record in git(source, 'ls-tree', '-z', revision, '--', relative).split(b'\0') if record]
    if not records:
        return None
    require(len(records) == 1, 'Ambiguous candidate tree entry: ' + relative)
    metadata, separator, name = records[0].partition(b'\t')
    fields = metadata.split()
    require(separator and name.decode() == relative and len(fields) == 3
            and fields[:2] == [b'100644', b'blob'] and is_hash(fields[2].decode(), 40),
            'Unreviewed candidate Git mode or type: ' + relative)
    return fields[2].decode()


def regular_file(source, relative):
    path = source / relative
    require(path.is_file() and not path.is_symlink(), 'Required candidate regular file missing: ' + relative)
    require(stat.S_ISREG(path.stat().st_mode) and not path.stat().st_mode & 0o111,
            'Unreviewed candidate working mode: ' + relative)
    require(all(not parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents),
            'Symlinked candidate source directory: ' + relative)
    return path.read_bytes()


def verify_pr_candidate(source: Path, contract_path: Path, revision: str) -> None:
    source = source.resolve()
    require(is_hash(revision, 40) and revision != BASELINE, 'Unpinned or baseline-only candidate revision')
    require(contract_path.absolute() == source / CONTRACT_PATH, 'Wrong explicit candidate contract path')
    contract = candidate_contract(contract_path)
    require(git(source, 'rev-parse', '--show-toplevel').decode().strip() == str(source),
            'Candidate source is not the Git checkout root')
    require(git(source, 'rev-parse', 'HEAD').decode().strip() == revision, 'Wrong candidate source commit')
    require(git(source, 'rev-parse', BASELINE + '^{tree}').decode().strip() == BASELINE_TREE,
            'Wrong candidate baseline tree')
    git(source, 'merge-base', '--is-ancestor', BASELINE, revision)
    delta = {path.decode() for path in git(source, 'diff', '--no-renames', '--name-only', '-z', BASELINE, revision).split(b'\0') if path}
    require(delta == set(contract['candidate_paths']), 'Missing or unreviewed candidate delta paths')
    require_visible_index(source)
    require(not git(source, 'status', '--porcelain', '--untracked-files=all'), 'Candidate source is dirty or untracked')
    for relative, pin in contract['candidate_paths'].items():
        before = tree_entry(source, BASELINE, relative)
        if pin['base_sha256'] is None:
            require(before is None, 'New candidate path exists in baseline: ' + relative)
        else:
            require(before is not None, 'Candidate predecessor path missing: ' + relative)
            raw = git(source, 'show', BASELINE + ':' + relative)
            require(hashlib.sha256(raw).hexdigest() == pin['base_sha256'], 'Candidate predecessor bytes changed: ' + relative)
        require(tree_entry(source, revision, relative) is not None, 'Candidate path deleted: ' + relative)
        committed = git(source, 'show', revision + ':' + relative)
        working = regular_file(source, relative)
        require(working == committed, 'Candidate working bytes differ from commit: ' + relative)
        if relative in SELF_HASH_EXEMPTIONS:
            executing = Path(__file__).with_name(Path(relative).name)
            require(working == executing.read_bytes(), 'Executing candidate trust root differs from source: ' + relative)
        else:
            require(hashlib.sha256(committed).hexdigest() == pin['sha256'], 'Unreviewed candidate bytes: ' + relative)
    require_visible_index(source)
    require(not git(source, 'status', '--porcelain', '--untracked-files=all'), 'Candidate source changed during verification')
    print('PR_CANDIDATE_SOURCE_PASS head=' + revision + ' baseline=' + BASELINE
          + ' release_eligibility=not_granted', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('revision')
    parser.add_argument('--contract', required=True, type=Path)
    args = parser.parse_args()
    verify_pr_candidate(args.source, args.contract, args.revision)


if __name__ == '__main__':
    main()
