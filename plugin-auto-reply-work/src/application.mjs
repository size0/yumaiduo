import path from 'node:path';
import { FileEventStore } from './event-store.mjs';
import { ConversationContextStore } from './conversation-context-store.mjs';
import { AgentEvaluationStore, agentCanaryReadinessFrom, automatedAgentSafetyReviewFrom } from './agent-evaluation-store.mjs';
import { createImageLoader } from './image-loader.mjs';
import { createUiHandler } from './ui-handler.mjs';
import { createQuotePreviewClient } from './quote-preview-client.mjs';
import { createReplyPreviewClient } from './reply-preview-client.mjs';
import { createConversationAgentClient } from './agent/conversation-agent-client.mjs';
import { AgentRunStore } from './agent/agent-run-store.mjs';
import { AgentReplyOutboxStore } from './agent/agent-reply-outbox-store.mjs';
import { AgentManualTaskStore } from './agent/agent-manual-task-store.mjs';
import { agentTraceReplayFrom } from './agent/agent-trace-replay.mjs';
import { AgentHumanComparisonStore } from './agent/agent-human-comparison-store.mjs';
import { createAgentHumanComparisonScanner } from './agent/agent-human-comparison-scanner.mjs';
import { agentImageOfflineEvaluationFrom } from './agent/agent-offline-evaluator.mjs';
import { createAgentReplyOutboxDispatcher } from './agent/agent-reply-outbox-dispatcher.mjs';
import { AGENT_RUNTIME_VERSION, createShadowAgentRuntime } from './agent/shadow-agent-runtime.mjs';
import { createActionExecutor } from './action-executor.mjs';
import { createWorkflow } from './workflow.mjs';
import { hasUnresolvedReplyPlaceholder } from './agent/response-composer.mjs';

const QUOTE_POLICY_FIELDS = Object.freeze([
  'wplus_adjustment_cents',
  'wplus_member_price_threshold_cents',
  'regular_adjustment_cents',
  'max_auto_order_amount_cents',
]);

export async function createApplication({ config, platformRuntime, backendClient, logger = console }) {
  const eventStore = new FileEventStore(path.join(config.dataDir, 'events.json'), {
    encryptionKey: config.configEncryptionKey,
    retentionMs: config.eventRetentionDays * 24 * 60 * 60 * 1_000,
  });
  const conversationContextStore = new ConversationContextStore(path.join(config.dataDir, 'conversation-context.json'));
  const agentEvaluationStore = new AgentEvaluationStore(path.join(config.dataDir, 'agent-evaluations.json')); 
  const imageLoader = createImageLoader({ allowlist: config.imageHostAllowlist });
  const quotePreviewClient = createQuotePreviewClient(config);
  const replyPreviewClient = createReplyPreviewClient(config);
  const conversationAgentPlanner = createConversationAgentClient(config);
  const agentRunStore = new AgentRunStore(path.join(config.dataDir, 'agent-runs.json'));
  const agentReplyOutboxStore = new AgentReplyOutboxStore(path.join(config.dataDir, 'agent-reply-outbox.json'));
  const agentManualTaskStore = new AgentManualTaskStore(path.join(config.dataDir, 'agent-manual-tasks.json'));
  const agentHumanComparisonStore = new AgentHumanComparisonStore(path.join(config.dataDir, 'agent-human-comparisons.json'));
  const agentHumanComparisonScanner = createAgentHumanComparisonScanner({
    conversationContextStore, eventStore, agentRunStore,
    coreFor: (tenantId) => platformRuntime.createClient(tenantId), comparisonStore: agentHumanComparisonStore, logger,
  });
  const agentOutboxExecutor = createActionExecutor({
    coreFor: (tenantId) => platformRuntime.createClient(tenantId),
    messageRegistry: eventStore,
  });
  const agentReplyOutboxDispatcher = createAgentReplyOutboxDispatcher({
    store: agentReplyOutboxStore,
    executeReply: (action) => agentOutboxExecutor.execute(action),
    commitDelivery: async (entry) => {
      const quote = entry.delivery;
      if (quote?.type !== 'quote' || !entry.platform_message_id) throw new TypeError('invalid quote delivery commit');
      await conversationContextStore.markQuoted(entry.tenant_id, {
        accountUnb: entry.account_unb, chatId: entry.chat_id, peerUnb: entry.peer_unb,
      }, {
        unitQuoteCents: quote.unit_quote_cents, totalQuoteCents: quote.total_quote_cents, ticketCount: quote.ticket_count,
        cinema: quote.cinema, movie: quote.movie, date: quote.date, showtime: quote.showtime, hall: quote.hall,
        quoteScope: quote.quote_scope, memberCostTotalCents: quote.member_cost_total_cents,
        originalPriceTotalCents: quote.original_price_total_cents, channelFeeTotalCents: quote.channel_fee_total_cents, pricingSource: quote.pricing_source,
        pricingRuleVersion: quote.pricing_rule_version, replyDelivered: true,
        deliveryActionId: entry.action_id, platformMessageId: entry.platform_message_id,
        circledDeliveryImageUrl: quote.circled_delivery_image_url,
      });
    },
    logger,
  });
  const shadowAgentRuntime = conversationAgentPlanner ? createShadowAgentRuntime({
    runStore: agentRunStore, eventStore, conversationContextStore, planner: conversationAgentPlanner, getSettings,
    replyOutboxStore: agentReplyOutboxStore, manualTaskStore: agentManualTaskStore,
    coreFor: (tenantId) => platformRuntime.createClient(tenantId), quotePreviewClient, logger,
  }) : null;
  const workflow = createWorkflow({
    backend: backendClient,
    coreFor: (tenantId) => platformRuntime.createClient(tenantId),
    eventStore,
    conversationContextStore,
    imageLoader,
    quotePreviewClient,
    replyPreviewClient,
    conversationAgentPlanner,
    shadowAgentScheduler: shadowAgentRuntime,
    manualTaskStore: agentManualTaskStore,
    autoReplyEnabled: config.replyAutoSendEnabled,
    logger,
  });
  let timer = null;
  let agentTimer = null;
  let agentOutboxTimer = null;
  let humanComparisonTimer = null;
  let historicalEvaluationTimer = null;
  let humanComparisonRunning = false;
  let historicalEvaluationRunning = false;
  let workerPool = null;
  let agentWorkerPool = null;
  let agentOutboxWorkerPool = null;
  const orderDisplayCache = new Map();
  const shopDisplayCache = new Map();

  async function start() {
    await Promise.all([eventStore.initialize(), agentRunStore.initialize(), agentReplyOutboxStore.initialize(), agentManualTaskStore.initialize()]);
    workerPool = createWorkerPool(workflow, { concurrency: 4, logger });
    timer = setInterval(() => workerPool?.poll(), config.workerIntervalMs);
    timer.unref();
    workerPool.poll();
    if (shadowAgentRuntime) {
      agentWorkerPool = createWorkerPool(shadowAgentRuntime, { concurrency: 1, logger });
      agentTimer = setInterval(() => agentWorkerPool?.poll(), Math.max(500, config.workerIntervalMs));
      agentTimer.unref();
      agentWorkerPool.poll();
    }
    agentOutboxWorkerPool = createWorkerPool(agentReplyOutboxDispatcher, { concurrency: 1, logger });
    agentOutboxTimer = setInterval(() => agentOutboxWorkerPool?.poll(), Math.max(500, config.workerIntervalMs));
    agentOutboxTimer.unref();
    agentOutboxWorkerPool.poll();
    const scheduleHistoricalEvaluations = async () => {
      if (!shadowAgentRuntime || historicalEvaluationRunning) return;
      historicalEvaluationRunning = true;
      try {
        const runs = await agentRunStore.list({ limit: 500 });
        const eventKeys = historicalEvaluationCandidatesFrom(runs, { runtimeVersion: AGENT_RUNTIME_VERSION });
        if (!eventKeys.length) return;
        const events = typeof eventStore.getMany === 'function' ? await eventStore.getMany(eventKeys) : [];
        for (const event of events) {
          if (event?.status === 'completed' && event.envelope?.event === 'im.message.received') {
            await shadowAgentRuntime.schedule(event.envelope, { mode: 'evaluation' });
          }
        }
        agentWorkerPool?.poll();
      } catch (error) {
        logger.warn?.('[agent-evaluation] historical replay scheduling failed', { error: String(error?.message ?? error) });
      } finally { historicalEvaluationRunning = false; }
    };
    historicalEvaluationTimer = setInterval(scheduleHistoricalEvaluations, 15_000);
    historicalEvaluationTimer.unref();
    void scheduleHistoricalEvaluations();
    const scanHumanComparisons = async () => {
      if (humanComparisonRunning) return;
      humanComparisonRunning = true;
      try { await agentHumanComparisonScanner.tick(); }
      catch (error) { logger.warn?.('[human-comparison] background scan failed', { error: String(error?.message ?? error) }); }
      finally { humanComparisonRunning = false; }
    };
    humanComparisonTimer = setInterval(scanHumanComparisons, 30_000);
    humanComparisonTimer.unref();
  }

  async function stop() {
    if (timer) clearInterval(timer);
    if (agentTimer) clearInterval(agentTimer);
    if (agentOutboxTimer) clearInterval(agentOutboxTimer);
    if (humanComparisonTimer) clearInterval(humanComparisonTimer);
    if (historicalEvaluationTimer) clearInterval(historicalEvaluationTimer);
    timer = null; agentTimer = null; agentOutboxTimer = null; humanComparisonTimer = null; historicalEvaluationTimer = null;
    shadowAgentRuntime?.stop?.();
    const pool = workerPool; const agentPool = agentWorkerPool; const outboxPool = agentOutboxWorkerPool;
    workerPool = null; agentWorkerPool = null; agentOutboxWorkerPool = null;
    await Promise.all([pool?.stop(), agentPool?.stop(), outboxPool?.stop()]);
  }

  async function health() {
    const [queue, agentQueue, agentOutbox, manualTasks, humanComparisons] = await Promise.all([eventStore.health(), agentRunStore.health(), agentReplyOutboxStore.health(), agentManualTaskStore.health(), agentHumanComparisonStore.health()]);
    return {
      ok: true,
      worker: timer ? 'running' : 'stopped',
      agent_worker: agentTimer ? 'running' : 'stopped',
      agent_outbox_worker: agentOutboxTimer ? 'running' : 'stopped',
      queue,
      agent_queue: agentQueue,
      agent_outbox: agentOutbox,
      manual_tasks: manualTasks,
      historical_evaluation_worker: historicalEvaluationTimer ? 'running' : 'stopped',
      human_comparison_worker: humanComparisonTimer ? 'running' : 'stopped',
      human_comparisons: humanComparisons,
    };
  }

  async function overview(tenantId) {
    await syncShopDirectory(tenantId);
    const [settings, allEvents, queue, openManualTasks] = await Promise.all([
      getSettings(tenantId),
      eventStore.list({ limit: 200 }),
      eventStore.health(),
      agentManualTaskStore.list({ tenantId, status: 'open', limit: 100 }),
    ]);
    const events = allEvents.filter((item) => String(item.envelope?.tenantId) === String(tenantId));
    const completed = events.filter((item) => item.status === 'completed').length;
    const review = events.filter((item) => ['failed', 'unknown'].includes(item.status)).length + openManualTasks.length;
    return {
      plugin: {
        name: '万达电影票 AI 客服',
        status: settings.automation_enabled ? 'running' : 'paused',
        updated_at: new Date().toISOString(),
      },
      metrics: {
        processed: events.length,
        completed,
        review,
        success_rate: events.length ? Number(((completed / events.length) * 100).toFixed(1)) : null,
      },
      services: {
        plugin_runtime: 'healthy',
        wanda_backend: 'configured',
        multimodal_model: 'configured',
        yumaiduo_events: 'subscribed',
      },
      settings,
      queue: queue.eventCounts,
      recent_records: events.slice(0, 20).map(toRecentRecord),
      review_records: [
        ...openManualTasks.map(manualTaskReviewRecord),
        ...events.filter((item) => ['failed', 'unknown'].includes(item.status)).map(toRecentRecord),
      ].sort((left, right) => String(right.updated_at).localeCompare(String(left.updated_at))).slice(0, 50),
    };
  }

  async function getSettings(tenantId) {
    const runtimeResponse = await backendClient.getRuntimeSettings();
    const runtime = runtimeResponse?.settings;
    if (!runtime || typeof runtime !== 'object' || Array.isArray(runtime)) {
      throw new Error('backend runtime settings response is invalid');
    }

    const policyResponse = await backendClient.getQuotePolicy(tenantId);
    const policy = policyResponse?.policy;
    if (!policy || typeof policy !== 'object' || Array.isArray(policy)) {
      throw new Error('backend quote policy response is invalid');
    }
    return Object.freeze({ ...runtimeToUiSettings(runtime), ...policy });
  }

  async function updateSettings(tenantId, patch) {
    const policyPatch = Object.fromEntries(
      QUOTE_POLICY_FIELDS.filter((field) => Object.hasOwn(patch, field)).map((field) => [field, patch[field]]),
    );
    const [runtimeResponse, policyResponse] = await Promise.all([
      backendClient.updateRuntimeSettings(runtimePatchFromUi(patch)),
      Object.keys(policyPatch).length
        ? backendClient.updateQuotePolicy(tenantId, policyPatch)
        : backendClient.getQuotePolicy(tenantId),
    ]);
    const runtime = runtimeResponse?.settings;
    if (!runtime || typeof runtime !== 'object' || Array.isArray(runtime)) {
      throw new Error('backend runtime settings response is invalid');
    }
    const policy = policyResponse?.policy;
    if (!policy || typeof policy !== 'object' || Array.isArray(policy)) {
      throw new Error('backend quote policy response is invalid');
    }
    return Object.freeze({ ...runtimeToUiSettings(runtime), ...policy });
  }

  async function uploadReplyTemplateImage(tenantId, input) {
    const key = compactText(input?.key, 80);
    const contentType = compactText(input?.content_type, 80).toLowerCase();
    const dataBase64 = String(input?.data_base64 ?? '').trim();
    if (!/^[a-z0-9_]{1,80}$/u.test(key) || !['image/png', 'image/jpeg', 'image/webp'].includes(contentType)) {
      throw uiApiError(422, 'invalid_reply_template_image');
    }
    if (!dataBase64 || dataBase64.length > 7 * 1024 * 1024 || !/^[A-Za-z0-9+/]+={0,2}$/u.test(dataBase64)) {
      throw uiApiError(422, 'invalid_reply_template_image');
    }
    const bytes = Buffer.from(dataBase64, 'base64');
    if (bytes.length < 12 || bytes.length > 5 * 1024 * 1024 || !imageSignatureMatches(bytes, contentType)) {
      throw uiApiError(422, 'invalid_reply_template_image');
    }
    const runtimeResponse = await backendClient.getRuntimeSettings();
    const runtime = runtimeResponse?.settings;
    if (!runtime?.reply_templates || !Object.hasOwn(runtime.reply_templates, key)) throw uiApiError(422, 'invalid_reply_template_key');
    const uploadReplyImage = backendClient.uploadReplyImage ?? backendClient.uploadTestImage;
    const uploaded = await uploadReplyImage({
      bytes,
      contentType,
      filename: compactText(input?.filename, 160) || `reply-${key}`,
    }, { tenantId, eventId: `reply-template-image:${key}` });
    const imageUrl = String(uploaded?.url ?? '').trim();
    if (!/^https:\/\//iu.test(imageUrl)) throw new Error('backend image upload response is invalid');
    const images = Object.fromEntries(Object.keys(runtime.reply_templates).map((templateKey) => [
      templateKey,
      templateKey === key ? imageUrl : String(runtime.reply_template_images?.[templateKey] ?? '').trim(),
    ]));
    await backendClient.updateRuntimeSettings({ reply_template_images: images });
    return Object.freeze({ key, image_url: imageUrl });
  }

  async function syncShopDirectory(tenantId) {
    if (typeof backendClient.syncShops !== 'function') return;
    try {
      const shops = await platformRuntime.createClient(tenantId).shops.list();
      if (!Array.isArray(shops)) throw new TypeError('platform shop directory response is invalid');
      await backendClient.syncShops(tenantId, shops
        .map((shop) => ({
          account_unb: String(shop?.unb ?? '').trim(),
          shop_name: String(shop?.shopName ?? shop?.displayName ?? '').trim(),
        }))
        .filter((shop) => shop.account_unb));
    } catch (error) {
      logger.warn?.('shop directory sync failed', {
        tenantId: String(tenantId),
        error: String(error?.message ?? error),
      });
    }
  }

  async function listOperations(tenantId) {
    const [operations, events] = await Promise.all([
      conversationContextStore.listOperations(tenantId),
      eventStore.list({ limit: 200 }),
    ]);
    const tenantEvents = events.filter((item) => String(item.envelope?.tenantId) === String(tenantId));
    return operations.map((operation) => {
      const relatedEvents = tenantEvents.filter((item) => matchesOperationEvent(operation, item));
      const buyerName = relatedEvents
        .map((item) => safeBuyerName(item.envelope?.payload))
        .find(Boolean);
      return Object.freeze({
        ...operation,
        // A stored `买家 <peerUnb>` label is a meaningful, stable fallback;
        // only replace it when an event carries an actual nickname.
        buyer_label: buyerName || operation.buyer_label || `买家 ${String(operation.peer_unb ?? '').trim() || 'ID 未知'}`,
        activity: relatedEvents.slice(0, 8).map(toOperationActivity),
      });
    });
  }

  async function listQuoteAnalytics(tenantId) {
    const [records, settings, shopNames] = await Promise.all([
      conversationContextStore.listQuoteRecords(tenantId),
      getSettings(tenantId),
      loadShopNames(tenantId),
    ]);
    const paid = records.filter((record) => record.paid_succeeded_at).length;
    const safeShopNames = shopNames instanceof Map ? shopNames : new Map();
    return Object.freeze({
      records: records.map((record) => Object.freeze({ ...record, shop_name: safeShopNames.get(record.account_unb) ?? '' })),
      summary: Object.freeze({
        sample_size: records.length,
        paid_success_count: paid,
        paid_success_rate: records.length ? Number(((paid / records.length) * 100).toFixed(1)) : null,
        minimum_pricing_evaluation_samples: 100,
        pricing_evaluation_status: records.length >= 100 ? 'eligible_for_manual_shadow_evaluation' : 'collecting_statistics',
        formula: pricingFormulaSummary(settings),
        prohibited_features: ['买家身份', '历史议价能力', '聊天风格', '支付意愿'],
      }),
    });
  }

  async function listTicketOrders(tenantId) {
    const [records, shopNames] = await Promise.all([
      conversationContextStore.listTicketOrders(tenantId),
      loadShopNames(tenantId),
    ]);
    const core = platformRuntime.createClient(tenantId);
    const safeShopNames = shopNames instanceof Map ? shopNames : new Map();
    return mapWithConcurrency(records, 8, async (record) => {
      let platform = { read_status: 'unavailable' };
      if (record.order_id && typeof core.orders?.get === 'function') {
        const cacheKey = `${tenantId}:${record.order_id}`;
        const cached = orderDisplayCache.get(cacheKey);
        if (cached && cached.expires_at > Date.now()) {
          platform = cached.platform;
        } else {
          try {
            const order = await core.orders.get(record.order_id);
            platform = {
              read_status: 'available',
              buyer_name: compactText(order?.buyerNick, 80),
              order_status: Number.isInteger(Number(order?.orderStatus)) ? Number(order.orderStatus) : null,
              order_status_text: compactText(order?.orderStatusText, 120),
              payment_cents: nonNegativeCentsOrNull(order?.payment),
              pay_time: compactText(order?.payTime, 64),
              create_time: compactText(order?.createTime, 64),
            };
            orderDisplayCache.set(cacheKey, { platform, expires_at: Date.now() + 60_000 });
          } catch {
            // Keep the plugin event stage visible when the platform mirror is unavailable.
          }
        }
      }
      const ticketIssuance = ticketIssuanceFromXianyuOrder(platform);
      return Object.freeze({
        ...record,
        buyer_label: platform.buyer_name || record.buyer_label,
        shop_name: safeShopNames.get(record.account_unb) ?? '',
        platform_read_status: platform.read_status,
        platform_order_status: platform.order_status ?? null,
        platform_order_status_text: platform.order_status_text ?? '',
        platform_payment_cents: platform.payment_cents ?? null,
        platform_pay_time: platform.pay_time ?? '',
        platform_create_time: platform.create_time ?? '',
        platform_ticket_status: ticketIssuance.status,
        platform_ticket_evidence: ticketIssuance.evidence,
      });
    });
  }

  async function loadShopNames(tenantId) {
    const key = String(tenantId);
    const cached = shopDisplayCache.get(key);
    if (cached?.promise) return cached.promise;
    if (cached?.names instanceof Map && cached.expires_at > Date.now()) return cached.names;
    const promise = (async () => {
      try {
        const shops = await platformRuntime.createClient(tenantId).shops.list();
        const names = new Map((Array.isArray(shops) ? shops : []).map((shop) => [
          compactText(shop?.unb, 128),
          compactText(shop?.shopName ?? shop?.displayName, 160),
        ]).filter(([account]) => account));
        shopDisplayCache.set(key, { names, expires_at: Date.now() + 30_000 });
        return names;
      } catch {
        const names = new Map();
        shopDisplayCache.set(key, { names, expires_at: Date.now() + 5_000 });
        return names;
      }
    })();
    shopDisplayCache.set(key, { promise, expires_at: Date.now() + 30_000 });
    return promise;
  }

  async function listOwnedShops(tenantId) {
    const shops = await platformRuntime.createClient(tenantId).shops.list();
    if (!Array.isArray(shops)) throw new Error('platform shop directory response is invalid');
    const ownedShops = shops.map((shop) => ({
      account_unb: compactText(shop?.unb, 128),
      shop_name: compactText(shop?.shopName ?? shop?.displayName, 160),
    })).filter((shop) => shop.account_unb);
    return Promise.all(ownedShops.map(async (shop) => {
      const response = await backendClient.getRuntimeSettings(shop.account_unb);
      if (typeof response?.settings?.shop_enabled !== 'boolean') {
        throw new Error('backend shop settings response is invalid');
      }
      return Object.freeze({ ...shop, automation_enabled: response.settings.shop_enabled });
    }));
  }

  async function updateShopEnabled(tenantId, accountUnb, automationEnabled) {
    const account = compactText(accountUnb, 128);
    if (!account || typeof automationEnabled !== 'boolean') throw uiApiError(400, 'invalid_shop_settings');
    const shops = await platformRuntime.createClient(tenantId).shops.list();
    if (!Array.isArray(shops) || !shops.some((shop) => compactText(shop?.unb, 128) === account)) {
      throw uiApiError(404, 'shop_not_found');
    }
    const response = await backendClient.updateShopSettings(account, automationEnabled);
    if (typeof response?.settings?.shop_enabled !== 'boolean') {
      throw new Error('backend shop settings response is invalid');
    }
    return Object.freeze({ account_unb: account, automation_enabled: response.settings.shop_enabled });
  }

  async function listKnowledgeBase(tenantId) {
    const response = await backendClient.listKnowledgeBase(tenantId);
    if (!Array.isArray(response?.entries)) throw new Error('backend knowledge base response is invalid');
    return response.entries;
  }
  async function listAgentEvaluations(tenantId) {
    const [recentEvents, runs, reviews] = await Promise.all([
      eventStore.list({ limit: 500 }), agentRunStore.list({ tenantId, limit: 500 }), agentEvaluationStore.list(tenantId),
    ]);
    const runEvents = typeof eventStore.getMany === 'function' ? await eventStore.getMany(runs.map((run) => run.event_key)) : [];
    const events = [...recentEvents, ...runEvents];
    const eventByKey = new Map(events.map((item) => [String(item.key), item]));
    const reviewByEvent = new Map(reviews.map((item) => [item.event_id, item]));
    const uniqueRuns = uniqueAgentEvaluationRuns(runs, { runtimeVersion: AGENT_RUNTIME_VERSION });
    const durable = uniqueRuns.map((run) => {
        const event = eventByKey.get(run.event_key); if (!event) return null;
        const first = run.result.trace[0] ?? {}; const payload = event.envelope?.payload ?? {};
        const actualAction = String(first.action ?? '');
        const proposedReply = String(run.result?.proposed_reply ?? '').trim();
        const authoritativeOutcome = run.result.authoritative_outcome ?? 'not_available';
        const finalReplySafe = run.result?.status === 'reply' && run.result?.reply_generated === true
          && proposedReply.length > 0 && !hasUnresolvedReplyPlaceholder(proposedReply)
          && (authoritativeOutcome !== 'quote_succeeded' || run.result?.authoritative_reply_used === true);
        const sourceAuthoritativeOutcome = event.result?.preview_status === 'preview_ready'
          ? 'quote_succeeded' : event.result?.quote_failure_code ? 'quote_failed' : 'not_available';
        const automatedReview = automatedAgentSafetyReviewFrom({
          actual_action: actualAction, run_status: run.status, result_status: run.result?.status,
          reason: run.result?.reason, tool_names: (run.tool_calls ?? []).map((call) => String(call?.tool ?? '')),
          authoritative_outcome: authoritativeOutcome, authoritative_consistent: authoritativeOutcome === sourceAuthoritativeOutcome,
          final_reply_safe: finalReplySafe, reply_generated: run.result?.reply_generated === true,
        });
        return Object.freeze({
          event_id: run.event_key, run_id: run.run_id, run_mode: run.mode, run_status: run.status,
          time: run.updated_at, runtime_version: String(run.result?.runtime_version ?? 'legacy'), account_unb: String(payload.accountUnb ?? ''), chat_id: String(payload.chatId ?? ''), peer_unb: String(payload.peerUnb ?? ''),
          buyer_label: agentEvaluationBuyerLabel(payload), turn_summary: agentEvaluationTurnSummary(payload),
          intent: String(first.intent ?? ''), confidence: first.confidence, actual_action: actualAction,
          suggested_ask: actualAction.startsWith('ask_for_'), suggested_handoff: actualAction === 'handoff',
          authoritative_outcome: authoritativeOutcome, authoritative_summary: agentEvaluationAuthoritativeSummary(event.result),
          final_reply_safe: finalReplySafe, review: automatedReview, manual_review: reviewByEvent.get(run.event_key) ?? null,
        });
      }).filter(Boolean);
    const durableKeys = new Set(durable.map((item) => item.event_id));
    const legacy = events.filter((item) => !durableKeys.has(String(item.key)) && String(item.envelope?.tenantId) === String(tenantId)
      && item.result?.conversation_agent_mode === 'shadow' && Array.isArray(item.result?.agent_actions) && item.result.agent_actions.length)
      .map((item) => {
        const actualAction = String(item.result.agent_actions[0] ?? ''); const payload = item.envelope?.payload ?? {};
        return Object.freeze({
          event_id: String(item.key), time: item.updatedAt, runtime_version: 'legacy', account_unb: String(payload.accountUnb ?? ''), chat_id: String(payload.chatId ?? ''), peer_unb: String(payload.peerUnb ?? ''),
          buyer_label: agentEvaluationBuyerLabel(payload), turn_summary: agentEvaluationTurnSummary(payload), intent: String(item.result.agent_intent ?? ''), confidence: item.result.agent_confidence, actual_action: actualAction,
          suggested_ask: actualAction.startsWith('ask_for_'), suggested_handoff: actualAction === 'handoff',
          authoritative_outcome: item.result.preview_status === 'preview_ready' ? 'quote_succeeded' : item.result.quote_failure_code ? 'quote_failed' : 'not_available',
          authoritative_summary: agentEvaluationAuthoritativeSummary(item.result), review: reviewByEvent.get(String(item.key)) ?? null,
        });
      });
    return [...durable, ...legacy].sort((left, right) => String(right.time).localeCompare(String(left.time))).slice(0, 100);
  }
  async function getAgentTrace(tenantId, runId) {
    const run = await agentRunStore.get(runId);
    if (!run || String(run.tenant_id) !== String(tenantId)) throw uiApiError(404, 'agent_run_not_found');
    const event = await eventStore.get(run.event_key);
    if (!event || String(event.envelope?.tenantId ?? '') !== String(tenantId)) throw uiApiError(404, 'agent_run_source_not_found');
    return agentTraceReplayFrom(run, event);
  }
  async function getAgentCanaryReadiness(tenantId) {
    const records = await listAgentEvaluations(tenantId);
    return { runtime_version: AGENT_RUNTIME_VERSION, ...agentCanaryReadinessFrom(records.filter((record) => record.runtime_version === AGENT_RUNTIME_VERSION)) };
  }
  async function getAgentOfflineEvaluation(tenantId) {
    const runs = await agentRunStore.list({ tenantId, limit: 500 });
    const events = typeof eventStore.getMany === 'function'
      ? await eventStore.getMany(runs.map((run) => run.event_key))
      : await eventStore.list({ limit: 500 });
    const tenantEvents = events.filter((event) => String(event.envelope?.tenantId) === String(tenantId));
    const historical = agentImageOfflineEvaluationFrom(runs, tenantEvents);
    const current = agentImageOfflineEvaluationFrom(runs.filter((run) => run.result?.runtime_version === AGENT_RUNTIME_VERSION), tenantEvents);
    return { ...current, runtime_version: AGENT_RUNTIME_VERSION, historical_sample_count: historical.sample_count };
  }
  async function reviewAgentEvaluation(tenantId, eventId, input) {
    const [event, runs] = await Promise.all([eventStore.get(eventId), agentRunStore.list({ tenantId, limit: 500 })]);
    const run = runs.find((candidate) => candidate.event_key === eventId && ['completed', 'timed_out'].includes(candidate.status) && Array.isArray(candidate.result?.trace) && candidate.result.trace.length);
    if (String(event?.envelope?.tenantId ?? '') !== String(tenantId) || (!run && event?.result?.conversation_agent_mode !== 'shadow')) throw uiApiError(404, 'agent_evaluation_not_found');
    return agentEvaluationStore.review(tenantId, eventId, input);
  }
  async function getConversationLearningSummary(tenantId) {
    const [events, runs, entries] = await Promise.all([
      eventStore.list({ limit: 500 }), agentRunStore.list({ tenantId, limit: 500 }), listKnowledgeBase(tenantId),
    ]);
    const syntheticAgentEvents = runs.filter((run) => ['completed', 'timed_out'].includes(run.status)).map((run) => ({
      updatedAt: run.updated_at,
      result: { conversation_agent_mode: run.mode, agent_turn_status: run.result?.status === 'handoff' ? 'failed' : run.result?.status },
    }));
    return conversationLearningSummaryFrom(
      [...events.filter((item) => String(item.envelope?.tenantId) === String(tenantId) && item.result?.conversation_agent_mode !== 'shadow'), ...syntheticAgentEvents],
      entries,
    );
  }
  async function listAgentHumanComparisons(tenantId) {
    return agentHumanComparisonStore.list({ tenantId, limit: 200 });
  }
  async function reviewAgentHumanComparison(tenantId, comparisonId, input) {
    return agentHumanComparisonStore.review(tenantId, comparisonId, input);
  }
  async function listManualTasks(tenantId) {
    return agentManualTaskStore.list({ tenantId, limit: 200 });
  }
  async function updateManualTask(tenantId, taskId, input, { userId = '' } = {}) {
    try { return await agentManualTaskStore.update(tenantId, taskId, input, { actorId: userId }); }
    catch (error) {
      if (/manual task not found/u.test(String(error?.message ?? ''))) throw uiApiError(404, 'manual_task_not_found');
      if (error instanceof TypeError || /cannot be reopened/u.test(String(error?.message ?? ''))) throw uiApiError(422, 'invalid_manual_task_update');
      throw error;
    }
  }
  async function resolveManualTask(tenantId, taskId) {
    return agentManualTaskStore.resolve(tenantId, taskId);
  }

  async function createKnowledgeEntry(tenantId, input) {
    const response = await backendClient.createKnowledgeEntry(tenantId, input);
    if (!response?.entry || typeof response.entry !== 'object') throw new Error('backend knowledge base response is invalid');
    return response.entry;
  }
  async function updateKnowledgeEntry(tenantId, id, input) {
    const response = await backendClient.updateKnowledgeEntry(tenantId, id, input);
    if (!response?.entry || typeof response.entry !== 'object') throw new Error('backend knowledge base response is invalid');
    return response.entry;
  }

  const uiHandler = createUiHandler({
    config,
    platformRuntime,
    logger,
    api: {
      overview,
      getSettings,
      updateSettings,
      uploadReplyTemplateImage,
      listOperations,
      listQuoteAnalytics,
      listTicketOrders,
      listOwnedShops,
      updateShopEnabled,
      listKnowledgeBase,
      getConversationLearningSummary,
      listAgentEvaluations,
      getAgentTrace,
      getAgentCanaryReadiness,
      getAgentOfflineEvaluation,
      reviewAgentEvaluation,
      listAgentHumanComparisons,
      reviewAgentHumanComparison,
      listManualTasks,
      updateManualTask,
      resolveManualTask,
      createKnowledgeEntry,
      updateKnowledgeEntry,
      listLogs: async (tenantId) => (await eventStore.list({ limit: 100 }))
        .filter((item) => String(item.envelope?.tenantId) === String(tenantId))
        .map(toLogRecord),
    },
  });

  return Object.freeze({
    enqueueEvent: workflow.enqueueEvent,
    start,
    stop,
    health,
    handleHttpRequest: uiHandler,
  });
}

export function uniqueAgentEvaluationRuns(runs, { runtimeVersion = AGENT_RUNTIME_VERSION } = {}) {
  const modePriority = (mode) => ({ active: 0, shadow: 1, evaluation: 2 })[mode] ?? 3;
  const eligible = (Array.isArray(runs) ? runs : [])
    .filter((run) => ['completed', 'timed_out'].includes(run?.status) && Array.isArray(run?.result?.trace) && run.result.trace.length)
    .sort((left, right) => {
      const leftCurrent = left.result?.runtime_version === runtimeVersion ? 0 : 1;
      const rightCurrent = right.result?.runtime_version === runtimeVersion ? 0 : 1;
      return leftCurrent - rightCurrent || modePriority(left.mode) - modePriority(right.mode) || String(right.updated_at).localeCompare(String(left.updated_at));
    });
  return [...eligible.reduce((byEvent, run) => {
    if (!byEvent.has(run.event_key)) byEvent.set(run.event_key, run);
    return byEvent;
  }, new Map()).values()];
}

export function createWorkerPool(workflow, { concurrency = 4, logger = console } = {}) {
  const limit = Number.isInteger(concurrency) && concurrency >= 1 && concurrency <= 16 ? concurrency : 4;
  const active = new Set();
  let running = true;

  function poll() {
    if (!running) return;
    while (active.size < limit) {
      let task;
      let completedWork = false;
      task = Promise.resolve()
        .then(() => workflow.tick())
        .then((result) => {
          completedWork = result != null;
          return result;
        })
        .catch((error) => {
          logger.error?.('workflow tick failed', { error });
          return null;
        })
        .finally(() => {
          active.delete(task);
          // Refill immediately after real work. Empty polls wait for the normal
          // timer, preventing a busy loop while still allowing later timer
          // ticks to use free slots alongside an older slow quote.
          if (running && completedWork) queueMicrotask(poll);
        });
      active.add(task);
    }
  }

  async function stop() {
    running = false;
    await Promise.allSettled([...active]);
  }

  return Object.freeze({ poll, stop });
}

export function historicalEvaluationCandidatesFrom(runs, { runtimeVersion, target = 100, batchSize = 1 } = {}) {
  const version = String(runtimeVersion ?? '').trim();
  const maximum = Number.isSafeInteger(Number(target)) ? Math.max(1, Math.min(100, Number(target))) : 100;
  const batch = Number.isSafeInteger(Number(batchSize)) ? Math.max(1, Math.min(20, Number(batchSize))) : 10;
  if (!version) return [];
  const values = Array.isArray(runs) ? runs : [];
  const evaluationPrefix = `evaluation:${version}:`;
  const current = values.filter((run) => run?.result?.runtime_version === version || String(run?.run_id ?? '').startsWith(evaluationPrefix));
  const currentEventKeys = new Set(current.map((run) => String(run?.event_key ?? '')).filter(Boolean));
  const evaluationRuns = current.filter((run) => run?.mode === 'evaluation');
  const inFlight = evaluationRuns.filter((run) => ['queued', 'processing', 'retry'].includes(run?.status)).length;
  const remaining = maximum - evaluationRuns.length;
  const slots = Math.max(0, Math.min(batch - inFlight, remaining));
  if (!slots) return [];
  const score = (run) => {
    const tools = Array.isArray(run?.tool_calls) ? run.tool_calls.map((call) => String(call?.tool ?? '')) : [];
    const actions = Array.isArray(run?.result?.trace) ? run.result.trace.map((item) => String(item?.action ?? '')) : [];
    return tools.includes('recognize_image') || actions.some((action) => ['recognize_image', 'start_quote'].includes(action)) ? 1 : 0;
  };
  const seen = new Set();
  return values
    .filter((run) => run?.mode === 'shadow' && run?.event_key && !currentEventKeys.has(String(run.event_key)))
    .sort((left, right) => score(right) - score(left) || String(right.updated_at ?? '').localeCompare(String(left.updated_at ?? '')))
    .map((run) => String(run.event_key))
    .filter((key) => key && !seen.has(key) && seen.add(key))
    .slice(0, slots);
}

export function runConcurrentTicks(workflow, concurrency = 4) {
  const count = Number.isInteger(concurrency) && concurrency >= 1 && concurrency <= 16 ? concurrency : 4;
  // Keep each lane work-conserving. The former one-tick-per-lane batch waited
  // for the slowest quote before any free lane could claim a newly due buyer
  // message, adding several seconds of avoidable head-of-line latency.
  return Promise.all(Array.from({ length: count }, async () => {
    for (let processed = 0; processed < 32; processed += 1) {
      const result = await workflow.tick();
      if (result == null) return;
    }
  }));
}

function runtimeToUiSettings(runtime) {
  return Object.freeze({
    ...runtime,
    automation_enabled: runtime.automation_enabled === true,
    recognition_enabled: runtime.recognition_enabled !== false,
    quote_enabled: runtime.quote_enabled !== false,
    price_change_enabled: runtime.auto_price_change === true,
    ai_reply_enabled: runtime.ai_reply_enabled === true,
    conversation_agent_mode: ['off', 'shadow', 'active'].includes(runtime.conversation_agent_mode) ? runtime.conversation_agent_mode : 'shadow',
    minimum_confidence: Number(runtime.low_confidence_threshold ?? 0.9),
    ai_base_url: String(runtime.ai_reply_base_url ?? ''),
    ai_model: String(runtime.ai_reply_model ?? ''),
    ai_key_configured: runtime.ai_reply_key_configured === true,
    ai_key_masked: String(runtime.ai_reply_api_key_masked ?? ''),
    updated_at: runtime.updated_at ?? null,
  });
}

function runtimePatchFromUi(patch) {
  const result = {};
  if (Object.hasOwn(patch, 'automation_enabled')) result.automation_enabled = patch.automation_enabled === true;
  if (Object.hasOwn(patch, 'recognition_enabled')) result.recognition_enabled = patch.recognition_enabled === true;
  if (Object.hasOwn(patch, 'quote_enabled')) result.quote_enabled = patch.quote_enabled === true;
  if (Object.hasOwn(patch, 'price_change_enabled')) result.auto_price_change = patch.price_change_enabled === true;
  if (Object.hasOwn(patch, 'minimum_confidence')) result.low_confidence_threshold = Number(patch.minimum_confidence);
  if (Object.hasOwn(patch, 'ai_reply_enabled')) {
    result.ai_reply_enabled = patch.ai_reply_enabled === true;
  }
  if (Object.hasOwn(patch, 'ai_base_url')) result.ai_reply_base_url = patch.ai_base_url;
  if (Object.hasOwn(patch, 'ai_model')) result.ai_reply_model = patch.ai_model;
  if (Object.hasOwn(patch, 'ai_api_key')) result.ai_reply_api_key = patch.ai_api_key;
  if (patch.clear_ai_api_key === true) result.ai_reply_clear_api_key = true;
  for (const field of [
    'ai_only_mode_enabled',
    'conversation_agent_mode',
    'ai_reply_system_prompt',
    'ai_reply_shop_background',
    'ai_reply_precautions',
    'ai_reply_style',
    'ai_reply_temperature',
    'ai_reply_timeout_seconds',
    'ai_reply_fallback',
    'ai_reply_daily_limit',
    'ai_reply_cooldown_seconds',
    'ai_reply_memory_hours',
    'ai_reply_memory_depth',
    'ai_reply_delay_seconds',
    'ai_reply_manual_takeover_seconds',
    'shop_execution_modes',
    'shop_automation_overrides',
    'shop_feature_overrides',
    'reply_templates',
    'reply_template_images',
  ]) {
    if (Object.hasOwn(patch, field)) result[field] = patch[field];
  }
  return result;
}

function imageSignatureMatches(bytes, contentType) {
  if (contentType === 'image/png') return bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex'));
  if (contentType === 'image/jpeg') return bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  if (contentType === 'image/webp') return bytes.subarray(0, 4).toString('ascii') === 'RIFF' && bytes.subarray(8, 12).toString('ascii') === 'WEBP';
  return false;
}

function pricingFormulaSummary(settings = {}) {
  const wplusAdjustment = Number(settings.wplus_adjustment_cents ?? -290);
  const threshold = Number(settings.wplus_member_price_threshold_cents ?? 6000);
  const regularAdjustment = Number(settings.regular_adjustment_cents ?? 100);
  return `W+区：会员价不高于${(threshold / 100).toFixed(2)}元时，取会员价与实时原价${wplusAdjustment < 0 ? '下调' : '上调'}${(Math.abs(wplusAdjustment) / 100).toFixed(2)}元的较高值；普通区：会员价加${(regularAdjustment / 100).toFixed(2)}元。最终按0.1元轮整，并受会员成本下限和实时原价上限约束。`;
}

async function mapWithConcurrency(items, concurrency, worker) {
  const values = Array.isArray(items) ? items : [];
  const results = new Array(values.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(concurrency, values.length) }, async () => {
    while (next < values.length) {
      const index = next;
      next += 1;
      results[index] = await worker(values[index], index);
    }
  }));
  return results;
}

function uiApiError(status, code) {
  const error = new Error(code);
  error.status = status;
  error.code = code;
  return error;
}

export function ticketIssuanceFromXianyuOrder(input = {}) {
  if (input.read_status !== 'available') return Object.freeze({ status: 'unknown', evidence: '闲鱼订单读取失败' });
  const status = Number(input.order_status);
  const text = compactText(input.order_status_text, 120);
  if (status === 4 || /(?:交易成功|交易完成)/u.test(text)) {
    return Object.freeze({ status: 'issued', evidence: '闲鱼交易完成' });
  }
  if (status === 3 || /(?:已发货|待买家收货|等待买家收货|待收货)/u.test(text)) {
    return Object.freeze({ status: 'issued', evidence: '闲鱼已发货' });
  }
  return Object.freeze({ status: 'not_confirmed', evidence: '闲鱼尚未发货' });
}

function nonNegativeCentsOrNull(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

function compactText(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

export function agentEvaluationTurnSummary(payload = {}) {
  const imageCount = Array.isArray(payload.imageUrls)
    ? payload.imageUrls.filter((value) => typeof value === 'string' && value.trim()).length
    : 0;
  const text = compactText(payload.content ?? payload.text, 120)
    .replace(/https?:\/\/[^\s，。；！？,;]+/giu, '[链接]')
    .replace(/(?<!\d)1\d{10}(?!\d)/gu, '[手机号]')
    .replace(/(?<!\d)\d{8,}(?!\d)/gu, '[编号]');
  if (text && imageCount) return `买家说：${text}；并发送${imageCount}张图片`;
  if (text) return `买家说：${text}`;
  if (imageCount) return `买家发送${imageCount}张图片`;
  return '本轮没有可展示的买家文字或图片';
}

function agentEvaluationBuyerLabel(payload = {}) {
  const name = safeBuyerName(payload);
  if (name) return name;
  const peer = compactText(payload.peerUnb, 128);
  return peer ? `买家 …${peer.slice(-4)}` : '买家（身份未知）';
}

function agentEvaluationAuthoritativeSummary(result = {}) {
  if (result.preview_status === 'preview_ready') return '确定性系统：已形成实时报价';
  if (result.quote_failure_code) return eventDiagnosticSummary(result);
  const statuses = (Array.isArray(result.actions) ? result.actions : []).map((item) => String(item?.status ?? ''));
  if (statuses.includes('succeeded')) return '确定性系统：已按既有规则完成本轮回复';
  if (statuses.includes('skipped')) return '确定性系统：本轮已安全跳过';
  return '确定性系统：本轮没有形成可对照的交易结果';
}

function toRecentRecord(record) {
  const payload = record.envelope?.payload ?? {};
  return {
    id: record.key,
    event: record.envelope?.event,
    status: record.status,
    updated_at: record.updatedAt,
    account_unb: payload.accountUnb ?? null,
    chat_id: payload.chatId ?? null,
    peer_unb: payload.peerUnb ?? null,
    order_id: payload.orderId ?? null,
    summary: safeSummary(payload.content ?? payload.text ?? record.envelope?.event),
    result: record.result ?? null,
    error: record.lastError ?? null,
  };
}

function manualTaskReviewRecord(task) {
  return {
    id: `manual:${task.task_id}`,
    manual_task_id: task.task_id,
    event: 'agent.manual_task',
    status: 'failed',
    updated_at: task.updated_at,
    account_unb: task.account_unb,
    chat_id: task.chat_id,
    peer_unb: task.peer_unb,
    order_id: task.order_id,
    summary: task.summary,
    error: task.reason_code,
  };
}

function matchesOperationEvent(operation, record) {
  const payload = record.envelope?.payload ?? {};
  const sameChat = String(payload.accountUnb ?? '') === String(operation.account_unb ?? '')
    && String(payload.chatId ?? '') === String(operation.chat_id ?? '');
  const sameOrder = operation.order_id && String(payload.orderId ?? '') === String(operation.order_id);
  return sameChat || sameOrder;
}

function safeBuyerName(payload) {
  return [
    payload?.peerNick, payload?.peerNickname, payload?.peer_nick,
    payload?.buyerNick, payload?.buyerNickname, payload?.buyer_nick, payload?.buyerName,
  ].map((value) => String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, 80)).find(Boolean) ?? '';
}

function toOperationActivity(record) {
  const result = record.result ?? {};
  const actions = Array.isArray(result.actions) ? result.actions : [];
  const actionSummary = actions.map((action) => String(action.status ?? '')).filter(Boolean).join('、');
  return Object.freeze({
    event: String(record.envelope?.event ?? ''),
    status: String(record.status ?? ''),
    updated_at: record.updatedAt ?? null,
    summary: safeSummary(
      actionSummary
      || result.reason
      || result.quote_skipped
      || quoteFailureSummary(result.quote_failure_code)
      || result.preview_status
      || record.lastError
      || record.envelope?.event,
    ),
  });
}

const WAITING_INPUT_QUOTE_CODES = new Set([
  'text_quote_missing_fields', 'cinema_catalog_not_unique', 'showtime_not_unique', 'showtime_not_found', 'need_image',
  'official_selection_unverifiable',
]);
const BUSINESS_BLOCKED_QUOTE_CODES = new Set([
  'wplus_area_unavailable', 'wplus_seats_unavailable', 'wplus_price_unavailable', 'insufficient_available_seats',
  'quote_price_conflict', 'ticket_count_conflict', 'non_wanda_cinema',
]);

function quoteFailureSummary(code) {
  return ({
    text_quote_missing_fields: '文字询价信息不完整',
    need_image: '需要完整选座页截图',
    wplus_area_unavailable: 'W+ 区域无法可靠核验',
    wplus_seats_unavailable: '当前场次没有可用 W+ 座位',
    wplus_account_unavailable: 'W+ 核价账号暂不可用',
    wplus_price_unavailable: '未找到可用 W+ 优惠价',
    insufficient_available_seats: '没有足够的同类可用座位',
    showtime_not_unique: '截图信息无法唯一匹配场次',
    showtime_not_found: '万达官方场次中未找到该日期和时间',
    cinema_catalog_not_unique: '影院无法在官方库唯一匹配',
    wanda_gateway_unavailable: '万达实时接口暂不可用',
    temporary_lock_failed: '临时试价订单创建失败',
    temporary_lock_release_unverified: '临时试价座位释放未确认',
    official_selection_unverifiable: '官方已选座无法逐座核验',
    quote_price_conflict: '会员成本与实时原价冲突',
    quote_verification_failed: '实时核价未通过',
  })[String(code ?? '')] ?? '';
}

export function conversationLearningSummaryFrom(events = [], entries = []) {
  const agentEvents = (Array.isArray(events) ? events : []).filter((item) => {
    const mode = String(item?.result?.conversation_agent_mode ?? '');
    return ['shadow', 'active'].includes(mode) && item?.result?.agent_turn_status;
  });
  const experiences = (Array.isArray(entries) ? entries : [])
    .filter((item) => item?.source === 'conversation_experience');
  const latestTimestamp = agentEvents.reduce((latest, item) => {
    const timestamp = new Date(item?.updatedAt ?? item?.envelope?.ts ?? 0).getTime();
    return Number.isFinite(timestamp) ? Math.max(latest, timestamp) : latest;
  }, 0);
  const evidenceCount = experiences.reduce((total, item) => {
    const count = Number(item?.evidence_count ?? 0);
    return total + (Number.isSafeInteger(count) && count > 0 ? count : 0);
  }, 0);
  return Object.freeze({
    model_training_enabled: false,
    automatic_activation_enabled: false,
    observed_turn_count: agentEvents.length,
    shadow_turn_count: agentEvents.filter((item) => item.result.conversation_agent_mode === 'shadow').length,
    active_turn_count: agentEvents.filter((item) => item.result.conversation_agent_mode === 'active').length,
    agent_failure_count: agentEvents.filter((item) => item.result.agent_turn_status === 'failed').length,
    experience_draft_count: experiences.filter((item) => item.status === 'draft').length,
    experience_approved_count: experiences.filter((item) => item.status === 'approved').length,
    experience_enabled_count: experiences.filter((item) => item.status === 'approved' && item.enabled === true).length,
    experience_evidence_count: evidenceCount,
    last_observed_at: latestTimestamp > 0 ? new Date(latestTimestamp).toISOString() : null,
  });
}

function toLogRecord(record) {
  const result = record.result && typeof record.result === 'object' ? record.result : {};
  return {
    id: record.key,
    time: record.updatedAt,
    event: record.envelope?.event,
    status: operationalLogStatus(record.status, result),
    attempts: record.attempts,
    error: record.lastError ?? null,
    diagnostic: eventDiagnosticSummary(result),
  };
}

export function operationalLogStatus(storedStatus, result) {
  const priceStatus = String(result?.order_price_change?.status ?? '');
  if (['rejected', 'blocked'].includes(priceStatus)) return 'failed';
  if (priceStatus === 'unknown') return 'unknown';
  const quoteCode = String(result?.quote_failure_code ?? result?.quote_diagnostics?.safe_error_code ?? '');
  if (WAITING_INPUT_QUOTE_CODES.has(quoteCode)) return 'waiting_input';
  if (BUSINESS_BLOCKED_QUOTE_CODES.has(quoteCode)) return 'business_blocked';
  if (quoteCode) return 'failed';
  const skippedAction = Array.isArray(result?.actions)
    ? result.actions.find((item) => item?.status === 'skipped')
    : null;
  if (skippedAction) {
    return skippedAction.reason === 'price_change_gate_failed' ? 'business_blocked' : 'skipped';
  }
  return String(storedStatus ?? '');
}

export function eventDiagnosticSummary(result) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return '';
  const price = result.order_price_change;
  if (price && typeof price === 'object' && !Array.isArray(price)) {
    const diagnostics = price.diagnostics && typeof price.diagnostics === 'object' ? price.diagnostics : {};
    const status = String(price.status ?? '');
    const statusLabel = ({ submitted: '改价已提交', succeeded: '改价成功', rejected: '改价拒绝', blocked: '改价阻止', unknown: '改价结果未知' })[status] ?? '改价处理';
    const current = moneyLabel(diagnostics.current_total_cents);
    const target = moneyLabel(diagnostics.target_total_cents ?? price.amount_cents);
    const direction = ({ increase: '涨价', decrease: '降价', unchanged: '不变' })[diagnostics.direction];
    return safeSummary([
      `${statusLabel}${price.code ? ` ${safeSummary(price.code)}` : ''}`,
      current && target ? `金额 ${current}→${target}元` : target ? `目标 ${target}元` : '',
      direction ? `方向 ${direction}` : '',
      Number.isInteger(diagnostics.http_status) ? `HTTP ${diagnostics.http_status}` : '',
      diagnostics.provider_reason_code ? `上游 ${safeSummary(diagnostics.provider_reason_code)}` : '',
    ].filter(Boolean).join('；'));
  }
  if (result.quote_diagnostics) {
    const code = String(result.quote_diagnostics?.safe_error_code ?? result.quote_failure_code ?? '');
    const prefix = WAITING_INPUT_QUOTE_CODES.has(code)
      ? '待买家补充'
      : BUSINESS_BLOCKED_QUOTE_CODES.has(code) ? '安全停止' : '核价异常';
    return `${prefix}；${quoteDiagnosticSummary(result.quote_diagnostics)}`;
  }
  if (result.quote_failure_code) {
    const code = String(result.quote_failure_code);
    const detail = quoteFailureSummary(code) || safeSummary(code);
    if (WAITING_INPUT_QUOTE_CODES.has(code)) return `待买家补充：${detail}`;
    if (BUSINESS_BLOCKED_QUOTE_CODES.has(code)) return `安全停止：${detail}`;
    return `核价异常：${detail}`;
  }
  const action = Array.isArray(result.actions)
    ? result.actions.find((item) => item && (item.status === 'skipped' || item.status === 'rejected' || item.status === 'blocked'))
    : null;
  if (action?.status === 'skipped' && action.reason) {
    const reason = ({ human_takeover: '人工接管', price_change_gate_failed: '改价门禁未通过' })[action.reason] ?? safeSummary(action.reason);
    return `跳过：${reason}`;
  }
  if (result.price_changed_notified === true && Number.isInteger(result.verified_amount_cents)) {
    return `改价结果已核验：${moneyLabel(result.verified_amount_cents)}元`;
  }
  if (result.conversation_agent_mode) {
    const shadow = result.conversation_agent_mode === 'shadow';
    const confidence = Number(result.agent_confidence);
    const confidenceLabel = Number.isFinite(confidence) && confidence >= 0 && confidence <= 1 ? `（置信度${Math.round(confidence * 100)}%）` : '';
    const intent = agentIntentLabel(result.agent_intent);
    const actionLabels = Array.isArray(result.agent_actions)
      ? result.agent_actions.map(agentActionLabel).filter(Boolean)
      : [];
    const action = actionLabels.length ? actionLabels.join('，然后') : '不执行动作';
    const reason = agentReasonLabel(result.agent_turn_reason, result.agent_turn_status);
    const experience = ['draft_created', 'draft_updated'].includes(result.conversation_experience_status)
      ? '已提炼会话经验草稿，尚未生效。'
      : '';
    return safeSummary(`${shadow ? 'AI观察（未执行）' : 'AI客服'}：识别为“${intent}”，${shadow ? '建议' : '请求'}“${action}”${confidenceLabel}。${reason}${experience}`);
  }
  if (result.ignored_event && result.reason) return `已忽略：${safeSummary(result.reason)}`;
  return '';
}

function agentIntentLabel(value) {
  const intent = safeSummary(value) || '未分类';
  return ({
    '票价咨询': '票价咨询', '选座核价': '选座核价', '订单进度': '订单进度',
    '补充信息': '需要补充信息', '其他': '其他问题',
  })[intent] ?? intent;
}

function agentActionLabel(value) {
  const action = String(value ?? '');
  return ({
    respond: '回复流程说明', ask_for_image: '请买家补发选座截图', ask_for_city: '询问城市',
    ask_for_missing_information: '询问缺失信息', start_quote: '请求旧版识图核价',
    recognize_image: '识别买家图片', resolve_showtime: '匹配影院场次', quote_realtime: '调用实时核价',
    request_price_change: '申请安全改价', create_manual_task: '创建人工处理任务',
    show_available_wplus_seats: '查询可用W+座位', record_seat_preference: '记录圈选出票指令',
    confirm_quote: '确认本次报价', get_order_status: '查询订单状态', read_linked_order: '读取关联订单',
    inspect_ticket_request: '检查票务请求', handoff: '转人工处理', wait: '暂停自动处理',
  })[action] ?? (action ? `未知动作 ${safeSummary(action)}` : '');
}

function agentReasonLabel(value, status) {
  const reason = String(value ?? '');
  const label = ({
    quote_requested: '判断依据：买家正在询价。',
    conversation_only: '判断依据：本轮只需继续沟通，不执行交易。',
    agent_wait: '判断依据：无需继续回复。',
    agent_requested_handoff: '安全判断：应转人工处理。',
    paid_order: '安全限制：订单已付款，不再核价或改价。',
    human_takeover: '安全限制：人工客服已经接管。',
    low_confidence: '安全限制：AI判断置信度不足。',
    tool_failed: '执行结果：权威工具调用失败，已停止。',
  })[reason];
  if (label) return label;
  if (String(status ?? '') === 'failed') return '执行结果：AI规划失败，未执行任何动作。';
  return reason ? `内部依据：${safeSummary(reason)}。` : '仅用于评估AI判断，不改变实际回复。';
}

function moneyLabel(value) {
  const cents = Number(value);
  return Number.isSafeInteger(cents) && cents >= 0 ? (cents / 100).toFixed(2) : '';
}

export function quoteDiagnosticSummary(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  const requested = value.requested_match && typeof value.requested_match === 'object' ? value.requested_match : {};
  const match = value.match && typeof value.match === 'object' ? value.match : {};
  const areas = Array.isArray(value.realtime_areas) ? value.realtime_areas : [];
  const requestedSummary = [requested.city, requested.cinema, requested.movie, requested.date, requested.showtime, requested.hall]
    .map(safeSummary).filter(Boolean).join(' / ');
  const parts = [
    value.safe_error_code && `代码 ${safeSummary(value.safe_error_code)}`,
    value.failure_step && `步骤 ${safeSummary(value.failure_step)}`,
    requestedSummary && `请求 ${requestedSummary}`,
    Number.isInteger(match.result_count) && `匹配 ${match.result_count} 个`,
    match.cinema && `影院 ${safeSummary(match.cinema)}`,
    match.showtime && `场次 ${safeSummary(match.showtime)}`,
    areas.length && `实时区域 ${areas.map((area) => `${safeSummary(area.label || area.area_code)}:可用${Number.isInteger(area.available_seat_count) ? area.available_seat_count : '-'}`).join('；')}`,
  ].filter(Boolean);
  return safeSummary(parts.join('；'));
}

function safeSummary(value) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, 120);
}
