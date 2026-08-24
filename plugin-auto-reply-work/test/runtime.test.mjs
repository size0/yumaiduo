import assert from 'node:assert/strict';
import { createHmac, timingSafeEqual } from 'node:crypto';
import test from 'node:test';
import { createPluginRuntime } from '../index.mjs';
import { BackendRequestError, createBackendClient } from '../src/backend-client.mjs';
import { ConfigurationError, createLogger, loadConfig } from '../src/config.mjs';
import { createHttpServer } from '../src/http-server.mjs';
import { createPlatformRuntime } from '../src/platform-runtime.mjs';
import manifest from '../yumaiduo.plugin.json' with { type: 'json' };

const validEnv = {
  CORE_URL: 'https://core.test',
  PLUGIN_DEVELOPER_TOKEN: 'pdk_12345678',
  PLUGIN_BASE_URL: 'http://plugin.test:4003',
  BACKEND_BASE_URL: 'http://127.0.0.1:8000',
  BACKEND_BRIDGE_KEY: 'backend-secret',
  PORT: '4003',
  LOG_LEVEL: 'error',
};

function sdkVerifier({ secret, timestamp, signature, rawBody }) {
  const expected = createHmac('sha256', secret).update(`${timestamp}.${rawBody}`).digest('hex');
  const left = Buffer.from(expected, 'hex');
  const right = Buffer.from(signature ?? '', 'hex');
  return left.length === right.length && timingSafeEqual(left, right);
}

function fakeSdk(clientCalls = []) {
  return {
    verifyWebhookSignature: sdkVerifier,
    createPluginClient(options) {
      clientCalls.push(options);
      return { options };
    },
  };
}

async function registeredRuntime(config, clientCalls = []) {
  const runtime = createPlatformRuntime(config, {
    sdk: fakeSdk(clientCalls),
    fetchImpl: async () => new Response(JSON.stringify({
      code: 'OK',
      data: { webhookSecret: '0123456789abcdef', token: 'yp_runtime_token' },
    }), { status: 200, headers: { 'content-type': 'application/json' } }),
  });
  await runtime.register();
  return runtime;
}

test('configuration validates required secrets and the fixed manifest contract', async () => {
  const config = await loadConfig({ env: validEnv, manifest });
  assert.equal(config.manifest.id, 'wanda-seat-autoquote');
  assert.equal(config.manifest.version, '1.0.0');
  assert.equal(config.manifest.name, '万达电影票 AI 客服 V4');
  assert.equal(config.webhookAckTimeoutMs, 4_500);
  assert.deepEqual(config.manifest.permissions, [
    'order.read',
    'order.write.price_change',
    'account.read',
    'im.session.read',
    'im.message.read',
    'im.message.send',
  ]);
  assert.deepEqual(config.manifest.extensionPoints.subscribes, [
    'im.message.received', 'order.created', 'order.price.changed', 'order.paid', 'order.closed',
  ]);
  await assert.rejects(
    loadConfig({ env: { ...validEnv, PLUGIN_DEVELOPER_TOKEN: '' }, manifest }),
    ConfigurationError,
  );
  await assert.rejects(
    loadConfig({ env: validEnv, manifest: { ...manifest, vendor: 'other' } }),
    /manifest.vendor/,
  );
  await assert.rejects(
    loadConfig({ env: { ...validEnv, NODE_ENV: 'production' }, manifest }),
    /CONFIG_ENCRYPTION_KEY/u,
  );
  await assert.rejects(
    loadConfig({
      env: {
        ...validEnv,
        NODE_ENV: 'production',
        CONFIG_ENCRYPTION_KEY: Buffer.alloc(32, 7).toString('base64'),
        ALLOW_LOCAL_UI_BYPASS: 'true',
      },
      manifest,
    }),
    /ALLOW_LOCAL_UI_BYPASS/u,
  );
});

test('plugin callback base URL must be an origin so the manifest webhook path is appended once', async () => {
  await assert.rejects(
    loadConfig({ env: { ...validEnv, PLUGIN_BASE_URL: 'https://wd.xdw0.cn/__plugin__' }, manifest }),
    /PLUGIN_BASE_URL must not include a path/u,
  );
});

test('quote preview configuration uses the V3 recognize and quote endpoints', async () => {
  const config = await loadConfig({
    env: {
      ...validEnv,
      QUOTE_PREVIEW_ONLY: 'true',
      AI_REPLY_PREVIEW_ENABLED: 'true',
      WANDA_V3_PREVIEW_INGEST_URL: 'http://127.0.0.1:8010/api/quotes/preview-ingest',
      WANDA_V3_PREVIEW_INGEST_KEY: 'a'.repeat(32),
    },
    manifest,
  });
  assert.equal(config.quotePreview.recognizeUrl, 'http://127.0.0.1:8010/api/quotes/preview-recognize');
  assert.equal(config.quotePreview.textFactUrl, 'http://127.0.0.1:8010/api/quotes/preview-extract-text');
  assert.equal(config.quotePreview.quoteUrl, 'http://127.0.0.1:8010/api/quotes/preview-quote');
  assert.equal('ingestUrl' in config.quotePreview, false);
  assert.equal(config.conversationAgent.url, 'http://127.0.0.1:8010/api/agents/turn');
  assert.equal(config.conversationAgent.nativeUrl, 'http://127.0.0.1:8010/api/agents/v2/completions');
});

test('retired external advisory environment variables do not create a secondary provider', async () => {
  const config = await loadConfig({
    env: {
      ...validEnv,
      DIFY_SHADOW_ENABLED: 'true',
      DIFY_WORKFLOW_URL: 'https://retired-provider.invalid/v1/workflows/run',
      DIFY_API_KEY: 'd'.repeat(32),
      DIFY_TIMEOUT_MS: '8000',
    },
    manifest,
  });
  assert.equal(Object.hasOwn(config, 'difyShadow'), false);
});

test('configuration does not accept previous plugin environment aliases', async () => {
  const withoutBackendUrl = { ...validEnv, TICKET_BRIDGE_BASE_URL: 'http://127.0.0.1:8000/api/xianyu-plugin' };
  delete withoutBackendUrl.BACKEND_BASE_URL;
  await assert.rejects(loadConfig({ env: withoutBackendUrl, manifest }), /BACKEND_BASE_URL/u);

  const withoutBridgeKey = { ...validEnv, TICKET_BRIDGE_KEY: 'previous-key', BACKEND_API_TOKEN: 'previous-token' };
  delete withoutBridgeKey.BACKEND_BRIDGE_KEY;
  await assert.rejects(loadConfig({ env: withoutBridgeKey, manifest }), /BACKEND_BRIDGE_KEY/u);

  const withoutPluginUrl = { ...validEnv, BASE_URL: 'https://previous-plugin.example.com' };
  delete withoutPluginUrl.PLUGIN_BASE_URL;
  const config = await loadConfig({ env: withoutPluginUrl, manifest });
  assert.equal(config.baseUrl, manifest.runtime.baseUrl);
});

test('temporary production UI requires a complete isolated proxy identity', async () => {
  const production = {
    ...validEnv,
    NODE_ENV: 'production',
    CONFIG_ENCRYPTION_KEY: Buffer.alloc(32, 7).toString('base64'),
    TEMP_UI_ENABLED: 'true',
    TEMP_UI_TENANT_ID: '107',
    TEMP_UI_USER_ID: 'temporary-admin',
    TEMP_UI_INTERNAL_SECRET: 'x'.repeat(48),
    TEMP_UI_BASIC_AUTH_SHA256: 'a'.repeat(64),
  };
  const config = await loadConfig({ env: production, manifest });
  assert.deepEqual(config.temporaryUi, {
    tenantId: '107',
    userId: 'temporary-admin',
    internalSecret: 'x'.repeat(48),
    basicAuthSha256: 'a'.repeat(64),
  });
  assert.equal('reviewBackendTenantId' in config, false);
  assert.equal('reviewAdminTenantIds' in config, false);
  await assert.rejects(
    loadConfig({ env: { ...production, TEMP_UI_INTERNAL_SECRET: 'short' }, manifest }),
    /TEMP_UI_INTERNAL_SECRET/u,
  );
});

test('production configuration rejects a previous plugin data directory', async () => {
  await assert.rejects(
    loadConfig({
      env: {
        ...validEnv,
        NODE_ENV: 'production',
        CONFIG_ENCRYPTION_KEY: Buffer.alloc(32, 7).toString('base64'),
        DATA_DIR: '/var/lib/ticket-system/plugin-data',
      },
      manifest,
    }),
    /isolated Wanda AI plugin data directory/u,
  );
});

test('logger redacts token-shaped strings and secret fields', () => {
  const lines = [];
  const logger = createLogger({
    level: 'debug',
    sink: { log: (line) => lines.push(line), debug: (line) => lines.push(line) },
  });
  logger.debug('request Bearer top-secret', {
    developerToken: 'pdk_12345678',
    nested: { authorization: 'Bearer abc', value: 'yp_runtime_token' },
  });
  assert.equal(lines.length, 1);
  assert.doesNotMatch(lines[0], /top-secret|pdk_12345678|yp_runtime_token|Bearer abc/);
  assert.match(lines[0], /REDACTED/);
});

test('platform runtime registers, verifies the raw body, and creates tenant clients', async () => {
  const config = await loadConfig({ env: validEnv, manifest });
  const calls = [];
  const clientCalls = [];
  const runtime = createPlatformRuntime(config, {
    sdk: fakeSdk(clientCalls),
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({
        data: { webhookSecret: '0123456789abcdef', token: 'yp_runtime_token' },
      }), { status: 200 });
    },
  });
  await runtime.register();
  assert.equal(calls[0].url, 'https://core.test/api/v1/plugin/runtime/register');
  assert.equal(calls[0].init.headers['x-plugin-developer-token'], validEnv.PLUGIN_DEVELOPER_TOKEN);
  assert.deepEqual(JSON.parse(calls[0].init.body), { manifest, baseUrl: validEnv.PLUGIN_BASE_URL });

  const rawBody = '{"id":"evt_1", "event":"order.paid"}';
  const timestamp = String(Date.now());
  const signature = createHmac('sha256', '0123456789abcdef').update(`${timestamp}.${rawBody}`).digest('hex');
  assert.equal(runtime.verifyWebhook({ timestamp, signature, rawBody }), true);
  runtime.createClient('tenant-1');
  assert.deepEqual(clientCalls[0], {
    coreUrl: 'https://core.test',
    pluginToken: 'yp_runtime_token',
    tenantId: 'tenant-1',
  });
  assert.equal(runtime.health().registered, true);
  runtime.stop();
  assert.equal(runtime.health().registered, false);
});

test('HTTP runtime rejects bad signatures and enqueues a verified envelope once', async (t) => {
  const config = await loadConfig({ env: validEnv, manifest });
  const platformRuntime = await registeredRuntime(config);
  const enqueued = [];
  const server = createHttpServer({
    config,
    platformRuntime,
    enqueueEvent: async (envelope) => enqueued.push(envelope),
    health: async () => ({ ok: true, registered: true }),
  });
  const address = await server.listen({ port: 0, host: '127.0.0.1' });
  t.after(() => server.close());
  const baseUrl = `http://127.0.0.1:${address.port}`;

  const healthResponse = await fetch(`${baseUrl}/healthz`);
  assert.equal(healthResponse.status, 200);
  assert.deepEqual(await healthResponse.json(), { ok: true, registered: true });

  const envelope = {
    id: 'evt_1',
    tenantId: 'tenant-1',
    event: 'order.paid',
    ts: Date.now(),
    payload: { orderId: 'order-1' },
  };
  const rawBody = JSON.stringify(envelope, null, 2);
  const timestamp = String(Date.now());
  const headers = {
    'content-type': 'application/json',
    'x-yumaiduo-event-id': envelope.id,
    'x-yumaiduo-tenant-id': envelope.tenantId,
    'x-yumaiduo-event': envelope.event,
    'x-yumaiduo-timestamp': timestamp,
  };

  const rejected = await fetch(`${baseUrl}/__plugin__/webhook/order.paid`, {
    method: 'POST',
    headers: { ...headers, 'x-yumaiduo-signature': '00' },
    body: rawBody,
  });
  assert.equal(rejected.status, 401);
  assert.equal(enqueued.length, 0);

  const signature = createHmac('sha256', '0123456789abcdef').update(`${timestamp}.${rawBody}`).digest('hex');
  const accepted = await fetch(`${baseUrl}/__plugin__/webhook/order.paid`, {
    method: 'POST',
    headers: { ...headers, 'x-yumaiduo-signature': signature },
    body: rawBody,
  });
  assert.equal(accepted.status, 202);
  assert.deepEqual(enqueued, [envelope]);
});

test('HTTP runtime returns before the five second platform deadline when enqueue stalls', async (t) => {
  const config = await loadConfig({
    env: { ...validEnv, WEBHOOK_ACK_TIMEOUT_MS: '100' },
    manifest,
  });
  const platformRuntime = await registeredRuntime(config);
  const server = createHttpServer({
    config,
    platformRuntime,
    enqueueEvent: () => new Promise(() => {}),
  });
  const address = await server.listen({ port: 0, host: '127.0.0.1' });
  t.after(() => server.close());

  const envelope = {
    id: 'evt_timeout',
    tenantId: 'tenant-1',
    event: 'order.created',
    ts: Date.now(),
    payload: {},
  };
  const rawBody = JSON.stringify(envelope);
  const timestamp = String(Date.now());
  const signature = createHmac('sha256', '0123456789abcdef').update(`${timestamp}.${rawBody}`).digest('hex');
  const startedAt = Date.now();
  const response = await fetch(`http://127.0.0.1:${address.port}/__plugin__/webhook/order.created`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-yumaiduo-timestamp': timestamp,
      'x-yumaiduo-signature': signature,
    },
    body: rawBody,
  });
  assert.equal(response.status, 503);
  assert.ok(Date.now() - startedAt < 1_000);
});

test('backend client uses the real bridge contract and bridge-key authentication', async () => {
  const config = await loadConfig({ env: validEnv, manifest });
  const calls = [];
  const client = createBackendClient(config, {
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    },
  });
  const envelope = {
    id: 'evt_2',
    tenantId: 'tenant-2',
    event: 'order.created',
    ts: Date.now(),
    payload: { orderId: 'order-2', chatId: 'chat-2' },
  };
  assert.deepEqual(await client.upsertOrder(envelope), { ok: true });
  assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/xianyu-plugin/bridge/orders/upsert');
  assert.equal(calls[0].init.headers['x-yumaiduo-tenant-id'], 'tenant-2');
  assert.equal(calls[0].init.headers['x-yumaiduo-event-id'], 'evt_2');
  assert.equal(calls[0].init.headers['x-plugin-bridge-key'], 'backend-secret');
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    tenant_id: 'tenant-2',
    external_order_id: 'chat:chat-2',
    platform_order_id: 'order-2',
    event_id: 'evt_2',
    event_type: 'order.created',
    payload: envelope.payload,
  });
  assert.equal(typeof client.request, 'function');
  assert.equal(typeof client.matchShowtime, 'function');
  assert.equal(typeof client.submitRecognition, 'function');
  assert.equal(typeof client.submitOcrRecognition, 'function');
  assert.equal(typeof client.updateTaskStatus, 'function');
  await client.getQuotePolicy('tenant-2');
  assert.equal(calls[1].url, 'http://127.0.0.1:8000/api/xianyu-plugin/bridge/quote-policy?tenant_id=tenant-2');
  await client.updateQuotePolicy('tenant-2', {
    wplus_adjustment_cents: -290,
    regular_adjustment_cents: 100,
    max_auto_order_amount_cents: 200_000,
  });
  assert.equal(calls[2].init.method, 'PUT');
  assert.deepEqual(JSON.parse(calls[2].init.body), {
    tenant_id: 'tenant-2',
    wplus_adjustment_cents: -290,
    regular_adjustment_cents: 100,
    max_auto_order_amount_cents: 200_000,
  });
  await client.getRuntimeSettings('shop-2');
  assert.equal(calls[3].url, 'http://127.0.0.1:8000/api/xianyu-plugin/bridge/settings?account_unb=shop-2');
  await client.updateShopSettings('shop-2', false);
  assert.equal(calls[4].url, 'http://127.0.0.1:8000/api/xianyu-plugin/bridge/shop-settings');
  assert.equal(calls[4].init.method, 'PUT');
  assert.deepEqual(JSON.parse(calls[4].init.body), { account_unb: 'shop-2', automation_enabled: false });
  await client.recordConversationExperience({ tenant_id: 'tenant-2', event_id: 'evt-experience', candidate: { topic: '图片要求' } }, { tenantId: 'tenant-2', eventId: 'evt-experience:conversation-experience' });
  assert.equal(calls[5].url, 'http://127.0.0.1:8000/api/xianyu-plugin/bridge/conversation-experiences');
  assert.equal(calls[5].init.headers['x-yumaiduo-tenant-id'], 'tenant-2');
  assert.equal(calls[5].init.headers['x-yumaiduo-event-id'], 'evt-experience:conversation-experience');
  assert.equal(calls[5].init.method, 'POST');
});

test('backend client retains the backend detail code for a review vision failure', async () => {
  const config = await loadConfig({ env: validEnv, manifest });
  const client = createBackendClient(config, {
    fetchImpl: async () => new Response(JSON.stringify({ detail: 'ai_vision_upstream_502' }), { status: 503 }),
  });
  await assert.rejects(
    client.previewAiVision({ tenant_id: 'tenant-2', image_data_url: 'data:image/png;base64,AA==' }),
    (error) => error instanceof BackendRequestError
      && error.code === 'ai_vision_upstream_502'
      && error.message.includes('ai_vision_upstream_502'),
  );
});

test('backend client uploads a test image before calling recognition and realtime quote', async () => {
  const config = await loadConfig({ env: validEnv, manifest });
  const calls = [];
  const client = createBackendClient(config, {
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ ok: true, url: 'https://example.com/image.webp' }), { status: 200 });
    },
  });
  await client.uploadTestImage({
    bytes: Buffer.from([1, 2, 3]),
    contentType: 'image/webp',
    filename: 'seat-map.webp',
  }, { tenantId: 'tenant-2', eventId: 'image-test-1' });
  await client.uploadReplyImage({
    bytes: Buffer.from([4, 5, 6]),
    contentType: 'image/png',
    filename: 'reply.png',
  }, { tenantId: 'tenant-2', eventId: 'reply-image-1' });
  await client.recognizeTestImage({ image_url: 'https://example.com/image.webp' }, { tenantId: 'tenant-2', eventId: 'image-test-1' });
  await client.quoteTestImage({ recognition: { image_type: 'SEAT_MAP' } }, { tenantId: 'tenant-2', eventId: 'image-test-1' });
  assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/storage/images');
  assert.equal(calls[0].init.headers['x-plugin-bridge-key'], 'backend-secret');
  assert.ok(calls[0].init.body instanceof FormData);
  assert.equal(calls[1].url, 'http://127.0.0.1:8000/api/storage/reply-images');
  assert.ok(calls[1].init.body instanceof FormData);
  assert.equal(calls[2].url, 'http://127.0.0.1:8000/api/wanda-ai/vision/recognize');
  assert.equal(calls[3].url, 'http://127.0.0.1:8000/api/wanda-ai/quote/realtime');
});

test('entrypoint accepts an injected application factory and honors its lifecycle contract', async () => {
  const lifecycle = [];
  let applicationDependencies;
  let serverOptions;
  const runtime = await createPluginRuntime({
    env: validEnv,
    manifest,
    sdk: fakeSdk(),
    fetchImpl: async () => new Response(JSON.stringify({
      data: { webhookSecret: '0123456789abcdef', token: 'yp_runtime_token' },
    }), { status: 200 }),
    createApplication: async (dependencies) => {
      applicationDependencies = dependencies;
      return {
        enqueueEvent: async () => {},
        start: async () => lifecycle.push('application:start'),
        stop: async () => lifecycle.push('application:stop'),
        health: async () => ({ ok: true }),
      };
    },
    httpServerFactory: (options) => {
      serverOptions = options;
      return {
        listen: async () => {
          lifecycle.push('server:listen');
          return { address: '127.0.0.1', port: 4003 };
        },
        close: async () => lifecycle.push('server:close'),
        address: () => ({ address: '127.0.0.1', port: 4003 }),
      };
    },
  });

  await runtime.start();
  assert.equal(applicationDependencies.config.manifest.id, manifest.id);
  assert.equal(typeof applicationDependencies.platformRuntime.createClient, 'function');
  assert.equal(typeof applicationDependencies.backendClient.request, 'function');
  assert.equal(typeof serverOptions.enqueueEvent, 'function');
  assert.deepEqual(lifecycle, ['application:start', 'server:listen']);
  assert.equal((await runtime.health()).ok, true);
  await runtime.stop();
  assert.deepEqual(lifecycle, ['application:start', 'server:listen', 'server:close', 'application:stop']);
});
