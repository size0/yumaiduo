export class BackendRequestError extends Error {
  constructor(message, { status = null, code = null, retryable = false } = {}) {
    super(message);
    this.name = 'BackendRequestError';
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

function buildUrl(baseUrl, requestPath) {
  if (typeof requestPath !== 'string' || !requestPath.startsWith('/') || requestPath.startsWith('//')) {
    throw new TypeError('backend request path must be an absolute path');
  }
  return `${baseUrl.replace(/\/$/, '')}${requestPath}`;
}

async function parseBody(response) {
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function createBackendClient(config, {
  fetchImpl = globalThis.fetch,
  logger = { debug() {}, warn() {} },
} = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');
  const backend = config?.backend ?? config;
  if (!backend?.baseUrl) throw new TypeError('backend baseUrl is required');
  const timeoutMs = config?.requestTimeoutMs ?? backend.requestTimeoutMs ?? 10_000;

  async function request(requestPath, {
    method = 'GET',
    body,
    tenantId,
    eventId,
    timeoutMs: requestTimeoutMs = timeoutMs,
  } = {}) {
    const normalizedMethod = String(method).toUpperCase();
    const headers = { accept: 'application/json' };
    let serializedBody;
    if (body !== undefined) {
      headers['content-type'] = 'application/json';
      serializedBody = JSON.stringify(body);
    }
    headers['x-plugin-bridge-key'] = backend.bridgeKey;
    if (tenantId !== undefined && tenantId !== null) headers['x-yumaiduo-tenant-id'] = String(tenantId);
    if (eventId !== undefined && eventId !== null) headers['x-yumaiduo-event-id'] = String(eventId);

    let response;
    try {
      response = await fetchImpl(buildUrl(backend.baseUrl, requestPath), {
        method: normalizedMethod,
        headers,
        body: serializedBody,
        signal: AbortSignal.timeout(requestTimeoutMs),
      });
    } catch (cause) {
      throw new BackendRequestError('backend request failed before receiving a response', {
        code: cause?.name === 'TimeoutError' ? 'BACKEND_TIMEOUT' : 'BACKEND_UNAVAILABLE',
        retryable: true,
      });
    }

    const payload = await parseBody(response);
    if (!response.ok) {
      const code = backendErrorCode(payload);
      logger.warn('backend request rejected', {
        path: requestPath,
        method: normalizedMethod,
        status: response.status,
        tenantId,
        eventId,
      });
      throw new BackendRequestError(
        `backend request failed with HTTP ${response.status}${code ? ` (${code})` : ''}`,
        {
        status: response.status,
        code,
        retryable: response.status === 408 || response.status === 429 || response.status >= 500,
        },
      );
    }
    logger.debug('backend request completed', {
      path: requestPath,
      method: normalizedMethod,
      status: response.status,
      tenantId,
      eventId,
    });
    return payload;
  }

  async function requestMultipart(requestPath, {
    form,
    tenantId,
    eventId,
    timeoutMs: requestTimeoutMs = timeoutMs,
  } = {}) {
    if (!(form instanceof FormData)) throw new TypeError('multipart form data is required');
    const headers = {
      accept: 'application/json',
      'x-plugin-bridge-key': backend.bridgeKey,
    };
    if (tenantId !== undefined && tenantId !== null) headers['x-yumaiduo-tenant-id'] = String(tenantId);
    if (eventId !== undefined && eventId !== null) headers['x-yumaiduo-event-id'] = String(eventId);

    let response;
    try {
      response = await fetchImpl(buildUrl(backend.baseUrl, requestPath), {
        method: 'POST',
        headers,
        body: form,
        signal: AbortSignal.timeout(requestTimeoutMs),
      });
    } catch (cause) {
      throw new BackendRequestError('backend multipart request failed before receiving a response', {
        code: cause?.name === 'TimeoutError' ? 'BACKEND_TIMEOUT' : 'BACKEND_UNAVAILABLE',
        retryable: true,
      });
    }

    const payload = await parseBody(response);
    if (!response.ok) {
      const code = backendErrorCode(payload);
      logger.warn('backend multipart request rejected', {
        path: requestPath,
        status: response.status,
        tenantId,
        eventId,
      });
      throw new BackendRequestError(
        `backend multipart request failed with HTTP ${response.status}${code ? ` (${code})` : ''}`,
        { status: response.status, code, retryable: response.status === 408 || response.status === 429 || response.status >= 500 },
      );
    }
    return payload;
  }

  function upsertOrder(envelope) {
    if (!envelope?.id || envelope.tenantId === undefined || !envelope.event) {
      throw new TypeError('event envelope requires id, tenantId, and event');
    }
    const payload = envelope.payload ?? {};
    const orderId = stringOrEmpty(payload.orderId ?? payload.order_id);
    const conversationId = stringOrEmpty(payload.chatId ?? payload.chat_id ?? payload.peerUnb ?? payload.peer_unb);
    return request(backend.paths.upsertOrder, {
      method: 'POST',
      body: {
        tenant_id: String(envelope.tenantId),
        external_order_id: conversationId ? `chat:${conversationId}` : `order:${orderId || envelope.id}`,
        platform_order_id: orderId,
        event_id: String(envelope.id),
        event_type: String(envelope.event),
        payload,
      },
      tenantId: envelope.tenantId,
      eventId: envelope.id,
    });
  }

  function matchShowtime(input, context = {}) {
    return request(backend.paths.matchShowtime, {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
    });
  }

  function submitRecognition(taskId, input, context = {}) {
    return request(taskPath(backend.paths.taskBase, taskId, 'recognition'), {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
    });
  }

  function submitOcrRecognition(taskId, input, context = {}) {
    return request(taskPath(backend.paths.taskBase, taskId, 'ocr-recognition'), {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
    });
  }

  function submitAiVisionRecognition(taskId, input, context = {}) {
    return request(taskPath(backend.paths.taskBase, taskId, 'ai-vision-recognition'), {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
    });
  }

  function runAgent(input, context = {}) {
    return request(backend.paths.agentRun, {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
    });
  }

  function updateTaskStatus(taskId, input, context = {}) {
    return request(taskPath(backend.paths.taskBase, taskId, 'status'), {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
    });
  }

  function markAiReplySent(eventId, input, context = {}) {
    return request(`/api/xianyu-plugin/bridge/ai-replies/${encodeURIComponent(String(eventId))}/sent`, {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
    });
  }

  function getQuotePolicy(tenantId) {
    const query = new URLSearchParams({ tenant_id: String(tenantId) });
    return request(`${backend.paths.quotePolicy}?${query.toString()}`, { tenantId });
  }

  function updateQuotePolicy(tenantId, policy) {
    return request(backend.paths.quotePolicy, {
      method: 'PUT',
      body: { tenant_id: String(tenantId), ...policy },
      tenantId,
    });
  }

  function getRuntimeSettings(accountUnb = '') {
    const account = String(accountUnb ?? '').trim();
    if (!account) return request(backend.paths.runtimeSettings);
    const query = new URLSearchParams({ account_unb: account });
    return request(`/api/xianyu-plugin/bridge/settings?${query.toString()}`);
  }

  function updateRuntimeSettings(settings) {
    return request(backend.paths.runtimeSettings, { method: 'PUT', body: settings });
  }

  function listKnowledgeBase(tenantId) {
    return request('/api/xianyu-plugin/bridge/knowledge-base', { tenantId });
  }
  function createKnowledgeEntry(tenantId, entry) {
    return request('/api/xianyu-plugin/bridge/knowledge-base', { method: 'POST', body: entry, tenantId });
  }
  function updateKnowledgeEntry(tenantId, id, entry) {
    return request(`/api/xianyu-plugin/bridge/knowledge-base/${encodeURIComponent(id)}`, { method: 'PUT', body: entry, tenantId });
  }
  function listCorrections(tenantId) {
    return request('/api/xianyu-plugin/bridge/corrections', { tenantId });
  }
  function createCorrection(tenantId, input) {
    return request('/api/xianyu-plugin/bridge/corrections', { method: 'POST', body: input, tenantId });
  }
  function reviewCorrection(tenantId, id, input) {
    return request(`/api/xianyu-plugin/bridge/corrections/${encodeURIComponent(id)}`, { method: 'PUT', body: input, tenantId });
  }
  function recordConversationExperience(input, context = {}) {
    return request('/api/xianyu-plugin/bridge/conversation-experiences', {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
    });
  }

  function updateShopSettings(accountUnb, automationEnabled) {
    const account = String(accountUnb ?? '').trim();
    if (!account) throw new TypeError('accountUnb is required');
    if (typeof automationEnabled !== 'boolean') throw new TypeError('automationEnabled must be a boolean');
    return request('/api/xianyu-plugin/bridge/shop-settings', {
      method: 'PUT',
      body: { account_unb: account, automation_enabled: automationEnabled },
    });
  }

  function syncShops(tenantId, shops) {
    if (!Array.isArray(shops)) throw new TypeError('shops must be an array');
    return request(backend.paths.shopDirectorySync, {
      method: 'POST',
      body: { tenant_id: String(tenantId), shops },
      tenantId,
    });
  }

  function previewAiVision(input, context = {}) {
    return request(backend.paths.aiVisionPreview, {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
      timeoutMs: 75_000,
    });
  }

  function uploadTestImage({ bytes, contentType, filename }, context = {}) {
    const type = String(contentType ?? '').split(';', 1)[0].trim().toLowerCase();
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(type)) {
      throw new TypeError('unsupported test image type');
    }
    if (!bytes || Number(bytes.length ?? bytes.byteLength ?? 0) < 1) {
      throw new TypeError('test image bytes are required');
    }
    const form = new FormData();
    form.append('image', new Blob([bytes], { type }), String(filename ?? 'seat-map-image'));
    return requestMultipart(backend.paths.storageUpload, {
      form,
      tenantId: context.tenantId,
      eventId: context.eventId,
      timeoutMs: 75_000,
    });
  }

  function uploadReplyImage({ bytes, contentType, filename }, context = {}) {
    const type = String(contentType ?? '').split(';', 1)[0].trim().toLowerCase();
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(type)) {
      throw new TypeError('unsupported reply image type');
    }
    if (!bytes || Number(bytes.length ?? bytes.byteLength ?? 0) < 1) {
      throw new TypeError('reply image bytes are required');
    }
    const form = new FormData();
    form.append('image', new Blob([bytes], { type }), String(filename ?? 'reply-image'));
    return requestMultipart('/api/storage/reply-images', {
      form,
      tenantId: context.tenantId,
      eventId: context.eventId,
      timeoutMs: 75_000,
    });
  }

  function recognizeTestImage(input, context = {}) {
    return request(backend.paths.visionRecognize, {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
      timeoutMs: 75_000,
    });
  }

  function quoteTestImage(input, context = {}) {
    return request(backend.paths.realtimeQuote, {
      method: 'POST',
      body: input,
      tenantId: context.tenantId,
      eventId: context.eventId,
      // The platform gateway has a shorter deadline than a full live quote.
      // The recognition result is useful on its own, so do not make the UI
      // wait long enough for the gateway to discard it.
      timeoutMs: context.timeoutMs ?? 75_000,
    });
  }

  return Object.freeze({
    request,
    upsertOrder,
    matchShowtime,
    submitRecognition,
    submitOcrRecognition,
    submitAiVisionRecognition,
    runAgent,
    updateTaskStatus,
    markAiReplySent,
    getQuotePolicy,
    updateQuotePolicy,
    getRuntimeSettings,
    updateRuntimeSettings,
    listKnowledgeBase,
    createKnowledgeEntry,
    updateKnowledgeEntry,
    listCorrections,
    createCorrection,
    reviewCorrection,
    recordConversationExperience,
    updateShopSettings,
    syncShops,
    previewAiVision,
    uploadTestImage,
    uploadReplyImage,
    recognizeTestImage,
    quoteTestImage,
  });
}

function backendErrorCode(payload) {
  const candidate = payload?.code ?? payload?.error ?? payload?.detail ?? null;
  if (typeof candidate !== 'string') return null;
  const code = candidate.trim();
  return /^[A-Za-z0-9_.-]{1,120}$/u.test(code) ? code : null;
}

function taskPath(basePath, taskId, suffix) {
  const id = String(taskId ?? '').trim();
  if (!/^[A-Za-z0-9_-]{1,128}$/u.test(id)) throw new TypeError('taskId is invalid');
  return `${basePath.replace(/\/$/u, '')}/${encodeURIComponent(id)}/${suffix}`;
}

function stringOrEmpty(value) {
  return String(value ?? '').trim();
}
