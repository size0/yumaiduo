import { createHmac, randomBytes } from 'node:crypto';

export class OrderEventDeliveryError extends Error {
  constructor(message, { status = null, retryable = true } = {}) {
    super(message);
    this.name = 'OrderEventDeliveryError';
    this.status = status;
    this.retryable = retryable;
  }
}

function requiredText(value, field) {
  const normalized = String(value ?? '').trim();
  if (!normalized) throw new TypeError(`${field} is required`);
  return normalized;
}

function sortedValue(value) {
  if (Array.isArray(value)) return value.map((item) => sortedValue(item));
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortedValue(value[key])]));
}

export function canonicalOrderEventBody(value) {
  return JSON.stringify(sortedValue(value));
}

export function orderEventSignature({ secret, tenantId, timestamp, nonce, body }) {
  return createHmac('sha256', requiredText(secret, 'secret'))
    .update(
      `${requiredText(tenantId, 'tenantId')}.${requiredText(timestamp, 'timestamp')}.${requiredText(nonce, 'nonce')}.${String(body ?? '')}`,
      'utf8',
    )
    .digest('hex');
}

function eventBody(payload, idempotencyKey) {
  const source = payload && typeof payload === 'object' ? payload : {};
  const tenantId = requiredText(source.tenant_id ?? source.tenantId, 'payload.tenant_id');
  const sourceOrderId = requiredText(
    source.source_order_id ?? source.sourceOrderId ?? source.order_id ?? source.orderId,
    'payload.source_order_id',
  );
  const eventId = requiredText(source.event_id ?? source.eventId, 'payload.event_id');
  const eventType = requiredText(source.event_type ?? source.eventType, 'payload.event_type');
  return {
    tenant_id: tenantId,
    source: 'wanda',
    source_order_id: sourceOrderId,
    event_id: eventId,
    idempotency_key: requiredText(idempotencyKey, 'idempotencyKey'),
    event_type: eventType,
    payload: source.payload && typeof source.payload === 'object' ? source.payload : source,
  };
}

export function buildOrderEventRequest({ url, secret, bridgeKey, payload, idempotencyKey, timestamp = Math.floor(Date.now() / 1000), nonce = randomBytes(16).toString('hex') }) {
  const target = requiredText(url, 'url');
  const bodyValue = eventBody(payload, idempotencyKey);
  const body = canonicalOrderEventBody(bodyValue);
  const signedTimestamp = String(timestamp);
  const signedNonce = requiredText(nonce, 'nonce');
  const tenantId = bodyValue.tenant_id;
  return {
    url: target,
    body,
    headers: {
      'content-type': 'application/json',
      'X-Plugin-Bridge-Key': requiredText(bridgeKey, 'bridgeKey'),
      'X-Plugin-Tenant-ID': tenantId,
      'X-Plugin-Timestamp': signedTimestamp,
      'X-Plugin-Nonce': signedNonce,
      'X-Plugin-Signature': orderEventSignature({ secret, tenantId, timestamp: signedTimestamp, nonce: signedNonce, body }),
    },
  };
}

export function createOrderEventClient({ url, secret, bridgeKey, fetchImpl = globalThis.fetch, timeoutMs = 15_000, logger = console } = {}) {
  if (!url || !secret || !bridgeKey) return null;
  return Object.freeze({
    async send(payload, idempotencyKey) {
      const request = buildOrderEventRequest({ url, secret, bridgeKey, payload, idempotencyKey });
      let response;
      try {
        response = await fetchImpl(request.url, {
          method: 'POST', headers: request.headers, body: request.body,
          signal: AbortSignal.timeout(timeoutMs),
        });
      } catch (error) {
        logger.warn?.('order event delivery request failed', { error });
        throw new OrderEventDeliveryError('order_event_delivery_network_error', { retryable: true });
      }
      const responseBody = await response.json().catch(() => null);
      if (!response.ok) {
        const retryable = response.status === 408 || response.status === 425 || response.status === 429 || response.status >= 500;
        throw new OrderEventDeliveryError(
          String(responseBody?.error ?? responseBody?.code ?? `order_event_delivery_http_${response.status}`),
          { status: response.status, retryable },
        );
      }
      return { status: response.status, body: responseBody };
    },
  });
}

function fulfillmentStatusBody(payload) {
  const source = payload && typeof payload === 'object' ? payload : {};
  const tenantId = requiredText(source.tenant_id ?? source.tenantId, 'payload.tenant_id');
  const orderId = requiredText(source.source_order_id ?? source.sourceOrderId ?? source.order_id ?? source.orderId, 'payload.source_order_id');
  const eventKey = requiredText(source.status_event_key ?? source.statusEventKey, 'payload.status_event_key');
  const shippingStatus = source.shipping_status ?? source.shippingStatus;
  const messageStatus = source.message_status ?? source.messageStatus;
  const statusVersion = source.status_version ?? source.statusVersion;
  if (shippingStatus == null && messageStatus == null) throw new TypeError('payload.shipping_status or payload.message_status is required');
  if (statusVersion != null && (!Number.isInteger(Number(statusVersion)) || Number(statusVersion) < 0)) {
    throw new TypeError('payload.status_version must be a non-negative integer');
  }
  const body = {
    tenant_id: tenantId, source: 'wanda', source_order_id: orderId, status_event_key: eventKey,
    ...(shippingStatus == null ? {} : { shipping_status: requiredText(shippingStatus, 'payload.shipping_status') }),
    ...(messageStatus == null ? {} : { message_status: requiredText(messageStatus, 'payload.message_status') }),
    ...(statusVersion == null ? {} : { status_version: Number(statusVersion) }),
    response: source.response && typeof source.response === 'object' ? source.response : {},
    error: String(source.error ?? '').slice(0, 2_000),
  };
  return { body, tenantId, orderId, eventKey };
}

export function buildFulfillmentStatusRequest({ url, secret, bridgeKey, payload, timestamp = Math.floor(Date.now() / 1000), nonce = randomBytes(16).toString('hex') }) {
  const { body: bodyValue, tenantId, orderId } = fulfillmentStatusBody(payload);
  const target = requiredText(url, 'url')
    .replaceAll('{source}', 'wanda')
    .replaceAll('{order_id}', encodeURIComponent(orderId));
  const body = canonicalOrderEventBody(bodyValue);
  const signedTimestamp = String(timestamp);
  const signedNonce = requiredText(nonce, 'nonce');
  return {
    url: target,
    body,
    headers: {
      'content-type': 'application/json',
      'X-Plugin-Bridge-Key': requiredText(bridgeKey, 'bridgeKey'),
      'X-Plugin-Tenant-ID': tenantId,
      'X-Plugin-Timestamp': signedTimestamp,
      'X-Plugin-Nonce': signedNonce,
      'X-Plugin-Signature': orderEventSignature({ secret, tenantId, timestamp: signedTimestamp, nonce: signedNonce, body }),
    },
  };
}

export function createFulfillmentStatusClient({ url, secret, bridgeKey, fetchImpl = globalThis.fetch, timeoutMs = 15_000, logger = console } = {}) {
  if (!url || !secret || !bridgeKey) return null;
  return Object.freeze({
    async send(payload) {
      const request = buildFulfillmentStatusRequest({ url, secret, bridgeKey, payload });
      let response;
      try {
        response = await fetchImpl(request.url, {
          method: 'POST', headers: request.headers, body: request.body,
          signal: AbortSignal.timeout(timeoutMs),
        });
      } catch (error) {
        logger.warn?.('fulfillment status delivery request failed', { error });
        throw new OrderEventDeliveryError('fulfillment_status_delivery_network_error', { retryable: true });
      }
      const responseBody = await response.json().catch(() => null);
      if (!response.ok) {
        const retryable = response.status === 408 || response.status === 425 || response.status === 429 || response.status >= 500;
        throw new OrderEventDeliveryError(
          String(responseBody?.error ?? responseBody?.code ?? `fulfillment_status_delivery_http_${response.status}`),
          { status: response.status, retryable },
        );
      }
      const acknowledged = responseBody && typeof responseBody === 'object' && !Array.isArray(responseBody)
        && (responseBody.task && typeof responseBody.task === 'object'
          || responseBody.plugin_callback === 'deferred'
          || responseBody.accepted === true);
      if (!acknowledged) {
        throw new OrderEventDeliveryError('fulfillment_status_delivery_invalid_response', { status: response.status, retryable: true });
      }
      return { status: response.status, body: responseBody };
    },
  });
}
