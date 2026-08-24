import assert from 'node:assert/strict';
import test from 'node:test';
import { releaseGuardMetricsFrom } from '../src/agent/release-guard-metrics.mjs';

test('release guard metrics use only the current active generation and expose zero-tolerance incidents', () => {
  const runs = [
    { mode: 'active', release_generation: 8, status: 'completed', created_at: '2026-08-24T00:00:00Z', updated_at: '2026-08-24T00:00:01Z', observations: [] },
    { mode: 'active', release_generation: 8, status: 'timed_out', created_at: '2026-08-24T00:00:00Z', updated_at: '2026-08-24T00:01:01Z', observations: [{ code: 'write_retry_blocked' }] },
    { mode: 'active', release_generation: 7, status: 'failed', created_at: '2026-08-24T00:00:00Z', updated_at: '2026-08-24T00:10:00Z', observations: [{ code: 'cross_tenant_access' }] },
  ];
  const metrics = releaseGuardMetricsFrom(runs, [{ release_generation: 8, status: 'unknown' }], 8);
  assert.equal(metrics.completed_turns, 2);
  assert.equal(metrics.hard_failures, 1);
  assert.equal(metrics.p95_latency_ms, 61_000);
  assert.equal(metrics.unknown_result_retries, 1);
  assert.equal(metrics.unsafe_final_replies, 1);
  assert.equal(metrics.cross_tenant_access, 0);
});
