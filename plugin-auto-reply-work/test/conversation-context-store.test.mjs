import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { ConversationContextStore } from '../src/conversation-context-store.mjs';

const message = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };

test('conversation contexts expose bounded recent addresses for independent supervision scans', async () => {
  let now = 2_000;
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-list-')), 'context.json'), { now: () => now });
  await store.add('tenant-1', { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '第一条' }, 1_000);
  now = 3_000;
  await store.add('tenant-2', { accountUnb: 'shop-2', chatId: 'chat-2', peerUnb: 'buyer-2', content: '第二条' }, 3_000);
  assert.deepEqual(await store.listRecent({ tenantId: 'tenant-1', limit: 5 }), [{ tenant_id: 'tenant-1', account_unb: 'shop-1', chat_id: 'chat-1', peer_unb: 'buyer-1', last_message_at: 1_000, facts: { first_seen_at: 1_000 }, messages: [{ at: 1_000, role: 'buyer', text: '第一条', image: false, image_urls: [] }] }]);
});

test('first-contact notice is claimable once only for a newly seen conversation', async () => {
  let now = 1_000;
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-first-contact-')), 'context.json');
  const store = new ConversationContextStore(file, { now: () => now });
  await store.add('tenant-1', { ...message, content: '首次进线' }, now);
  assert.equal(await store.claimFirstContactNotice('tenant-1', message), true);
  assert.equal(await store.claimFirstContactNotice('tenant-1', message), false);
  assert.equal(await store.releaseFirstContactNotice('tenant-1', message), true);
  assert.equal(await store.claimFirstContactNotice('tenant-1', message), true);

  const later = { ...message, peerUnb: 'buyer-late' };
  await store.add('tenant-1', later, now);
  now += 2 * 60 * 1_000 + 1;
  assert.equal(await store.claimFirstContactNotice('tenant-1', later), false);
});

test('quote processing receipt has an independent one-time first-conversation claim', async () => {
  let now = 1_000;
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-quote-receipt-')), 'context.json');
  const store = new ConversationContextStore(file, { now: () => now });
  await store.add('tenant-1', { ...message, content: '您好' }, now);
  assert.equal(await store.claimFirstContactNotice('tenant-1', message), true);
  assert.equal(await store.claimQuoteProcessingReceipt('tenant-1', message), true);
  assert.equal(await store.claimQuoteProcessingReceipt('tenant-1', message), false);
  assert.equal(await store.releaseQuoteProcessingReceipt('tenant-1', message), true);
  assert.equal(await store.claimQuoteProcessingReceipt('tenant-1', message), true);

  const later = { ...message, peerUnb: 'buyer-late-receipt' };
  await store.add('tenant-1', later, now);
  now += 2 * 60 * 1_000 + 1;
  assert.equal(await store.claimQuoteProcessingReceipt('tenant-1', later), false);
});

test('typed same-row seat preferences retain every explicitly written seat', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-seat-preferences-')), 'context.json'));
  await store.add('tenant-1', { ...message, content: '那我这边看错了，是8排10和11座，谢谢' });
  assert.deepEqual((await store.get('tenant-1', message)).facts.seats, ['8排10座', '8排11座']);

  const spaced = { ...message, peerUnb: 'buyer-spaced' };
  await store.add('tenant-1', { ...spaced, content: '8排13 14' });
  assert.deepEqual((await store.get('tenant-1', spaced)).facts.seats, ['8排13座', '8排14座']);

  const repeatedRow = { ...message, peerUnb: 'buyer-repeated-row' };
  await store.add('tenant-1', { ...repeatedRow, content: '最中间的是7排9座，7排10座吧' });
  assert.deepEqual((await store.get('tenant-1', repeatedRow)).facts.seats, ['7排9座', '7排10座']);
});


test('purchase information confirmation is bound to the prompted image only', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-purchase-confirmation-')), 'context.json'));
  const firstImage = 'https://img.alicdn.com/first.png';
  const secondImage = 'https://img.alicdn.com/second.png';

  assert.equal(await store.claimConfirmation('tenant-1', message, firstImage), true);
  assert.equal(await store.confirmPurchaseInfo('tenant-1', message, firstImage), true);
  assert.equal((await store.get('tenant-1', message)).facts.purchase_info_confirmed_image_url, firstImage);
  assert.equal(await store.claimConfirmation('tenant-1', message, secondImage), true);
  assert.equal((await store.get('tenant-1', message)).facts.purchase_info_confirmed_image_url, undefined);
  assert.equal(await store.confirmPurchaseInfo('tenant-1', message, firstImage), false);
});

test('a circled image is stored as an exact fulfillment instruction without extracting seat numbers', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-circled-instruction-')), 'context.json'));
  const imageUrl = 'https://img.alicdn.com/buyer-circle.png';
  await store.recordCircledDeliveryInstruction('tenant-1', message, imageUrl);
  await store.setOrderStage('tenant-1', message, 'paid_manual_delivery', 'order-circle');
  const facts = (await store.get('tenant-1', message)).facts;
  assert.equal(facts.seat_delivery_instruction, 'buyer_circled_image');
  assert.equal(facts.seat_delivery_instruction_image_url, imageUrl);
  assert.equal(facts.seats, undefined);
  const [order] = await store.listTicketOrders('tenant-1');
  assert.equal(order.seat_delivery_instruction, '按买家原图圈选位置出票');
  assert.equal(order.seat_delivery_image_recorded, true);
});

test('one failed durable write does not poison all later conversation updates', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'wanda-context-recovery-'));
  const file = join(directory, 'context.json');
  await mkdir(file);
  const store = new ConversationContextStore(file);
  await assert.rejects(() => store.add('tenant-1', { ...message, content: 'first' }));
  await rm(file, { recursive: true, force: true });
  await store.add('tenant-1', { ...message, content: 'second' });
  assert.equal((await store.get('tenant-1', message)).messages.at(-1).text, 'second');
});

test('quote state expires and a paid order becomes manual-delivery only', async () => {
  let now = 1_000;
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'), { now: () => now });
  await store.markQuoted('tenant-1', message, { validForMs: 10_000 });
  assert.equal((await store.get('tenant-1', message)).facts.stage, 'quoted');
  now += 10_001;
  assert.equal(await store.bindOrder('tenant-1', message, 'order-1'), false);
  assert.equal((await store.get('tenant-1', message)).facts.stage, 'quote_expired');
  await store.setOrderStage('tenant-1', message, 'paid_manual_delivery', 'order-1');
  assert.equal((await store.get('tenant-1', message)).facts.stage, 'paid_manual_delivery');
});

test('binding an order preserves the quote stage until a verified price-changed event', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  await store.markQuoted('tenant-1', message, { ticketCount: 2, totalQuoteCents: 12000, pricingRuleVersion: 'policy-v1', replyDelivered: true });
  await store.markQuoteConfirmed('tenant-1', message);
  assert.equal(await store.bindOrder('tenant-1', message, 'order-1', { quantity: 1, total_fee: 2000 }), true);
  const facts = (await store.get('tenant-1', message)).facts;
  assert.equal(facts.stage, 'quote_confirmed');
  assert.equal(facts.order_id, 'order-1');
});

test('a replacement exact-seat quote invalidates confirmation for the previous area quote', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  await store.markQuoted('tenant-1', message, { ticketCount: 2, totalQuoteCents: 7400, quoteScope: 'area_probe', pricingRuleVersion: 'policy-v1', replyDelivered: true });
  await store.markQuoteConfirmed('tenant-1', message);
  await store.markQuoted('tenant-1', message, { ticketCount: 2, totalQuoteCents: 8200, quoteScope: 'exact_seats' });
  const facts = (await store.get('tenant-1', message)).facts;
  assert.equal(facts.quote_scope, 'exact_seats');
  assert.equal(facts.quote_total_cents, 8200);
  assert.equal(facts.stage, 'quoted');
  assert.equal(facts.quote_confirmed, false);
});

test('quote confirmation requires amount, count, policy version, delivery, and a live expiry', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  await store.markQuoted('tenant-1', message, { ticketCount: 2, totalQuoteCents: 7400, pricingRuleVersion: 'policy-v1', replyDelivered: false });
  assert.equal(await store.markQuoteConfirmed('tenant-1', message), false);
  await store.markQuoted('tenant-1', message, { ticketCount: 2, totalQuoteCents: 7400, pricingRuleVersion: 'policy-v1', replyDelivered: true });
  assert.equal(await store.markQuoteConfirmed('tenant-1', message), true);
  const facts = (await store.get('tenant-1', message)).facts;
  assert.equal(facts.pricing_rule_version, 'policy-v1');
  assert.equal(facts.quote_reply_delivered, true);
});

test('conversation agent persists only bounded workflow state without model prose', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  await store.recordAgentTurn('tenant-1', message, {
    status: 'reply', intent: '补充信息', confidence: 0.93, goal: '补充影院信息', action: 'ask_for_city', missingFields: ['城市'],
    reply: '模型原始回复不得持久化', reason: '模型原始原因不得持久化',
  });
  const facts = (await store.get('tenant-1', message)).facts;
  assert.equal(facts.agent_intent, '补充信息');
  assert.equal(facts.agent_confidence, 0.93);
  assert.equal(facts.agent_goal, '补充影院信息');
  assert.equal(facts.agent_pending_action, 'ask_for_city');
  assert.deepEqual(facts.agent_missing_fields, ['城市']);
  assert.equal(facts.agent_last_status, 'reply');
  assert.equal(facts.reply, undefined);
  assert.equal(facts.reason, undefined);
});

test('order exceptions persist a bounded reason in the manual-review stage', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  await store.markOrderException('tenant-1', message, 'paid_amount_mismatch', 'order-1');
  const facts = (await store.get('tenant-1', message)).facts;
  assert.equal(facts.stage, 'exception_review');
  assert.equal(facts.exception_reason, 'paid_amount_mismatch');
  assert.equal(facts.order_id, 'order-1');
});

test('a delayed order-created event cannot regress a paid conversation back to waiting payment', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  await store.markQuoted('tenant-1', message);
  await store.setOrderStage('tenant-1', message, 'paid_manual_delivery', 'order-paid');
  assert.equal(await store.bindOrder('tenant-1', message, 'order-paid'), true);
  assert.equal((await store.get('tenant-1', message)).facts.stage, 'paid_manual_delivery');
});

test('agent quote delivery evidence is idempotent for one outbox action', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  const quote = { ticketCount: 2, unitQuoteCents: 5000, totalQuoteCents: 10000, cinema: '测试万达', pricingRuleVersion: 'quote-policy-test', replyDelivered: true, deliveryActionId: 'active:quote:reply', platformMessageId: 'platform-quote-1', circledDeliveryImageUrl: 'https://img.alicdn.com/circled.png' };
  await store.markQuoted('tenant-1', message, quote);
  await store.markQuoted('tenant-1', message, quote);
  const facts = (await store.get('tenant-1', message)).facts;
  assert.equal(facts.quote_history.length, 1);
  assert.equal(facts.quote_delivery_action_id, 'active:quote:reply');
  assert.equal(facts.quote_reply_platform_message_id, 'platform-quote-1');
  assert.equal(facts.quote_reply_delivered, true);
  assert.equal(facts.seat_delivery_instruction, 'buyer_circled_image');
  assert.equal(facts.seat_delivery_instruction_image_url, 'https://img.alicdn.com/circled.png');
});

test('operations queue exposes the next action and verified quote facts for an active conversation', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  await store.add('tenant-1', { ...message, peerNick: '万达影迷小王' });
  await store.markQuoted('tenant-1', message, { ticketCount: 2, unitQuoteCents: 6000, totalQuoteCents: 12000, cinema: '测试万达广场店', quoteScope: 'exact_seats' });
  const [item] = await store.listOperations('tenant-1');
  assert.deepEqual(item, {
    account_unb: 'shop-1', chat_id: 'chat-1', peer_unb: 'buyer-1', buyer_label: '万达影迷小王', stage: 'quoted',
    order_id: '', cinema: '测试万达广场店', quote_scope: 'exact_seats', quote_unit_cents: 6000, quote_total_cents: 12000, quote_ticket_count: 2,
    quote_expires_at: item.quote_expires_at, seats: [], last_message: '', updated_at: item.updated_at, next_action: '等待买家确认报价',
  });
});

test('quote analytics records non-personal showtime conversion and authoritative amounts', async () => {
  let now = 1_000;
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-quote-analytics-')), 'context.json'), { now: () => now });
  const screening = { cinema: '潍坊万达广场店', movie: '机器人总动员', date: '2026-08-22', showtime: '09:25', hall: '3号厅' };
  await store.markQuoted('tenant-1', message, { ...screening, ticketCount: 1, unitQuoteCents: 3300, totalQuoteCents: 3300, memberCostTotalCents: 3100, channelFeeTotalCents: 300, pricingSource: 'wanda_realtime', pricingRuleVersion: 'policy-v1', replyDelivered: true });
  await store.markQuoteConfirmed('tenant-1', message);
  await store.bindOrder('tenant-1', message, 'order-1');
  await store.setOrderStage('tenant-1', message, 'paid_manual_delivery', 'order-1');
  now += 1_000;
  const second = { ...message, chatId: 'chat-2', peerUnb: 'buyer-2' };
  await store.markQuoted('tenant-1', second, { ...screening, ticketCount: 2, unitQuoteCents: 3400, totalQuoteCents: 6800, memberCostTotalCents: 6200, pricingSource: 'wanda_realtime' });

  const records = await store.listQuoteRecords('tenant-1');
  assert.equal(records.length, 2);
  assert.deepEqual(records.map((item) => item.quote_total_cents).sort((a, b) => a - b), [3300, 6800]);
  assert.ok(records.some((item) => item.channel_fee_total_cents === 300));
  assert.equal(records[0].screening_sample_size, 2);
  assert.equal(records[0].screening_order_created_count, 1);
  assert.equal(records[0].screening_order_success_rate, 50);
  assert.equal(records[0].screening_paid_success_rate, 50);
  assert.ok(records.some((item) => item.stage === 'paid_manual_delivery' && item.order_id === 'order-1'));
  assert.equal(JSON.stringify(records).includes('buyer_name'), false);
});

test('legacy paid events without any plugin quote are presented as unmanaged instead of exceptions', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  await store.markOrderException('tenant-1', message, 'paid_quote_unconfirmed_or_expired', 'external-order');
  const [operation] = await store.listOperations('tenant-1');
  const [ticket] = await store.listTicketOrders('tenant-1');
  assert.equal(operation.stage, 'paid_unmanaged');
  assert.equal(operation.next_action, '非本插件报价订单，无需自动处理');
  assert.equal(ticket.stage, 'paid_unmanaged');
  assert.equal(ticket.exception_reason, '');
});

test('operations queue reads every platform nickname alias and falls back to the peer ID', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  const aliases = ['peerNick', 'peerNickname', 'peer_nick', 'buyerNick', 'buyerNickname', 'buyer_nick', 'buyerName'];
  for (const [index, field] of aliases.entries()) {
    const peerUnb = `peer-${index}`;
    await store.add('tenant-1', { ...message, peerUnb, [field]: `  平台昵称 ${index}  ` });
    await store.markQuoted('tenant-1', { ...message, peerUnb });
  }
  await store.setOrderStage('tenant-1', { ...message, peerUnb: 'peer-without-nickname' }, 'waiting_payment', 'order-1');
  const operations = await store.listOperations('tenant-1');
  aliases.forEach((_field, index) => assert.equal(operations.find((item) => item.peer_unb === `peer-${index}`)?.buyer_label, `平台昵称 ${index}`));
  assert.equal(operations.find((item) => item.peer_unb === 'peer-without-nickname')?.buyer_label, '买家 peer-without-nickname');
});

test('ticket-order list exposes only actionable paid or aftersales conversation stages', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  await store.setOrderStage('tenant-1', message, 'paid_manual_delivery', 'order-paid');
  await store.setOrderStage('tenant-1', { ...message, peerUnb: 'buyer-2' }, 'waiting_payment', 'order-waiting');
  await store.setOrderStage('tenant-1', { ...message, peerUnb: 'buyer-3' }, 'aftersale', 'order-after');
  assert.deepEqual((await store.listTicketOrders('tenant-1')).map((item) => [item.stage, item.order_id]), [
    ['paid_manual_delivery', 'order-paid'], ['aftersale', 'order-after'],
  ]);
});

test('manual fulfillment writeback records status history without storing ticket codes or sending anything', async () => {
  let now = 5_000;
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'), { now: () => now });
  await store.setOrderStage('tenant-1', message, 'paid_manual_delivery', 'order-paid');
  await assert.rejects(() => store.updateFulfillment('tenant-1', 'order-paid', 'ticket_sent'), /invalid_fulfillment_transition/u);
  const issued = await store.updateFulfillment('tenant-1', 'order-paid', 'ticket_issued');
  assert.equal(issued.stage, 'ticket_issued');
  now += 1_000;
  const sent = await store.updateFulfillment('tenant-1', 'order-paid', 'ticket_sent');
  assert.equal(sent.stage, 'ticket_sent');
  assert.deepEqual(sent.fulfillment_history, [
    { at: 5_000, status: 'ticket_issued', source: 'authenticated_ui' },
    { at: 6_000, status: 'ticket_sent', source: 'authenticated_ui' },
  ]);
  now += 1_000;
  const duplicate = await store.updateFulfillment('tenant-1', 'order-paid', 'ticket_sent');
  assert.equal(duplicate.fulfillment_updated_at, 6_000);
  assert.deepEqual(duplicate.fulfillment_history, sent.fulfillment_history);
  await store.setOrderStage('tenant-1', message, 'paid_manual_delivery', 'order-paid');
  assert.equal((await store.get('tenant-1', message)).facts.stage, 'ticket_sent');
  const [listed] = await store.listTicketOrders('tenant-1');
  assert.equal(listed.fulfillment_updated_at, 6_000);
  assert.equal(JSON.stringify(listed).includes('ticket_code'), false);
  await assert.rejects(() => store.updateFulfillment('tenant-1', 'missing', 'ticket_sent'), /order_not_found/u);
  await assert.rejects(() => store.updateFulfillment('tenant-1', 'order-paid', 'shipped'), /invalid_fulfillment_status/u);
});

test('a late older buyer message cannot overwrite newer conversation facts', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'), { now: () => 3_000 });
  await store.add('tenant-1', message, 2_000);
  await store.add('tenant-1', { ...message, content: '3张' }, 1_000);
  const context = await store.get('tenant-1', message);
  assert.equal(context.facts.ticket_count, undefined);
  await store.add('tenant-1', { ...message, content: '2张' }, 2_500);
  await store.add('tenant-1', { ...message, content: '3张' }, 2_100);
  const updated = await store.get('tenant-1', message);
  assert.equal(updated.facts.ticket_count, 2);
  assert.deepEqual(updated.messages.map((item) => item.at), [1_000, 2_000, 2_100, 2_500]);
});

test('buyer message bodies, image URLs and expired quote drafts are removed after twenty four hours', async () => {
  let now = 1_000;
  const file = join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json');
  const store = new ConversationContextStore(file, { now: () => now });
  await store.add('tenant-1', { ...message, peerNick: '测试买家昵称', content: '包含临时买家信息', imageUrls: ['https://img.alicdn.com/private-seat.png'] }, now);
  await store.recordQuoteDraft('tenant-1', message, {
    recognition: { cinema: '测试万达影院', movie: '测试影片', date: '2026-08-19', showtime: '19:10' },
    imageUrl: 'https://img.alicdn.com/private-seat.png',
  });
  now += 24 * 60 * 60 * 1_000 + 1;
  const expired = await store.get('tenant-1', message);
  assert.deepEqual(expired.messages, []);
  assert.equal(expired.facts.quote_draft, undefined);
  assert.equal(expired.facts.buyer_name, undefined);

  await store.markOrderException('tenant-1', message, 'retention_self_test');
  const persisted = await readFile(file, 'utf8');
  assert.equal(persisted.includes('包含临时买家信息'), false);
  assert.equal(persisted.includes('private-seat.png'), false);
  assert.equal(persisted.includes('测试买家昵称'), false);
});

test('quote drafts retain only bounded matching facts and never a quoted price or typed seat selection', async () => {
  let now = 1_000;
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'), { now: () => now });
  const saved = await store.recordQuoteDraft('tenant-1', message, {
    recognition: {
      cinema: ' 十堰万达影城 ', movie: '欢迎来龙餐馆', date: '2026-08-19', showtime: '19:10', hall: '7号厅',
      official_selection: { is_selected: false, selected_seat_numbers: ['6排8座', '6排9座'] },
    },
    ticketCount: 2,
    imageUrl: 'https://img.alicdn.com/seat.png',
  });
  assert.deepEqual(saved, {
    fields: {
      cinema: { value: '十堰万达影城', source: 'image_or_text', confidence: 0.8 }, movie: { value: '欢迎来龙餐馆', source: 'image_or_text', confidence: 0.8 },
      date: { value: '2026-08-19', source: 'image_or_text', confidence: 0.8 }, showtime: { value: '19:10', source: 'image_or_text', confidence: 0.8 },
      hall: { value: '7号厅', source: 'image_or_text', confidence: 0.8 }, ticket_count: { value: 2, source: 'buyer_text', confidence: 0.8 },
    }, last_image: 'https://img.alicdn.com/seat.png', state: 'matching', updated_at: 1_000, expires_at: 601_000,
  });
  const draft = (await store.get('tenant-1', message)).facts.quote_draft;
  assert.equal(draft.quote_total_cents, undefined);
  assert.equal(draft.selected_seat_numbers, undefined);
  await store.setOrderStage('tenant-1', message, 'paid_manual_delivery', 'order-1');
  now += 1;
  assert.equal(await store.recordQuoteDraft('tenant-1', message, { recognition: { cinema: '北京万达影城' } }), null);
});

test('quote drafts retain a bounded recognition artifact for count-only supplements', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-artifact-')), 'context.json'));
  const artifact = {
    status: 'recognized', tenant_id: 'tenant-1', ticket_count: 1,
    recognition: { cinema: '江桥万达', movie: '测试影片', date: '2026-08-22', showtime: '19:30', official_selection: { is_selected: true, selected_seat_numbers: ['6排16座'], selected_count: 1 } },
    field_sources: { cinema: 'image' }, token: 'must-not-persist',
  };
  const saved = await store.recordQuoteDraft('tenant-1', message, { recognition: artifact.recognition, ticketCount: 1, recognitionArtifact: artifact, imageUrl: 'https://img.alicdn.com/seat.png' });
  assert.equal(saved.recognition_artifact.recognition.official_selection.selected_seat_numbers[0], '6排16座');
  assert.equal(JSON.stringify(saved).includes('must-not-persist'), false);
  assert.ok(JSON.stringify(saved.recognition_artifact).length <= 5_000);
});

test('quote drafts preserve field provenance and dedupe an unchanged matching attempt', async () => {
  let now = 1_000;
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'), { now: () => now });
  const input = {
    recognition: { cinema: '十堰万达影城', movie: '欢迎来龙餐馆', date: '2026-08-19', showtime: '19:10', hall: '7号厅' },
    ticketCount: 2,
    imageUrl: 'https://img.alicdn.com/seat.png',
    fieldSources: { cinema: 'buyer_text', movie: 'buyer_text', date: 'buyer_text', showtime: 'buyer_text', hall: 'buyer_text', ticket_count: 'typed_seats' },
  };

  const saved = await store.recordQuoteDraft('tenant-1', message, input);
  assert.deepEqual(saved.fields.showtime, { value: '19:10', source: 'buyer_text', confidence: 1 });
  assert.deepEqual(saved.fields.ticket_count, { value: 2, source: 'typed_seats', confidence: 0.9 });
  assert.equal(saved.last_image, 'https://img.alicdn.com/seat.png');
  assert.equal(saved.state, 'matching');
  assert.equal(await store.claimQuoteDraftAttempt('tenant-1', message), true);
  assert.equal(await store.claimQuoteDraftAttempt('tenant-1', message), false);

  now += 1;
  await store.recordQuoteDraft('tenant-1', message, { ...input, recognition: { ...input.recognition, showtime: '20:00' } });
  assert.equal(await store.claimQuoteDraftAttempt('tenant-1', message), true);
});

test('confirmation fallback may be claimed once per selected image', async () => {
  const store = new ConversationContextStore(join(await mkdtemp(join(tmpdir(), 'wanda-context-')), 'context.json'));
  assert.equal(await store.claimConfirmation('tenant-1', message, 'https://images.example/one.png'), true);
  assert.equal(await store.claimConfirmation('tenant-1', message, 'https://images.example/one.png'), false);
  assert.equal(await store.claimConfirmation('tenant-1', message, 'https://images.example/two.png'), true);
});
