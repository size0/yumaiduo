import { createHash } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';
import { performance } from 'node:perf_hooks';

const current = new AsyncLocalStorage();
const hash = (value) => createHash('sha256').update(String(value ?? '')).digest('hex').slice(0, 24);
function emit(state, value) {
  // Observability must never change execution or expose arguments/results.
  try { state.logger.info('reply_latency', { event_key: state.key, ...value }); } catch {}
}
export async function latencyStage(stage, run) {
  const state = current.getStore();
  if (!state) return run();
  const start = performance.now();
  let success = false;
  emit(state, { stage, phase: 'start' });
  try { const result = await run(); success = true; return result; }
  finally {
    const duration_ms = performance.now() - start;
    state.spans.push({ stage, duration_ms, success });
    emit(state, { stage, phase: 'end', duration_ms, success });
  }
}
export async function latencyRun(eventId, scope, logger, run) {
  const state = { key: hash(eventId), logger, spans: [] };
  const start = performance.now();
  return current.run(state, async () => {
    try { return await latencyStage(scope, run); }
    finally {
      const stages = {};
      for (const span of state.spans) {
        if (span.stage === scope) continue;
        const value = stages[span.stage] ??= { count: 0, duration_ms: 0 };
        value.count++; value.duration_ms += span.duration_ms;
      }
      emit(state, { phase: 'summary', scope, total_ms: performance.now() - start, stages,
        top_slow_stages: Object.entries(stages).sort((a, b) => b[1].duration_ms - a[1].duration_ms).slice(0, 5) });
    }
  });
}
