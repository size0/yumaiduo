import assert from 'node:assert/strict';
import test from 'node:test';
import { createAgentHumanComparisonScanner } from '../src/agent/agent-human-comparison-scanner.mjs';

test('scanner captures a seller reply against the prior buyer turn and shadow plan', async () => {
  const captured = [];
  const scanner = createAgentHumanComparisonScanner({
    conversationContextStore: { async listRecent() { return [{ tenant_id: '107', account_unb: 'shop', chat_id: 'chat', peer_unb: 'buyer', facts: { cinema: '江桥万达', quote_ticket_count: 2, quote_unit_cents: 5_000, stage: 'quoted' }, messages: [] }]; } },
    eventStore: {
      async list() { return [{ key: '107:event-1', envelope: { tenantId: '107', id: 'event-1', payload: { accountUnb: 'shop', chatId: 'chat', peerUnb: 'buyer', messageId: 'buyer-message-1', content: '两张多少钱', imageUrls: ['https://img/a.jpg'] } } }]; },
      async wasSentMessage(_tenant, _chat, messageId) { return messageId === 'plugin-message'; },
    },
    agentRunStore: { async list() { return [{ event_key: '107:event-1', mode: 'shadow', result: { proposed_reply: '实时价格是50元。', trace: [{ intent: '核价', action: 'quote_realtime', goal: '获得权威报价' }] } }]; } },
    coreFor() { return { im: { async listMessages() { return { items: [
      { direction: 'outbound', messageId: 'seller-message-1', content: '请补一张完整选座图', sentAt: '2026-08-22T04:00:02Z' },
      { direction: 'inbound', messageId: 'buyer-message-1', content: '两张多少钱', sentAt: '2026-08-22T04:00:01Z' },
    ] }; } } }; },
    comparisonStore: { async capture(input) { captured.push(input); return { created: true }; } },
  });
  const result = await scanner.tick();
  assert.equal(result.captured, 1);
  assert.equal(captured[0].buyerTurn.summary, '两张多少钱');
  assert.equal(captured[0].human.expected_action, 'ask_for_image');
  assert.equal(captured[0].automaticComparison.suggested_label, 'wrong_tool');
  assert.equal(captured[0].agent.proposed_reply, '实时价格是50元。');
  assert.notEqual(captured[0].comparisonId, 'seller-message-1');
});
