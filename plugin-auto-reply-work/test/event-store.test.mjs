import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { FileEventStore } from '../src/event-store.mjs';

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'wanda-plugin-store-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const file = join(directory, 'events.json');
  const store = new FileEventStore(file);
  await store.initialize();
  return { file, store };
}

function envelope(id = 'evt-1') {
  return {
    id,
    tenantId: 'tenant-1',
    event: 'im.message.received',
    ts: Date.now(),
    payload: { chatId: 'chat-1', content: '两张' },
  };
}

test('idle claim attempts do not rewrite or increment the durable event store', async (t) => {
  const { file, store } = await fixture(t);
  const before = JSON.parse(await readFile(file, 'utf8'));
  assert.equal(await store.claimDue(), null);
  assert.equal(await store.claimDue(), null);
  const after = JSON.parse(await readFile(file, 'utf8'));
  assert.equal(after.revision, before.revision);
  assert.deepEqual(after, before);
});

test('concurrent cold gets share a safe store load and return stable records', async (t) => {
  const { file, store } = await fixture(t);
  await store.enqueue(envelope('evt-cold-read'));
  const cold = new FileEventStore(file);
  const records = await Promise.all(Array.from({ length: 200 }, () => cold.get('tenant-1:evt-cold-read')));
  assert.equal(records.length, 200);
  assert.ok(records.every((record) => record?.envelope?.id === 'evt-cold-read'));
  const selected = await cold.getMany(['tenant-1:evt-cold-read', 'tenant-1:missing', 'tenant-1:evt-cold-read']);
  assert.deepEqual(selected.map((record) => record.envelope.id), ['evt-cold-read']);
});

test('event envelope is durably enqueued once', async (t) => {
  const { file, store } = await fixture(t);
  const first = await store.enqueue(envelope());
  const replay = await store.enqueue(envelope());

  assert.equal(first.created, true);
  assert.equal(replay.created, false);
  const persisted = JSON.parse(await readFile(file, 'utf8'));
  assert.equal(Object.keys(persisted.events).length, 1);
});

test('the same platform messageId is idempotent across different event ids', async (t) => {
  const { file, store } = await fixture(t);
  const first = envelope('evt-message-1');
  first.payload.messageId = 'platform-message-1';
  const duplicate = envelope('evt-message-2');
  duplicate.payload.messageId = 'platform-message-1';
  assert.equal((await store.enqueue(first)).created, true);
  assert.equal((await store.enqueue(duplicate)).created, false);
  const persisted = JSON.parse(await readFile(file, 'utf8'));
  assert.equal(Object.keys(persisted.events).length, 1);
});

test('claimed event requires the matching lease to complete', async (t) => {
  const { store } = await fixture(t);
  await store.enqueue(envelope());
  const claimed = await store.claimDue();

  assert.equal(claimed.status, 'processing');
  await assert.rejects(
    store.complete(claimed.key, 'wrong-lease'),
    /lease mismatch/,
  );
  const completed = await store.complete(claimed.key, claimed.leaseId, { ok: true });
  assert.equal(completed.status, 'completed');
  assert.deepEqual(completed.result, { ok: true });
});

test('deferred event returns to the queue without consuming a retry', async (t) => {
  const { store } = await fixture(t);
  await store.enqueue(envelope(), { availableAt: 0 });
  const claimed = await store.claimDue();
  const deferred = await store.defer(claimed.key, claimed.leaseId, { delayMs: 10_000, reason: 'reply_delay' });
  assert.equal(deferred.status, 'queued');
  assert.equal(deferred.attempts, 1);
  assert.equal(deferred.result.deferred, 'reply_delay');
});

test('workers may claim different chats concurrently but never the same chat', async (t) => {
  const { store } = await fixture(t);
  const base = { accountUnb: 'shop-1', peerUnb: 'buyer-1' };
  await store.enqueue({ id: 'chat-a-1', tenantId: 'tenant-1', event: 'im.message.received', ts: 1, payload: { ...base, chatId: 'chat-a' } }, { availableAt: 0 });
  await store.enqueue({ id: 'chat-a-2', tenantId: 'tenant-1', event: 'im.message.received', ts: 2, payload: { ...base, chatId: 'chat-a' } }, { availableAt: 0 });
  await store.enqueue({ id: 'chat-b-1', tenantId: 'tenant-1', event: 'im.message.received', ts: 3, payload: { ...base, chatId: 'chat-b' } }, { availableAt: 0 });
  const first = await store.claimDue();
  const second = await store.claimDue();
  assert.equal(first.envelope.payload.chatId, 'chat-a');
  assert.equal(second.envelope.payload.chatId, 'chat-b');
});

test('expired processing lease can be reclaimed after restart-style timeout', async (t) => {
  const { store } = await fixture(t);
  await store.enqueue(envelope(), { availableAt: 0 });
  const first = await store.claimDue({ now: 100, leaseMs: 10 });
  const second = await store.claimDue({ now: 111, leaseMs: 10 });

  assert.equal(second.key, first.key);
  assert.notEqual(second.leaseId, first.leaseId);
  assert.equal(second.attempts, 2);
});

test('event payload is encrypted at rest while workers receive the original fields', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'wanda-plugin-encrypted-store-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const file = join(directory, 'events.json');
  const store = new FileEventStore(file, { encryptionKey: randomBytes(32) });
  await store.initialize();
  await store.enqueue(envelope('evt-encrypted'));

  const raw = await readFile(file, 'utf8');
  assert.doesNotMatch(raw, /chat-1|两张/u);
  const claimed = await store.claimDue();
  assert.equal(claimed.envelope.payload.chatId, 'chat-1');
  assert.equal(claimed.envelope.payload.content, '两张');
});

test('a newer quote message supersedes an in-flight receipt event when it returns to the merge window', async (t) => {
  const { store } = await fixture(t);
  const commonPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  await store.enqueue({ id: 'old-processing', tenantId: 'tenant-1', event: 'im.message.received', ts: 1, payload: commonPayload }, { availableAt: 0 });
  const claimed = await store.claimDue({ now: Date.now(), leaseMs: 60_000 });

  await store.cancelPendingChatMessages('tenant-1', 'shop-1:chat-1:buyer-1', 'new');
  const deferred = await store.defer(claimed.key, claimed.leaseId, {
    delayMs: 2_000,
    reason: 'buyer_message_merge_window',
    metadata: { actions: [{ action_id: 'old-processing:quote-processing-notice', status: 'succeeded', message_id: 'receipt-1' }] },
  });

  assert.equal(deferred.status, 'cancelled');
  assert.equal(deferred.result.skipped, 'superseded_by_newer_buyer_message');
  assert.equal(deferred.result.actions[0].message_id, 'receipt-1');
});

test('new buyer message cancels pending replies in the same chat merge window', async (t) => {
  const { store } = await fixture(t);
  const commonPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  await store.enqueue({ id: 'old', tenantId: 'tenant-1', event: 'im.message.received', ts: 1, payload: commonPayload }, { availableAt: Date.now() + 2_000 });
  await store.cancelPendingChatMessages('tenant-1', 'shop-1:chat-1:buyer-1', 'new');
  await store.enqueue({ id: 'new', tenantId: 'tenant-1', event: 'im.message.received', ts: 2, payload: commonPayload }, { availableAt: Date.now() - 1 });
  const claimed = await store.claimDue();
  assert.equal(claimed.envelope.id, 'new');
  const old = await store.get('tenant-1:old');
  assert.equal(old.status, 'cancelled');
});
