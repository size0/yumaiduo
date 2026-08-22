import assert from 'node:assert/strict';
import { access, mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { FileEventStore } from '../src/event-store.mjs';
import { ConversationContextStore } from '../src/conversation-context-store.mjs';
import { AgentEvaluationStore } from '../src/agent-evaluation-store.mjs';
import { AgentRunStore } from '../src/agent/agent-run-store.mjs';
import { AgentReplyOutboxStore } from '../src/agent/agent-reply-outbox-store.mjs';
import { AgentManualTaskStore } from '../src/agent/agent-manual-task-store.mjs';
import { AgentHumanComparisonStore } from '../src/agent/agent-human-comparison-store.mjs';
import { createStorageBundle } from '../src/bootstrap/create-storage-bundle.mjs';

test('constructs the complete isolated storage bundle', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-storage-bundle-'));
  const bundle = createStorageBundle({
    dataDir,
    configEncryptionKey: Buffer.alloc(32, 7),
    eventRetentionDays: 14,
  });
  assert.equal(bundle.eventStore instanceof FileEventStore, true);
  assert.equal(bundle.conversationContextStore instanceof ConversationContextStore, true);
  assert.equal(bundle.agentEvaluationStore instanceof AgentEvaluationStore, true);
  assert.equal(bundle.agentRunStore instanceof AgentRunStore, true);
  assert.equal(bundle.agentReplyOutboxStore instanceof AgentReplyOutboxStore, true);
  assert.equal(bundle.agentManualTaskStore instanceof AgentManualTaskStore, true);
  assert.equal(bundle.agentHumanComparisonStore instanceof AgentHumanComparisonStore, true);
  assert.equal(Object.isFrozen(bundle), true);
});

test('initializes only the same eager durable queues as the previous application lifecycle', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-storage-init-'));
  const bundle = createStorageBundle({ dataDir, configEncryptionKey: Buffer.alloc(32, 8), eventRetentionDays: 30 });
  await bundle.initialize();

  for (const file of ['events.json', 'agent-runs.json', 'agent-reply-outbox.json', 'agent-manual-tasks.json']) {
    await access(join(dataDir, file));
  }
  for (const file of ['conversation-context.json', 'agent-evaluations.json', 'agent-human-comparisons.json']) {
    await assert.rejects(() => access(join(dataDir, file)), (error) => error?.code === 'ENOENT');
  }
});

test('passes the encryption key into the event store without exposing it in the bundle', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-storage-encrypted-'));
  const bundle = createStorageBundle({ dataDir, configEncryptionKey: Buffer.alloc(32, 9), eventRetentionDays: 30 });
  await bundle.initialize();
  await bundle.eventStore.enqueue({
    id: 'event-1', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '敏感买家消息' },
  });
  const stored = await readFile(join(dataDir, 'events.json'), 'utf8');
  assert.doesNotMatch(stored, /敏感买家消息|buyer-1/u);
  assert.equal('configEncryptionKey' in bundle, false);
});

test('fails closed for an invalid storage configuration', () => {
  assert.throws(() => createStorageBundle({ dataDir: '', eventRetentionDays: 30 }), /storage dataDir is required/u);
  assert.throws(() => createStorageBundle({ dataDir: '/tmp/test', eventRetentionDays: 0 }), /eventRetentionDays/u);
});
