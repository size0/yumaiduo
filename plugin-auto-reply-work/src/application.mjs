import { agentCanaryReadinessFrom, automatedAgentSafetyReviewFrom } from './agent-evaluation-store.mjs';
import { createStorageBundle } from './bootstrap/create-storage-bundle.mjs';
import { createAgentRuntimeBundle } from './bootstrap/create-agent-runtime-bundle.mjs';
import { createLifecycleController } from './bootstrap/create-lifecycle-controller.mjs';
export { createWorkerPool, historicalEvaluationCandidatesFrom } from './bootstrap/create-lifecycle-controller.mjs';
import { createImageLoader } from './image-loader.mjs';
import { createUiHandler } from './ui-handler.mjs';
import { createQuotePreviewClient } from './quote-preview-client.mjs';
import { createReplyPreviewClient } from './reply-preview-client.mjs';
import { agentTraceReplayFrom } from './agent/agent-trace-replay.mjs';
import { agentImageOfflineEvaluationFrom } from './agent/agent-offline-evaluator.mjs';
import { AGENT_RUNTIME_VERSION } from './agent/shadow-agent-runtime.mjs';
import { createWorkflow } from './workflow.mjs';
import { hasUnresolvedReplyPlaceholder } from './agent/response-composer.mjs';
import {
  agentEvaluationAuthoritativeSummary,
  agentEvaluationBuyerLabel,
  agentEvaluationTurnSummary,
  compactText,
  conversationLearningSummaryFrom,
  imageSignatureMatches,
  manualTaskReviewRecord,
  matchesOperationEvent,
  nonNegativeCentsOrNull,
  pricingFormulaSummary,
  runtimePatchFromUi,
  runtimeToUiSettings,
  safeBuyerName,
  ticketIssuanceFromXianyuOrder,
  toLogRecord,
  toOperationActivity,
  toRecentRecord,
  uiApiError,
} from './admin/operator-presenters.mjs';
export {
  agentEvaluationTurnSummary,
  conversationLearningSummaryFrom,
  eventDiagnosticSummary,
  operationalLogStatus,
  quoteDiagnosticSummary,
  ticketIssuanceFromXianyuOrder,
} from './admin/operator-presenters.mjs';

const QUOTE_POLICY_FIELDS = Object.freeze([
  'wplus_adjustment_cents',
  'wplus_member_price_threshold_cents',
  'regular_adjustment_cents',
  'max_auto_order_amount_cents',
]);

export async function createApplication({ config, platformRuntime, backendClient, logger = console }) {
  const storage = createStorageBundle(config);
  const {
    eventStore,
    conversationContextStore,
    agentEvaluationStore,
    agentRunStore,
    agentReplyOutboxStore,
    agentManualTaskStore,
    agentHumanComparisonStore,
  } = storage;
  const imageLoader = createImageLoader({ allowlist: config.imageHostAllowlist });
  const quotePreviewClient = createQuotePreviewClient(config);
  const replyPreviewClient = createReplyPreviewClient(config);
  const agentRuntime = createAgentRuntimeBundle({
    config,
    platformRuntime,
    storage,
    quotePreviewClient,
    getSettings,
    logger,
  });
  const {
    conversationAgentPlanner,
    agentHumanComparisonScanner,
    agentReplyOutboxDispatcher,
    shadowAgentRuntime,
  } = agentRuntime;
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
  const lifecycle = createLifecycleController({
    storage,
    workflow,
    shadowAgentRuntime,
    agentReplyOutboxDispatcher,
    agentHumanComparisonScanner,
    workerIntervalMs: config.workerIntervalMs,
    runtimeVersion: AGENT_RUNTIME_VERSION,
    logger,
  });
  const orderDisplayCache = new Map();
  const shopDisplayCache = new Map();

  async function start() {
    await lifecycle.start();
  }

  async function stop() {
    await lifecycle.stop();
  }

  async function health() {
    const [queue, agentQueue, agentOutbox, manualTasks, humanComparisons] = await Promise.all([eventStore.health(), agentRunStore.health(), agentReplyOutboxStore.health(), agentManualTaskStore.health(), agentHumanComparisonStore.health()]);
    const workerStatus = lifecycle.status();
    return {
      ok: true,
      worker: workerStatus.worker,
      agent_worker: workerStatus.agent_worker,
      agent_outbox_worker: workerStatus.agent_outbox_worker,
      queue,
      agent_queue: agentQueue,
      agent_outbox: agentOutbox,
      manual_tasks: manualTasks,
      historical_evaluation_worker: workerStatus.historical_evaluation_worker,
      human_comparison_worker: workerStatus.human_comparison_worker,
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
