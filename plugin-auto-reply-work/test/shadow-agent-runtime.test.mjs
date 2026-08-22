import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { AgentRunStore } from '../src/agent/agent-run-store.mjs';
import { AGENT_RUNTIME_VERSION, createShadowAgentRuntime } from '../src/agent/shadow-agent-runtime.mjs';

const envelope = {
  id: 'event-1', tenantId: 'tenant-1', event: 'im.message.received', ts: 1,
  payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '两张多少钱', imageUrls: ['https://img.alicdn.com/a.png'] },
};

test('shadow agent fails closed when a successful source quote lacks its authoritative reply snapshot', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-runtime-')), 'runs.json'));
  await store.initialize();
  const plans = [];
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope, result: { preview_status: 'preview_ready' } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [{ role: 'buyer', text: '两张多少钱' }] }; } },
    planner: { async plan(input) {
      plans.push(input);
      const actions = ['recognize_image', 'resolve_showtime', 'quote_realtime', 'respond'];
      const action = actions[input.observations.length];
      return { intent: '选座核价', confidence: 0.98, goal: '按权威观察推进', action, arguments: {}, missing_fields: [], reply: action === 'respond' ? '影子建议不会发送' : '', needs_human: false, reason: '根据观察继续' };
    } },
    getSettings: async () => ({ recognition_enabled: true, quote_enabled: true }),
  });
  await runtime.schedule(envelope);
  const result = await runtime.tick();
  assert.equal(result.status, 'completed');
  assert.equal(plans.length, 3);
  const [run] = await store.list({ tenantId: 'tenant-1' });
  assert.equal(run.status, 'completed');
  assert.equal(run.result.runtime_version, AGENT_RUNTIME_VERSION);
  assert.deepEqual(run.result.trace.map((item) => item.action), ['recognize_image', 'resolve_showtime', 'quote_realtime']);
  assert.equal(run.result.status, 'handoff');
  assert.equal(run.result.reason, 'missing_authoritative_reply_snapshot');
  assert.equal(run.result.reply_generated, false);
  assert.equal(run.result.proposed_reply, undefined);
  assert.deepEqual(run.result.source_snapshot, { has_image: true, authoritative_outcome: 'quote_succeeded' });
});

test('shadow runtime replays a persisted authoritative reply without asking the model to rewrite it', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-authoritative-')), 'runs.json'));
  await store.initialize();
  let planned = 0;
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope, result: { preview_status: 'preview_ready', agent_reply_snapshot: { kind: 'quote', text: '实时单价50.00元/张，1张合计50.00元。' } } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan(input) {
      planned += 1;
      const action = ['recognize_image', 'resolve_showtime', 'quote_realtime'][input.observations.length];
      return { intent: '选座核价', confidence: 0.99, goal: '按顺序读取权威结果', action, arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '工具推进' };
    } },
    getSettings: async () => ({ recognition_enabled: true, quote_enabled: true }),
  });
  await runtime.schedule(envelope);
  await runtime.tick();
  const run = await store.get('shadow:tenant-1:event-1');
  assert.equal(planned, 3);
  assert.equal(run.result.status, 'reply');
  assert.equal(run.result.reason, 'authoritative_tool_response');
  assert.equal(run.result.proposed_reply, '实时单价50.00元/张，1张合计50.00元。');
  assert.equal(run.result.reply_generated, true);
});

test('shadow runtime stops a silent duplicate draft without asking the model to invent a reply', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-duplicate-')), 'runs.json'));
  await store.initialize();
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope, result: { preview_status: 'quote_deduplicated', quote_skipped: 'duplicate_quote_draft' } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan(input) {
      const action = ['recognize_image', 'resolve_showtime', 'quote_realtime'][input.observations.length];
      return { intent: '选座核价', confidence: 0.99, goal: '按顺序读取结果', action, arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '工具推进' };
    } },
    getSettings: async () => ({ recognition_enabled: true, quote_enabled: true }),
  });
  await runtime.schedule(envelope);
  await runtime.tick();
  const run = await store.get('shadow:tenant-1:event-1');
  assert.equal(run.result.status, 'handoff');
  assert.equal(run.result.reason, 'duplicate_quote_draft');
  assert.equal(run.result.reply_generated, false);
  assert.equal(run.result.proposed_reply, undefined);
});

test('shadow runtime uses a delivered deterministic follow-up as the authoritative tool reply', async () => {
  const followUpEnvelope = { ...envelope, payload: { ...envelope.payload, content: '8排还有吗', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-follow-up-')), 'runs.json'));
  await store.initialize();
  let planned = 0;
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: followUpEnvelope, result: { agent_reply_snapshot: { kind: 'conversation_follow_up', text: '当前实时可选座位以系统列表为准。' } } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() { planned += 1; return { intent: '选座核价', confidence: 0.99, goal: '查询座位', action: 'show_available_wplus_seats', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '座位咨询' }; } },
    getSettings: async () => ({ quote_enabled: true }),
  });
  await runtime.schedule(followUpEnvelope);
  await runtime.tick();
  const run = await store.get('shadow:tenant-1:event-1');
  assert.equal(planned, 1);
  assert.equal(run.result.status, 'reply');
  assert.equal(run.result.authoritative_reply_used, true);
  assert.equal(run.result.proposed_reply, '当前实时可选座位以系统列表为准。');
});

test('shadow seat-preference simulation preserves a delivered deterministic follow-up', async () => {
  const preferenceEnvelope = { ...envelope, payload: { ...envelope.payload, content: '7排5座', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-preference-')), 'runs.json'));
  await store.initialize();
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: preferenceEnvelope, result: { agent_reply_snapshot: { kind: 'conversation_follow_up', text: '已记录文字座位偏好，具体以出票时实时可选为准。' } } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() { return { intent: '选座核价', confidence: 0.99, goal: '记录偏好', action: 'record_seat_preference', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '文字座位' }; } },
    getSettings: async () => ({}),
  });
  await runtime.schedule(preferenceEnvelope);
  await runtime.tick();
  const run = await store.get('shadow:tenant-1:event-1');
  assert.equal(run.result.status, 'reply');
  assert.equal(run.result.authoritative_reply_used, true);
  assert.match(run.result.proposed_reply, /实时可选/u);
});

test('historical confirmation uses the source-time quote snapshot instead of later conversation state', async () => {
  const confirmationEnvelope = { ...envelope, payload: { ...envelope.payload, content: '确认', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-evaluation-confirmation-')), 'runs.json'));
  await store.initialize();
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return {
      key: 'tenant-1:event-1', status: 'completed', envelope: confirmationEnvelope,
      result: {
        agent_state_snapshot: { quote_total_cents: 10_000, quote_ticket_count: 2, quote_expires_at: 2_000, pricing_rule_version: 'v1', quote_reply_delivered: true, stage: 'quoted' },
        agent_reply_snapshot: { kind: 'conversation_follow_up', text: '请提交待付款订单，提交后请先不要付款，等待系统确认改价成功后再付款。' },
      },
    }; } },
    conversationContextStore: { async get() { return { facts: { stage: 'paid_manual_delivery' }, messages: [] }; } },
    planner: { async plan() { return { intent: '补充信息', confidence: 0.99, goal: '处理确认', action: 'wait', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '模型误判' }; } },
    getSettings: async () => ({}), now: () => 1_000,
  });
  await runtime.schedule(confirmationEnvelope, { mode: 'evaluation' });
  await runtime.tick();
  const [run] = await store.list({ tenantId: 'tenant-1' });
  assert.deepEqual(run.result.trace.map((item) => item.action), ['confirm_quote']);
  assert.equal(run.result.status, 'reply');
  assert.equal(run.result.authoritative_reply_used, true);
  assert.match(run.result.proposed_reply, /先不要付款/u);
});

test('historical evaluation runs are versioned, side-effect-free, and independently replayable', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-evaluation-runtime-')), 'runs.json'));
  await store.initialize();
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope, result: { preview_status: 'preview_ready' } }; } },
    conversationContextStore: { async get() { return { facts: { stage: 'paid_manual_delivery' }, messages: [{ role: 'buyer', text: '后来已付款' }] }; } },
    planner: { async plan(input) {
      assert.notEqual(input.state?.facts?.stage, 'paid_manual_delivery');
      const actions = ['recognize_image', 'resolve_showtime', 'quote_realtime', 'respond'];
      const action = actions[input.observations.length];
      return { intent: '选座核价', confidence: 0.99, goal: '历史离线评测', action, arguments: {}, missing_fields: [], reply: action === 'respond' ? '只记录不发送' : '', needs_human: false, reason: '按只读观察推进' };
    } },
    getSettings: async () => ({ recognition_enabled: true, quote_enabled: true }),
  });
  const scheduled = await runtime.schedule(envelope, { mode: 'evaluation' });
  assert.equal(scheduled.created, true);
  assert.match(scheduled.run.run_id, new RegExp(`^evaluation:${AGENT_RUNTIME_VERSION}:[a-f0-9]{64}$`, 'u'));
  const result = await runtime.tick();
  assert.equal(result.status, 'completed');
  const run = await store.get(scheduled.run.run_id);
  assert.equal(run.mode, 'evaluation');
  assert.equal(run.result.reply_generated, false);
  assert.equal(run.result.reason, 'missing_authoritative_reply_snapshot');
  assert.equal(run.result.reply_queued, undefined);
  assert.deepEqual(run.tool_calls.map((call) => call.tool), ['recognize_image', 'resolve_showtime', 'quote_realtime']);
});

test('durable active agent queues a bounded reply in the outbox instead of sending inline', async () => {
  const activeEnvelope = { ...envelope, payload: { ...envelope.payload, content: '图片怎么发', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-runtime-')), 'runs.json'));
  await store.initialize();
  const queuedReplies = [];
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: activeEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() { return { intent: '其他', confidence: 0.98, goal: '说明流程', action: 'respond', arguments: {}, missing_fields: [], reply: '请发送完整选座页截图并说明张数。', needs_human: false, reason: '流程咨询' }; } },
    getSettings: async () => ({}),
    replyOutboxStore: { async enqueue(input) { queuedReplies.push(input); return { created: true, entry: { status: 'pending' } }; } },
  });
  await runtime.schedule(activeEnvelope, { mode: 'active' });
  const result = await runtime.tick();
  assert.equal(result.status, 'completed');
  assert.equal(result.result.reply_queued, true);
  assert.deepEqual(queuedReplies, [{
    actionId: 'active:tenant-1:event-1:reply', runId: 'active:tenant-1:event-1', tenantId: 'tenant-1', mode: 'active',
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', text: '请发送完整选座页截图并说明张数。',
  }]);
  assert.equal((await store.get('active:tenant-1:event-1')).status, 'completed');
});

test('durable active image turn invokes real recognition, read-only resolution, and realtime quote tools in order', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-real-quote-')), 'runs.json'));
  await store.initialize();
  const calls = []; const queued = [];
  const plans = ['recognize_image', 'resolve_showtime', 'quote_realtime', 'respond'].map((action) => ({
    intent: '选座核价', confidence: 0.99, goal: '取得权威报价', action, arguments: {}, missing_fields: [], reply: action === 'respond' ? '模型报价不得采用' : '', needs_human: false, reason: '按工具顺序执行',
  }));
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() { return plans.shift(); } }, getSettings: async () => ({ recognition_enabled: true, quote_enabled: true }),
    quotePreviewClient: {
      async recognize(input) { calls.push(['recognize', input.id]); return { status: 'recognized', tenant_id: 'tenant-1', ticket_count: 1, recognition: { image_type: 'SEAT_MAP', cinema: '测试万达', movie: '测试电影', date: '2026-08-22', showtime: '19:30', official_selection: { is_selected: true, selected_seat_numbers: ['6排16座'], selected_count: 1 }, hand_drawn_circle: { exists: true } } }; },
      async resolveShowtime(input) { calls.push(['resolve', input.recognition.cinema]); return { ...input, status: 'resolved' }; },
      async quote(input) { calls.push(['quote', input.status]); return { status: 'preview_ready', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1, pricing_rule_version: 'quote-policy-test', recognition: input.recognition, reply_text: '实时单价50.00元/张，1张合计50.00元。' }; },
    },
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(envelope, { mode: 'active' });
  await runtime.tick();
  assert.deepEqual(calls, [['recognize', 'event-1'], ['resolve', '测试万达'], ['quote', 'resolved']]);
  assert.equal(queued[0].text, '实时单价50.00元/张，1张合计50.00元。\n接受本次报价请回复“确认”。');
  assert.deepEqual(queued[0].delivery, {
    type: 'quote', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1,
    pricing_rule_version: 'quote-policy-test', cinema: '测试万达', movie: '测试电影', date: '2026-08-22', showtime: '19:30', hall: '', quote_scope: '',
    member_cost_total_cents: null, original_price_total_cents: null, channel_fee_total_cents: null, pricing_source: '',
    circled_delivery_image_url: 'https://img.alicdn.com/a.png',
  });
  const run = await store.get('active:tenant-1:event-1');
  assert.deepEqual(run.observations.map((item) => item.tool), ['recognize_image', 'resolve_showtime', 'quote_realtime']);
});

test('active quote tool cannot execute before read-only showtime resolution', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-quote-order-')), 'runs.json'));
  await store.initialize();
  const plans = ['recognize_image', 'quote_realtime', 'resolve_showtime', 'quote_realtime', 'respond'].map((action) => ({
    intent: '选座核价', confidence: 0.99, goal: '核价', action, arguments: {}, missing_fields: [], reply: action === 'respond' ? '忽略模型金额' : '', needs_human: false, reason: '测试顺序门禁',
  }));
  let quoteExecutions = 0;
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() { return plans.shift(); } }, getSettings: async () => ({ recognition_enabled: true, quote_enabled: true }),
    quotePreviewClient: {
      async recognize() { return { status: 'recognized', tenant_id: 'tenant-1', ticket_count: 1, recognition: { image_type: 'SEAT_MAP', cinema: '测试万达', movie: '测试电影', date: '2026-08-22', showtime: '19:30', official_selection: { is_selected: true, selected_seat_numbers: ['6排16座'], selected_count: 1 }, hand_drawn_circle: { exists: false } } }; },
      async resolveShowtime(input) { return { ...input, status: 'resolved' }; },
      async quote(input) { quoteExecutions += 1; return { status: 'preview_ready', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1, pricing_rule_version: 'v1', recognition: input.recognition, reply_text: '实时单价50.00元/张，1张合计50.00元。' }; },
    },
    replyOutboxStore: { async enqueue() { return { created: true }; } },
  });
  await runtime.schedule(envelope, { mode: 'active' });
  await runtime.tick();
  const run = await store.get('active:tenant-1:event-1');
  assert.equal(quoteExecutions, 1);
  assert.equal(run.result.reason, 'authoritative_tool_response');
  assert.equal(run.tool_calls.filter((call) => call.tool === 'quote_realtime').length, 1);
  assert.deepEqual(run.result.trace.map((item) => item.action), ['recognize_image', 'resolve_showtime', 'quote_realtime']);
});

test('durable active read_linked_order returns a minimal authoritative order observation', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-order-')), 'runs.json'));
  await store.initialize();
  const activeEnvelope = { ...envelope, payload: { ...envelope.payload, content: '订单付款了吗', imageUrls: [] } };
  const plans = [
    { intent: '订单进度', confidence: 0.99, goal: '读取订单', action: 'read_linked_order', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '存在关联订单' },
    { intent: '订单进度', confidence: 0.99, goal: '回复状态', action: 'respond', arguments: {}, missing_fields: [], reply: '模型不得改写权威状态', needs_human: false, reason: '已读取' },
  ];
  const queued = [];
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: activeEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: { order_id: 'platform-order-secret', stage: 'waiting_payment' }, messages: [] }; } },
    planner: { async plan() { return plans.shift(); } }, getSettings: async () => ({}),
    coreFor() { return { orders: { async get(orderId) { assert.equal(orderId, 'platform-order-secret'); return { orderStatus: 2, orderStatusText: '<b>买家已付款</b>', buyerNick: 'sensitive-buyer', payment: 9999 }; } } }; },
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(activeEnvelope, { mode: 'active' });
  await runtime.tick();
  const run = await store.get('active:tenant-1:event-1');
  assert.equal(run.observations[0].tool, 'read_linked_order');
  assert.deepEqual(run.observations[0].facts, { has_linked_order: true, lifecycle: 'paid', paid: true, fulfilled: false });
  assert.doesNotMatch(JSON.stringify(run.observations[0]), /platform-order-secret|sensitive-buyer|9999/u);
  assert.equal(queued[0].text, '闲鱼订单显示已付款，正在等待人工出票处理，请勿重复付款。');
});

test('durable active order-read failure creates an idempotent manual task and queues a safe fallback', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-order-fail-')), 'runs.json'));
  await store.initialize();
  const activeEnvelope = { ...envelope, payload: { ...envelope.payload, content: '订单状态', imageUrls: [] } };
  const manual = []; const queued = [];
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: activeEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: { order_id: 'platform-order-secret', stage: 'waiting_payment' }, messages: [] }; } },
    planner: { async plan() { return { intent: '订单进度', confidence: 0.99, goal: '读取订单', action: 'read_linked_order', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '订单咨询' }; } },
    getSettings: async () => ({}), coreFor() { return { orders: { async get() { throw new Error('upstream secret failure'); } } }; },
    manualTaskStore: { async create(input) { manual.push(input); return { created: true, task: { status: 'open' } }; } },
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(activeEnvelope, { mode: 'active' });
  await runtime.tick();
  assert.equal(manual.length, 1);
  assert.equal(manual[0].taskId, 'agent:tenant-1:event-1:fallback');
  assert.equal(manual[0].orderId, 'platform-order-secret');
  assert.equal(queued[0].text, '这个问题需要人工进一步确认，已记录处理，请稍候。');
  assert.doesNotMatch(JSON.stringify(queued), /secret failure|platform-order-secret/u);
});

test('durable active agent refuses a source turn not assigned to the agent owner', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-owner-')), 'runs.json'));
  await store.initialize();
  let planned = 0; let queued = 0;
  const activeEnvelope = { ...envelope, payload: { ...envelope.payload, content: '你好', imageUrls: [] } };
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: activeEnvelope, result: { execution_owner: 'deterministic' } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() { planned += 1; throw new Error('must not plan'); } }, getSettings: async () => ({}),
    replyOutboxStore: { async enqueue() { queued += 1; } },
  });
  await runtime.schedule(activeEnvelope, { mode: 'active' });
  const result = await runtime.tick();
  assert.equal(result.result.reason, 'execution_owner_mismatch');
  assert.equal(planned, 0); assert.equal(queued, 0);
});

test('shadow agent renews its independent lease while a model call is slow', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-heartbeat-')), 'runs.json'));
  await store.initialize();
  let renewals = 0;
  const runStore = {
    enqueue: (...args) => store.enqueue(...args), claimDue: (...args) => store.claimDue(...args),
    beginTool: (...args) => store.beginTool(...args), completeTool: (...args) => store.completeTool(...args),
    checkpoint: (...args) => store.checkpoint(...args), complete: (...args) => store.complete(...args),
    timeout: (...args) => store.timeout(...args), defer: (...args) => store.defer(...args), retry: (...args) => store.retry(...args),
    async renewLease(...args) { renewals += 1; return store.renewLease(...args); },
  };
  const runtime = createShadowAgentRuntime({
    runStore,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope, result: {} }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() {
      await new Promise((resolve) => setTimeout(resolve, 35));
      return { intent: '其他', confidence: 0.98, goal: '结束', action: 'respond', arguments: {}, missing_fields: [], reply: '影子回复', needs_human: false, reason: '完成' };
    } },
    getSettings: async () => ({}),
    heartbeatMs: 10,
  });
  await runtime.schedule(envelope);
  assert.equal((await runtime.tick()).status, 'completed');
  assert.ok(renewals >= 1, `expected at least one renewal, got ${renewals}`);
});

test('shadow agent shutdown aborts an in-flight model request without waiting for its timeout', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-stop-')), 'runs.json'));
  await store.initialize();
  let planningStarted;
  const started = new Promise((resolve) => { planningStarted = resolve; });
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope, result: {} }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan(input) {
      planningStarted();
      await new Promise((resolve, reject) => {
        input.signal.addEventListener('abort', () => reject(input.signal.reason ?? new Error('aborted')), { once: true });
      });
    } },
    getSettings: async () => ({}),
  });
  await runtime.schedule(envelope);
  const tick = runtime.tick();
  await started;
  runtime.stop();
  assert.equal((await tick).status, 'deferred');
});

test('shadow agent recovery never repeats a tool whose prior result is unknown', async () => {
  let now = 1_000;
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-unknown-tool-')), 'runs.json'), { now: () => now });
  await store.initialize();
  await store.enqueue({ runId: 'shadow:tenant-1:event-1', eventKey: 'tenant-1:event-1', tenantId: 'tenant-1', mode: 'shadow', deadlineMs: 600_000 });
  const first = await store.claimDue({ leaseMs: 60_000 });
  await store.beginTool(first.run_id, first.lease_id, {
    callId: 'tool:1:quote_realtime', step: 1, tool: 'quote_realtime',
    trace: [{ step: 1, action: 'quote_realtime', intent: '选座核价', confidence: 0.98 }], observations: [],
  });
  now += 60_001;
  let planned = 0;
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope, result: {} }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() { planned += 1; throw new Error('must not replan unknown tool'); } },
    getSettings: async () => ({}), now: () => now,
  });
  const result = await runtime.tick();
  assert.equal(result.status, 'completed');
  assert.equal(result.result.reason, 'agent_tool_result_unknown');
  assert.equal(planned, 0);
});

test('shadow agent persists a timed-out result when the run deadline is crossed', async () => {
  let now = 1_000;
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-deadline-')), 'runs.json'), { now: () => now });
  await store.initialize();
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope, result: {} }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() {
      now = 31_001;
      return { intent: '选座核价', confidence: 0.98, goal: '识图', action: 'recognize_image', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '调用工具' };
    } },
    getSettings: async () => ({}),
    deadlineMs: 30_000,
    now: () => now,
  });
  await runtime.schedule(envelope);
  assert.equal((await runtime.tick()).status, 'timed_out');
  const run = await store.get('shadow:tenant-1:event-1');
  assert.equal(run.status, 'timed_out');
  assert.equal(run.result.reason, 'agent_deadline_exceeded');
});

test('shadow agent defers without model calls until the source business event is complete', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-runtime-')), 'runs.json'));
  await store.initialize();
  let planned = 0;
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'processing', envelope, result: null }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() { planned += 1; throw new Error('must wait'); } },
    getSettings: async () => ({ recognition_enabled: true, quote_enabled: true }),
  });
  await runtime.schedule(envelope);
  assert.equal((await runtime.tick()).status, 'deferred');
  assert.equal(planned, 0);
  assert.equal((await store.get('shadow:tenant-1:event-1')).status, 'queued');
});
