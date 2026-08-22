import { createHash, timingSafeEqual } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { BackendRequestError } from './backend-client.mjs';

const ASSETS = new Map([
  ['/ui', ['index.html', 'text/html; charset=utf-8']],
  ['/ui/', ['index.html', 'text/html; charset=utf-8']],
  ['/ui/index.html', ['index.html', 'text/html; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/sdk.js', ['sdk.js', 'text/javascript; charset=utf-8']],
  ['/ui/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/ui/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/ui/sdk.js', ['sdk.js', 'text/javascript; charset=utf-8']],
  // The platform gateway may preserve or remove the trailing slash from /ui.
  // index.html uses ui/<asset>, so preserve the nested form as well.
  ['/ui/ui/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/ui/ui/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/ui/ui/sdk.js', ['sdk.js', 'text/javascript; charset=utf-8']],
]);

class UiHttpError extends Error {
  constructor(status, code, message = code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function header(req, name) {
  const value = req.headers[name];
  return Array.isArray(value) ? value[0] : value;
}

function isLoopback(address) {
  return ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address);
}

function secretsEqual(left, right) {
  const first = Buffer.from(String(left ?? ''), 'utf8');
  const second = Buffer.from(String(right ?? ''), 'utf8');
  return first.length === second.length && first.length > 0 && timingSafeEqual(first, second);
}

async function readBody(req, limit) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new UiHttpError(413, 'body_too_large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'cache-control': 'no-store',
  });
  res.end(body);
}

function parseJson(rawBody) {
  if (!rawBody) return {};
  try {
    const value = JSON.parse(rawBody);
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('object required');
    return value;
  } catch {
    throw new UiHttpError(400, 'invalid_json');
  }
}

export function createUiHandler({ config, platformRuntime, api, logger = console }) {
  const uiRoot = path.join(config.projectRoot, 'ui');
  const replyImageUploads = new Map();

  return async function handleUiRequest(req, res, pathname) {
    pathname = normalizeUiPath(pathname);
    if (!pathname.startsWith('/ui') && !ASSETS.has(pathname)) return false;
    let rawBody = '';
    try {
      rawBody = ['POST', 'PUT', 'PATCH'].includes(req.method)
        ? await readBody(req, config.maxUiBodyBytes)
        : '';

      if (pathname.startsWith('/ui/api/')) {
        const context = authenticate(req, rawBody, config, platformRuntime);
        await handleApi(req, res, pathname, rawBody, context, api, replyImageUploads);
        return true;
      }

      const asset = ASSETS.get(pathname);
      if (req.method !== 'GET' || !asset) throw new UiHttpError(404, 'not_found');
      const [filename, contentType] = asset;
      const body = await readFile(path.join(uiRoot, filename));
      res.writeHead(200, {
        'content-type': contentType,
        'content-length': body.length,
        // This is an authenticated management console. Keeping its small
        // assets uncached prevents an iframe from combining a new HTML page
        // with an older SDK or application module after a hotfix.
        'cache-control': 'no-store',
        'content-security-policy': contentSecurityPolicy(config),
        'x-content-type-options': 'nosniff',
        'referrer-policy': 'no-referrer',
      });
      res.end(body);
      return true;
    } catch (error) {
      const normalizedError = normalizeRequestError(error);
      const { status } = normalizedError;
      logger.error?.('ui request failed', { pathname, status, error });
      sendJson(res, status, {
        ok: false,
        error: normalizedError.code,
        message: normalizedError.message,
      });
      return true;
    }
  };
}

function normalizeRequestError(error) {
  if (error instanceof UiHttpError) {
    return { status: error.status, code: error.code, message: error.message };
  }
  if (error instanceof BackendRequestError) {
    // Preserve a validated backend 4xx response (for example an inaccessible
    // image URL). Turning it into 502 incorrectly tells the dashboard that
    // the plugin gateway is down.
    const status = error.status === 422
      ? 422
      : error.code === 'BACKEND_TIMEOUT' ? 504 : 502;
    return {
      status,
      code: error.code ?? 'backend_request_failed',
      message: error.message,
    };
  }
  if (Number.isInteger(error?.status)) {
    return {
      status: error.status,
      code: error.code ?? 'request_failed',
      message: error.message ?? error.code ?? 'request_failed',
    };
  }
  return {
    status: 500,
    code: error?.code ?? 'internal_error',
    message: error?.message ?? 'internal_error',
  };
}
function normalizeUiPath(pathname) {
  const value = String(pathname ?? '');
  const normalized = value.startsWith('/__plugin__/') ? value.slice('/__plugin__'.length) : value;
  // Fish-Mai-Duo's iframe SDK sends authedFetch('api/...') through the gateway.
  // The gateway forwards that request to the plugin as /api/..., while local
  // preview uses /ui/api/.... Keep one internal route contract for both.
  return normalized.startsWith('/api/') ? `/ui${normalized}` : normalized;
}

function authenticate(req, rawBody, config, platformRuntime) {
  if (config.allowLocalUiBypass && isLoopback(req.socket?.remoteAddress)) {
    return { tenantId: 'local-dev', userId: 'local-dev' };
  }
  const authorization = String(header(req, 'authorization') ?? '');
  const bearerMatch = authorization.match(/^Bearer\s+(.+)$/iu);
  const temporarySecret = bearerMatch?.[1] ?? header(req, 'x-wanda-temp-ui-secret');
  const basicAuthSha256 = hashBasicAuthorization(authorization);
  if (
    config.temporaryUi
    && isLoopback(req.socket?.remoteAddress)
    && (
      secretsEqual(temporarySecret, config.temporaryUi.internalSecret)
      || secretsEqual(basicAuthSha256, config.temporaryUi.basicAuthSha256)
    )
  ) {
    return { tenantId: config.temporaryUi.tenantId, userId: config.temporaryUi.userId };
  }
  const pluginId = header(req, 'x-yumaiduo-plugin-id');
  const tenantId = header(req, 'x-yumaiduo-tenant-id');
  const userId = header(req, 'x-yumaiduo-user-id');
  const timestamp = header(req, 'x-yumaiduo-timestamp');
  const signature = header(req, 'x-yumaiduo-signature');
  if (pluginId !== config.manifest.id || !tenantId || !userId) {
    throw new UiHttpError(401, 'invalid_gateway_context');
  }
  if (!platformRuntime.verifyGateway({ timestamp, signature, rawBody })) {
    throw new UiHttpError(401, 'invalid_gateway_signature');
  }
  return { tenantId: String(tenantId), userId: String(userId) };
}

function hashBasicAuthorization(authorization) {
  const match = String(authorization ?? '').match(/^Basic\s+([A-Za-z0-9+/=]{4,1024})$/iu);
  if (!match) return '';
  let decoded;
  try {
    decoded = Buffer.from(match[1], 'base64').toString('utf8');
  } catch {
    return '';
  }
  if (!decoded.includes(':') || decoded.length > 512) return '';
  return createHash('sha256').update(decoded, 'utf8').digest('hex');
}

function contentSecurityPolicy(config) {
  void config;
  // The gateway controls where this authenticated iframe can be embedded.
  return "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; base-uri 'none'; form-action 'self'";
}

async function ingestReplyImageChunk(uploads, tenantId, input, api) {
  const now = Date.now();
  for (const [id, upload] of uploads) {
    if (now - upload.updatedAt > 10 * 60 * 1000) uploads.delete(id);
  }
  const uploadId = String(input.upload_id ?? '');
  const key = String(input.key ?? '');
  const filename = String(input.filename ?? '').slice(0, 160);
  const contentType = String(input.content_type ?? '');
  const index = Number(input.index);
  const total = Number(input.total);
  const chunk = String(input.data_base64_chunk ?? '');
  if (!/^[A-Za-z0-9-]{16,64}$/u.test(uploadId)
      || !/^[a-z0-9_]{2,80}$/u.test(key)
      || !filename
      || !['image/png', 'image/jpeg', 'image/webp'].includes(contentType)
      || !Number.isInteger(index) || !Number.isInteger(total)
      || total < 1 || total > 160 || index < 0 || index >= total
      || chunk.length < 1 || chunk.length > 200 * 1024
      || !/^[A-Za-z0-9+/=]+$/u.test(chunk)) {
    throw new UiHttpError(422, 'invalid_reply_template_image_chunk');
  }
  const mapKey = `${tenantId}:${uploadId}`;
  let upload = uploads.get(mapKey);
  if (!upload) {
    if (uploads.size >= 20) throw new UiHttpError(429, 'too_many_image_uploads');
    upload = { tenantId, key, filename, contentType, total, chunks: new Array(total), updatedAt: now };
    uploads.set(mapKey, upload);
  }
  if (upload.key !== key || upload.filename !== filename || upload.contentType !== contentType || upload.total !== total) {
    uploads.delete(mapKey);
    throw new UiHttpError(422, 'reply_template_image_chunk_conflict');
  }
  if (upload.chunks[index] && upload.chunks[index] !== chunk) {
    uploads.delete(mapKey);
    throw new UiHttpError(422, 'reply_template_image_chunk_conflict');
  }
  upload.chunks[index] = chunk;
  upload.updatedAt = now;
  const received = upload.chunks.reduce((count, value) => count + (value ? 1 : 0), 0);
  if (received < total) return { complete: false, received, total };
  uploads.delete(mapKey);
  const dataBase64 = upload.chunks.join('');
  if (dataBase64.length > 7 * 1024 * 1024) throw new UiHttpError(413, 'body_too_large');
  return api.uploadReplyTemplateImage(tenantId, {
    key: upload.key,
    filename: upload.filename,
    content_type: upload.contentType,
    data_base64: dataBase64,
  });
}

async function handleApi(req, res, pathname, rawBody, context, api, replyImageUploads) {
  if (req.method === 'GET' && pathname === '/ui/api/overview') {
    sendJson(res, 200, { ok: true, data: await api.overview(context.tenantId) });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/settings') {
    sendJson(res, 200, { ok: true, data: await api.getSettings(context.tenantId) });
    return;
  }
  if (req.method === 'PUT' && pathname === '/ui/api/settings') {
    sendJson(res, 200, { ok: true, data: await api.updateSettings(context.tenantId, parseJson(rawBody)) });
    return;
  }
  if (req.method === 'POST' && pathname === '/ui/api/reply-template-images') {
    const payload = parseJson(rawBody);
    const result = Object.hasOwn(payload, 'upload_id')
      ? await ingestReplyImageChunk(replyImageUploads, context.tenantId, payload, api)
      : await api.uploadReplyTemplateImage(context.tenantId, payload);
    sendJson(res, 200, { ok: true, data: result });
    return;
  }
  if (req.method === 'POST' && pathname === '/ui/api/reply-template-images/chunks') {
    const result = await ingestReplyImageChunk(replyImageUploads, context.tenantId, parseJson(rawBody), api);
    sendJson(res, 200, { ok: true, data: result });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/logs') {
    sendJson(res, 200, { ok: true, data: await api.listLogs(context.tenantId) });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/conversation-learning-summary') {
    sendJson(res, 200, { ok: true, data: await api.getConversationLearningSummary(context.tenantId) });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/agent-evaluations') {
    sendJson(res, 200, { ok: true, data: await api.listAgentEvaluations(context.tenantId) });
    return;
  }
  const agentTraceMatch = pathname.match(/^\/ui\/api\/agent-runs\/([^/]+)\/trace$/u);
  if (req.method === 'GET' && agentTraceMatch) {
    sendJson(res, 200, { ok: true, data: await api.getAgentTrace(context.tenantId, decodeURIComponent(agentTraceMatch[1])) });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/agent-canary-readiness') {
    sendJson(res, 200, { ok: true, data: await api.getAgentCanaryReadiness(context.tenantId) });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/agent-offline-evaluation') {
    sendJson(res, 200, { ok: true, data: await api.getAgentOfflineEvaluation(context.tenantId) });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/agent-human-comparisons') {
    sendJson(res, 200, { ok: true, data: await api.listAgentHumanComparisons(context.tenantId) });
    return;
  }
  const humanComparisonMatch = pathname.match(/^\/ui\/api\/agent-human-comparisons\/([^/]+)$/u);
  if (req.method === 'PUT' && humanComparisonMatch) {
    sendJson(res, 200, { ok: true, data: await api.reviewAgentHumanComparison(context.tenantId, decodeURIComponent(humanComparisonMatch[1]), parseJson(rawBody)) });
    return;
  }
  const agentEvaluationMatch = pathname.match(/^\/ui\/api\/agent-evaluations\/([^/]+)$/u);
  if (req.method === 'PUT' && agentEvaluationMatch) {
    sendJson(res, 200, { ok: true, data: await api.reviewAgentEvaluation(context.tenantId, decodeURIComponent(agentEvaluationMatch[1]), parseJson(rawBody)) });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/manual-tasks') {
    sendJson(res, 200, { ok: true, data: await api.listManualTasks(context.tenantId) });
    return;
  }
  const manualTaskMatch = pathname.match(/^\/ui\/api\/manual-tasks\/([^/]+)$/u);
  if (req.method === 'PUT' && manualTaskMatch) {
    const payload = parseJson(rawBody);
    const taskId = decodeURIComponent(manualTaskMatch[1]);
    const data = payload.status === 'resolved' && Object.keys(payload).length === 1
      ? await api.resolveManualTask(context.tenantId, taskId)
      : await api.updateManualTask(context.tenantId, taskId, payload, { userId: context.userId });
    sendJson(res, 200, { ok: true, data });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/operations') {
    sendJson(res, 200, { ok: true, data: await api.listOperations(context.tenantId) });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/quote-analytics') {
    sendJson(res, 200, { ok: true, data: await api.listQuoteAnalytics(context.tenantId) });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/orders') {
    sendJson(res, 200, { ok: true, data: await api.listTicketOrders(context.tenantId) });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/shops') {
    sendJson(res, 200, { ok: true, data: await api.listOwnedShops(context.tenantId) });
    return;
  }
  const shopSettingsMatch = pathname.match(/^\/ui\/api\/shops\/([^/]+)$/u);
  if (req.method === 'PUT' && shopSettingsMatch) {
    const payload = parseJson(rawBody);
    if (typeof payload.automation_enabled !== 'boolean') throw new UiHttpError(422, 'invalid_automation_enabled');
    sendJson(res, 200, {
      ok: true,
      data: await api.updateShopEnabled(
        context.tenantId,
        decodeURIComponent(shopSettingsMatch[1]),
        payload.automation_enabled,
      ),
    });
    return;
  }
  if (req.method === 'GET' && pathname === '/ui/api/knowledge-base') {
    sendJson(res, 200, { ok: true, data: await api.listKnowledgeBase(context.tenantId) });
    return;
  }
  if (req.method === 'POST' && pathname === '/ui/api/knowledge-base') {
    sendJson(res, 201, { ok: true, data: await api.createKnowledgeEntry(context.tenantId, parseJson(rawBody)) });
    return;
  }
  const knowledgeMatch = pathname.match(/^\/ui\/api\/knowledge-base\/([a-f0-9]{32})$/i);
  if (req.method === 'PUT' && knowledgeMatch) {
    sendJson(res, 200, { ok: true, data: await api.updateKnowledgeEntry(context.tenantId, knowledgeMatch[1], parseJson(rawBody)) });
    return;
  }
  throw new UiHttpError(404, 'not_found');
}



