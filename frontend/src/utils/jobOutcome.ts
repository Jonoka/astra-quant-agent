/** Inspection completion and the correlated brain result are separate facts. */
export interface JobRun {
  job_name?: string;
  status?: string;
  return_code?: number | null;
  process_status?: string;
  brain_status?: string;
}

export interface JobRunIdentity {
  job_run_id?: number | string | null;
  scheduled_at?: string | null;
}

/** A reused SQLite ID alone cannot identify a result after DB replacement. */
export function isSameJobRun(left: JobRunIdentity = {}, right: JobRunIdentity = {}): boolean {
  const validId = (id: unknown) => (typeof id === 'number' && Number.isInteger(id) && id > 0)
    || (typeof id === 'string' && /^[1-9]\d*$/.test(id));
  return validId(left.job_run_id) && validId(right.job_run_id)
    && String(left.job_run_id) === String(right.job_run_id)
    && typeof left.scheduled_at === 'string' && left.scheduled_at.length > 0
    && left.scheduled_at === right.scheduled_at;
}

export function statusTone(status: string): string {
  if (['success', 'ok', 'healthy'].includes(status)) return 'badge-up';
  if (['failed', 'interrupted', 'degraded'].includes(status)) return 'badge-down';
  return 'badge-warn';
}

export function resolveJobOutcome(run: JobRun = {}) {
  const processStatus = run.process_status || (run.return_code != null
    ? run.return_code === 0 ? 'success' : 'failed'
    : run.status || 'unknown');
  const brainStatus = run.brain_status || (run.job_name === 'trader'
    ? run.status === 'skipped' ? 'not_run' : 'unknown'
    : 'not_applicable');
  let status = run.status || 'unknown';
  if (run.job_name === 'trader' && processStatus === 'success') {
    status = brainStatus === 'failed' ? 'failed' : brainStatus === 'ok' ? 'success' : 'completed';
  }
  return { status, processStatus, brainStatus, tone: statusTone(status),
    processTone: statusTone(processStatus), brainTone: statusTone(brainStatus) };
}
