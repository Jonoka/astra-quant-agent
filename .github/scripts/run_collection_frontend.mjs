// Focused 23-test frontend gate. Does not claim the known-failing full suite passes.
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

export const SUITES = Object.freeze({
  'tests/absorbedPageTabs.test.mjs': 5,
  'tests/agentsCacheTelemetry.test.mjs': 4,
  'tests/jobOutcome.test.mjs': 6,
  'tests/tradingSessionWindow.test.mjs': 8,
});

export function requireCompleteTap(text, minimum) {
  const count = (name) => {
    const matches = [...text.matchAll(new RegExp(`^# ${name} (\\d+)$`, 'gm'))];
    if (matches.length !== 1) throw new Error(`Missing or ambiguous TAP ${name} summary`);
    return Number(matches[0][1]);
  };
  const tests = count('tests');
  if (tests < minimum || count('pass') !== tests || count('fail') !== 0
      || count('cancelled') !== 0 || count('skipped') !== 0 || count('todo') !== 0
      || /OFFLINE_GUARD_BLOCKED|^not ok /m.test(text)) {
    throw new Error('Frontend tests failed, incomplete, skipped, todo or attempted network');
  }
  return tests;
}

function main() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const root = path.resolve(here, '../..');
  const frontend = path.join(root, 'frontend');
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (key.startsWith('ASTRA_') || ['GITHUB_TOKEN', 'GH_TOKEN'].includes(key)
        || /(?:API_KEY|ACCESS_TOKEN|SECRET_KEY)$/.test(key)) delete env[key];
  }
  env.NODE_OPTIONS = `--import=${pathToFileURL(path.join(here, 'node_offline_guard.mjs')).href}`;
  env.npm_config_update_notifier = 'false';
  env.npm_config_offline = 'true';
  function run(command, args, cwd = frontend) {
    const result = spawnSync(command, args, { cwd, env, encoding: 'utf8', timeout: 240000, maxBuffer: 32 * 1024 * 1024 });
    const output = (result.stdout || '') + (result.stderr || '');
    process.stdout.write(output);
    if (result.error || result.status !== 0 || result.signal || output.includes('OFFLINE_GUARD_BLOCKED')) {
      throw new Error(`Offline frontend command failed: ${path.basename(command)} ${args.join(' ')}`);
    }
    return output;
  }
  if (process.argv.slice(2).some(arg => arg !== '--self-test')) throw new Error('Unexpected frontend gate argument');
  if (process.argv.includes('--self-test')) {
    const count = requireCompleteTap(run(process.execPath, ['--test', '--test-reporter=tap', path.join(here, 'test_collection_frontend_gate.mjs')]), 6);
    process.stdout.write(`FRONTEND_GATE_SELF_TEST_PASS tests=${count} skips=0 todo=0\n`);
    return;
  }
  let count = 0;
  for (const [suite, minimum] of Object.entries(SUITES)) {
    count += requireCompleteTap(run(process.execPath, ['--test', '--test-reporter=tap', suite]), minimum);
  }
  run(path.join(frontend, 'node_modules/.bin/vue-tsc'), ['-b', '--pretty', 'false']);
  run('npm', ['run', 'build', '--offline']);
  process.stdout.write(`COLLECTION_FRONTEND_PR_PASS focused_tests=${count} skips=0 todo=0 types=passed build=passed full_suite=not_run release_eligibility=not_granted\n`);
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) main();
