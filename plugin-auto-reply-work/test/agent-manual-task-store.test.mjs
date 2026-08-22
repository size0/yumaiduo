import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { AgentManualTaskStore } from '../src/agent/agent-manual-task-store.mjs';

test('manual tasks are idempotent, bounded, and isolated by tenant', async () => {
  const store = new AgentManualTaskStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-manual-')), 'tasks.json'));
  await store.initialize();
  const input = {
    taskId: 'agent:tenant-1:event-1:manual', tenantId: 'tenant-1', eventId: 'event-1',
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', orderId: 'order-1',
    reasonCode: 'agent_requested_manual_review', summary: 'Agent请求人工处理', source: 'agent',
  };
  assert.equal((await store.create(input)).created, true);
  assert.equal((await store.create(input)).created, false);
  await store.create({ ...input, taskId: 'agent:tenant-2:event-2:manual', tenantId: 'tenant-2', eventId: 'event-2' });
  const tenantOne = await store.list({ tenantId: 'tenant-1' });
  assert.equal(tenantOne.length, 1);
  assert.equal(tenantOne[0].status, 'open');
  assert.equal(tenantOne[0].summary, 'Agent请求人工处理');
  assert.equal(JSON.stringify(tenantOne).includes('buyer-1'), true);
});

test('finds the latest manual task only for the exact tenant conversation', async () => {
  const store = new AgentManualTaskStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-manual-lookup-')), 'tasks.json'));
  await store.initialize();
  const base = {
    tenantId: 'tenant-1', accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1',
    orderId: '', reasonCode: 'manual_review', summary: '需要人工处理', source: 'agent',
  };
  await store.create({ ...base, taskId: 'task-old', eventId: 'event-old' });
  await store.create({ ...base, taskId: 'task-new', eventId: 'event-new' });
  await store.update('tenant-1', 'task-new', { status: 'in_progress' });
  await store.create({ ...base, taskId: 'other-buyer', eventId: 'event-other', peerUnb: 'buyer-2' });

  const latest = await store.findLatestForConversation('tenant-1', {
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1',
  });
  assert.equal(latest.task_id, 'task-new');
  assert.equal(latest.status, 'in_progress');
  assert.equal(await store.findLatestForConversation('tenant-1', {
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'missing',
  }), null);
  await assert.rejects(() => store.findLatestForConversation('', {}), /manual task conversation address/u);
});

test('manual tasks support assignment, priority, labels, SLA, notes, and lifecycle updates', async () => {
  let now = Date.parse('2026-08-22T00:00:00.000Z');
  const store = new AgentManualTaskStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-manual-')), 'tasks.json'), { now: () => now });
  await store.initialize();
  await store.create({
    taskId: 'agent:tenant-1:event-1:manual', tenantId: 'tenant-1', eventId: 'event-1',
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', reasonCode: 'agent_requested_manual_review', summary: '需要人工处理', source: 'agent',
    priority: 'high', labels: ['核价', '场次'], dueAt: '2026-08-22T00:10:00.000Z',
  });
  now += 1_000;
  const updated = await store.update('tenant-1', 'agent:tenant-1:event-1:manual', {
    status: 'in_progress', assignee: 'operator-1', priority: 'urgent', labels: ['核价', '紧急'], note: '已联系买家补充场次。',
  }, { actorId: 'operator-1' });
  assert.equal(updated.status, 'in_progress');
  assert.equal(updated.assignee, 'operator-1');
  assert.equal(updated.priority, 'urgent');
  assert.deepEqual(updated.labels, ['核价', '紧急']);
  assert.equal(updated.notes[0].author, 'operator-1');
  assert.equal(updated.notes[0].content, '已联系买家补充场次。');
  assert.equal(updated.sla_status, 'within_sla');

  now = Date.parse('2026-08-22T00:11:00.000Z');
  assert.equal((await store.get('tenant-1', updated.task_id)).sla_status, 'overdue');
  assert.equal((await store.list({ tenantId: 'tenant-1', status: 'in_progress', assignee: 'operator-1', priority: 'urgent', label: '紧急' })).length, 1);
  await assert.rejects(() => store.update('tenant-2', updated.task_id, { status: 'resolved' }), /manual task not found/u);
});

test('manual task store reads version one data without losing open tasks', async () => {
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-manual-v1-')), 'tasks.json');
  await writeFile(file, JSON.stringify({ version: 1, revision: 3, tasks: {
    old: { taskId: 'old', tenantId: 'tenant-1', eventId: 'event-1', accountUnb: 'shop', chatId: 'chat', peerUnb: 'buyer', orderId: '', reasonCode: 'legacy', summary: '旧任务', source: 'agent', status: 'open', createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z', resolvedAt: null },
  } }));
  const store = new AgentManualTaskStore(file);
  await store.initialize();
  const task = await store.get('tenant-1', 'old');
  assert.equal(task.priority, 'normal');
  assert.deepEqual(task.labels, []);
  assert.deepEqual(task.notes, []);
  await store.update('tenant-1', 'old', { status: 'in_progress' }, { actorId: 'operator-1' });
  assert.equal(JSON.parse(await readFile(file, 'utf8')).version, 2);
});

test('manual task resolution is tenant-scoped and idempotent', async () => {
  const store = new AgentManualTaskStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-manual-')), 'tasks.json'));
  await store.initialize();
  await store.create({
    taskId: 'agent:tenant-1:event-1:manual', tenantId: 'tenant-1', eventId: 'event-1',
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', reasonCode: 'agent_requested_manual_review', summary: '需要人工处理', source: 'agent',
  });
  await assert.rejects(() => store.resolve('tenant-2', 'agent:tenant-1:event-1:manual'), /manual task not found/u);
  assert.equal((await store.resolve('tenant-1', 'agent:tenant-1:event-1:manual')).status, 'resolved');
  assert.equal((await store.resolve('tenant-1', 'agent:tenant-1:event-1:manual')).status, 'resolved');
  assert.equal((await store.list({ tenantId: 'tenant-1', status: 'open' })).length, 0);
});
