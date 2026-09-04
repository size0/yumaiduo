import { createHash } from 'node:crypto';

const UNPAID_STATUSES = new Set([
  '1',
  'created',
  'pending',
  'wait_pay',
  'waiting_payment',
  'awaiting_payment',
  'price_changed',
  '待付款',
  '已改价',
]);

const PAID_OR_CLOSED_STATUSES = new Set([
  '2',
  '3',
  '4',
  'paid',
  'payment_success',
  'shipped',
  'completed',
  'finished',
  'closed',
  'cancelled',
  'canceled',
  'refunding',
  'refunded',
  'refund_pending',
  '已付款',
  '已发货',
  '交易关闭',
  '退款中',
  '退款成功',
]);

const NO_REFUND_STATUSES = new Set(['', '0', 'none', 'no_refund', 'not_refunded', 'false']);

export class ExecutorContractError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = 'ExecutorContractError';
    this.code = code;
  }
}

function own(object, key) {
  return object != null && typeof object === 'object' && Object.hasOwn(object, key);
}

function atPath(object, path) {
  let current = object;
  for (const key of path) {
    if (!own(current, key)) return { found: false, value: undefined };
    current = current[key];
  }
  return { found: true, value: current };
}

function pick(object, candidates, { allow_null = false } = {}) {
  let null_candidate = null;
  for (const candidate of candidates) {
    const path = candidate.split('.');
    const result = atPath(object, path);
    if (!result.found || result.value === undefined) continue;
    if (result.value === null || String(result.value).trim() === '') {
      if (allow_null && !null_candidate) null_candidate = { value: result.value, provider_field: candidate };
      continue;
    }
    return { value: result.value, provider_field: candidate };
  }
  return null_candidate ?? { value: null, provider_field: null };
}

function text(value) {
  const normalized = String(value ?? '').trim();
  return normalized || null;
}

function integerCents(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'boolean') return null;
  if (typeof value === 'number') return Number.isSafeInteger(value) && value >= 0 ? value : null;
  if (typeof value !== 'string') return null;
  const normalized = value.trim();
  if (!/^\d+$/.test(normalized)) return null;
  const number = Number(normalized);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

function summaryValue(value) {
  if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) return value;
  if (Array.isArray(value)) return `[array:${value.length}]`;
  return '[object]';
}

function rawSummary(raw, selected) {
  return {
    raw_field_names: raw && typeof raw === 'object' ? Object.keys(raw).sort() : [],
    selected_fields: selected
      .filter((item) => item.provider_field)
      .map((item) => ({ provider_field: item.provider_field, value: summaryValue(item.value) })),
  };
}

function sourceEntry(source, picked, fallback = null) {
  if (picked.provider_field) return { source, provider_field: picked.provider_field };
  return fallback;
}

function requireText(object, key, code) {
  const value = text(object?.[key]);
  if (!value) throw new ExecutorContractError(code);
  return value;
}

export function normalizeInboundEvent(provider_event) {
  if (!provider_event || typeof provider_event !== 'object' || Array.isArray(provider_event)) {
    throw new ExecutorContractError('invalid_provider_event');
  }

  const event_id = pick(provider_event, ['id', 'eventId', 'event_id']);
  const tenant_id = pick(provider_event, ['tenantId', 'tenant_id']);
  const event_type = pick(provider_event, ['event', 'eventType', 'event_type']);
  const occurred_at = pick(provider_event, ['ts', 'occurredAtMs', 'occurred_at_ms']);
  const order_id = pick(provider_event, [
    'payload.orderId',
    'payload.order_id',
    'payload.platformOrderId',
    'payload.platform_order_id',
    'payload.order.orderId',
    'payload.order.order_id',
  ]);
  const shop_id = pick(provider_event, ['payload.accountUnb', 'payload.account_unb', 'payload.shopId', 'payload.shop_id']);
  const buyer_id = pick(provider_event, ['payload.peerUnb', 'payload.peer_unb', 'payload.buyerUnb', 'payload.buyer_unb']);
  const chat_id = pick(provider_event, ['payload.chatId', 'payload.chat_id']);
  const timestamp = Number(occurred_at.value);

  if (!text(event_id.value)) throw new ExecutorContractError('event_id_required');
  if (!text(tenant_id.value)) throw new ExecutorContractError('event_tenant_id_required');
  if (!text(event_type.value)) throw new ExecutorContractError('event_type_required');
  if (!Number.isFinite(timestamp)) throw new ExecutorContractError('event_timestamp_invalid');

  const selected = [event_id, tenant_id, event_type, occurred_at, order_id, shop_id, buyer_id, chat_id];
  return {
    schema_version: 'executor_next.inbound_event.v1',
    event_id: text(event_id.value),
    tenant_id: text(tenant_id.value),
    event_type: text(event_type.value),
    occurred_at_ms: timestamp,
    order_id: text(order_id.value),
    shop_id: text(shop_id.value),
    buyer_id: text(buyer_id.value),
    chat_id: text(chat_id.value),
    field_sources: {
      event_id: sourceEntry('provider_event', event_id),
      tenant_id: sourceEntry('provider_event', tenant_id),
      event_type: sourceEntry('provider_event', event_type),
      occurred_at_ms: sourceEntry('provider_event', occurred_at),
      order_id: sourceEntry('provider_event', order_id),
      shop_id: sourceEntry('provider_event', shop_id),
      buyer_id: sourceEntry('provider_event', buyer_id),
      chat_id: sourceEntry('provider_event', chat_id),
    },
    provider_raw_summary: rawSummary(provider_event, selected),
  };
}

export function normalizeQuoteSnapshot(quote_snapshot) {
  if (!quote_snapshot || typeof quote_snapshot !== 'object' || Array.isArray(quote_snapshot)) {
    throw new ExecutorContractError('invalid_quote_snapshot');
  }
  const camel_case_fields = ['orderId', 'quoteVersion', 'targetAmountCents', 'tenantId', 'shopId', 'buyerId', 'chatId'];
  if (camel_case_fields.some((field) => own(quote_snapshot, field))) {
    throw new ExecutorContractError('quote_snapshot_requires_snake_case');
  }

  const target_amount_cents = integerCents(quote_snapshot.target_amount_cents);
  if (!Number.isSafeInteger(target_amount_cents) || target_amount_cents <= 0 || target_amount_cents > 200_000) {
    throw new ExecutorContractError('target_amount_cents_invalid');
  }

  const normalized = {
    schema_version: 'executor_next.quote_snapshot.v1',
    quote_version: requireText(quote_snapshot, 'quote_version', 'quote_version_required'),
    order_id: requireText(quote_snapshot, 'order_id', 'quote_order_id_required'),
    tenant_id: requireText(quote_snapshot, 'tenant_id', 'quote_tenant_id_required'),
    shop_id: requireText(quote_snapshot, 'shop_id', 'quote_shop_id_required'),
    buyer_id: requireText(quote_snapshot, 'buyer_id', 'quote_buyer_id_required'),
    chat_id: requireText(quote_snapshot, 'chat_id', 'quote_chat_id_required'),
    target_amount_cents,
  };
  if (quote_snapshot.flow_version === 'V4_NEW_FLOW_V2') {
    normalized.flow_version = quote_snapshot.flow_version;
    normalized.source = requireText(quote_snapshot, 'source', 'quote_source_required');
    if (normalized.source !== 'phase_9a_authorization') {
      throw new ExecutorContractError('quote_source_invalid');
    }
    normalized.quote_id = requireText(quote_snapshot, 'quote_id', 'quote_id_required');
    normalized.quote_hash = requireText(quote_snapshot, 'quote_hash', 'quote_hash_required');
    normalized.terms_fingerprint = normalized.quote_hash;
    normalized.quote_generation = integerCents(quote_snapshot.quote_generation);
    normalized.binding_revision = integerCents(quote_snapshot.binding_revision);
    normalized.transaction_revision = integerCents(quote_snapshot.transaction_revision);
    normalized.idempotency_key = requireText(quote_snapshot, 'idempotency_key', 'quote_idempotency_key_required');
    if (!Number.isSafeInteger(normalized.quote_generation) || normalized.quote_generation < 1) {
      throw new ExecutorContractError('quote_generation_invalid');
    }
    if (!Number.isSafeInteger(normalized.binding_revision) || normalized.binding_revision < 1) {
      throw new ExecutorContractError('binding_revision_invalid');
    }
    if (!Number.isSafeInteger(normalized.transaction_revision) || normalized.transaction_revision < 0) {
      throw new ExecutorContractError('transaction_revision_invalid');
    }
  }
  if (own(quote_snapshot, 'observed_order_amount_cents')) {
    const observedAmount = integerCents(quote_snapshot.observed_order_amount_cents);
    if (!Number.isSafeInteger(observedAmount) || observedAmount <= 0 || observedAmount > 200_000) {
      throw new ExecutorContractError('observed_order_amount_cents_invalid');
    }
    normalized.observed_order_amount_cents = observedAmount;
  }
  const durableFields = ['quote_record_id', 'confirmation_version', 'quote_expires_at', 'confirmed_ticket_count'];
  const hasDurableBinding = durableFields.some((field) => own(quote_snapshot, field));
  if (hasDurableBinding) {
    normalized.quote_record_id = requireText(quote_snapshot, 'quote_record_id', 'quote_record_id_required');
    normalized.confirmation_version = requireText(quote_snapshot, 'confirmation_version', 'confirmation_version_required');
    normalized.quote_expires_at = requireText(quote_snapshot, 'quote_expires_at', 'quote_expires_at_required');
    if (!Number.isFinite(Date.parse(normalized.quote_expires_at))) throw new ExecutorContractError('quote_expires_at_invalid');
    const confirmedCount = integerCents(quote_snapshot.confirmed_ticket_count);
    if (!Number.isSafeInteger(confirmedCount) || confirmedCount < 1 || confirmedCount > 20) {
      throw new ExecutorContractError('confirmed_ticket_count_invalid');
    }
    normalized.confirmed_ticket_count = confirmedCount;
  }
  if (normalized.flow_version === 'V4_NEW_FLOW_V2') {
    const expectedKey = buildPriceChangeIdempotencyKey(normalized);
    if (normalized.idempotency_key !== expectedKey) {
      throw new ExecutorContractError('idempotency_key_mismatch');
    }
  }
  normalized.field_sources = Object.fromEntries(
    Object.keys(normalized)
      .filter((key) => key !== 'schema_version')
      .map((key) => [key, { source: 'backend_quote_snapshot', field: key }]),
  );
  return normalized;
}

export function normalizeAuthoritativeOrder(provider_order, { sdk_tenant_id } = {}) {
  if (!provider_order || typeof provider_order !== 'object' || Array.isArray(provider_order)) {
    throw new ExecutorContractError('authoritative_order_missing');
  }

  const order_id = pick(provider_order, ['orderId', 'order_id', 'platformOrderId', 'platform_order_id']);
  const provider_tenant_id = pick(provider_order, ['tenantId', 'tenant_id']);
  const shop_id = pick(provider_order, ['accountUnb', 'account_unb', 'shopId', 'shop_id']);
  const buyer_id = pick(provider_order, ['buyerUnb', 'buyer_unb', 'peerUnb', 'peer_unb', 'buyerId', 'buyer_id']);
  const chat_id = pick(provider_order, ['chatId', 'chat_id']);
  const status = pick(provider_order, ['orderStatus', 'order_status', 'status']);
  const paid_at = pick(provider_order, ['payTime', 'pay_time', 'paidAt', 'paid_at', 'paymentTime', 'payment_time'], { allow_null: true });
  const amount = pick(provider_order, ['payment', 'paymentCents', 'payment_cents', 'priceFee', 'price_fee', 'totalAmountCents', 'total_amount_cents']);
  const post_fee = pick(provider_order, ['postFee', 'post_fee'], { allow_null: true });
  const refund_status = pick(provider_order, ['refundStatus', 'refund_status', 'refundState', 'refund_state'], { allow_null: true });
  const quantity = pick(provider_order, ['quantity', 'ticketCount', 'ticket_count', 'num']);
  const selected = [order_id, provider_tenant_id, shop_id, buyer_id, chat_id, status, paid_at, amount, post_fee, refund_status, quantity];
  const tenant_id = text(provider_tenant_id.value) ?? text(sdk_tenant_id);

  return {
    schema_version: 'executor_next.authoritative_order.v1',
    order_id: text(order_id.value),
    tenant_id,
    shop_id: text(shop_id.value),
    buyer_id: text(buyer_id.value),
    chat_id: text(chat_id.value),
    order_status: text(status.value)?.toLowerCase() ?? null,
    paid_at: text(paid_at.value),
    amount_cents: integerCents(amount.value),
    post_fee_cents: integerCents(post_fee.value),
    refund_status: text(refund_status.value)?.toLowerCase() ?? null,
    quantity: integerCents(quantity.value),
    field_sources: {
      order_id: sourceEntry('provider_order', order_id),
      tenant_id: sourceEntry('provider_order', provider_tenant_id, text(sdk_tenant_id) ? { source: 'sdk_client_context', field: 'tenant_id' } : null),
      shop_id: sourceEntry('provider_order', shop_id),
      buyer_id: sourceEntry('provider_order', buyer_id),
      chat_id: sourceEntry('provider_order', chat_id),
      order_status: sourceEntry('provider_order', status),
      paid_at: sourceEntry('provider_order', paid_at),
      amount_cents: sourceEntry('provider_order', amount),
      post_fee_cents: sourceEntry('provider_order', post_fee),
      refund_status: sourceEntry('provider_order', refund_status),
      quantity: sourceEntry('provider_order', quantity),
    },
    provider_raw_summary: rawSummary(provider_order, selected),
  };
}

export function normalizeAuthoritativeSession(provider_session) {
  if (!provider_session || typeof provider_session !== 'object' || Array.isArray(provider_session)) return null;
  const shop_id = pick(provider_session, ['accountUnb', 'account_unb', 'shopId', 'shop_id']);
  const buyer_id = pick(provider_session, ['peerUnb', 'peer_unb', 'buyerUnb', 'buyer_unb']);
  const chat_id = pick(provider_session, ['chatId', 'chat_id']);
  const selected = [shop_id, buyer_id, chat_id];
  return {
    schema_version: 'executor_next.authoritative_session.v1',
    shop_id: text(shop_id.value),
    buyer_id: text(buyer_id.value),
    chat_id: text(chat_id.value),
    field_sources: {
      shop_id: sourceEntry('provider_session', shop_id),
      buyer_id: sourceEntry('provider_session', buyer_id),
      chat_id: sourceEntry('provider_session', chat_id),
    },
    provider_raw_summary: rawSummary(provider_session, selected),
  };
}

export function orderChangeability(order) {
  const status = text(order?.order_status)?.toLowerCase() ?? '';
  const refund_status = text(order?.refund_status)?.toLowerCase() ?? '';
  if (order?.paid_at) return { allowed: false, reason_code: 'order_already_paid' };
  if (refund_status && !NO_REFUND_STATUSES.has(refund_status)) {
    return { allowed: false, reason_code: 'order_refund_in_progress_or_complete' };
  }
  if (PAID_OR_CLOSED_STATUSES.has(status) || /refund|closed|cancel|退款|关闭/.test(status)) {
    return { allowed: false, reason_code: 'order_paid_or_closed' };
  }
  if (!UNPAID_STATUSES.has(status)) return { allowed: false, reason_code: 'order_status_not_changeable' };
  return { allowed: true, reason_code: null };
}

export function canonicalRepriceIdentity(snapshot) {
  const identity = [
    requireText({ platform_order_id: snapshot?.order_id }, 'platform_order_id', 'idempotency_order_id_required'),
    requireText(snapshot, 'quote_id', 'idempotency_quote_id_required'),
    integerCents(snapshot?.quote_generation),
    integerCents(snapshot?.binding_revision),
    integerCents(snapshot?.target_amount_cents),
  ];
  if (!Number.isSafeInteger(identity[2]) || identity[2] < 1) {
    throw new ExecutorContractError('idempotency_quote_generation_invalid');
  }
  if (!Number.isSafeInteger(identity[3]) || identity[3] < 1) {
    throw new ExecutorContractError('idempotency_binding_revision_invalid');
  }
  if (!Number.isSafeInteger(identity[4]) || identity[4] <= 0) {
    throw new ExecutorContractError('idempotency_target_amount_cents_invalid');
  }
  return identity;
}

export function buildPriceChangeIdempotencyKey(snapshot) {
  if (snapshot?.flow_version === 'V4_NEW_FLOW_V2') {
    const material = canonicalRepriceIdentity(snapshot);
    const digest = createHash('sha256').update(JSON.stringify(material)).digest('base64url');
    return `price_change:v1:${digest}`;
  }

  const {
    tenant_id, shop_id, buyer_id, chat_id, order_id, quote_version,
    target_amount_cents, observed_order_amount_cents, quote_record_id, confirmation_version,
  } = snapshot ?? {};
  const material = {
    tenant_id: requireText({ tenant_id }, 'tenant_id', 'idempotency_tenant_id_required'),
    shop_id: requireText({ shop_id }, 'shop_id', 'idempotency_shop_id_required'),
    buyer_id: requireText({ buyer_id }, 'buyer_id', 'idempotency_buyer_id_required'),
    chat_id: requireText({ chat_id }, 'chat_id', 'idempotency_chat_id_required'),
    order_id: requireText({ order_id }, 'order_id', 'idempotency_order_id_required'),
    quote_version: requireText({ quote_version }, 'quote_version', 'idempotency_quote_version_required'),
    target_amount_cents: integerCents(target_amount_cents),
    ...(Number.isSafeInteger(integerCents(observed_order_amount_cents))
      ? { observed_order_amount_cents: integerCents(observed_order_amount_cents) }
      : {}),
    ...(quote_record_id ? { quote_record_id: text(quote_record_id) } : {}),
    ...(confirmation_version ? { confirmation_version: text(confirmation_version) } : {}),
  };
  if (!Number.isSafeInteger(material.target_amount_cents)) {
    throw new ExecutorContractError('idempotency_target_amount_cents_invalid');
  }
  const digest = createHash('sha256').update(JSON.stringify(material)).digest('base64url');
  return `price_change:v1:${digest}`;
}

export function summarizeProviderResult(value, source = 'provider_result') {
  const safe_booleans = ['ok', 'success', 'accepted', 'duplicate'];
  const safe_statuses = new Set(['ok', 'success', 'succeeded', 'accepted', 'completed', 'failed', 'rejected', 'unknown', 'skipped']);
  const selected_fields = [];
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    for (const provider_field of safe_booleans) {
      if (typeof value[provider_field] === 'boolean') {
        selected_fields.push({ provider_field, value: value[provider_field] });
      }
    }
    if (typeof value.status === 'string' && safe_statuses.has(value.status.trim().toLowerCase())) {
      selected_fields.push({ provider_field: 'status', value: value.status.trim().toLowerCase() });
    }
  }
  return {
    source,
    provider_result_received: value !== undefined,
    selected_fields,
    omitted_field_count: value && typeof value === 'object' && !Array.isArray(value)
      ? Math.max(0, Object.keys(value).length - selected_fields.length)
      : value === undefined ? 0 : 1,
  };
}
