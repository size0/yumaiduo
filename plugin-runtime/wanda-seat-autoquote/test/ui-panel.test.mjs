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
  getOrder = async () => ({ orderId: 'order-1' }),
  fulfillOrder = async () => ({ status: 'submitted' }),
  fulfillmentEnabled = false,
  orderApiKey = '',
  orderApiTenantId = '',
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
      orderApiKey,
      orderApiTenantId,
      fulfillmentEnabled,
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
    listOrders, getOrder, fulfillOrder,
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
    assert.match(pageHtml, /万达报价规则/u);
    assert.match(pageHtml, /(?:官方价格基准 \+ 固定加\/减价|按折扣率匹配区间)/u);
    assert.match(pageHtml, /id="(?:wplusDiscount|wandaRuleList)"/u);
    assert.match(pageHtml, /class="pricing-rule-card liangpiao-pricing-card"[\s\S]*class="pricing-rule-card pricing-rule-wplus"/u);
    assert.match(pageHtml, /id="pricingEnabled"/u);
    assert.match(pageHtml, /id="roundingIncrement"/u);
    assert.match(pageHtml, /良票报价（动态折扣）/u);
    assert.match(pageHtml, /折扣率 = 预估价格 ÷ 原价 × 100%/u);
    assert.match(pageHtml, /买家报价 = 预估价格 ×（1 \+ 调整比例）/u);
    assert.match(pageHtml, /添加区间/u);
    assert.doesNotMatch(pageHtml, /普通座报价/u);
    assert.doesNotMatch(pageHtml, /确定性运营报价/u);
    assert.doesNotMatch(pageHtml, /本地试算/u);
    assert.doesNotMatch(pageHtml, /安全与风控设置/u);
    assert.match(pageHtml, /id="personaBackground"/u);
    assert.doesNotMatch(pageHtml, /id="agentPersona"/u);
    assert.doesNotMatch(pageHtml, /id="businessBackground"/u);
    assert.match(pageHtml, /id="customerServiceKnowledge"/u);
    assert.match(pageHtml, /id="replyStyle"/u);
    assert.match(pageHtml, /id="humanServiceHours"/u);
    assert.match(pageHtml, /id="movieReminderTemplate"/u);
    const asset = await fetch(`${baseUrl}/ui/styles.css`, { headers: signedHeaders() });
    assert.equal(asset.status, 200);
    assert.match(asset.headers.get('content-type') ?? '', /^text\/css/u);
    const styles = await asset.text();
    assert.match(styles, /\.settings-drawer \{[^}]*position:relative/u);
    assert.match(styles, /\.settings-body \{[^}]*overflow-y:auto/u);
    assert.match(styles, /\.pricing-rule-grid \{[^}]*grid-template-columns:repeat\(2,minmax\(0,1fr\)/u);
    assert.doesNotMatch(styles, /\.liangpiao-pricing-card \{ grid-column:1\/-1; \}/u);
    assert.match(styles, /\.wanda-rule-row/u);
    assert.match(styles, /\.order-detail-dialog \{[^}]*width:min\(960px/u);
    assert.doesNotMatch(styles, /\.order-detail-dialog \{[^}]*width:100vw/u);
    assert.match(styles, /\.order-detail-summary-card/u);
    assert.match(styles, /\.order-ticket-viewer/u);
    assert.match(styles, /html, body \{ height:auto; min-height:0; overflow-x:hidden; overflow-y:auto; \}/u);
    assert.match(styles, /#mainPage \.chat-app,[\s\S]*#settingsDrawer,[\s\S]*height:min\(900px,calc\(100dvh - 170px\)\)/u);
    assert.match(styles, /\.page \{ padding-bottom:0; \}/u);
    assert.match(styles, /#mainPage \{ padding-bottom:0; \}/u);
    assert.match(styles, /margin-bottom:0;/u);
    assert.match(styles, /\.plugin-status-bar \{ position:fixed; bottom:0; \}/u);
    assert.match(styles, /\.knowledge-entry summary b \{ font-size:14px; \}/u);
    assert.match(styles, /\.knowledge-entry summary small \{ font-size:11px; \}/u);
    assert.match(styles, /\.order-table \{ font-size:13px; \}/u);
    assert.match(styles, /\.order-table th \{ font-size:12px; \}/u);

    const appAsset = await fetch(`${baseUrl}/ui/app.js`, { headers: signedHeaders() });
    const appSource = await appAsset.text();
    assert.match(appSource, /api\/settings\/knowledge/u);
    assert.match(appSource, /loadOperations/u);
    assert.match(appSource, /initWandaRules/u);
    assert.match(pageHtml, /id="orderSearch"/u);
    assert.match(pageHtml, /id="orderDetailDialog"/u);
    assert.match(pageHtml, /class="order-detail-dialog"/u);
    assert.match(appSource, /order-detail-summary-card/u);
    assert.match(appSource, /order-ticket-viewer/u);
    assert.match(appSource, /取票信息/u);
    assert.match(appSource, /良票订单暂不可用/u);
    for (const label of ['影片','城市','影院','场次','座位','出票方式','票面价','报价','成交价','状态','下单时间','咸鱼买家','操作']) assert.match(appSource, new RegExp(label, 'u'));
    assert.match(pageHtml, /id="reminderEnabled"/u);
    assert.match(pageHtml, /散场后提醒收货/u);
    assert.match(appSource, /saveReminderSettings/u);
    assert.match(pageHtml, /影院匹配失败补问/u);
    assert.match(pageHtml, /自定义关键词回复/u);
    assert.match(pageHtml, /id="addKeywordRule"/u);
    assert.match(appSource, /reply-keyword-images/u);
    assert.match(appSource, /image\/jpeg,image\/png,image\/webp,image\/gif/u);
    assert.match(appSource, /image_asset_id/u);
    assert.match(appSource, /linkedFactMap/u);
    assert.match(appSource, /recordFact\('原价'/u);
    assert.match(appSource, /recordFact\('会员价'/u);
    assert.match(appSource, /original_unit_price_cents/u);
    assert.match(appSource, /member_unit_price_cents/u);
    assert.match(appSource, /order\.fulfillment/u);
    assert.match(appSource, /name==='orders'/u);
    assert.doesNotMatch(appSource, /activeWorkspace==='orders'\)void loadOrders\(\)/u);
    assert.doesNotMatch(appSource, /activeWorkspace==='orders'\)void loadOrders\(\),15000/u);
    assert.doesNotMatch(appSource, /orderModal/u);
    assert.match(appSource, /wandaOrderRow/u);
    assert.match(appSource, /良票订单/u);
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

test('signed panel order detail is tenant scoped by the gateway', async () => {
  const calls = [];
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/ui/api/orders/order-1`, { headers: signedHeaders() });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true, order: { orderId: 'order-1' } });
  }, { getOrder: async (tenantId, orderId) => { calls.push({ tenantId, orderId }); return { orderId }; } });
  assert.deepEqual(calls, [{ tenantId: 'tenant-test', orderId: 'order-1' }]);
});

test('Wanda order integration API is authenticated, tenant-pinned and read-only', async () => {
  const calls = [];
  await withServer(async (baseUrl) => {
    const rejected = await fetch(`${baseUrl}/api/wanda/orders`);
    assert.equal(rejected.status, 401);
    const wrong = await fetch(`${baseUrl}/api/wanda/orders`, { headers: { authorization: 'Bearer wrong' } });
    assert.equal(wrong.status, 401);
    const listed = await fetch(`${baseUrl}/__plugin__/api/wanda/orders?limit=25`, { headers: { authorization: 'Bearer wanda-secret' } });
    assert.equal(listed.status, 200);
    assert.deepEqual(await listed.json(), {
      source: 'wanda', orders: [{ orderId: 'order-1' }], count: 1, observedCount: 1,
    });
    const detail = await fetch(`${baseUrl}/__plugin__/api/wanda/orders/order-1`, { headers: { authorization: 'Bearer wanda-secret' } });
    assert.equal(detail.status, 200);
    assert.deepEqual(await detail.json(), { source: 'wanda', order: { orderId: 'order-1' } });
  }, {
    orderApiKey: 'wanda-secret', orderApiTenantId: 'tenant-wanda',
    listOrders: async (tenantId, limit) => {
      calls.push(['list', tenantId, limit]);
      return { orders: [{ orderId: 'order-1' }], count: 1, observedCount: 1 };
    },
    getOrder: async (tenantId, orderId) => {
      calls.push(['detail', tenantId, orderId]);
      return { orderId };
    },
  });
  assert.deepEqual(calls, [['list', 'tenant-wanda', 25], ['detail', 'tenant-wanda', 'order-1']]);
});

test('Wanda fulfillment API requires idempotency and delegates authoritative ticket delivery', async () => {
  const calls = [];
  await withServer(async (baseUrl) => {
    const missingKey = await fetch(`${baseUrl}/__plugin__/api/wanda/orders/order-1/fulfillment`, {
      method: 'POST', headers: { authorization: 'Bearer wanda-secret', 'content-type': 'application/json' },
      body: JSON.stringify({ ticket_codes: ['WANDA-001'] }),
    });
    assert.equal(missingKey.status, 400);
    const response = await fetch(`${baseUrl}/__plugin__/api/wanda/orders/order-1/fulfillment`, {
      method: 'POST',
      headers: { authorization: 'Bearer wanda-secret', 'content-type': 'application/json', 'idempotency-key': 'fulfillment-1' },
      body: JSON.stringify({ ticket_codes: ['WANDA-001'], movie_name: '奥德赛' }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { source: 'wanda', status: 'submitted' });
  }, {
    orderApiKey: 'wanda-secret', orderApiTenantId: 'tenant-wanda', fulfillmentEnabled: true,
    fulfillOrder: async (tenantId, orderId, request, idempotencyKey) => {
      calls.push({ tenantId, orderId, request, idempotencyKey });
      return { status: 'submitted' };
    },
  });
  assert.deepEqual(calls, [{
    tenantId: 'tenant-wanda', orderId: 'order-1',
    request: { ticket_codes: ['WANDA-001'], movie_name: '奥德赛' }, idempotencyKey: 'fulfillment-1',
  }]);
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
