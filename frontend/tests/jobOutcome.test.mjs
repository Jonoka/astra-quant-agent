import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { isSameJobRun, resolveJobOutcome } from '../src/utils/jobOutcome.ts';

test('historical brain result identity requires both run ID and scheduled slot', () => {
  const run = { job_run_id: 7, scheduled_at: '2026-10-05T12:00:00+08:00' };
  assert.equal(isSameJobRun(run, { ...run, job_run_id: '7' }), true);
  for (const historical of [{ ...run, scheduled_at: 'old-slot' }, { ...run, job_run_id: 8 },
    { job_run_id: 7 }, {}, { ...run, job_run_id: true }]) {
    assert.equal(isSameJobRun(historical, run), false);
  }
  assert.equal(isSameJobRun({}, {}), false);
  const radar = readFileSync(new URL('../src/views/dashboard/RadarView.vue', import.meta.url), 'utf8');
  assert.match(radar, /!isSameJobRun\(aiHealth\.last_known_result, aiHealth\)/);
});

test('legacy scheduler failure and unconfirmed brain are never painted healthy', () => {
  const source = readFileSync(new URL('../public/admin/legacy.html', import.meta.url), 'utf8');
  const renderer = source.split('\n').find((line) => line.startsWith('function renderGateway('));
  assert.ok(renderer);
  const elements = {};
  const context = vm.createContext({
    $: (id) => elements[id] ||= {}, metric: () => '', esc: String,
    window: {}, document: { querySelectorAll: () => [] },
  });
  vm.runInContext(renderer, context);
  const payload = { stats: {}, event_health: {}, deliveries: [],
    scheduler: { recent_runs: [], jobs: [] } };
  for (const [status, tone] of [['failed', 'bad'], ['interrupted', 'bad'],
    ['completed', 'amber'], ['running', 'amber'], ['success', 'good']]) {
    payload.scheduler.jobs = [{ name: 'daily_briefing', overdue: false,
      latest_run: { status } }];
    context.renderGateway(payload);
    assert.ok(elements.schedulerRows.innerHTML.includes(`<td class="${tone}">${status}`));
  }
  payload.scheduler.jobs = [{ name: 'trader', overdue: false,
    latest_run: { status: 'completed', process_status: 'success', brain_status: 'unknown' } }];
  context.renderGateway(payload);
  assert.match(elements.schedulerRows.innerHTML, /巡检: success · 主脑: unknown/);
  assert.doesNotMatch(elements.schedulerRows.innerHTML, /class="good"/);
});

test('inspection rc0 and a failed brain preserve separate outcomes with failure styling', () => {
  const result = resolveJobOutcome({ job_name: 'trader', status: 'failed', return_code: 0, process_status: 'success', brain_status: 'failed' });
  assert.deepEqual([result.status, result.processStatus, result.brainStatus, result.tone, result.processTone, result.brainTone],
    ['failed', 'success', 'failed', 'badge-down', 'badge-up', 'badge-down']);
});

test('legacy rc0 and unknown brain results never produce a green success', () => {
  for (const brain_status of [undefined, '', 'unknown', 'skipped', 'pending']) {
    const result = resolveJobOutcome({ job_name: 'trader', status: 'success', return_code: 0, brain_status });
    assert.equal(result.status, 'completed');
    assert.equal(result.tone, 'badge-warn');
    assert.notEqual(result.brainTone, 'badge-up');
  }
});

test('confirmed success, process failures and non-brain jobs remain distinct', () => {
  assert.equal(resolveJobOutcome({ job_name: 'trader', status: 'success', process_status: 'success', brain_status: 'ok' }).status, 'success');
  assert.equal(resolveJobOutcome({ job_name: 'trader', status: 'failed', process_status: 'failed', brain_status: 'ok' }).status, 'failed');
  assert.equal(resolveJobOutcome({ job_name: 'news', status: 'success', return_code: 0 }).brainStatus, 'not_applicable');
  assert.equal(resolveJobOutcome({ job_name: 'trader', status: 'skipped' }).brainStatus, 'not_run');
  assert.equal(resolveJobOutcome().status, 'unknown');
});

test('Gateway, worker roster and Radar expose both facts and audit identity', () => {
  const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
  const gateway = read('../src/views/admin/GatewayPage.vue');
  const agents = read('../src/views/admin/AgentsPage.vue');
  const radar = read('../src/views/dashboard/RadarView.vue');
  assert.match(gateway, /scheduler\?\.recent_runs/);
  assert.match(gateway, /r\.scheduled_at/);
  assert.match(gateway, /selectedRun\.return_code/);
  assert.match(gateway, /selectedRun\.brain_detail/);
  assert.match(gateway, /resolveJobOutcome\(j\.latest_run\)\.tone/);
  assert.match(gateway, /<BaseDrawer[^>]*:open="!!selectedRun"[^>]*@close="selectedRun = null"/);
  assert.match(agents, /a\.brain_status/);
  assert.match(agents, /agentOutcome\(a\)\.processStatus/);
  assert.match(agents, /a\.last_job_run_id/);
  assert.match(radar, /aiHealth\.last_status/);
  assert.match(radar, /aiHealth\.process_status/);
  assert.match(radar, /last_known_result/);
  for (const language of ['zh', 'en']) {
    const locale = read(`../src/locales/${language}/admin/gateway.ts`);
    for (const key of ['unknown', 'not_run', 'skipped', 'pending', 'failed', 'ok']) {
      assert.match(locale, new RegExp(`\\b${key}:`));
    }
  }
});
