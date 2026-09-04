import assert from 'node:assert/strict';
import test from 'node:test';
import {
  buildPriceChangeIdempotencyKey,
  canonicalRepriceIdentity,
  normalizeAuthoritativeOrder,
  normalizeInboundEvent,
  normalizeQuoteSnapshot,
  summarizeProviderResult,
} from '../src/actions/contracts.mjs';
import { createPriceChangeExecutor } from '../src/actions/change-order-price.mjs';

class FakeReceiptStore {
  constructor() {
    this.records = new Map();
    this.saves = [];
  }

  async claim(initial) {
    const existing = this.records.get(initial.idempotency_key);
    if (existing) return { created: false, receipt: structuredClone(existing) };
    const receipt = structuredClone(initial);
    this.records.set(initial.idempotency_key, receipt);
    return { created: true, receipt: structuredClone(receipt) };
  }

  async save(receipt) {
    const copy = structuredClone(receipt);
    this.records.set(receipt.idempotency_key, copy);
    this.saves.push(copy);
  }
}

function providerEvent(overrides = {}) {
  return {
    id: 'event-1',
    tenantId: 'tenant-1',
    event: 'order.created',
    ts: 1_786_464_000_000,
    payload: {
      orderId: 'order-1',
      accountUnb: 'shop-1',
      peerUnb: 'buyer-1',
      chatId: 'chat-1',
      ...overrides.payload,
    },
    ...overrides,
  };
}

function quoteSnapshot(overrides = {}) {
  return {
    quote_version: 'quote-v1',
    order_id: 'order-1',
    tenant_id: 'tenant-1',
    shop_id: 'shop-1',
    buyer_id: 'buyer-1',
    chat_id: 'chat-1',
    target_amount_cents: 2_900,
    observed_order_amount_cents: 2_000,
    ...overrides,
  };
}

function newFlowQuoteSnapshot(overrides = {}) {
  const snapshot = {
    ...quoteSnapshot(),
    quote_id: 'quote-1',
    quote_hash: 'tf-1',
    terms_fingerprint: 'tf-1',
    quote_generation: 1,
    binding_revision: 1,
    transaction_revision: 0,
    flow_version: 'V4_NEW_FLOW_V2',
    source: 'phase_9a_authorization',
    idempotency_key: '',
    ...overrides,
  };
  snapshot.idempotency_key = buildPriceChangeIdempotencyKey(snapshot);
  return snapshot;
}

function backendFullQuoteSnapshot(overrides = {}) {
  return {
    ...quoteSnapshot(),
    schema_version: 'wanda_backend.quote_snapshot.v1',
    quote_id: 'backend-quote-1',
    source_event_id: 'event-1',
    current_amount_cents: 2_000,
    currency: 'CNY',
    decision: {
      action: 'change_price',
      reason_code: 'user_confirmed_quote',
      confidence: 0.99,
    },
    price_items: [
      {
        item_id: 'ticket-1',
        item_type: 'movie_ticket',
        quantity: 1,
        target_amount_cents: 2_900,
        metadata: {
          cinema_name: 'Wanda Cinema',
          show_time: '2026-08-12T20:00:00+08:00',
          seat_labels: ['A1', 'A2'],
        },
      },
    ],
    audit_context: {
      generated_by: 'backend',
      model_trace_id: 'trace-1',
    },
    ...overrides,
  };
}

function order(overrides = {}) {
  return {
    orderId: 'order-1',
    accountUnb: 'shop-1',
    buyerUnb: 'buyer-1',
    orderStatus: 1,
    payTime: null,
    payment: 2_000,
    refundStatus: 'none',
    ...overrides,
  };
}

function snakeCaseKeysOnly(value) {
  if (Array.isArray(value)) return value.every(snakeCaseKeysOnly);
  if (!value || typeof value !== 'object') return true;
  return Object.entries(value).every(([key, item]) => /^[a-z][a-z0-9_]*$/.test(key) && snakeCaseKeysOnly(item));
}

function harness({ before = order(), after = order({ payment: 2_900 }), change_price, get_order, session, receipt_store, retry_delays, sleep } = {}) {
  const calls = [];
  let reads = 0;
  const store = receipt_store ?? new FakeReceiptStore();
  const sdk = {
    orders: {
      get: async (order_id) => {
        calls.push(['orders.get', order_id]);
        reads += 1;
        if (get_order) return structuredClone(await get_order({ order_id, reads, calls }));
        return reads === 1 ? structuredClone(before) : structuredClone(after);
      },
      changePrice: async (order_id, request) => {
        calls.push(['orders.change_price', order_id, structuredClone(request)]);
        if (change_price) return change_price({ order_id, request, calls });
        return { ok: true, actionId: 'provider-action-1' };
      },
    },
    im: {
      getSessionByOrder: async (order_id) => {
        calls.push(['im.get_session_by_order', order_id]);
        const value = typeof session === 'function' ? session() : session;
        return structuredClone(value ?? { accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' });
      },
    },
  };
  const executor = createPriceChangeExecutor({
    sdk,
    tenant_id: 'tenant-1',
    receipt_store: store,
    now: () => '2026-08-12T00:00:00.000Z',
    retry_delays,
    sleep,
  });
  return { executor, calls, store };
}

test('canonical V2 reprice identity is stable and changes with quote lineage or target', () => {
  const snapshot = {
    quote_version: 'qv-1', quote_id: 'quote-1', quote_hash: 'tf-1',
    quote_generation: 1, binding_revision: 1, transaction_revision: 0,
    flow_version: 'V4_NEW_FLOW_V2', source: 'phase_9a_authorization',
    idempotency_key: '', order_id: 'order-1', tenant_id: 'tenant-1',
    shop_id: 'shop-1', buyer_id: 'buyer-1', chat_id: 'chat-1',
    target_amount_cents: 2_900,
  };
  snapshot.idempotency_key = buildPriceChangeIdempotencyKey(snapshot);
  const normalized = normalizeQuoteSnapshot(snapshot);
  assert.equal(normalized.idempotency_key, snapshot.idempotency_key);
  assert.deepEqual(canonicalRepriceIdentity(snapshot), ['order-1', 'quote-1', 1, 1, 2900]);
  assert.equal(snapshot.idempotency_key, 'price_change:v1:bb4dUMUImWo-FCYwmA4mc2RD4FqQFYHz4uev8sNUttk');
  assert.equal(buildPriceChangeIdempotencyKey(snapshot), snapshot.idempotency_key);
  assert.notEqual(buildPriceChangeIdempotencyKey({ ...snapshot, quote_generation: 2 }), snapshot.idempotency_key);
  assert.notEqual(buildPriceChangeIdempotencyKey({ ...snapshot, binding_revision: 2 }), snapshot.idempotency_key);
  assert.notEqual(buildPriceChangeIdempotencyKey({ ...snapshot, target_amount_cents: 3_000 }), snapshot.idempotency_key);
});

test('new-flow executor confirms only after authoritative reread and keeps the canonical result class', async () => {
  const { executor, calls } = harness();
  const result = await executor.execute({
    provider_event: providerEvent(), quote_snapshot: newFlowQuoteSnapshot(),
  });

  assert.equal(result.status, 'succeeded');
  assert.equal(result.reprice_status, 'REPRICE_CONFIRMED');
  assert.equal(calls.filter(([name]) => name === 'orders.change_price').length, 1);
  assert.deepEqual(calls.map(([name]) => name), [
    'orders.get', 'im.get_session_by_order', 'orders.change_price', 'orders.get',
  ]);
});

test('new-flow already-priced and paid orders never call the provider writer', async (t) => {
  await t.test('already priced', async () => {
    const { executor, calls } = harness({ before: order({ payment: 2_900 }), after: order({ payment: 2_900 }) });
    const result = await executor.execute({
      provider_event: providerEvent(), quote_snapshot: newFlowQuoteSnapshot(),
    });
    assert.equal(result.status, 'succeeded');
    assert.equal(result.reprice_status, 'ALREADY_PRICED');
    assert.equal(calls.some(([name]) => name === 'orders.change_price'), false);
  });
  await t.test('paid before execution', async () => {
    const { executor, calls } = harness({ before: order({ payTime: '2026-08-12T00:01:00Z', orderStatus: 2 }), after: order({ payTime: '2026-08-12T00:01:00Z', orderStatus: 2 }) });
    const result = await executor.execute({
      provider_event: providerEvent(), quote_snapshot: newFlowQuoteSnapshot(),
    });
    assert.equal(result.status, 'skipped');
    assert.equal(result.reprice_status, 'ORDER_NOT_UNPAID');
    assert.equal(calls.some(([name]) => name === 'orders.change_price'), false);
  });
});

test('new-flow separates unconfirmed writes from unknown writes and never blindly retries unknown', async (t) => {
  await t.test('provider success but mismatched reread', async () => {
    const { executor } = harness({
      after: order({ payment: 2_000 }),
      change_price: async () => ({ ok: true }),
    });
    const result = await executor.execute({
      provider_event: providerEvent(), quote_snapshot: newFlowQuoteSnapshot(),
    });
    assert.equal(result.status, 'failed');
    assert.equal(result.reprice_status, 'REPRICE_UNCONFIRMED');
  });
  await t.test('timeout reconciles to target', async () => {
    const { executor, calls } = harness({
      change_price: async () => { throw Object.assign(new Error('timeout'), { name: 'TimeoutError', code: 'ETIMEDOUT', status: 504 }); },
      after: order({ payment: 2_900 }),
    });
    const result = await executor.execute({
      provider_event: providerEvent(), quote_snapshot: newFlowQuoteSnapshot(),
    });
    assert.equal(result.status, 'succeeded');
    assert.equal(result.reprice_status, 'REPRICE_CONFIRMED');
    assert.equal(result.reconciled, true);
    assert.equal(calls.filter(([name]) => name === 'orders.change_price').length, 1);
  });
  await t.test('timeout remains unknown and duplicate does not write again', async () => {
    const store = new FakeReceiptStore();
    const { executor, calls } = harness({
      receipt_store: store,
      change_price: async () => { throw Object.assign(new Error('timeout'), { name: 'TimeoutError', code: 'ETIMEDOUT', status: 504 }); },
      after: order({ payment: 2_000 }),
    });
    const snapshot = newFlowQuoteSnapshot();
    const first = await executor.execute({ provider_event: providerEvent(), quote_snapshot: snapshot });
    const callsAfterFirst = calls.filter(([name]) => name === 'orders.change_price').length;
    const second = await executor.execute({ provider_event: providerEvent(), quote_snapshot: snapshot });
    assert.equal(first.status, 'unknown');
    assert.equal(first.reprice_status, 'REPRICE_UNKNOWN');
    assert.equal(second.status, 'unknown');
    assert.equal(second.reprice_status, 'REPRICE_UNKNOWN');
    assert.equal(callsAfterFirst, 1);
    assert.equal(calls.filter(([name]) => name === 'orders.change_price').length, 1);
  });
});

test('normalizers map provider fields once and keep snake_case contracts with raw summaries', () => {
  const event = normalizeInboundEvent(providerEvent());
  const snapshot = normalizeAuthoritativeOrder(order(), { sdk_tenant_id: 'tenant-1' });

  assert.equal(event.order_id, 'order-1');
  assert.equal(event.shop_id, 'shop-1');
  assert.equal(event.field_sources.order_id.provider_field, 'payload.orderId');
  assert.equal(snapshot.amount_cents, 2_000);
  assert.equal(snapshot.field_sources.amount_cents.provider_field, 'payment');
  assert.deepEqual(snapshot.provider_raw_summary.raw_field_names, [
    'accountUnb', 'buyerUnb', 'orderId', 'orderStatus', 'payTime', 'payment', 'refundStatus',
  ]);
  assert.equal(snakeCaseKeysOnly(event), true);
  assert.equal(snakeCaseKeysOnly(snapshot), true);
});

test('order normalization prefers a populated paid alias over an earlier null alias', () => {
  const snapshot = normalizeAuthoritativeOrder(order({
    payTime: null,
    paidAt: '2026-08-12T01:00:00Z',
  }), { sdk_tenant_id: 'tenant-1' });
  assert.equal(snapshot.paid_at, '2026-08-12T01:00:00Z');
  assert.equal(snapshot.field_sources.paid_at.provider_field, 'paidAt');
});

test('quote snapshot accepts the backend full snake_case structure and normalizes only executor fields', () => {
  const snapshot = normalizeQuoteSnapshot(backendFullQuoteSnapshot());

  assert.deepEqual(snapshot, {
    schema_version: 'executor_next.quote_snapshot.v1',
    quote_version: 'quote-v1',
    order_id: 'order-1',
    tenant_id: 'tenant-1',
    shop_id: 'shop-1',
    buyer_id: 'buyer-1',
    chat_id: 'chat-1',
    target_amount_cents: 2_900,
    observed_order_amount_cents: 2_000,
    field_sources: {
      quote_version: { source: 'backend_quote_snapshot', field: 'quote_version' },
      order_id: { source: 'backend_quote_snapshot', field: 'order_id' },
      tenant_id: { source: 'backend_quote_snapshot', field: 'tenant_id' },
      shop_id: { source: 'backend_quote_snapshot', field: 'shop_id' },
      buyer_id: { source: 'backend_quote_snapshot', field: 'buyer_id' },
      chat_id: { source: 'backend_quote_snapshot', field: 'chat_id' },
      target_amount_cents: { source: 'backend_quote_snapshot', field: 'target_amount_cents' },
      observed_order_amount_cents: { source: 'backend_quote_snapshot', field: 'observed_order_amount_cents' },
    },
  });
});

test('durable quote binding fields and authoritative order quantity are normalized', () => {
  const quote = normalizeQuoteSnapshot(quoteSnapshot({
    quote_record_id: 'record-1',
    confirmation_version: 'v4c-1',
    quote_expires_at: '2026-08-12T00:15:00.000Z',
    confirmed_ticket_count: 2,
  }));
  const snapshot = normalizeAuthoritativeOrder(order({ quantity: 2 }), { sdk_tenant_id: 'tenant-1' });

  assert.equal(quote.quote_record_id, 'record-1');
  assert.equal(quote.confirmation_version, 'v4c-1');
  assert.equal(quote.confirmed_ticket_count, 2);
  assert.equal(quote.observed_order_amount_cents, 2_000);
  assert.equal(snapshot.quantity, 2);
});


test('idempotency key is stable and covers ownership bindings, order, quote version and target cents', () => {
  const base = quoteSnapshot();
  const first = buildPriceChangeIdempotencyKey(base);
  assert.equal(first, buildPriceChangeIdempotencyKey({ ...base }));
  assert.notEqual(first, buildPriceChangeIdempotencyKey({ ...base, tenant_id: 'tenant-2' }));
  assert.notEqual(first, buildPriceChangeIdempotencyKey({ ...base, shop_id: 'shop-2' }));
  assert.notEqual(first, buildPriceChangeIdempotencyKey({ ...base, buyer_id: 'buyer-2' }));
  assert.notEqual(first, buildPriceChangeIdempotencyKey({ ...base, chat_id: 'chat-2' }));
  assert.notEqual(first, buildPriceChangeIdempotencyKey({ ...base, order_id: 'order-2' }));
  assert.notEqual(first, buildPriceChangeIdempotencyKey({ ...base, quote_version: 'quote-v2' }));
  assert.notEqual(first, buildPriceChangeIdempotencyKey({ ...base, target_amount_cents: 2_901 }));
  assert.notEqual(first, buildPriceChangeIdempotencyKey({ ...base, observed_order_amount_cents: 2_001 }));
});

test('price change reads ownership, saves provider receipt and succeeds only after matching reread', async () => {
  const store = new FakeReceiptStore();
  let durable_at_provider_call = null;
  const { executor, calls } = harness({
    receipt_store: store,
    change_price: async () => {
      durable_at_provider_call = structuredClone([...store.records.values()][0]);
      return { ok: true, actionId: 'provider-action-1' };
    },
  });
  const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quoteSnapshot() });

  assert.equal(result.status, 'succeeded');
  assert.equal(result.reason_code, 'price_change_verified');
  assert.equal(result.verified_amount_cents, 2_900);
  assert.equal(durable_at_provider_call.phase, 'provider_call_started');
  assert.equal(durable_at_provider_call.action_attempted, true);
  assert.equal(durable_at_provider_call.provider_receipt, null);
  assert.deepEqual(calls, [
    ['orders.get', 'order-1'],
    ['im.get_session_by_order', 'order-1'],
    ['orders.change_price', 'order-1', { priceFee: 2_900, transportFee: 0 }],
    ['orders.get', 'order-1'],
  ]);
  const receipt = store.records.get(result.idempotency_key);
  assert.equal(receipt.status, 'succeeded');
  assert.equal(receipt.provider_receipt.source, 'orders.change_price');
  assert.equal(receipt.before_order.amount_cents, 2_000);
  assert.equal(receipt.after_order.amount_cents, 2_900);
});

test('authoritative order without buyer aliases binds buyer and chat through the order session', async () => {
  const { executor, calls } = harness({
    before: order({ buyerUnb: undefined }),
    after: order({ buyerUnb: undefined, payment: 2_900 }),
    session: { accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' },
  });

  const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quoteSnapshot() });

  assert.equal(result.status, 'succeeded');
  assert.equal(result.reason_code, 'price_change_verified');
  assert.equal(result.verified_amount_cents, 2_900);
  assert.deepEqual(calls.map(([name]) => name), [
    'orders.get', 'im.get_session_by_order', 'orders.change_price', 'orders.get',
  ]);
});


test('provider receipt persistence uses a low-sensitivity allowlist', async () => {
  const sensitive_values = {
    phone: '13800138000',
    address: 'Sensitive full address',
    token: 'secret-provider-token',
    cookie: 'session=secret-cookie',
    authorization: 'Bearer secret-authorization',
    message: 'Complete provider message with private content',
  };
  const { executor, store } = harness({
    change_price: async () => ({
      ok: true,
      status: 'SUCCEEDED',
      actionId: 'provider-action-1',
      ...sensitive_values,
    }),
  });
  const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quoteSnapshot() });
  const receipt = store.records.get(result.idempotency_key);
  const serialized = JSON.stringify(receipt.provider_receipt);

  assert.deepEqual(receipt.provider_receipt.selected_fields, [
    { provider_field: 'ok', value: true },
    { provider_field: 'status', value: 'succeeded' },
  ]);
  for (const [field, sensitive_value] of Object.entries(sensitive_values)) {
    assert.equal(serialized.includes(field), false, `${field} key must not persist`);
    assert.equal(serialized.includes(sensitive_value), false, `${field} value must not persist`);
  }
  assert.equal(JSON.stringify(summarizeProviderResult('13800138000')).includes('13800138000'), false);
});

test('provider error persistence stores only stable redacted classification', async () => {
  const sensitive_message = 'POST https://provider.example/change?token=secret-token body={"authorization":"Bearer secret","phone":"13800138000"}';
  const timeout = Object.assign(new Error(sensitive_message), {
    name: 'TimeoutError',
    code: 'ETIMEDOUT',
    status: 504,
    url: 'https://provider.example/change?token=secret-token',
    body: { authorization: 'Bearer secret', phone: '13800138000' },
  });
  const { executor, store } = harness({
    change_price: async () => { throw timeout; },
    after: order({ payment: 2_000 }),
  });
  const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quoteSnapshot() });
  const receipt = store.records.get(result.idempotency_key);
  const serialized = JSON.stringify(receipt);

  assert.equal(result.status, 'unknown');
  assert.deepEqual(receipt.provider_error, {
    schema_version: 'executor_next.provider_error_summary.v1',
    category: 'timeout',
    code: 'etimedout',
    status: 504,
    retryable: true,
    summary: 'provider_timeout',
  });
  assert.equal(Object.hasOwn(receipt.provider_error, 'message'), false);
  for (const value of ['secret-token', 'provider.example', 'Bearer secret', '13800138000', sensitive_message]) {
    assert.equal(serialized.includes(value), false, `${value} must not persist`);
  }
});

test('paid and closed orders are skipped without calling changePrice', async (t) => {
  const cases = [
    ['paid_at', order({ payTime: '2026-08-12T01:00:00Z', orderStatus: 1 }), 'order_already_paid'],
    ['paid_status', order({ orderStatus: 2 }), 'order_paid_or_closed'],
    ['closed_status', order({ orderStatus: 'closed' }), 'order_paid_or_closed'],
  ];
  for (const [name, before, reason_code] of cases) {
    await t.test(name, async () => {
      const { executor, calls } = harness({ before });
      const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quoteSnapshot() });
      assert.equal(result.status, 'skipped');
      assert.equal(result.reason_code, reason_code);
      if (name === 'paid_at') {
        assert.equal(result.observed_order_amount_cents, 2_000);
        assert.equal(result.auto_refund_eligible, true);
        assert.equal(result.manual_price_change_suspected, false);
      }
      assert.equal(calls.some(([name]) => name === 'orders.change_price'), false);
    });
  }
});

test('paid order with a changed amount is never authorized for automatic refund', async () => {
  const { executor, calls } = harness({
    before: order({ payTime: '2026-08-12T01:00:00Z', orderStatus: 1, payment: 2_500 }),
  });

  const result = await executor.execute({
    provider_event: providerEvent(), quote_snapshot: quoteSnapshot(),
  });

  assert.equal(result.status, 'skipped');
  assert.equal(result.reason_code, 'order_already_paid');
  assert.equal(result.observed_order_amount_cents, 2_000);
  assert.equal(result.verified_amount_cents, 2_500);
  assert.equal(result.auto_refund_eligible, false);
  assert.equal(result.manual_price_change_suspected, true);
  assert.equal(calls.some(([name]) => name === 'orders.change_price'), false);
});

test('refunding orders are skipped without calling changePrice', async () => {
  const { executor, calls } = harness({ before: order({ refundStatus: 'processing' }) });
  const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quoteSnapshot() });
  assert.equal(result.status, 'skipped');
  assert.equal(result.reason_code, 'order_refund_in_progress_or_complete');
  assert.equal(calls.some(([name]) => name === 'orders.change_price'), false);
});

test('order, shop, buyer and session ownership mismatches are rejected before changePrice', async (t) => {
  const cases = [
    ['order', { before: order({ orderId: 'order-2' }) }, 'order_id_order_ownership_mismatch'],
    ['shop', { before: order({ accountUnb: 'shop-2' }) }, 'shop_id_order_ownership_mismatch'],
    ['buyer', { before: order({ buyerUnb: 'buyer-2' }) }, 'buyer_id_order_ownership_mismatch'],
    ['session', { session: { accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-2' } }, 'chat_id_session_ownership_mismatch'],
  ];
  for (const [name, options, reason_code] of cases) {
    await t.test(name, async () => {
      const { executor, calls } = harness(options);
      const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quoteSnapshot() });
      assert.equal(result.status, 'skipped');
      assert.equal(result.reason_code, reason_code);
      assert.equal(calls.some(([call_name]) => call_name === 'orders.change_price'), false);
    });
  }
});

test('expired durable quote is rejected but listing quantity does not gate changePrice', async () => {
  const expired = harness({ before: order({ quantity: 2 }) });
  const expiredResult = await expired.executor.execute({
    provider_event: providerEvent(),
    quote_snapshot: quoteSnapshot({
      quote_record_id: 'record-1', confirmation_version: 'v4c-1',
      quote_expires_at: '2026-08-11T23:59:00.000Z', confirmed_ticket_count: 2,
    }),
  });
  assert.equal(expiredResult.reason_code, 'quote_expired');
  assert.deepEqual(expired.calls, []);

  const listingQuantityOne = harness({ before: order({ quantity: 1 }) });
  const quantityIndependentResult = await listingQuantityOne.executor.execute({
    provider_event: providerEvent(),
    quote_snapshot: quoteSnapshot({
      quote_record_id: 'record-1', confirmation_version: 'v4c-1',
      quote_expires_at: '2026-08-12T00:15:00.000Z', confirmed_ticket_count: 2,
    }),
  });
  assert.equal(quantityIndependentResult.reason_code, 'price_change_verified');
  assert.equal(listingQuantityOne.calls.some(([name]) => name === 'orders.change_price'), true);
});


test('invalid target cents and camelCase quote snapshots are rejected before any SDK call', async () => {
  const { executor, calls } = harness();
  const decimal = await executor.execute({
    provider_event: providerEvent(),
    quote_snapshot: quoteSnapshot({ target_amount_cents: 29.5 }),
  });
  const mixed = await executor.execute({
    provider_event: providerEvent(),
    quote_snapshot: { ...quoteSnapshot(), quoteVersion: 'mixed' },
  });
  assert.equal(decimal.reason_code, 'target_amount_cents_invalid');
  assert.equal(mixed.reason_code, 'quote_snapshot_requires_snake_case');
  assert.equal(calls.length, 0);
});

test('an acknowledged change with a mismatched reread is failed, never succeeded', async () => {
  const { executor, calls } = harness({ after: order({ payment: 2_800 }) });
  const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quoteSnapshot() });
  assert.equal(result.status, 'failed');
  assert.equal(result.reason_code, 'price_change_verification_failed');
  assert.equal(result.verified_amount_cents, 2_800);
  assert.equal(calls.filter(([name]) => name === 'orders.change_price').length, 1);
});

test('explicit OAuth flow rejection retries after unchanged readback and reruns all gates', async () => {
  let changed = false;
  let attempts = 0;
  const sleeps = [];
  const { executor, calls } = harness({
    get_order: async () => order({ payment: changed ? 2_900 : 2_000 }),
    change_price: async () => {
      attempts += 1;
      if (attempts === 1) {
        const error = new Error('oauth flow rejected');
        error.code = 'e_oauth_flow_failed';
        error.response = { status: 400 };
        throw error;
      }
      changed = true;
      return { ok: true };
    },
    retry_delays: [2_000, 5_000],
    sleep: async (delay) => { sleeps.push(delay); },
  });

  const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quoteSnapshot() });

  assert.equal(result.status, 'succeeded');
  assert.equal(result.reason_code, 'price_change_verified_after_explicit_retry');
  assert.equal(result.verified_amount_cents, 2_900);
  assert.equal(attempts, 2);
  assert.deepEqual(sleeps, [2_000]);
  assert.equal(calls.filter(([name]) => name === 'orders.get').length, 4);
});

test('timeout is reconciled by reread and never blindly retried', async () => {
  const timeout = Object.assign(new Error('request timed out'), { name: 'TimeoutError' });
  const { executor, calls } = harness({
    change_price: async () => { throw timeout; },
    after: order({ payment: 2_900 }),
  });
  const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quoteSnapshot() });
  assert.equal(result.status, 'succeeded');
  assert.equal(result.reason_code, 'reconciled_after_platform_error');
  assert.equal(result.reconciled, true);
  assert.equal(calls.filter(([name]) => name === 'orders.change_price').length, 1);
  assert.equal(calls.at(-1)[0], 'orders.get');
});

test('unknown timeout result stays unknown after reread and duplicate submission only rereads', async () => {
  const timeout = Object.assign(new Error('request timed out'), { name: 'AbortError' });
  const { executor, calls } = harness({
    change_price: async () => { throw timeout; },
    after: order({ payment: 2_000 }),
  });
  const request = { provider_event: providerEvent(), quote_snapshot: quoteSnapshot() };
  const first = await executor.execute(request);
  const second = await executor.execute(request);

  assert.equal(first.status, 'unknown');
  assert.equal(first.reason_code, 'platform_result_unknown_after_readback');
  assert.equal(second.status, 'unknown');
  assert.equal(second.reason_code, 'previous_price_change_result_unknown');
  assert.equal(second.deduplicated, true);
  assert.equal(calls.filter(([name]) => name === 'orders.change_price').length, 1);
  assert.equal(calls.filter(([name]) => name === 'orders.get').length, 3);
});

test('a completed duplicate returns the saved result without another SDK call', async () => {
  const { executor, calls } = harness();
  const request = { provider_event: providerEvent(), quote_snapshot: quoteSnapshot() };
  const first = await executor.execute(request);
  const call_count = calls.length;
  const duplicate = await executor.execute(request);

  assert.equal(first.status, 'succeeded');
  assert.equal(duplicate.status, 'succeeded');
  assert.equal(duplicate.deduplicated, true);
  assert.equal(calls.length, call_count);
});

test('an unknown duplicate rereads and revalidates session ownership before reconciliation', async () => {
  const timeout = Object.assign(new Error('request timed out'), { name: 'TimeoutError' });
  let current_session = { accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' };
  const { executor, calls } = harness({
    change_price: async () => { throw timeout; },
    after: order({ payment: 2_000 }),
    session: () => current_session,
  });
  const request = { provider_event: providerEvent(), quote_snapshot: quoteSnapshot() };
  const first = await executor.execute(request);
  current_session = { accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-2' };
  const duplicate = await executor.execute(request);

  assert.equal(first.status, 'unknown');
  assert.equal(duplicate.status, 'skipped');
  assert.equal(duplicate.reason_code, 'chat_id_session_ownership_mismatch');
  assert.equal(calls.filter(([name]) => name === 'orders.change_price').length, 1);
});

test('a duplicate after a crash between provider call and receipt persistence reconciles without retry', async () => {
  const store = new FakeReceiptStore();
  const quote = quoteSnapshot();
  const idempotency_key = buildPriceChangeIdempotencyKey(quote);
  store.records.set(idempotency_key, {
    schema_version: 'executor_next.price_change_receipt.v1',
    idempotency_key,
    quote_snapshot: structuredClone(quote),
    status: 'started',
    phase: 'provider_call_started',
    action_attempted: true,
    provider_receipt: null,
    audit_reason: 'provider_receipt_pending',
    result: null,
  });
  const { executor, calls } = harness({
    before: order({ payment: 2_900 }),
    after: order({ payment: 2_900 }),
    receipt_store: store,
  });
  const result = await executor.execute({ provider_event: providerEvent(), quote_snapshot: quote });
  const receipt = store.records.get(idempotency_key);

  assert.equal(result.status, 'succeeded');
  assert.equal(result.reason_code, 'idempotent_readback_verified');
  assert.equal(result.audit_reason, 'provider_receipt_unavailable_due_to_crash');
  assert.equal(result.prior_action_attempted, true);
  assert.equal(receipt.action_attempted, true);
  assert.equal(receipt.audit_reason, 'provider_receipt_unavailable_due_to_crash');
  assert.equal(calls.some(([name]) => name === 'orders.change_price'), false);
  assert.deepEqual(calls.map(([name]) => name), ['orders.get', 'im.get_session_by_order']);
});
