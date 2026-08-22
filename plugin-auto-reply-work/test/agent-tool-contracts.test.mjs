import assert from 'node:assert/strict';
import test from 'node:test';
import {
  AGENT_TOOL_CONTRACTS,
  validateToolObservation,
} from '../src/agent/agent-tool-contracts.mjs';

test('every runtime tool has an explicit risk and observation contract', () => {
  for (const tool of [
    'inspect_ticket_request', 'recognize_image', 'resolve_showtime', 'quote_realtime',
    'recognize_and_quote', 'list_available_wplus_seats', 'record_seat_preference',
    'confirm_active_quote', 'create_manual_task', 'read_linked_order', 'policy_guard',
  ]) {
    const contract = AGENT_TOOL_CONTRACTS[tool];
    assert.ok(contract, `${tool} must have a contract`);
    assert.ok(['read', 'write', 'external_temporary_write'].includes(contract.effect));
    assert.ok(Array.isArray(contract.allowed_fact_keys));
    assert.ok(Array.isArray(contract.allowed_next_actions));
  }
  assert.equal(AGENT_TOOL_CONTRACTS.create_manual_task.effect, 'write');
  assert.equal(AGENT_TOOL_CONTRACTS.quote_realtime.effect, 'external_temporary_write');
  assert.equal(AGENT_TOOL_CONTRACTS.read_linked_order.effect, 'read');
});

test('tool contracts accept the bounded authoritative quote shape', () => {
  const observation = {
    status: 'success', tool: 'quote_realtime', summary: '实时核价完成',
    facts: {
      quote_succeeded: true, unit_quote_cents: 5300, total_quote_cents: 10600,
      ticket_count: 2, pricing_rule_version: 'rule-v1',
      _quote_delivery: { type: 'quote', total_quote_cents: 10600 },
    },
    authoritative_reply: '53元/张，2张合计106元。', next_actions: ['respond'], stop_reason: null,
  };
  assert.equal(validateToolObservation('quote_realtime', observation, { mode: 'active' }), observation);
});

test('tool contracts reject undeclared facts, actions, replies, and write tools outside active mode', () => {
  assert.throws(() => validateToolObservation('read_linked_order', {
    status: 'success', tool: 'read_linked_order', summary: '读取完成',
    facts: { lifecycle: 'paid', buyer_secret: 'unsafe' }, next_actions: ['respond'], stop_reason: null,
  }, { mode: 'shadow' }), /undeclared fact/u);
  assert.throws(() => validateToolObservation('inspect_ticket_request', {
    status: 'success', tool: 'inspect_ticket_request', summary: '检查完成', facts: {},
    authoritative_reply: '模型不应从检查工具形成权威回复', next_actions: ['respond'], stop_reason: null,
  }, { mode: 'shadow' }), /authoritative reply/u);
  assert.throws(() => validateToolObservation('recognize_image', {
    status: 'success', tool: 'recognize_image', summary: '识图完成', facts: {},
    next_actions: ['request_price_change'], stop_reason: null,
  }, { mode: 'shadow' }), /undeclared next action/u);
  assert.throws(() => validateToolObservation('create_manual_task', {
    status: 'success', tool: 'create_manual_task', summary: '已创建', facts: { manual_task_created: true },
    authoritative_reply: '已转人工', next_actions: ['respond'], stop_reason: null,
  }, { mode: 'shadow' }), /write tool is disabled/u);
});
