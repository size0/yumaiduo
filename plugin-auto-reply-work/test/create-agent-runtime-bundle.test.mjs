import assert from 'node:assert/strict';
import test from 'node:test';
import { createAgentRuntimeBundle } from '../src/bootstrap/create-agent-runtime-bundle.mjs';

function dependencies() {
  const marks = [];
  return {
    marks,
    config: { conversationAgent: { url: 'https://agent.test/plan' } },
    platformRuntime: { createClient: (tenantId) => ({ tenantId }) },
    storage: {
      eventStore: { wasSentMessage: async () => false },
      conversationContextStore: { markQuoted: async (...args) => marks.push(args) },
      agentRunStore: {}, agentReplyOutboxStore: {}, agentManualTaskStore: {}, agentHumanComparisonStore: {},
    },
    quotePreviewClient: { quote: async () => ({}) },
    getSettings: async () => ({}),
  };
}

test('builds the provider-neutral Agent runtime without exposing provider capabilities', async () => {
  const input = dependencies();
  const captured = {};
  const primaryProvider = { plan: async () => ({}) };
  const planner = { plan: async () => ({}) };
  const scanner = { tick: async () => null };
  const dispatcher = { tick: async () => null };
  const runtime = { schedule: async () => null, tick: async () => null };
  const bundle = createAgentRuntimeBundle({
    ...input,
    factories: {
      createConversationAgentClient: () => primaryProvider,
      createDifyClient: () => { throw new Error('retired provider factory must not be called'); },
      createAiOrchestrator: (options) => { captured.ai = options; return planner; },
      createAgentHumanComparisonScanner: (options) => { captured.scanner = options; return scanner; },
      createActionExecutor: (options) => { captured.executor = options; return { execute: async () => ({}) }; },
      createReplyOrchestrator: (options) => { captured.reply = options; return { deliver: async () => ({}) }; },
      createAgentReplyOutboxDispatcher: (options) => { captured.dispatcher = options; return dispatcher; },
      createShadowAgentRuntime: (options) => { captured.runtime = options; return runtime; },
    },
  });

  assert.deepEqual(captured.ai, { primaryProvider });
  assert.equal(captured.runtime.planner, planner);
  assert.equal(captured.runtime.runStore, input.storage.agentRunStore);
  assert.equal(captured.runtime.replyOutboxStore, input.storage.agentReplyOutboxStore);
  assert.equal(captured.runtime.manualTaskStore, input.storage.agentManualTaskStore);
  assert.equal(captured.scanner.comparisonStore, input.storage.agentHumanComparisonStore);
  assert.equal(captured.dispatcher.store, input.storage.agentReplyOutboxStore);
  assert.equal(captured.executor.coreFor('tenant-a').tenantId, 'tenant-a');
  assert.deepEqual(await captured.dispatcher.executeReply({ kind: 'reply' }), {});
  assert.equal(bundle.conversationAgentPlanner, planner);
  assert.equal(bundle.agentHumanComparisonScanner, scanner);
  assert.equal(bundle.agentReplyOutboxDispatcher, dispatcher);
  assert.equal(bundle.shadowAgentRuntime, runtime);
  assert.equal(Object.isFrozen(bundle), true);
  assert.deepEqual(Object.keys(bundle).sort(), [
    'agentHumanComparisonScanner', 'agentReplyOutboxDispatcher', 'conversationAgentPlanner', 'shadowAgentRuntime',
  ]);
});

test('keeps quote outbox delivery as a two-phase commit into conversation facts', async () => {
  const input = dependencies();
  let dispatcherOptions;
  createAgentRuntimeBundle({
    ...input,
    factories: {
      createConversationAgentClient: () => null,
      createDifyClient: () => { throw new Error('retired provider factory must not be called'); },
      createAiOrchestrator: () => null,
      createAgentHumanComparisonScanner: () => ({ tick: async () => null }),
      createActionExecutor: () => ({ execute: async () => ({}) }),
      createReplyOrchestrator: () => ({ deliver: async () => ({}) }),
      createAgentReplyOutboxDispatcher: (options) => { dispatcherOptions = options; return { tick: async () => null }; },
      createShadowAgentRuntime: () => { throw new Error('disabled planner must not create a runtime'); },
    },
  });

  await dispatcherOptions.commitDelivery({
    tenant_id: 'tenant-1', account_unb: 'shop-1', chat_id: 'chat-1', peer_unb: 'buyer-1',
    action_id: 'reply-1', platform_message_id: 'platform-1',
    delivery: {
      type: 'quote', unit_quote_cents: 3000, total_quote_cents: 6000, ticket_count: 2,
      pricing_rule_version: 'rule-v1', cinema: '影院', movie: '影片', date: '2026-08-24',
      showtime: '20:00', hall: '1厅', quote_scope: 'exact_seats', member_cost_total_cents: 5000,
      original_price_total_cents: 8000, channel_fee_total_cents: 100, pricing_source: 'wanda',
      circled_delivery_image_url: 'https://image.test/seat.jpg',
    },
  });
  assert.deepEqual(input.marks, [[
    'tenant-1', { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' },
    {
      unitQuoteCents: 3000, totalQuoteCents: 6000, ticketCount: 2,
      cinema: '影院', movie: '影片', date: '2026-08-24', showtime: '20:00', hall: '1厅',
      quoteScope: 'exact_seats', memberCostTotalCents: 5000, originalPriceTotalCents: 8000,
      channelFeeTotalCents: 100, pricingSource: 'wanda', pricingRuleVersion: 'rule-v1',
      replyDelivered: true, deliveryActionId: 'reply-1', platformMessageId: 'platform-1',
      circledDeliveryImageUrl: 'https://image.test/seat.jpg',
    },
  ]]);
  await assert.rejects(
    () => dispatcherOptions.commitDelivery({ delivery: { type: 'quote' }, platform_message_id: '' }),
    /invalid quote delivery commit/u,
  );
});

test('fails closed when required runtime composition dependencies are missing', () => {
  const input = dependencies();
  assert.throws(() => createAgentRuntimeBundle({ ...input, platformRuntime: null }), /platformRuntime\.createClient/u);
  assert.throws(() => createAgentRuntimeBundle({ ...input, getSettings: null }), /getSettings/u);
  assert.throws(() => createAgentRuntimeBundle({ ...input, storage: {} }), /Agent runtime storage dependencies/u);
});
