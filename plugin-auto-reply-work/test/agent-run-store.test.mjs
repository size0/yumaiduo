import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { AgentRunStore } from '../src/agent/agent-run-store.mjs';

test('agent runs are idempotent, leased independently, and resume from durable checkpoints', async () => {
  let now = 1_000;
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-runs-')), 'runs.json');
  const store = new AgentRunStore(file, { now: () => now });
  await store.initialize();
  const input = { runId: 'shadow:event-1', eventKey: 'tenant-1:event-1', tenantId: 'tenant-1', mode: 'shadow', deadlineMs: 600_000 };
  assert.equal((await store.enqueue(input)).created, true);
  assert.equal((await store.enqueue(input)).created, false);

  const first = await store.claimDue({ leaseMs: 300_000 });
  assert.equal(first.status, 'processing');
  await store.checkpoint(first.run_id, first.lease_id, {
    trace: [{ step: 1, action: 'start_quote', intent: '选座核价', confidence: 0.95 }],
    observations: [{ status: 'success', tool: 'recognize_and_quote', summary: '权威流程已完成', facts: { status: 'preview_ready' }, next_actions: ['respond'] }],
  }, { leaseMs: 300_000 });

  now += 300_001;
  const reclaimed = await store.claimDue({ leaseMs: 300_000 });
  assert.equal(reclaimed.run_id, first.run_id);
  assert.equal(reclaimed.attempts, 2);
  assert.equal(reclaimed.trace.length, 1);
  assert.equal(reclaimed.observations.length, 1);
  await store.complete(reclaimed.run_id, reclaimed.lease_id, { status: 'reply', reason: 'agent_response' });
  assert.equal((await store.get(first.run_id)).status, 'completed');
});

test('agent run persists a bounded source-time planner snapshot without transaction identifiers', async () => {
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-context-')), 'runs.json');
  const store = new AgentRunStore(file);
  await store.initialize();
  await store.enqueue({
    runId: 'shadow:context', eventKey: 'tenant-1:context', tenantId: 'tenant-1', mode: 'shadow',
    contextSnapshot: {
      facts: { stage: 'collecting_information', city: '泉州', order_id: 'platform-order-secret', ticket_count: 1 },
      messages: [
        { at: 100, role: 'seller', source: 'external_seller', content: '哪个店？' },
        { at: 200, role: 'buyer', source: 'buyer', content: '第二个' },
      ],
    },
  });

  const run = await store.get('shadow:context');
  assert.equal(run.context_snapshot.facts.city, '泉州');
  assert.equal(run.context_snapshot.facts.has_linked_order, true);
  assert.equal(run.context_snapshot.facts.order_id, undefined);
  assert.deepEqual(run.context_snapshot.messages.map(({ role, content, source }) => ({ role, content, source })), [
    { role: 'seller', content: '哪个店？', source: 'external_seller' },
    { role: 'buyer', content: '第二个', source: 'buyer' },
  ]);
});

test('agent snapshot preserves cross-turn assistant tool calls and tool results without a fifty-message slice', async () => {
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-standard-context-')), 'runs.json');
  const store = new AgentRunStore(file);
  await store.initialize();
  const messages = Array.from({ length: 60 }, (_, index) => ({ role: 'buyer', content: `历史消息${index}`, at: index }));
  messages.push({ role: 'assistant', content: '', at: 61, tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'read_active_quote', arguments: '{}' } }] });
  messages.push({ role: 'tool', content: '{"status":"success"}', at: 62, tool_call_id: 'call-1', name: 'read_active_quote' });
  await store.enqueue({ runId: 'shadow:standard-context', eventKey: 'tenant-1:standard-context', tenantId: 'tenant-1', contextSnapshot: { facts: {}, messages } });
  const snapshot = (await store.get('shadow:standard-context')).context_snapshot.messages;
  assert.equal(snapshot.length, 62);
  assert.equal(snapshot[0].content, '历史消息0');
  assert.equal(snapshot.at(-2).role, 'assistant');
  assert.equal(snapshot.at(-2).tool_calls[0].function.name, 'read_active_quote');
  assert.equal(snapshot.at(-1).role, 'tool');
  assert.equal(snapshot.at(-1).tool_call_id, 'call-1');
});

test('historical evaluation runs enqueue in one idempotent bounded batch', async () => {
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-batch-')), 'runs.json');
  const store = new AgentRunStore(file);
  await store.initialize();
  const batch = [1, 2].map((index) => ({ runId: `evaluation:v3:${index}`, eventKey: `tenant-1:event-${index}`, tenantId: 'tenant-1', mode: 'evaluation' }));
  assert.deepEqual(await store.enqueueMany(batch), { created: 2, existing: 0 });
  assert.deepEqual(await store.enqueueMany(batch), { created: 0, existing: 2 });
  assert.equal((await store.list({ tenantId: 'tenant-1' })).filter((run) => run.mode === 'evaluation').length, 2);
  await store.enqueue({ runId: 'shadow:live', eventKey: 'tenant-1:live', tenantId: 'tenant-1', mode: 'shadow' });
  assert.equal((await store.claimDue()).run_id, 'shadow:live');
  await assert.rejects(() => store.enqueueMany(Array.from({ length: 501 }, () => batch[0])), /invalid agent run batch/u);
});

test('evaluation scheduling index scans every persisted run without exposing trace payloads', async () => {
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-index-')), 'runs.json');
  const store = new AgentRunStore(file);
  await store.initialize();
  const inputs = Array.from({ length: 601 }, (_, index) => ({
    runId: `shadow:index-${index}`, eventKey: `tenant-1:event-${index}`, tenantId: 'tenant-1', mode: 'shadow',
  }));
  await store.enqueueMany(inputs.slice(0, 500));
  await store.enqueueMany(inputs.slice(500));
  assert.equal((await store.list({ limit: 1_000 })).length, 500, 'operator list remains bounded');
  assert.equal((await store.listEvaluationIndex()).length, 601, 'scheduler index must scan the complete durable set');

  const claimed = await store.claimDue();
  await store.complete(claimed.run_id, claimed.lease_id, {
    runtime_version: 'v1', trace: [{ action: 'recognize_image' }], source_snapshot: { has_image: true },
  });
  const image = (await store.listEvaluationIndex()).find((item) => item.run_id === claimed.run_id);
  assert.equal(image.has_image, true);
  assert.equal(image.runtime_version, 'v1');
  assert.equal('result' in image, false);
  assert.equal('trace' in image, false);
  assert.equal('tool_calls' in image, false);
});

test('agent run deadline prevents stale queued work and supports an explicit in-flight timeout', async () => {
  let now = 10_000;
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-deadline-')), 'runs.json');
  const store = new AgentRunStore(file, { now: () => now });
  await store.initialize();
  await store.enqueue({ runId: 'shadow:expired', eventKey: 'tenant-1:expired', tenantId: 'tenant-1', mode: 'shadow', deadlineMs: 30_000 });
  now += 30_001;
  assert.equal(await store.claimDue(), null);
  assert.equal((await store.get('shadow:expired')).status, 'timed_out');

  await store.enqueue({ runId: 'shadow:running', eventKey: 'tenant-1:running', tenantId: 'tenant-1', mode: 'shadow', deadlineMs: 60_000 });
  const running = await store.claimDue();
  await store.timeout(running.run_id, running.lease_id, { status: 'handoff', reason: 'agent_deadline_exceeded' });
  const timedOut = await store.get(running.run_id);
  assert.equal(timedOut.status, 'timed_out');
  assert.equal(timedOut.result.reason, 'agent_deadline_exceeded');
});

test('agent tool journal replays completed observations and fails closed on an unknown pending result', async () => {
  let now = 50_000;
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-tools-')), 'runs.json');
  const store = new AgentRunStore(file, { now: () => now });
  await store.initialize();
  await store.enqueue({ runId: 'shadow:tools', eventKey: 'tenant-1:tools', tenantId: 'tenant-1', mode: 'shadow', deadlineMs: 600_000 });
  const first = await store.claimDue({ leaseMs: 60_000 });
  const started = await store.beginTool(first.run_id, first.lease_id, {
    callId: 'shadow:tools:1:recognize_image', step: 1, tool: 'recognize_image',
    trace: [{ step: 1, action: 'recognize_image', intent: '选座核价', confidence: 0.98 }], observations: [],
  });
  assert.equal(started.state, 'started');
  assert.equal((await store.beginTool(first.run_id, first.lease_id, {
    callId: 'shadow:tools:1:recognize_image', step: 1, tool: 'recognize_image', trace: [], observations: [],
  })).state, 'unknown');
  await store.completeTool(first.run_id, first.lease_id, 'shadow:tools:1:recognize_image', {
    status: 'success', tool: 'recognize_image', summary: '识图完成', facts: { cinema: '测试影院' }, next_actions: ['resolve_showtime'],
  });
  const replay = await store.beginTool(first.run_id, first.lease_id, {
    callId: 'shadow:tools:1:recognize_image', step: 1, tool: 'recognize_image', trace: [], observations: [],
  });
  assert.equal(replay.state, 'replay');
  assert.equal(replay.observation.facts.cinema, '测试影院');
  assert.deepEqual((await store.get(first.run_id)).observations.map((item) => item.tool), ['recognize_image']);

  await store.beginTool(first.run_id, first.lease_id, {
    callId: 'shadow:tools:2:quote_realtime', step: 2, tool: 'quote_realtime', trace: [], observations: [],
  });
  now += 60_001;
  const reclaimed = await store.claimDue({ leaseMs: 60_000 });
  const unknown = await store.beginTool(reclaimed.run_id, reclaimed.lease_id, {
    callId: 'shadow:tools:2:quote_realtime', step: 2, tool: 'quote_realtime', trace: [], observations: [],
  });
  assert.equal(unknown.state, 'unknown');
  assert.equal((await store.get(first.run_id)).tool_calls.length, 2);
});

test('pending write observations survive durable persistence without becoming errors', async () => {
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-pending-')), 'runs.json');
  const store = new AgentRunStore(file);
  await store.initialize();
  await store.enqueue({ runId: 'active:pending', eventKey: 'tenant-1:pending', tenantId: 'tenant-1', mode: 'active' });
  const run = await store.claimDue();
  await store.beginTool(run.run_id, run.lease_id, { callId: 'tool:1:change_order_price', step: 1, tool: 'change_order_price', trace: [], observations: [] });
  await store.completeTool(run.run_id, run.lease_id, 'tool:1:change_order_price', {
    status: 'pending', tool: 'change_order_price', code: 'price_change_submitted', summary: '等待平台确认', facts: {}, missing: [], retryable: false,
  });
  const persisted = await store.get(run.run_id);
  assert.equal(persisted.observations[0].status, 'pending');
  assert.equal(persisted.tool_calls[0].observation.status, 'pending');
});

test('release generation fences stale active runs before tool authorization', async () => {
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-generation-')), 'runs.json');
  const store = new AgentRunStore(file);
  await store.initialize();
  await store.enqueue({
    runId: 'active:generation', eventKey: 'tenant-1:generation', tenantId: 'tenant-1', mode: 'active',
    releaseId: 'release-7', releaseGeneration: 7,
  });
  assert.equal(await store.claimDue({ releaseGeneration: 8 }), null);
  const superseded = await store.get('active:generation');
  assert.equal(superseded.status, 'release_superseded');
  assert.equal(superseded.release_generation, 7);

  await store.enqueue({
    runId: 'active:tool-generation', eventKey: 'tenant-1:tool-generation', tenantId: 'tenant-1', mode: 'active',
    releaseId: 'release-8', releaseGeneration: 8,
  });
  const running = await store.claimDue({ releaseGeneration: 8 });
  const authorization = await store.beginTool(running.run_id, running.lease_id, {
    callId: 'call-1', step: 1, tool: 'change_order_price', trace: [], observations: [],
  }, { releaseGeneration: 9 });
  assert.equal(authorization.state, 'release_superseded');
  await store.supersede(running.run_id, running.lease_id);
  assert.equal((await store.get(running.run_id)).status, 'release_superseded');
});

test('agent run checkpoint rejects stale leases and stores only bounded observation fields', async () => {
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-runs-')), 'runs.json');
  const store = new AgentRunStore(file);
  await store.initialize();
  await store.enqueue({ runId: 'shadow:event-2', eventKey: 'tenant-1:event-2', tenantId: 'tenant-1', mode: 'shadow' });
  const run = await store.claimDue();
  await assert.rejects(() => store.checkpoint(run.run_id, 'stale', { trace: [], observations: [] }), /lease mismatch/u);
  await assert.rejects(() => store.checkpoint(run.run_id, run.lease_id, {
    trace: [], observations: Array.from({ length: 13 }, () => ({ status: 'success', tool: 'x', summary: '', facts: {}, next_actions: [] })),
  }), /too many observations/u);
});
