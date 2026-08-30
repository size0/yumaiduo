import assert from 'node:assert/strict';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { V2EventStore } from '../src/runtime/event-store.mjs';

test('queued image messages are never superseded by later images or buyer text', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-image-queue-'));
  const store = new V2EventStore(join(dataDir, 'events.v2.json'), Buffer.alloc(32, 8));
  await store.initialize();
  const event = (id, { image = false, content = '' } = {}) => ({
    id, tenantId: 'tenant-1', event: 'im.message.received', timestamp: Date.now(),
    payload: {
      accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1',
      messageType: image ? 2 : 1, content,
      imageUrls: image ? [`https://img.alicdn.com/${id}.jpg`] : [],
    },
  });

  await store.enqueue(event('image-1', { image: true }));
  await store.enqueue(event('image-2', { image: true }));
  await store.enqueue(event('text-1', { content: '你好' }));

  const claimed = [];
  for (let index = 0; index < 3; index += 1) {
    const record = await store.claim(new Set());
    assert.ok(record);
    claimed.push(record.envelope.id);
    await store.complete(record.id, record.lease, { ok: true });
  }
  assert.deepEqual(claimed, ['image-1', 'image-2', 'text-1']);
});

test('rapid buyer text messages remain queued so facts are never overwritten', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-text-queue-'));
  const store = new V2EventStore(join(dataDir, 'events.v2.json'), Buffer.alloc(32, 11));
  await store.initialize();
  const event = (id, content) => ({
    id, tenantId: 'tenant-1', event: 'im.message.received', timestamp: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', messageType: 1, content, imageUrls: [] },
  });

  await store.enqueue(event('text-old', '你好'));
  await store.enqueue(event('text-new', '请报价'));
  const first = await store.claim(new Set());
  assert.equal(first.envelope.id, 'text-old');
  await store.complete(first.id, first.lease, { ok: true });
  const second = await store.claim(new Set());

  assert.equal(second.envelope.id, 'text-new');
  assert.equal((await store.health()).counts.cancelled ?? 0, 0);
});

test('observed order references are deduplicated and tenant scoped', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-order-references-'));
  const store = new V2EventStore(join(dataDir, 'events.v2.json'), Buffer.alloc(32, 12));
  await store.initialize();
  const event = (id, tenantId, orderId) => ({
    id, tenantId, event: 'order.created', timestamp: Date.now(),
    payload: { orderId, accountUnb: `shop-${tenantId}` },
  });
  await store.enqueue(event('a-1', 'tenant-a', 'order-1'));
  await store.enqueue(event('a-2', 'tenant-a', 'order-1'));
  await store.enqueue(event('a-3', 'tenant-a', 'order-2'));
  await store.enqueue(event('b-1', 'tenant-b', 'order-secret'));

  const references = await store.listOrderReferences('tenant-a', 10);

  assert.deepEqual(new Set(references.map((item) => item.orderId)), new Set(['order-1', 'order-2']));
  assert.equal(JSON.stringify(references).includes('order-secret'), false);
});

test('Wanda fulfillment facts are encrypted, unique by tenant and order, and reject identity conflicts', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-fulfillment-'));
  const path = join(dataDir, 'events.v2.json');
  const key = Buffer.alloc(32, 14);
  const store = new V2EventStore(path, key);
  await store.initialize();
  const record = {
    tenant_id: 'tenant-a', order_id: 'order-1', shop_id: 'shop-1',
    buyer_id: 'buyer-1', chat_id: 'chat-1', status: 'processing',
    request_fingerprint: 'fingerprint-1', movie_name: '奥德赛', showtime_start: '20:10',
    ticket_codes: ['SECRET-CODE'], message_text: '取票码：SECRET-CODE',
  };
  const first = await store.saveWandaFulfillment(record);
  assert.equal(first.created, true);
  assert.deepEqual(await store.getWandaFulfillment('tenant-a', 'order-1'), record);
  const repeated = await store.saveWandaFulfillment({ ...record, status: 'submitted' });
  assert.equal(repeated.created, false);
  assert.equal(repeated.record.status, 'submitted');
  await assert.rejects(
    store.saveWandaFulfillment({ ...record, request_fingerprint: 'other', status: 'processing' }),
    /wanda_fulfillment_conflict/u,
  );
  const reloaded = new V2EventStore(path, key);
  await reloaded.initialize();
  assert.equal((await reloaded.getWandaFulfillment('tenant-a', 'order-1')).movie_name, '奥德赛');
  assert.equal((await readFile(path, 'utf8')).includes('SECRET-CODE'), false);
});

test('keyword image CDN upload cache persists encrypted and remains tenant scoped', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-image-upload-cache-'));
  const path = join(dataDir, 'events.v2.json');
  const key = Buffer.alloc(32, 13);
  const store = new V2EventStore(path, key);
  await store.initialize();
  await store.saveKeywordImageUpload({
    tenantId: 'tenant-a', accountUnb: 'shop-a', assetId: `ki-${'a'.repeat(40)}`,
    imageUrl: 'https://img.alicdn.com/cached-keyword.png', width: 320, height: 180,
    sha256: 'hash-a', expiresAt: new Date(Date.now() + 60_000).toISOString(),
  });

  const reloaded = new V2EventStore(path, key);
  await reloaded.initialize();
  const cached = await reloaded.getKeywordImageUpload({
    tenantId: 'tenant-a', accountUnb: 'shop-a', assetId: `ki-${'a'.repeat(40)}`,
  });
  const denied = await reloaded.getKeywordImageUpload({
    tenantId: 'tenant-b', accountUnb: 'shop-a', assetId: `ki-${'a'.repeat(40)}`,
  });
  const raw = await readFile(path, 'utf8');

  assert.equal(cached.imageUrl, 'https://img.alicdn.com/cached-keyword.png');
  assert.equal(denied, null);
  assert.equal(raw.includes('cached-keyword.png'), false);
});

test('price change receipt claims are atomic for one executor idempotency key', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-receipt-'));
  const store = new V2EventStore(join(dataDir, 'events.v2.json'), Buffer.alloc(32, 9));
  await store.initialize();
  const receipts = store.createPriceChangeReceiptStore();
  const initial = {
    idempotency_key: 'price_change:v1:atomic-test',
    status: 'started',
    phase: 'claimed',
    quote_snapshot: { quote_version: 'quote-sensitive-marker' },
  };

  const claims = await Promise.all(Array.from({ length: 8 }, () => receipts.claim(initial)));
  assert.equal(claims.filter((claim) => claim.created).length, 1);
  assert.equal(claims.every((claim) => claim.receipt.idempotency_key === initial.idempotency_key), true);
});

test('price change receipts persist encrypted updates across store restarts', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-receipt-restart-'));
  const statePath = join(dataDir, 'events.v2.json');
  const key = Buffer.alloc(32, 10);
  const first = new V2EventStore(statePath, key);
  await first.initialize();
  const firstReceipts = first.createPriceChangeReceiptStore();
  const initial = {
    idempotency_key: 'price_change:v1:restart-test',
    status: 'started',
    phase: 'claimed',
    quote_snapshot: { quote_version: 'quote-sensitive-marker' },
  };
  const claim = await firstReceipts.claim(initial);
  claim.receipt.status = 'unknown';
  claim.receipt.phase = 'finished';
  claim.receipt.result = { status: 'unknown', reason_code: 'platform_result_unknown_after_readback' };
  await firstReceipts.save(claim.receipt);

  const serialized = await readFile(statePath, 'utf8');
  assert.equal(serialized.includes('quote-sensitive-marker'), false);
  assert.equal(serialized.includes('platform_result_unknown_after_readback'), false);

  const restarted = new V2EventStore(statePath, key);
  await restarted.initialize();
  const duplicate = await restarted.createPriceChangeReceiptStore().claim(initial);
  assert.equal(duplicate.created, false);
  assert.equal(duplicate.receipt.status, 'unknown');
  assert.equal(duplicate.receipt.result.reason_code, 'platform_result_unknown_after_readback');
});
