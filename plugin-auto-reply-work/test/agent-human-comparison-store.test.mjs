import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { AgentHumanComparisonStore } from '../src/agent/agent-human-comparison-store.mjs';

const input = {
  comparisonId: 'cmp-1', tenantId: '107', sourceEventId: 'event-1', sellerMessageId: 'seller-message-1',
  buyerTurn: { summary: '这场两张多少钱', has_image: true },
  conversationFacts: { cinema: '江桥万达', ticket_count: 2, stage: 'quoted', has_linked_order: false },
  quoteOrderState: { quote_status: 'valid', order_lifecycle: 'none' },
  agent: { intent: '核价', action: 'quote_realtime', proposed_reply: '实时价格需要工具结果。' },
  human: { reply: '请补一张完整选座图。', intent: '核价', expected_action: 'ask_for_image' },
  outcome: { stage: 'quoted', paid: false },
  automaticComparison: { intent_aligned: true, tool_aligned: false, facts_aligned: null, risk_aligned: true, reply_strategy_aligned: false, suggested_label: 'wrong_tool' },
};

test('human comparison is tenant isolated, idempotent, and review remains explicit', async () => {
  const store = new AgentHumanComparisonStore(join(await mkdtemp(join(tmpdir(), 'human-comparison-')), 'comparisons.json'), { now: () => 123 });
  assert.equal((await store.capture(input)).created, true);
  assert.equal((await store.capture({ ...input, outcome: { stage: 'paid', paid: true } })).created, false);
  assert.deepEqual(await store.list({ tenantId: 'other' }), []);
  const [record] = await store.list({ tenantId: '107' });
  assert.equal(record.outcome.stage, 'paid');
  assert.equal(record.review.status, 'unreviewed');
  assert.doesNotMatch(JSON.stringify(record), /seller-message-1/u);
  const reviewed = await store.review('107', record.id, { label: 'needs_policy', target: 'business_rule', note: '先形成规则草稿' });
  assert.deepEqual(reviewed.review, { status: 'reviewed', label: 'needs_policy', target: 'business_rule', note: '先形成规则草稿', reviewed_at: 123 });
  await assert.rejects(() => store.review('107', record.id, { label: 'aligned', target: 'training', note: '' }), /invalid human comparison review/u);
});
