import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { AgentReplyOutboxStore } from '../src/agent/agent-reply-outbox-store.mjs';
import { createAgentReplyOutboxDispatcher } from '../src/agent/agent-reply-outbox-dispatcher.mjs';

async function fixture() {
  const store = new AgentReplyOutboxStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-dispatch-')), 'outbox.json'));
  await store.initialize();
  await store.enqueue({
    actionId: 'active:tenant-1:event-1:reply', runId: 'active:tenant-1:event-1', tenantId: 'tenant-1', mode: 'active',
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', text: '请发送完整选座截图。',
  });
  return store;
}

test('reply outbox dispatcher sends one claimed active reply and records its platform id', async () => {
  const store = await fixture();
  const actions = [];
  const dispatcher = createAgentReplyOutboxDispatcher({
    store,
    async executeReply(action) { actions.push(action); return { status: 'succeeded', message_id: 'platform-1' }; },
  });
  const result = await dispatcher.tick();
  assert.equal(result.status, 'sent');
  assert.equal(actions.length, 1);
  assert.equal(actions[0].kind, 'reply');
  assert.equal(actions[0].action_id, 'active:tenant-1:event-1:reply');
  assert.equal((await store.get(actions[0].action_id)).status, 'sent');
  assert.equal(await dispatcher.tick(), null);
});

test('quote outbox commits authoritative quote state only after a successful platform send', async () => {
  const store = new AgentReplyOutboxStore(join(await mkdtemp(join(tmpdir(), 'wanda-agent-quote-dispatch-')), 'outbox.json'));
  await store.initialize();
  await store.enqueue({
    actionId: 'active:quote:reply', runId: 'active:quote', tenantId: 'tenant-1', mode: 'active', accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', text: '权威报价',
    delivery: { type: 'quote', unit_quote_cents: 5000, total_quote_cents: 10000, ticket_count: 2, pricing_rule_version: 'quote-policy-test', cinema: '测试万达' },
  });
  let sends = 0; const commits = [];
  const dispatcher = createAgentReplyOutboxDispatcher({
    store,
    async executeReply() { sends += 1; return { status: 'succeeded', message_id: 'platform-quote-1' }; },
    async commitDelivery(entry) { commits.push(entry); },
  });
  assert.equal((await dispatcher.tick()).status, 'sent_pending_commit');
  assert.equal(sends, 1); assert.equal(commits.length, 0);
  assert.equal((await dispatcher.tick()).status, 'sent');
  assert.equal(sends, 1); assert.equal(commits.length, 1);
  assert.equal(commits[0].platform_message_id, 'platform-quote-1');
  assert.equal(commits[0].delivery.total_quote_cents, 10000);
});

test('reply outbox dispatcher preserves human takeover and fails closed on an unknown send result', async () => {
  const takeoverStore = await fixture();
  const takeover = createAgentReplyOutboxDispatcher({
    store: takeoverStore,
    async executeReply() { return { status: 'skipped', reason: 'human_takeover' }; },
  });
  assert.equal((await takeover.tick()).status, 'skipped');
  assert.equal((await takeoverStore.get('active:tenant-1:event-1:reply')).status, 'skipped');

  const unknownStore = await fixture();
  const unknown = createAgentReplyOutboxDispatcher({
    store: unknownStore,
    async executeReply() { throw new TypeError('network result unknown'); },
    logger: { warn() {} },
  });
  assert.equal((await unknown.tick()).status, 'unknown');
  const entry = await unknownStore.get('active:tenant-1:event-1:reply');
  assert.equal(entry.status, 'unknown');
  assert.equal(entry.last_error, 'send_result_unknown');
});
