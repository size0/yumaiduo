import http from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { V4UiJobStore } from '../runtime/v4-ui-job-store.mjs';

const UI_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../ui');
const UI_ASSETS = new Map([
  ['/ui', ['index.html', 'text/html; charset=utf-8']],
  ['/ui/', ['index.html', 'text/html; charset=utf-8']],
  ['/ui/index.html', ['index.html', 'text/html; charset=utf-8']],
  ['/ui/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/ui/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/ui/sdk.js', ['sdk.js', 'text/javascript; charset=utf-8']],
  ['/ui/ui/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/ui/ui/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/ui/ui/sdk.js', ['sdk.js', 'text/javascript; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/sdk.js', ['sdk.js', 'text/javascript; charset=utf-8']],
]);
const UI_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'";

function header(req, name) { const value = req.headers[name]; return Array.isArray(value) ? value[0] : value; }
function securityHeaders() { return { 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer' }; }
function json(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, { ...securityHeaders(), 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body) });
  res.end(body);
}
async function body(req, limit) {
  const chunks = [];
  let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > limit) throw Object.assign(new Error('payload_too_large'), { status: 413 });
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}
async function file(res, filename, mime) {
  try {
    const data = await fs.readFile(path.join(UI_DIR, filename));
    res.writeHead(200, { ...securityHeaders(), 'content-type': mime, 'content-length': data.length, 'content-security-policy': UI_CSP });
    res.end(data);
    return true;
  } catch { return false; }
}
function parseJson(rawBody) {
  try { return JSON.parse(rawBody); }
  catch { throw Object.assign(new Error('invalid_json'), { status: 400, code: 'invalid_json' }); }
}
function secureEqual(left, right) {
  const expected = Buffer.from(String(left ?? ''));
  const actual = Buffer.from(String(right ?? ''));
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}
function wandaOrderApiAuthorized(req, config) {
  if (!config.orderApiKey || !config.orderApiTenantId) return { status: 503, error: 'wanda_order_api_not_configured' };
  const authorization = String(header(req, 'authorization') ?? '');
  const token = authorization.match(/^Bearer\s+(.+)$/i)?.[1]?.trim()
    ?? String(header(req, 'x-wanda-order-api-key') ?? '').trim();
  if (!secureEqual(config.orderApiKey, token)) return { status: 401, error: 'wanda_order_api_unauthorized' };
  return { tenantId: config.orderApiTenantId };
}
function parseOrderLimit(requestUrl) {
  const raw = requestUrl.searchParams.get('limit');
  if (raw === null || /^\s*$/.test(raw)) return 50;
  if (!/^\d+$/.test(raw)) throw Object.assign(new Error('limit_invalid'), { status: 400, code: 'limit_invalid' });
  const limit = Number(raw);
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
    throw Object.assign(new Error('limit_invalid'), { status: 400, code: 'limit_invalid' });
  }
  return limit;
}
function parseImHistoryRequest(requestUrl) {
  const accountUnb = String(requestUrl.searchParams.get('accountUnb') ?? '').trim();
  const chatId = String(requestUrl.searchParams.get('chatId') ?? '').trim();
  if (!accountUnb || accountUnb.length > 160 || !chatId || chatId.length > 160) {
    throw Object.assign(new Error('im_history_identity_invalid'), { status: 400, code: 'im_history_identity_invalid' });
  }
  return { accountUnb, chatId, limit: parseOrderLimit(requestUrl) };
}
function firstMessageValue(message, ...keys) {
  for (const key of keys) {
    const value = message?.[key];
    if (value !== undefined && value !== null && String(value).trim() !== '') return value;
  }
  return null;
}
function messageTimestamp(message) {
  const value = firstMessageValue(message, 'sentAt', 'sent_at', 'createdAt', 'created_at', 'timestamp');
  if (value !== null && Number.isFinite(Number(value)) && String(value).trim().length >= 10) {
    const date = new Date(Number(value));
    if (!Number.isNaN(date.getTime())) return date.toISOString();
  }
  if (value !== null) {
    const date = new Date(String(value));
    if (!Number.isNaN(date.getTime())) return date.toISOString();
  }
  const milliseconds = firstMessageValue(message, 'sentAtMs', 'sent_at_ms');
  if (milliseconds !== null && Number.isFinite(Number(milliseconds))) {
    const date = new Date(Number(milliseconds));
    if (!Number.isNaN(date.getTime())) return date.toISOString();
  }
  return null;
}
function summarizeMessageText(message) {
  const content = message?.content;
  const text = typeof content === 'string'
    ? content
    : firstMessageValue(content, 'text', 'content', 'message')
      ?? firstMessageValue(message, 'text', 'message');
  const normalized = String(text ?? '').replace(/\s+/gu, ' ').trim();
  if (normalized) return normalized.slice(0, 160);
  const messageType = Number(firstMessageValue(message, 'messageType', 'message_type', 'type'));
  return messageType === 2 ? '[图片]' : '';
}
function sanitizeImHistoryMessage(message) {
  const directionValue = String(firstMessageValue(message, 'direction', 'senderType', 'sender_type') ?? '').toLowerCase();
  const direction = ['buyer', 'inbound', 'receive', 'received'].includes(directionValue)
    ? 'buyer'
    : ['seller', 'outbound', 'send', 'sent'].includes(directionValue) ? 'seller' : 'unknown';
  const senderUnb = String(firstMessageValue(message, 'senderUnb', 'sender_unb') ?? '').trim();
  const accountUnb = String(firstMessageValue(message, 'accountUnb', 'account_unb') ?? '').trim();
  const senderClassification = senderUnb && accountUnb && senderUnb === accountUnb
    ? 'self' : direction === 'buyer' ? 'buyer' : direction === 'seller' ? 'seller' : 'unknown';
  const rawType = firstMessageValue(message, 'messageType', 'message_type', 'type');
  const numericType = rawType === null ? null : Number(rawType);
  return {
    messageId: String(firstMessageValue(message, 'messageId', 'message_id', 'remoteMessageId', 'remote_message_id', 'id') ?? ''),
    timestamp: messageTimestamp(message),
    direction,
    senderClassification,
    messageType: Number.isFinite(numericType) ? numericType : null,
    textSummary: summarizeMessageText(message),
  };
}
function gatewayRequest(req, platform, rawBody) {
  return platform.verifyGateway?.({
    pluginId: header(req, 'x-yumaiduo-plugin-id'),
    tenantId: header(req, 'x-yumaiduo-tenant-id'),
    userId: header(req, 'x-yumaiduo-user-id'),
    timestamp: header(req, 'x-yumaiduo-timestamp'),
    signature: header(req, 'x-yumaiduo-signature'),
    rawBody,
  }) === true;
}
function decodeV4FormData(rawBody) {
  const payload = parseJson(rawBody);
  if (payload?._wanda_v4_formdata !== true || !Array.isArray(payload.entries)) return null;
  const form = new FormData();
  for (const entry of payload.entries) {
    if (!entry || typeof entry.name !== 'string') continue;
    if (entry.file && typeof entry.file.base64 === 'string') {
      const bytes = Buffer.from(entry.file.base64, 'base64');
      form.append(entry.name, new Blob([bytes], { type: entry.file.type || 'application/octet-stream' }), entry.file.name || 'upload.bin');
    } else {
      form.append(entry.name, String(entry.value ?? ''));
    }
  }
  return form;
}

async function requestV4({ method, pathname, rawBody, contentType, config, fetchImpl, tenantId, shopId, modelScope }) {
  const targetPath = pathname.slice('/ui/v4'.length);
  if (!targetPath.startsWith('/api/')) return null;
  let requestBody = rawBody || undefined;
  const headers = {
    'x-wanda-tenant-id': String(tenantId),
    ...(shopId ? { 'x-wanda-shop-id': String(shopId) } : {}),
    ...(modelScope ? { 'x-wanda-model-scope': String(modelScope) } : {}),
    ...(config.backend?.sharedSecret ? { 'x-wanda-ai-v2-bridge-key': config.backend.sharedSecret } : {}),
  };
  if (rawBody && /^application\/json(?:\s*;|$)/i.test(contentType ?? '')) {
    const form = decodeV4FormData(rawBody);
    if (form) requestBody = form;
    else headers['content-type'] = 'application/json';
  } else if (contentType && requestBody !== undefined) headers['content-type'] = contentType;
  const response = await fetchImpl(`${config.v4BackendUrl}${targetPath}`, {
    method,
    headers,
    body: ['GET', 'HEAD'].includes(method ?? '') ? undefined : requestBody,
    signal: AbortSignal.timeout(config.requestTimeoutMs),
  });
  return {
    status: response.status,
    contentType: response.headers.get('content-type') ?? 'application/octet-stream',
    body: Buffer.from(await response.arrayBuffer()),
  };
}
function sendV4Result(res, result) {
  if (!result) return json(res, 404, { ok: false, error: 'not_found' });
  res.writeHead(result.status, { ...securityHeaders(), 'content-type': result.contentType, 'content-length': result.body.length });
  res.end(result.body);
}
async function proxyV4(req, res, pathname, rawBody, config, fetchImpl, tenantId, shopId, modelScope) {
  return sendV4Result(res, await requestV4({
    method: req.method, pathname, rawBody, contentType: header(req, 'content-type'), config, fetchImpl, tenantId, shopId, modelScope,
  }));
}

function overview(manifest, snapshot) {
  const registered = snapshot?.registered === true;
  const workerHealthy = snapshot?.runtime?.ok === true;
  return {
    plugin: {
      id: manifest.id,
      name: manifest.name,
      version: manifest.version,
      status: registered && workerHealthy ? 'healthy' : 'degraded',
      updatedAt: snapshot?.registeredAt ?? null,
    },
    services: {
      platform: { label: '鱼麦多平台注册', status: registered ? 'healthy' : 'error' },
      eventWorker: { label: '事件执行器', status: workerHealthy ? 'healthy' : 'error' },
      backendConnector: { label: '万达业务后端连接器', status: 'configured' },
    },
    boundaries: [
      '插件只执行鱼麦多平台事件、回复和权威改价动作。',
      '识图、场次匹配、锁座核价和报价规则由独立业务后端负责。',
      '发送回复前重新读取消息历史，发现人工或新买家消息时停止发送。',
      '改价前校验租户、店铺、买家、会话、订单状态与报价版本。',
    ],
  };
}

export function createV2HttpServer({
  config, platform, enqueue, syncShops, listOrders, getOrder, fulfillOrder, health, logger = console,
  fetchImpl = globalThis.fetch,
  jobStore = new V4UiJobStore(path.join(config.dataDir, 'ui-jobs'), config.encryptionKey),
}) {
  const webhookBase = config.manifest.entrypoint.webhookPath.replace(/\/$/, '');
  let jobsStopped = true;
  let jobDrainRequested = false;
  let jobDrainPromise = null;

  async function drainImageJobs() {
    do {
      jobDrainRequested = false;
      while (!jobsStopped) {
        const job = await jobStore.claim();
        if (!job) break;
        try {
          const result = await requestV4({
            method: 'POST', pathname: '/ui/v4/api/chat/image-messages',
            rawBody: job.rawBody, contentType: job.contentType, config, fetchImpl, tenantId: job.tenantId,
          });
          await jobStore.complete(job.id, job.lease, result);
        } catch (error) {
          logger.error('v4 image job failed', { error, jobId: job.id });
          try { await jobStore.fail(job.id, job.lease); }
          catch (storeError) { logger.error('v4 image job result persistence failed', { error: storeError, jobId: job.id }); }
        }
      }
    } while (!jobsStopped && jobDrainRequested);
  }

  function requestImageJobDrain() {
    if (jobsStopped) return;
    jobDrainRequested = true;
    if (jobDrainPromise) return;
    const drain = drainImageJobs();
    jobDrainPromise = drain;
    void drain.then(
      () => {
        if (jobDrainPromise !== drain) return;
        jobDrainPromise = null;
        if (jobDrainRequested && !jobsStopped) requestImageJobDrain();
      },
      (error) => {
        logger.error('v4 image job drain failed', { error });
        if (jobDrainPromise !== drain) return;
        jobDrainPromise = null;
        if (jobDrainRequested && !jobsStopped) requestImageJobDrain();
      },
    );
  }

  async function startImageJob(rawBody, contentType, tenantId, userId) {
    const job = await jobStore.enqueue({ rawBody, contentType, tenantId, userId });
    requestImageJobDrain();
    return job.id;
  }
  const handler = async (req, res) => {
    const pathname = new URL(req.url ?? '/', 'http://v2.local').pathname;
    try {
      if (req.method === 'GET' && pathname === '/healthz') {
        const snapshot = await health();
        return json(res, snapshot.ok ? 200 : 503, snapshot);
      }

      const wandaOrderPath = pathname.startsWith('/__plugin__/')
        ? pathname.slice('/__plugin__'.length) : pathname;
      const wandaOrderCollection = wandaOrderPath === '/api/wanda/orders';
      const wandaOrderDetail = wandaOrderPath.startsWith('/api/wanda/orders/') && !wandaOrderPath.endsWith('/fulfillment');
      const wandaFulfillment = wandaOrderPath.startsWith('/api/wanda/orders/') && wandaOrderPath.endsWith('/fulfillment');
      if (wandaOrderCollection || wandaOrderDetail || wandaFulfillment) {
        const authorization = wandaOrderApiAuthorized(req, config);
        if (authorization.status) return json(res, authorization.status, { ok: false, error: authorization.error });
        if (wandaOrderCollection) {
          if (req.method !== 'GET') return json(res, 405, { ok: false, error: 'method_not_allowed' });
          if (typeof listOrders !== 'function') return json(res, 503, { ok: false, error: 'wanda_order_list_unavailable' });
          const requestUrl = new URL(req.url ?? '/', 'http://v2.local');
          const result = await listOrders(authorization.tenantId, parseOrderLimit(requestUrl));
          return json(res, 200, { source: 'wanda', ...result });
        }
        const suffix = wandaFulfillment ? '/fulfillment' : '';
        const orderId = decodeURIComponent(wandaOrderPath.slice('/api/wanda/orders/'.length, suffix ? -suffix.length : undefined));
        if (!orderId || orderId.includes('/')) return json(res, 400, { ok: false, error: 'order_id_invalid' });
        if (wandaFulfillment) {
          if (req.method !== 'POST') return json(res, 405, { ok: false, error: 'method_not_allowed' });
          if (config.fulfillmentEnabled !== true) return json(res, 503, { ok: false, error: 'wanda_fulfillment_disabled' });
          if (typeof fulfillOrder !== 'function') return json(res, 503, { ok: false, error: 'wanda_fulfillment_unavailable' });
          if (!/^application\/json(?:\s*;|$)/i.test(header(req, 'content-type') ?? '')) return json(res, 415, { ok: false, error: 'json_required' });
          const rawBody = await body(req, config.maxWebhookBodyBytes);
          const request = parseJson(rawBody);
          const idempotencyKey = header(req, 'idempotency-key');
          if (!idempotencyKey || !String(idempotencyKey).trim()) return json(res, 400, { ok: false, error: 'idempotency_key_required' });
          const result = await fulfillOrder(authorization.tenantId, orderId, request, idempotencyKey);
          return json(res, 200, { source: 'wanda', ...result });
        }
        if (req.method !== 'GET') return json(res, 405, { ok: false, error: 'method_not_allowed' });
        if (typeof getOrder !== 'function') return json(res, 503, { ok: false, error: 'wanda_order_detail_unavailable' });
        const order = await getOrder(authorization.tenantId, orderId);
        return json(res, 200, { source: 'wanda', order });
      }
      const normalizedUiPath = pathname.startsWith('/api/') ? `/ui${pathname}` : pathname;
      const uiAsset = UI_ASSETS.get(normalizedUiPath);
      const isUiApi = normalizedUiPath.startsWith('/ui/api/');
      const isV4Api = normalizedUiPath.startsWith('/ui/v4/api/');
      const isV4Job = normalizedUiPath.startsWith('/ui/v4/jobs/');
      const isImHistoryDiagnostic = normalizedUiPath === '/ui/api/diagnostics/im-history';
      if (uiAsset || isUiApi || isV4Api || isV4Job) {
        const rawBody = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method ?? '')
          ? await body(req, (isV4Api || isV4Job) ? config.maxUiBodyBytes : config.maxWebhookBodyBytes)
          : '';
        const gatewayAuthorized = gatewayRequest(req, platform, rawBody);
        const operationalAuthorization = isImHistoryDiagnostic && !gatewayAuthorized
          ? wandaOrderApiAuthorized(req, config) : null;
        if (!gatewayAuthorized && !operationalAuthorization?.tenantId) {
          if (operationalAuthorization?.status) {
            return json(res, operationalAuthorization.status, { ok: false, error: operationalAuthorization.error });
          }
          return json(res, 401, { ok: false, error: 'invalid_gateway_signature' });
        }
        const authorizedTenantId = gatewayAuthorized
          ? header(req, 'x-yumaiduo-tenant-id') : operationalAuthorization?.tenantId;
        if (uiAsset) {
          if (req.method !== 'GET') return json(res, 405, { ok: false, error: 'method_not_allowed' });
          return (await file(res, uiAsset[0], uiAsset[1])) || json(res, 404, { ok: false, error: 'not_found' });
        }
        if (isV4Api) return proxyV4(
          req, res, normalizedUiPath, rawBody, config, fetchImpl,
          header(req, 'x-yumaiduo-tenant-id'), header(req, 'x-wanda-shop-id'), header(req, 'x-wanda-model-scope'),
        );
        if (isV4Job && req.method === 'POST' && normalizedUiPath === '/ui/v4/jobs/image-message') {
          const id = await startImageJob(rawBody, header(req, 'content-type'), header(req, 'x-yumaiduo-tenant-id'), header(req, 'x-yumaiduo-user-id'));
          return json(res, 202, { ok: true, job_id: id, status: 'processing' });
        }
        if (isV4Job && req.method === 'GET') {
          const id = normalizedUiPath.slice('/ui/v4/jobs/'.length);
          const job = await jobStore.get(id, {
            tenantId: header(req, 'x-yumaiduo-tenant-id'), userId: header(req, 'x-yumaiduo-user-id'),
          });
          if (!job) return json(res, 404, { ok: false, error: 'job_not_found' });
          if (job.status !== 'completed') return json(res, 202, { ok: true, job_id: id, status: 'processing' });
          return sendV4Result(res, job.result);
        }
        if (isV4Job) return json(res, 405, { ok: false, error: 'method_not_allowed' });
        if (req.method === 'GET' && normalizedUiPath === '/ui/api/overview') {
          return json(res, 200, { ok: true, data: overview(config.manifest, await health()) });
        }
        if (req.method === 'GET' && normalizedUiPath === '/ui/api/diagnostics/im-history') {
          const tenantId = authorizedTenantId;
          if (tenantId !== '107') return json(res, 403, { ok: false, error: 'diagnostic_tenant_forbidden' });
          const requestUrl = new URL(req.url ?? '/', 'http://v2.local');
          const { accountUnb, chatId, limit } = parseImHistoryRequest(requestUrl);
          if (typeof platform.createClient !== 'function') {
            return json(res, 503, { ok: false, error: 'im_history_runtime_unavailable' });
          }
          try {
            const client = platform.createClient(tenantId);
            const page = await client.im.listMessages({ accountUnb, chatId, pageSize: limit });
            const items = Array.isArray(page?.items) ? page.items : Array.isArray(page) ? page : [];
            return json(res, 200, {
              ok: true, accountUnb, chatId,
              messages: items.slice(0, limit).map(sanitizeImHistoryMessage),
            });
          } catch {
            return json(res, 503, { ok: false, error: 'im_history_unavailable' });
          }
        }
        if (req.method === 'POST' && normalizedUiPath === '/ui/api/shops/sync') {
          if (typeof syncShops !== 'function') return json(res, 503, { ok: false, error: 'shop_sync_unavailable' });
          const result = await syncShops(header(req, 'x-yumaiduo-tenant-id'));
          return json(res, 200, { ok: true, ...result });
        }
        if (req.method === 'GET' && normalizedUiPath.startsWith('/ui/api/orders/')) {
          if (typeof getOrder !== 'function') return json(res, 503, { ok: false, error: 'order_detail_unavailable' });
          const orderId = decodeURIComponent(normalizedUiPath.slice('/ui/api/orders/'.length));
          if (!orderId || orderId.includes('/')) return json(res, 400, { ok: false, error: 'order_id_invalid' });
          const order = await getOrder(header(req, 'x-yumaiduo-tenant-id'), orderId);
          return json(res, 200, { ok: true, order });
        }
        if (req.method === 'GET' && normalizedUiPath === '/ui/api/orders') {
          if (typeof listOrders !== 'function') return json(res, 503, { ok: false, error: 'order_list_unavailable' });
          const requestUrl = new URL(req.url ?? '/', 'http://v2.local');
          const limit = Math.max(1, Math.min(Number(requestUrl.searchParams.get('limit')) || 50, 100));
          const result = await listOrders(header(req, 'x-yumaiduo-tenant-id'), limit);
          return json(res, 200, { ok: true, ...result });
        }
        return json(res, 404, { ok: false, error: 'not_found' });
      }

      if (req.method !== 'POST' || !pathname.startsWith(`${webhookBase}/`)) return json(res, 404, { ok: false, error: 'not_found' });
      const event = decodeURIComponent(pathname.slice(webhookBase.length + 1));
      if (!event || event.includes('/')) return json(res, 404, { ok: false, error: 'not_found' });
      if (!/^application\/json(?:\s*;|$)/i.test(header(req, 'content-type') ?? '')) return json(res, 415, { ok: false, error: 'json_required' });
      const rawBody = await body(req, config.maxWebhookBodyBytes);
      if (!platform.verifyWebhook({ timestamp: header(req, 'x-yumaiduo-timestamp'), signature: header(req, 'x-yumaiduo-signature'), rawBody })) return json(res, 401, { ok: false, error: 'invalid_signature' });
      const envelope = parseJson(rawBody);
      if (!envelope?.id || !envelope?.tenantId || envelope.event !== event || !Number.isFinite(envelope.ts)) return json(res, 400, { ok: false, error: 'invalid_envelope' });
      const accepted = await enqueue(envelope);
      return json(res, 202, { ok: true, accepted: accepted.created, duplicate: !accepted.created });
    } catch (error) {
      logger.error('v2 request rejected', { error, path: pathname });
      const status = error.status ?? 503;
      const code = error.code ?? (status === 413 ? 'payload_too_large' : 'temporarily_unavailable');
      return json(res, status, { ok: false, error: code });
    }
  };
  const server = http.createServer((req, res) => void handler(req, res));
  return Object.freeze({
    handler,
    listen: async ({ port = config.port, host = config.host } = {}) => {
      await jobStore.initialize();
      jobsStopped = false;
      requestImageJobDrain();
      return new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(port, host, () => { server.off('error', reject); resolve(server.address()); });
      });
    },
    close: async () => {
      jobsStopped = true;
      await jobDrainPromise;
      if (!server.listening) return;
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    },
  });
}
