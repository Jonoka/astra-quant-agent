import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { requireCompleteTap, SUITES } from './run_collection_frontend.mjs';
const good = '# tests 23\n# suites 0\n# pass 23\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n';
test('all four focused suites and 23 tests remain explicit', () => {
  assert.equal(Object.keys(SUITES).length, 4);
  assert.equal(Object.values(SUITES).reduce((a, b) => a + b, 0), 23);
  assert.equal(SUITES['tests/jobOutcome.test.mjs'], 6);
});
test('complete TAP is accepted', () => assert.equal(requireCompleteTap(good, 23), 23));
test('empty, missing and partial discovery fail closed', () => {
  for (const value of ['', good.replace('# tests 23\n', ''), good.replaceAll('23', '22'), good + '# tests 23\n']) {
    assert.throws(() => requireCompleteTap(value, 23));
  }
});
test('fail, cancellation, skip and todo cannot be green', () => {
  for (const label of ['fail', 'cancelled', 'skipped', 'todo']) {
    assert.throws(() => requireCompleteTap(good.replace(`# ${label} 0`, `# ${label} 1`), 23));
  }
  assert.throws(() => requireCompleteTap(good.replace('# pass 23', '# pass 22'), 23));
});
test('caught network attempts still fail the frontend gate', () => {
  assert.throws(() => requireCompleteTap(good + 'OFFLINE_GUARD_BLOCKED fetch\n', 23));
});
test('actual guard denies fetch and DNS even when caught by the process', () => {
  const guard = fileURLToPath(new URL('./node_offline_guard.mjs', import.meta.url));
  for (const code of ["try { fetch('https://192.0.2.1/'); } catch {}",
                      "const dns=require('node:dns'); try {dns.lookup('example.com',()=>{});} catch {}",
                      "const net=require('node:net'); try {net.connect(443,'192.0.2.1');} catch {}"] ) {
    const result = spawnSync(process.execPath, ['--import', guard, '-e', code], { encoding: 'utf8', timeout: 10000 });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /OFFLINE_GUARD_BLOCKED/);
  }
});
