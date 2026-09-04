import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createV2BackendClient } from '../src/backend/client.mjs';
import { loadV2Config } from '../src/config.mjs';
import { createV2HttpServer } from '../src/http/server.mjs';
import { createV2PlatformRuntime } from '../src/platform/runtime.mjs';
import { createV2Runtime as createRulesFirstRuntime } from '../src/runtime/event-processor.mjs';
import { V2EventStore } from '../src/runtime/event-store.mjs';
import manifest from '../yumaiduo.plugin.json' with { type: 'json' };

const silent = { debug() {}, info() {}, warn() {}, error() {} };

// Characterization tests written before the durable command API use this
// test-only adapter. Production source has no inline decision/action fallback.
function createV2Runtime(options) {
  if (typeof options.backend.claimCommands === 'function') return createRulesFirstRuntime(options);
  const legacy = options.backend;
  const pending = [];
  const commands = new Map();
  const outstanding = new Map();
  let sequence = 0;
  const backend = {
    ...legacy,
    processEvent: async (context) => {
      const response = await legacy.processEvent(context);
      const decision = response?.decision ?? response;
      if (decision?.mode === 'auto') {
        for (const action of Array.isArray(decision?.actions) ? decision.actions : []) {
          const commandId = `test-command-${sequence += 1}`;
          const command = {
            command_id: commandId, lease_token: `test-lease-${commandId}`,
            tenant_id: context.envelope.tenantId, event_id: context.envelope.id,
            action: structuredClone(action), context: {
              envelope: structuredClone(context.envelope),
              session: structuredClone(context.session), order: structuredClone(context.order),
              recent_messages: structuredClone(context.recentMessages ?? []),
            },
          };
          commands.set(commandId, command);
          pending.push(command);
          outstanding.set(context.envelope.id, (outstanding.get(context.envelope.id) ?? 0) + 1);
        }
      }
      return { event_id: context.envelope.id, accepted: true, duplicate: false };
    },
    claimCommands: async (limit = 10) => ({ commands: pending.splice(0, limit) }),
    reportCommand: async ({ commandId, result }) => {
      const command = commands.get(commandId);
      let response;
      try {
        response = await legacy.reportAction?.({
          tenantId: command.tenant_id, eventId: command.event_id,
          actionId: command.action.id, result,
        }) ?? {};
      } catch (error) {
        pending.push({ ...command, lease_token: `${command.lease_token}-retry` });
        throw error;
      }
      outstanding.set(command.event_id, Math.max(0, (outstanding.get(command.event_id) ?? 1) - 1));
      for (const action of Array.isArray(response.actions) ? response.actions : []) {
        const followId = `test-command-${sequence += 1}`;
        const follow = {
          ...command, command_id: followId, lease_token: `test-lease-${followId}`,
          action: { ...structuredClone(action), _completed_action_result: structuredClone(result) },
        };
        commands.set(followId, follow);
        pending.push(follow);
        outstanding.set(command.event_id, (outstanding.get(command.event_id) ?? 0) + 1);
      }
      return { ok: true };
    },
  };
  const runtime = createRulesFirstRuntime({ ...options, backend });
  return Object.freeze({
    ...runtime,
    health: async () => {
      const value = await runtime.health();
      const openEvents = [...outstanding.values()].filter((count) => count > 0).length;
      return {
        ...value,
        counts: { ...value.counts, completed: Math.max(0, (value.counts.completed ?? 0) - openEvents) },
      };
    },
  });
}

async function waitFor(check, timeoutMs = 30_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error('condition timed out');
}

function envelope(id, event = 'im.message.received') {
  return {
    id,
    tenantId: 'tenant-1',
    event,
    ts: Date.now(),
    payload: event === 'im.message.received'
      ? { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', text: 'hello' }
      : { orderId: 'order-1' },
  };
}

async function harness({ mode = 'auto', event = 'im.message.received', listMessages } = {}) {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-'));
  const calls = { process: 0, sent: 0, shops: 0, reports: 0, reportResults: [], getSessionByOrder: 0, listMessages: 0, processBodies: [] };
  const client = {
    shops: { list: async () => [{ accountUnb: 'shop-1', shopName: 'V2 Shop' }] },
    orders: { get: async () => ({ orderId: 'order-1', orderStatus: 1, payment: '0', payTime: null }) },
    im: {
      getSessionByOrder: async () => { calls.getSessionByOrder += 1; return { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }; },
      listMessages: async (query) => {
        calls.listMessages += 1;
        if (listMessages) return listMessages(query);
        return { items: [{ id: 'manual-1', direction: 'seller', content: { text: 'manual context' } }] };
      },
      sendMessage: async () => { calls.sent += 1; return { messageId: 'sent-1' }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 2 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => { calls.shops += 1; },
      processEvent: async (body) => { calls.process += 1; calls.processBodies.push(body); return { decision: { mode, actions: [{ id: 'reply-1', type: 'send_message', text: 'V2 reply' }] } }; },
      reportAction: async ({ result }) => { calls.reports += 1; calls.reportResults.push(result); },
    },
    logger: silent,
  });
  await runtime.start();
  return { runtime, calls, event: envelope('evt-1', event), dataDir };
}

function priceEnvelope(id = 'price-event') {
  return {
    id,
    tenantId: 'tenant-1',
    event: 'order.created',
    ts: Date.now(),
    payload: { orderId: 'order-1', accountUnb: 'shop-1', orderStatus: 1 },
  };
}

function providerOrder(overrides = {}) {
  return {
    orderId: 'order-1',
    accountUnb: 'shop-1',
    buyerUnb: 'buyer-1',
    orderStatus: 1,
    payTime: null,
    payment: 2_000,
    refundStatus: 'none',
    ...overrides,
  };
}

function quoteSnapshot(overrides = {}) {
  return {
    schema_version: 'wanda_backend.quote_snapshot.v1',
    quote_version: 'quote-v1',
    order_id: 'order-1',
    tenant_id: 'tenant-1',
    shop_id: 'shop-1',
    buyer_id: 'buyer-1',
    chat_id: 'chat-1',
    target_amount_cents: 2_900,
    ...overrides,
  };
}

function priceAction(overrides = {}) {
  return {
    id: 'price-1',
    type: 'change_order_price',
    quote_snapshot: quoteSnapshot(),
    ...overrides,
  };
}

async function priceChangeHarness({
  mode = 'auto',
  before = providerOrder(),
  after = providerOrder({ payment: 2_900 }),
  action = priceAction(),
  eventIds = ['price-event'],
  changePrice,
  history = [],
  confirmationOrderId = 'order-1',
} = {}) {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-price-'));
  const calls = { changed: 0, sent: 0, reports: [], orderReads: 0, historyReads: 0 };
  let reads = 0;
  const client = {
    shops: { list: async () => [{ accountUnb: 'shop-1', shopName: 'V2 Shop' }] },
    orders: {
      get: async () => {
        reads += 1;
        calls.orderReads += 1;
        return structuredClone(reads <= 2 ? before : after);
      },
      changePrice: async (orderId, request) => {
        calls.changed += 1;
        if (changePrice) return changePrice({ orderId, request });
        return { ok: true };
      },
    },
    im: {
      getSessionByOrder: async () => ({ accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' }),
      listMessages: async () => {
        calls.historyReads += 1;
        const items = typeof history === 'function' ? await history(calls.historyReads) : history;
        return { items: structuredClone(items) };
      },
      sendMessage: async () => { calls.sent += 1; return { messageId: `sent-${calls.sent}` }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode, actions: [structuredClone(action)] } }),
      reportAction: async ({ result }) => {
        calls.reports.push(structuredClone(result));
        return result.status === 'succeeded' && calls.reports.length === 1
          ? { actions: [{ id: 'confirm-1', type: 'send_price_change_confirmation', order_id: confirmationOrderId, text: '请核对订单金额后再付款。' }] }
          : {};
      },
    },
    logger: silent,
  });
  await runtime.start();
  for (let index = 0; index < eventIds.length; index += 1) {
    await runtime.enqueue(priceEnvelope(eventIds[index]));
    await waitFor(async () => (await runtime.health()).counts.completed === index + 1);
  }
  await waitFor(async () => {
    const health = await runtime.health();
    const reportsSettled = Object.entries(health.actionReportCounts).every(([status, count]) => status === 'delivered' || count === 0);
    return health.running === 0 && reportsSettled;
  });
  await runtime.stop();
  return { calls, dataDir };
}

test('rules-first runtime persists events then claims and reports durable commands', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-rules-first-'));
  const calls = { accepted: 0, sent: 0, reported: [] };
  let offered = false;
  const source = envelope('rules-first-event');
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => ({
      shops: { list: async () => [] },
      orders: { get: async () => providerOrder() },
      im: {
        listMessages: async () => ({ items: [] }),
        getSessionByOrder: async () => ({ accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }),
        sendMessage: async () => { calls.sent += 1; return { messageId: 'sent-command-1' }; },
      },
    }) },
    backend: {
      syncShops: async () => {},
      processEvent: async () => { calls.accepted += 1; return { event_id: source.id, accepted: true, duplicate: false }; },
      claimCommands: async () => {
        if (calls.accepted === 0 || offered) return { commands: [] };
        offered = true;
        return { commands: [{
          command_id: 'command-1', lease_token: 'lease-1', tenant_id: 'tenant-1', event_id: source.id,
          action: { id: 'reply-command-1', type: 'send_message', text: '固定规则回复', rule_governed: true },
          context: {
            envelope: source,
            session: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' },
            order: null,
            recent_messages: [],
          },
        }] };
      },
      reportCommand: async (value) => { calls.reported.push(value); return { ok: true }; },
    },
    logger: silent,
  });
  await runtime.start();
  await runtime.enqueue(source);
  await waitFor(() => calls.reported.length === 1);
  await runtime.stop();

  assert.equal(calls.accepted, 1);
  assert.equal(calls.sent, 1);
  assert.equal(calls.reported[0].commandId, 'command-1');
  assert.equal(calls.reported[0].result.status, 'succeeded');
  assert.equal(calls.reported[0].result.message_id, 'sent-command-1');
});

async function runCanonicalWplusReplyTest({ externalBetweenMessages = false } = {}) {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-canonical-wplus-reply-'));
  const source = envelope(`canonical-wplus-${externalBetweenMessages ? 'external' : 'self'}`);
  const sent = [];
  const reports = [];
  const history = [];
  let offered = 0;
  let eventAccepted = false;
  let processCalls = 0;
  let listCalls = 0;
  const client = {
    shops: { list: async () => [] },
    orders: { get: async () => providerOrder() },
    im: {
      listMessages: async () => {
        listCalls += 1;
        return { items: structuredClone(history) };
      },
      getSessionByOrder: async () => ({ accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }),
      sendMessage: async ({ text }) => {
        const messageId = `canonical-sent-${sent.length + 1}`;
        sent.push(text);
        history.push({ messageId, direction: 'seller', messageType: '1', content: { text } });
        if (externalBetweenMessages && sent.length === 1) {
          history.push({
            messageId: 'external-operator-message', direction: 'seller', messageType: '1',
            content: { text: '人工客服介入' }, agent_generated: false,
          });
        }
        return { messageId };
      },
    },
  };
  const replies = [
    { kind: 'purchase_summary', text: '影院\n《电影》\n9月5日19:55这场' },
    { kind: 'price', text: 'W+ 49.9一张，需要几张呀' },
  ];
  const runtime = createRulesFirstRuntime({
    config: {
      dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1,
      externalWritesEnabled: true, messageSendEnabled: true,
      xianyuRepriceEnabled: false, liangpiaoOrderCreateEnabled: false,
      wandaProviderWritesEnabled: false, refundEnabled: false, shipEnabled: false,
    },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => {
        processCalls += 1;
        eventAccepted = true;
        return { accepted: true, event_id: source.id, duplicate: false };
      },
      claimCommands: async () => {
        if (!eventAccepted || offered >= replies.length) return { commands: [] };
        const index = offered;
        offered += 1;
        return { commands: [{
          command_id: `canonical-command-${index + 1}`, lease_token: `canonical-lease-${index + 1}`,
          tenant_id: source.tenantId, event_id: source.id,
          action: {
            id: `${source.id}:canonical-reply:${index === 0 ? 'summary' : 'price'}`,
            type: 'send_message', text: replies[index].text, rule_governed: true,
            source: 'canonical_quote_runtime', canonical_reply_kind: `QUOTE_PREVIEW_WPLUS:${replies[index].kind}`,
          },
          context: {
            envelope: source,
            session: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' },
            order: null, recent_messages: [],
          },
        }] };
      },
      reportCommand: async (value) => { reports.push(value); return { ok: true }; },
    },
    logger: silent,
  });
  await runtime.start();
  await runtime.enqueue(source);
  await waitFor(() => reports.length === 2);
  await runtime.stop();
  return { sent, reports, listCalls, processCalls };
}

test('canonical W+ preview sends summary before price and self summary does not block price', async () => {
  const result = await runCanonicalWplusReplyTest();
  assert.deepEqual(result.sent, ['影院\n《电影》\n9月5日19:55这场', 'W+ 49.9一张，需要几张呀']);
  assert.deepEqual(result.reports.map((item) => item.result.status), ['succeeded', 'succeeded']);
  assert.ok(result.listCalls >= 4, 'each message must execute final IM history preflight');
  assert.equal(result.processCalls, 1, 'command failure or success must not rerun recognition');
});

test('canonical W+ price message is blocked when an external operator replies between messages', async () => {
  const result = await runCanonicalWplusReplyTest({ externalBetweenMessages: true });
  assert.deepEqual(result.sent, ['影院\n《电影》\n9月5日19:55这场']);
  assert.equal(result.reports[0].result.status, 'succeeded');
  assert.equal(result.reports[1].result.status, 'skipped');
  assert.equal(result.reports[1].result.reason, 'human_message_arrived_before_send');
});

test('durable verified price confirmation uses backend result binding and custom text', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-rules-price-confirmation-'));
  const source = priceEnvelope('verified-confirmation-event');
  const calls = { accepted: 0, sent: [], reported: [] };
  let offered = false;
  const runtime = createRulesFirstRuntime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => ({
      shops: { list: async () => [] },
      orders: { get: async () => providerOrder({ payment: 2_900 }) },
      im: {
        listMessages: async () => ({ items: [] }),
        getSessionByOrder: async () => ({ accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }),
        sendMessage: async ({ text }) => { calls.sent.push(text); return { messageId: 'confirmation-sent' }; },
      },
    }) },
    backend: {
      syncShops: async () => {},
      processEvent: async () => { calls.accepted += 1; return { event_id: source.id, accepted: true, duplicate: false }; },
      claimCommands: async () => {
        if (calls.accepted === 0 || offered) return { commands: [] };
        offered = true;
        return { commands: [{
          command_id: 'confirmation-command', lease_token: 'confirmation-lease',
          tenant_id: 'tenant-1', event_id: source.id,
          action: {
            id: `${source.id}:confirm-price-change`, type: 'send_price_change_confirmation',
            order_id: 'order-1', text: '后台自定义：金额29.00元已核验，可以付款。',
            _completed_action_result: {
              order_id: 'order-1', target_amount_cents: 2_900, verified_amount_cents: 2_900,
            },
          },
          context: {
            envelope: source,
            session: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' },
            order: providerOrder({ payment: 2_900 }), recent_messages: [],
          },
        }] };
      },
      reportCommand: async (value) => { calls.reported.push(value); return { ok: true }; },
    },
    logger: silent,
  });

  await runtime.start();
  await runtime.enqueue(source);
  await waitFor(() => calls.reported.length === 1);
  await runtime.stop();

  assert.deepEqual(calls.sent, ['后台自定义：金额29.00元已核验，可以付款。']);
  assert.equal(calls.reported[0].result.status, 'succeeded');
  assert.equal(calls.reported[0].result.message_id, 'confirmation-sent');
});

test('durable reconciliation-only price command never calls platform changePrice', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-rules-reconcile-'));
  const source = priceEnvelope('reconcile-event');
  const calls = { accepted: 0, changed: 0, reported: [] };
  let offered = false;
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => ({
      shops: { list: async () => [] },
      orders: {
        get: async () => providerOrder({ payment: 2_000 }),
        changePrice: async () => { calls.changed += 1; return { ok: true }; },
      },
      im: {
        listMessages: async () => ({ items: [] }),
        getSessionByOrder: async () => ({ accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }),
      },
    }) },
    backend: {
      syncShops: async () => {},
      processEvent: async () => { calls.accepted += 1; return { event_id: source.id, accepted: true, duplicate: false }; },
      claimCommands: async () => {
        if (calls.accepted === 0 || offered) return { commands: [] };
        offered = true;
        return { commands: [{
          command_id: 'reconcile-command', lease_token: 'lease-reconcile', tenant_id: 'tenant-1', event_id: source.id,
          reconciliation_only: true, action: priceAction(),
          context: { envelope: source, session: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }, order: providerOrder(), recent_messages: [] },
        }] };
      },
      reportCommand: async (value) => { calls.reported.push(value); return { ok: true }; },
    },
    logger: silent,
  });
  await runtime.start();
  await runtime.enqueue(source);
  await waitFor(() => calls.reported.length === 1);
  await runtime.stop();

  assert.equal(calls.changed, 0);
  assert.equal(calls.reported[0].result.status, 'unknown');
  assert.equal(calls.reported[0].result.reason_code, 'read_only_reconciliation_not_verified');
});

test('image-event price action uses the backend quote binding to read and change the order', async () => {
  const sourceEvent = envelope('image-price-action');
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-image-price-'));
  const observed = { changed: 0, sent: 0, reports: [] };
  let reads = 0;
  const client = {
    shops: { list: async () => [] },
    orders: {
      get: async (orderId) => {
        assert.equal(orderId, 'order-1');
        reads += 1;
        return providerOrder({ payment: reads <= 2 ? 2_000 : 2_900 });
      },
      changePrice: async (orderId, request) => {
        assert.equal(orderId, 'order-1');
        assert.deepEqual(request, { priceFee: 2_900, transportFee: 0 });
        observed.changed += 1;
        return { ok: true };
      },
    },
    im: {
      listMessages: async () => ({ items: [] }),
      getSessionByOrder: async () => ({ accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' }),
      sendMessage: async () => {
        observed.sent += 1;
        return { messageId: `sent-${observed.sent}` };
      },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [priceAction()] } }),
      reportAction: async ({ result }) => {
        observed.reports.push(result);
        return result.status === 'succeeded' && observed.reports.length === 1
          ? {
              actions: [{
                id: 'confirm-image-1',
                type: 'send_price_change_confirmation',
                order_id: 'order-1',
                text: '请核对订单金额后再付款。',
              }],
            }
          : {};
      },
    },
    logger: silent,
  });
  await runtime.start();
  await runtime.enqueue(sourceEvent);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await waitFor(() => observed.reports.length === 2);
  await runtime.stop();

  assert.equal(observed.changed, 1);
  assert.equal(observed.sent, 1);
  assert.equal(observed.reports[0].status, 'succeeded');
  assert.equal(observed.reports[0].verified_amount_cents, 2_900);
  assert.equal(observed.reports[1].status, 'succeeded');
});

test('price confirmation rejects an order id different from the completed price change', async () => {
  const { calls } = await priceChangeHarness({ confirmationOrderId: 'order-2' });

  assert.equal(calls.changed, 1);
  assert.equal(calls.sent, 0);
  assert.equal(calls.reports.length, 2);
  assert.equal(calls.reports[1].status, 'skipped');
  assert.equal(calls.reports[1].reason, 'price_change_confirmation_order_mismatch');
});

test('event idempotency executes a duplicate webhook event once', async () => {
  const { runtime, calls, event } = await harness();
  const first = await runtime.enqueue(event);
  const duplicate = await runtime.enqueue(event);
  assert.equal(first.created, true);
  assert.equal(duplicate.created, false);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  assert.equal(calls.process, 1);
  assert.equal(calls.sent, 1);
  await runtime.stop();
});

test('a processing event is safely reclaimed after a plugin restart', async () => {
  const { runtime, event, dataDir } = await harness();
  await runtime.enqueue(event);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();
  const raw = JSON.parse(await readFile(join(dataDir, 'events.v2.json'), 'utf8'));
  raw.events['tenant-1:evt-1'].status = 'processing';
  raw.events['tenant-1:evt-1'].lease = 'stale-lease';
  await (await import('node:fs/promises')).writeFile(join(dataDir, 'events.v2.json'), JSON.stringify(raw));
  const restarted = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => ({ shops: { list: async () => [] }, im: { sendMessage: async () => ({ messageId: 'ignored' }) } }) },
    backend: { syncShops: async () => {}, processEvent: async () => ({ decision: { mode: 'shadow', actions: [] } }), reportAction: async () => ({}) },
    logger: silent,
  });
  await restarted.start();
  await waitFor(async () => (await restarted.health()).counts.completed === 1);
  await restarted.stop();
});

test('only automatic shop mode is allowed to send a reply', async () => {
  for (const mode of ['off', 'shadow', 'auto']) {
    const { runtime, calls, event } = await harness({ mode });
    await runtime.enqueue(event);
    await waitFor(async () => (await runtime.health()).counts.completed === 1);
    assert.equal(calls.sent, mode === 'auto' ? 1 : 0, `mode ${mode}`);
    await runtime.stop();
  }
});

test('conversation history failure blocks a stale automatic reply', async () => {
  const { runtime, calls, event } = await harness({
    listMessages: async () => { throw new Error('history unavailable'); },
  });
  await runtime.enqueue(event);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.process, 1);
  assert.equal(calls.sent, 0);
  assert.equal(calls.reports, 1);
  assert.equal(calls.reportResults[0].reason, 'conversation_preflight_unavailable');
});

test('order events resolve their session with the official order lookup before reply', async () => {
  const { runtime, calls, event } = await harness({ event: 'order.paid' });
  await runtime.enqueue(event);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  assert.equal(calls.getSessionByOrder, 1);
  assert.equal(calls.sent, 1);
  await runtime.stop();
});

test('order events retry authoritative reads during the platform propagation window', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-order-propagation-'));
  const calls = { orderReads: 0, sessionReads: 0, body: null };
  const client = {
    shops: { list: async () => [] },
    orders: { get: async () => {
      calls.orderReads += 1;
      if (calls.orderReads < 3) throw new Error('order_not_propagated');
      return providerOrder({
        chatId: 'chat-1', receiverName: '敏感收件人', receiverPhone: '13800138000',
        receiverAddress: '敏感地址', buyerNick: '无需转发的昵称',
      });
    } },
    im: {
      getSessionByOrder: async () => {
        calls.sessionReads += 1;
        if (calls.sessionReads < 2) throw new Error('session_not_propagated');
        return { accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' };
      },
      listMessages: async () => ({ items: [] }),
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async (body) => { calls.body = body; return { decision: { mode: 'auto', actions: [] } }; },
      reportAction: async () => ({}),
    },
    logger: silent,
  });
  await runtime.start();
  const propagationEvent = priceEnvelope('order-propagation');
  propagationEvent.payload.receiverPhone = '13900139000';
  await runtime.enqueue(propagationEvent);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.sessionReads, 2);
  assert.equal(calls.orderReads, 3);
  assert.equal(calls.body.order.order_id, 'order-1');
  assert.equal(calls.body.order.shop_id, 'shop-1');
  assert.equal(calls.body.order.receiverName, undefined);
  assert.equal(calls.body.order.receiverPhone, undefined);
  assert.equal(calls.body.envelope.payload.receiverPhone, undefined);
  assert.equal(calls.body.order.receiverAddress, undefined);
  assert.equal(calls.body.order.buyerNick, undefined);
  assert.equal(JSON.stringify(calls.body.order).includes('13800138000'), false);
  assert.equal(calls.body.session.chatId, 'chat-1');
});

test('buyer text resolves the latest authoritative order from transaction history', async () => {
  const { runtime, calls, event } = await harness({
    listMessages: async () => ({ items: [
      { id: 'buyer-current', direction: 'buyer', messageType: 1, content: 'OK了吗', sentAtMs: 3000 },
      { id: 'paid-status', direction: 'buyer', messageType: 26, orderId: 'order-1', content: '我已付款', sentAtMs: 2000 },
    ] }),
  });
  await runtime.enqueue(event);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);

  assert.equal(calls.processBodies[0].order.order_id, 'order-1');
  assert.equal(calls.processBodies[0].order.order_status, '1');
  await runtime.stop();
});

test('a stale order id cannot prevent a buyer message from reaching the Agent', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-stale-order-'));
  const calls = { process: 0, sent: 0, body: null };
  const client = {
    shops: { list: async () => [] },
    orders: { get: async () => { throw new Error('order_not_found'); } },
    im: {
      listMessages: async () => ({ items: [] }),
      sendMessage: async () => { calls.sent += 1; return { messageId: 'sent-stale-order' }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async (body) => {
        calls.process += 1;
        calls.body = body;
        return { decision: { mode: 'auto', actions: [{ id: 'reply-stale-order', type: 'send_message', text: '继续处理当前会话' }] } };
      },
      reportAction: async () => ({}),
    },
    logger: silent,
  });
  await runtime.start();
  const message = envelope('evt-stale-order');
  message.payload.orderId = 'stale-order-id';
  await runtime.enqueue(message);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  assert.equal(calls.process, 1);
  assert.equal(calls.sent, 1);
  assert.equal(calls.body.order, null);
  assert.equal(calls.body.session.chatId, 'chat-1');
  await runtime.stop();
});

test('a null order session lookup does not crash an order event', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-null-session-'));
  const calls = { process: 0, body: null };
  const client = {
    shops: { list: async () => [] },
    orders: { get: async () => ({ orderId: 'order-1', orderStatus: 4 }) },
    im: { getSessionByOrder: async () => null },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async (body) => { calls.process += 1; calls.body = body; return { decision: { mode: 'shadow', actions: [] } }; },
      reportAction: async () => ({}),
    },
    logger: silent,
  });
  await runtime.start();
  await runtime.enqueue(envelope('evt-null-session', 'order.finished'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  assert.equal(calls.process, 1);
  assert.equal(calls.body.session, null);
  assert.equal(calls.body.order.order_id, 'order-1');
  await runtime.stop();
});

test('recent platform messages are passed to backend before AI processing', async () => {
  const { runtime, calls, event } = await harness();
  await runtime.enqueue(event);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  assert.equal(calls.listMessages, 3);
  assert.equal(calls.processBodies[0].recentMessages[0].direction, 'seller');
  assert.equal(calls.processBodies[0].recentMessages[0].content.text, 'manual context');
  await runtime.stop();
});

test('canonical history sync respects the platform page cap while preflight stays at 50', async () => {
  const queries = [];
  const { runtime, calls, event } = await harness({
    listMessages: async (query) => {
      queries.push(structuredClone(query));
      return { items: [{ id: 'buyer-1', direction: 'buyer', content: { text: 'hello' } }] };
    },
  });
  await runtime.enqueue(event);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.sent, 1);
  assert.equal(queries[0].pageSize, 100);
  assert.ok(queries.some((query) => query.pageSize === 50), 'send preflight must keep its smaller history window');
  assert.ok(queries.every((query) => query.pageSize <= 100), 'all platform history requests must respect the cap');
});

test('a seller reply arriving while the Agent runs cancels the stale AI quote before send', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-human-race-'));
  const calls = { listMessages: 0, sent: 0, reports: [] };
  const baseline = [{ id: 'buyer-image', direction: 'buyer', content: { text: 'seat image' } }];
  const client = {
    shops: { list: async () => [] },
    im: {
      listMessages: async () => {
        calls.listMessages += 1;
        return { items: calls.listMessages === 1
          ? baseline
          : [...baseline, { id: 'manual-price', direction: 'seller', content: { text: '47.9一张' } }] };
      },
      sendMessage: async () => { calls.sent += 1; return { messageId: 'should-not-send' }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [{ id: 'stale-quote', type: 'send_message', text: '45元/张' }] } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });

  await runtime.start();
  await runtime.enqueue(envelope('evt-human-race'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.sent, 0);
  assert.equal(calls.reports[0].status, 'skipped');
  assert.equal(calls.reports[0].reason, 'human_message_arrived_before_send');
});

test('the final send preflight catches an operator reply after the planning preflight', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-final-send-preflight-'));
  const calls = { listMessages: 0, sent: 0, reports: [] };
  const buyer = { id: 'buyer-image', direction: 'buyer', content: { text: 'seat image' }, sentAtMs: 100 };
  const client = {
    shops: { list: async () => [] },
    im: {
      listMessages: async () => {
        calls.listMessages += 1;
        if (calls.listMessages < 3) return { items: [buyer] };
        return { items: [buyer, { id: 'human-final', direction: 'seller', messageType: 1, content: { text: '人工已接待' }, sentAtMs: 200 }] };
      },
      sendMessage: async () => { calls.sent += 1; return { messageId: 'must-not-send' }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [{ id: 'final-preflight-reply', type: 'send_message', text: 'Canonical reply' }] } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });

  await runtime.start();
  await runtime.enqueue(envelope('evt-final-send-preflight'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.listMessages, 3);
  assert.equal(calls.sent, 0);
  assert.equal(calls.reports[0].status, 'skipped');
  assert.equal(calls.reports[0].reason, 'human_message_arrived_before_send');
});

test('an agent warning arriving during order pricing does not cancel the price change', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-agent-warning-race-'));
  const calls = { listMessages: 0, changed: 0, reports: [] };
  let changed = false;
  const sentWarning = { id: 'agent-warning', messageId: 'agent-warning', direction: 'seller', agent_generated: true, content: { text: '请先不要付款' } };
  const client = {
    shops: { list: async () => [] },
    orders: {
      get: async () => providerOrder({ payment: changed ? 2_900 : 2_000 }),
      changePrice: async () => { calls.changed += 1; changed = true; return { ok: true }; },
    },
    im: {
      getSessionByOrder: async () => ({ accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' }),
      listMessages: async () => {
        calls.listMessages += 1;
        return { items: calls.listMessages >= 4 ? [sentWarning] : [] };
      },
      sendMessage: async () => ({ messageId: 'agent-warning' }),
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async ({ envelope: current }) => ({ decision: {
        mode: 'auto',
        actions: current.event === 'order.created'
          ? [priceAction()]
          : [{ id: 'warning', type: 'send_message', text: '请先不要付款' }],
      } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });

  await runtime.start();
  await runtime.enqueue(envelope('warning-event'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.enqueue(priceEnvelope('price-after-warning'));
  await waitFor(async () => (await runtime.health()).counts.completed === 2);
  await runtime.stop();

  assert.equal(calls.changed, 1);
  assert.equal(calls.reports.at(-1).status, 'succeeded');
  assert.notEqual(calls.reports.at(-1).reason, 'human_message_arrived_before_send');
});

test('a newer buyer message cancels a reply planned from the older conversation snapshot', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-buyer-race-'));
  const calls = { listMessages: 0, sent: 0, reports: [] };
  const baseline = [{ id: 'buyer-1', direction: 'buyer', content: { text: '多少钱' } }];
  const client = {
    shops: { list: async () => [] },
    im: {
      listMessages: async () => {
        calls.listMessages += 1;
        return { items: calls.listMessages === 1
          ? baseline
          : [...baseline, { id: 'buyer-2', direction: 'buyer', content: { text: '要三张' } }] };
      },
      sendMessage: async () => { calls.sent += 1; return { messageId: 'should-not-send' }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [{ id: 'old-reply', type: 'send_message', text: '两张90元' }] } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });

  await runtime.start();
  await runtime.enqueue(envelope('evt-buyer-race'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.sent, 0);
  assert.equal(calls.reports[0].reason, 'buyer_message_arrived_before_send');
});

test('an older preserved buyer event absorbs facts but does not send a stale reply', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-preserved-text-'));
  const calls = { sent: 0, reports: [] };
  const messages = [
    { messageId: 'buyer-old', direction: 'buyer', messageType: 1, content: '2张', sentAtMs: 100 },
    { messageId: 'buyer-new', direction: 'buyer', messageType: 1, content: '确认', sentAtMs: 200 },
  ];
  const client = {
    shops: { list: async () => [] },
    im: {
      listMessages: async () => ({ items: messages }),
      sendMessage: async () => { calls.sent += 1; return { messageId: 'should-not-send' }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [{ id: 'old-reply', type: 'send_message', text: '已记下2张' }] } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });
  const event = envelope('evt-preserved-old');
  event.payload.remoteMessageId = 'buyer-old';

  await runtime.start();
  await runtime.enqueue(event);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.sent, 0);
  assert.equal(calls.reports[0].reason, 'buyer_message_already_newer');
});

test('queued newer screenshot suppresses generic text reply when FishMore history still lags', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-queued-newer-'));
  const calls = { sent: 0, reports: [] };
  let releaseFirst;
  let firstStarted;
  const started = new Promise((resolve) => { firstStarted = resolve; });
  const gate = new Promise((resolve) => { releaseFirst = resolve; });
  const client = {
    shops: { list: async () => [] },
    im: {
      listMessages: async () => ({ items: [] }),
      sendMessage: async () => { calls.sent += 1; return { messageId: 'stale-generic' }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 17), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async ({ envelope: current }) => {
        if (current.id === 'queued-text') {
          firstStarted();
          await gate;
          return { decision: { mode: 'auto', actions: [{
            id: 'queued-text:reply', type: 'send_message', text: '请发送截图',
          }] } };
        }
        return { decision: { mode: 'auto', actions: [] } };
      },
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });
  const textEvent = envelope('queued-text');
  textEvent.payload.messageType = 1;
  textEvent.payload.content = '这个能买吗';
  const imageEvent = envelope('queued-image');
  imageEvent.payload.messageType = 2;
  imageEvent.payload.imageUrls = ['https://img.alicdn.com/current.jpg'];

  await runtime.start();
  await runtime.enqueue(textEvent);
  await started;
  await runtime.enqueue(imageEvent);
  releaseFirst();
  await waitFor(async () => (await runtime.health()).counts.completed === 2);
  await runtime.stop();

  assert.equal(calls.sent, 0);
  assert.equal(calls.reports[0].reason, 'newer_session_event_already_queued');
});

test('ordinary buyer messages arriving after order creation do not cancel one verified price change', async () => {
  const ordinaryMessages = [
    '还在吗', '麻烦快一点', '好的', '收到', '辛苦了',
    '我等着', '大概多久', '可以的', '嗯嗯', '谢谢',
  ].map((content, index) => ({
    id: `ordinary-${index}`, direction: 'buyer', messageType: 1, content: { text: content }, sentAtMs: 200 + index,
  }));
  const { calls } = await priceChangeHarness({
    history: (read) => read === 1 ? [] : ordinaryMessages,
  });

  assert.equal(calls.changed, 1);
  assert.equal(calls.reports[0].status, 'succeeded');
});

test('a verified price confirmation is sent despite ordinary buyer follow-up messages', async () => {
  const ordinary = { id: 'ordinary-follow-up', direction: 'buyer', messageType: 1, content: { text: '好的，收到' }, sentAtMs: 201 };
  const { calls } = await priceChangeHarness({ history: (read) => read === 1 ? [] : [ordinary] });

  assert.equal(calls.changed, 1);
  assert.equal(calls.sent, 1);
  assert.equal(calls.reports.at(-1).status, 'succeeded');
});

test('the same confirmed ticket count does not block an authorized price change', async () => {
  const sameCount = { id: 'same-count', direction: 'buyer', messageType: 1, content: { text: '2张' }, sentAtMs: 201 };
  const action = priceAction({ quote_snapshot: quoteSnapshot({
    quote_record_id: 'quote-1', confirmation_version: 'confirm-1',
    quote_expires_at: '2099-08-26T05:00:00+00:00', confirmed_ticket_count: 2,
  }) });
  const { calls } = await priceChangeHarness({
    action, history: (read) => read === 1 ? [] : [sameCount],
  });

  assert.equal(calls.changed, 1);
  assert.equal(calls.reports[0].status, 'succeeded');
});

test('buyer quote-changing input still blocks an old price command', async (context) => {
  const mutations = [
    { name: 'new screenshot', message: { id: 'new-image', direction: 'buyer', messageType: 2, imageUrls: ['https://img.alicdn.com/new.jpg'], sentAtMs: 201 } },
    { name: 'ticket count', message: { id: 'new-count', direction: 'buyer', messageType: 1, content: { text: '改成3张' }, sentAtMs: 201 } },
    { name: 'seat or showtime', message: { id: 'new-seat', direction: 'buyer', messageType: 1, content: { text: '换成后排座位' }, sentAtMs: 201 } },
    { name: 'cancellation', message: { id: 'new-cancel', direction: 'buyer', messageType: 1, content: { text: '取消，不要了' }, sentAtMs: 201 } },
  ];
  for (const mutation of mutations) {
    await context.test(mutation.name, async () => {
      const { calls } = await priceChangeHarness({ history: (read) => read === 1 ? [] : [mutation.message] });
      assert.equal(calls.changed, 0);
      assert.equal(calls.reports[0].status, 'skipped');
      assert.equal(calls.reports[0].reason, 'buyer_quote_inputs_changed_before_price_change');
    });
  }
});

test('a human seller message stops an authorized price change before the platform write', async () => {
  const manual = { id: 'manual', direction: 'seller', messageType: 1, content: { text: '我来处理' }, sentAtMs: 201 };
  const { calls } = await priceChangeHarness({ history: (read) => read === 1 ? [] : [manual] });

  assert.equal(calls.changed, 0);
  assert.equal(calls.reports[0].status, 'skipped');
  assert.equal(calls.reports[0].reason, 'human_message_arrived_before_send');
});

test('a human seller message suppresses verified price result notification', async () => {
  const manual = { id: 'manual', direction: 'seller', messageType: 2, imageUrls: ['https://img.alicdn.com/manual.jpg'], sentAtMs: 201 };
  const { calls } = await priceChangeHarness({ history: (read) => read <= 2 ? [] : [manual] });

  assert.equal(calls.changed, 1);
  assert.equal(calls.sent, 0);
  assert.equal(calls.reports.at(-1).status, 'skipped');
  assert.equal(calls.reports.at(-1).reason, 'human_message_arrived_before_send');
});

test('a newer screenshot suppresses an older screenshot reply after both facts are processed', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-image-burst-'));
  const calls = { sent: 0, reports: [] };
  const messages = [
    { messageId: 'image-old', direction: 'buyer', messageType: 2, imageUrls: ['https://img.alicdn.com/old.jpg'], sentAtMs: 100 },
    { messageId: 'image-new', direction: 'buyer', messageType: 2, imageUrls: ['https://img.alicdn.com/new.jpg'], sentAtMs: 200 },
  ];
  const client = {
    shops: { list: async () => [] },
    im: {
      listMessages: async () => ({ items: messages }),
      sendMessage: async () => { calls.sent += 1; return { messageId: 'should-not-send' }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [{
        id: 'old-image-reply', type: 'send_message', text: '旧图报价',
        preserve_on_new_buyer_message: true, suppress_on_newer_image: true,
      }] } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });
  const event = envelope('evt-image-burst-old');
  event.payload.remoteMessageId = 'image-old';

  await runtime.start();
  await runtime.enqueue(event);
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.sent, 0);
  assert.equal(calls.reports[0].reason, 'newer_buyer_image_already_present');
});

test('a newer buyer message does not cancel an in-flight image recognition result', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-image-race-'));
  const calls = { listMessages: 0, sent: 0, reports: [] };
  const baseline = [{ id: 'buyer-image', direction: 'buyer', content: { text: '[图片]' } }];
  const client = {
    shops: { list: async () => [] },
    im: {
      listMessages: async () => {
        calls.listMessages += 1;
        return { items: calls.listMessages === 1
          ? baseline
          : [...baseline, { id: 'buyer-city', direction: 'buyer', content: { text: '南宁' } }] };
      },
      sendMessage: async () => { calls.sent += 1; return { messageId: 'image-result-sent' }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [{
        id: 'image-reply', type: 'send_message', text: '识别和报价结果', preserve_on_new_buyer_message: true,
      }] } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });

  await runtime.start();
  await runtime.enqueue(envelope('evt-image-race'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.sent, 1);
  assert.equal(calls.reports[0].status, 'succeeded');
  assert.equal(calls.reports[0].message_id, 'image-result-sent');
});

test('tenant-bound keyword image is fetched, uploaded to FishMore CDN, and sent', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-keyword-image-'));
  const calls = { fetched: [], uploaded: [], sentImages: [], reports: [] };
  const client = {
    shops: { list: async () => [] },
    im: {
      listMessages: async () => ({ items: [] }),
      uploadImage: async (args) => {
        calls.uploaded.push(structuredClone({ ...args, data: [...args.data] }));
        return { imageUrl: 'https://img.alicdn.com/keyword.png', width: 320, height: 180 };
      },
      sendImage: async (args) => {
        calls.sentImages.push(structuredClone(args));
        return { messageId: 'keyword-image-sent' };
      },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [{
        id: 'keyword-image', type: 'send_image',
        image_asset_id: `ki-${'a'.repeat(40)}`, image_filename: 'keyword.png',
      }] } }),
      fetchKeywordImage: async (input) => {
        calls.fetched.push(structuredClone(input));
        return {
          data: new Uint8Array([0x89, 0x50, 0x4e, 0x47]),
          contentType: 'image/png', filename: 'keyword.png', sha256: 'hash-a',
        };
      },
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });

  await runtime.start();
  await runtime.enqueue(envelope('evt-keyword-image'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.deepEqual(calls.fetched, [{ tenantId: 'tenant-1', assetId: `ki-${'a'.repeat(40)}` }]);
  assert.equal(calls.uploaded[0].accountUnb, 'shop-1');
  assert.deepEqual(calls.uploaded[0].data, [0x89, 0x50, 0x4e, 0x47]);
  assert.equal(calls.sentImages[0].imageUrl, 'https://img.alicdn.com/keyword.png');
  assert.equal(calls.sentImages[0].chatId, 'chat-1');
  assert.equal(calls.reports[0].status, 'succeeded');
  assert.equal(calls.reports[0].message_id, 'keyword-image-sent');
});

test('keyword image CDN upload is reused after plugin restart', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-keyword-image-cache-'));
  const calls = { fetched: 0, uploaded: 0, sent: 0 };
  const client = {
    shops: { list: async () => [] },
    im: {
      listMessages: async () => ({ items: [] }),
      uploadImage: async () => {
        calls.uploaded += 1;
        return { imageUrl: 'https://img.alicdn.com/cached.png', width: 320, height: 180 };
      },
      sendImage: async () => { calls.sent += 1; return { messageId: `image-${calls.sent}` }; },
    },
  };
  const backend = {
    syncShops: async () => {},
    processEvent: async ({ envelope: current }) => ({ decision: { mode: 'auto', actions: [{
      id: `${current.id}:image`, type: 'send_image', rule_governed: true,
      image_asset_id: `ki-${'b'.repeat(40)}`, image_filename: 'cached.png',
    }] } }),
    fetchKeywordImage: async () => {
      calls.fetched += 1;
      return { data: new Uint8Array([1, 2, 3]), contentType: 'image/png', filename: 'cached.png', sha256: 'hash-b' };
    },
    reportAction: async () => ({}),
  };
  const config = { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 };

  for (const [index, eventId] of ['cache-first', 'cache-after-restart'].entries()) {
    const runtime = createV2Runtime({ config, platform: { createClient: () => client }, backend, logger: silent });
    await runtime.start();
    await runtime.enqueue(envelope(eventId));
    await waitFor(async () => (await runtime.health()).counts.completed >= index + 1);
    await runtime.stop();
  }

  assert.equal(calls.sent, 2);
  assert.equal(calls.fetched, 1);
  assert.equal(calls.uploaded, 1);
});

test('sendMessage provider id aliases are normalized into the action receipt', async () => {
  for (const [field, value] of [['message_id', 'snake-id'], ['id', 'plain-id']]) {
    const dataDir = await mkdtemp(join(tmpdir(), `wanda-ai-v2-message-id-${field}-`));
    const reports = [];
    const history = [{ id: 'buyer-1', direction: 'buyer', content: { text: 'hello' } }];
    const runtime = createV2Runtime({
      config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
      platform: { createClient: () => ({
        shops: { list: async () => [] },
        im: {
          listMessages: async () => ({ items: history }),
          sendMessage: async () => ({ [field]: value }),
        },
      }) },
      backend: {
        syncShops: async () => {},
        processEvent: async () => ({ decision: { mode: 'auto', actions: [{ id: `reply-${field}`, type: 'send_message', text: 'reply' }] } }),
        reportAction: async ({ result }) => { reports.push(structuredClone(result)); return {}; },
      },
      logger: silent,
    });
    await runtime.start();
    await runtime.enqueue(envelope(`evt-${field}`));
    await waitFor(async () => (await runtime.health()).counts.completed === 1);
    await runtime.stop();
    assert.equal(reports[0].message_id, value);
  }
});

test('an explicit platform 429 is reconciled against history and retried once', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-message-rate-limit-'));
  const calls = { sent: 0, reports: [] };
  const history = [{ id: 'buyer-1', direction: 'buyer', content: { text: 'hello' } }];
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => ({
      shops: { list: async () => [] },
      im: {
        listMessages: async () => ({ items: history }),
        sendMessage: async () => {
          calls.sent += 1;
          if (calls.sent === 1) throw Object.assign(new Error('rate limited'), { name: 'PluginApiError', status: 429, code: 'E_RATE_LIMITED' });
          return { messageId: 'retry-sent' };
        },
      },
    }) },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [{ id: 'reply-rate-limit', type: 'send_message', text: 'reply' }] } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });
  await runtime.start();
  await runtime.enqueue(envelope('evt-rate-limit'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.sent, 2);
  assert.equal(calls.reports[0].status, 'succeeded');
  assert.equal(calls.reports[0].message_id, 'retry-sent');
});

test('due viewing reminders send opening text and call the official receipt reminder idempotently', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-reminders-'));
  const calls = { sent: [], reminded: [], completed: [], failed: [] };
  let claimed = false;
  const session = { accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' };
  const client = {
    shops: { list: async () => [] },
    orders: {
      get: async () => providerOrder({ orderStatus: 3 }),
      remindReceipt: async (orderId, request) => { calls.reminded.push({ orderId, request }); return { alreadyReminded: false }; },
    },
    im: {
      getSessionByOrder: async () => session,
      listMessages: async () => ({ items: [] }),
      sendMessage: async ({ text: message }) => { calls.sent.push(message); return { messageId: 'reminder-message-1' }; },
    },
  };
  const backend = {
    claimReminders: async () => {
      if (claimed) return { tasks: [] };
      claimed = true;
      const common = { tenant_id: 'tenant-1', shop_id: 'shop-1', buyer_id: 'buyer-1', chat_id: 'chat-1', order_id: 'order-1' };
      return { tasks: [
        { ...common, task_id: 'pre-1', lease_token: 'lease-pre', kind: 'pre_show_text', message: '奥德赛即将开场', idempotency_key: 'reminder:pre-1' },
        { ...common, task_id: 'post-1', lease_token: 'lease-post', kind: 'post_show_receipt', idempotency_key: 'reminder:post-1' },
      ] };
    },
    completeReminder: async (taskId, leaseToken, result) => { calls.completed.push({ taskId, leaseToken, result }); },
    failReminder: async (...args) => { calls.failed.push(args); },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client }, backend, logger: silent,
  });

  const result = await runtime.pollReminders();

  assert.equal(result.processed, 2);
  assert.deepEqual(calls.sent, ['奥德赛即将开场']);
  assert.deepEqual(calls.reminded, [{ orderId: 'order-1', request: { idempotencyKey: 'reminder:post-1' } }]);
  assert.equal(calls.completed.length, 2);
  assert.deepEqual(calls.failed, []);
});

test('price change verifies order amount and sends the returned confirmation action', async () => {
  const { calls } = await priceChangeHarness();
  assert.equal(calls.changed, 1);
  assert.equal(calls.reports[0].status, 'succeeded');
  assert.equal(calls.reports[0].reason_code, 'price_change_verified');
  assert.equal(calls.reports[0].verified_amount_cents, 2_900);
  assert.equal(calls.reports[0].target_amount_cents, 2_900);
  assert.match(calls.reports[0].idempotency_key, /^price_change:v1:/);
  assert.equal(calls.sent, 1);
});

test('platform transaction cards do not suppress a verified price-change confirmation', async () => {
  const history = [];
  const { calls } = await priceChangeHarness({
    history,
    changePrice: async () => {
      history.push({
        id: 'transaction-card', direction: 'seller', messageType: 26,
        content: '订单价格已修改', sentAtMs: Date.now(),
      });
      return { ok: true };
    },
  });

  assert.equal(calls.changed, 1);
  assert.equal(calls.sent, 1);
  assert.equal(calls.reports.at(-1).status, 'succeeded');
});

test('a minimal order.created event is enriched from authoritative session and order ownership', async () => {
  const { calls } = await priceChangeHarness();
  assert.equal(calls.changed, 1);
  assert.equal(calls.reports[0].status, 'succeeded');
  assert.equal(calls.reports[0].reason_code, 'price_change_verified');
  assert.equal(calls.reports[0].verified_amount_cents, 2_900);
});

test('unverified-order guard binds missing buyer and chat aliases through the authoritative order session', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-order-guard-'));
  const calls = { sent: 0, reports: [] };
  const client = {
    shops: { list: async () => [] },
    orders: { get: async () => providerOrder({ buyerUnb: undefined, chatId: undefined }) },
    im: {
      getSessionByOrder: async () => ({ accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' }),
      listMessages: async () => ({ items: [] }),
      sendMessage: async () => { calls.sent += 1; return { messageId: 'guard-sent' }; },
    },
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [{
        id: 'guard-1', type: 'guard_unverified_order', order_id: 'order-1',
        unpaid_text: '请先不要付款。', paid_text: '请申请退款。',
      }] } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });

  await runtime.start();
  await runtime.enqueue(priceEnvelope('guard-event'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await runtime.stop();

  assert.equal(calls.sent, 1);
  assert.equal(calls.reports[0].status, 'succeeded');
  assert.equal(calls.reports[0].order_id, 'order-1');
});

test('price change requires a complete backend quote snapshot', async () => {
  const missingSnapshot = await priceChangeHarness({ action: { id: 'price-1', type: 'change_order_price' } });
  assert.equal(missingSnapshot.calls.changed, 0);
  assert.equal(missingSnapshot.calls.reports[0].status, 'skipped');
  assert.equal(missingSnapshot.calls.reports[0].reason_code, 'invalid_quote_snapshot');
});

test('paid and closed orders are skipped without changing price or sending confirmation', async (t) => {
  const cases = [
    ['paid', providerOrder({ payTime: '2026-08-12T01:00:00Z', orderStatus: 2 }), 'order_already_paid'],
    ['closed', providerOrder({ orderStatus: 'closed' }), 'order_paid_or_closed'],
  ];
  for (const [name, before, reasonCode] of cases) {
    await t.test(name, async () => {
      const { calls } = await priceChangeHarness({ before });
      assert.equal(calls.changed, 0);
      assert.equal(calls.reports[0].status, 'skipped');
      assert.equal(calls.reports[0].reason_code, reasonCode);
      assert.equal(calls.sent, 0);
    });
  }
});

test('verified paid amount mismatch cancels once and sends a recovery message after closed readback', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-paid-mismatch-'));
  const calls = { cancelled: 0, sent: 0, reports: [], cancelRequest: null };
  let closed = false;
  const client = {
    shops: { list: async () => [] },
    orders: {
      get: async () => providerOrder({
        chatId: 'chat-1', payment: 13_980,
        orderStatus: closed ? 'closed' : 2,
        payTime: closed ? '2026-08-25T06:15:51Z' : '2026-08-25T06:15:51Z',
      }),
      cancel: async (orderId, request) => {
        assert.equal(orderId, 'order-1');
        assert.match(request.reason, /金额/u);
        calls.cancelRequest = request;
        calls.cancelled += 1;
        closed = true;
        return { ok: true };
      },
    },
    im: {
      getSessionByOrder: async () => ({ accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' }),
      listMessages: async () => ({ items: [] }),
      sendMessage: async ({ text: message }) => { calls.sent += 1; assert.match(message, /订单已关闭/u); return { messageId: 'recovery-1' }; },
    },
  };
  const action = {
    id: 'paid-mismatch:cancel-paid-amount-mismatch', type: 'cancel_paid_amount_mismatch',
    order_id: 'order-1', target_amount_cents: 11_660,
    observed_order_amount_cents: 13_980, observed_amount_cents: 13_980,
    refund_authorization: 'unchanged_prechange_amount',
    closed_text: '订单已关闭，款项将原路退回。', refund_text: '请申请退款。',
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 7), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [action] } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });
  await runtime.start();
  await runtime.enqueue(priceEnvelope('paid-mismatch'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await waitFor(() => calls.reports.length === 1);
  await runtime.stop();

  assert.equal(calls.cancelled, 1);
  assert.equal(calls.cancelRequest.idempotencyKey, 'paid-mismatch:cancel-paid-amount-mismatch:cancel');
  assert.equal(calls.sent, 1);
  assert.equal(calls.reports[0].cancel_confirmed, true);
});

test('manual amount change blocks paid mismatch cancellation at the final write gate', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-manual-price-mismatch-'));
  const calls = { cancelled: 0, reports: [] };
  const client = {
    shops: { list: async () => [] },
    orders: {
      get: async () => providerOrder({
        chatId: 'chat-1', payment: 9_900, orderStatus: 2,
        payTime: '2026-08-25T06:15:51Z',
      }),
      cancel: async () => { calls.cancelled += 1; return { ok: true }; },
    },
    im: {
      getSessionByOrder: async () => ({ accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' }),
      listMessages: async () => ({ items: [] }),
      sendMessage: async () => ({ messageId: 'must-not-send' }),
    },
  };
  const action = {
    id: 'manual-mismatch:cancel-paid-amount-mismatch', type: 'cancel_paid_amount_mismatch',
    order_id: 'order-1', target_amount_cents: 11_660,
    observed_order_amount_cents: 13_980, observed_amount_cents: 9_900,
    refund_authorization: 'unchanged_prechange_amount',
    closed_text: '订单已关闭。', refund_text: '请申请退款。',
  };
  const runtime = createV2Runtime({
    config: { dataDir, encryptionKey: Buffer.alloc(32, 19), maxConcurrentRuns: 1 },
    platform: { createClient: () => client },
    backend: {
      syncShops: async () => {},
      processEvent: async () => ({ decision: { mode: 'auto', actions: [action] } }),
      reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
    },
    logger: silent,
  });

  await runtime.start();
  await runtime.enqueue(priceEnvelope('manual-mismatch'));
  await waitFor(async () => (await runtime.health()).counts.completed === 1);
  await waitFor(() => calls.reports.length === 1);
  await runtime.stop();

  assert.equal(calls.cancelled, 0);
  assert.equal(calls.reports[0].status, 'skipped');
  assert.equal(calls.reports[0].reason, 'manual_price_change_or_amount_unverified');
});

test('price change fails on a mismatched reread', async () => {
  const mismatch = await priceChangeHarness({ after: providerOrder({ payment: 2_800 }) });
  assert.equal(mismatch.calls.changed, 1);
  assert.equal(mismatch.calls.reports[0].status, 'failed');
  assert.equal(mismatch.calls.reports[0].reason_code, 'price_change_verification_failed');
  assert.equal(mismatch.calls.reports[0].verified_amount_cents, 2_800);
  assert.equal(mismatch.calls.sent, 0);
});

test('a delayed price change confirmation is not sent after the order becomes paid', async () => {
  const paidAfterChange = providerOrder({
    payment: 2_900,
    orderStatus: 2,
    payTime: '2026-08-12T08:00:00Z',
  });
  const { calls } = await priceChangeHarness({ after: paidAfterChange });
  assert.equal(calls.changed, 1);
  assert.equal(calls.reports[0].status, 'succeeded');
  assert.equal(calls.sent, 0);
  assert.equal(calls.reports[1].status, 'skipped');
  assert.equal(calls.reports[1].reason, 'price_change_confirmation_order_already_paid');
});

test('the backend client retries durable command results on 503 with the same idempotency key', async () => {
  const requests = [];
  const client = createV2BackendClient({
    requestTimeoutMs: 1_000,
    backend: {
      baseUrl: 'http://backend.test',
      sharedSecret: 'test-secret',
    },
  }, {
    logger: silent,
    fetchImpl: async (_url, init) => {
      requests.push({ headers: structuredClone(init.headers), body: init.body });
      return requests.length < 3
        ? new Response(JSON.stringify({ code: 'temporary_failure' }), { status: 503 })
        : new Response(JSON.stringify({ ok: true }), { status: 200 });
    },
  });

  await client.reportCommand({ commandId: 'command-1', leaseToken: 'lease-1', result: { status: 'succeeded' } });
  assert.equal(requests.length, 3);
  assert.equal(requests.every((item) => item.headers['idempotency-key'] === 'command-1'), true);
  assert.equal(requests.every((item) => item.body === requests[0].body), true);
});

test('duplicate price actions share the executor receipt and change price once', async () => {
  const { calls } = await priceChangeHarness({ eventIds: ['price-event-1', 'price-event-2'] });
  const priceReports = calls.reports.filter((result) => result.idempotency_key);
  assert.equal(calls.changed, 1);
  assert.equal(priceReports.length, 2);
  assert.equal(priceReports[1].status, 'succeeded');
  assert.equal(priceReports[1].deduplicated, true);
});

test('unknown price result re-enters after restart and only rereads the order', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-ai-v2-price-restart-'));
  const key = Buffer.alloc(32, 7);
  const calls = { changed: 0, reports: [], orderReads: 0 };
  const action = priceAction();
  const backend = {
    syncShops: async () => {},
    processEvent: async () => ({ decision: { mode: 'auto', actions: [structuredClone(action)] } }),
    reportAction: async ({ result }) => { calls.reports.push(structuredClone(result)); return {}; },
  };
  const platform = {
    createClient: () => ({
      shops: { list: async () => [] },
      orders: {
        get: async () => { calls.orderReads += 1; return providerOrder(); },
        changePrice: async () => {
          calls.changed += 1;
          throw Object.assign(new Error('timed out'), { name: 'TimeoutError' });
        },
      },
      im: {
        getSessionByOrder: async () => ({ accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' }),
        listMessages: async () => ({ items: [] }),
        sendMessage: async () => ({ messageId: 'unused' }),
      },
    }),
  };
  const config = { dataDir, encryptionKey: key, maxConcurrentRuns: 1 };
  const first = createV2Runtime({ config, platform, backend, logger: silent });
  await first.start();
  await first.enqueue(priceEnvelope('price-restart'));
  await waitFor(async () => (await first.health()).counts.completed === 1);
  await first.stop();
  assert.equal(calls.reports[0].reason_code, 'platform_result_unknown_after_readback');

  const statePath = join(dataDir, 'events.v2.json');
  const raw = JSON.parse(await readFile(statePath, 'utf8'));
  raw.events['tenant-1:price-restart'].status = 'processing';
  raw.events['tenant-1:price-restart'].lease = 'stale-lease';
  await writeFile(statePath, JSON.stringify(raw));

  const restarted = createV2Runtime({ config, platform, backend, logger: silent });
  await restarted.start();
  await waitFor(async () => calls.reports.length === 2);
  await restarted.stop();
  assert.equal(calls.changed, 1);
  assert.equal(calls.reports[1].status, 'unknown');
  assert.equal(calls.reports[1].reason_code, 'previous_price_change_result_unknown');
  assert.equal(calls.reports[1].deduplicated, true);
});

test('the V2 source tree contains no legacy bridge contract', async () => {
  const root = new URL('..', import.meta.url);
  const files = [
    new URL('../index.mjs', import.meta.url),
    new URL('../README.md', import.meta.url),
    new URL('../.env.example', import.meta.url),
    ...((await readdir(new URL('../src/', import.meta.url))).filter((name) => name.startsWith('v2-')).map((name) => new URL(`../src/${name}`, import.meta.url))),
  ];
  const text = await Promise.all(files.map((file) => readFile(file, 'utf8'))).then((items) => items.join('\n'));
  const forbidden = new RegExp([
    ['xianyu', 'plugin'].join('[_-]?'),
    ['plugin', 'bridge'].join('[_-]?'),
    `/api/${['xianyu', 'plugin'].join('-')}`,
  ].join('|'), 'i');
  assert.doesNotMatch(text, forbidden);
  assert.ok(root);
});

test('official registration and signed V2 webhooks are required before enqueueing', async (t) => {
  const config = loadV2Config({ env: {
    CORE_URL: 'https://core.test', PLUGIN_DEVELOPER_TOKEN: 'pdk_test', PLUGIN_BASE_URL: 'http://plugin.test:4003',
    WANDA_AI_V2_BACKEND_URL: 'http://backend.test', WANDA_AI_V2_BRIDGE_KEY: 'v2-secret',
    CONFIG_ENCRYPTION_KEY: Buffer.alloc(32, 3).toString('base64'), LOG_LEVEL: 'error',
  }, manifest });
  const sdk = {
    createPluginClient() { return {}; },
    verifyWebhookSignature({ secret, timestamp, signature, rawBody }) {
      return signature === createHmac('sha256', secret).update(`${timestamp}.${rawBody}`).digest('hex');
    },
  };
  const platform = createV2PlatformRuntime(config, {
    sdk,
    fetchImpl: async () => new Response(JSON.stringify({ data: { token: 'yp_v2_test', webhookSecret: '0123456789abcdef' } }), { status: 200 }),
    logger: silent,
  });
  await platform.register();
  const events = [];
  const server = createV2HttpServer({ config, platform, enqueue: async (value) => { events.push(value); return { created: true }; }, health: () => platform.health(), logger: silent });
  const address = await server.listen({ host: '127.0.0.1', port: 0 });
  t.after(() => server.close());
  const value = envelope('evt-signed');
  const raw = JSON.stringify(value);
  const timestamp = String(Date.now());
  const headers = { 'content-type': 'application/json', 'x-yumaiduo-timestamp': timestamp };
  const rejected = await fetch(`http://127.0.0.1:${address.port}/__plugin__/webhook/${value.event}`, { method: 'POST', headers: { ...headers, 'x-yumaiduo-signature': 'bad' }, body: raw });
  assert.equal(rejected.status, 401);
  const malformed = '{';
  const malformedSignature = createHmac('sha256', '0123456789abcdef').update(`${timestamp}.${malformed}`).digest('hex');
  const invalid = await fetch(`http://127.0.0.1:${address.port}/__plugin__/webhook/${value.event}`, {
    method: 'POST', headers: { ...headers, 'x-yumaiduo-signature': malformedSignature }, body: malformed,
  });
  assert.equal(invalid.status, 400);
  assert.deepEqual(await invalid.json(), { ok: false, error: 'invalid_json' });
  assert.deepEqual(events, []);
  const signature = createHmac('sha256', '0123456789abcdef').update(`${timestamp}.${raw}`).digest('hex');
  const accepted = await fetch(`http://127.0.0.1:${address.port}/__plugin__/webhook/${value.event}`, { method: 'POST', headers: { ...headers, 'x-yumaiduo-signature': signature }, body: raw });
  assert.equal(accepted.status, 202);
  assert.deepEqual(events, [value]);
});

test('Wanda fulfillment uses authoritative ticket facts and never ships one order twice', async (t) => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-fulfillment-runtime-'));
  let orderStatus = 2;
  let shipCalls = 0;
  let messageCalls = 0;
  const config = loadV2Config({ env: {
    CORE_URL: 'https://core.test', PLUGIN_DEVELOPER_TOKEN: 'pdk_test', PLUGIN_BASE_URL: 'http://plugin.test:4003',
    WANDA_AI_V2_BACKEND_URL: 'http://backend.test', WANDA_AI_V2_BRIDGE_KEY: 'v2-secret',
    CONFIG_ENCRYPTION_KEY: Buffer.alloc(32, 23).toString('base64'), LOG_LEVEL: 'error',
    WANDA_ORDER_FULFILLMENT_ENABLED: 'true', EXTERNAL_WRITES_ENABLED: 'true',
    MESSAGE_SEND_ENABLED: 'true', SHIP_ENABLED: 'true', WANDA_PROVIDER_WRITES_ENABLED: 'true',
  }, manifest });
  const platform = {
    createClient: () => ({
      orders: {
        get: async () => ({ orderId: 'order-1', tenantId: 'tenant-1', accountUnb: 'shop-1', buyerUnb: 'buyer-1', chatId: 'chat-1', orderStatus, payTime: '2026-08-30T10:00:00Z', payment: '7900', quantity: 1 }),
        ship: async (orderId, request) => { assert.equal(orderId, 'order-1'); assert.equal(request.ticketCode, 'WANDA-001'); shipCalls += 1; orderStatus = 3; return { ok: true }; },
      },
      im: {
        getSessionByOrder: async () => ({ accountUnb: 'shop-1', peerUnb: 'buyer-1', chatId: 'chat-1' }),
        listMessages: async () => ({ items: [] }),
        sendMessage: async (request) => { assert.match(request.text, /WANDA-001/u); messageCalls += 1; return { ok: true }; },
      },
    }),
    health: () => ({ ok: true }),
    verifyGateway: () => false,
    verifyWebhook: () => false,
  };
  const runtime = createRulesFirstRuntime({
    config, platform, backend: {}, logger: silent,
    store: new V2EventStore(join(dataDir, 'events.v2.json'), config.encryptionKey),
  });
  await runtime.start();
  t.after(() => runtime.stop());
  const input = {
    city: '昆明', movie_name: '奥德赛', cinema_name: '昆明西山万达广场店',
    showtime_start: '20:10', showtime_end: '22:50', hall_name: 'IMAX厅', seats: ['5排6座'],
    ticket_codes: ['WANDA-001'], message_text: '电影：奥德赛\n取票码：WANDA-001',
  };
  const first = await runtime.fulfillTenantOrder('tenant-1', 'order-1', input, 'fulfillment-1');
  assert.equal(first.status, 'submitted');
  assert.equal(shipCalls, 1);
  assert.equal(messageCalls, 1);
  const repeated = await runtime.fulfillTenantOrder('tenant-1', 'order-1', input, 'fulfillment-2');
  assert.equal(repeated.status, 'already_submitted');
  assert.equal(shipCalls, 1);
  assert.equal(messageCalls, 1);
  await assert.rejects(
    runtime.fulfillTenantOrder('tenant-1', 'order-1', { ...input, ticket_codes: ['WANDA-002'], message_text: '取票码：WANDA-002' }, 'fulfillment-3'),
    /wanda_fulfillment_conflict/u,
  );
});
