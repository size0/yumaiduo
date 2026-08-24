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

test('scheduling persists a workflow-projected source-time buyer and seller history', async () => {
  const enqueued = [];
  const sourceEnvelope = { ...envelope, ts: Date.parse('2026-08-23T04:00:02Z'), payload: { ...envelope.payload, imageUrls: [], remoteMessageId: 'buyer-current' } };
  const runtime = createShadowAgentRuntime({
    runStore: { async enqueue(input) { enqueued.push(input); return { created: true, run: { run_id: input.runId } }; } },
    eventStore: {
      async wasSentMessage(_tenantId, _chatId, messageId) { return messageId === 'seller-plugin'; },
    },
    conversationContextStore: { async get() { return {
      facts: { city: '泉州', stage: 'collecting_information', order_id: 'must-not-reach-planner' },
      messages: [{ at: sourceEnvelope.ts, role: 'buyer', text: '第二个' }, { at: sourceEnvelope.ts + 10_000, role: 'buyer', text: '未来消息' }],
    }; } },
    coreFor() { return { im: { async listMessages() { return { items: [
      { direction: 'outbound', messageId: 'seller-human', content: '哪个店？', sentAt: '2026-08-23T04:00:00Z' },
      { direction: 'inbound', messageId: 'buyer-current', content: '第二个', sentAt: '2026-08-23T04:00:02Z' },
      { direction: 'outbound', messageId: 'seller-plugin', content: '未来插件回复', sentAt: '2026-08-23T04:00:03Z' },
    ] }; } } }; },
    planner: { async plan() { throw new Error('not used'); } },
    getSettings: async () => ({}),
  });

  await runtime.schedule(sourceEnvelope, { contextSnapshot: {
    facts: { city: '泉州', stage: 'collecting_information', order_id: 'must-not-reach-planner' },
    messages: [
      { at: Date.parse('2026-08-23T04:00:00Z'), role: 'seller', source: 'external_seller', content: '哪个店？' },
      { at: sourceEnvelope.ts, role: 'buyer', source: 'buyer', content: '第二个' },
    ],
  } });
  assert.equal(enqueued.length, 1);
  assert.equal(enqueued[0].contextSnapshot.facts.city, '泉州');
  assert.deepEqual(enqueued[0].contextSnapshot.messages.map((item) => [item.role, item.content, item.source]), [
    ['seller', '哪个店？', 'external_seller'],
    ['buyer', '第二个', 'buyer'],
  ]);
});

test('live shadow planning uses the durable source-time snapshot instead of mutable future state', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-snapshot-')), 'runs.json'));
  await store.initialize();
  let state = { facts: { stage: 'collecting_information', city: '泉州' }, messages: [{ at: 1, role: 'buyer', text: '还有W座位吗' }] };
  const planned = [];
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: { ...envelope, payload: { ...envelope.payload, imageUrls: [], content: '还有W座位吗' } }, result: {} }; } },
    conversationContextStore: { async get() { return structuredClone(state); } },
    planner: { async plan(input) {
      planned.push(input);
      return { intent: '其他', confidence: 0.99, goal: '安全结束', action: 'wait', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '测试快照' };
    } },
    getSettings: async () => ({}),
  });
  await runtime.schedule({ ...envelope, payload: { ...envelope.payload, imageUrls: [], content: '还有W座位吗' } });
  state = { facts: { stage: 'paid_manual_delivery', paid: true }, messages: [{ at: 2, role: 'buyer', text: '未来已付款' }] };
  await runtime.tick();

  assert.equal(planned[0].state.facts.stage, 'collecting_information');
  assert.equal(planned[0].state.facts.paid, undefined);
  assert.equal(planned[0].state.messages.some((item) => item.content === '未来已付款' || item.text === '未来已付款'), false);
});

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

test('live Shadow runs ignore retired secondary advisory hooks', async () => {
  const advisoryEnvelope = { ...envelope, payload: { ...envelope.payload, content: '你好', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-no-secondary-provider-')), 'runs.json'));
  await store.initialize();
  let retiredProviderCalls = 0;
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: advisoryEnvelope, result: {} }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: {
      async plan() { return { intent: '其他', confidence: 0.99, goal: '问候', action: 'respond', arguments: {}, missing_fields: [], reply: '您好，请问需要查询什么？', needs_human: false, reason: '普通问候' }; },
      evaluateShadow() { retiredProviderCalls += 1; throw new Error('retired provider must not run'); },
    },
    getSettings: async () => ({}),
  });
  await runtime.schedule(advisoryEnvelope);
  await runtime.tick();
  const run = await store.get('shadow:tenant-1:event-1');
  assert.equal(retiredProviderCalls, 0);
  assert.equal(run.result.status, 'reply');
  assert.equal(run.result.proposed_reply, '您好，请问需要查询什么？');
  assert.equal(run.result.shadow_provider_evaluation, undefined);
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

test('Active semantic seat preference is persisted without claiming an official seat selection', async () => {
  const preferenceEnvelope = { ...envelope, payload: { ...envelope.payload, content: '后面一点的位置', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-semantic-preference-')), 'runs.json'));
  await store.initialize();
  const recorded = []; const queued = [];
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: preferenceEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: {
      async get() { return { facts: {}, messages: [] }; },
      async recordSeatPreference(tenantId, payload, value) { recorded.push([tenantId, payload.peerUnb, value]); return true; },
    },
    planner: { async plan() { return { intent: '补充信息', confidence: 0.99, goal: '记录位置偏好', action: 'record_seat_preference', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '买家描述靠后偏好' }; } },
    getSettings: async () => ({}),
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(preferenceEnvelope, { mode: 'active' });
  await runtime.tick();

  assert.deepEqual(recorded, [['tenant-1', 'buyer-1', '后面一点的位置']]);
  assert.match(queued[0].text, /位置偏好/u);
  assert.doesNotMatch(queued[0].text, /已选座|已锁座/u);
});

test('shadow and evaluation write-shaped plans remain externally side-effect-free', async (t) => {
  for (const mode of ['shadow', 'evaluation']) {
    await t.test(mode, async () => {
      const preferenceEnvelope = { ...envelope, payload: { ...envelope.payload, content: '7排5座', imageUrls: [] } };
      const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), `wanda-${mode}-no-effects-`)), 'runs.json'));
      await store.initialize();
      let contextReads = 0;
      const runtime = createShadowAgentRuntime({
        runStore: store,
        eventStore: { async get() { return {
          key: 'tenant-1:event-1', status: 'completed', envelope: preferenceEnvelope,
          result: { agent_reply_snapshot: { kind: 'conversation_follow_up', text: '已记录文字座位偏好，具体以出票时实时可选为准。' } },
        }; } },
        conversationContextStore: {
          async get() { contextReads += 1; return { facts: {}, messages: [] }; },
          async recordCircledDeliveryInstruction() { throw new Error(`${mode} must not write conversation state`); },
        },
        planner: { async plan() { return { intent: '选座核价', confidence: 0.99, goal: '记录偏好', action: 'record_seat_preference', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '文字座位' }; } },
        getSettings: async () => ({}),
        manualTaskStore: { async create() { throw new Error(`${mode} must not create manual tasks`); } },
        replyOutboxStore: { async enqueue() { throw new Error(`${mode} must not enqueue replies`); } },
        coreFor() { throw new Error(`${mode} must not call platform APIs`); },
        quotePreviewClient: {
          async recognize() { throw new Error(`${mode} must not recognize again`); },
          async resolveShowtime() { throw new Error(`${mode} must not resolve showtimes`); },
          async quote() { throw new Error(`${mode} must not quote again`); },
        },
      });
      const scheduled = await runtime.schedule(preferenceEnvelope, { mode });
      assert.equal((await runtime.tick()).status, 'completed');
      const run = await store.get(scheduled.run.run_id);
      assert.deepEqual(run.tool_calls.map((call) => call.tool), ['record_seat_preference']);
      assert.equal(run.result.status, 'reply');
      assert.equal(run.result.authoritative_reply_used, true);
      assert.equal(run.result.reply_queued, undefined);
      assert.equal(contextReads, mode === 'shadow' ? 1 : 0);
    });
  }
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
    planner: {
      async plan() { return { intent: '其他', confidence: 0.98, goal: '说明流程', action: 'respond', arguments: {}, missing_fields: [], reply: '请发送完整选座页截图并说明张数。', needs_human: false, reason: '流程咨询' }; },
    },
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

test('durable active confirmation delegates to the deterministic quote confirmation gate', async () => {
  const activeEnvelope = { ...envelope, payload: { ...envelope.payload, content: '确认', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-confirm-')), 'runs.json'));
  await store.initialize();
  const confirmations = []; const queued = [];
  const contextStore = {
    async get() { return { facts: { stage: 'quoted', quote_total_cents: 10_000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, pricing_rule_version: 'v1', quote_reply_delivered: true }, messages: [] }; },
    async markQuoteConfirmed(tenantId, payload) { confirmations.push([tenantId, payload]); return true; },
  };
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: activeEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: contextStore,
    planner: { async plan() { return { intent: '补充信息', confidence: 0.99, goal: '确认报价', action: 'confirm_quote', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '买家确认' }; } },
    getSettings: async () => ({}),
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(activeEnvelope, { mode: 'active' });
  await runtime.tick();

  assert.deepEqual(confirmations, [['tenant-1', activeEnvelope.payload]]);
  assert.equal(queued.length, 1);
  assert.match(queued[0].text, /先不要付款/u);
  const run = await store.get('active:tenant-1:event-1');
  assert.deepEqual(run.tool_calls.map((call) => call.tool), ['confirm_active_quote']);
  assert.equal(run.observations[0].facts.quote_confirmed, true);
});

test('active confirmation refuses an expired or undelivered quote without claiming success', async () => {
  const activeEnvelope = { ...envelope, payload: { ...envelope.payload, content: '确认', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-confirm-rejected-')), 'runs.json'));
  await store.initialize();
  const queued = [];
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: activeEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: {
      async get() { return { facts: { stage: 'quoted', quote_total_cents: 10_000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, pricing_rule_version: 'v1', quote_reply_delivered: true }, messages: [] }; },
      async markQuoteConfirmed() { return false; },
    },
    planner: { async plan() { return { intent: '补充信息', confidence: 0.99, goal: '确认报价', action: 'confirm_quote', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '买家确认' }; } },
    getSettings: async () => ({}),
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(activeEnvelope, { mode: 'active' });
  await runtime.tick();

  assert.equal(queued.length, 1);
  assert.match(queued[0].text, /不能进入下单流程/u);
  assert.doesNotMatch(queued[0].text, /已确认/u);
  const run = await store.get('active:tenant-1:event-1');
  assert.equal(run.observations[0].facts.quote_confirmed, false);
});

test('manual task status is read by exact conversation without exposing task or order identifiers', async () => {
  const activeEnvelope = { ...envelope, payload: { ...envelope.payload, content: '人工处理进度怎么样了', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-manual-status-')), 'runs.json'));
  await store.initialize();
  const lookups = []; const queued = [];
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: activeEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() { return { intent: '订单进度', confidence: 0.99, goal: '查询人工进度', action: 'respond', arguments: {}, missing_fields: [], reply: '模型不得猜测进度', needs_human: false, reason: '进度咨询' }; } },
    getSettings: async () => ({}),
    manualTaskStore: { async findLatestForConversation(tenantId, address) { lookups.push([tenantId, address]); return { task_id: 'secret-task', order_id: 'secret-order', assignee: 'secret-operator', status: 'resolved' }; } },
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(activeEnvelope, { mode: 'active' });
  await runtime.tick();

  assert.deepEqual(lookups, [['tenant-1', { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }]]);
  assert.equal(queued.length, 1);
  assert.match(queued[0].text, /是否已经出票仍以闲鱼订单状态/u);
  assert.doesNotMatch(JSON.stringify(queued), /secret-task|secret-order|secret-operator/u);
  const run = await store.get('active:tenant-1:event-1');
  assert.deepEqual(run.observations[0].facts, {
    manual_task_found: true, manual_task_status: 'resolved', manual_task_resolved: true,
  });
});

test('historical manual task evaluation never reads mutable current task state', async () => {
  const statusEnvelope = { ...envelope, payload: { ...envelope.payload, content: '人工处理进度怎么样了', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-evaluation-manual-status-')), 'runs.json'));
  await store.initialize();
  let reads = 0;
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: statusEnvelope, result: {} }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan() { return { intent: '订单进度', confidence: 0.99, goal: '查询人工进度', action: 'get_manual_task_status', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '进度咨询' }; } },
    getSettings: async () => ({}),
    manualTaskStore: { async findLatestForConversation() { reads += 1; return null; } },
  });
  const scheduled = await runtime.schedule(statusEnvelope, { mode: 'evaluation' });
  await runtime.tick();
  assert.equal(reads, 0);
  const run = await store.get(scheduled.run.run_id);
  assert.equal(run.result.reason, 'historical_manual_task_snapshot_unavailable');
});

test('Shadow can evaluate a parameterless price-change request without executing platform writes', async () => {
  const requestEnvelope = { ...envelope, payload: { ...envelope.payload, content: '请处理一下', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-shadow-price-request-')), 'runs.json'));
  await store.initialize();
  let platformCalls = 0;
  const plans = [
    { intent: '订单进度', confidence: 0.99, goal: '申请改价', action: 'request_price_change', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '订单待付款' },
    { intent: '订单进度', confidence: 0.99, goal: '说明状态', action: 'respond', arguments: {}, missing_fields: [], reply: '已记录您的处理请求。', needs_human: false, reason: '仅评估' },
  ];
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: requestEnvelope, result: {} }; } },
    conversationContextStore: { async get() { return { facts: { order_id: 'system-only', quote_confirmed: true, quote_total_cents: 10_000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 }, messages: [] }; } },
    planner: { async plan() { return plans.shift(); } }, getSettings: async () => ({}),
    coreFor() { platformCalls += 1; throw new Error('Shadow must not access platform writes'); },
  });
  await runtime.schedule(requestEnvelope, { mode: 'shadow' });
  await runtime.tick();
  const run = await store.get('shadow:tenant-1:event-1');
  assert.deepEqual(run.tool_calls.map((call) => call.tool), ['request_price_change']);
  assert.deepEqual(run.observations[0].facts, { price_change_requested: false });
  assert.equal(platformCalls, 0);
});

test('Active price-change requests remain hard-disabled before any tool execution', async () => {
  const requestEnvelope = { ...envelope, payload: { ...envelope.payload, content: '请处理一下', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-price-request-disabled-')), 'runs.json'));
  await store.initialize();
  let writes = 0;
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: requestEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: { order_id: 'system-only', quote_confirmed: true, quote_total_cents: 10_000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 }, messages: [] }; } },
    planner: { async plan() { return { intent: '订单进度', confidence: 0.99, goal: '申请改价', action: 'request_price_change', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '订单待付款' }; } },
    getSettings: async () => ({}),
    replyOutboxStore: { async enqueue() { writes += 1; return { created: true }; } },
    coreFor() { writes += 1; throw new Error('disabled request must not reach platform'); },
  });
  await runtime.schedule(requestEnvelope, { mode: 'active' });
  await runtime.tick();
  const run = await store.get('active:tenant-1:event-1');
  assert.equal(run.result.reason, 'agent_price_change_not_enabled');
  assert.equal(run.tool_calls.length, 0);
  assert.equal(writes, 0);
});

test('Active W+ seat lookup uses the read-only realtime endpoint with a system-parsed row', async () => {
  const seatEnvelope = { ...envelope, payload: { ...envelope.payload, content: '8排还有W+位置吗', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-seat-lookup-')), 'runs.json'));
  await store.initialize();
  const calls = []; const queued = [];
  const recognition = { cinema: '测试万达影城', movie: '测试电影', date: '2026-08-22', showtime: '19:30' };
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: seatEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: { quote_draft: { recognition_artifact: { status: 'recognized', tenant_id: 'tenant-1', recognition } } }, messages: [] }; } },
    planner: { async plan() { return { intent: '选座核价', confidence: 0.99, goal: '查询W+座位', action: 'show_available_wplus_seats', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '买家询问8排' }; } },
    getSettings: async () => ({ quote_enabled: true }),
    quotePreviewClient: {
      async availableSeats(input) { calls.push(input); return { row: 8, seats: ['8排10座', '8排11座'], available_count: 2, wplus_offer_available: true, matched_cinema_name: '测试万达影城' }; },
      async quote() { throw new Error('seat lookup must not start a temporary price probe'); },
    },
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(seatEnvelope, { mode: 'active' });
  await runtime.tick();

  assert.deepEqual(calls, [{ recognition, row: 8 }]);
  assert.equal(queued.length, 1);
  assert.match(queued[0].text, /8排10座、8排11座/u);
  const run = await store.get('active:tenant-1:event-1');
  assert.deepEqual(run.observations[0].facts, {
    requested_row: 8, available_count: 2, seat_numbers: ['8排10座', '8排11座'],
    wplus_offer_available: true, cinema: '测试万达影城',
  });
});

test('Active ordinal follow-up resolves the persisted cinema candidate before reading W+ seats', async () => {
  const ordinalEnvelope = { ...envelope, payload: { ...envelope.payload, content: '第二个', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-candidate-reference-')), 'runs.json'));
  await store.initialize();
  const resolvedCinemas = []; const queued = [];
  const baseRecognition = { city: '泉州', movie: '奥德赛', date: '2026-08-23', showtime: '15:50' };
  const contextStore = {
    async get() { return { facts: { candidate_set: { base_recognition: baseRecognition, candidates: [{ index: 1, cinema: '晋江万达广场店' }, { index: 2, cinema: '晋江万达影城SM广场店' }] } }, messages: [] }; },
    async clearCandidateSet() { return true; },
  };
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: ordinalEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: contextStore,
    planner: { async plan(input) {
      const action = input.observations.length === 0 ? 'resolve_ticket_identity' : 'show_available_wplus_seats';
      return { intent: '补充信息', confidence: 0.99, goal: '解析上一轮影院候选', action, arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '序号指代候选' };
    } },
    getSettings: async () => ({ recognition_enabled: true }),
    quotePreviewClient: {
      async recognize() { throw new Error('ordinal reference must reuse the candidate set'); },
      async resolveShowtime(input) { resolvedCinemas.push(input.recognition.cinema); return { ...input, status: 'resolved' }; },
      async availableSeats() { return { row: null, seats: ['9排9座'], available_count: 1, wplus_offer_available: true, matched_cinema_name: '晋江万达影城SM广场店' }; },
    },
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(ordinalEnvelope, { mode: 'active' });
  await runtime.tick();

  assert.deepEqual(resolvedCinemas, ['晋江万达影城SM广场店']);
  assert.match(queued[0].text, /9排9座/u);
});

test('Active text-only W+ question resolves identity then reads all current W+ seats without price probes', async () => {
  const textEnvelope = { ...envelope, payload: { ...envelope.payload, content: '泉州晋江万达今天15:50奥德赛还有W座位吗', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-text-wplus-')), 'runs.json'));
  await store.initialize();
  const calls = []; const queued = [];
  const recognition = { cinema: '晋江万达广场店', city: '泉州', movie: '奥德赛', date: '2026-08-23', showtime: '15:50' };
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: textEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    planner: { async plan(input) {
      const action = input.observations.length === 0 ? 'resolve_ticket_identity' : 'show_available_wplus_seats';
      return { intent: '选座核价', confidence: 0.99, goal: '查询实时W+库存', action, arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '纯文字场次信息完整' };
    } },
    getSettings: async () => ({ recognition_enabled: true }),
    quotePreviewClient: {
      async recognize(input) { calls.push(['recognize', input.payload.content]); return { status: 'recognized', tenant_id: 'tenant-1', recognition, text_quote: true }; },
      async resolveShowtime(input) { calls.push(['resolve', input.recognition]); return { ...input, status: 'resolved', recognition }; },
      async availableSeats(input) { calls.push(['seats', input]); return { row: null, seats: ['8排10座', '8排11座'], available_count: 2, wplus_offer_available: true, matched_cinema_name: '晋江万达广场店' }; },
      async quote() { throw new Error('availability lookup must not create a temporary price probe'); },
    },
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(textEnvelope, { mode: 'active' });
  await runtime.tick();

  assert.deepEqual(calls.map(([name]) => name), ['recognize', 'resolve', 'seats']);
  assert.equal(calls[2][1].row, null);
  assert.match(queued[0].text, /8排10座、8排11座/u);
  const run = await store.get('active:tenant-1:event-1');
  assert.deepEqual(run.tool_calls.map((call) => call.tool), ['resolve_ticket_identity', 'list_available_wplus_seats']);
});

test('Active W+ seat lookup with reusable identity may list all rows when no row is requested', async () => {
  const seatEnvelope = { ...envelope, payload: { ...envelope.payload, content: '还有W+位置吗', imageUrls: [] } };
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-seat-row-missing-')), 'runs.json'));
  await store.initialize();
  let calls = 0;
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: seatEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: { quote_draft: { recognition_artifact: { status: 'recognized', tenant_id: 'tenant-1', recognition: { cinema: '测试万达' } } } }, messages: [] }; } },
    planner: { async plan() { return { intent: '选座核价', confidence: 0.99, goal: '查询W+座位', action: 'show_available_wplus_seats', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '座位咨询' }; } },
    getSettings: async () => ({ quote_enabled: true }),
    quotePreviewClient: { async availableSeats(input) { calls += 1; assert.equal(input.row, null); return { row: null, seats: ['8排10座'], available_count: 1, wplus_offer_available: true, matched_cinema_name: '测试万达' }; } },
    replyOutboxStore: { async enqueue() { return { created: true }; } },
  });
  await runtime.schedule(seatEnvelope, { mode: 'active' });
  await runtime.tick();
  const run = await store.get('active:tenant-1:event-1');
  assert.equal(calls, 1);
  assert.equal(run.result.reason, 'authoritative_tool_response');
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
      async quote(input) { calls.push(['quote', input.status]); return { status: 'preview_ready', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1, pricing_rule_version: 'quote-policy-test', pricing_account_ref: 'a'.repeat(32), recognition: input.recognition, reply_text: '实时单价50.00元/张，1张合计50.00元。' }; },
    },
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(envelope, { mode: 'active' });
  await runtime.tick();
  assert.deepEqual(calls, [['recognize', 'event-1'], ['resolve', '测试万达'], ['quote', 'resolved']]);
  assert.equal(queued.length, 1);
  assert.equal(queued[0].text, '实时单价50.00元/张，1张合计50.00元。\n接受本次报价请回复“确认”。');
  assert.doesNotMatch(queued[0].text, /模型报价不得采用/u);
  assert.deepEqual(queued[0].delivery, {
    type: 'quote', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1,
    pricing_rule_version: 'quote-policy-test', cinema: '测试万达', movie: '测试电影', date: '2026-08-22', showtime: '19:30', hall: '', quote_scope: '',
    member_cost_total_cents: null, original_price_total_cents: null, channel_fee_total_cents: null, pricing_source: '',
    circled_delivery_image_url: 'https://img.alicdn.com/a.png', pricing_account_ref: 'a'.repeat(32),
  });
  const run = await store.get('active:tenant-1:event-1');
  assert.deepEqual(run.observations.map((item) => item.tool), ['recognize_image', 'resolve_showtime', 'quote_realtime']);
  assert.equal(run.result.reason, 'authoritative_tool_response');
  assert.equal(run.result.authoritative_reply_used, true);
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

test('active quote clarification uses the persisted quote instead of a vague manual fallback when planning fails', async () => {
  const store = new AgentRunStore(join(await mkdtemp(join(tmpdir(), 'wanda-active-quote-fallback-')), 'runs.json'));
  await store.initialize();
  const activeEnvelope = { ...envelope, payload: { ...envelope.payload, content: '是灰色的诶，W+才能买到', imageUrls: [], remoteMessageId: 'buyer-source-1' } };
  const queued = [];
  const runtime = createShadowAgentRuntime({
    runStore: store,
    eventStore: { async get() { return { key: 'tenant-1:event-1', status: 'completed', envelope: activeEnvelope, result: { execution_owner: 'agent' } }; } },
    conversationContextStore: { async get() { return { facts: { stage: 'quoted', quote_unit_cents: 5950, quote_total_cents: 5950, quote_ticket_count: 1, quote_expires_at: Date.now() + 60_000 }, messages: [] }; } },
    planner: { async plan() { return {}; } }, getSettings: async () => ({}),
    manualTaskStore: { async create() { return { created: true, task: { status: 'open' } }; } },
    replyOutboxStore: { async enqueue(input) { queued.push(input); return { created: true }; } },
  });
  await runtime.schedule(activeEnvelope, { mode: 'active' });
  await runtime.tick();
  assert.equal(queued.length, 1);
  assert.equal(queued[0].sourceMessageId, 'buyer-source-1');
  assert.equal(queued[0].text, '系统刚才已通过万达实时核验，当前有效报价是59.50元/张，1张合计59.50元。座位状态可能变化；需要按这份报价购买请回复“确认”。');
  assert.doesNotMatch(queued[0].text, /人工进一步确认/u);
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
