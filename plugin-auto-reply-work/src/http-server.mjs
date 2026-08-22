import http from 'node:http';

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function singleHeader(req, name) {
  const value = req.headers[name];
  return Array.isArray(value) ? value[0] : value;
}

async function readRawBody(req, limit) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new HttpError(413, 'request body is too large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

function writeJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'cache-control': 'no-store',
  });
  res.end(body);
}

async function withTimeout(operation, timeoutMs) {
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('enqueue timeout')), timeoutMs);
        timer.unref();
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

function validateEnvelope(envelope, routeEvent, headers) {
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) {
    throw new HttpError(400, 'event envelope must be an object');
  }
  if (typeof envelope.id !== 'string' || envelope.id === '') throw new HttpError(400, 'event id is required');
  if (typeof envelope.event !== 'string' || envelope.event !== routeEvent) throw new HttpError(400, 'event name mismatch');
  if (envelope.tenantId === undefined || envelope.tenantId === null || String(envelope.tenantId) === '') {
    throw new HttpError(400, 'tenant id is required');
  }
  if (!Number.isFinite(envelope.ts)) throw new HttpError(400, 'event timestamp is required');
  if (envelope.payload !== undefined && (typeof envelope.payload !== 'object' || envelope.payload === null || Array.isArray(envelope.payload))) {
    throw new HttpError(400, 'event payload must be an object');
  }
  if (headers.eventId && headers.eventId !== envelope.id) throw new HttpError(400, 'event id header mismatch');
  if (headers.event && headers.event !== envelope.event) throw new HttpError(400, 'event header mismatch');
  if (headers.tenantId && headers.tenantId !== String(envelope.tenantId)) throw new HttpError(400, 'tenant header mismatch');
  return envelope;
}

export function createHttpServer({
  config,
  platformRuntime,
  enqueueEvent,
  handleHttpRequest,
  health = () => platformRuntime.health(),
  logger = { info() {}, warn() {}, error() {} },
} = {}) {
  if (typeof platformRuntime?.verifyWebhook !== 'function') throw new TypeError('platformRuntime.verifyWebhook is required');
  if (typeof enqueueEvent !== 'function') throw new TypeError('enqueueEvent is required');
  const webhookPath = config.manifest.entrypoint.webhookPath.replace(/\/$/, '');

  const handler = async (req, res) => {
    const pathname = new URL(req.url ?? '/', 'http://runtime.local').pathname;
    try {
      if (req.method === 'GET' && pathname === '/healthz') {
        const status = await health();
        const ok = status?.ok === true && status?.registered === true;
        writeJson(res, ok ? 200 : 503, status);
        return;
      }

      if (typeof handleHttpRequest === 'function' && await handleHttpRequest(req, res, pathname)) {
        return;
      }

      if (req.method !== 'POST' || !pathname.startsWith(`${webhookPath}/`)) {
        writeJson(res, 404, { ok: false, error: 'not_found' });
        return;
      }
      const routeEvent = decodeURIComponent(pathname.slice(webhookPath.length + 1));
      if (!routeEvent || routeEvent.includes('/')) throw new HttpError(404, 'unknown webhook path');
      if (!/^application\/json(?:\s*;|$)/i.test(singleHeader(req, 'content-type') ?? '')) {
        throw new HttpError(415, 'content-type must be application/json');
      }

      const rawBody = await readRawBody(req, config.maxWebhookBodyBytes);
      const timestamp = singleHeader(req, 'x-yumaiduo-timestamp');
      const signature = singleHeader(req, 'x-yumaiduo-signature');
      const valid = platformRuntime.verifyWebhook({ timestamp, signature, rawBody });
      if (!valid) {
        logger.warn('webhook rejected', { event: routeEvent, reason: 'invalid_signature' });
        writeJson(res, 401, { ok: false, error: 'invalid_signature' });
        return;
      }

      let envelope;
      try {
        envelope = JSON.parse(rawBody);
      } catch {
        throw new HttpError(400, 'request body must be valid JSON');
      }
      validateEnvelope(envelope, routeEvent, {
        eventId: singleHeader(req, 'x-yumaiduo-event-id'),
        event: singleHeader(req, 'x-yumaiduo-event'),
        tenantId: singleHeader(req, 'x-yumaiduo-tenant-id'),
      });

      await withTimeout(() => enqueueEvent(envelope), config.webhookAckTimeoutMs);
      logger.info('webhook enqueued', {
        event: envelope.event,
        eventId: envelope.id,
        tenantId: String(envelope.tenantId),
      });
      writeJson(res, 202, { ok: true, accepted: true });
    } catch (error) {
      const status = error instanceof HttpError ? error.status : 503;
      logger.error('runtime request failed', { path: pathname, status, error });
      writeJson(res, status, {
        ok: false,
        error: status === 503 ? 'temporarily_unavailable' : 'invalid_request',
      });
    }
  };

  const server = http.createServer((req, res) => void handler(req, res));

  async function listen({ port = config.port, host = config.host } = {}) {
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, host, () => {
        server.off('error', reject);
        resolve();
      });
    });
    return server.address();
  }

  async function close() {
    if (!server.listening) return;
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }

  return Object.freeze({ handler, listen, close, address: () => server.address() });
}
