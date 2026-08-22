import assert from 'node:assert/strict';
import test from 'node:test';
import { createAiOrchestrator } from '../src/ai/ai-orchestrator.mjs';

function validPlan(overrides = {}) {
  return {
    intent: '票价咨询',
    confidence: 0.96,
    goal: '读取当前报价',
    action: 'read_active_quote',
    arguments: {},
    missing_fields: [],
    reply: '',
    needs_human: false,
    reason: '已有有效报价',
    ...overrides,
  };
}

test('returns null when no primary AI provider is configured', () => {
  assert.equal(createAiOrchestrator({ primaryProvider: null }), null);
});

test('requires a provider with the planner contract', () => {
  assert.throws(() => createAiOrchestrator({ primaryProvider: {} }), /primary AI provider/u);
});

test('delegates a bounded source-time snapshot and returns a normalized typed plan', async () => {
  let received;
  const signal = new AbortController().signal;
  const orchestrator = createAiOrchestrator({
    primaryProvider: {
      async plan(input) {
        received = input;
        return validPlan({ goal: 'x'.repeat(400), reply: 'y'.repeat(800) });
      },
    },
  });
  const messages = Array.from({ length: 25 }, (_, index) => ({
    role: index % 2 ? 'seller' : 'buyer',
    text: `message-${index}`,
    source: index % 2 ? 'external_seller' : 'buyer',
    secret: 'must-not-leave-runtime',
  }));
  const observations = Array.from({ length: 10 }, (_, index) => ({
    status: 'success',
    tool: `tool-${index}`,
    summary: `summary-${index}`,
    facts: { quote_total_cents: 8_800, token: 'secret', _quote_delivery: { order_id: 'hidden' } },
    next_actions: ['respond'],
    authoritative_reply: 'must remain local',
  }));

  const plan = await orchestrator.plan({
    event_id: 'event-1',
    tenant_id: 'tenant-1',
    latest_message: '多少钱',
    has_image: false,
    state: {
      facts: {
        stage: 'quoted', quote_total_cents: 8_800, order_id: 'order-secret', api_key: 'secret',
        quote_draft: { fields: { cinema: { value: '北京万达影城' }, movie: '测试电影', token: 'hidden' } },
        recognition_draft: { cinema: '北京万达影城', selected: false, token: 'nested-secret', order_id: 'nested-order' },
        available_wplus_seats: ['7排7座', '7排8座'],
      },
      messages,
    },
    observations,
    settings: { api_key: 'secret', recognition_enabled: true },
    trace: [{ hidden: 'trace' }],
    signal,
  });

  assert.deepEqual(Object.keys(received).sort(), ['event_id', 'has_image', 'latest_message', 'observations', 'signal', 'state', 'tenant_id']);
  assert.equal(received.signal, signal);
  assert.equal(received.state.messages.length, 20);
  assert.equal(received.state.messages[0].content, 'message-5');
  assert.equal(received.state.messages[0].secret, undefined);
  assert.equal(received.state.facts.quote_total_cents, 8_800);
  assert.equal(received.state.facts.has_linked_order, true);
  assert.equal(received.state.facts.order_id, undefined);
  assert.equal(received.state.facts.api_key, undefined);
  assert.deepEqual(received.state.facts.quote_draft, { fields: { cinema: '北京万达影城', movie: '测试电影' } });
  assert.deepEqual(received.state.facts.recognition_draft, { cinema: '北京万达影城', selected: false });
  assert.deepEqual(received.state.facts.available_wplus_seats, ['7排7座', '7排8座']);
  assert.equal(received.observations.length, 8);
  assert.equal(received.observations[0].tool, 'tool-2');
  assert.deepEqual(received.observations[0].facts, { quote_total_cents: 8_800 });
  assert.equal(received.observations[0].authoritative_reply, undefined);
  assert.equal(plan.goal.length, 160);
  assert.equal(plan.reply.length, 500);
  assert.equal(Object.isFrozen(plan), true);
});

test('planner input is immutable and provider mutation cannot affect caller state', async () => {
  const source = {
    event_id: 'event-1', tenant_id: 'tenant-1', latest_message: '这个呢', has_image: false,
    state: { facts: { stage: 'quoted', quote_total_cents: 5_300 }, messages: [{ role: 'buyer', text: '这个呢' }] },
    observations: [],
  };
  const orchestrator = createAiOrchestrator({
    primaryProvider: {
      async plan(input) {
        assert.equal(Object.isFrozen(input), true);
        assert.equal(Object.isFrozen(input.state), true);
        assert.equal(Object.isFrozen(input.state.facts), true);
        assert.throws(() => { input.state.facts.stage = 'paid'; }, TypeError);
        return validPlan();
      },
    },
  });
  await orchestrator.plan(source);
  assert.equal(source.state.facts.stage, 'quoted');
});

test('provider errors and aborts propagate to the durable Agent runtime', async () => {
  const failure = new Error('provider unavailable');
  const orchestrator = createAiOrchestrator({ primaryProvider: { async plan() { throw failure; } } });
  await assert.rejects(() => orchestrator.plan({ latest_message: '你好', state: {}, observations: [] }), failure);
});

test('invalid or transaction-parameterized provider plans fail at the AI boundary', async () => {
  const malformed = createAiOrchestrator({ primaryProvider: { async plan() { return { action: 'respond' }; } } });
  await assert.rejects(() => malformed.plan({}), /unsupported agent intent/u);

  const unsafe = createAiOrchestrator({
    primaryProvider: { async plan() { return validPlan({ action: 'request_price_change', arguments: { amount_cents: 8_800 } }); } },
  });
  await assert.rejects(() => unsafe.plan({}), /forbidden agent argument/u);
});

test('the AI boundary exposes planning only and has no send or tool execution capability', () => {
  const orchestrator = createAiOrchestrator({ primaryProvider: { async plan() { return validPlan(); } } });
  assert.deepEqual(Object.keys(orchestrator), ['plan']);
  assert.equal(orchestrator.send, undefined);
  assert.equal(orchestrator.executeTool, undefined);
});
