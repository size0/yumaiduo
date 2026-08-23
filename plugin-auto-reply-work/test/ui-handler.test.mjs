import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import http from 'node:http';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { BackendRequestError } from '../src/backend-client.mjs';
import { createUiHandler } from '../src/ui-handler.mjs';

test('local UI preview serves assets and tenant-scoped API without exposing a secret', async (t) => {
  let savedPatch = null;
  let savedShop = null;
  let uploadedReplyImage = null;
  let resolvedManualTask = null;
  let updatedManualTask = null;
  const handler = createUiHandler({
    config: {
      projectRoot: fileURLToPath(new URL('..', import.meta.url)),
      coreUrl: 'https://core.example.com',
      maxUiBodyBytes: 64 * 1024,
      allowLocalUiBypass: true,
      manifest: { id: 'wanda-seat-autoquote' },
    },
    platformRuntime: { verifyGateway() { return false; } },
    api: {
      async overview(tenantId) { return { tenantId }; },
      async getSettings() { return { ai_key_configured: true }; },
      async updateSettings(_tenantId, patch) { savedPatch = patch; return { ai_key_configured: true }; },
      async uploadReplyTemplateImage(tenantId, input) { uploadedReplyImage = { tenantId, ...input }; return { key: input.key, image_url: 'https://cdn.example/reply.png' }; },
      async listLogs() { return []; },
      async getConversationLearningSummary() { return { observed_turn_count: 12, experience_draft_count: 1, model_training_enabled: false }; },
      async listAgentEvaluations() { return []; },
      async getAgentTrace(tenantId, runId) { return { tenant_id: tenantId, run_id: runId, steps: [] }; },
      async getAgentCanaryReadiness() { return { ready: false, audited_sample_count: 0, blockers: ['insufficient_audited_samples'] }; },
      async getAgentOfflineEvaluation() { return { ready: false, sample_count: 0, blockers: ['insufficient_image_samples'] }; },
      async listAgentHumanComparisons() { return [{ id: 'comparison-1', review: { status: 'unreviewed' } }]; },
      async reviewAgentHumanComparison(_tenantId, id, input) { return { id, review: { status: 'reviewed', ...input } }; },
      async listOperations() { return [{ buyer_label: 'b***01', stage: 'quoted', next_action: '等待买家确认报价' }]; },
      async listManualTasks() { return [{ task_id: 'manual-1', status: 'open' }]; },
      async updateManualTask(tenantId, taskId, input, actor) { updatedManualTask = { tenantId, taskId, input, actor }; return { task_id: taskId, ...input }; },
      async resolveManualTask(tenantId, taskId) { resolvedManualTask = { tenantId, taskId }; return { task_id: taskId, status: 'resolved' }; },
      async listQuoteAnalytics() { return { records: [{ id: 'quote-1', screening_order_success_rate: 50 }], summary: { sample_size: 2 } }; },
      async listTicketOrders() { return [{ order_id: 'order-paid', stage: 'paid_manual_delivery', platform_order_status_text: '买家已付款，等待卖家发货' }]; },
      async listOwnedShops() { return [{ account_unb: 'shop-1', shop_name: 'test-shop' }]; },
      async updateShopEnabled(tenantId, accountUnb, automationEnabled) {
        savedShop = { tenantId, accountUnb, automationEnabled };
        return { account_unb: accountUnb, automation_enabled: automationEnabled };
      },
    },
    logger: { error() {} },
  });
  const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    void handler(req, res, pathname);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const { port } = server.address();

  const page = await fetch(`http://127.0.0.1:${port}/ui`);
  assert.equal(page.status, 200);
  assert.doesNotMatch(page.headers.get('content-security-policy') ?? '', /frame-ancestors/u);
  const pageHtml = await page.text();
  assert.match(pageHtml, /人工回复，系统守住交易边界/u);
  assert.doesNotMatch(pageHtml, /reference-run|待报价预览|闲鱼改价金额核验/u);

  const retiredSettingsPage = await fetch(`http://127.0.0.1:${port}/ui/settings`);
  assert.equal(retiredSettingsPage.status, 404);

  const prefixedPage = await fetch(`http://127.0.0.1:${port}/__plugin__/ui`);
  assert.equal(prefixedPage.status, 200);

  const rootScript = await fetch(`http://127.0.0.1:${port}/app.js`);
  assert.equal(rootScript.status, 200);
  assert.match(rootScript.headers.get('content-type'), /text\/javascript/u);
  assert.match(rootScript.headers.get('cache-control'), /no-store/u);
  assert.match(await rootScript.text(), /createPluginSdk/u);

  const scopedStyles = await fetch(`http://127.0.0.1:${port}/ui/styles.css`);
  assert.equal(scopedStyles.status, 200);
  assert.match(scopedStyles.headers.get('content-type'), /text\/css/u);
  const scopedScript = await fetch(`http://127.0.0.1:${port}/ui/app.js`);
  assert.equal(scopedScript.status, 200);
  assert.match(await scopedScript.text(), /createPluginSdk/u);

  const slashScopedStyles = await fetch(`http://127.0.0.1:${port}/ui/ui/styles.css`);
  assert.equal(slashScopedStyles.status, 200);
  assert.match(slashScopedStyles.headers.get('content-type'), /text\/css/u);
  const slashScopedScript = await fetch(`http://127.0.0.1:${port}/ui/ui/app.js`);
  assert.equal(slashScopedScript.status, 200);
  assert.match(await slashScopedScript.text(), /createPluginSdk/u);
  const slashScopedSdk = await fetch(`http://127.0.0.1:${port}/ui/ui/sdk.js`);
  assert.equal(slashScopedSdk.status, 200);
  const retiredWorkbenchAlias = await fetch(`http://127.0.0.1:${port}/ui/workbench`);
  assert.equal(retiredWorkbenchAlias.status, 404);
  const retiredNestedWorkbenchAlias = await fetch(`http://127.0.0.1:${port}/ui/ui/workbench`);
  assert.equal(retiredNestedWorkbenchAlias.status, 404);
  for (const asset of ['workbench.js', 'workbench.css', 'workbench-model.js']) {
    const response = await fetch(`http://127.0.0.1:${port}/ui/${asset}`);
    assert.equal(response.status, 200);
    assert.match(response.headers.get('cache-control'), /no-store/u);
  }
  const retiredEvaluationAsset = await fetch(`http://127.0.0.1:${port}/ui/ui/evaluation-review.js`);
  assert.equal(retiredEvaluationAsset.status, 404);

  const overview = await fetch(`http://127.0.0.1:${port}/ui/api/overview`);
  assert.deepEqual(await overview.json(), { ok: true, data: { tenantId: 'local-dev' } });

  const gatewayOverview = await fetch(`http://127.0.0.1:${port}/api/overview`);
  assert.deepEqual(await gatewayOverview.json(), { ok: true, data: { tenantId: 'local-dev' } });

  for (const retiredPath of ['recognition/test', 'quotes/pending', 'price-changes/validate', 'quote-tasks']) {
    const retired = await fetch(`http://127.0.0.1:${port}/ui/api/${retiredPath}`, { method: retiredPath === 'recognition/test' || retiredPath === 'price-changes/validate' ? 'POST' : 'GET' });
    assert.equal(retired.status, 404);
  }

  const learningSummary = await fetch(`http://127.0.0.1:${port}/ui/api/conversation-learning-summary`);
  assert.deepEqual(await learningSummary.json(), { ok: true, data: { observed_turn_count: 12, experience_draft_count: 1, model_training_enabled: false } });
  const trace = await fetch(`http://127.0.0.1:${port}/ui/api/agent-runs/${encodeURIComponent('shadow:tenant:event')}/trace`);
  assert.deepEqual(await trace.json(), { ok: true, data: { tenant_id: 'local-dev', run_id: 'shadow:tenant:event', steps: [] } });
  const readiness = await fetch(`http://127.0.0.1:${port}/ui/api/agent-canary-readiness`);
  assert.deepEqual(await readiness.json(), { ok: true, data: { ready: false, audited_sample_count: 0, blockers: ['insufficient_audited_samples'] } });
  const offlineEvaluation = await fetch(`http://127.0.0.1:${port}/ui/api/agent-offline-evaluation`);
  assert.deepEqual(await offlineEvaluation.json(), { ok: true, data: { ready: false, sample_count: 0, blockers: ['insufficient_image_samples'] } });
  const comparisons = await fetch(`http://127.0.0.1:${port}/ui/api/agent-human-comparisons`);
  assert.deepEqual(await comparisons.json(), { ok: true, data: [{ id: 'comparison-1', review: { status: 'unreviewed' } }] });
  const reviewedComparison = await fetch(`http://127.0.0.1:${port}/ui/api/agent-human-comparisons/comparison-1`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ label: 'aligned', target: 'none', note: '' }) });
  assert.deepEqual(await reviewedComparison.json(), { ok: true, data: { id: 'comparison-1', review: { status: 'reviewed', label: 'aligned', target: 'none', note: '' } } });
  const operations = await fetch(`http://127.0.0.1:${port}/ui/api/operations`);
  assert.deepEqual(await operations.json(), { ok: true, data: [{ buyer_label: 'b***01', stage: 'quoted', next_action: '等待买家确认报价' }] });
  const manualTasks = await fetch(`http://127.0.0.1:${port}/ui/api/manual-tasks`);
  assert.deepEqual(await manualTasks.json(), { ok: true, data: [{ task_id: 'manual-1', status: 'open' }] });
  const startedTask = await fetch(`http://127.0.0.1:${port}/ui/api/manual-tasks/manual-1`, {
    method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ status: 'in_progress', priority: 'high', labels: ['核价'], note: '开始处理' }),
  });
  assert.deepEqual(await startedTask.json(), { ok: true, data: { task_id: 'manual-1', status: 'in_progress', priority: 'high', labels: ['核价'], note: '开始处理' } });
  assert.deepEqual(updatedManualTask, { tenantId: 'local-dev', taskId: 'manual-1', input: { status: 'in_progress', priority: 'high', labels: ['核价'], note: '开始处理' }, actor: { userId: 'local-dev' } });
  const resolvedTask = await fetch(`http://127.0.0.1:${port}/ui/api/manual-tasks/manual-1`, {
    method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ status: 'resolved' }),
  });
  assert.deepEqual(await resolvedTask.json(), { ok: true, data: { task_id: 'manual-1', status: 'resolved' } });
  assert.deepEqual(resolvedManualTask, { tenantId: 'local-dev', taskId: 'manual-1' });
  const quoteAnalytics = await fetch(`http://127.0.0.1:${port}/ui/api/quote-analytics`);
  assert.deepEqual(await quoteAnalytics.json(), { ok: true, data: { records: [{ id: 'quote-1', screening_order_success_rate: 50 }], summary: { sample_size: 2 } } });
  const orders = await fetch(`http://127.0.0.1:${port}/ui/api/orders`);
  assert.deepEqual(await orders.json(), { ok: true, data: [{ order_id: 'order-paid', stage: 'paid_manual_delivery', platform_order_status_text: '买家已付款，等待卖家发货' }] });
  const retiredFulfillment = await fetch(`http://127.0.0.1:${port}/ui/api/orders/order-paid/fulfillment`, {
    method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ status: 'ticket_issued' }),
  });
  assert.equal(retiredFulfillment.status, 404);

  const shops = await fetch(`http://127.0.0.1:${port}/ui/api/shops`);
  assert.deepEqual(await shops.json(), { ok: true, data: [{ account_unb: 'shop-1', shop_name: 'test-shop' }] });
  const shopSetting = await fetch(`http://127.0.0.1:${port}/ui/api/shops/shop-1`, {
    method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ automation_enabled: false }),
  });
  assert.deepEqual(await shopSetting.json(), { ok: true, data: { account_unb: 'shop-1', automation_enabled: false } });
  assert.deepEqual(savedShop, { tenantId: 'local-dev', accountUnb: 'shop-1', automationEnabled: false });
  const uploaded = await fetch(`http://127.0.0.1:${port}/ui/api/reply-template-images`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ key: 'first_contact_notice', filename: 'guide.png', content_type: 'image/png', data_base64: 'iVBORw0KGgo=' }),
  });
  assert.deepEqual(await uploaded.json(), { ok: true, data: { key: 'first_contact_notice', image_url: 'https://cdn.example/reply.png' } });
  assert.equal(uploadedReplyImage.tenantId, 'local-dev');

  uploadedReplyImage = null;
  const chunkBase = { upload_id: '1234567890abcdef', key: 'need_image', filename: 'large.png', content_type: 'image/png', total: 2 };
  const firstChunk = await fetch(`http://127.0.0.1:${port}/ui/api/reply-template-images`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ...chunkBase, index: 0, data_base64_chunk: 'iVBO' }),
  });
  assert.deepEqual(await firstChunk.json(), { ok: true, data: { complete: false, received: 1, total: 2 } });
  assert.equal(uploadedReplyImage, null);
  const finalChunk = await fetch(`http://127.0.0.1:${port}/ui/api/reply-template-images`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ...chunkBase, index: 1, data_base64_chunk: 'Rw0K' }),
  });
  assert.deepEqual(await finalChunk.json(), { ok: true, data: { key: 'need_image', image_url: 'https://cdn.example/reply.png' } });
  assert.equal(uploadedReplyImage.data_base64, 'iVBORw0K');
  assert.equal(uploadedReplyImage.tenantId, 'local-dev');

  const saved = await fetch(`http://127.0.0.1:${port}/ui/api/settings`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ ai_api_key: 'merchant-secret' }),
  });
  assert.deepEqual(await saved.json(), { ok: true, data: { ai_key_configured: true } });
  assert.deepEqual(savedPatch, { ai_api_key: 'merchant-secret' });
});

test('production UI serves static assets but rejects API requests without Yumaiduo gateway signature', async () => {
  const handler = createUiHandler({
    config: {
      projectRoot: '.',
      coreUrl: 'https://core.example.com',
      maxUiBodyBytes: 64 * 1024,
      allowLocalUiBypass: false,
      manifest: { id: 'wanda-seat-autoquote' },
    },
    platformRuntime: { verifyGateway() { return false; } },
    api: {},
    logger: { error() {} },
  });
  const req = { method: 'GET', headers: {}, socket: { remoteAddress: '203.0.113.2' } };
  const response = {
    status: null,
    body: '',
    writeHead(status) { this.status = status; },
    end(body) { this.body = body; },
  };
  assert.equal(await handler(req, response, '/ui/api/overview'), true);
  assert.equal(response.status, 401);
  assert.equal(JSON.parse(response.body).error, 'invalid_gateway_context');
});

test('temporary UI accepts only the loopback reverse proxy with the configured secret', async () => {
  const basicCredentials = 'admin:temporary-password';
  const basicAuthSha256 = createHash('sha256').update(basicCredentials).digest('hex');
  const handler = createUiHandler({
    config: {
      projectRoot: fileURLToPath(new URL('..', import.meta.url)),
      coreUrl: 'https://core.example.com',
      maxUiBodyBytes: 64 * 1024,
      allowLocalUiBypass: false,
      temporaryUi: {
        tenantId: '107',
        userId: 'temporary-admin',
        internalSecret: 's'.repeat(48),
        basicAuthSha256,
      },
      manifest: { id: 'wanda-seat-autoquote' },
    },
    platformRuntime: { verifyGateway() { return false; } },
    api: {
      async overview(tenantId) { return { tenantId }; },
    },
    logger: { error() {} },
  });
  const response = {
    status: null,
    body: '',
    writeHead(status) { this.status = status; },
    end(body) { this.body = body; },
  };
  const request = {
    method: 'GET',
    headers: { authorization: `Basic ${Buffer.from(basicCredentials).toString('base64')}` },
    socket: { remoteAddress: '127.0.0.1' },
  };
  assert.equal(await handler(request, response, '/ui/api/overview'), true);
  assert.equal(response.status, 200);
  assert.deepEqual(JSON.parse(response.body), { ok: true, data: { tenantId: '107' } });

  response.status = null;
  response.body = '';
  request.socket.remoteAddress = '203.0.113.2';
  assert.equal(await handler(request, response, '/ui/api/overview'), true);
  assert.equal(response.status, 401);
});

test('retired sample-evaluation APIs are not exposed, including to the backend bridge', async () => {
  const handler = createUiHandler({
    config: {
      projectRoot: fileURLToPath(new URL('..', import.meta.url)), coreUrl: 'https://core.example.com', maxUiBodyBytes: 64 * 1024,
      allowLocalUiBypass: true, reviewBackendTenantId: '107', backend: { bridgeKey: 'bridge-secret' }, manifest: { id: 'wanda-seat-autoquote' },
    },
    platformRuntime: { verifyGateway() { return false; } }, api: {}, logger: { error() {} },
  });
  const response = { status: null, body: '', writeHead(status) { this.status = status; }, end(body) { this.body = body; } };
  const request = { method: 'GET', url: '/ui/api/review/samples', headers: { 'x-wanda-backend-bridge-key': 'bridge-secret' }, socket: { remoteAddress: '127.0.0.1' } };
  assert.equal(await handler(request, response, '/ui/api/review/samples'), true);
  assert.equal(response.status, 404);
  assert.equal(JSON.parse(response.body).error, 'not_found');
});
test('UI API maps backend request errors to actionable gateway errors', async () => {
  const handler = createUiHandler({
    config: {
      projectRoot: fileURLToPath(new URL('..', import.meta.url)),
      coreUrl: 'https://core.example.com',
      maxUiBodyBytes: 64 * 1024,
      allowLocalUiBypass: true,
      manifest: { id: 'wanda-seat-autoquote' },
    },
    platformRuntime: { verifyGateway() { return false; } },
    api: {
      async overview() {
        throw new BackendRequestError('backend request failed with HTTP 404', {
          status: 404,
          code: 'backend_not_found',
          retryable: false,
        });
      },
    },
    logger: { error() {} },
  });
  const response = {
    status: null,
    headers: null,
    body: '',
    writeHead(status, headers) { this.status = status; this.headers = headers; },
    end(body) { this.body = body; },
  };
  const request = { method: 'GET', headers: {}, socket: { remoteAddress: '127.0.0.1' } };

  assert.equal(await handler(request, response, '/ui/api/overview'), true);
  assert.equal(response.status, 502);
  assert.deepEqual(JSON.parse(response.body), {
    ok: false,
    error: 'backend_not_found',
    message: 'backend request failed with HTTP 404',
  });
});



