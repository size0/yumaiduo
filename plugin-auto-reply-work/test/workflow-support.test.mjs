import assert from 'node:assert/strict';
import test from 'node:test';

import {
  agentStateSnapshot,
  awaitWithDelayedNotice,
  boundedInteger,
  quoteCostEvidence,
  recentExplicitTicketCount,
} from '../src/workflow-support.mjs';

test('workflow support snapshots only bounded authoritative agent facts', () => {
  assert.deepEqual(agentStateSnapshot({
    stage: 'quoted', quote_total_cents: 11400, quote_ticket_count: 2,
    quote_reply_delivered: true, phone: '13800138000',
  }, 123), {
    observed_at: 123, quote_total_cents: 11400, quote_ticket_count: 2,
    stage: 'quoted', quote_reply_delivered: true,
  });
});

test('workflow support keeps bounded settings and explicit ticket count deterministic', () => {
  assert.equal(boundedInteger(5, 1, 10, 3), 5);
  assert.equal(boundedInteger(50, 1, 10, 3), 3);
  assert.equal(recentExplicitTicketCount([{ role: 'buyer', text: '需要2张' }]), 2);
});

test('workflow support reports delayed operations without changing their result', async () => {
  let notices = 0;
  const result = await awaitWithDelayedNotice(Promise.resolve('ok'), 100, async () => { notices += 1; });
  assert.equal(result, 'ok');
  assert.equal(notices, 0);
});

test('workflow support derives cost evidence only from integer cents', () => {
  assert.deepEqual(quoteCostEvidence({
    ticket_count: 2,
    seat_quotes: [
      { original_price_cents: 6000, member_price_cents: 5500, channel_fee_cents: 200 },
      { original_price_cents: 6000, member_price_cents: 5500, channel_fee_cents: 200 },
    ],
    pricing_source: 'wanda_realtime_member_offer',
  }), {
    memberCostTotalCents: 11000,
    channelFeeTotalCents: 400,
    originalPriceTotalCents: 12000,
    pricingSource: 'wanda_realtime_member_offer',
  });
});
