import assert from 'node:assert/strict';
import test from 'node:test';
import { createWorkflow, paymentSafeOrderInstruction } from '../src/workflow.mjs';
import { agentCanaryBucket } from '../src/agent/agent-canary-router.mjs';
import { AGENT_RUNTIME_VERSION } from '../src/agent/shadow-agent-runtime.mjs';

const quotePolicySnapshot = Object.freeze({
  wplus_adjustment_cents: -290,
  regular_adjustment_cents: 100,
  max_auto_order_amount_cents: 200_000,
});

function selectedCanaryEventId(prefix = 'evt-canary') {
  for (let index = 0; index < 10_000; index += 1) {
    const eventId = `${prefix}-${index}`;
    if (agentCanaryBucket('tenant-1', eventId) < 500) return eventId;
  }
  throw new Error('no selected canary event id');
}

const approvedCanarySettings = Object.freeze({
  agent_canary_enabled: true, agent_canary_kill_switch: false, agent_canary_percentage: 5,
  agent_canary_approved: true, agent_canary_runtime_version: AGENT_RUNTIME_VERSION,
});

function record(envelope) {
  return { key: `tenant:${envelope.id}`, leaseId: 'lease-1', attempts: 1, envelope };
}

function harness(overrides = {}) {
  const calls = [];
  const runtimeSettings = overrides.runtimeSettings ?? {
    automation_enabled: true,
    auto_price_change: true,
    ai_reply_enabled: true,
    ai_only_mode_enabled: false,
  };
  const eventStore = {
    async enqueue() {},
    async claimDue() { return null; },
    async complete(key, leaseId, result) { calls.push(['complete', key, leaseId, result]); },
    async retry() { throw new Error('unexpected retry'); },
    async fail() { throw new Error('unexpected failure'); },
    async markUnknown() { throw new Error('unexpected unknown'); },
    async wasSentMessage() { return false; },
    async recordSentMessage(...args) { calls.push(['record-message', ...args]); },
    ...overrides.eventStore,
  };
  const core = {
    shops: {
      async list() { return overrides.shops ?? []; },
    },
    im: {
      async listSessions() { return { items: [] }; },
      async getSessionByOrder() { return null; },
      async listMessages() { return { items: [] }; },
      async sendMessage(input) { calls.push(['send', input]); return { messageId: 'sent-1' }; },
    },
    orders: {
      async get() { return { orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '9900', postFee: '0' }; },
      async changePrice(orderId, input) { calls.push(['change-price', orderId, input]); },
    },
    ...overrides.core,
  };
  const backend = {
    async getRuntimeSettings(accountUnb) {
      calls.push(['runtime-settings', accountUnb]);
      return { settings: runtimeSettings };
    },
    ...overrides.backend,
  };
  const workflow = createWorkflow({
    backend,
    coreFor: () => core,
    eventStore,
    conversationContextStore: overrides.conversationContextStore ?? null,
    imageLoader: overrides.imageLoader ?? { async load() { return { bytes: new Uint8Array([1]), contentType: 'image/png' }; } },
    quotePreviewClient: overrides.quotePreviewClient,
    replyPreviewClient: overrides.replyPreviewClient,
    conversationAgentPlanner: overrides.conversationAgentPlanner,
    shadowAgentScheduler: overrides.shadowAgentScheduler,
    manualTaskStore: overrides.manualTaskStore,
    autoReplyEnabled: overrides.autoReplyEnabled,
    sleep: overrides.sleep ?? (async () => {}),
    quoteProgressNoticeDelayMs: overrides.quoteProgressNoticeDelayMs,
    logger: { error() {} },
  });
  return { workflow, calls };
}

test('casual buyer text does not inherit an image before it is classified as a quote supplement', async () => {
  const enqueued = [];
  const { workflow } = harness({
    conversationContextStore: {
      async add() {},
      async get() { return { messages: [{ at: Date.now(), image_urls: ['https://img.alicdn.com/seat.png'] }] }; },
    },
    eventStore: { async enqueue(envelope) { enqueued.push(envelope); }, async cancelPendingChatMessages() {} },
  });
  await workflow.enqueueEvent({ id: 'evt-text', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '可以吗' } });
  assert.equal(enqueued[0].payload.imageUrls, undefined);
});

test('platform transaction notices are ignored before conversation history or AI planning', async (t) => {
  const notices = [
    '买家已确认收货，交易成功',
    '快给ta一个评价吧～',
    '我完成了评价',
    '你关闭了订单，钱款已原路退返',
    '你已发货',
    '你人真不错，送你闲鱼小红花',
    '不想宝贝被砍价? <a size=13 href="fleamarket://message_no_bargain?flutter=true&bizId=bargain">去设置</a>',
  ];
  for (const [index, content] of notices.entries()) {
    await t.test(content, async () => {
      let plannerCalls = 0;
      const { workflow, calls } = harness({
        runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'shadow' },
        core: { im: { async listMessages() { throw new Error('history must not be requested'); } } },
        conversationAgentPlanner: { async plan() { plannerCalls += 1; } },
      });
      const result = await workflow.processClaimed(record({ id: `evt-platform-notice-${index}`, tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'invalid-system-chat', peerUnb: 'buyer-1', content } }));
      assert.equal(result.status, 'completed');
      assert.equal(plannerCalls, 0);
      assert.equal(calls.some(([name]) => name === 'send'), false);
      assert.equal(calls.find(([name]) => name === 'complete')[3].skipped, 'platform_system_message');
    });
  }
});

test('buyer messages use the two-second minimum merge window but casual text cannot supersede a pending image', async () => {
  const enqueued = [];
  const cancelled = [];
  const { workflow } = harness({
    eventStore: {
      async enqueue(envelope, options) { enqueued.push({ envelope, options }); },
      async cancelPendingChatMessages(...args) { cancelled.push(args); },
    },
  });
  const now = Date.now();
  await workflow.enqueueEvent({
    id: 'evt-casual', tenantId: 'tenant-1', event: 'im.message.received', ts: now,
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '好的' },
  });
  assert.ok(enqueued[0].options.availableAt >= now + 1_900);
  assert.ok(enqueued[0].options.availableAt <= Date.now() + 2_100);
  assert.deepEqual(cancelled, []);

  await workflow.enqueueEvent({
    id: 'evt-image', tenantId: 'tenant-1', event: 'im.message.received', ts: now,
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] },
  });
  assert.ok(enqueued[1].options.availableAt <= Date.now() + 100);
  assert.deepEqual(cancelled[0], ['tenant-1', 'shop-1:chat-1:buyer-1', 'evt-image']);

  await workflow.enqueueEvent({
    id: 'evt-supplement', tenantId: 'tenant-1', event: 'im.message.received', ts: now,
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '两张多少钱' },
  });
  assert.deepEqual(cancelled[1], ['tenant-1', 'shop-1:chat-1:buyer-1', 'evt-supplement']);

  await workflow.enqueueEvent({
    id: 'evt-city', tenantId: 'tenant-1', event: 'im.message.received', ts: now,
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '广州' },
  });
  assert.deepEqual(cancelled[2], ['tenant-1', 'shop-1:chat-1:buyer-1', 'evt-city']);

  await workflow.enqueueEvent({
    id: 'evt-wplus', tenantId: 'tenant-1', event: 'im.message.received', ts: now,
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '是W+' },
  });
  assert.deepEqual(cancelled[3], ['tenant-1', 'shop-1:chat-1:buyer-1', 'evt-wplus']);
});

test('a first quote image sends one receipt before deferring transaction fact merging', async () => {
  const sequence = [];
  let released = 0;
  const { workflow, calls } = harness({
    autoReplyEnabled: true,
    runtimeSettings: {
      automation_enabled: true, recognition_enabled: true, quote_enabled: true,
      ai_reply_enabled: true, conversation_agent_mode: 'shadow', ai_reply_delay_seconds: 2,
      reply_templates: { quote_processing_notice: '收到，正在核对。' },
    },
    eventStore: {
      async defer(_key, _lease, options) { sequence.push(['defer', options]); return { status: 'queued' }; },
    },
    conversationContextStore: {
      async get() { return { facts: {}, messages: [] }; },
      async claimQuoteProcessingReceipt() { return true; },
      async releaseQuoteProcessingReceipt() { released += 1; },
    },
    core: { im: {
      async listSessions() { return { items: [] }; },
      async listMessages() { return { items: [] }; },
      async sendMessage(input) { sequence.push(['send', input.text]); return { messageId: 'receipt-1' }; },
    } },
    quotePreviewClient: {
      async recognize() { throw new Error('recognition must wait for the merge window'); },
      async quote() { throw new Error('quote must wait for the merge window'); },
    },
  });
  const result = await workflow.processClaimed(record({
    id: 'evt-immediate-receipt', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] },
  }));
  assert.equal(result.status, 'queued');
  assert.deepEqual(sequence.map(([name]) => name), ['send', 'defer']);
  assert.equal(released, 0);
  assert.equal(sequence[1][1].metadata.actions[0].action_id, 'evt-immediate-receipt:quote-processing-notice');
  assert.equal(sequence[1][1].metadata.actions[0].status, 'succeeded');
  assert.equal(calls.some(([name]) => name === 'complete'), false);
});

test('a failed immediate quote receipt releases its independent claim for retry', async () => {
  let released = 0;
  const { workflow } = harness({
    autoReplyEnabled: true,
    runtimeSettings: { automation_enabled: true, recognition_enabled: true, quote_enabled: true, ai_reply_enabled: true, ai_reply_delay_seconds: 2 },
    eventStore: {
      async defer() { throw new Error('failed receipt must not defer'); },
      async retry() { return { status: 'retry' }; },
    },
    conversationContextStore: {
      async claimQuoteProcessingReceipt() { return true; },
      async releaseQuoteProcessingReceipt() { released += 1; return true; },
    },
    core: { im: {
      async listSessions() { return { items: [] }; },
      async listMessages() { return { items: [] }; },
      async sendMessage() { throw Object.assign(new Error('WS unavailable'), { code: 'E_IM_WS_UNAVAILABLE' }); },
    } },
    quotePreviewClient: { async recognize() { throw new Error('must not recognize'); } },
  });
  const result = await workflow.processClaimed(record({
    id: 'evt-immediate-receipt-failed', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] },
  }));
  assert.equal(result.status, 'retry');
  assert.equal(released, 1);
});

test('system mode sends image to OCR pipeline, then replies through Yumaiduo', async () => {
  const backendCalls = [];
  const backend = {
    async upsertOrder() { return { task: { id: 'task_1' } }; },
    async submitOcrRecognition(taskId, input) {
      backendCalls.push(['ocr', taskId, input]);
      return { processing_mode: 'ocr', reply_origin: 'ocr', task: { id: taskId, status: 'quoted', quantity: 2 }, action: { reply_message: '报价80元', modify_order_amount: false } };
    },
  };
  const { workflow, calls } = harness({ backend });
  const envelope = {
    id: 'evt-1', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '要两张', imageUrls: ['https://img.alicdn.com/a.png'] },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.status, 'completed');
  assert.equal(backendCalls[0][0], 'ocr');
  assert.equal(backendCalls[0][2].event_id, 'evt-1:ocr-recognition');
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '报价80元');
});

test('a message from another owned shop is ignored rather than treated as a buyer message', async () => {
  const previews = [];
  const { workflow, calls } = harness({
    shops: [{ unb: 'shop-1' }, { unb: 'shop-2' }],
    quotePreviewClient: { async capture(envelope) { previews.push(envelope); return { status: 'preview_ready', reply_text: 'must not send' }; } },
    autoReplyEnabled: true,
  });
  const result = await workflow.processClaimed(record({
    id: 'evt-owned-peer', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'shop-2', content: 'test', imageUrls: ['https://img.alicdn.com/a.png'] },
  }));
  assert.equal(result.status, 'completed');
  assert.deepEqual(previews, []);
  assert.equal(calls.some(([name]) => name === 'send'), false);
  assert.equal(calls.find(([name]) => name === 'complete').at(-1).skipped, 'owned_shop_peer');
});

test('a disabled shop still schedules shadow learning without sending or running transaction work', async () => {
  const scheduled = [];
  const { workflow, calls } = harness({
    runtimeSettings: {
      automation_enabled: false, ai_reply_enabled: true,
      conversation_agent_mode: 'shadow', execution_owner: 'deterministic',
    },
    quotePreviewClient: {},
    shadowAgentScheduler: { async schedule(envelope) { scheduled.push(envelope.id); return { created: true }; } },
    autoReplyEnabled: true,
  });
  const result = await workflow.processClaimed(record({
    id: 'evt-disabled-shadow', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '你好' },
  }));

  assert.equal(result.status, 'completed');
  assert.deepEqual(scheduled, ['evt-disabled-shadow']);
  assert.equal(calls.some(([name]) => name === 'send'), false);
  assert.equal(calls.find(([name]) => name === 'complete').at(-1).agent_run_scheduled, true);
});

test('a disabled shop does not trigger automatic message processing', async () => {
  const backendCalls = [];
  const backend = {
    async upsertOrder() { return { task: { id: 'task_disabled' } }; },
    async runAgent() { backendCalls.push('agent'); return { task: { id: 'task_disabled', status: 'received' } }; },
  };
  const { workflow, calls } = harness({ backend, runtimeSettings: { automation_enabled: true, shop_enabled: false } });
  const envelope = {
    id: 'evt-disabled-shop', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: 'hello' },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.status, 'completed');
  assert.deepEqual(backendCalls, []);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('preview recognition, quote and AI reply switches independently block their own work', async (t) => {
  const envelope = {
    id: 'evt-switches', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '多少钱', imageUrls: ['https://img.alicdn.com/a.png'] },
  };

  await t.test('recognition off', async () => {
    let recognized = 0;
    const { workflow } = harness({
      runtimeSettings: { automation_enabled: true, recognition_enabled: false, quote_enabled: true, ai_reply_enabled: false },
      quotePreviewClient: { async recognize() { recognized += 1; } },
    });
    await workflow.processClaimed(record(envelope));
    assert.equal(recognized, 0);
  });

  await t.test('quote off', async () => {
    let quoted = 0;
    const { workflow } = harness({
      runtimeSettings: { automation_enabled: true, recognition_enabled: true, quote_enabled: false, ai_reply_enabled: false },
      quotePreviewClient: {
        async recognize() { return { status: 'recognized', recognition: { image_type: 'SEAT_MAP' } }; },
        async quote() { quoted += 1; },
      },
    });
    await workflow.processClaimed(record(envelope));
    assert.equal(quoted, 0);
  });

  await t.test('AI reply off', async () => {
    let drafted = 0;
    const { workflow } = harness({
      runtimeSettings: { automation_enabled: true, recognition_enabled: false, quote_enabled: false, ai_reply_enabled: false },
      replyPreviewClient: { async capture() { drafted += 1; } },
    });
    await workflow.processClaimed(record({ ...envelope, id: 'evt-ai-off', payload: { ...envelope.payload, imageUrls: [] } }));
    assert.equal(drafted, 0);
  });
});

test('a disabled shop suppresses order lifecycle replies and state changes', async () => {
  const stages = [];
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, shop_enabled: false, auto_price_change: true },
    quotePreviewClient: {},
    conversationContextStore: {
      async get() { return { facts: { quote_confirmed: true, quote_total_cents: 8800, quote_expires_at: Date.now() + 60_000 } }; },
      async setOrderStage(...args) { stages.push(args); },
    },
    core: {
      im: { async getSessionByOrder() { return { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }; }, async listMessages() { return { items: [] }; } },
      orders: { async get() { throw new Error('disabled lifecycle must not read order'); } },
    },
  });
  await workflow.processClaimed(record({ id: 'evt-disabled-paid', tenantId: 'tenant-1', event: 'order.paid', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.deepEqual(stages, []);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('a missing platform session fails the event once instead of escaping the worker or retrying', async () => {
  const failed = [];
  const { workflow } = harness({
    quotePreviewClient: {
      async recognize() { return { status: 'recognized', recognition: { image_type: 'SEAT_MAP' } }; },
      async quote() { return { status: 'preview_ready', reply_text: '核价结果' }; },
    },
    autoReplyEnabled: true,
    eventStore: {
      async fail(...args) { failed.push(args); },
      async retry() { throw new Error('404 session must not retry'); },
    },
    core: { im: {
      async listMessages() { return { items: [] }; },
      async sendMessage() { throw Object.assign(new Error('session missing'), { status: 404, code: 'E_NOT_FOUND' }); },
    } },
  });
  const result = await workflow.processClaimed(record({
    id: 'evt-missing-session', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-missing', peerUnb: 'buyer-1', content: '你好' },
  }));
  assert.equal(result.status, 'failed');
  assert.equal(failed.length, 1);
});

test('a new image conversation skips the process tutorial and sends the complete quote with confirmation', async () => {
  let claimed = 0;
  let sentCount = 0;
  const sentIds = new Set();
  const { workflow, calls } = harness({
    autoReplyEnabled: true,
    runtimeSettings: {
      automation_enabled: true, ai_reply_enabled: true,
      reply_templates: {
        first_contact_notice: '后台修改后的首次进线文案',
        quote_confirmation_instruction: '后台修改后的确认提示',
      },
    },
    eventStore: {
      async wasSentMessage(_tenant, _chat, id) { return sentIds.has(id); },
      async recordSentMessage(_tenant, _chat, id) { sentIds.add(id); },
    },
    core: { im: {
      async listSessions() { return { items: [] }; },
      async listMessages() { return { items: sentCount ? [{ direction: 'outbound', messageId: 'sent-1', sentAt: new Date().toISOString() }] : [] }; },
      async sendMessage(input) { sentCount += 1; calls.push(['send', input]); return { messageId: 'sent-1' }; },
    } },
    conversationContextStore: {
      async get() { return { facts: {}, messages: [] }; },
      async claimFirstContactNotice() { claimed += 1; return claimed === 1; },
    },
    quotePreviewClient: {
      async capture() {
        return { status: 'preview_ready', reply_text: '60.00元/张，2张共120.00元。', total_quote_cents: 12000, ticket_count: 2 };
      },
    },
  });
  await workflow.processClaimed(record({
    id: 'evt-first-contact-quote', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] },
  }));
  const sent = calls.filter(([name]) => name === 'send').map(([, input]) => input.text);
  assert.equal(sent.length, 1);
  assert.match(sent[0], /60\.00元\/张/u);
  assert.match(sent[0], /后台修改后的确认提示/u);
  assert.doesNotMatch(sent[0], /保持待付款|价格已修改|首次进线文案/u);
});

test('a text supplement inheriting the buyer first image does not receive the screenshot tutorial', async () => {
  const imageUrl = 'https://img.alicdn.com/seat.png';
  const { workflow, calls } = harness({
    autoReplyEnabled: true,
    runtimeSettings: { automation_enabled: true, recognition_enabled: true, quote_enabled: true, ai_reply_enabled: true, reply_templates: { first_contact_notice: '请上传截图教程' } },
    conversationContextStore: {
      async get() { return { facts: {}, messages: [{ at: Date.now() - 500, role: 'buyer', text: imageUrl, image: true, image_urls: [imageUrl] }] }; },
      async claimFirstContactNotice() { return true; },
    },
    quotePreviewClient: {
      async recognize(envelope) { assert.deepEqual(envelope.payload.imageUrls, [imageUrl]); return { status: 'recognized', recognition: { image_type: 'SEAT_MAP' } }; },
      async quote() { return { status: 'quote_failed', failure_code: 'showtime_not_found', reply_text: '当前官方场次未找到，请发送最新截图。' }; },
    },
  });
  await workflow.processClaimed(record({
    id: 'evt-supplement-inherits-first-image', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '武汉' },
  }));
  const sent = calls.filter(([name]) => name === 'send').map(([, input]) => input.text);
  assert.deepEqual(sent, ['当前官方场次未找到，请发送最新截图。']);
});

test('first-contact guidance is not followed by a duplicate missing-information reply', async () => {
  let claimed = false;
  const { workflow, calls } = harness({
    autoReplyEnabled: true,
    conversationContextStore: {
      async get() { return { facts: {}, messages: [] }; },
      async claimFirstContactNotice() { if (claimed) return false; claimed = true; return true; },
    },
    quotePreviewClient: {
      async capture() { return { status: 'needs_confirmation', failure_code: 'text_quote_missing_fields', reply_text: '请发送完整截图并说明张数。' }; },
    },
  });
  await workflow.processClaimed(record({
    id: 'evt-first-contact-missing', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '你好，我想买会员座' },
  }));
  const sent = calls.filter(([name]) => name === 'send');
  assert.equal(sent.length, 1);
  assert.match(sent[0][1].text, /完整选座页截图/u);
});

test('a failed first-contact send releases its claim and retries WS unavailability with a short fixed delay', async () => {
  let released = 0; let retryDelay = null; let retryMaxAttempts = null;
  const wsError = Object.assign(new Error('账号 WS 当前不可用，请稍后重试'), { code: 'E_IM_WS_UNAVAILABLE' });
  const { workflow } = harness({
    runtimeSettings: { automation_enabled: true, recognition_enabled: true, quote_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'shadow' },
    core: { shops: { async list() { return []; } }, im: { async listSessions() { return { items: [] }; }, async listMessages() { return { items: [] }; }, async sendMessage() { throw wsError; } }, orders: { async get() { return null; } } },
    eventStore: { async retry(_key, _lease, _error, options) { retryDelay = options.delayMs; retryMaxAttempts = options.maxAttempts; return { status: 'retry' }; } },
    conversationContextStore: {
      async get() { return { facts: {}, messages: [] }; }, async claimFirstContactNotice() { return true; },
      async releaseFirstContactNotice() { released += 1; return true; },
    },
    quotePreviewClient: { async recognize() { return { status: 'needs_confirmation', failure_code: 'text_quote_missing_fields' }; }, async quote(value) { return value; } },
    autoReplyEnabled: true,
  });
  const result = await workflow.processClaimed(record({ id: 'evt-ws-first-contact', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now() - 5_000, payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '上海江桥万达' } }));
  assert.equal(result.status, 'retry');
  assert.equal(released, 1);
  assert.equal(retryDelay, 10_000);
  assert.equal(retryMaxAttempts, 360);
});

test('an inherited seat image plus an explicit count receives a deterministic identity follow-up instead of silence', async () => {
  const now = Date.now();
  const { workflow, calls } = harness({
    autoReplyEnabled: true,
    conversationContextStore: {
      async get() { return { facts: {}, messages: [
        { role: 'buyer', text: 'https://img.alicdn.com/seat.png', image: true, image_urls: ['https://img.alicdn.com/seat.png'], at: now - 2_000 },
        { role: 'buyer', text: '这三个', image: false, image_urls: [], at: now },
      ] }; },
      async claimFirstContactNotice() { return false; },
    },
    quotePreviewClient: {
      async recognize() { return { status: 'ignored' }; },
      async quote() { throw new Error('must not quote ignored recognition'); },
    },
  });
  await workflow.processClaimed(record({
    id: 'evt-inherited-image-needs-identity', tenantId: 'tenant-1', event: 'im.message.received', ts: now,
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '这三个' },
  }));
  const sent = calls.filter(([name]) => name === 'send');
  assert.equal(sent.length, 1);
  assert.match(sent[0][1].text, /已收到截图和3张需求/u);
  assert.match(sent[0][1].text, /城市、完整影院分店名和开场时间/u);
});

test('a bare acknowledgement after a failed quote stays silent instead of making a future quote promise', async () => {
  let replyCaptures = 0;
  const { workflow, calls } = harness({
    autoReplyEnabled: true,
    conversationContextStore: {
      async get() { return { facts: {}, messages: [{ role: 'buyer', text: '好的', at: Date.now() }] }; },
      async claimFirstContactNotice() { return false; },
    },
    quotePreviewClient: { async capture() { return { status: 'ignored_no_image' }; } },
    replyPreviewClient: {
      async capture() {
        replyCaptures += 1;
        return { status: 'preview_ready', draft: { intent: '票价咨询', confidence: 0.99, reply: '好的，正在核对票价，稍后直接报给您。' }, autoSend: true };
      },
    },
  });
  await workflow.processClaimed(record({
    id: 'evt-ack-after-failure', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '好的' },
  }));
  assert.equal(replyCaptures, 0);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('a new official-seat image sends one combined recognition and realtime quote', async () => {
  const imageUrl = 'https://img.alicdn.com/confirmed-seat-map.png';
  const venueUrl = 'https://img.alicdn.com/venue-detail.png';
  let quoteCalls = 0;
  let recognitionEnvelope;
  const recognition = {
    image_type: 'SEAT_MAP', cinema: '呼和浩特万达影城喜悦广场店', movie: '奥德赛', date: '2026-08-20', showtime: '15:50', hall: '8号IMAX厅',
    official_selection: { is_selected: true, selected_seat_numbers: ['9排13座', '9排14座', '9排15座'], selected_count: 3 },
  };
  const contextStore = {
    async get() { return { facts: {}, messages: [{ at: Date.now() - 100, text: venueUrl, image_urls: [venueUrl] }, { at: Date.now(), text: imageUrl, image_urls: [imageUrl] }] }; },
    async claimFirstContactNotice() { return false; }, async recordQuoteDraft() {},
    async claimQuoteDraftAttempt() { return true; }, async markQuoted() {},
  };
  const { workflow, calls } = harness({
    autoReplyEnabled: true,
    conversationContextStore: contextStore,
    core: { im: {
      async listSessions() { return { items: [] }; },
      async listMessages() { return { items: [] }; },
      async sendMessage(input) { calls.push(['send', input]); return { messageId: `sent-${calls.length}` }; },
    } },
    quotePreviewClient: {
      async recognize(envelope) { recognitionEnvelope = envelope; return { status: 'recognized', tenant_id: 'tenant-1', recognition, ticket_count: 3, recognition_reply_text: '已识别：\n影院：呼和浩特万达影城喜悦广场店\n影片：奥德赛\n日期：2026-08-20\n场次：15:50\n影厅：8号IMAX厅\n座位：9排13座、9排14座、9排15座\n张数：3\n正在按万达实时价格查询。' }; },
      async quote() { quoteCalls += 1; return { status: 'preview_ready', reply_text: '※【呼和浩特的】| 呼和浩特万达影城喜悦广场店\n电影：奥德赛\n影厅：8号IMAX厅\n场次：2026-08-20 15:50\n座位：9排13座、9排14座、9排15座\n\n52.80元/张，3张合计158.40元。', unit_quote_cents: 5280, total_quote_cents: 15840, ticket_count: 3, recognition }; },
    },
  });

  await workflow.processClaimed(record({ id: 'purchase-info-image', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: [imageUrl] } }));
  assert.equal(quoteCalls, 1);
  assert.deepEqual(recognitionEnvelope.payload.imageUrls, [venueUrl, imageUrl]);
  const sent = calls.filter(([name]) => name === 'send').map(([, input]) => input.text);
  assert.equal(sent.length, 1);
  assert.match(sent[0], /9排13座/u);
  assert.match(sent[0], /158\.40元/u);
});

test('marked-position and no-selection member-seat replies omit seats and ask count and mark status', async () => {
  for (const scenario of [
    { id: 'marked', hand_drawn_circle: { exists: true } },
    { id: 'unselected', hand_drawn_circle: { exists: false } },
  ]) {
    const imageUrl = `https://img.alicdn.com/${scenario.id}.png`;
    let quoteCalls = 0;
    const recordedInstructions = [];
    const { workflow, calls } = harness({
      autoReplyEnabled: true,
      conversationContextStore: {
        async get() { return { facts: {}, messages: [{ at: Date.now(), image_urls: [imageUrl] }] }; },
        async claimFirstContactNotice() { return false; }, async recordQuoteDraft() {}, async claimQuoteDraftAttempt() { return true; },
        async claimConfirmation() { return true; },
        async recordCircledDeliveryInstruction(...args) { recordedInstructions.push(args); },
      },
      quotePreviewClient: {
        async recognize() { return { status: 'recognized', tenant_id: 'tenant-1', recognition: { image_type: 'SEAT_MAP', cinema: '测试万达影城', movie: '奥德赛', date: '2026-08-20', showtime: '16:00', hand_drawn_circle: scenario.hand_drawn_circle, official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } }, recognition_reply_text: '已识别：影院：测试万达影城。正在按万达实时价格查询。' }; },
        async quote() { quoteCalls += 1; return { status: 'preview_ready', quote_scope: 'area_probe', reply_text: '※| 测试万达影城\n电影：奥德赛\n影厅：测试厅\n场次：2026-08-20 16:00\n\n实时单价50.00元/张，请告诉我需要几张，并确认原图是否已标记需要购买的位置；人工出票将按原图标记处理。', unit_quote_cents: 5000, total_quote_cents: null, ticket_count: null }; },
      },
    });
    await workflow.processClaimed(record({ id: scenario.id, tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: `chat-${scenario.id}`, peerUnb: 'buyer-1', imageUrls: [imageUrl] } }));
    assert.equal(quoteCalls, 1);
    const text = calls.find(([name]) => name === 'send')[1].text;
    assert.doesNotMatch(text, /座位：/u);
    assert.match(text, /需要几张/u);
    assert.match(text, /是否已标记/u);
    assert.match(text, /按原图标记处理/u);
    assert.equal(recordedInstructions.length, scenario.hand_drawn_circle.exists ? 1 : 0);
    if (scenario.hand_drawn_circle.exists) assert.equal(recordedInstructions[0][2], imageUrl);
  }
});

test('quote preview only mode captures an inbound image without calling the legacy backend or sending a message', async () => {
  const previews = [];
  const { workflow, calls } = harness({
    backend: { async upsertOrder() { throw new Error('legacy backend must not run in preview mode'); } },
    quotePreviewClient: { async capture(envelope) { previews.push(envelope); return { id: 'preview-1', status: 'preview_ready' }; } },
  });
  const envelope = {
    id: 'evt-preview-1', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '两张多少钱', imageUrls: ['https://img.alicdn.com/a.png'] },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.mode, 'quote_preview_only');
  assert.equal(previews.length, 1);
  assert.equal(calls.some(([name]) => name === 'send'), false);
  const completed = calls.find(([name]) => name === 'complete');
  assert.deepEqual(completed.at(-1).actions, []);
});

test('a first-contact image is claimed without sending an irrelevant screenshot guide before its quote', async () => {
  let recognitionStarted = false;
  let claimed = false;
  const sent = [];
  const { workflow } = harness({
    autoReplyEnabled: true,
    conversationContextStore: {
      async get() { return { facts: {}, messages: [] }; },
      async claimFirstContactNotice() { if (claimed) return false; claimed = true; return true; },
    },
    core: { im: {
      async listMessages() { return { items: [] }; },
      async sendMessage(input) { assert.equal(recognitionStarted, true); sent.push(input.text); return { messageId: `sent-${Date.now()}` }; },
    } },
    quotePreviewClient: {
      async recognize() { recognitionStarted = true; return { status: 'recognized', ticket_count: 1, recognition: { image_type: 'SEAT_MAP' } }; },
      async quote() { return { status: 'preview_ready', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1, reply_text: '50.00元/张，1张合计50.00元。' }; },
    },
  });

  await workflow.processClaimed(record({
    id: 'evt-overlap-first-contact', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] },
  }));
  assert.equal(claimed, true);
  assert.deepEqual(sent, ['50.00元/张，1张合计50.00元。\n接受本次报价请回复“确认”。']);
  assert.equal(sent.some((text) => /请发送.*截图/u.test(text)), false);
});

test('a verified seat quote sends one combined identity and price message after realtime quote succeeds', async () => {
  let quoteStartedAfterRecognitionReply = false;
  let calls;
  const setup = harness({
    quotePreviewClient: {
      async recognize() {
        return {
          status: 'recognized', tenant_id: 'tenant-1', ticket_count: 1,
          recognition: { image_type: 'SEAT_MAP', cinema: '北京万达影城', official_selection: { selected_count: 1 } },
          recognition_reply_text: '已识别：影院：北京万达影城；张数：1。正在按万达实时价格查询。',
        };
      },
      async quote() {
        quoteStartedAfterRecognitionReply = calls.some(([name, input]) => name === 'send' && input.text.includes('北京万达影城'));
        return {
          status: 'preview_ready', quote_unit_cents: 6290, quote_total_cents: 6290, quote_ticket_count: 1,
          reply_text: '※【北京的】| 北京万达影城\n电影：奥德赛\n影厅：1号厅\n场次：2026-08-20 20:00\n座位：6排8座\n\n62.90元/张，1张合计62.90元。',
        };
      },
    },
    autoReplyEnabled: true,
  });
  ({ calls } = setup);
  await setup.workflow.processClaimed(record({
    id: 'evt-two-stage', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] },
  }));
  assert.equal(quoteStartedAfterRecognitionReply, false);
  const sent = calls.filter(([name]) => name === 'send').map(([, input]) => input.text);
  assert.equal(sent.length, 1);
  assert.match(sent[0], /北京万达影城/u);
  assert.match(sent[0], /座位：6排8座/u);
  assert.match(sent[0], /62\.90元\/张，1张合计62\.90元/u);
  assert.match(sent[0], /请回复“确认”/u);
  const completed = calls.find(([name]) => name === 'complete');
  assert.equal(completed.at(-1).agent_reply_snapshot.kind, 'quote');
  assert.equal(completed.at(-1).agent_reply_snapshot.text, sent[0]);
});

test('a better buyer-app price sends only the configured recommendation and never binds a quote', async () => {
  const marked = [];
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async recognize() { return { status: 'recognized', tenant_id: 'tenant-1', ticket_count: 2, recognition: { image_type: 'SEAT_MAP' } }; },
      async quote() { return { status: 'quote_not_competitive', reply_text: '您现在用的APP有合适的优惠价，可以自行购买。', recognition: { image_type: 'SEAT_MAP' } }; },
    },
    conversationContextStore: {
      async get() { return { facts: {}, messages: [] }; },
      async markQuoted(...args) { marked.push(args); },
    },
    autoReplyEnabled: true,
  });

  await workflow.processClaimed(record({
    id: 'evt-buyer-app-better', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] },
  }));

  const sent = calls.filter(([name]) => name === 'send').map(([, input]) => input.text);
  assert.deepEqual(sent, ['您现在用的APP有合适的优惠价，可以自行购买。']);
  assert.deepEqual(marked, []);
});

test('an exact-seat screenshot clearly replaces a different active area quote', async () => {
  const marked = [];
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async recognize() {
        return {
          status: 'recognized', tenant_id: 'tenant-1', ticket_count: 2,
          recognition: { image_type: 'ORDER_CONFIRM', official_selection: { is_selected: true, selected_seat_numbers: ['5排8座', '5排9座'], selected_count: 2 } },
        };
      },
      async quote() {
        return {
          status: 'preview_ready', quote_scope: 'exact_seats', unit_quote_cents: 4100,
          total_quote_cents: 8200, ticket_count: 2,
          recognition: { official_selection: { is_selected: true, selected_count: 2 } },
          reply_text: '具体座位实时报价：41.00元/张，2张合计82.00元。',
        };
      },
    },
    conversationContextStore: {
      async get() { return { facts: { quote_scope: 'area_probe', quote_total_cents: 7400, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, quote_confirmed: true }, messages: [] }; },
      async markQuoted(...args) { marked.push(args); },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-exact-replaces-area', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/exact.png'] },
  }));
  const sent = calls.find(([name]) => name === 'send')[1].text;
  assert.match(sent, /官方已选座截图/u);
  assert.match(sent, /上一版未选座试价已失效/u);
  assert.match(sent, /2张合计82\.00元/u);
  assert.deepEqual(marked[0][2], { unitQuoteCents: 4100, totalQuoteCents: 8200, ticketCount: 2, cinema: undefined, quoteScope: 'exact_seats', pricingRuleVersion: undefined, replyDelivered: true });
});

test('reprocessing the same event keeps one stable reply action and does not send twice', async () => {
  const messages = [];
  const pluginMessageIds = new Set();
  let sends = 0;
  const completions = [];
  const { workflow } = harness({
    autoReplyEnabled: true,
    eventStore: {
      async wasSentMessage(_tenantId, _chatId, messageId) { return pluginMessageIds.has(messageId); },
      async recordSentMessage(_tenantId, _chatId, messageId) { pluginMessageIds.add(messageId); },
      async complete(_key, _leaseId, result) { completions.push(result); },
    },
    core: { im: {
      async listMessages() { return { items: [...messages] }; },
      async sendMessage(input) {
        sends += 1;
        const messageId = `same-event-reply-${sends}`;
        messages.unshift({ direction: 'outbound', messageId, content: input.text, sentAt: new Date().toISOString() });
        return { messageId };
      },
    } },
    quotePreviewClient: {
      async capture() { return { status: 'quote_failed', failure_code: 'showtime_not_found', reply_text: '暂未核到该场实时价格，请补充开场时间。' }; },
    },
  });
  const claimed = record({
    id: 'evt-same-reply', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '这场多少钱' },
  });
  await workflow.processClaimed(claimed);
  await workflow.processClaimed(claimed);

  assert.equal(sends, 1);
  assert.equal(completions.length, 2);
  assert.deepEqual(completions.map((item) => item.actions[0].action_id), ['evt-same-reply:quote-reply', 'evt-same-reply:quote-reply']);
  assert.equal(completions[0].actions[0].status, 'succeeded');
  assert.deepEqual(completions[1].actions[0], {
    action_id: 'evt-same-reply:quote-reply', status: 'skipped', reason: 'duplicate_reply',
  });
});

test('a pre-recognition duplicate draft does not call the quote or generic reply clients', async () => {
  let quoteCalls = 0;
  let replyCalls = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async recognize() { return { status: 'quote_deduplicated' }; },
      async quote() { quoteCalls += 1; throw new Error('duplicate draft must not quote'); },
    },
    replyPreviewClient: { async capture() { replyCalls += 1; return { status: 'preview_ready', autoSend: true }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-duplicate-draft', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] },
  }));
  assert.equal(quoteCalls, 0);
  assert.equal(replyCalls, 0);
  assert.equal(calls.some(([name]) => name === 'send'), false);
  assert.equal(calls.find(([name]) => name === 'complete').at(-1).quote_skipped, 'duplicate_quote_draft');
});

test('a seat supplement rechecks the latest image but sends only the refreshed quote', async () => {
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async recognize(envelope) {
        assert.deepEqual(envelope.payload.imageUrls, ['https://img.alicdn.com/seat.png']);
        return { status: 'recognized', tenant_id: 'tenant-1', recognition: { image_type: 'SEAT_MAP' }, recognition_reply_text: '已识别：不应重复发送' };
      },
      async quote() { return { status: 'preview_ready', unit_quote_cents: 6200, reply_text: '万达实时核价：62.0元/张。' }; },
    },
    conversationContextStore: { async get() { return { messages: [{ at: Date.now() - 1_000, image_urls: ['https://img.alicdn.com/seat.png'] }] }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-seat-supplement', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '8排13' },
  }));
  assert.deepEqual(calls.filter(([name]) => name === 'send').map(([, input]) => input.text), ['万达实时核价：62.0元/张。']);
});

test('a count-only supplement reuses the bounded recognition artifact without another vision call', async () => {
  let recognitionCalls = 0;
  const now = Date.now();
  const artifact = { status: 'recognized', tenant_id: 'tenant-1', ticket_count: 1, recognition: { image_type: 'SEAT_MAP', cinema: '江桥万达', movie: '测试影片', date: '2026-08-22', showtime: '19:30', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } };
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async recognize() { recognitionCalls += 1; throw new Error('count-only supplement must reuse recognition'); },
      async quote(input) { assert.equal(input.ticket_count, 2); return { status: 'preview_ready', unit_quote_cents: 5000, total_quote_cents: 10000, ticket_count: 2, reply_text: '实时单价50.00元/张，2张合计100.00元。' }; },
    },
    conversationContextStore: {
      async get() { return { facts: { quote_draft: { recognition_artifact: artifact, expires_at: now + 60_000 } }, messages: [{ at: now - 1_000, image_urls: ['https://img.alicdn.com/seat.png'] }, { at: now, text: '2张' }] }; },
      async recordQuoteDraft() {}, async claimQuoteDraftAttempt() { return true; }, async markQuoted() {},
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-count-artifact', tenantId: 'tenant-1', event: 'im.message.received', ts: now,
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '2张' },
  }));
  assert.equal(recognitionCalls, 0);
  const completion = calls.find(([name]) => name === 'complete').at(-1);
  assert.equal(completion.recognition_reused, 'count_only_quote_draft');
  assert.equal(completion.timings_ms.recognition, 0);
});

test('a Chinese-row seat list inherits the latest image for a three-ticket refreshed quote', async () => {
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async recognize(envelope) {
        assert.deepEqual(envelope.payload.imageUrls, ['https://img.alicdn.com/seat.png']);
        assert.match(envelope.payload.content, /八排12 13 14/u);
        return { status: 'recognized', tenant_id: 'tenant-1', ticket_count: 3, recognition: { image_type: 'SEAT_MAP' } };
      },
      async quote() { return { status: 'preview_ready', quote_ticket_count: 3, quote_unit_cents: 4300, quote_total_cents: 12900, reply_text: '万达实时核价：43.0元/张，3张合计129.0元。' }; },
    },
    conversationContextStore: { async get() { return { messages: [{ at: Date.now() - 1_000, image_urls: ['https://img.alicdn.com/seat.png'] }] }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-chinese-row-seat-supplement', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '八排12 13 14' },
  }));
  const sent = calls.filter(([name]) => name === 'send').map(([, input]) => input.text);
  assert.equal(sent.length, 1);
  assert.match(sent[0], /^万达实时核价：43\.0元\/张，3张合计129\.0元/u);
  assert.match(sent[0], /请回复“确认”/u);
});

test('a later showtime supplement combines the active quote draft with the recent seat image', async () => {
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async recognize(envelope) {
        assert.deepEqual(envelope.payload.imageUrls, ['https://img.alicdn.com/seat.png']);
        assert.match(envelope.payload.content, /影院：十堰万达影城/u);
        assert.match(envelope.payload.content, /影片：欢迎来龙餐馆/u);
        assert.match(envelope.payload.content, /19:10/u);
        return { status: 'recognized', tenant_id: 'tenant-1', ticket_count: 2, recognition: { image_type: 'SEAT_MAP' } };
      },
      async quote() { return { status: 'preview_ready', quote_unit_cents: 5300, quote_total_cents: 10600, quote_ticket_count: 2, reply_text: '万达实时核价：53元/张，2张合计106元。' }; },
    },
    conversationContextStore: {
      async get() {
        return {
          facts: { quote_draft: { cinema: '十堰万达影城', movie: '欢迎来龙餐馆', date: '2026-08-19', showtime: '19:10', hall: '7号厅', ticket_count: 2, expires_at: Date.now() + 60_000 } },
          messages: [{ at: Date.now() - 1_000, image_urls: ['https://img.alicdn.com/seat.png'] }],
        };
      },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-draft-showtime-supplement', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '19:10这场' },
  }));
  const sent = calls.filter(([name]) => name === 'send').map(([, input]) => input.text);
  assert.equal(sent.length, 1);
  assert.match(sent[0], /^万达实时核价：53元\/张，2张合计106元/u);
  assert.match(sent[0], /请回复“确认”/u);
});

test('a quote failure sends one failure reply without an extra recognition message', async () => {
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async recognize() { return { status: 'recognized', recognition_reply_text: '已识别：影院：北京万达影城。正在核价。' }; },
      async quote() { return { status: 'quote_failed', failure_code: 'showtime_not_found', reply_text: '暂未核到该场实时价格，请补充开场时间。', recognition: { city: '武汉', cinema: '万达影城（汉街万达IMAX激光店）', movie: '奥德赛', date: '2026-08-23', showtime: '22:25', hall: 'IMAX厅-COLA银幕' } }; },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-quote-failure', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] },
  }));
  assert.deepEqual(calls.filter(([name]) => name === 'send').map(([, input]) => input.text), ['暂未核到该场实时价格，请补充开场时间。']);
  assert.deepEqual(calls.find(([name]) => name === 'complete')[3].cinema_match_evaluation_snapshot, {
    status: 'quote_failed', failure_code: 'showtime_not_found', city: '武汉', cinema: '万达影城（汉街万达IMAX激光店）',
    movie: '奥德赛', date: '2026-08-23', showtime: '22:25', hall: 'IMAX厅-COLA银幕', matched_cinema_name: null,
  });
});

test('temporary-lock release failure fails closed and cannot be overwritten by an agent reply', async () => {
  let agentCalls = 0;
  const markedQuotes = [];
  const { workflow, calls } = harness({
    autoReplyEnabled: true,
    runtimeSettings: {
      automation_enabled: true, recognition_enabled: true, quote_enabled: true, ai_reply_enabled: true,
      conversation_agent_mode: 'active', execution_owner: 'agent',
      reply_templates: { temporary_lock_release_unverified: '临时试价座位未确认释放，已停止自动报价并转人工处理。' },
    },
    conversationContextStore: {
      async get() { return { facts: {}, messages: [] }; },
      async recordQuoteDraft() {},
      async claimQuoteDraftAttempt() { return true; },
      async markQuoted(...args) { markedQuotes.push(args); },
    },
    conversationAgentPlanner: {
      async plan() {
        agentCalls += 1;
        return { intent: '选座核价', confidence: 1, goal: '覆盖失败', action: 'respond', arguments: {}, missing_fields: [], reply: '座位已释放，可以继续付款。', needs_human: false, reason: '模型误判' };
      },
    },
    quotePreviewClient: {
      async recognize() { return { status: 'recognized', recognition: { image_type: 'SEAT_MAP' } }; },
      async quote() { return { status: 'quote_failed', failure_code: 'temporary_lock_release_unverified', reply_text: '临时试价座位的释放状态暂未确认，已停止自动报价；请勿付款，并稍后刷新选座页后重试。' }; },
    },
  });
  await workflow.processClaimed(record({
    id: 'evt-safe-release-failure', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] },
  }));
  const sent = calls.filter(([name]) => name === 'send').map(([, input]) => input.text);
  assert.deepEqual(sent, ['临时试价座位的释放状态暂未确认，已停止自动报价；请勿付款，并稍后刷新选座页后重试。']);
  assert.equal(agentCalls, 0);
  assert.deepEqual(markedQuotes, []);
  const completion = calls.find(([name]) => name === 'complete')[3];
  assert.equal(completion.quote_failure_code, 'temporary_lock_release_unverified');
  assert.deepEqual(completion.agent_reply_snapshot, { kind: 'conversation_follow_up', text: sent[0] });
});

test('a failed seat-map recognition sends a safe request for a clearer image', async () => {
  const { workflow, calls } = harness({
    quotePreviewClient: { async recognize() { throw new Error('ai_vision_schema_invalid'); }, async quote() { throw new Error('must not quote'); } },
    replyPreviewClient: { async capture() { return { status: 'preview_ready', autoSend: false }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-vision-failure', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] } }));
  assert.match(calls.find(([name]) => name === 'send')[1].text, /暂未识别成功/);
});

test('custom purchase copy cannot remove the mandatory wait-for-price-change instruction', () => {
  const mandatory = '提交订单后请先不要付款，等待系统确认改价成功后再付款。';
  assert.equal(paymentSafeOrderInstruction('点击立即购买后返回'), `点击立即购买后返回\n${mandatory}`);
  assert.equal(paymentSafeOrderInstruction(`操作说明\n${mandatory}`), `操作说明\n${mandatory}`);
});

test('a confirmation after an active quote sends safe purchase instructions and the guide image', async () => {
  let confirmed = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: { async recognize() { throw new Error('must not re-recognize'); }, async quote() { throw new Error('must not re-quote'); } },
    conversationContextStore: {
      async get() { return { facts: { quote_total_cents: 8800, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, stage: 'quoted' } }; },
      async markQuoteConfirmed() { confirmed += 1; return true; },
    },
    core: {
      im: {
        async listMessages() { return { items: [] }; },
        async sendMessage(input) { calls.push(['send', input]); return { messageId: 'instruction-1' }; },
        async uploadImage(input) {
          assert.equal(input.contentType, 'image/jpeg');
          assert.ok(input.data.byteLength > 100_000);
          calls.push(['upload-guide', input.filename]);
          return { imageUrl: 'https://img.alicdn.com/order-submit-guide.jpg', width: 1080, height: 2059 };
        },
        async sendImage(input) { calls.push(['send-guide', input]); return { messageId: 'guide-1' }; },
      },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-confirm-quote', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: 'OK' } }));
  assert.equal(confirmed, 1);
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）\n提交订单后请先不要付款，等待系统确认改价成功后再付款。');
  assert.equal(calls.filter(([name]) => name === 'upload-guide').length, 1);
  assert.equal(calls.filter(([name]) => name === 'send-guide').length, 1);
  const completed = calls.find(([name]) => name === 'complete')?.at(-1);
  assert.equal(completed.agent_state_snapshot.stage, 'quoted');
  assert.equal(completed.agent_state_snapshot.quote_total_cents, 8800);
  assert.equal(completed.agent_reply_snapshot.kind, 'conversation_follow_up');
  assert.match(completed.agent_reply_snapshot.text, /先不要付款/u);
});

test('a quote missing policy-version or delivery evidence cannot enter the order flow', async () => {
  const { workflow, calls } = harness({
    eventStore: { async fail(_key, _lease, error) { throw error; } },
    quotePreviewClient: { async recognize() { throw new Error('must not re-recognize'); }, async quote() { throw new Error('must not re-quote'); } },
    core: { im: { async listMessages() { return { items: [] }; }, async sendMessage(input) { calls.push(['send', input]); return { messageId: 'safe-stop-1' }; } } },
    conversationContextStore: {
      async get() { return { facts: { quote_total_cents: 8800, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, stage: 'quoted' } }; },
      async markQuoteConfirmed() { return false; },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-confirm-incomplete-quote', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '确认' } }));
  const text = calls.find(([name]) => name === 'send')?.[1]?.text ?? '';
  assert.match(text, /缺少有效规则版本或送达确认/u);
  assert.doesNotMatch(text, /提交订单/u);
});

test('order-intent phrases confirm an active quote before the order is created', async () => {
  for (const content of ['下单', '我拍了', '已拍下', '待付款', '改价']) {
    let confirmed = 0;
    const { workflow } = harness({
      quotePreviewClient: { async recognize() { throw new Error('must not re-recognize'); }, async quote() { throw new Error('must not re-quote'); } },
      conversationContextStore: {
        async get() { return { facts: { quote_total_cents: 13000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, stage: 'quoted' }, messages: [] }; },
        async markQuoteConfirmed() { confirmed += 1; return true; },
      },
      autoReplyEnabled: true,
    });
    await workflow.processClaimed(record({ id: `evt-order-intent-${content}`, tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content } }));
    assert.equal(confirmed, 1, content);
  }
});

test('a conflicting recent ticket count blocks generic quote confirmation and requires a new official selection', async () => {
  let markedForReview = 0;
  let confirmed = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: { async recognize() { throw new Error('must not re-recognize'); }, async quote() { throw new Error('must not re-quote'); } },
    conversationContextStore: {
      async get() { return { facts: { quote_total_cents: 4500, quote_ticket_count: 1, quote_expires_at: Date.now() + 60_000, stage: 'quoted' }, messages: [{ role: 'buyer', text: '六张呗' }, { role: 'buyer', text: '好的' }] }; },
      async markQuoteConfirmed() { confirmed += 1; return true; },
      async markQuoteNeedsReview() { markedForReview += 1; return true; },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-conflicting-count', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '好的' } }));
  assert.equal(confirmed, 0);
  assert.equal(markedForReview, 1);
  assert.match(calls.find(([name]) => name === 'send')[1].text, /需要 6 张/);
  assert.match(calls.find(([name]) => name === 'send')[1].text, /官方已选好 6 个座位的截图/);
});

test('a changed ticket count blocks an active quote before any further quote work', async () => {
  let markedForReview = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async availableSeats() { throw new Error('a count conflict must stop before seat lookup'); },
      async recognize() { throw new Error('must not re-recognize'); },
      async quote() { throw new Error('must not re-quote'); },
    },
    conversationContextStore: {
      async get() { return { facts: { quote_total_cents: 3600, quote_ticket_count: 1, quote_expires_at: Date.now() + 60_000, stage: 'quoted' }, messages: [] }; },
      async markQuoteNeedsReview() { markedForReview += 1; return true; },
    },
    eventStore: { async retry(key, leaseId, error) { throw error; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-count-change-before-row', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '我要两张票' },
  }));
  assert.equal(markedForReview, 1);
  const sent = calls.find(([name]) => name === 'send')[1].text;
  assert.match(sent, /只核验了1张/u);
  assert.match(sent, /需要2张/u);
  assert.match(sent, /请先不要付款/u);
});

test('seat coordinates are never interpreted as a conflicting ticket count', async () => {
  let markedForReview = 0;
  let confirmed = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: { async recognize() { throw new Error('must not re-recognize'); }, async quote() { throw new Error('must not re-quote'); } },
    conversationContextStore: {
      async get() {
        return {
          facts: { quote_total_cents: 4490, quote_ticket_count: 1, quote_expires_at: Date.now() + 60_000, stage: 'quoted' },
          messages: [
            { role: 'buyer', text: '7排15座' },
            { role: 'buyer', text: '6排16' },
            { role: 'buyer', text: '好的' },
          ],
        };
      },
      async markQuoteConfirmed() { confirmed += 1; return true; },
      async markQuoteNeedsReview() { markedForReview += 1; return true; },
    },
    core: {
      im: {
        async listMessages() { return { items: [] }; },
        async sendMessage(input) { calls.push(['send', input]); return { messageId: 'instruction-seat-count' }; },
        async uploadImage() { return { imageUrl: 'https://img.alicdn.com/order-guide.jpg' }; },
        async sendImage() { return { messageId: 'guide-seat-count' }; },
      },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-seat-not-count', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '好的' } }));
  assert.equal(confirmed, 1);
  assert.equal(markedForReview, 0);
  assert.doesNotMatch(calls.find(([name]) => name === 'send')[1].text, /15\s*张/u);
});

test('an exact seat coordinate without the 座 suffix is recorded without querying row availability', async () => {
  let replyPreviewCalls = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async availableSeats() { throw new Error('an exact seat coordinate must not query row availability'); },
      async recognize() { throw new Error('must not re-recognize'); },
      async quote() { throw new Error('must not re-quote'); },
    },
    replyPreviewClient: { async capture() { replyPreviewCalls += 1; return { status: 'preview_ready' }; } },
    conversationContextStore: {
      async get() { return { facts: { quote_total_cents: 4490, quote_ticket_count: 1, quote_expires_at: Date.now() + 60_000, stage: 'quoted' }, messages: [] }; },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-exact-seat-no-suffix', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '6排16' } }));
  assert.equal(replyPreviewCalls, 0);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('an explicit row preference after an active quote lists current realtime W+ seats', async () => {
  const quoteDraft = {
    expires_at: Date.now() + 60_000,
    fields: {
      city: { value: '重庆' }, cinema: { value: '重庆北碚万达广场店' }, movie: { value: '奥德赛' },
      date: { value: '2026-08-20' }, showtime: { value: '21:00-23:30' }, hall: { value: 'IMAX厅' },
    },
  };
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async availableSeats({ recognition, row }) {
        assert.equal(row, 10);
        assert.equal(recognition.cinema, '重庆北碚万达广场店');
        return { row, seats: ['10排8座', '10排9座', '10排10座'], available_count: 3, wplus_offer_available: true };
      },
      async recognize() { throw new Error('row lookup must not re-run recognition'); },
      async quote() { throw new Error('row lookup must not re-run quote'); },
    },
    conversationContextStore: {
      async get() { return { facts: { quote_total_cents: 11220, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, stage: 'quoted', quote_draft: quoteDraft }, messages: [] }; },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-row-availability', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '10排的座位' },
  }));
  const sent = calls.find(([name]) => name === 'send')[1].text;
  assert.match(sent, /可以选/u);
  assert.match(sent, /10排8座、10排9座、10排10座/u);
  assert.doesNotMatch(sent, /请回复想要的/u);
  assert.doesNotMatch(sent, /实时复核/u);
});

test('typed seat preferences after an active quote do not query seats or recheck price', async () => {
  let replyPreviewCalls = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async availableSeats() { throw new Error('typed choices must not query seats again'); },
      async recognize() { throw new Error('typed choices must not re-recognize'); },
      async quote() { throw new Error('typed choices must not re-quote'); },
    },
    replyPreviewClient: { async capture() { replyPreviewCalls += 1; return { status: 'preview_ready' }; } },
    conversationContextStore: {
      async get() { return { facts: { quote_total_cents: 15600, quote_ticket_count: 3, quote_expires_at: Date.now() + 60_000, stage: 'quoted' }, messages: [] }; },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-typed-seat-choice', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '5排3座、5排4座、5排5座' },
  }));
  assert.equal(replyPreviewCalls, 0);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('price and purchase questions reuse the delivered active quote without recognition or another temporary probe', async () => {
  for (const [index, content] of ['多少钱', '你这多少', '会员价可以优惠吗', '那我就是直接62.70一张拍下是吧', '不是53？', '这个呢'].entries()) {
    const { workflow, calls } = harness({
      quotePreviewClient: { async recognize() { throw new Error('must not re-recognize an active quote'); }, async quote() { throw new Error('must not re-quote an active quote'); } },
      conversationContextStore: { async get() { return { facts: { quote_unit_cents: 6270, quote_total_cents: 12540, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, stage: 'quoted' }, messages: [] }; } },
      autoReplyEnabled: true,
    });
    await workflow.processClaimed(record({ id: `evt-active-price-${index}`, tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: `chat-${index}`, peerUnb: 'buyer-1', content } }));
    const sent = calls.find(([name]) => name === 'send')[1].text;
    assert.match(sent, /62\.70元\/张，2张合计125\.40元/u);
    assert.match(sent, /请回复“确认”/u);
    assert.match(sent, /请不要直接付款/u);
  }
});

test('a seat-reference statement after an active quote is suppressed without recognition, quote, or generic model', async () => {
  let replyCalls = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: { async recognize() { throw new Error('must not re-recognize'); }, async quote() { throw new Error('must not re-quote'); } },
    replyPreviewClient: { async capture() { replyCalls += 1; return { status: 'preview_ready' }; } },
    conversationContextStore: { async get() { return { facts: { quote_total_cents: 12540, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, stage: 'quoted' }, messages: [] }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-seat-reference', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '这两个座位' } }));
  assert.equal(replyCalls, 0);
  assert.equal(calls.some(([name]) => name === 'send'), false);
  assert.equal(calls.find(([name]) => name === 'complete').at(-1).quote_skipped, 'typed_seat_preference_after_quote');
});

test('a seat clarification question after an active quote does not re-run recognition', async () => {
  const { workflow, calls } = harness({
    quotePreviewClient: { async recognize() { throw new Error('must not re-recognize'); }, async quote() { throw new Error('must not re-quote'); } },
    conversationContextStore: { async get() { return { facts: { quote_total_cents: 8800, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, stage: 'quoted' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-seat-question-after-quote', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '是第七行正中间这两个座位吗' } }));
  assert.match(calls.find(([name]) => name === 'send')[1].text, /暂不能确认最终锁定的具体座位/);
});

test('seat or quantity fragments after an active quote do not trigger repetitive generic AI replies', async () => {
  let replyPreviewCalls = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: { async recognize() { return { status: 'ignored' }; }, async quote() { throw new Error('must not quote'); } },
    replyPreviewClient: { async capture() { replyPreviewCalls += 1; return { status: 'preview_ready', draft: { reply: '重复的通用回复' } }; } },
    conversationContextStore: { async get() { return { facts: { quote_total_cents: 10975, quote_ticket_count: 1, quote_expires_at: Date.now() + 60_000, stage: 'quoted' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-seat-fragment-after-quote', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '10排 7 8 座' } }));
  assert.equal(replyPreviewCalls, 0);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('a confirmed unpaid order is automatically changed to the verified quote total', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async bindOrder() { return true; },
      async get() { return { facts: { quote_confirmed: true, quote_total_cents: 8800, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 } }; },
    },
    core: { im: { async getSessionByOrder() { return contextPayload; }, async listMessages() { return { items: [] }; } }, orders: { async get() { return { orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '10000', postFee: '0' }; }, async changePrice(orderId, input) { calls.push(['change-price', orderId, input]); } } },
  });
  await workflow.processClaimed(record({ id: 'evt-order-created', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.deepEqual(calls.find(([name]) => name === 'change-price'), ['change-price', 'order-1', { priceFee: 8800, transportFee: 0 }]);
});

test('order creation blocks price change when recent buyer quantity conflicts with the quote', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const exceptions = [];
  let priceChanges = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async bindOrder() { return true; },
      async get() {
        return {
          facts: { quote_confirmed: false, quote_total_cents: 3600, quote_ticket_count: 1, quote_expires_at: Date.now() + 60_000 },
          messages: [
            { role: 'buyer', text: '鸡西万达，明天下午2点05的票，两张，7排中间两位' },
            { role: 'buyer', text: '我已拍下，待付款' },
          ],
        };
      },
      async markOrderException(...args) { exceptions.push(args); return true; },
      async markQuoteConfirmed() { throw new Error('a conflicting quote must not be confirmed'); },
    },
    core: {
      im: {
        async getSessionByOrder() { return contextPayload; },
        async listMessages() { return { items: [] }; },
        async sendMessage(input) { calls.push(['send', input]); return { messageId: 'count-conflict-warning' }; },
      },
      orders: { async changePrice() { priceChanges += 1; } },
    },
    eventStore: { async retry(key, leaseId, error) { throw error; } },
  });
  await workflow.processClaimed(record({ id: 'evt-order-created-count-conflict', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(priceChanges, 0);
  assert.equal(exceptions[0][2], 'ticket_count_conflict');
  assert.equal(exceptions[0][3], 'order-1');
  assert.match(calls.find(([name]) => name === 'send')[1].text, /只核验了1张/u);
  assert.match(calls.find(([name]) => name === 'send')[1].text, /需要2张/u);
});

test('a human-takeover price gate is visible in the manual exception queue', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const exceptions = [];
  const { workflow } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async bindOrder() { return true; },
      async get() { return { facts: { quote_confirmed: true, quote_total_cents: 12000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 } }; },
      async markOrderException(...args) { exceptions.push(args); },
    },
    core: {
      im: {
        async getSessionByOrder() { return contextPayload; },
        async listMessages() { return { items: [{ direction: 'outbound', messageId: 'human-1', sentAt: new Date().toISOString() }] }; },
      },
      orders: { async get() { return { orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '2000', postFee: '0' }; } },
    },
  });
  await workflow.processClaimed(record({ id: 'evt-order-created-human', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(exceptions[0][2], 'price_change_gate_human_takeover');
  assert.equal(exceptions[0][3], 'order-1');
});

test('a queued order-created event honors a recent purchase intent before the delayed message worker confirms it', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  let confirmed = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async get() { return { facts: { quote_total_cents: 13000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 }, messages: [{ role: 'buyer', text: '我已拍下，待付款' }] }; },
      async markQuoteConfirmed() { confirmed += 1; return true; },
    },
    core: { im: { async getSessionByOrder() { return contextPayload; }, async listMessages() { return { items: [] }; } }, orders: { async get() { return { orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '10000', postFee: '0' }; }, async changePrice(orderId, input) { calls.push(['change-price', orderId, input]); } } },
  });
  await workflow.processClaimed(record({ id: 'evt-order-created-purchase-intent', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(confirmed, 1);
  assert.deepEqual(calls.find(([name]) => name === 'change-price'), ['change-price', 'order-1', { priceFee: 13000, transportFee: 0 }]);
});

test('CANNOT_MODIFY_FEE uses bounded readiness retries but never retries the whole event', async () => {
  const failed = [];
  let writes = 0;
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    runtimeSettings: {
      automation_enabled: true, auto_price_change: true, price_change_enabled: true,
      reply_templates: { order_price_change_failed: '当前订单金额无法自动修改，请先不要付款，已转人工处理。' },
    },
    conversationContextStore: {
      async bindOrder() { return true; },
      async get() { return { facts: { quote_confirmed: true, quote_total_cents: 8800, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 } }; },
    },
    eventStore: {
      async fail(...args) { failed.push(args); },
      async retry() { throw new Error('terminal rejection must not be retried'); },
    },
    core: {
      im: { async getSessionByOrder() { return contextPayload; }, async listMessages() { return { items: [] }; }, async sendMessage(input) { calls.push(['send', input]); return { messageId: 'terminal-notice-1' }; } },
      orders: {
        async get() { return { orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '10000', postFee: '0' }; },
        async changePrice() {
          writes += 1;
          throw Object.assign(new Error('CANNOT_MODIFY_FEE'), {
            status: 400,
            code: 'E_OAUTH_FLOW_FAILED',
            body: { message: 'CANNOT_MODIFY_FEE' },
          });
        },
      },
    },
  });

  const result = await workflow.processClaimed(record({ id: 'evt-terminal-price-rejection', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(), payload: { orderId: 'order-1' } }));

  assert.equal(result.status, 'completed');
  assert.equal(failed.length, 0);
  assert.equal(writes, 3);
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '当前订单金额无法自动修改，请先不要付款，已转人工处理。');
});

test('a terminal price-change rejection tells the buyer not to pay and enters manual review', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const exceptions = [];
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async get() { return { facts: { quote_confirmed: true, quote_total_cents: 8800, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 } }; },
      async markOrderException(...args) { exceptions.push(args); },
    },
    core: {
      im: { async getSessionByOrder() { return contextPayload; }, async listMessages() { return { items: [] }; }, async sendMessage(input) { calls.push(['send', input]); return { messageId: 'failed-price-change-1' }; } },
      orders: { async get() { return { orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '10000', postFee: '0' }; }, async changePrice() { throw Object.assign(new Error('price change rejected'), { retryable: false, code: 'CANNOT_MODIFY_FEE' }); } },
    },
  });
  const result = await workflow.processClaimed(record({ id: 'evt-terminal-price-notice', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(result.status, 'completed');
  assert.match(calls.find(([name]) => name === 'send')[1].text, /请先不要付款/);
  assert.equal(exceptions[0][2], 'CANNOT_MODIFY_FEE');
});

test('an order.price.changed event without a verified quote does not mark the chat waiting for payment', async () => {
  const stages = [];
  const { workflow } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async get() { return { facts: {} }; },
      async setOrderStage(...args) { stages.push(args); },
    },
    core: { im: { async getSessionByOrder() { return { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }; }, async listMessages() { return { items: [] }; } } },
  });
  await workflow.processClaimed(record({ id: 'evt-unverified-price-event', tenantId: 'tenant-1', event: 'order.price.changed', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(stages.length, 0);
});

test('an expired confirmed quote never changes a newly created order', async () => {
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: { async bindOrder() { return false; }, async get() { return { facts: { quote_confirmed: true, quote_total_cents: 8800, quote_ticket_count: 2, quote_expires_at: Date.now() - 1 } }; } },
    core: { im: { async getSessionByOrder() { return { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }; } }, orders: { async get() { throw new Error('expired quote must not read order'); } } },
  });
  await workflow.processClaimed(record({ id: 'evt-expired-order-created', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(calls.some(([name]) => name === 'change-price'), false);
});

test('a platform price-changed event rereads the order before sending the verified amount', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const stages = [];
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async get() { return { facts: { quote_confirmed: true, quote_total_cents: 13005, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 } }; },
      async setOrderStage(...args) { stages.push(args); },
    },
    core: {
      im: { async getSessionByOrder() { return contextPayload; }, async listMessages() { return { items: [] }; }, async sendMessage(input) { calls.push(['send', input]); return { messageId: 'price-changed-1' }; } },
      orders: { async get() { return { orderStatus: 1, payment: '13005', postFee: '0' }; } },
    },
  });
  await workflow.processClaimed(record({ id: 'evt-price-changed', tenantId: 'tenant-1', event: 'order.price.changed', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '价格已修改为130.05元，请核对后付款。订单付款后不支持退改签。');
  assert.equal(stages.at(-1)[2], 'waiting_payment');
});

test('a mismatched price-changed event enters manual review and never tells the buyer to pay', async () => {
  const exceptions = [];
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async get() { return { facts: { quote_confirmed: true, quote_total_cents: 13000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 } }; },
      async markOrderException(...args) { exceptions.push(args); },
    },
    core: {
      im: { async getSessionByOrder() { return { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }; }, async listMessages() { return { items: [] }; } },
      orders: { async get() { return { orderStatus: 1, payment: '9999', postFee: '0' }; } },
    },
  });
  await workflow.processClaimed(record({ id: 'evt-price-mismatch', tenantId: 'tenant-1', event: 'order.price.changed', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(calls.some(([name]) => name === 'send'), false);
  assert.equal(exceptions[0][2], 'price_changed_amount_mismatch');
});

test('a paid order with the verified amount tells the buyer the manual delivery window', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: { async setOrderStage() {}, async get() { return { facts: { quote_confirmed: true, quote_total_cents: 13000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 } }; } },
    core: { im: { async getSessionByOrder() { return contextPayload; }, async listMessages() { return { items: [] }; }, async sendMessage(input) { calls.push(['send', input]); return { messageId: 'paid-1' }; } }, orders: { async get() { return { paidAmount: 13000 }; } } },
  });
  await workflow.processClaimed(record({ id: 'evt-order-paid-match', tenantId: 'tenant-1', event: 'order.paid', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '已收到付款，请稍等人工出票。订单已付款，不会重新核价。');
});

test('a paid order with no plugin quote is ignored instead of creating a false exception', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  let orderReads = 0;
  let exceptions = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async get() { return { facts: {} }; },
      async markOrderException() { exceptions += 1; },
    },
    core: { im: { async getSessionByOrder() { return contextPayload; } }, orders: { async get() { orderReads += 1; return { paidAmount: 13000 }; } } },
  });
  const result = await workflow.processClaimed(record({ id: 'evt-order-paid-unmanaged', tenantId: 'tenant-1', event: 'order.paid', ts: Date.now(), payload: { orderId: 'order-external' } }));
  assert.equal(result.status, 'completed');
  assert.equal(orderReads, 0);
  assert.equal(exceptions, 0);
  assert.equal(calls.some(([name]) => name === 'send'), false);
  assert.equal(calls.find(([name]) => name === 'complete')[3].reason, 'plugin_quote_missing');
});

test('a paid order after a delivered unit quote is not misclassified as completely unmanaged', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const exceptions = [];
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async get() { return { facts: { quote_confirmed: false, quote_unit_cents: 12050, quote_expires_at: Date.now() + 60_000 } }; },
      async markOrderException(...args) { exceptions.push(args); },
    },
    core: { im: { async getSessionByOrder() { return contextPayload; }, async listMessages() { return { items: [] }; }, async sendMessage(input) { calls.push(['send', input]); return { messageId: 'paid-partial-quote-1' }; } }, orders: { async get() { return { paidAmount: 12050 }; } } },
  });
  await workflow.processClaimed(record({ id: 'evt-order-paid-partial-quote', tenantId: 'tenant-1', event: 'order.paid', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(exceptions[0][2], 'paid_quote_unconfirmed_or_expired');
  assert.match(calls.find(([name]) => name === 'send')[1].text, /未找到有效确认报价/u);
});

test('a paid order matching an unconfirmed quote still enters manual review', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const exceptions = [];
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async get() { return { facts: { quote_confirmed: false, quote_total_cents: 13000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 } }; },
      async markOrderException(...args) { exceptions.push(args); },
    },
    core: { im: { async getSessionByOrder() { return contextPayload; }, async listMessages() { return { items: [] }; }, async sendMessage(input) { calls.push(['send', input]); return { messageId: 'paid-unconfirmed-1' }; } }, orders: { async get() { return { paidAmount: 13000 }; } } },
  });
  await workflow.processClaimed(record({ id: 'evt-order-paid-unconfirmed', tenantId: 'tenant-1', event: 'order.paid', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '订单已付款，但未找到有效确认报价；请勿重复下单，联系人工处理。');
  assert.equal(exceptions[0][2], 'paid_quote_unconfirmed_or_expired');
});

test('a paid order with a mismatched verified quote gets a safe warning and enters manual review', async () => {
  const contextPayload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
  const exceptions = [];
  const { workflow, calls } = harness({
    quotePreviewClient: {},
    conversationContextStore: {
      async get() { return { facts: { quote_confirmed: true, quote_total_cents: 8800, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 } }; },
      async markOrderException(...args) { exceptions.push(args); },
    },
    core: { im: { async getSessionByOrder() { return contextPayload; }, async listMessages() { return { items: [] }; }, async sendMessage(input) { calls.push(['send', input]); return { messageId: 'sent-1' }; } }, orders: { async get() { return { paidAmount: 10000 }; } } },
  });
  await workflow.processClaimed(record({ id: 'evt-order-paid-mismatch', tenantId: 'tenant-1', event: 'order.paid', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.match(calls.find(([name]) => name === 'send')[1].text, /金额与本次核验报价不一致/);
  assert.equal(exceptions[0][2], 'paid_amount_mismatch');
});

test('a casual seat-position remark does not reuse an image as a new quote task', async () => {
  const previews = [];
  const receivedAt = Date.now() - 60_000;
  const { workflow } = harness({
    quotePreviewClient: { async capture(envelope) { previews.push(envelope); return { status: 'needs_confirmation' }; } },
    conversationContextStore: {
      async get() { return { messages: [{ at: receivedAt - 10_000, image_urls: ['https://img.alicdn.com/seat.png'] }] }; },
    },
  });
  await workflow.processClaimed(record({
    id: 'evt-delayed-supplement', tenantId: 'tenant-1', event: 'im.message.received', ts: receivedAt,
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '中间这个座位' },
  }));
  assert.equal(previews[0].payload.imageUrls, undefined);
});

test('a confirmed seat-image round can reuse its image after the short merge window', async () => {
  const previews = [];
  const receivedAt = Date.now();
  const imageUrl = 'https://img.alicdn.com/seat.png';
  const { workflow } = harness({
    quotePreviewClient: { async capture(envelope) { previews.push(envelope); return { status: 'preview_ready' }; } },
    conversationContextStore: {
      async get() { return { facts: { confirmation_image_url: imageUrl }, messages: [{ at: receivedAt - 60_000, image_urls: [imageUrl] }] }; },
    },
  });
  await workflow.processClaimed(record({
    id: 'evt-confirmed-supplement', tenantId: 'tenant-1', event: 'im.message.received', ts: receivedAt,
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '确认，两张' },
  }));
  assert.deepEqual(previews[0].payload.imageUrls, [imageUrl]);
});

test('an order-linked chat does not re-enter quote preview or receive first-contact guidance', async () => {
  const previews = [];
  let noticeClaims = 0;
  const { workflow, calls } = harness({
    autoReplyEnabled: true,
    conversationContextStore: {
      async get() { return { facts: {}, messages: [] }; },
      async claimFirstContactNotice() { noticeClaims += 1; return true; },
    },
    core: { im: {
      async listSessions() { return { items: [{ chatId: 'chat-1', accountUnb: 'shop-1', peerUnb: 'buyer-1', orderId: 'order-1' }] }; },
      async listMessages() { return { items: [] }; },
      async sendMessage() { return { messageId: 'sent-1' }; },
    } },
    quotePreviewClient: { async capture(envelope) { previews.push(envelope); return { status: 'preview_ready', reply_text: 'must not quote' }; } },
  });
  await workflow.processClaimed(record({
    id: 'evt-order-linked-image', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] },
  }));
  assert.deepEqual(previews, []);
  assert.equal(noticeClaims, 0);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('an order-linked purchase message cannot repeat the submit-order prompt after a price-change failure', async () => {
  const { workflow, calls } = harness({
    autoReplyEnabled: true,
    conversationContextStore: {
      async get() { return { facts: { order_id: 'order-1', stage: 'exception_review', quote_total_cents: 7400, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000, quote_confirmed: true }, messages: [] }; },
    },
    core: {
      im: { async listMessages() { return { items: [] }; } },
      orders: { async get() { return { orderStatus: 1 }; } },
    },
    quotePreviewClient: { async capture() { throw new Error('order-linked chat must not quote'); } },
    replyPreviewClient: { async capture() { throw new Error('order-linked chat must not generate another reply'); } },
  });
  await workflow.processClaimed(record({
    id: 'evt-order-linked-purchase-repeat', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', orderId: 'order-1', content: '我已拍下，待付款' },
  }));
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('a completed or closed historical order does not block a new image quote', async () => {
  const previews = [];
  const { workflow } = harness({
    core: {
      im: {
        async listSessions() { return { items: [{ chatId: 'chat-1', orderId: 'historical-order' }] }; },
        async listMessages() { return { items: [] }; },
      },
      orders: { async get() { return { orderStatus: 4, orderStatusText: '交易成功' }; } },
    },
    quotePreviewClient: { async capture(envelope) { previews.push(envelope); return { status: 'preview_ready' }; } },
  });
  await workflow.processClaimed(record({
    id: 'evt-after-completed-order', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/new.png'] },
  }));
  assert.equal(previews.length, 1);
});

test('a terse price question reuses the just-received seat image and sends combined context to realtime quote', async () => {
  const previews = [];
  const { workflow } = harness({
    quotePreviewClient: { async capture(envelope) { previews.push(envelope); return { status: 'preview_ready' }; } },
    conversationContextStore: {
      async get() {
        return {
          facts: { ticket_count: 3 },
          messages: [
            { at: Date.now() - 8_000, role: 'buyer', text: '', image_urls: ['https://img.alicdn.com/seat.png'] },
            { at: Date.now(), role: 'buyer', text: '多少呢', image_urls: [] },
          ],
        };
      },
    },
  });
  await workflow.processClaimed(record({
    id: 'evt-supplement', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '多少呢' },
  }));
  assert.equal(previews.length, 1);
  assert.deepEqual(previews[0].payload.imageUrls, ['https://img.alicdn.com/seat.png']);
  assert.match(previews[0].payload.content, /多少呢/u);
});

test('a disabled shop blocks quote preview capture and automatic quote replies', async () => {
  const previews = [];
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, shop_enabled: false },
    quotePreviewClient: {
      async capture(envelope) {
        previews.push(envelope);
        return { status: 'preview_ready', reply_text: 'Verified quote' };
      },
    },
    autoReplyEnabled: true,
  });
  const envelope = {
    id: 'evt-preview-disabled-shop', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.status, 'completed');
  assert.deepEqual(previews, []);
  assert.equal(calls.some(([name]) => name === 'send'), false);
  const completed = calls.find(([name]) => name === 'complete');
  assert.equal(completed.at(-1).skipped, 'shop_automation_disabled');
});

test('reply preview reads the most recent conversation and never sends a buyer message', async () => {
  const previews = [];
  const { workflow, calls } = harness({
    backend: { async upsertOrder() { throw new Error('legacy backend must not run in preview mode'); } },
    core: {
      im: {
        async listMessages() {
          return { items: [
            { direction: 'inbound', content: '两张还有吗', sentAt: new Date().toISOString() },
            { direction: 'outbound', content: '您好，请问需要几张？', sentAt: new Date().toISOString() },
          ] };
        },
      },
    },
    replyPreviewClient: { async capture(envelope, history) { previews.push({ envelope, history }); return { status: 'preview_ready' }; } },
  });
  const envelope = {
    id: 'evt-reply-1', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '两张还有吗' },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.mode, 'quote_preview_only');
  assert.deepEqual(previews[0].history.map((item) => [item.role, item.content]), [['seller', '您好，请问需要几张？'], ['buyer', '两张还有吗']]);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('reply preview uses the configured bounded conversation memory depth', async () => {
  const previews = [];
  const { workflow } = harness({
    runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, ai_reply_memory_depth: 5, ai_reply_memory_hours: 24 },
    core: { im: { async listMessages(input) {
      assert.equal(input.pageSize, 5);
      return { items: [
        { direction: 'inbound', content: '最新', sentAt: new Date().toISOString() },
        { direction: 'outbound', content: '上一条', sentAt: new Date(Date.now() - 60_000).toISOString() },
        { direction: 'inbound', content: '不应进入上下文', sentAt: new Date(Date.now() - 120_000).toISOString() },
      ] };
    } } },
    replyPreviewClient: { async capture(_envelope, history) { previews.push(history); return { status: 'preview_ready' }; } },
  });
  await workflow.processClaimed(record({
    id: 'evt-memory-depth', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '最新' },
  }));
  assert.deepEqual(previews[0].map((item) => item.content), ['不应进入上下文', '上一条', '最新']);
});

test('a newly verified image quote may follow this plugin’s earlier failure reply', async () => {
  const { workflow, calls } = harness({
    eventStore: { async wasSentMessage() { return true; } },
    core: { im: {
      async listMessages() { return { items: [{ direction: 'outbound', messageId: 'prior-plugin-failure' }] }; },
      async sendMessage(input) { calls.push(['send', input]); return { messageId: 'verified-after-failure' }; },
    } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; }, async recordQuoteDraft() {}, async claimQuoteDraftAttempt() { return true; }, async markQuoted() {} },
    quotePreviewClient: {
      async recognize() { return { status: 'recognized', recognition: { cinema: '测试万达', movie: '测试电影', date: '2026-08-22', showtime: '10:00', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }; },
      async quote() { return { status: 'preview_ready', reply_text: '本轮成功报价50元。', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1 }; },
    },
    autoReplyEnabled: true,
  });
  const result = await workflow.processClaimed(record({ id: 'evt-verified-after-failure', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] } }));
  assert.equal(result.actions[0].status, 'succeeded');
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '本轮成功报价50元。\n接受本次报价请回复“确认”。');
});

test('a verified text quote may follow the plugin’s earlier generic reply with its realtime result', async () => {
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async recognize() { return { status: 'recognized', text_quote: true, tenant_id: 'tenant-1', ticket_count: 2, recognition: { image_type: 'UNKNOWN' } }; },
      async quote() { return { status: 'preview_ready', text_quote: true, reply_text: '实时核价：61.6元/张，2张合计123.3元。' }; },
    },
    eventStore: { async wasSentMessage() { return true; } },
    core: { im: {
      async listMessages() { return { items: [{ direction: 'outbound', messageId: 'prior-plugin-message' }] }; },
      async sendMessage(input) { calls.push(['send', input]); return { messageId: 'quote-message' }; },
    } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-text-quote-follow-up', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '8月22日 上海万达 16:25 奥德赛 2张' },
  }));
  assert.equal(calls.filter(([name]) => name === 'send').length, 1);
  assert.match(calls.find(([name]) => name === 'send')[1].text, /实时核价/u);
});

test('a bounded missing-facts reply may follow this plugin’s earlier first-contact message', async () => {
  const { workflow, calls } = harness({
    eventStore: { async wasSentMessage() { return true; } },
    core: { im: {
      async listMessages() { return { items: [{ direction: 'outbound', messageId: 'prior-first-contact' }] }; },
      async sendMessage(input) { calls.push(['send', input]); return { messageId: 'missing-facts-reply' }; },
    } },
    quotePreviewClient: {
      async recognize() { return { status: 'needs_confirmation', failure_code: 'text_quote_missing_fields', reply_text: '还缺完整选座图。' }; },
      async quote() { throw new Error('must not quote incomplete text'); },
    },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; }, async claimFirstContactNotice() { return false; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-missing-after-first-contact', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '广州万达天河智慧城店' } }));
  assert.equal(calls.filter(([name]) => name === 'send').length, 1);
  assert.match(calls.find(([name]) => name === 'send')[1].text, /完整选座图/u);
});

test('incomplete text quote sends its missing-facts reply without image confirmation dedupe', async () => {
  let claims = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: { async recognize() { return { status: 'needs_confirmation', reply_text: '还缺：完整万达影院名、开场时间。' }; }, async quote() { throw new Error('must not quote incomplete text'); } },
    conversationContextStore: { async get() { return { messages: [] }; }, async claimConfirmation() { claims += 1; return false; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-text-missing', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '两张多少钱？' },
  }));
  assert.equal(claims, 0);
  assert.equal(calls.filter(([name]) => name === 'send').length, 1);
  assert.match(calls.find(([name]) => name === 'send')[1].text, /还缺/u);
});

test('confirmation fallback is sent only once for the same image', async () => {
  let claims = 0;
  const { workflow, calls } = harness({
    quotePreviewClient: { async capture() { return { status: 'needs_confirmation', reply_text: '请确认圈选和张数' }; } },
    conversationContextStore: { async get() { return { messages: [] }; }, async claimConfirmation() { claims += 1; return claims === 1; } },
    autoReplyEnabled: true,
  });
  const payload = { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] };
  await workflow.processClaimed(record({ id: 'evt-confirm-1', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload }));
  await workflow.processClaimed(record({ id: 'evt-confirm-2', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload }));
  assert.equal(claims, 2);
  assert.equal(calls.filter(([name]) => name === 'send').length, 1);
});

test('order lifecycle events do not mark an unverified paid order for manual delivery', async () => {
  const calls = [];
  const context = {
    async bindOrder(...args) { calls.push(['bind', ...args]); return true; },
    async setOrderStage(...args) { calls.push(['stage', ...args]); },
  };
  const core = { im: {
    async getSessionByOrder() { return { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }; },
    async listSessions() { return { items: [] }; }, async listMessages() { return { items: [] }; }, async sendMessage() { return { messageId: 'sent-1' }; },
  } };
  const { workflow } = harness({ conversationContextStore: context, core, quotePreviewClient: { async capture() { throw new Error('not used for order events'); } } });
  await workflow.processClaimed(record({ id: 'evt-order-created', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(), payload: { orderId: 'order-1' } }));
  await workflow.processClaimed(record({ id: 'evt-order-paid', tenantId: 'tenant-1', event: 'order.paid', ts: Date.now(), payload: { orderId: 'order-1' } }));
  assert.deepEqual(calls.map(([kind, , , stage]) => [kind, stage]), [['bind', 'order-1']]);
});

test('a verified quote is persisted as the current conversation quote state', async () => {
  const marks = [];
  const { workflow } = harness({
    quotePreviewClient: { async capture() { return { status: 'preview_ready', reply_text: 'Verified quote' }; } },
    conversationContextStore: { async get() { return { messages: [] }; }, async markQuoted(...args) { marks.push(args); } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({
    id: 'evt-quote-state', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] },
  }));
  assert.equal(marks.length, 1);
});

test('an undelivered quote does not replace the current conversation quote state', async () => {
  const marks = [];
  const { workflow } = harness({
    quotePreviewClient: { async capture() { return { status: 'preview_ready', reply_text: 'Verified quote' }; } },
    conversationContextStore: { async get() { return { messages: [] }; }, async markQuoted(...args) { marks.push(args); } },
    autoReplyEnabled: false,
  });
  await workflow.processClaimed(record({
    id: 'evt-undelivered-quote', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] },
  }));
  assert.equal(marks.length, 0);
});

test('verified quote replies are sent only when automatic reply is enabled', async () => {
  const { workflow, calls } = harness({
    backend: { async upsertOrder() { throw new Error('legacy backend must not run in preview mode'); } },
    quotePreviewClient: { async capture() { return { status: 'preview_ready', reply_text: 'Verified quote' }; } },
    autoReplyEnabled: true,
  });
  const envelope = {
    id: 'evt-auto-quote', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.actions[0].status, 'succeeded');
  assert.equal(calls.find(([name]) => name === 'send')[1].text, 'Verified quote');
});

test('a safe supplement reply may auto-send below the legacy 0.90 self-score cutoff', async () => {
  const { workflow, calls } = harness({
    replyPreviewClient: { async capture() { return { status: 'preview_ready', autoSend: true, draft: { intent: '补充信息', confidence: 0.85, needs_human: false, reply: '请补充影片和场次，我再帮您核价。' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-low-score-supplement', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '合肥天鹅湖万达' } }));
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '请补充影片和场次，我再帮您核价。');
});

test('a bare count completes an active unit quote and tells the buyer to submit unpaid even after the prior plugin quote', async () => {
  const marked = [];
  const confirmed = [];
  const { workflow, calls } = harness({
    eventStore: { async wasSentMessage() { return true; } },
    core: { im: {
      async listMessages() { return { items: [{ direction: 'outbound', messageId: 'prior-unit-quote' }] }; },
      async sendMessage(input) { calls.push(['send', input]); return { messageId: 'sent-1' }; },
    } },
    conversationContextStore: {
      async get() { return { facts: { quote_expires_at: Date.now() + 60_000, quote_unit_cents: 5500, cinema: '重庆南坪万达广场店', stage: 'quoted', pricing_rule_version: 'policy-v1', quote_reply_delivered: true }, messages: [] }; },
      async markQuoted(...args) { marked.push(args); },
      async markQuoteConfirmed(...args) { confirmed.push(args); },
    },
    replyPreviewClient: { async capture() { throw new Error('a deterministic count completion must not call the model'); } },
    autoReplyEnabled: true,
  });
  const result = await workflow.processClaimed(record({ id: 'evt-unit-quote-count', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '2' } }));
  const sent = calls.find(([name]) => name === 'send')[1].text;
  assert.match(sent, /55\.00元\/张/);
  assert.match(sent, /2张合计110\.00元/);
  assert.match(sent, /请提交订单后先不要付款/);
  assert.match(sent, /等待系统改价/);
  assert.doesNotMatch(sent, /请回复“确认”/);
  assert.equal(result.actions[0].status, 'succeeded');
  assert.equal(marked.length, 1);
  assert.equal(confirmed.length, 1);
  assert.deepEqual(marked[0][2], { unitQuoteCents: 5500, totalQuoteCents: 11000, ticketCount: 2, cinema: '重庆南坪万达广场店', pricingRuleVersion: 'policy-v1', replyDelivered: true });
});

test('a price question replays a delivered unit quote without vision or another temporary probe', async () => {
  const { workflow, calls } = harness({
    quotePreviewClient: {
      async recognize() { throw new Error('unit quote question must not re-recognize'); },
      async quote() { throw new Error('unit quote question must not re-quote'); },
    },
    conversationContextStore: {
      async get() { return { facts: { quote_expires_at: Date.now() + 60_000, quote_unit_cents: 5500, cinema: '重庆南坪万达广场店', stage: 'quoted', pricing_rule_version: 'policy-v1', quote_reply_delivered: true }, messages: [] }; },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-unit-quote-question', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '你这多少' } }));
  const sent = calls.find(([name]) => name === 'send')[1].text;
  assert.match(sent, /55\.00元\/张/u);
  assert.match(sent, /请告诉我需要几张/u);
  assert.equal(calls.find(([name]) => name === 'complete').at(-1).quote_skipped, 'unit_quote_replayed');
});

test('a slow realtime quote sends one bounded progress notice before its authoritative result', async () => {
  const { workflow, calls } = harness({
    quoteProgressNoticeDelayMs: 5,
    quotePreviewClient: {
      async recognize() {
        await new Promise((resolve) => setTimeout(resolve, 12));
        return { status: 'recognized', tenant_id: 'tenant-1', ticket_count: 1, recognition: { image_type: 'SEAT_MAP', cinema: '测试万达影城' } };
      },
      async quote() { return { status: 'preview_ready', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1, reply_text: '权威实时报价50.00元。', timings_ms: { account: 1, match: 20, realtime_seats: 30, locked_offer: 40, calculate_quote: 1, total: 92 } }; },
    },
    conversationContextStore: {
      async get() { return { facts: {}, messages: [{ at: Date.now(), image_urls: ['https://img.alicdn.com/seat.png'] }] }; },
      async claimFirstContactNotice() { return false; }, async recordQuoteDraft() {}, async claimQuoteDraftAttempt() { return true; }, async markQuoted() {},
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-slow-quote-progress', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] } }));
  const sent = calls.filter(([name]) => name === 'send').map(([, input]) => input.text);
  assert.equal(sent.length, 2);
  assert.match(sent[0], /正在按当前信息核对万达实时场次和优惠/u);
  assert.match(sent[1], /权威实时报价50\.00元/u);
  assert.deepEqual(calls.find(([name]) => name === 'complete').at(-1).quote_stage_timings_ms, { account: 1, match: 20, realtime_seats: 30, locked_offer: 40, calculate_quote: 1, total: 92 });
});

test('a slow duplicate draft closes a previously sent progress notice with a safe final reply', async () => {
  let quoteCalls = 0;
  const { workflow, calls } = harness({
    quoteProgressNoticeDelayMs: 5,
    quotePreviewClient: {
      async recognize() {
        await new Promise((resolve) => setTimeout(resolve, 12));
        return { status: 'recognized', tenant_id: 'tenant-1', ticket_count: 1, recognition: { image_type: 'SEAT_MAP', cinema: '测试万达影城' } };
      },
      async quote() { quoteCalls += 1; throw new Error('duplicate draft must not quote'); },
    },
    conversationContextStore: {
      async get() { return { facts: {}, messages: [{ at: Date.now(), image_urls: ['https://img.alicdn.com/seat.png'] }] }; },
      async claimFirstContactNotice() { return false; }, async recordQuoteDraft() {}, async claimQuoteDraftAttempt() { return false; },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-slow-duplicate-progress', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/seat.png'] } }));
  const sent = calls.filter(([name]) => name === 'send').map(([, input]) => input.text);
  assert.equal(quoteCalls, 0);
  assert.equal(sent.length, 2);
  assert.match(sent[0], /正在按当前信息核对万达实时场次和优惠/u);
  assert.match(sent[1], /当前信息与上一轮一致/u);
  assert.match(sent[1], /最新完整选座图/u);
  const completed = calls.find(([name]) => name === 'complete').at(-1);
  assert.equal(completed.quote_skipped, 'duplicate_quote_draft');
  assert.equal(completed.agent_reply_snapshot.kind, 'conversation_follow_up');
  assert.equal(completed.agent_reply_snapshot.text, sent[1]);
});

test('an unverified AI reply cannot invent a circled or marked seat', async () => {
  const { workflow, calls } = harness({
    replyPreviewClient: { async capture() { return { status: 'preview_ready', autoSend: true, draft: { intent: '选座核价', confidence: 0.85, needs_human: false, reply: '看到您圈的位置了，请问需要几张？确认后我按您标记的位置帮您核价。' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-seat-question', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '灰色区域' } }));
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('an AI reply cannot claim it is locking seats or generating an order price', async () => {
  const { workflow, calls } = harness({
    replyPreviewClient: { async capture() { return { status: 'preview_ready', autoSend: true, draft: { intent: '选座核价', confidence: 0.95, needs_human: false, reply: '收到，正在为您锁定该场次座位并生成最终订单价格，请稍等。' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-false-lock-claim', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '2' } }));
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('an AI reply cannot promise that an unstarted realtime price check is in progress', async () => {
  const { workflow, calls } = harness({
    replyPreviewClient: { async capture() { return { status: 'preview_ready', autoSend: true, draft: { intent: '票价咨询', confidence: 0.95, needs_human: false, reply: '好的，正在为您实时核对7排17、18的票价和库存，稍后直接报给您。' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-false-price-check', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '帮我查一下' } }));
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('seat-selection drafts that claim availability remain blocked from auto-send', async () => {
  const { workflow, calls } = harness({
    replyPreviewClient: { async capture() { return { status: 'preview_ready', autoSend: true, draft: { intent: '选座核价', confidence: 0.95, needs_human: false, reply: '这个位置能买，还有票。' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-seat-availability-claim', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: 'W位置能买吗' } }));
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('an AI reply cannot invite ordering or promise a later price change without an active complete quote', async () => {
  const { workflow, calls } = harness({
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    replyPreviewClient: { async capture() { return {
      status: 'preview_ready', autoSend: true,
      draft: { intent: '票价咨询', confidence: 0.95, needs_human: false, reply: '您先拍下订单但暂时不要付款，我核实后按实时票价改价。' },
    }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-unverified-order-flow', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '我拍你改价吗' } }));
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('an AI order-flow reply remains allowed while a delivered complete quote is active', async () => {
  const { workflow, calls } = harness({
    conversationContextStore: { async get() { return { facts: { quote_expires_at: Date.now() + 60_000, quote_total_cents: 10_000, quote_ticket_count: 2 }, messages: [] }; } },
    replyPreviewClient: { async capture() { return {
      status: 'preview_ready', autoSend: true,
      draft: { intent: '票价咨询', confidence: 0.95, needs_human: false, reply: '请提交订单并保持待付款，改价完成后再付款。' },
    }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-verified-order-flow', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '我拍你改价吗' } }));
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '请提交订单并保持待付款，改价完成后再付款。');
});

test('model reply requires the safe gate before it can be sent', async () => {
  const { workflow, calls } = harness({
    backend: { async upsertOrder() { throw new Error('legacy backend must not run in preview mode'); } },
    replyPreviewClient: {
      async capture() {
        return {
          status: 'preview_ready',
          autoSend: true,
          draft: { intent: '\u7968\u4ef7\u54a8\u8be2', confidence: 0.95, needs_human: false, reply: 'Safe model reply' },
        };
      },
    },
    autoReplyEnabled: true,
  });
  const envelope = {
    id: 'evt-auto-model', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: 'Price please' },
  };
  await workflow.processClaimed(record(envelope));
  assert.equal(calls.find(([name]) => name === 'send')[1].text, 'Safe model reply');
});

test('durable shadow scheduling never waits for model planning inside the business event lease', async () => {
  const scheduled = [];
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, recognition_enabled: true, quote_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'shadow' },
    shadowAgentScheduler: { async schedule(envelope) { scheduled.push(envelope.id); } },
    conversationAgentPlanner: { async plan() { throw new Error('inline shadow planning must be disabled'); } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; }, async recordQuoteDraft() {}, async claimQuoteDraftAttempt() { return true; }, async markQuoted() {} },
    quotePreviewClient: {
      async recognize() { return { status: 'recognized', recognition: { cinema: '测试万达', movie: '测试电影', date: '2026-08-22', showtime: '10:00', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }; },
      async quote() { return { status: 'preview_ready', reply_text: '权威报价及时送达。', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1 }; },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-durable-shadow', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] } }));
  assert.deepEqual(scheduled, ['evt-durable-shadow']);
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '权威报价及时送达。\n接受本次报价请回复“确认”。');
});

test('durable active owner schedules a low-risk turn and completes the business lease without inline planning', async () => {
  const scheduled = [];
  const eventId = selectedCanaryEventId('evt-durable-active');
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'active', execution_owner: 'agent', ...approvedCanarySettings },
    shadowAgentScheduler: { async schedule(envelope, options) { scheduled.push([envelope.id, options]); } },
    conversationAgentPlanner: { async plan() { throw new Error('active planning must run only in the durable agent worker'); } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    replyPreviewClient: { async capture() { throw new Error('legacy reply must not run for an agent-owned turn'); } },
    autoReplyEnabled: true,
  });
  const result = await workflow.processClaimed(record({ id: eventId, tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '图片怎么发' } }));
  assert.equal(result.status, 'completed');
  assert.deepEqual(scheduled, [[eventId, { mode: 'active' }]]);
  assert.equal(calls.some(([name]) => name === 'send'), false);
  const completion = calls.find(([name]) => name === 'complete')[3];
  assert.equal(completion.execution_owner, 'agent');
  assert.equal(completion.agent_run_scheduled, true);
});

test('durable active owner may schedule an order-status turn only when a linked unpaid order exists', async () => {
  const scheduled = [];
  const eventId = selectedCanaryEventId('evt-active-order-read');
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'active', execution_owner: 'agent', ...approvedCanarySettings },
    shadowAgentScheduler: { async schedule(envelope, options) { scheduled.push([envelope.id, options]); } },
    conversationAgentPlanner: { async plan() { throw new Error('must run in agent worker'); } },
    conversationContextStore: { async get() { return { facts: { order_id: 'system-linked-order', stage: 'waiting_payment' }, messages: [] }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: eventId, tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '订单付款了吗' } }));
  assert.deepEqual(scheduled, [[eventId, { mode: 'active' }]]);
  assert.equal(calls.some(([name]) => name === 'send'), false);
  assert.equal(calls.find(([name]) => name === 'complete')[3].execution_owner, 'agent');
});

test('durable active canary defaults to zero and keeps the deterministic owner', async () => {
  const scheduled = [];
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'active', execution_owner: 'agent' },
    shadowAgentScheduler: { async schedule(envelope, options) { scheduled.push([envelope.id, options ?? null]); } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    replyPreviewClient: { async capture() { return { status: 'preview_ready', autoSend: true, draft: { intent: '其他', confidence: 1, needs_human: false, reply: '确定性链路继续回复' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-canary-default-zero', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '图片怎么发' } }));
  assert.deepEqual(scheduled, [['evt-canary-default-zero', null]]);
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '确定性链路继续回复');
  assert.equal(calls.find(([name]) => name === 'complete')[3].execution_owner, 'deterministic');
});

test('shadow history lookup failure cannot crash or block the deterministic quote path', async () => {
  let historyCalls = 0;
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, recognition_enabled: true, quote_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'shadow' },
    core: { im: { async listMessages() { historyCalls += 1; if (historyCalls === 1) { const error = new Error('session missing'); error.status = 404; throw error; } return { items: [{ direction: 'inbound', content: '[图片]' }] }; }, async sendMessage(input) { calls.push(['send', input]); return { messageId: 'safe-send' }; } } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; }, async recordQuoteDraft() {}, async claimQuoteDraftAttempt() { return true; }, async markQuoted() {} },
    conversationAgentPlanner: { async plan() { throw new Error('planner must not run without history'); } },
    quotePreviewClient: {
      async recognize() { return { status: 'recognized', recognition: { cinema: '测试万达', movie: '测试电影', date: '2026-08-22', showtime: '10:00', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }; },
      async quote() { return { status: 'preview_ready', reply_text: '权威报价安全送达。', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1 }; },
    },
    autoReplyEnabled: true,
  });
  const result = await workflow.processClaimed(record({ id: 'evt-shadow-history-404', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'missing-chat', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] } }));
  assert.equal(result.status, 'completed');
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '权威报价安全送达。\n接受本次报价请回复“确认”。');
});

test('shadow agent evaluates verified quote turns without changing the authoritative reply', async () => {
  let agentCalls = 0;
  const recorded = [];
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, recognition_enabled: true, quote_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'shadow' },
    conversationContextStore: {
      async get() { return { facts: {}, messages: [] }; },
      async recordQuoteDraft() {}, async claimQuoteDraftAttempt() { return true; }, async markQuoted() {},
      async recordAgentTurn(...args) { recorded.push(args); },
    },
    conversationAgentPlanner: { async plan(input) {
      agentCalls += 1;
      const actions = ['recognize_image', 'resolve_showtime', 'quote_realtime', 'respond'];
      return { intent: '选座核价', confidence: 0.97, goal: '按权威结果推进', action: actions[input.observations?.length ?? 0], arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '根据工具观察继续' };
    } },
    quotePreviewClient: {
      async recognize() { return { status: 'recognized', recognition: { cinema: '测试万达', movie: '测试电影', date: '2026-08-22', showtime: '10:00', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }; },
      async quote() { return { status: 'preview_ready', reply_text: '权威报价：50.00元/张。', unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1 }; },
    },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-agent-shadow-quote', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] } }));
  assert.equal(agentCalls, 1);
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '权威报价：50.00元/张。\n接受本次报价请回复“确认”。');
  assert.equal(recorded[0][2].action, 'recognize_image');
});

test('shadow agent records a generalized external-seller conversation experience as a disabled draft', async () => {
  const experiences = [];
  const now = new Date().toISOString();
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'shadow', ai_reply_memory_depth: 20, ai_reply_memory_hours: 24 },
    eventStore: { async wasSentMessage(_tenantId, _chatId, messageId) { return messageId === 'plugin-message'; } },
    core: { im: {
      async listMessages() { return { items: [
        { messageId: 'buyer-current', direction: 'inbound', content: '好的我现在发', sentAt: now },
        { messageId: 'human-message', direction: 'outbound', content: '请发送完整选座页截图并说明张数。', sentAt: now },
        { messageId: 'buyer-question', direction: 'inbound', content: '需要发什么', sentAt: now },
      ] }; },
    } },
    backend: {
      async recordConversationExperience(input, context) { experiences.push({ input, context }); return { status: 'draft_created', entry: { id: 'draft-1' } }; },
    },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; }, async recordAgentTurn() {} },
    conversationAgentPlanner: { async plan(input) {
      assert.equal(input.state.messages.find((item) => item.role === 'seller').source, 'external_seller');
      return {
        intent: '其他', confidence: 0.92, goal: '结束本轮', action: 'wait', arguments: {}, missing_fields: [], reply: '', needs_human: false, reason: '买家已理解',
        experience_candidate: {
          topic: '图片要求', question_pattern: '买家询问核价前需要提供什么资料',
          response_guidance: '说明需要完整选座页截图和明确张数。', example_reply: '请发送完整选座页截图并说明需要的张数。',
          outcome_signal: 'buyer_progressed', confidence: 0.91,
        },
      };
    } },
    replyPreviewClient: { async capture() { return null; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-experience', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '好的我现在发' } }));
  assert.equal(experiences.length, 1);
  assert.equal(experiences[0].input.candidate.topic, '图片要求');
  assert.deepEqual(experiences[0].context, { tenantId: 'tenant-1', eventId: 'evt-experience:conversation-experience' });
  const completion = calls.find(([name]) => name === 'complete')[3];
  assert.equal(completion.conversation_experience_status, 'draft_created');
  assert.equal(JSON.stringify(completion).includes('example_reply'), false);
});

test('active conversation agent owns safe free-form customer-service replies', async () => {
  let legacyCalls = 0;
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'active', execution_owner: 'agent' },
    conversationContextStore: { async get() { return { facts: {}, messages: [{ role: 'buyer', content: '怎么购买' }] }; } },
    conversationAgentPlanner: { async plan() { return { intent: '其他', confidence: 0.92, goal: '说明流程', action: 'respond', arguments: {}, missing_fields: [], reply: '请先发送完整选座页截图，我会根据实时结果继续协助。', needs_human: false, reason: '普通流程咨询' }; } },
    replyPreviewClient: { async capture() { legacyCalls += 1; return { status: 'preview_ready', autoSend: true, draft: { intent: '其他', confidence: 1, needs_human: false, reply: 'legacy' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-agent-active', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '怎么购买' } }));
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '请先发送完整选座页截图，我会根据实时结果继续协助。');
  assert.equal(legacyCalls, 0);
});

test('active create_manual_task derives a persistent task from system context without model identifiers', async () => {
  const tasks = [];
  let plans = 0;
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'active', execution_owner: 'agent' },
    conversationContextStore: { async get() { return { facts: { order_id: 'order-from-system' }, messages: [] }; } },
    manualTaskStore: { async create(input) { tasks.push(input); return { created: true, task: { status: 'open' } }; } },
    conversationAgentPlanner: { async plan() {
      plans += 1;
      return plans === 1
        ? { intent: '人工接管', confidence: 0.98, goal: '转人工', action: 'create_manual_task', arguments: { note: '模型不得提供订单号' }, missing_fields: [], reply: '', needs_human: true, reason: '需要人工处理' }
        : { intent: '人工接管', confidence: 0.98, goal: '告知买家', action: 'respond', arguments: {}, missing_fields: [], reply: '模型自拟回复', needs_human: false, reason: '任务已创建' };
    } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-agent-manual', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '这个需要人工看看' } }));
  assert.equal(tasks.length, 1);
  assert.deepEqual(tasks[0], {
    taskId: 'agent:tenant-1:evt-agent-manual:manual', tenantId: 'tenant-1', eventId: 'evt-agent-manual',
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', orderId: 'order-from-system',
    reasonCode: 'agent_requested_manual_review', summary: 'Agent请求人工处理', source: 'agent',
  });
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '这个问题需要人工进一步确认，已记录处理，请稍候。');
});

test('conversation agent receives bounded buyer and seller platform history', async () => {
  let plannedInput;
  const sent = [];
  const { workflow } = harness({
    runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'active', execution_owner: 'agent', ai_reply_memory_depth: 20, ai_reply_memory_hours: 24 },
    eventStore: { async wasSentMessage() { return true; } },
    core: { im: {
      async listMessages() { return { items: [{ messageId: 'buyer-history', direction: 'inbound', content: '图片怎么发', sentAt: new Date().toISOString() }, { messageId: 'plugin-history', direction: 'outbound', content: '请发送选座图', sentAt: new Date().toISOString() }] }; },
      async sendMessage(input) { sent.push(input); return { messageId: 'agent-history-sent' }; },
    } },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    conversationAgentPlanner: { async plan(input) { plannedInput = input; return { intent: '补充信息', confidence: 0.9, goal: '继续收集', action: 'ask_for_image', arguments: {}, missing_fields: ['选座图'], reply: '好的，请继续发送选座图。', needs_human: false, reason: '沿用上轮问题' }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-agent-history', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '图片怎么发' } }));
  assert.deepEqual(plannedInput.state.messages.map(({ role, content, source }) => ({ role, content, source })), [
    { role: 'seller', content: '请发送选座图', source: 'plugin' },
    { role: 'buyer', content: '图片怎么发', source: 'buyer' },
  ]);
  assert.equal(sent[0].text, '好的，请继续发送选座图。');
});

test('shadow conversation agent cannot change the existing buyer reply', async () => {
  let agentCalls = 0;
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'shadow' },
    conversationContextStore: { async get() { return { facts: {}, messages: [{ role: 'buyer', content: '怎么购买' }] }; } },
    conversationAgentPlanner: { async plan() { agentCalls += 1; return { intent: '其他', confidence: 0.9, goal: '回答', action: 'respond', arguments: {}, missing_fields: [], reply: 'agent-shadow', needs_human: false, reason: 'shadow' }; } },
    replyPreviewClient: { async capture() { return { status: 'preview_ready', autoSend: true, draft: { intent: '其他', confidence: 0.9, needs_human: false, reply: 'legacy-safe-reply' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-agent-shadow', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '怎么购买' } }));
  assert.equal(agentCalls, 1);
  assert.equal(calls.find(([name]) => name === 'send')[1].text, 'legacy-safe-reply');
});

test('active mode without execution_owner agent is downgraded to shadow deterministically', async () => {
  let scheduled = 0;
  let activePlans = 0;
  const { workflow, calls } = harness({
    runtimeSettings: { automation_enabled: true, ai_reply_enabled: true, conversation_agent_mode: 'active', execution_owner: 'deterministic' },
    conversationContextStore: { async get() { return { facts: {}, messages: [] }; } },
    shadowAgentScheduler: { async schedule() { scheduled += 1; } },
    conversationAgentPlanner: { async plan() { activePlans += 1; throw new Error('active planner must not run in business worker'); } },
    replyPreviewClient: { async capture() { return { status: 'preview_ready', autoSend: true, draft: { intent: '其他', confidence: 0.9, needs_human: false, reply: 'deterministic-safe-reply' } }; } },
    autoReplyEnabled: true,
  });
  await workflow.processClaimed(record({ id: 'evt-owner-guard', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '怎么购买' } }));
  assert.equal(scheduled, 1);
  assert.equal(activePlans, 0);
  assert.equal(calls.find(([name]) => name === 'send')[1].text, 'deterministic-safe-reply');
});

test('quote preview only mode ignores non-message events and never consumes delivery queues', async () => {
  const { workflow, calls } = harness({
    quotePreviewClient: { async capture() { throw new Error('capture must not run for non-message events'); } },
    eventStore: { async claimDue() { return null; } },
    backend: {
      async claimDelivery() { throw new Error('delivery queue must not run in preview mode'); },
      async getRuntimeSettings() { throw new Error('runtime settings must not be requested for non-message events'); },
    },
  });
  const eventResult = await workflow.processClaimed(record({
    id: 'evt-preview-status', tenantId: 'tenant-1', event: 'order.paid', ts: Date.now(), payload: {},
  }));
  assert.equal(eventResult.mode, 'quote_preview_only');
  assert.equal(await workflow.tick(), null);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('text message is routed through backend agent before replying', async () => {
  const backendCalls = [];
  const backend = {
    async upsertOrder() { return { task: { id: 'task_text' } }; },
    async runAgent(input) {
      backendCalls.push(input);
      return {
        task: { id: 'task_text', status: 'received' },
        actions: [{ kind: 'send_message', text: '收到，请发座位图我来核对。', ai_reply_event_id: input.event_id, task_id: input.task_id }],
      };
    },
    async markAiReplySent(eventId, input) { backendCalls.push(['sent', eventId, input]); },
  };
  const { workflow, calls } = harness({ backend, visionFor: async () => null });
  const envelope = {
    id: 'evt-text', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', content: '能买这场吗' },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.status, 'completed');
  assert.equal(backendCalls[0].max_tool_calls, 6);
  assert.equal(backendCalls[0].max_run_seconds, 45);
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '收到，请发座位图我来核对。');
  assert.deepEqual(backendCalls[1], ['sent', 'evt-text:agent-run', { tenant_id: 'tenant-1', task_id: 'task_text' }]);
});

test('idle workers never consume legacy delivery or reminder queues', async () => {
  const backend = {
    async claimDelivery() { throw new Error('automatic ticket delivery is disabled'); },
    async claimReminder() { throw new Error('automatic reminders are disabled'); },
  };
  const { workflow } = harness({ backend, eventStore: { async claimDue() { return null; } } });
  assert.equal(await workflow.tick(), null);
});

test('system-mode image handling never falls back to a plugin-local visual model', async () => {
  const backendCalls = [];
  const backend = {
    async upsertOrder() { return { task: { id: 'task_regular_incomplete' } }; },
    async submitOcrRecognition(_taskId, input) { backendCalls.push(input); return { processing_mode: 'ocr', task: { id: 'task_regular_incomplete' } }; },
  };
  const { workflow } = harness({ backend });
  const envelope = {
    id: 'evt-regular-incomplete', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] },
  };
  await workflow.processClaimed(record(envelope));
  assert.equal(backendCalls.length, 1);
  assert.equal(backendCalls[0].event_id, 'evt-regular-incomplete:ocr-recognition');
});

test('AI-only mode sends image to the AI vision pipeline', async () => {
  const backendCalls = [];
  const backend = {
    async upsertOrder() { return { task: { id: 'task-ai-only' } }; },
    async submitAiVisionRecognition(_taskId, input) {
      backendCalls.push(input);
      return { task: { id: 'task-ai-only' } };
    },
  };
  const { workflow } = harness({
    backend,
    runtimeSettings: {
      automation_enabled: true,
      auto_price_change: false,
      ai_reply_enabled: true,
      ai_only_mode_enabled: true,
    },
  });
  const envelope = {
    id: 'evt-ai-only', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] },
  };
  await workflow.processClaimed(record(envelope));
  assert.equal(backendCalls[0].event_id, 'evt-ai-only:ai-vision-recognition');
});

test('OCR quote reply does not depend on the AI customer-service switch', async () => {
  const backend = {
    async upsertOrder() { return { task: { id: 'task-ocr-ai-off' } }; },
    async submitOcrRecognition() {
      return {
        processing_mode: 'ocr',
        reply_origin: 'ocr',
        task: { id: 'task-ocr-ai-off', status: 'quoted' },
        action: { reply_message: '47.9元一张', modify_order_amount: false },
      };
    },
  };
  const { workflow, calls } = harness({
    backend,
    runtimeSettings: {
      automation_enabled: true,
      auto_price_change: false,
      ai_reply_enabled: false,
      ai_only_mode_enabled: false,
    },
  });
  const envelope = {
    id: 'evt-ocr-ai-off', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] },
  };
  await workflow.processClaimed(record(envelope));
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '47.9元一张');
});

test('shop off mode blocks OCR before image download', async () => {
  const { workflow, calls } = harness({
    backend: { async upsertOrder() { return { task: { id: 'task-shop-off' } }; } },
    runtimeSettings: {
      automation_enabled: false,
      execution_mode: 'off',
      shop_features: { recognition_enabled: false, quote_enabled: false, price_change_enabled: false },
      ai_reply_enabled: true,
      ai_only_mode_enabled: false,
    },
    imageLoader: { async load() { throw new Error('image must not be loaded'); } },
  });
  const envelope = {
    id: 'evt-shop-off', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-off', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.status, 'completed');
  assert.deepEqual(calls.find(([name]) => name === 'runtime-settings'), ['runtime-settings', 'shop-off']);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('shop recognition switch blocks OCR before image download', async () => {
  const { workflow, calls } = harness({
    backend: { async upsertOrder() { return { task: { id: 'task-recognition-off' } }; } },
    runtimeSettings: {
      automation_enabled: true,
      execution_mode: 'auto',
      shop_features: { recognition_enabled: false, quote_enabled: true, price_change_enabled: false },
      ai_reply_enabled: true,
      ai_only_mode_enabled: false,
    },
    imageLoader: { async load() { throw new Error('image must not be loaded'); } },
  });
  const envelope = {
    id: 'evt-recognition-off', tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(),
    payload: { accountUnb: 'shop-recognition-off', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.alicdn.com/a.png'] },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.status, 'completed');
  assert.deepEqual(calls.find(([name]) => name === 'runtime-settings'), ['runtime-settings', 'shop-recognition-off']);
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('backend-authorized price change does not send payment guidance before price.changed event', async () => {
  const backend = {
    async upsertOrder() {
      return {
        task: {
          id: 'task_2',
          status: 'price_update_pending',
          quantity: 2,
          quote_policy_snapshot: quotePolicySnapshot,
        },
        linked_action: { modify_order_amount: true, amount_cents: 8_000, reply_message: '请付款' },
      };
    },
  };
  const { workflow, calls } = harness({ backend, visionFor: async () => null });
  const envelope = {
    id: 'evt-2', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(),
    payload: { orderId: 'order-2', accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' },
  };
  await workflow.processClaimed(record(envelope));
  assert.ok(calls.some(([name]) => name === 'change-price'));
  assert.equal(calls.some(([name]) => name === 'send'), false);
});

test('tenant price-change switch blocks a backend change-price action', async () => {
  const backend = {
    async upsertOrder() {
      return {
        task: {
          id: 'task_switch',
          status: 'price_update_pending',
          quantity: 2,
          quote_policy_snapshot: quotePolicySnapshot,
        },
        linked_action: { modify_order_amount: true, amount_cents: 8_000 },
      };
    },
  };
  const { workflow, calls } = harness({ backend, runtimeSettings: {
    automation_enabled: true,
    auto_price_change: false,
    ai_reply_enabled: true,
  } });
  const envelope = {
    id: 'evt-switch', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(),
    payload: { orderId: 'order-switch', accountUnb: 'shop-1' },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.actions[0].reason, 'price_change_gate_failed');
  assert.equal(calls.some(([name]) => name === 'change-price'), false);
});

test('price change rejects string amounts from the backend', async () => {
  const backend = {
    async upsertOrder() {
      return {
        task: {
          id: 'task-string-amount',
          status: 'price_update_pending',
          quantity: 2,
          quote_policy_snapshot: quotePolicySnapshot,
        },
        linked_action: { modify_order_amount: true, amount_cents: '8000' },
      };
    },
  };
  const { workflow, calls } = harness({ backend });
  const envelope = {
    id: 'evt-string-amount', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(),
    payload: { orderId: 'order-string-amount', accountUnb: 'shop-1' },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.actions[0].reason, 'price_change_gate_failed');
  assert.equal(calls.some(([name]) => name === 'change-price'), false);
});

test('price change fails closed when backend task has no quote policy snapshot', async () => {
  const backend = {
    async upsertOrder() {
      return {
        task: { id: 'task_without_policy', status: 'price_update_pending', quantity: 2 },
        linked_action: { modify_order_amount: true, amount_cents: 8_000 },
      };
    },
  };
  const { workflow, calls } = harness({ backend, visionFor: async () => null });
  const envelope = {
    id: 'evt-without-policy', tenantId: 'tenant-1', event: 'order.created', ts: Date.now(),
    payload: { orderId: 'order-without-policy', accountUnb: 'shop-1' },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.actions[0].reason, 'price_change_gate_failed');
  assert.equal(calls.some(([name]) => name === 'change-price'), false);
});

test('price.changed is acknowledged by backend before payment guidance is sent', async () => {
  const backend = {
    async upsertOrder() { return { task: { id: 'task_3' } }; },
    async updateTaskStatus(taskId, input) {
      assert.equal(taskId, 'task_3');
      assert.equal(input.status, 'awaiting_payment');
      assert.equal(input.price_changed_amount_cents, 8_000);
      return { task: { id: taskId }, reply_message: '已经改好价格，可以付款了' };
    },
  };
  const { workflow, calls } = harness({ backend, visionFor: async () => null });
  const envelope = {
    id: 'evt-3', tenantId: 'tenant-1', event: 'order.price.changed', ts: Date.now(),
    payload: { priceFee: 8_000, accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' },
  };
  await workflow.processClaimed(record(envelope));
  assert.equal(calls.find(([name]) => name === 'send')[1].text, '已经改好价格，可以付款了');
});

test('paid event reads the authoritative order amount before backend payment validation', async () => {
  const backend = {
    async upsertOrder() { return { task: { id: 'task_4' } }; },
    async updateTaskStatus(_taskId, input) {
      assert.equal(input.status, 'paid');
      assert.equal(input.paid_amount_cents, 8_000);
      return { task: { id: 'task_4', status: 'paid' } };
    },
  };
  const { workflow } = harness({
    backend,
    visionFor: async () => null,
    core: { orders: { async get() { return { orderStatus: 2, priceFee: 8_000 }; } } },
  });
  const envelope = {
    id: 'evt-4', tenantId: 'tenant-1', event: 'order.paid', ts: Date.now(),
    payload: { orderId: 'order-4', accountUnb: 'shop-1' },
  };
  const result = await workflow.processClaimed(record(envelope));
  assert.equal(result.status, 'completed');
});
