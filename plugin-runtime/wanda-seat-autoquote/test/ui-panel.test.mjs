import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { createV2HttpServer } from '../src/http/server.mjs';

function signedHeaders() {
  return {
    'x-yumaiduo-plugin-id': 'wanda-seat-autoquote',
    'x-yumaiduo-tenant-id': 'tenant-test',
    'x-yumaiduo-user-id': 'user-test',
    'x-yumaiduo-timestamp': String(Date.now()),
    'x-yumaiduo-signature': 'valid-signature',
  };
}

async function withServer(run, {
  fetchImpl = globalThis.fetch,
  syncShops = async () => ({ ok: true, count: 0 }),
  listOrders = async () => ({ orders: [], count: 0, observedCount: 0 }),
} = {}) {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ui-panel-'));
  const server = createV2HttpServer({
    config: {
      dataDir,
      encryptionKey: Buffer.alloc(32, 30),
      maxWebhookBodyBytes: 1024 * 1024,
      maxUiBodyBytes: 30_000_000,
      requestTimeoutMs: 5_000,
      v4BackendUrl: 'http://127.0.0.1:8012',
      manifest: {
        id: 'wanda-seat-autoquote',
        name: '万达电影票 AI 客服 V4',
        version: '2.2.18',
        entrypoint: { webhookPath: '/__plugin__/webhook' },
      },
    },
    platform: {
      verifyGateway: ({ pluginId, tenantId, userId, signature, rawBody }) => (
        pluginId === 'wanda-seat-autoquote'
        && tenantId === 'tenant-test'
        && userId === 'user-test'
        && signature === 'valid-signature'
        && typeof rawBody === 'string'
      ),
      verifyWebhook: () => false,
    },
    enqueue: async () => ({ created: true }),
    syncShops,
    listOrders,
    health: async () => ({
      ok: true,
      registered: true,
      pluginId: 'wanda-seat-autoquote',
      runtime: { ok: true, running: 0, limit: 12 },
    }),
    logger: { error() {} },
    fetchImpl,
  });
  const address = await server.listen({ port: 0, host: '127.0.0.1' });
  try { await run(`http://127.0.0.1:${address.port}`); } finally { await server.close(); }
}

test('FishMore panel and relative assets require a signed gateway request', async () => {
  await withServer(async (baseUrl) => {
    const rejected = await fetch(`${baseUrl}/ui`);
    assert.equal(rejected.status, 401);

    const page = await fetch(`${baseUrl}/ui`, { headers: signedHeaders() });
    assert.equal(page.status, 200);
    assert.match(page.headers.get('content-security-policy') ?? '', /script-src 'self'/u);
    const pageHtml = await page.text();
    assert.match(pageHtml, /客服工作台/u);
    assert.match(pageHtml, /报价记录/u);
    assert.match(pageHtml, /订单管理/u);
    assert.ok(pageHtml.indexOf('店铺开关') < pageHtml.indexOf('运营报价'));
    assert.ok(pageHtml.indexOf('运营报价') < pageHtml.indexOf('报价记录'));
    assert.doesNotMatch(pageHtml, /data-workspace="safety"/u);
    assert.doesNotMatch(pageHtml, /data-workspace="logs"/u);
    assert.doesNotMatch(pageHtml, /规则试算（仅本地预览，不锁座）/u);
    assert.match(pageHtml, /id="knowledgeList"/u);
    assert.match(pageHtml, /W\+代订与纯文字咨询/u);
    assert.match(pageHtml, /客服知识库/u);
    assert.match(pageHtml, /W\+原价减免/u);
    assert.match(pageHtml, /W\+会员价阈值/u);
    assert.match(pageHtml, /普通座调整/u);
    assert.match(pageHtml, /确定性运营报价/u);
    assert.doesNotMatch(pageHtml, /本地试算/u);
    assert.doesNotMatch(pageHtml, /安全与风控设置/u);
    assert.match(pageHtml, /id="agentPersona"/u);
    assert.match(pageHtml, /id="businessBackground"/u);
    assert.match(pageHtml, /id="customerServiceKnowledge"/u);
    assert.match(pageHtml, /id="replyStyle"/u);
    assert.match(pageHtml, /id="humanServiceHours"/u);
    assert.match(pageHtml, /id="movieReminderTemplate"/u);
    const asset = await fetch(`${baseUrl}/ui/styles.css`, { headers: signedHeaders() });
    assert.equal(asset.status, 200);
    assert.match(asset.headers.get('content-type') ?? '', /^text\/css/u);

    const appAsset = await fetch(`${baseUrl}/ui/app.js`, { headers: signedHeaders() });
    const appSource = await appAsset.text();
    assert.match(appSource, /api\/settings\/knowledge/u);
    assert.match(pageHtml, /id="orderSearch"/u);
    assert.match(pageHtml, /id="reminderEnabled"/u);
    assert.match(pageHtml, /散场后提醒收货/u);
    assert.match(appSource, /saveReminderSettings/u);
    assert.match(pageHtml, /影院匹配失败补问/u);
    assert.match(pageHtml, /自定义关键词回复/u);
    assert.match(pageHtml, /id="addKeywordRule"/u);
    assert.match(appSource, /reply-keyword-images/u);
    assert.match(appSource, /image\/jpeg,image\/png,image\/webp,image\/gif/u);
    assert.match(appSource, /image_asset_id/u);
    assert.match(appSource, /record\.order_id/u);
    assert.match(appSource, /影院信息待关联/u);
    assert.match(appSource, /开场时间/u);
  });
});

test('signed panel shop refresh pulls current tenant shops from FishMore', async () => {
  const tenants = [];
  await withServer(async (baseUrl) => {
    const rejected = await fetch(`${baseUrl}/ui/api/shops/sync`, { method: 'POST' });
    assert.equal(rejected.status, 401);
    const response = await fetch(`${baseUrl}/ui/api/shops/sync`, { method: 'POST', headers: signedHeaders() });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true, count: 3 });
  }, {
    syncShops: async (tenantId) => { tenants.push(tenantId); return { ok: true, count: 3 }; },
  });
  assert.deepEqual(tenants, ['tenant-test']);
});

test('signed panel order management is tenant scoped by the gateway', async () => {
  const calls = [];
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/ui/api/orders?limit=25`, { headers: signedHeaders() });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true, orders: [{ orderId: 'order-1' }], count: 1, observedCount: 1 });
  }, {
    listOrders: async (tenantId, limit) => {
      calls.push({ tenantId, limit });
      return { orders: [{ orderId: 'order-1' }], count: 1, observedCount: 1 };
    },
  });
  assert.deepEqual(calls, [{ tenantId: 'tenant-test', limit: 25 }]);
});

test('V4 panel API is signed at the gateway and proxied only to the loopback backend', async () => {
  const calls = [];
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/ui/v4/api/settings/vision`, { headers: signedHeaders() });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { model: 'qwen-v4' });
  }, {
    fetchImpl: async (url, options) => {
      calls.push({ url, method: options.method, tenantId: options.headers['x-wanda-tenant-id'] });
      return new Response(JSON.stringify({ model: 'qwen-v4' }), { status: 200, headers: { 'content-type': 'application/json' } });
    },
  });
  assert.deepEqual(calls, [{ url: 'http://127.0.0.1:8012/api/settings/vision', method: 'GET', tenantId: 'tenant-test' }]);
});

test('slow image recognition runs as a tenant-bound background job beyond the gateway timeout', async () => {
  const calls = [];
  await withServer(async (baseUrl) => {
    const payload = JSON.stringify({ _wanda_v4_formdata: true, entries: [{ name: 'conversation_id', value: 'chat-1' }] });
    const startedAt = Date.now();
    const started = await fetch(`${baseUrl}/ui/v4/jobs/image-message`, {
      method: 'POST', headers: { ...signedHeaders(), 'content-type': 'application/json' }, body: payload,
    });
    assert.equal(started.status, 202);
    assert.ok(Date.now() - startedAt < 100);
    const { job_id: jobId } = await started.json();
    assert.match(jobId, /^[a-f0-9-]{36}$/u);

    let completed;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      completed = await fetch(`${baseUrl}/ui/v4/jobs/${jobId}`, { headers: signedHeaders() });
      if (completed.status !== 202) break;
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    assert.equal(completed.status, 200);
    assert.deepEqual(await completed.json(), { reply: '报价完成' });
  }, {
    fetchImpl: async (url, options) => {
      calls.push({ url, method: options.method, tenantId: options.headers['x-wanda-tenant-id'] });
      await new Promise((resolve) => setTimeout(resolve, 80));
      return new Response(JSON.stringify({ reply: '报价完成' }), { status: 200, headers: { 'content-type': 'application/json' } });
    },
  });
  assert.deepEqual(calls, [{ url: 'http://127.0.0.1:8012/api/chat/image-messages', method: 'POST', tenantId: 'tenant-test' }]);
});

test('panel overview is tenant-authenticated and exposes no cross-tenant event counts', async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/ui/api/overview`, { headers: signedHeaders() });
    assert.equal(response.status, 200);
    const payload = await response.json();
    assert.equal(payload.ok, true);
    assert.equal(payload.data.plugin.id, 'wanda-seat-autoquote');
    assert.equal(payload.data.plugin.version, '2.2.18');
    assert.equal(payload.data.services.platform.status, 'healthy');
    assert.equal(JSON.stringify(payload).includes('eventCounts'), false);
    assert.equal(JSON.stringify(payload).includes('tenant-test'), false);
  });
});
