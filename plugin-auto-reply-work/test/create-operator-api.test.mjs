import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { createOperatorApi } from '../src/admin/create-operator-api.mjs';

function harness() {
  const calls = [];
  const runtime = {
    automation_enabled: true,
    recognition_enabled: true,
    quote_enabled: true,
    auto_price_change: false,
    ai_reply_enabled: true,
    conversation_agent_mode: 'shadow',
    low_confidence_threshold: 0.9,
  };
  const policy = {
    wplus_adjustment_cents: -290,
    wplus_member_price_threshold_cents: 6000,
    regular_adjustment_cents: 100,
    max_auto_order_amount_cents: 20000,
  };
  const backendClient = {
    async getRuntimeSettings() { calls.push('get-runtime'); return { settings: runtime }; },
    async getQuotePolicy(tenantId) { calls.push(['get-policy', tenantId]); return { policy }; },
    async updateRuntimeSettings(patch) { calls.push(['update-runtime', patch]); return { settings: { ...runtime, ...patch } }; },
    async updateQuotePolicy(tenantId, patch) { calls.push(['update-policy', tenantId, patch]); return { policy: { ...policy, ...patch } }; },
  };
  const storage = {
    eventStore: {}, conversationContextStore: {}, agentEvaluationStore: {}, agentRunStore: {},
    agentManualTaskStore: {}, agentHumanComparisonStore: {},
  };
  const platformRuntime = { createClient() { return {}; } };
  const config = { replyTemplateImageUploadUrl: 'https://upload.test/image' };
  const api = createOperatorApi({ config, platformRuntime, backendClient, storage, logger: { warn() {} } });
  return { api, calls };
}

test('exposes the complete frozen operator API contract', () => {
  const { api } = harness();
  assert.equal(Object.isFrozen(api), true);
  assert.deepEqual(Object.keys(api).sort(), [
    'createAgentCorrection', 'createKnowledgeEntry', 'getAgentCanaryReadiness', 'getAgentOfflineEvaluation', 'getAgentTrace',
    'getConversationLearningSummary', 'getSettings', 'listAgentEvaluations', 'listAgentHumanComparisons',
    'listCorrections', 'listKnowledgeBase', 'listLogs', 'listManualTasks', 'listOperations', 'listOwnedShops',
    'listQuoteAnalytics', 'listTicketOrders', 'overview', 'resolveManualTask', 'reviewAgentEvaluation',
    'reviewAgentHumanComparison', 'reviewCorrection', 'updateKnowledgeEntry', 'updateManualTask', 'updateSettings',
    'updateShopEnabled', 'uploadReplyTemplateImage',
  ]);
});

test('reads and merges authoritative runtime and quote-policy settings', async () => {
  const { api, calls } = harness();
  const result = await api.getSettings('tenant-1');
  assert.equal(result.conversation_agent_mode, 'shadow');
  assert.equal(result.price_change_enabled, false);
  assert.equal(result.wplus_adjustment_cents, -290);
  assert.deepEqual(calls, ['get-runtime', ['get-policy', 'tenant-1']]);
});

test('updates runtime and quote policy through their existing separate backend ports', async () => {
  const { api, calls } = harness();
  const result = await api.updateSettings('tenant-1', {
    automation_enabled: false,
    price_change_enabled: true,
    wplus_adjustment_cents: -100,
    max_auto_order_amount_cents: 15000,
    ignored: 'value',
  });
  assert.equal(result.automation_enabled, false);
  assert.equal(result.price_change_enabled, true);
  assert.equal(result.wplus_adjustment_cents, -100);
  assert.deepEqual(calls, [
    ['update-runtime', { automation_enabled: false, auto_price_change: true }],
    ['update-policy', 'tenant-1', { wplus_adjustment_cents: -100, max_auto_order_amount_cents: 15000 }],
  ]);
});

test('application delegates the complete admin surface instead of retaining operator handlers', async () => {
  const source = await readFile(new URL('../src/application.mjs', import.meta.url), 'utf8');
  assert.match(source, /const operatorApi = createOperatorApi/u);
  assert.match(source, /api: operatorApi/u);
  assert.doesNotMatch(source, /async function (?:overview|updateSettings|listTicketOrders|listAgentEvaluations)/u);
});

test('fails closed when operator API dependencies are incomplete', () => {
  const valid = harness();
  assert.throws(() => createOperatorApi({}), /operator API backendClient/u);
  assert.throws(() => createOperatorApi({ backendClient: { getRuntimeSettings() {} }, platformRuntime: {}, storage: {} }), /platformRuntime\.createClient/u);
  assert.equal(typeof valid.api.getSettings, 'function');
});
