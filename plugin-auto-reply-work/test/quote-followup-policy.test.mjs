import assert from 'node:assert/strict';
import test from 'node:test';

import {
  completedUnitQuotePreview,
  createQuoteReplyAction,
  deliveredUnitQuoteQuestionPreview,
  duplicateQuoteClosureAction,
  hasActiveQuote,
  paymentSafeOrderInstruction,
  quoteSupersessionPreview,
} from '../src/quote/quote-followup-policy.mjs';

const now = Date.parse('2026-08-23T00:00:00Z');
const envelope = {
  id: 'event-1', tenantId: '107',
  payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '这个呢？' },
};

test('always appends the mandatory wait-for-price-change instruction once', () => {
  const required = '提交订单后请先不要付款，等待系统确认改价成功后再付款。';
  assert.equal(paymentSafeOrderInstruction('请提交订单。'), `请提交订单。\n${required}`);
  assert.equal(paymentSafeOrderInstruction(required), required);
});

test('detects only complete unexpired authoritative quotes', () => {
  assert.equal(hasActiveQuote({ quote_expires_at: now + 1, quote_total_cents: 5900, quote_ticket_count: 1 }, now), true);
  assert.equal(hasActiveQuote({ quote_expires_at: now - 1, quote_total_cents: 5900, quote_ticket_count: 1 }, now), false);
  assert.equal(hasActiveQuote({ quote_expires_at: now + 1, quote_ticket_count: 1 }, now), false);
});

test('replays a delivered unit quote question without creating a new quote attempt', () => {
  const preview = deliveredUnitQuoteQuestionPreview(envelope, { facts: {
    quote_unit_cents: 5300, quote_expires_at: now + 60_000, quote_reply_delivered: true,
  } }, now);
  assert.deepEqual(preview, {
    status: 'preview_ready', unit_quote_cents: 5300, unit_replay: true,
    reply_text: '当前有效报价为53.00元/张。请告诉我需要几张，我再核对合计。',
  });
});

test('completes a delivered unit quote from an explicit count using the existing rule version', () => {
  const preview = completedUnitQuotePreview(
    { ...envelope, payload: { ...envelope.payload, content: '2张' } },
    { facts: { quote_unit_cents: 5300, quote_expires_at: now + 60_000, quote_reply_delivered: true, pricing_rule_version: 'rule-v1', cinema: '测试万达' } },
    { max_auto_order_amount_cents: 20_000 },
    now,
  );
  assert.equal(preview.total_quote_cents, 10_600);
  assert.equal(preview.ticket_count, 2);
  assert.equal(preview.pricing_rule_version, 'rule-v1');
});

test('quote failure keeps the deterministic authoritative text instead of a stale configured template', () => {
  const action = createQuoteReplyAction(envelope, {
    status: 'quote_failed', failure_code: 'temporary_lock_release_unverified',
    reply_text: '座位释放状态未确认，本轮不能报价。',
  }, true, true, {
    reply_templates: { temporary_lock_release_unverified: '已转人工，价格是{价格}' },
  });
  assert.equal(action.text, '座位释放状态未确认，本轮不能报价。');
  assert.equal(action.action_id, 'event-1:quote-reply');
});

test('exact official seats can supersede a different active area quote with an explicit notice', () => {
  const next = quoteSupersessionPreview(
    { status: 'preview_ready', quote_scope: 'exact_seats', total_quote_cents: 10_000, reply_text: '新报价' },
    { facts: { quote_scope: 'area_probe', quote_total_cents: 9_000, quote_expires_at: now + 60_000 } },
    { ...envelope, payload: { ...envelope.payload, imageUrls: ['https://img.example/seat.jpg'] } },
    {},
    now,
  );
  assert.match(next.reply_text, /上一版未选座试价已失效/u);
  assert.match(next.reply_text, /新报价/u);
});

test('duplicate closure returns the current delivered quote instead of starting another probe', () => {
  const action = duplicateQuoteClosureAction(envelope, { facts: {
    quote_unit_cents: 5300, quote_total_cents: 10_600, quote_ticket_count: 2,
    quote_reply_delivered: true, quote_expires_at: now + 60_000,
  } }, now);
  assert.equal(action.action_id, 'event-1:duplicate-quote-closure');
  assert.match(action.text, /53\.00元\/张/u);
  assert.match(action.text, /2张合计106\.00元/u);
});
