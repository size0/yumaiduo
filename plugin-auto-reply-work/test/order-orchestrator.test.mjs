import assert from 'node:assert/strict';
import test from 'node:test';
import { EVENT_ROUTE_KIND } from '../src/event-router.mjs';
import { createOrderOrchestrator } from '../src/orders/order-orchestrator.mjs';

function record(event, payload = {}, id = `evt-${event}`) {
  return {
    key: `tenant-1:${id}`,
    leaseId: 'lease-1',
    envelope: { id, tenantId: 'tenant-1', event, ts: Date.now(), payload },
  };
}

function harness(overrides = {}) {
  const calls = [];
  const now = Date.now();
  const session = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const facts = {
    quote_confirmed: true,
    quote_total_cents: 8_800,
    quote_ticket_count: 2,
    quote_expires_at: now + 60_000,
    ...overrides.facts,
  };
  const core = {
    im: {
      async getSessionByOrder(orderId) { calls.push(['session', orderId]); return session; },
    },
    orders: {
      async get(orderId) { calls.push(['order-get', orderId]); return { orderStatus: 1, quantity: 2, payment: '8800', postFee: '0', paidAmount: 8_800 }; },
    },
    ...overrides.core,
  };
  const conversationContextStore = {
    async get() { return { facts, messages: overrides.messages ?? [] }; },
    async bindOrder(...args) { calls.push(['bind-order', ...args]); return true; },
    async markQuoteConfirmed(...args) { calls.push(['confirm-quote', ...args]); return true; },
    async markOrderException(...args) { calls.push(['order-exception', ...args]); return true; },
    async setOrderStage(...args) { calls.push(['order-stage', ...args]); return true; },
    ...overrides.conversationContextStore,
  };
  const eventStore = {
    async complete(...args) { calls.push(['complete', ...args]); },
    ...overrides.eventStore,
  };
  const backend = {
    async getRuntimeSettings(accountUnb) {
      calls.push(['settings', accountUnb]);
      return { settings: { automation_enabled: true, auto_price_change: true, ...overrides.runtimeSettings } };
    },
  };
  const executeAction = overrides.executeAction ?? (async (action) => {
    calls.push(['action', action]);
    return { status: 'succeeded', message_id: action.kind === 'send_text' ? 'message-1' : undefined };
  });
  const orchestrator = createOrderOrchestrator({
    backend,
    coreFor: () => core,
    eventStore,
    conversationContextStore,
    executeAction,
    logger: { warn() {} },
  });
  return { orchestrator, calls, session };
}

test('construction fails closed without required execution dependencies', () => {
  assert.throws(() => createOrderOrchestrator(), /dependencies are required/u);
});

test('unrelated event routes are not claimed by the order orchestrator', async () => {
  const { orchestrator, calls } = harness();
  const result = await orchestrator.process(record('im.message.received'), { kind: EVENT_ROUTE_KIND.MESSAGE });
  assert.equal(result, null);
  assert.deepEqual(calls, []);
});

test('a confirmed active quote creates the same bounded change-price action', async () => {
  const { orchestrator, calls } = harness();
  const source = record('order.created', { orderId: 'order-1' });
  const result = await orchestrator.process(source, { kind: EVENT_ROUTE_KIND.ORDER_CREATED });
  const action = calls.find(([name]) => name === 'action')[1];
  assert.equal(result.status, 'completed');
  assert.deepEqual(action, {
    action_id: `${source.envelope.id}:quoted-order-price-change`,
    kind: 'change_price',
    tenant_id: 'tenant-1',
    account_unb: 'shop-1',
    order_id: 'order-1',
    price_fee: 8_800,
    transport_fee: 0,
    expected_total_cents: 8_800,
    expected_quantity: 2,
    order_quantity_policy: 'listing_unit',
    gates: {
      feature_enabled: true,
      unique_showtime: true,
      quantity_confirmed: true,
      selection_confirmed: true,
      quote_valid: true,
      order_linked: true,
      human_takeover: false,
      max_amount_cents: 200_000,
    },
  });
});

test('direct Wanda order creation blocks price change when pricing-account evidence is missing', async () => {
  const { orchestrator, calls } = harness({
    facts: { pricing_source: '万达临时锁座 available-offers + 后台报价规则' },
  });
  await orchestrator.process(record('order.created', { orderId: 'order-1' }), { kind: EVENT_ROUTE_KIND.ORDER_CREATED });
  assert.equal(calls.some(([name, action]) => name === 'action' && action.kind === 'change_price'), false);
  assert.equal(calls.find(([name]) => name === 'order-exception')[3], 'pricing_account_evidence_missing');
  assert.match(calls.find(([name]) => name === 'action')[1].text, /先不要付款/u);
  assert.equal(calls.find(([name]) => name === 'complete')[3].order_price_change.status, 'blocked');
});

test('order creation fails closed on a conflicting recent ticket count', async () => {
  const { orchestrator, calls } = harness({
    facts: { quote_ticket_count: 1 },
    messages: [{ role: 'buyer', text: '需要两张' }],
  });
  await orchestrator.process(record('order.created', { orderId: 'order-1' }), { kind: EVENT_ROUTE_KIND.ORDER_CREATED });
  assert.equal(calls.some(([name, action]) => name === 'action' && action.kind === 'change_price'), false);
  assert.equal(calls.find(([name]) => name === 'order-exception')[3], 'ticket_count_conflict');
  assert.match(calls.find(([name]) => name === 'action')[1].text, /只核验了1张/u);
  assert.match(calls.find(([name]) => name === 'action')[1].text, /需要2张/u);
});

test('order creation honors a persisted purchase intent before changing price', async () => {
  const { orchestrator, calls } = harness({
    facts: { quote_confirmed: false },
    messages: [{ role: 'buyer', text: '我已拍下，待付款' }],
  });
  await orchestrator.process(record('order.created', { orderId: 'order-1' }), { kind: EVENT_ROUTE_KIND.ORDER_CREATED });
  assert.equal(calls.some(([name]) => name === 'confirm-quote'), true);
  assert.equal(calls.some(([name, action]) => name === 'action' && action.kind === 'change_price'), true);
});

test('a paid order is acknowledged only after authoritative amount verification', async () => {
  const { orchestrator, calls } = harness();
  await orchestrator.process(record('order.paid', { orderId: 'order-1' }), { kind: EVENT_ROUTE_KIND.ORDER_PAID });
  assert.equal(calls.find(([name]) => name === 'order-stage')[3], 'paid_manual_delivery');
  assert.equal(calls.find(([name]) => name === 'action')[1].text, '已收到付款，请稍等人工出票。订单已付款，不会重新核价。');
});

test('a paid-order read failure is recorded without sending a buyer reply', async () => {
  const { orchestrator, calls } = harness({
    core: { orders: { async get() { throw new Error('platform unavailable'); } } },
  });
  await orchestrator.process(record('order.paid', { orderId: 'order-1' }), { kind: EVENT_ROUTE_KIND.ORDER_PAID });
  assert.equal(calls.some(([name]) => name === 'action'), false);
  assert.equal(calls.find(([name]) => name === 'order-exception')[3], 'paid_order_read_failed');
  assert.equal(calls.find(([name]) => name === 'complete')[3].reason, 'paid_order_read_failed');
});

test('a paid order without plugin quote evidence is ignored without reading the order', async () => {
  const { orchestrator, calls } = harness({
    facts: { quote_confirmed: false, quote_total_cents: undefined, quote_ticket_count: undefined, quote_expires_at: undefined },
  });
  await orchestrator.process(record('order.paid', { orderId: 'external-1' }), { kind: EVENT_ROUTE_KIND.ORDER_PAID });
  assert.equal(calls.some(([name]) => name === 'order-get'), false);
  assert.equal(calls.find(([name]) => name === 'complete')[3].reason, 'plugin_quote_missing');
});

test('price-changed notification is sent only after rereading the exact confirmed amount', async () => {
  const { orchestrator, calls } = harness();
  const source = record('order.price.changed', { orderId: 'order-1', operatorId: 'plugin:wanda-seat-autoquote' });
  await orchestrator.process(source, { kind: EVENT_ROUTE_KIND.ORDER_PRICE_CHANGED });
  const action = calls.find(([name]) => name === 'action')[1];
  assert.equal(calls.find(([name]) => name === 'order-stage')[3], 'waiting_payment');
  assert.equal(action.text, '价格已修改为88.00元，请核对后付款。订单付款后不支持退改签。');
  assert.equal(action.ignore_platform_price_change_notice, true);
});

test('a failed authoritative read after price.changed never sends payment guidance', async () => {
  const { orchestrator, calls } = harness({
    core: { orders: { async get() { throw new Error('platform unavailable'); } } },
  });
  await orchestrator.process(record('order.price.changed', { orderId: 'order-1' }), { kind: EVENT_ROUTE_KIND.ORDER_PRICE_CHANGED });
  assert.equal(calls.some(([name]) => name === 'action'), false);
  assert.equal(calls.find(([name]) => name === 'order-exception')[3], 'price_changed_order_read_failed');
  assert.equal(calls.find(([name]) => name === 'complete')[3].reason, 'price_changed_order_read_failed');
});

test('a mismatched price-changed event never sends payment guidance', async () => {
  const { orchestrator, calls } = harness({
    core: { orders: { async get() { return { orderStatus: 1, payment: '9900', postFee: '0' }; } } },
  });
  await orchestrator.process(record('order.price.changed', { orderId: 'order-1' }), { kind: EVENT_ROUTE_KIND.ORDER_PRICE_CHANGED });
  assert.equal(calls.some(([name]) => name === 'action'), false);
  assert.equal(calls.find(([name]) => name === 'order-exception')[3], 'price_changed_amount_mismatch');
  assert.equal(calls.find(([name]) => name === 'complete')[3].reason, 'price_changed_amount_mismatch');
});

test('conversation lifecycle lookup failures are contained and logged', async () => {
  const warnings = [];
  const backend = { async getRuntimeSettings() { throw new Error('settings unavailable'); } };
  const orchestrator = createOrderOrchestrator({
    backend,
    coreFor: () => ({ im: { async getSessionByOrder() { return { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }; } } }),
    eventStore: {},
    conversationContextStore: {},
    executeAction: async () => ({ status: 'succeeded' }),
    logger: { warn(...args) { warnings.push(args); } },
  });
  await orchestrator.updateConversationStage(
    record('order.closed', { orderId: 'order-1' }).envelope,
    { kind: EVENT_ROUTE_KIND.ORDER_STATUS, conversation_stage: 'cancelled' },
  );
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0][0], '[workflow] unable to update order conversation stage');
});

test('conversation lifecycle extraction preserves order binding and closed stage updates', async () => {
  const { orchestrator, calls } = harness();
  await orchestrator.updateConversationStage(
    record('order.created', { orderId: 'order-1' }).envelope,
    { kind: EVENT_ROUTE_KIND.ORDER_CREATED, conversation_stage: null },
  );
  await orchestrator.updateConversationStage(
    record('order.closed', { orderId: 'order-1' }).envelope,
    { kind: EVENT_ROUTE_KIND.ORDER_STATUS, conversation_stage: 'cancelled' },
  );
  assert.equal(calls.some(([name]) => name === 'bind-order'), true);
  assert.equal(calls.findLast(([name]) => name === 'order-stage')[3], 'cancelled');
});
