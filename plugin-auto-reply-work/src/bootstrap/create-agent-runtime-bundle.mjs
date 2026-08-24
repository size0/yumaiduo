import { createConversationAgentClient } from '../agent/conversation-agent-client.mjs';
import { createAiOrchestrator } from '../ai/ai-orchestrator.mjs';
import { createAgentHumanComparisonScanner } from '../agent/agent-human-comparison-scanner.mjs';
import { createAgentReplyOutboxDispatcher } from '../agent/agent-reply-outbox-dispatcher.mjs';
import { conversationProjectionVersion, createShadowAgentRuntime } from '../agent/shadow-agent-runtime.mjs';
import { createActionExecutor } from '../action-executor.mjs';
import { createReplyOrchestrator } from '../reply/reply-orchestrator.mjs';

const DEFAULT_FACTORIES = Object.freeze({
  createConversationAgentClient,
  createAiOrchestrator,
  createAgentHumanComparisonScanner,
  createAgentReplyOutboxDispatcher,
  createShadowAgentRuntime,
  createActionExecutor,
  createReplyOrchestrator,
});

/** Compose the advisory Agent runtime around deterministic stores and ports. */
export function createAgentRuntimeBundle({
  config,
  platformRuntime,
  storage,
  quotePreviewClient,
  getSettings,
  logger = console,
  factories: overrides = {},
} = {}) {
  if (typeof platformRuntime?.createClient !== 'function') throw new TypeError('platformRuntime.createClient is required');
  if (typeof getSettings !== 'function') throw new TypeError('getSettings is required');
  const requiredStores = [
    'eventStore', 'conversationContextStore', 'agentRunStore', 'agentReplyOutboxStore',
    'agentManualTaskStore', 'agentHumanComparisonStore',
  ];
  if (!storage || requiredStores.some((name) => !storage[name])) {
    throw new TypeError('Agent runtime storage dependencies are required');
  }
  const factories = { ...DEFAULT_FACTORIES, ...overrides };
  const coreFor = (tenantId) => platformRuntime.createClient(tenantId);
  const primaryProvider = factories.createConversationAgentClient(config);
  const conversationAgentPlanner = factories.createAiOrchestrator({ primaryProvider });

  const agentHumanComparisonScanner = factories.createAgentHumanComparisonScanner({
    conversationContextStore: storage.conversationContextStore,
    eventStore: storage.eventStore,
    agentRunStore: storage.agentRunStore,
    coreFor,
    comparisonStore: storage.agentHumanComparisonStore,
    logger,
  });
  const outboxExecutor = factories.createActionExecutor({
    coreFor,
    messageRegistry: storage.eventStore,
  });
  const outboxReplyOrchestrator = factories.createReplyOrchestrator({ actionExecutor: outboxExecutor });
  const agentReplyOutboxDispatcher = factories.createAgentReplyOutboxDispatcher({
    store: storage.agentReplyOutboxStore,
    executeReply: (action) => outboxReplyOrchestrator.deliver(action),
    commitDelivery: (entry) => commitQuoteDelivery(storage.conversationContextStore, entry),
    validateReply: async (entry) => {
      if (!entry.projection_version) return false;
      const state = await storage.conversationContextStore.get(entry.tenant_id, { accountUnb: entry.account_unb, chatId: entry.chat_id, peerUnb: entry.peer_unb });
      return conversationProjectionVersion(state?.facts) === entry.projection_version;
    },
    logger,
  });
  const shadowAgentRuntime = conversationAgentPlanner
    ? factories.createShadowAgentRuntime({
      runStore: storage.agentRunStore,
      eventStore: storage.eventStore,
      conversationContextStore: storage.conversationContextStore,
      planner: conversationAgentPlanner,
      getSettings,
      replyOutboxStore: storage.agentReplyOutboxStore,
      manualTaskStore: storage.agentManualTaskStore,
      coreFor,
      quotePreviewClient,
      executeAction: (action) => outboxExecutor.execute(action),
      requireNativeActive: true,
      logger,
    })
    : null;

  return Object.freeze({
    conversationAgentPlanner,
    agentHumanComparisonScanner,
    agentReplyOutboxDispatcher,
    shadowAgentRuntime,
  });
}

async function commitQuoteDelivery(conversationContextStore, entry) {
  const quote = entry?.delivery;
  if (quote?.type !== 'quote' || !entry?.platform_message_id) throw new TypeError('invalid quote delivery commit');
  await conversationContextStore.markQuoted(entry.tenant_id, {
    accountUnb: entry.account_unb,
    chatId: entry.chat_id,
    peerUnb: entry.peer_unb,
  }, {
    unitQuoteCents: quote.unit_quote_cents,
    totalQuoteCents: quote.total_quote_cents,
    ticketCount: quote.ticket_count,
    cinema: quote.cinema,
    movie: quote.movie,
    date: quote.date,
    showtime: quote.showtime,
    hall: quote.hall,
    quoteScope: quote.quote_scope,
    memberCostTotalCents: quote.member_cost_total_cents,
    originalPriceTotalCents: quote.original_price_total_cents,
    channelFeeTotalCents: quote.channel_fee_total_cents,
    pricingSource: quote.pricing_source,
    ...(quote.pricing_account_ref ? { pricingAccountRef: quote.pricing_account_ref } : {}),
    pricingRuleVersion: quote.pricing_rule_version,
    replyDelivered: true,
    deliveryActionId: entry.action_id,
    platformMessageId: entry.platform_message_id,
    circledDeliveryImageUrl: quote.circled_delivery_image_url,
  });
}
