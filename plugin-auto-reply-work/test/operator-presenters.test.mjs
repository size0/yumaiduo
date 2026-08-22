import assert from 'node:assert/strict';
import test from 'node:test';
import {
  agentEvaluationAuthoritativeSummary,
  agentEvaluationBuyerLabel,
  compactText,
  imageSignatureMatches,
  manualTaskReviewRecord,
  matchesOperationEvent,
  nonNegativeCentsOrNull,
  pricingAccountEvidenceSummary,
  pricingFormulaSummary,
  runtimePatchFromUi,
  runtimeToUiSettings,
  safeBuyerName,
  toLogRecord,
  toOperationActivity,
  toRecentRecord,
  uiApiError,
} from '../src/admin/operator-presenters.mjs';

test('normalizes backend runtime settings without leaking alternate field semantics', () => {
  const source = {
    automation_enabled: 1,
    recognition_enabled: false,
    quote_enabled: true,
    auto_price_change: true,
    ai_reply_enabled: true,
    conversation_agent_mode: 'invalid',
    low_confidence_threshold: 0.87,
    ai_reply_base_url: 'https://ai.test',
    ai_reply_model: 'model-a',
    ai_reply_key_configured: true,
    ai_reply_api_key_masked: '****abcd',
    updated_at: '2026-08-24T00:00:00Z',
  };
  const result = runtimeToUiSettings(source);
  assert.equal(result.automation_enabled, false);
  assert.equal(result.recognition_enabled, false);
  assert.equal(result.quote_enabled, true);
  assert.equal(result.price_change_enabled, true);
  assert.equal(result.conversation_agent_mode, 'shadow');
  assert.equal(result.minimum_confidence, 0.87);
  assert.equal(result.ai_key_configured, true);
  assert.equal(Object.isFrozen(result), true);
});

test('maps only supported operator setting fields back to backend names', () => {
  assert.deepEqual(runtimePatchFromUi({
    automation_enabled: true,
    recognition_enabled: false,
    quote_enabled: true,
    price_change_enabled: true,
    minimum_confidence: '0.91',
    ai_reply_enabled: true,
    ai_base_url: 'https://ai.test',
    ai_model: 'model-b',
    ai_api_key: 'server-only-key',
    clear_ai_api_key: true,
    conversation_agent_mode: 'shadow',
    reply_templates: { quote: 'ok' },
    unknown_field: 'ignored',
  }), {
    automation_enabled: true,
    recognition_enabled: false,
    quote_enabled: true,
    auto_price_change: true,
    low_confidence_threshold: 0.91,
    ai_reply_enabled: true,
    ai_reply_base_url: 'https://ai.test',
    ai_reply_model: 'model-b',
    ai_reply_api_key: 'server-only-key',
    ai_reply_clear_api_key: true,
    conversation_agent_mode: 'shadow',
    reply_templates: { quote: 'ok' },
  });
});

test('passes through every supported advanced runtime field and ignores unsupported keys', () => {
  const advancedFields = {
    ai_only_mode_enabled: true,
    conversation_agent_mode: 'shadow',
    ai_reply_system_prompt: 'prompt',
    ai_reply_shop_background: 'background',
    ai_reply_precautions: 'safe',
    ai_reply_style: 'brief',
    ai_reply_temperature: 0.2,
    ai_reply_timeout_seconds: 10,
    ai_reply_fallback: 'handoff',
    ai_reply_daily_limit: 100,
    ai_reply_cooldown_seconds: 5,
    ai_reply_memory_hours: 24,
    ai_reply_memory_depth: 20,
    ai_reply_delay_seconds: 2,
    ai_reply_manual_takeover_seconds: 60,
    shop_execution_modes: {},
    shop_automation_overrides: {},
    shop_feature_overrides: {},
    reply_templates: {},
    reply_template_images: {},
  };
  assert.deepEqual(runtimePatchFromUi({ ...advancedFields, unsupported: true }), advancedFields);
});

test('validates reply-template image signatures independently of MIME claims', () => {
  assert.equal(imageSignatureMatches(Buffer.from('89504e470d0a1a0a00000000', 'hex'), 'image/png'), true);
  assert.equal(imageSignatureMatches(Buffer.from('ffd8ff000000000000000000', 'hex'), 'image/jpeg'), true);
  assert.equal(imageSignatureMatches(Buffer.from('524946460000000057454250', 'hex'), 'image/webp'), true);
  assert.equal(imageSignatureMatches(Buffer.alloc(12), 'image/png'), false);
  assert.equal(imageSignatureMatches(Buffer.alloc(12), 'image/gif'), false);
});

test('preserves bounded operator record and manual-task presentation contracts', () => {
  assert.deepEqual(toRecentRecord({
    key: 'tenant:event', status: 'failed', updatedAt: 'now', attempts: 1, lastError: 'failure',
    envelope: { event: 'im.message.received', payload: { accountUnb: 'shop', chatId: 'chat', peerUnb: 'buyer', orderId: 'order', content: ' hello   world ' } },
    result: { quote_failure_code: 'need_image' },
  }), {
    id: 'tenant:event', event: 'im.message.received', status: 'failed', updated_at: 'now',
    account_unb: 'shop', chat_id: 'chat', peer_unb: 'buyer', order_id: 'order', summary: 'hello world',
    result: { quote_failure_code: 'need_image' }, error: 'failure',
  });
  assert.deepEqual(manualTaskReviewRecord({
    task_id: 'task-1', updated_at: 'now', account_unb: 'shop', chat_id: 'chat', peer_unb: 'buyer',
    order_id: null, summary: '人工处理', reason_code: 'manual_required',
  }), {
    id: 'manual:task-1', manual_task_id: 'task-1', event: 'agent.manual_task', status: 'failed',
    updated_at: 'now', account_unb: 'shop', chat_id: 'chat', peer_unb: 'buyer', order_id: null,
    summary: '人工处理', error: 'manual_required',
  });
});

test('presents buyer identity, authoritative outcomes, and operation activity safely', () => {
  assert.equal(safeBuyerName({ buyerNick: ' 买家  A ' }), '买家 A');
  assert.equal(agentEvaluationBuyerLabel({ buyerNick: '买家A' }), '买家A');
  assert.equal(agentEvaluationBuyerLabel({ peerUnb: 'peer-123456' }), '买家 …3456');
  assert.equal(agentEvaluationBuyerLabel({}), '买家（身份未知）');
  assert.equal(agentEvaluationAuthoritativeSummary({ preview_status: 'preview_ready' }), '确定性系统：已形成实时报价');
  assert.equal(agentEvaluationAuthoritativeSummary({ actions: [{ status: 'succeeded' }] }), '确定性系统：已按既有规则完成本轮回复');
  assert.equal(agentEvaluationAuthoritativeSummary({ actions: [{ status: 'skipped' }] }), '确定性系统：本轮已安全跳过');
  assert.equal(agentEvaluationAuthoritativeSummary({}), '确定性系统：本轮没有形成可对照的交易结果');

  const operation = { account_unb: 'shop', chat_id: 'chat', order_id: 'order' };
  assert.equal(matchesOperationEvent(operation, { envelope: { payload: { accountUnb: 'shop', chatId: 'chat' } } }), true);
  assert.equal(matchesOperationEvent(operation, { envelope: { payload: { orderId: 'order' } } }), true);
  assert.equal(matchesOperationEvent(operation, { envelope: { payload: {} } }), false);
  assert.deepEqual(toOperationActivity({
    status: 'completed', updatedAt: 'now', envelope: { event: 'im.message.received' },
    result: { actions: [{ status: 'succeeded' }] },
  }), { event: 'im.message.received', status: 'completed', updated_at: 'now', summary: 'succeeded' });
});

test('presents operational logs and numeric fields with bounded defaults', () => {
  assert.equal(nonNegativeCentsOrNull(0), 0);
  assert.equal(nonNegativeCentsOrNull(-1), null);
  assert.equal(nonNegativeCentsOrNull(1.5), null);
  assert.equal(compactText(' a   b ', 3), 'a b');
  assert.deepEqual(toLogRecord({
    key: 'event-1', updatedAt: 'now', status: 'completed', attempts: 2, lastError: null,
    envelope: { event: 'order.created' }, result: { ignored_event: true, reason: 'not applicable' },
  }), {
    id: 'event-1', time: 'now', event: 'order.created', status: 'completed', attempts: 2,
    error: null, diagnostic: '已忽略：not applicable',
  });
});

test('summarizes opaque pricing-account evidence without exposing account references', () => {
  const summary = pricingAccountEvidenceSummary([
    { pricing_account_ref: 'a'.repeat(32) },
    { pricing_account_ref: 'a'.repeat(32) },
    { pricing_account_ref: 'b'.repeat(32) },
    { pricing_account_ref: 'invalid' },
    {},
  ]);
  assert.deepEqual(summary, {
    pricing_account_evidence_count: 3,
    pricing_account_unknown_count: 2,
    pricing_account_count: 2,
  });
  assert.equal(Object.isFrozen(summary), true);
  assert.doesNotMatch(JSON.stringify(summary), /a{32}|b{32}/u);
});

test('creates stable API errors and pricing policy descriptions', () => {
  const error = uiApiError(422, 'invalid_input');
  assert.equal(error.message, 'invalid_input');
  assert.equal(error.status, 422);
  assert.equal(error.code, 'invalid_input');
  assert.match(pricingFormulaSummary({}), /W\+区/u);
  assert.match(pricingFormulaSummary({ wplus_adjustment_cents: 100 }), /上调1\.00元/u);
});
