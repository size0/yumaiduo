import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { AgentReplyOutboxStore } from '../src/agent/agent-reply-outbox-store.mjs';

test('active reply outbox is idempotent and records the platform message id', async () => {
  let now = 1_000;
  const store = new AgentReplyOutboxStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-outbox-')), 'outbox.json'), { now: () => now });
  await store.initialize();
  const input = {
    actionId: 'active:tenant-1:event-1:reply', runId: 'active:tenant-1:event-1', tenantId: 'tenant-1',
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', sourceMessageId: 'buyer-source-1', text: '请发送完整选座截图。', mode: 'active',
  };
  assert.equal((await store.enqueue(input)).created, true);
  assert.equal((await store.enqueue(input)).created, false);
  const claimed = await store.claimDue({ leaseMs: 30_000 });
  assert.equal(claimed.status, 'sending');
  assert.equal(claimed.source_message_id, 'buyer-source-1');
  await store.markSent(claimed.action_id, claimed.lease_id, 'platform-message-1');
  const sent = await store.get(input.actionId);
  assert.equal(sent.status, 'sent');
  assert.equal(sent.platform_message_id, 'platform-message-1');
  assert.equal((await store.health()).counts.sent, 1);
});

test('quote reply persists delivery metadata and commits after the platform send without resending', async () => {
  let now = 2_000;
  const store = new AgentReplyOutboxStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-quote-outbox-')), 'outbox.json'), { now: () => now });
  await store.initialize();
  await store.enqueue({
    actionId: 'active:quote:reply', runId: 'active:quote', tenantId: 'tenant-1', accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', text: '权威报价', mode: 'active',
    delivery: { type: 'quote', unit_quote_cents: 5000, total_quote_cents: 10000, ticket_count: 2, pricing_rule_version: 'quote-policy-test', pricing_account_ref: 'a'.repeat(32), cinema: '测试万达', channel_fee_total_cents: 300, circled_delivery_image_url: 'https://img.alicdn.com/circled.png' },
  });
  const sending = await store.claimDue();
  const delivered = await store.markSent(sending.action_id, sending.lease_id, 'platform-quote-1');
  assert.equal(delivered.status, 'sent_pending_commit');
  assert.equal(delivered.delivery.type, 'quote');
  assert.equal(delivered.delivery.channel_fee_total_cents, 300);
  assert.equal(delivered.delivery.pricing_account_ref, 'a'.repeat(32));
  assert.equal(delivered.delivery.circled_delivery_image_url, 'https://img.alicdn.com/circled.png');
  const committing = await store.claimDue();
  assert.equal(committing.status, 'committing');
  await store.deferCommit(committing.action_id, committing.lease_id, 'context_temporarily_unavailable', { delayMs: 1_000 });
  now += 1_001;
  const retried = await store.claimDue();
  assert.equal(retried.status, 'committing');
  await store.markCommitted(retried.action_id, retried.lease_id);
  assert.equal((await store.get(retried.action_id)).status, 'sent');
});

test('reply outbox can close a human-takeover item without sending it again', async () => {
  const store = new AgentReplyOutboxStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-outbox-')), 'outbox.json'));
  await store.initialize();
  await store.enqueue({
    actionId: 'active:skipped', runId: 'active:run', tenantId: 'tenant-1', accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', text: '安全回复', mode: 'active',
  });
  const claimed = await store.claimDue();
  await store.markSkipped(claimed.action_id, claimed.lease_id, 'human_takeover');
  const skipped = await store.get(claimed.action_id);
  assert.equal(skipped.status, 'skipped');
  assert.equal(skipped.last_error, 'human_takeover');
});

test('reply outbox never claims a reply after the shared agent deadline', async () => {
  let now = 5_000;
  const store = new AgentReplyOutboxStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-expired-outbox-')), 'outbox.json'), { now: () => now });
  await store.initialize();
  await store.enqueue({ actionId: 'active:expired', runId: 'active:run', tenantId: 'tenant-1', accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', sourceMessageId: 'm1', projectionVersion: 'p1', replyProvenance: { model: 'm' }, expiresAt: 5_100, text: '不得发送', mode: 'active' });
  now = 5_101;
  assert.equal(await store.claimDue(), null);
  assert.equal((await store.get('active:expired')).last_error, 'agent_deadline_exceeded');
});

test('release generation fences queued active replies before claim', async () => {
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-agent-generation-outbox-')), 'outbox.json');
  const store = new AgentReplyOutboxStore(file);
  await store.initialize();
  await store.enqueue({
    actionId: 'reply-generation-1', runId: 'active:generation', tenantId: 'tenant-1', mode: 'active',
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', text: '不会发送',
    releaseId: 'release-7', releaseGeneration: 7,
  });
  assert.equal(await store.claimDue({ releaseGeneration: 8 }), null);
  const entry = await store.get('reply-generation-1');
  assert.equal(entry.status, 'release_superseded');
  assert.equal(entry.release_generation, 7);
});

test('reply outbox rejects shadow writes and never blindly retries an unknown send result', async () => {
  let now = 10_000;
  const store = new AgentReplyOutboxStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-outbox-')), 'outbox.json'), { now: () => now });
  await store.initialize();
  await assert.rejects(() => store.enqueue({
    actionId: 'shadow:reply', runId: 'shadow:run', tenantId: 'tenant-1', accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', text: '不能发送', mode: 'shadow',
  }), /active agent runs/u);
  await store.enqueue({
    actionId: 'active:reply', runId: 'active:run', tenantId: 'tenant-1', accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', text: '安全回复', mode: 'active',
  });
  const claimed = await store.claimDue({ leaseMs: 30_000 });
  now += 30_001;
  assert.equal(await store.claimDue({ leaseMs: 30_000 }), null);
  const unknown = await store.get(claimed.action_id);
  assert.equal(unknown.status, 'unknown');
  assert.equal(unknown.last_error, 'send_result_unknown');
});
