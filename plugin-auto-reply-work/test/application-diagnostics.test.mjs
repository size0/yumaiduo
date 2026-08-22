import assert from 'node:assert/strict';
import test from 'node:test';
import { agentEvaluationTurnSummary, conversationLearningSummaryFrom, eventDiagnosticSummary, operationalLogStatus, quoteDiagnosticSummary, ticketIssuanceFromXianyuOrder } from '../src/application.mjs';

test('match diagnostics expose only the bounded requested identity facts needed by operators', () => {
  const summary = quoteDiagnosticSummary({
    safe_error_code: 'showtime_not_unique',
    failure_step: 'match',
    requested_match: {
      city: '苏州', cinema: '张家港万达广场店', movie: '空枪',
      date: '2026-08-19', showtime: '20:05', hall: '5号厅',
    },
    match: { result_count: 0 },
  });
  assert.match(summary, /请求 苏州 \/ 张家港万达广场店 \/ 空枪 \/ 2026-08-19 \/ 20:05 \/ 5号厅/u);
  assert.match(summary, /匹配 0 个/u);
  assert.ok(summary.length <= 120);
});

test('price-change business rejections become actionable diagnostics instead of a completed event with no error', () => {
  const result = {
    order_price_change: {
      status: 'rejected', code: 'CANNOT_MODIFY_FEE',
      diagnostics: {
        http_status: 400, provider_reason_code: 'CANNOT_MODIFY_FEE',
        current_total_cents: 1980, target_total_cents: 5300, direction: 'increase',
      },
    },
  };

  assert.equal(operationalLogStatus('completed', result), 'failed');
  assert.equal(
    eventDiagnosticSummary(result),
    '改价拒绝 CANNOT_MODIFY_FEE；金额 19.80→53.00元；方向 涨价；HTTP 400；上游 CANNOT_MODIFY_FEE',
  );
});

test('conversation learning summary distinguishes observation, reviewed experience, and model training', () => {
  const summary = conversationLearningSummaryFrom([
    { updatedAt: 300, result: { conversation_agent_mode: 'shadow', agent_turn_status: 'shadow' } },
    { updatedAt: 200, result: { conversation_agent_mode: 'shadow', agent_turn_status: 'failed' } },
    { updatedAt: 100, result: { conversation_agent_mode: 'active', agent_turn_status: 'reply' } },
    { updatedAt: 50, result: {} },
  ], [
    { source: 'conversation_experience', status: 'draft', enabled: false, evidence_count: 3 },
    { source: 'conversation_experience', status: 'approved', enabled: true, evidence_count: 2 },
    { source: 'manual', status: 'approved', enabled: true },
  ]);

  assert.deepEqual(summary, {
    model_training_enabled: false,
    automatic_activation_enabled: false,
    observed_turn_count: 3,
    shadow_turn_count: 2,
    active_turn_count: 1,
    agent_failure_count: 1,
    experience_draft_count: 1,
    experience_approved_count: 1,
    experience_enabled_count: 1,
    experience_evidence_count: 5,
    last_observed_at: new Date(300).toISOString(),
  });
});

test('agent evaluation shows only the current bounded buyer turn and redacts contact data', () => {
  assert.equal(agentEvaluationTurnSummary({
    content: '帮我看下 https://example.com/a，手机号13800138000，订单123456789012',
    imageUrls: ['https://img.alicdn.com/seat.png'],
  }), '买家说：帮我看下 [链接]，手机号[手机号]，订单[编号]；并发送1张图片');
  assert.equal(agentEvaluationTurnSummary({ imageUrls: ['https://img.alicdn.com/seat.png'] }), '买家发送1张图片');
});

test('shadow-agent records use plain operator language and clearly state that no action ran', () => {
  const result = {
    conversation_agent_mode: 'shadow', agent_turn_status: 'shadow', agent_intent: '选座核价', agent_confidence: 0.95,
    agent_actions: ['recognize_image', 'resolve_showtime', 'quote_realtime'], agent_turn_reason: 'quote_requested',
  };
  assert.equal(
    eventDiagnosticSummary(result),
    'AI观察（未执行）：识别为“选座核价”，建议“识别买家图片，然后匹配影院场次，然后调用实时核价”（置信度95%）。判断依据：买家正在询价。',
  );
  assert.equal(eventDiagnosticSummary({
    ...result, agent_intent: '订单进度', agent_confidence: 0.85, agent_turn_reason: 'agent_requested_handoff',
  }), 'AI观察（未执行）：识别为“订单进度”，建议“识别买家图片，然后匹配影院场次，然后调用实时核价”（置信度85%）。安全判断：应转人工处理。');
  assert.equal(eventDiagnosticSummary({
    ...result, agent_intent: '其他', agent_actions: ['wait'], agent_turn_reason: 'paid_order',
  }), 'AI观察（未执行）：识别为“其他问题”，建议“暂停自动处理”（置信度95%）。安全限制：订单已付款，不再核价或改价。');
  assert.equal(eventDiagnosticSummary({
    ...result, conversation_experience_status: 'draft_created', agent_intent: '其他', agent_actions: ['wait'], agent_turn_reason: 'agent_wait',
  }), 'AI观察（未执行）：识别为“其他问题”，建议“暂停自动处理”（置信度95%）。判断依据：无需继续回复。已提炼会话经验草稿，尚未生效。');
});

test('Xianyu shipped or completed status deterministically means ticket issued for this shop workflow', () => {
  assert.deepEqual(ticketIssuanceFromXianyuOrder({ read_status: 'available', order_status: 3, order_status_text: '卖家已发货，等待买家收货' }), {
    status: 'issued', evidence: '闲鱼已发货',
  });
  assert.deepEqual(ticketIssuanceFromXianyuOrder({ read_status: 'available', order_status: 4, order_status_text: '交易成功' }), {
    status: 'issued', evidence: '闲鱼交易完成',
  });
  assert.deepEqual(ticketIssuanceFromXianyuOrder({ read_status: 'available', order_status: 2, order_status_text: '买家已付款，等待卖家发货' }), {
    status: 'not_confirmed', evidence: '闲鱼尚未发货',
  });
  assert.deepEqual(ticketIssuanceFromXianyuOrder({ read_status: 'unavailable' }), {
    status: 'unknown', evidence: '闲鱼订单读取失败',
  });
});

test('expected buyer follow-ups and safe business stops are not mislabeled as execution failures', () => {
  assert.equal(operationalLogStatus('completed', { quote_failure_code: 'text_quote_missing_fields' }), 'waiting_input');
  assert.equal(operationalLogStatus('completed', { quote_failure_code: 'cinema_catalog_not_unique' }), 'waiting_input');
  assert.equal(operationalLogStatus('completed', { quote_failure_code: 'official_selection_unverifiable' }), 'waiting_input');
  assert.equal(operationalLogStatus('completed', { quote_failure_code: 'wplus_seats_unavailable' }), 'business_blocked');
  assert.equal(operationalLogStatus('completed', {
    quote_failure_code: 'showtime_not_unique',
    actions: [{ status: 'skipped', reason: 'duplicate_reply' }],
  }), 'waiting_input');
  assert.equal(eventDiagnosticSummary({ quote_failure_code: 'text_quote_missing_fields' }), '待买家补充：文字询价信息不完整');
  assert.match(eventDiagnosticSummary({ quote_diagnostics: { safe_error_code: 'showtime_not_unique', failure_step: 'match', match: { result_count: 0 } } }), /^待买家补充；/u);
});

test('successful and safety-critical automation results have readable operator summaries', () => {
  assert.equal(eventDiagnosticSummary({ order_price_change: { status: 'submitted', amount_cents: 5300 } }), '改价已提交；目标 53.00元');
  assert.equal(eventDiagnosticSummary({ actions: [{ status: 'skipped', reason: 'human_takeover' }] }), '跳过：人工接管');
  assert.equal(operationalLogStatus('completed', { quote_failure_code: 'temporary_lock_release_unverified' }), 'failed');
  assert.equal(operationalLogStatus('completed', {
    quote_failure_code: 'temporary_lock_release_unverified',
    actions: [{ status: 'skipped', reason: 'human_takeover' }],
  }), 'failed');
  assert.match(eventDiagnosticSummary({
    quote_failure_code: 'temporary_lock_release_unverified',
    actions: [{ status: 'skipped', reason: 'human_takeover' }],
  }), /^核价异常：/u);
});
