import assert from 'node:assert/strict';
import test from 'node:test';
import { createWorkerPool, historicalEvaluationCandidatesFrom, runConcurrentTicks, uniqueAgentEvaluationRuns } from '../src/application.mjs';

test('historical replay defaults to one in-flight run for its single worker', () => {
  const runs = Array.from({ length: 10 }, (_, index) => ({
    run_id: `shadow:${index}`, event_key: `tenant:event-${index}`, mode: 'shadow', status: 'completed',
    updated_at: `2026-08-22T00:00:${String(index).padStart(2, '0')}Z`, result: { runtime_version: 'older', trace: [{ action: 'respond' }] }, tool_calls: [],
  }));
  assert.equal(historicalEvaluationCandidatesFrom(runs, { runtimeVersion: 'current' }).length, 1);
});

test('historical replay scheduler prioritizes image-like sources and keeps only a bounded backlog', () => {
  const version = 'runtime-v3';
  const runs = [
    { run_id: 'shadow:text', event_key: 'tenant:text', mode: 'shadow', status: 'completed', updated_at: '2026-01-03', result: { trace: [{ action: 'respond' }] }, tool_calls: [] },
    { run_id: 'shadow:image', event_key: 'tenant:image', mode: 'shadow', status: 'completed', updated_at: '2026-01-01', result: { trace: [{ action: 'recognize_image' }] }, tool_calls: [{ tool: 'recognize_image' }] },
    { run_id: `evaluation:${version}:existing`, event_key: 'tenant:old', mode: 'evaluation', status: 'processing', result: null, tool_calls: [] },
  ];
  assert.deepEqual(historicalEvaluationCandidatesFrom(runs, { runtimeVersion: version, target: 100, batchSize: 2 }), ['tenant:image']);
  assert.deepEqual(historicalEvaluationCandidatesFrom(runs, {
    runtimeVersion: version, target: 100, batchSize: 1, candidateWindowSize: 20,
  }), [], 'a larger scan window must not create a second in-flight evaluation');
  runs.push({ run_id: `evaluation:${version}:image`, event_key: 'tenant:image', mode: 'evaluation', status: 'completed', result: { runtime_version: version }, tool_calls: [] });
  assert.deepEqual(historicalEvaluationCandidatesFrom(runs, { runtimeVersion: version, target: 2, batchSize: 10 }), ['tenant:text']);
});

test('historical replay keeps independent 100-run image and text quotas', () => {
  const version = 'runtime-v4';
  const imageEvaluations = Array.from({ length: 100 }, (_, index) => ({
    run_id: `evaluation:${version}:image-${index}`, event_key: `tenant:image-done-${index}`,
    mode: 'evaluation', status: 'completed', runtime_version: version, has_image: true,
  }));
  const candidates = [
    { run_id: 'shadow:image-new', event_key: 'tenant:image-new', mode: 'shadow', status: 'completed', has_image: true, updated_at: '2026-01-02' },
    { run_id: 'shadow:text-new', event_key: 'tenant:text-new', mode: 'shadow', status: 'completed', has_image: false, updated_at: '2026-01-01' },
  ];
  assert.deepEqual(historicalEvaluationCandidatesFrom([...imageEvaluations, ...candidates], {
    runtimeVersion: version, imageTarget: 100, textTarget: 100, batchSize: 10,
  }), ['tenant:text-new']);

  const textEvaluations = Array.from({ length: 100 }, (_, index) => ({
    run_id: `evaluation:${version}:text-${index}`, event_key: `tenant:text-done-${index}`,
    mode: 'evaluation', status: 'completed', runtime_version: version, has_image: false,
  }));
  assert.deepEqual(historicalEvaluationCandidatesFrom([...textEvaluations, ...candidates], {
    runtimeVersion: version, imageTarget: 100, textTarget: 100, batchSize: 10,
  }), ['tenant:image-new']);
});

test('agent audit samples deduplicate one source event and prefer current live runs over historical replay', () => {
  const trace = [{ step: 1, action: 'respond' }];
  const selected = uniqueAgentEvaluationRuns([
    { run_id: 'evaluation-current', event_key: 'tenant:event-1', mode: 'evaluation', status: 'completed', updated_at: '2026-08-22T00:03:00Z', result: { runtime_version: 'v5', trace } },
    { run_id: 'shadow-old', event_key: 'tenant:event-1', mode: 'shadow', status: 'completed', updated_at: '2026-08-22T00:01:00Z', result: { runtime_version: 'v4', trace } },
    { run_id: 'shadow-current', event_key: 'tenant:event-1', mode: 'shadow', status: 'completed', updated_at: '2026-08-22T00:02:00Z', result: { runtime_version: 'v5', trace } },
    { run_id: 'evaluation-event-2', event_key: 'tenant:event-2', mode: 'evaluation', status: 'timed_out', updated_at: '2026-08-22T00:04:00Z', result: { runtime_version: 'v5', trace } },
  ], { runtimeVersion: 'v5' });
  assert.deepEqual(selected.map((run) => run.run_id), ['shadow-current', 'evaluation-event-2']);
});

test('one worker cycle starts bounded concurrent workflow ticks', async () => {
  let calls = 0;
  const releases = [];
  const workflow = {
    tick() {
      calls += 1;
      return new Promise((resolve) => releases.push(resolve));
    },
  };
  const running = runConcurrentTicks(workflow, 4);
  await Promise.resolve();
  assert.equal(calls, 4);
  releases.forEach((resolve) => resolve(null));
  await running;
});

test('a free worker lane claims the next event without waiting for a slow lane', async () => {
  let calls = 0;
  let releaseSlow;
  const slow = new Promise((resolve) => { releaseSlow = resolve; });
  const workflow = {
    async tick() {
      calls += 1;
      if (calls === 1) return slow;
      if (calls === 2) return { status: 'completed' };
      return null;
    },
  };
  const running = runConcurrentTicks(workflow, 2);
  for (let index = 0; index < 10 && calls < 3; index += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls, 3, 'the free lane should immediately request another event');
  releaseSlow({ status: 'completed' });
  await running;
});

test('worker pool polling can fill free slots while an earlier quote is still running', async () => {
  let calls = 0;
  let available = false;
  let processed = 0;
  let releaseSlow;
  const slow = new Promise((resolve) => { releaseSlow = resolve; });
  const workflow = {
    async tick() {
      calls += 1;
      if (calls === 1) return slow;
      if (available) {
        available = false;
        processed += 1;
        return { status: 'completed' };
      }
      return null;
    },
  };
  const pool = createWorkerPool(workflow, { concurrency: 4 });
  pool.poll();
  for (let index = 0; index < 10 && calls < 4; index += 1) await new Promise((resolve) => setImmediate(resolve));
  available = true;
  pool.poll();
  for (let index = 0; index < 10 && processed < 1; index += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(processed, 1, 'a newly due message should use a free slot before the slow quote finishes');
  releaseSlow({ status: 'completed' });
  await pool.stop();
});
