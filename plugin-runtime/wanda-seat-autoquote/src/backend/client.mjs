import { createFulfillmentStatusClient, createOrderEventClient } from './order-event-client.mjs';

export class V2BackendRequestError extends Error {
  constructor(status, code) { super(`wanda ai v2 backend failed: ${status}${code ? ` (${code})` : ''}`); this.status = status; this.code = code; }
}

function join(base, path) { return `${base.replace(/\/$/, '')}${path}`; }

export function createV2BackendClient(config, { fetchImpl = globalThis.fetch, logger = console } = {}) {
  const orderEventClient = createOrderEventClient({
    url: config.orderEventOutbox?.url,
    secret: config.orderEventOutbox?.sharedSecret,
    bridgeKey: config.orderEventOutbox?.bridgeKey,
    fetchImpl, timeoutMs: config.requestTimeoutMs, logger,
  });
  const fulfillmentStatusClient = createFulfillmentStatusClient({
    url: config.orderEventOutbox?.statusUrl,
    secret: config.orderEventOutbox?.sharedSecret,
    bridgeKey: config.orderEventOutbox?.bridgeKey,
    fetchImpl, timeoutMs: config.requestTimeoutMs, logger,
  });
  async function request(path, { method = 'POST', tenantId, eventId, body, timeoutMs = config.requestTimeoutMs, retry = false } = {}) {
    for (let attempt = 0; attempt < (retry ? 3 : 1); attempt += 1) {
      try {
        const response = await fetchImpl(join(config.backend.baseUrl, path), {
          method,
          headers: {
            'content-type': 'application/json',
            'x-wanda-ai-v2-bridge-key': config.backend.sharedSecret,
            ...(tenantId ? { 'x-yumaiduo-tenant-id': String(tenantId) } : {}),
            ...(eventId ? { 'idempotency-key': String(eventId) } : {}),
          },
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: AbortSignal.timeout(timeoutMs),
        });
        const payload = await response.json().catch(() => null);
        if (response.ok) return payload ?? {};
        logger.warn('v2 backend request failed', { path, status: response.status, code: payload?.code ?? payload?.detail, attempt });
        if (!retry || ![429, 502, 503, 504].includes(response.status) || attempt === 2) throw new V2BackendRequestError(response.status, payload?.code ?? payload?.detail);
      } catch (error) {
        if (!retry || error instanceof V2BackendRequestError || attempt === 2) throw error;
        logger.warn('v2 backend request retrying', { path, attempt, error });
      }
      await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
    }
    throw new Error('unreachable backend retry state');
  }

  async function recognizeFulfillmentImage({ tenantId, imageUrl }) {
    const response = await fetchImpl(join(config.v4BackendUrl, '/api/ticket-images/recognize'), {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-wanda-tenant-id': String(tenantId ?? ''),
      },
      body: JSON.stringify({ image_url: String(imageUrl ?? '') }),
      signal: AbortSignal.timeout(config.requestTimeoutMs),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new V2BackendRequestError(response.status, payload?.error?.code ?? payload?.detail ?? 'ticket_image_recognition_failed');
    if (!payload?.data || typeof payload.data !== 'object') throw new V2BackendRequestError(502, 'ticket_image_recognition_invalid');
    return payload.data;
  }

  async function fetchKeywordImage({ tenantId, assetId }) {
    const response = await fetchImpl(join(
      config.backend.baseUrl,
      `/api/wanda-ai-v2/plugin/keyword-images/${encodeURIComponent(String(assetId ?? ''))}`,
    ), {
      method: 'GET',
      headers: {
        'x-wanda-ai-v2-bridge-key': config.backend.sharedSecret,
        'x-yumaiduo-tenant-id': String(tenantId ?? ''),
      },
      signal: AbortSignal.timeout(config.requestTimeoutMs),
    });
    if (!response.ok) throw new V2BackendRequestError(response.status, 'keyword_image_fetch_failed');
    const data = new Uint8Array(await response.arrayBuffer());
    const contentType = String(response.headers.get('content-type') ?? '').split(';', 1)[0].toLowerCase();
    if (!['image/jpeg', 'image/png', 'image/webp', 'image/gif'].includes(contentType) || !data.length || data.length > 5 * 1024 * 1024) {
      throw new V2BackendRequestError(422, 'keyword_image_invalid');
    }
    return {
      data,
      contentType,
      filename: response.headers.get('x-keyword-image-filename') || 'keyword-image',
      sha256: response.headers.get('x-keyword-image-sha256') || null,
    };
  }

  return Object.freeze({
    syncShops: (tenantId, shops) => request(config.backend.shopsPath, { tenantId, body: { tenant_id: String(tenantId), shops } }),
    // `order` is already the normalized authoritative snapshot from the
    // executor runtime; callers must never pass the provider raw object.
    processEvent: ({ envelope, session, order, recentMessages }) => request(config.backend.processPath, {
      tenantId: envelope.tenantId,
      eventId: envelope.id,
      body: { envelope, session, order: order ?? null, recent_messages: recentMessages ?? [] },
      retry: true,
    }),
    claimCommands: (limit = 10) => request('/api/wanda-ai-v2/plugin/commands/claim', { body: { limit }, retry: true }),
    reportCommand: ({ commandId, leaseToken, result }) => request(`/api/wanda-ai-v2/plugin/commands/${encodeURIComponent(commandId)}/result`, { eventId: commandId, body: { lease_token: leaseToken, result }, retry: true }),
    sendOrderEvent: orderEventClient
      ? (payload, idempotencyKey) => orderEventClient.send(payload, idempotencyKey)
      : undefined,
    sendFulfillmentStatus: fulfillmentStatusClient
      ? (payload) => fulfillmentStatusClient.send(payload)
      : undefined,
    claimReminders: (limit = 10) => request('/api/wanda-ai-v2/plugin/reminders/claim', { body: { limit }, retry: true }),
    completeReminder: (taskId, leaseToken, result) => request(`/api/wanda-ai-v2/plugin/reminders/${encodeURIComponent(taskId)}/complete`, { body: { lease_token: leaseToken, result }, retry: true }),
    failReminder: (taskId, leaseToken, reason) => request(`/api/wanda-ai-v2/plugin/reminders/${encodeURIComponent(taskId)}/fail`, { body: { lease_token: leaseToken, reason }, retry: true }),
    fetchKeywordImage,
    recognizeFulfillmentImage,
  });
}
