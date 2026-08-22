import assert from 'node:assert/strict';
import test from 'node:test';
import {
  createLifecycleController,
  createWorkerPool,
  historicalEvaluationCandidatesFrom,
} from '../src/bootstrap/create-lifecycle-controller.mjs';

function harness({ withAgent = true } = {}) {
  const calls = [];
  const intervals = [];
  const cleared = [];
  const pools = [];
  const sourceEnvelope = { id: 'event-1', tenantId: 'tenant-1', event: 'im.message.received', payload: {} };
  const storage = {
    async initialize() { calls.push('initialize'); },
    agentRunStore: {
      async list(input) { calls.push(['list-runs', input]); return [{ mode: 'shadow', event_key: 'tenant-1:event-1', updated_at: '2026-08-24T00:00:00Z' }]; },
    },
    eventStore: {
      async getMany(keys) { calls.push(['get-events', keys]); return [{ status: 'completed', envelope: sourceEnvelope }]; },
    },
  };
  const workflow = { name: 'workflow', async tick() { return null; } };
  const shadowAgentRuntime = withAgent ? {
    name: 'agent',
    async tick() { return null; },
    async schedule(envelope, options) { calls.push(['schedule', envelope, options]); },
    stop() { calls.push('abort-agent'); },
  } : null;
  const agentReplyOutboxDispatcher = { name: 'outbox', async tick() { return null; } };
  const agentHumanComparisonScanner = { async tick() { calls.push('scan-humans'); } };
  const createWorkerPool = (worker, options) => {
    const pool = {
      worker, options, polls: 0, stopped: false,
      poll() { this.polls += 1; },
      async stop() { this.stopped = true; calls.push(`stop-${worker.name}`); },
    };
    pools.push(pool);
    return pool;
  };
  const setIntervalFn = (callback, milliseconds) => {
    const timer = { callback, milliseconds, unrefCalls: 0, unref() { this.unrefCalls += 1; } };
    intervals.push(timer);
    return timer;
  };
  const clearIntervalFn = (timer) => cleared.push(timer);
  const controller = createLifecycleController({
    storage, workflow, shadowAgentRuntime, agentReplyOutboxDispatcher, agentHumanComparisonScanner,
    workerIntervalMs: 250, runtimeVersion: 'runtime-v1', logger: { warn() {}, error() {} },
    createWorkerPool, setIntervalFn, clearIntervalFn,
  });
  return { controller, calls, intervals, cleared, pools, sourceEnvelope };
}

test('starts durable workers in the original bounded topology and schedules historical evaluation', async () => {
  const state = harness();
  await state.controller.start();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(state.calls[0], 'initialize');
  assert.deepEqual(state.pools.map((pool) => [pool.worker.name, pool.options.concurrency, pool.polls]), [
    ['workflow', 4, 1], ['agent', 1, 2], ['outbox', 1, 1],
  ]);
  assert.deepEqual(state.intervals.map((timer) => timer.milliseconds), [250, 500, 500, 15_000, 30_000]);
  assert.equal(state.intervals.every((timer) => timer.unrefCalls === 1), true);
  assert.deepEqual(state.calls.find((item) => Array.isArray(item) && item[0] === 'schedule'), [
    'schedule', state.sourceEnvelope, { mode: 'evaluation' },
  ]);
  assert.deepEqual(state.controller.status(), {
    worker: 'running', agent_worker: 'running', agent_outbox_worker: 'running',
    historical_evaluation_worker: 'running', human_comparison_worker: 'running',
  });
  state.intervals[0].callback();
  state.intervals[1].callback();
  state.intervals[2].callback();
  assert.deepEqual(state.pools.map((pool) => pool.polls), [2, 3, 2]);
});

test('isolates background scanner failures and prevents overlapping scanner work', async () => {
  let release;
  let scans = 0;
  const state = harness();
  state.controller = createLifecycleController({
    storage: { ...state.controller.storage, initialize: async () => {}, agentRunStore: { list: async () => [] }, eventStore: { getMany: async () => [] } },
    workflow: { name: 'workflow', tick: async () => null },
    shadowAgentRuntime: null,
    agentReplyOutboxDispatcher: { name: 'outbox', tick: async () => null },
    agentHumanComparisonScanner: { tick: async () => { scans += 1; await new Promise((resolve) => { release = resolve; }); } },
    workerIntervalMs: 250, runtimeVersion: 'runtime-v1', logger: { warn() {}, error() {} },
    createWorkerPool: (worker, options) => ({ worker, options, poll() {}, async stop() {} }),
    setIntervalFn: (callback, milliseconds) => { const timer = { callback, milliseconds, unref() {} }; state.intervals.push(timer); return timer; },
    clearIntervalFn() {},
  });
  await state.controller.start();
  const scanTimer = state.intervals.filter((timer) => timer.milliseconds === 30_000).at(-1);
  const first = scanTimer.callback();
  const second = scanTimer.callback();
  await Promise.resolve();
  assert.equal(scans, 1);
  release();
  await Promise.all([first, second]);
});

test('stops timers, aborts Agent work, and drains every worker pool', async () => {
  const state = harness();
  await state.controller.start();
  await state.controller.stop();

  assert.equal(state.cleared.length, 5);
  assert.equal(state.calls.includes('abort-agent'), true);
  assert.equal(state.pools.every((pool) => pool.stopped), true);
  assert.deepEqual(state.controller.status(), {
    worker: 'stopped', agent_worker: 'stopped', agent_outbox_worker: 'stopped',
    historical_evaluation_worker: 'stopped', human_comparison_worker: 'stopped',
  });
});

test('does not create an Agent worker when no planner runtime exists', async () => {
  const state = harness({ withAgent: false });
  await state.controller.start();
  assert.deepEqual(state.pools.map((pool) => pool.worker.name), ['workflow', 'outbox']);
  assert.equal(state.controller.status().agent_worker, 'stopped');
});

test('historical scheduler prefers the complete durable evaluation index', async () => {
  let indexReads = 0;
  let boundedReads = 0;
  const scheduled = [];
  const controller = createLifecycleController({
    storage: {
      initialize: async () => {},
      agentRunStore: {
        listEvaluationIndex: async () => { indexReads += 1; return [
          { run_id: 'shadow:missing-image', event_key: 'tenant:missing-image', mode: 'shadow', status: 'completed', has_image: true },
          { run_id: 'shadow:old-text', event_key: 'tenant:old-text', mode: 'shadow', status: 'completed', has_image: false },
        ]; },
        list: async () => { boundedReads += 1; return []; },
      },
      eventStore: { getMany: async () => [{ status: 'completed', envelope: { id: 'old-text', tenantId: 'tenant', event: 'im.message.received', payload: {} } }] },
    },
    workflow: { tick: async () => null },
    shadowAgentRuntime: { tick: async () => null, schedule: async (event) => scheduled.push(event.id), stop() {} },
    agentReplyOutboxDispatcher: { tick: async () => null },
    agentHumanComparisonScanner: { tick: async () => null },
    workerIntervalMs: 250,
    runtimeVersion: 'runtime-v1',
    logger: { warn() {}, error() {} },
    createWorkerPool: () => ({ poll() {}, async stop() {} }),
    setIntervalFn: (callback, milliseconds) => ({ callback, milliseconds, unref() {} }),
    clearIntervalFn() {},
  });
  await controller.start();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(indexReads, 1);
  assert.equal(boundedReads, 0);
  assert.deepEqual(scheduled, ['old-text']);
  await controller.stop();
});

test('contains historical and human comparison background failures', async () => {
  const warnings = [];
  const intervals = [];
  const controller = createLifecycleController({
    storage: {
      initialize: async () => {},
      agentRunStore: { list: async () => { throw new Error('history unavailable'); } },
      eventStore: { getMany: async () => [] },
    },
    workflow: { tick: async () => null },
    shadowAgentRuntime: { tick: async () => null, schedule: async () => {}, stop() {} },
    agentReplyOutboxDispatcher: { tick: async () => null },
    agentHumanComparisonScanner: { tick: async () => { throw new Error('scan unavailable'); } },
    workerIntervalMs: 250,
    runtimeVersion: 'runtime-v1',
    logger: { warn(message) { warnings.push(message); }, error() {} },
    createWorkerPool: () => ({ poll() {}, async stop() {} }),
    setIntervalFn: (callback, milliseconds) => { const timer = { callback, milliseconds, unref() {} }; intervals.push(timer); return timer; },
    clearIntervalFn() {},
  });
  await controller.start();
  await new Promise((resolve) => setImmediate(resolve));
  await intervals.find((timer) => timer.milliseconds === 30_000).callback();
  assert.deepEqual(warnings, [
    '[agent-evaluation] historical replay scheduling failed',
    '[human-comparison] background scan failed',
  ]);
  await controller.stop();
});

test('historical candidate selection rejects unversioned and malformed inputs', () => {
  assert.deepEqual(historicalEvaluationCandidatesFrom(null, { runtimeVersion: '' }), []);
  assert.deepEqual(historicalEvaluationCandidatesFrom([], { runtimeVersion: 'v1', target: 'invalid', batchSize: 'invalid' }), []);
});

test('worker pool contains tick failures without leaving active lanes behind', async () => {
  const errors = [];
  const pool = createWorkerPool({ tick: async () => { throw new Error('tick failed'); } }, {
    concurrency: 1,
    logger: { error(message) { errors.push(message); } },
  });
  pool.poll();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(errors, ['workflow tick failed']);
  await pool.stop();
});

test('fails closed when lifecycle dependencies are incomplete', () => {
  const storage = { initialize() {}, agentRunStore: { list() {} }, eventStore: {} };
  const workflow = { tick() {} };
  const outbox = { tick() {} };
  const scanner = { tick() {} };
  assert.throws(() => createLifecycleController({}), /lifecycle storage dependencies/u);
  assert.throws(() => createLifecycleController({ storage }), /workflow\.tick/u);
  assert.throws(() => createLifecycleController({ storage, workflow, agentReplyOutboxDispatcher: outbox, agentHumanComparisonScanner: scanner, createWorkerPool: null }), /scheduling dependencies/u);
});
