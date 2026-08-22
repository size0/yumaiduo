import test from 'node:test';
import assert from 'node:assert/strict';
import { createConversationAgent } from '../src/agent/conversation-agent.mjs';
import { normalizeAgentPlan } from '../src/agent/agent-schema.mjs';
import { authorizeAgentPlan, guardAgentPlan } from '../src/agent/policy-engine.mjs';
import { composeAgentReply } from '../src/agent/response-composer.mjs';

const baseContext = Object.freeze({
  event_id: 'event-1', tenant_id: 'tenant-1', latest_message: '郑州中原万达两张多少钱',
  has_image: false, settings: { recognition_enabled: true, quote_enabled: true },
  state: { facts: {}, messages: [] }, observations: [],
});

test('agent plan accepts only the bounded customer-service action space', () => {
  assert.deepEqual(normalizeAgentPlan({
    intent: '选座核价', confidence: 0.93, goal: '查询实时价格', action: 'start_quote',
    arguments: { use_current_message: true }, missing_fields: [], reply: '', needs_human: false, reason: '信息完整',
  }), {
    intent: '选座核价', confidence: 0.93, goal: '查询实时价格', action: 'start_quote',
    arguments: { use_current_message: true }, missing_fields: [], reply: '', needs_human: false, reason: '信息完整',
  });
  for (const action of ['inspect_ticket_request', 'recognize_image', 'resolve_showtime', 'quote_realtime', 'read_active_quote', 'read_linked_order', 'request_price_change', 'create_manual_task', 'get_manual_task_status']) {
    assert.equal(normalizeAgentPlan({ intent: '选座核价', confidence: 0.9, goal: '推进', action, arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '受控工具' }).action, action);
  }
  assert.throws(() => normalizeAgentPlan({
    intent: '选座核价', confidence: 1, goal: '改价', action: 'change_price', arguments: {},
    missing_fields: [], reply: '', needs_human: false, reason: '直接改价',
  }), /unsupported agent action/u);
});

test('agent plan rejects model-supplied money and order execution arguments', () => {
  assert.throws(() => normalizeAgentPlan({
    intent: '票价咨询', confidence: 0.9, goal: '报价', action: 'start_quote',
    arguments: { amount_cents: 1, order_id: 'unsafe' }, missing_fields: [], reply: '', needs_human: false, reason: '报价',
  }), /forbidden agent argument/u);
});

test('agent plan accepts only generalized low-risk conversation experience candidates', () => {
  const normalized = normalizeAgentPlan({
    intent: '其他', confidence: 0.93, goal: '回答流程问题', action: 'respond', arguments: {}, missing_fields: [],
    reply: '请发送完整选座页截图并说明需要的张数。', needs_human: false, reason: '买家询问需要提供什么',
    experience_candidate: {
      topic: '图片要求', question_pattern: '买家询问核价前需要提供什么资料',
      response_guidance: '简短说明需要完整选座页截图和明确张数，不重复追问已知信息。',
      example_reply: '请发送完整选座页截图并说明需要的张数。', outcome_signal: 'buyer_progressed', confidence: 0.91,
    },
  });
  assert.equal(normalized.experience_candidate.topic, '图片要求');
  assert.equal(normalized.experience_candidate.confidence, 0.91);
  for (const unsafe of [
    { topic: '服务流程', question_pattern: '买家问价格', response_guidance: '给买家优惠到三十元', example_reply: '价格三十元', outcome_signal: 'buyer_acknowledged', confidence: 0.9 },
    { topic: '服务流程', question_pattern: '订单问题', response_guidance: '查询订单一二三四五六', example_reply: '已经改价可以付款', outcome_signal: 'buyer_acknowledged', confidence: 0.9 },
  ]) {
    assert.throws(() => normalizeAgentPlan({
      intent: '其他', confidence: 0.9, goal: '回答', action: 'respond', arguments: {}, missing_fields: [], reply: '好的', needs_human: false, reason: '普通咨询', experience_candidate: unsafe,
    }), /unsafe conversation experience/u);
  }
});

test('deterministic guards correct unsafe or context-blind model routing', () => {
  const plan = normalizeAgentPlan({ intent: '选座核价', confidence: 0.9, goal: '询问城市', action: 'ask_for_city', arguments: {}, missing_fields: ['城市'], reply: '请补城市', needs_human: false, reason: '模型未先识图' });
  assert.equal(guardAgentPlan(plan, { ...baseContext, has_image: true }).action, 'recognize_image');
  const imageSeatPlan = normalizeAgentPlan({ ...plan, action: 'record_seat_preference', reply: '' });
  const imageContext = { ...baseContext, has_image: true, latest_message: '我要9排11、12座', now: 1_000, state: { facts: { quote_total_cents: 10_000, quote_ticket_count: 2, quote_expires_at: 2_000 } } };
  assert.equal(guardAgentPlan(imageSeatPlan, { ...imageContext, observations: [{ tool: 'recognize_image', status: 'success', facts: { cinema: '测试万达影城' } }] }).action, 'resolve_showtime');
  assert.equal(guardAgentPlan(imageSeatPlan, { ...imageContext, observations: [{ tool: 'recognize_image', status: 'success', facts: { cinema: '测试万达影城' } }, { tool: 'resolve_showtime', status: 'success' }] }).action, 'quote_realtime');
  assert.equal(guardAgentPlan(plan, { ...baseContext, latest_message: '确认', now: 1_000, state: { facts: { quote_total_cents: 10_000, quote_ticket_count: 2, quote_expires_at: 2_000 } } }).action, 'confirm_quote');
  assert.equal(guardAgentPlan(plan, { ...baseContext, latest_message: '想要11排18和19座', state: { facts: { quote_total_cents: 10_000, quote_ticket_count: 2, quote_expires_at: 2_000 } }, now: 1_000 }).action, 'record_seat_preference');
  assert.equal(guardAgentPlan(plan, { ...baseContext, latest_message: '改好了吗', state: { facts: { order_id: 'internal', stage: 'waiting_payment' } } }).action, 'get_order_status');
  for (const latestMessage of ['不是53？', '这个呢']) {
    assert.equal(guardAgentPlan(plan, { ...baseContext, latest_message: latestMessage, now: 1_000, state: { facts: { quote_unit_cents: 5900, quote_total_cents: 5900, quote_ticket_count: 1, quote_expires_at: 2_000 } } }).action, 'read_active_quote');
  }
  const repeat = normalizeAgentPlan({ ...plan, action: 'start_quote', reply: '' });
  assert.equal(guardAgentPlan(repeat, { ...baseContext, latest_message: '我等等考虑一下', now: 1_000, state: { facts: { quote_total_cents: 10_000, quote_ticket_count: 2, quote_expires_at: 2_000 } } }).action, 'wait');
  assert.equal(guardAgentPlan(repeat, { ...baseContext, latest_message: '红点标出来两位置能买吗' }).action, 'record_seat_preference');
  assert.equal(guardAgentPlan(repeat, { ...baseContext, latest_message: '什么时候出票' }).action, 'handoff');
  assert.equal(guardAgentPlan(repeat, { ...baseContext, latest_message: '什么时候出票', state: { facts: { order_id: 'internal', stage: 'waiting_payment' } } }).action, 'get_order_status');
  assert.equal(guardAgentPlan(repeat, { ...baseContext, latest_message: '你已发货' }).action, 'handoff');
  assert.equal(guardAgentPlan(repeat, { ...baseContext, latest_message: '谢谢大哥' }).action, 'wait');
  assert.equal(guardAgentPlan(repeat, { ...baseContext, latest_message: '可以' }).action, 'wait');
  const askAgain = normalizeAgentPlan({ ...plan, action: 'ask_for_image', reply: '请发截图' });
  assert.equal(guardAgentPlan(askAgain, { ...baseContext, latest_message: '多少钱', now: 10_000, state: { facts: {}, messages: [{ role: 'buyer', image: true, at: 9_000 }] } }).action, 'recognize_image');
  assert.equal(guardAgentPlan(repeat, { ...baseContext, latest_message: '郑州市', state: { facts: { quote_draft: { fields: {} } } } }).action, 'inspect_ticket_request');
  assert.equal(guardAgentPlan(repeat, { ...baseContext, latest_message: '郑州中原万达两张多少钱', state: { facts: { quote_draft: { fields: {} } } } }).action, 'inspect_ticket_request');
  const wrongTextVision = normalizeAgentPlan({ ...plan, action: 'recognize_image', reply: '' });
  assert.equal(guardAgentPlan(wrongTextVision, { ...baseContext, latest_message: '你好', has_image: false }).action, 'inspect_ticket_request');
  assert.equal(guardAgentPlan(wrongTextVision, { ...baseContext, latest_message: '宁波奉化万达多少', has_image: false, state: { facts: { quote_draft: { recognition_artifact: { status: 'recognized', recognition: {} } } } } }).action, 'resolve_showtime');
  const prematureQuote = normalizeAgentPlan({ ...plan, action: 'quote_realtime', reply: '' });
  assert.equal(guardAgentPlan(prematureQuote, { ...baseContext, latest_message: '多少钱', has_image: false, state: { facts: { quote_draft: { fields: {} } } } }).action, 'inspect_ticket_request');
});

test('policy authorizes active quote follow-ups only through the authoritative quote reader', () => {
  const result = authorizeAgentPlan(normalizeAgentPlan({
    intent: '票价咨询', confidence: 0.95, goal: '读取当前报价', action: 'read_active_quote',
    arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '买家追问当前报价',
  }), { ...baseContext, now: 1_000, state: { facts: { quote_total_cents: 5900, quote_ticket_count: 1, quote_expires_at: 2_000 } } });
  assert.deepEqual(result, { status: 'allowed', tool: 'read_active_quote', reason: 'active_quote_available' });
});

test('policy maps legacy and current order actions to the authoritative linked-order reader', () => {
  for (const action of ['get_order_status', 'read_linked_order']) {
    const result = authorizeAgentPlan(normalizeAgentPlan({
      intent: '订单进度', confidence: 0.95, goal: '读取订单', action, arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '订单咨询',
    }), { ...baseContext, state: { facts: { order_id: 'system-only' }, messages: [] } });
    assert.deepEqual(result, { status: 'allowed', tool: 'read_linked_order', reason: 'linked_order_available' });
  }
});

test('policy allows quote tools but never gives the agent transaction authority', () => {
  assert.deepEqual(authorizeAgentPlan(normalizeAgentPlan({
    intent: '选座核价', confidence: 0.9, goal: '报价', action: 'start_quote', arguments: {},
    missing_fields: [], reply: '', needs_human: false, reason: '买家询价',
  }), { ...baseContext, has_image: true }), { status: 'allowed', tool: 'recognize_and_quote', reason: 'legacy_quote_requested' });

  const confirmed = authorizeAgentPlan(normalizeAgentPlan({
    intent: '补充信息', confidence: 0.9, goal: '确认报价', action: 'confirm_quote', arguments: {},
    missing_fields: [], reply: '', needs_human: false, reason: '买家确认',
  }), {
    ...baseContext,
    now: 1_000,
    state: { facts: { quote_total_cents: 10_600, quote_ticket_count: 2, quote_expires_at: 2_000 } },
  });
  assert.deepEqual(confirmed, { status: 'allowed', tool: 'confirm_active_quote', reason: 'active_quote_confirmable' });
});

test('policy closes the flow when paid, taken over, or quote authorization is stale', () => {
  const plan = normalizeAgentPlan({
    intent: '补充信息', confidence: 0.9, goal: '确认报价', action: 'confirm_quote', arguments: {},
    missing_fields: [], reply: '', needs_human: false, reason: '确认',
  });
  assert.equal(authorizeAgentPlan(plan, { ...baseContext, now: 2_001, state: { facts: { quote_total_cents: 100, quote_ticket_count: 1, quote_expires_at: 2_000 } } }).status, 'denied');
  assert.deepEqual(authorizeAgentPlan(plan, { ...baseContext, state: { facts: { stage: 'paid_manual_delivery' } } }), { status: 'stop', tool: null, reason: 'paid_order' });
  assert.deepEqual(authorizeAgentPlan(plan, { ...baseContext, human_takeover: true }), { status: 'stop', tool: null, reason: 'human_takeover' });
});

test('response composer preserves authoritative quote text instead of model money', () => {
  const reply = composeAgentReply({
    plan: { reply: '给您优惠到1元，两张2元。' },
    observation: { status: 'success', tool: 'recognize_and_quote', facts: { total_quote_cents: 10_600 }, authoritative_reply: '53.00元/张，2张合计106.00元。' },
  });
  assert.equal(reply, '53.00元/张，2张合计106.00元。');
});

test('response composer blocks unverified transaction claims and unresolved template placeholders', () => {
  assert.equal(composeAgentReply({ plan: { reply: '已经锁座，可以付款了。' } }), null);
  assert.equal(composeAgentReply({ plan: { reply: '正在为您查询实时票价和库存，稍后报价。' } }), null);
  assert.equal(composeAgentReply({ plan: { reply: '请补充{影院全称}，再查询{影片}。' } }), null);
  assert.equal(composeAgentReply({ plan: { reply: '模型文本' }, observation: { authoritative_reply: '{价格}元/张。' } }), null);
  assert.equal(composeAgentReply({ plan: { reply: '请问您所在的城市是哪里？' } }), '请问您所在的城市是哪里？');
});

test('conversation agent returns an authoritative tool reply immediately without another model step', async () => {
  const plannerCalls = [];
  const planner = {
    async plan(input) {
      plannerCalls.push(input);
      return { intent: '选座核价', confidence: 0.96, goal: '报价', action: 'start_quote', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '截图询价' };
    },
  };
  const toolCalls = [];
  const agent = createConversationAgent({
    planner,
    tools: {
      async recognize_and_quote(input) {
        toolCalls.push(input);
        return { status: 'success', summary: '核价完成', facts: { ticket_count: 2, total_quote_cents: 10_600 }, authoritative_reply: '53.00元/张，2张合计106.00元。', next_actions: ['respond'] };
      },
    },
  });
  const result = await agent.runTurn({ ...baseContext, has_image: false, state: { facts: { quote_draft: { recognition_artifact: { status: 'recognized', recognition: {} } } }, messages: [] } });
  assert.equal(toolCalls.length, 1);
  assert.equal(plannerCalls.length, 1);
  assert.equal(result.status, 'reply');
  assert.equal(result.reason, 'authoritative_tool_response');
  assert.equal(result.reply, '53.00元/张，2张合计106.00元。');
  assert.deepEqual(result.trace.map((item) => item.action), ['start_quote']);
});

test('conversation agent supplies deterministic prompts for empty missing-information plans', async () => {
  const agent = createConversationAgent({
    planner: { async plan() { return { intent: '选座核价', confidence: 0.95, goal: '补全信息', action: 'ask_for_missing_information', arguments: {}, missing_fields: ['ticket_count'], reply: '', needs_human: false, reason: '缺少张数' }; } },
  });
  const result = await agent.runTurn(baseContext);
  assert.equal(result.status, 'reply');
  assert.equal(result.reply, '请补充需要的张数。');
});

test('conversation agent checkpoints observations and can resume a durable run', async () => {
  const checkpoints = [];
  const planner = { async plan(input) {
    return input.observations.length
      ? { intent: '选座核价', confidence: 0.98, goal: '回复', action: 'respond', arguments: {}, missing_fields: [], reply: '已读取结果。', needs_human: false, reason: '完成' }
      : { intent: '选座核价', confidence: 0.96, goal: '核价', action: 'recognize_image', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '调用工具' };
  } };
  const tools = { async recognize_image() { return { status: 'success', summary: '完成', facts: { status: 'recognized' }, next_actions: ['respond'] }; } };
  const first = createConversationAgent({ planner, tools, maxSteps: 1 });
  const interrupted = await first.runTurn({ ...baseContext, has_image: true }, { async onCheckpoint(value) { checkpoints.push(value); } });
  assert.equal(interrupted.reason, 'agent_step_limit');
  assert.equal(checkpoints.length, 1);
  assert.equal(checkpoints[0].observations.length, 1);

  const resumed = createConversationAgent({ planner, tools, maxSteps: 3 });
  const result = await resumed.runTurn({ ...baseContext, has_image: true, observations: checkpoints[0].observations, trace: checkpoints[0].trace });
  assert.equal(result.status, 'reply');
  assert.equal(result.trace.length, 2);
});

test('conversation agent reuses a journaled tool result instead of executing the tool twice', async () => {
  let toolCalls = 0;
  let finishes = 0;
  const planner = { async plan(input) {
    return input.observations.length
      ? { intent: '选座核价', confidence: 0.98, goal: '回复', action: 'respond', arguments: {}, missing_fields: [], reply: '模型回复', needs_human: false, reason: '完成' }
      : { intent: '选座核价', confidence: 0.98, goal: '识图', action: 'recognize_image', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '识图' };
  } };
  const agent = createConversationAgent({ planner, tools: { async recognize_image() { toolCalls += 1; return {}; } } });
  const result = await agent.runTurn({ ...baseContext, has_image: true }, {
    async onToolStart() { return { state: 'replay', observation: { status: 'success', tool: 'recognize_image', summary: '已恢复结果', facts: { image_type: 'OTHER' }, next_actions: ['respond'] } }; },
    async onToolFinish() { finishes += 1; },
  });
  assert.equal(result.status, 'reply');
  assert.equal(toolCalls, 0);
  assert.equal(finishes, 0);
});

test('conversation agent deterministically corrects realtime quote before showtime resolution', async () => {
  const planned = ['quote_realtime', 'resolve_showtime', 'quote_realtime', 'respond'];
  let quoteCalls = 0; let resolveCalls = 0;
  const agent = createConversationAgent({
    maxSteps: 5,
    planner: { async plan() { const action = planned.shift(); return { intent: '选座核价', confidence: 0.98, goal: '取得权威报价', action, arguments: {}, missing_fields: [], reply: action === 'respond' ? '已完成' : '', needs_human: false, reason: '按观察推进' }; } },
    tools: {
      async resolve_showtime() { resolveCalls += 1; return { status: 'success', tool: 'resolve_showtime', summary: '场次已匹配', facts: {}, next_actions: ['quote_realtime'] }; },
      async quote_realtime() { quoteCalls += 1; return { status: 'success', tool: 'quote_realtime', summary: '核价完成', facts: {}, authoritative_reply: '权威报价回复', next_actions: ['respond'] }; },
    },
  });
  const checkpoints = [];
  const result = await agent.runTurn({ ...baseContext, state: { facts: { quote_draft: { fields: {} } }, messages: [] }, observations: [{ status: 'success', tool: 'recognize_image', summary: '识图完成', facts: {}, next_actions: ['resolve_showtime'] }] }, { async onCheckpoint(value) { checkpoints.push(value); } });
  assert.equal(result.status, 'reply');
  assert.equal(result.reply, '权威报价回复');
  assert.equal(resolveCalls, 1);
  assert.equal(quoteCalls, 1);
  assert.deepEqual(result.trace.map((item) => item.action), ['resolve_showtime', 'quote_realtime']);
  assert.equal(checkpoints[0].observations.at(-1).tool, 'resolve_showtime');
  assert.equal(checkpoints[0].observations.at(-1).next_actions[0], 'quote_realtime');
});

test('conversation agent fails closed rather than repeating a tool with an unknown prior result', async () => {
  let toolCalls = 0;
  const agent = createConversationAgent({
    planner: { async plan() { return { intent: '选座核价', confidence: 0.98, goal: '核价', action: 'quote_realtime', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '核价' }; } },
    tools: { async quote_realtime() { toolCalls += 1; return {}; } },
  });
  const result = await agent.runTurn({ ...baseContext, observations: [{ status: 'success', tool: 'resolve_showtime', summary: '已匹配', facts: {}, next_actions: ['quote_realtime'] }] }, {
    async onToolStart() { return { state: 'unknown' }; },
  });
  assert.equal(result.status, 'handoff');
  assert.equal(result.reason, 'agent_tool_result_unknown');
  assert.equal(toolCalls, 0);
});

test('conversation agent blocks write tools before execution outside active mode', async () => {
  let calls = 0;
  const agent = createConversationAgent({
    mode: 'shadow',
    planner: { async plan() { return { intent: '人工接管', confidence: 0.99, goal: '建人工任务', action: 'create_manual_task', arguments: {}, missing_fields: [], reply: '', needs_human: true, reason: '需要人工' }; } },
    tools: { async create_manual_task() { calls += 1; return { status: 'success', facts: { manual_task_created: true }, next_actions: ['respond'] }; } },
  });
  const result = await agent.runTurn(baseContext);
  assert.equal(result.reason, 'shadow_write_tool_disabled');
  assert.equal(calls, 0);
});

test('conversation agent journals and fails closed when a tool violates its declared observation contract', async () => {
  let journaled = null;
  const agent = createConversationAgent({
    planner: { async plan() { return { intent: '订单进度', confidence: 0.99, goal: '读取订单', action: 'read_linked_order', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '读取权威订单' }; } },
    tools: { async read_linked_order() { return { status: 'success', summary: '读取完成', facts: { lifecycle: 'paid', undeclared_secret: 'x' }, next_actions: ['respond'] }; } },
  });
  const result = await agent.runTurn({ ...baseContext, state: { facts: { order_id: 'system-only' }, messages: [] } }, {
    async onToolFinish(_call, observation) { journaled = observation; },
  });
  assert.equal(result.status, 'handoff');
  assert.equal(result.reason, 'agent_tool_contract_violation');
  assert.equal(journaled.stop_reason, 'agent_tool_contract_violation');
  assert.equal(JSON.stringify(journaled).includes('undeclared_secret'), false);
});

test('conversation agent stops before another model or tool step when its durable deadline expires', async () => {
  let checks = 0;
  let toolCalls = 0;
  const agent = createConversationAgent({
    planner: { async plan() { return { intent: '选座核价', confidence: 0.98, goal: '识图', action: 'recognize_image', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '调用工具' }; } },
    tools: { async recognize_image() { toolCalls += 1; return { status: 'success', summary: '完成', facts: {}, next_actions: ['respond'] }; } },
  });
  const result = await agent.runTurn({ ...baseContext, has_image: true }, { shouldContinue: () => ++checks === 1 });
  assert.equal(result.status, 'handoff');
  assert.equal(result.reason, 'agent_deadline_exceeded');
  assert.equal(result.trace.length, 1);
  assert.equal(toolCalls, 0);
});

test('conversation agent stops safely on unknown plans and tool failures', async () => {
  const unknown = createConversationAgent({ planner: { async plan() { return { intent: '其他', confidence: 1, goal: '执行', action: 'delete_order', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: 'x' }; } }, tools: {} });
  assert.deepEqual(await unknown.runTurn(baseContext), { status: 'handoff', reason: 'invalid_agent_plan', reply: null, trace: [] });

  const failing = createConversationAgent({
    planner: { async plan() { return { intent: '选座核价', confidence: 1, goal: '报价', action: 'recognize_image', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: 'x' }; } },
    tools: { async recognize_image() { return { status: 'error', summary: '万达暂不可用', next_actions: ['handoff'], stop_reason: 'wanda_gateway_unavailable' }; } },
  });
  const failed = await failing.runTurn({ ...baseContext, has_image: true });
  assert.equal(failed.status, 'handoff');
  assert.equal(failed.reason, 'wanda_gateway_unavailable');
  assert.equal(failed.trace.length, 1);
});

test('conversation agent follows a single declared next action instead of repeating a completed tool', async () => {
  const calls = [];
  const agent = createConversationAgent({
    maxSteps: 5,
    planner: { async plan() { return { intent: '选座核价', confidence: 1, goal: '报价', action: 'recognize_image', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '模型重复识图' }; } },
    tools: {
      async recognize_image() { calls.push('recognize_image'); return { status: 'success', summary: '识图完成', facts: { image_type: 'SEAT_MAP' }, next_actions: ['resolve_showtime'] }; },
      async resolve_showtime() { calls.push('resolve_showtime'); return { status: 'success', summary: '场次完成', facts: {}, next_actions: ['quote_realtime'] }; },
      async quote_realtime() { calls.push('quote_realtime'); return { status: 'success', summary: '核价完成', facts: {}, authoritative_reply: '权威报价已形成。', next_actions: ['respond'] }; },
    },
  });
  const result = await agent.runTurn({ ...baseContext, has_image: true });
  assert.equal(result.status, 'reply');
  assert.equal(result.reply, '权威报价已形成。');
  assert.deepEqual(calls, ['recognize_image', 'resolve_showtime', 'quote_realtime']);
  assert.deepEqual(result.trace.map((item) => item.action), ['recognize_image', 'resolve_showtime', 'quote_realtime']);
});

test('conversation agent returns a final-step authoritative reply instead of step-limit handoff', async () => {
  const agent = createConversationAgent({
    maxSteps: 1,
    planner: { async plan() { return { intent: '选座核价', confidence: 1, goal: '报价', action: 'quote_realtime', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '执行最终工具' }; } },
    tools: { async quote_realtime() { return { status: 'success', summary: '核价完成', facts: {}, authoritative_reply: '最终安全回复。', next_actions: ['respond'] }; } },
  });
  const result = await agent.runTurn({ ...baseContext, observations: [{ status: 'success', tool: 'resolve_showtime', summary: '已匹配', facts: {}, next_actions: ['quote_realtime'] }] });
  assert.equal(result.status, 'reply');
  assert.equal(result.reason, 'authoritative_tool_response');
  assert.equal(result.reply, '最终安全回复。');
});
