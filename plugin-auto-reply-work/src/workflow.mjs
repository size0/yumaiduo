import { UnknownActionResultError, createActionExecutor } from './action-executor.mjs';
import { isLocationQuoteSupplement } from './quote-supplement.mjs';
import { createConversationAgent } from './agent/conversation-agent.mjs';
import { normalizeAgentPlan } from './agent/agent-schema.mjs';
import { authorizeAgentPlan, guardAgentPlan } from './agent/policy-engine.mjs';
import { agentCanaryDecision } from './agent/agent-canary-router.mjs';
import { requestedTicketCount } from './agent/ticket-request-inspector.mjs';
import { AGENT_RUNTIME_VERSION } from './agent/shadow-agent-runtime.mjs';
import { hasUnresolvedReplyPlaceholder } from './agent/response-composer.mjs';
import { EVENT_ROUTE_KIND, routeWorkflowEvent } from './event-router.mjs';
import { createReplyOrchestrator, isBuyerReplyAction } from './reply/reply-orchestrator.mjs';
import {
  configuredReply,
  configuredReplyImage,
  createBuyerReplyAction as autoReplyAction,
  createQuoteFollowUpAction as quoteFollowUpAction,
  withConfiguredReplyImage,
} from './reply/reply-policy.mjs';
import {
  isBareAcknowledgement,
  isCurrentQuoteQuestion,
  isExplicitTypedSeatChoice,
  isQuoteConfirmation,
  isQuotePurchaseIntent,
  isSeatClarificationQuestion,
} from './conversation/message-classifier.mjs';
import {
  completedUnitQuotePreview,
  createImageFailureReplyAction as imageFailureReplyAction,
  createQuoteReplyAction as autoQuoteReplyAction,
  deliveredUnitQuoteQuestionPreview,
  duplicateQuoteClosureAction,
  hasActiveQuote,
  paymentSafeOrderInstruction,
  quoteSupersessionPreview,
} from './quote/quote-followup-policy.mjs';
import { createQuoteOrchestrator } from './quote/quote-orchestrator.mjs';
import { createOrderOrchestrator } from './orders/order-orchestrator.mjs';
import {
  deferUntilReplyDelay,
  settle,
  awaitWithDelayedNotice,
  autoModelReplyAction,
  agentStateSnapshot,
  recentExplicitTicketCount,
  conflictingRecentTicketCount,
  orderSubmitGuideAction,
  boundedInteger,
  cinemaMatchEvaluationSnapshot,
  quoteStageTimings,
  quoteCostEvidence,
  positiveCents,
  nonnegativeCents,
  bridgeActions,
  withManualTakeoverWindow,
  normalizeBackendAction,
  quotePolicySnapshot,
  isValidQuotePolicy,
  statusPayload,
  centsFromOrder,
  centsFromPayload,
  isImageMessageText,
  firstImageUrl,
  activeQuoteDraftText,
  isQuoteDetailSupplement,
  recentContextImages,
  recentContextImage,
  isTerminalHistoricalOrder,
  messageContext,
  toReplyHistoryMessage,
  isDurableActiveLowRiskTurn,
  isPlatformSystemMessage,
  messageChatKey,
  dataUrl,
  context,
  nonRetryable,
  isImWsUnavailable,
  retryDelay,
  safeLog,
} from './workflow-support.mjs';

export { paymentSafeOrderInstruction };

// Buyers commonly send the screenshot, city, seats, and quantity as separate
// messages. Process only the final event after this quiet window.
const MIN_IM_REPLY_DELAY_MS = 2_000;
const IMAGE_SUPPLEMENT_WINDOW_MS = 10 * 60 * 1_000;
const MULTI_IMAGE_PAIR_WINDOW_MS = 30 * 1_000;
export function createWorkflow({
  backend,
  coreFor,
  eventStore,
  conversationContextStore = null,
  imageLoader,
  quotePreviewClient = null,
  replyPreviewClient = null,
  conversationAgentPlanner = null,
  shadowAgentScheduler = null,
  manualTaskStore = null,
  autoReplyEnabled = false,
  sleep,
  quoteProgressNoticeDelayMs = 3_000,
  logger = console,
}) {
  const executor = createActionExecutor({ coreFor, messageRegistry: eventStore, imageLoader, ...(sleep ? { sleep } : {}) });
  const replyOrchestrator = createReplyOrchestrator({ actionExecutor: executor });
  const quoteOrchestrator = createQuoteOrchestrator({ quotePreviewClient, conversationContextStore });
  const executeAction = (action) => isBuyerReplyAction(action)
    ? replyOrchestrator.deliver(action)
    : executor.execute(action);
  const orderOrchestrator = createOrderOrchestrator({
    backend,
    coreFor,
    eventStore,
    conversationContextStore,
    executeAction,
    logger,
  });

  async function enqueueEvent(envelope) {
    let queuedEnvelope = envelope;
    if (envelope.event === 'im.message.received' && conversationContextStore) {
      await conversationContextStore.add(envelope.tenantId, envelope.payload ?? {}, envelope.ts);
      // Keep the raw buyer event intact. Image inheritance is decided later
      // only for a concrete quote-detail supplement, never for casual text.
      // Otherwise "好的" and similar messages become repeat quote jobs.
    }
    // Webhook enqueue cannot fetch tenant settings without delaying ACK. Queue at
    // the supported two-second minimum; processClaimed extends the window when
    // the authoritative runtime setting is higher.
    // A first quote image is persisted and claimed immediately so its bounded
    // receipt can be sent without waiting for transaction-fact merging. The
    // same event returns to the normal two-second merge window before vision
    // or any quote tool runs.
    const delay = envelope.event === 'im.message.received' && !firstImageUrl(queuedEnvelope.payload)
      ? MIN_IM_REPLY_DELAY_MS
      : 0;
    if (
      typeof eventStore.cancelPendingChatMessages === 'function'
      && (Boolean(firstImageUrl(queuedEnvelope.payload)) || isQuoteDetailSupplement(queuedEnvelope.payload) || isLocationQuoteSupplement(queuedEnvelope.payload))
    ) {
      await eventStore.cancelPendingChatMessages(queuedEnvelope.tenantId, messageChatKey(queuedEnvelope.payload), queuedEnvelope.id);
    }
    return eventStore.enqueue(queuedEnvelope, { availableAt: Date.now() + delay });
  }

  async function processClaimed(record) {
    const { envelope } = record;
    const eventRoute = routeWorkflowEvent(envelope.event);
    try {
      if (eventRoute.kind !== EVENT_ROUTE_KIND.MESSAGE) await orderOrchestrator.updateConversationStage(envelope, eventRoute);
      // Await lifecycle handlers so their failures pass through the terminal/
      // retry classifier below instead of leaving a leased event to recover
      // only after its 60-second lease expires.
      if (quotePreviewClient && typeof conversationContextStore?.get === 'function') {
        const orderResult = await orderOrchestrator.process(record, eventRoute);
        if (orderResult) return orderResult;
      }
      if (eventRoute.kind === EVENT_ROUTE_KIND.MESSAGE && isPlatformSystemMessage(envelope.payload)) {
        await eventStore.complete(record.key, record.leaseId, {
          mode: 'quote_preview_only', skipped: 'platform_system_message', actions: [],
        });
        return { status: 'completed', mode: 'quote_preview_only', actions: [] };
      }
      if (eventRoute.kind === EVENT_ROUTE_KIND.MESSAGE && await isOwnedShopPeer(envelope)) {
        await eventStore.complete(record.key, record.leaseId, {
          mode: 'quote_preview_only',
          skipped: 'owned_shop_peer',
          actions: [],
        });
        return { status: 'completed', mode: 'quote_preview_only', actions: [] };
      }
      if ((quotePreviewClient || replyPreviewClient || conversationAgentPlanner) && eventRoute.kind === EVENT_ROUTE_KIND.MESSAGE) {
        const settings = await loadRuntimeSettings(envelope);
        if (!settings.automation_enabled) {
          let agentRunScheduled = false;
          if (
            (settings.ai_reply_enabled === true || settings.shadow_evaluation_enabled === true)
            && settings.conversation_agent_mode === 'shadow'
            && shadowAgentScheduler?.schedule
          ) {
            try {
              const scheduled = await shadowAgentScheduler.schedule(envelope, {
                contextSnapshot: await durableAgentContextSnapshot(envelope, null, settings),
              });
              agentRunScheduled = scheduled?.created === true || Boolean(scheduled?.run);
            } catch (error) {
              logger.warn?.('[workflow] disabled-shop shadow scheduling failed without enabling buyer actions', {
                eventId: String(envelope.id), error: String(error?.message ?? error),
              });
            }
          }
          await eventStore.complete(record.key, record.leaseId, {
            mode: 'quote_preview_only',
            skipped: 'shop_automation_disabled',
            ...(agentRunScheduled ? { agent_run_scheduled: true } : {}),
            actions: [],
          });
          return { status: 'completed', mode: 'quote_preview_only', agent_run_scheduled: agentRunScheduled, actions: [] };
        }
        const preMergeActions = Array.isArray(record.result?.actions) ? [...record.result.actions] : [];
        if (!preMergeActions.length) {
          const receipt = await sendImmediateQuoteReceipt(envelope, settings);
          if (receipt) {
            preMergeActions.push(receipt);
            record.result = { ...(record.result ?? {}), actions: preMergeActions };
          }
        }
        const deferred = await deferUntilReplyDelay(record, settings, eventStore, preMergeActions.length ? { actions: preMergeActions } : null);
        if (deferred) return { status: deferred.status === 'cancelled' ? 'cancelled' : 'queued', mode: 'quote_preview_only', actions: preMergeActions };
        return await processPreviewOnly(record, settings);
      }
      if (quotePreviewClient || replyPreviewClient || conversationAgentPlanner) return await processPreviewOnly(record);
      const upsert = await backend.upsertOrder(envelope);
      const taskId = String(upsert?.task?.id ?? '').trim();
      if (!taskId) throw nonRetryable('bridge upsert response is missing task.id');
      const settings = await loadRuntimeSettings(envelope);

      let bridgeResult = upsert;
      if (eventRoute.kind === EVENT_ROUTE_KIND.MESSAGE) {
        bridgeResult = await handleMessage(envelope, taskId, upsert, settings);
      } else if (eventRoute.bridge_status) {
        const statusInput = await statusPayload(envelope, eventRoute.bridge_status, coreFor);
        bridgeResult = await backend.updateTaskStatus(
          taskId,
          statusInput,
          context(envelope, `status:${eventRoute.bridge_status}`),
        );
      }

      const actions = bridgeActions(envelope, bridgeResult, upsert, settings);
      const results = [];
      for (const action of actions) {
        const result = await executeAction(action);
        results.push({ action_id: action.action_id, ...result });
        await markAiReplySentIfNeeded(action, result);
      }
      await eventStore.complete(record.key, record.leaseId, {
        task_id: taskId,
        bridge_status: bridgeResult?.task?.status ?? upsert?.task?.status ?? null,
        actions: results,
      });
      return { status: 'completed', actions: results };
    } catch (error) {
      if (error instanceof UnknownActionResultError) {
        await eventStore.markUnknown(record.key, record.leaseId, error);
        logger.error?.('[workflow] unknown external action result', safeLog(record, error));
        return { status: 'unknown' };
      }
      if (error?.retryable === false || Number(error?.status) === 404 || error instanceof TypeError || error instanceof RangeError) {
        await eventStore.fail(record.key, record.leaseId, error);
        logger.error?.('[workflow] non-retryable event failure', safeLog(record, error));
        return { status: 'failed' };
      }
      const wsUnavailable = isImWsUnavailable(error);
      const delayMs = retryDelay(record.attempts, error);
      const next = await eventStore.retry(record.key, record.leaseId, error, { delayMs, maxAttempts: wsUnavailable ? 360 : 8 });
      logger.error?.('[workflow] event processing failed', safeLog(record, error));
      return { status: next.status };
    }
  }

  async function sendImmediateQuoteReceipt(envelope, runtimeSettings) {
    if (
      !autoReplyEnabled
      || !quotePreviewClient
      || !firstImageUrl(envelope.payload)
      || runtimeSettings.ai_reply_enabled !== true
      || runtimeSettings.recognition_enabled !== true
      || runtimeSettings.quote_enabled !== true
      || typeof conversationContextStore?.claimQuoteProcessingReceipt !== 'function'
    ) return null;
    if (await isOrderLinkedChat(envelope)) return null;
    const claimed = await conversationContextStore.claimQuoteProcessingReceipt(envelope.tenantId, envelope.payload ?? {});
    if (!claimed) return null;
    const action = autoReplyAction(
      envelope,
      configuredReply(runtimeSettings, 'quote_processing_notice', '收到，正在按当前信息核对万达实时场次和优惠，请稍等。'),
      'quote_processing',
      'quote-processing-notice',
    );
    if (!action) return null;
    try {
      const result = await executeAction({
        ...action,
        allow_plugin_followup: true,
        reply_origin: 'quote_processing',
        human_takeover_window_ms: boundedInteger(runtimeSettings.ai_reply_manual_takeover_seconds, 5, 60, 20) * 1_000,
      });
      return { action_id: action.action_id, ...result };
    } catch (error) {
      await conversationContextStore.releaseQuoteProcessingReceipt?.(envelope.tenantId, envelope.payload ?? {}).catch?.(() => false);
      throw error;
    }
  }

  async function processPreviewOnly(record, runtimeSettings = {}) {
    const { envelope } = record;
    if (envelope.event !== 'im.message.received') {
      await eventStore.complete(record.key, record.leaseId, {
        mode: 'quote_preview_only',
        ignored_event: envelope.event,
        actions: [],
      });
      return { status: 'completed', mode: 'quote_preview_only', actions: [] };
    }
    const processingStartedAt = Date.now();
    const actions = Array.isArray(record.result?.actions) ? [...record.result.actions] : [];
    const preMergeReceipt = actions.find((item) => String(item?.action_id ?? '').endsWith(':quote-processing-notice'));
    let firstContactNoticeSent = false;
    let quoteProcessingNoticeAttempted = Boolean(preMergeReceipt);
    let quoteProcessingNoticeSent = preMergeReceipt?.status === 'succeeded';
    const quoteContext = conversationContextStore && envelope.event === 'im.message.received'
      ? await conversationContextStore.get(envelope.tenantId, envelope.payload ?? {})
      : null;
    if (
      runtimeSettings.ai_reply_enabled
      && runtimeSettings.conversation_agent_mode === 'active'
      && runtimeSettings.execution_owner === 'agent'
      && shadowAgentScheduler?.schedule
    ) {
      const canary = agentCanaryDecision({
        settings: runtimeSettings, tenantId: envelope.tenantId, eventId: envelope.id,
        runtimeVersion: AGENT_RUNTIME_VERSION, eligible: isDurableActiveLowRiskTurn(envelope, quoteContext),
      });
      if (canary.selected) {
        try {
          await shadowAgentScheduler.schedule(envelope, {
            mode: 'active', contextSnapshot: await durableAgentContextSnapshot(envelope, quoteContext, runtimeSettings),
          });
          await eventStore.complete(record.key, record.leaseId, {
            mode: 'quote_preview_only', execution_owner: 'agent', agent_run_scheduled: true,
            agent_canary_bucket: canary.bucket, agent_canary_percentage: canary.percentage, actions: [],
          });
          return { status: 'completed', mode: 'quote_preview_only', execution_owner: 'agent', actions: [] };
        } catch (error) {
          logger.warn?.('[workflow] durable active scheduling failed; falling back to deterministic owner', { eventId: String(envelope.id), error: String(error?.message ?? error) });
        }
      }
      runtimeSettings = { ...runtimeSettings, conversation_agent_mode: 'shadow', execution_owner: 'deterministic' };
    }
    if ((runtimeSettings.ai_reply_enabled || runtimeSettings.shadow_evaluation_enabled) && runtimeSettings.conversation_agent_mode === 'shadow' && shadowAgentScheduler?.schedule) {
      try {
        await shadowAgentScheduler.schedule(envelope, {
          contextSnapshot: await durableAgentContextSnapshot(envelope, quoteContext, runtimeSettings),
        });
      }
      catch (error) { logger.warn?.('[workflow] durable shadow scheduling failed without blocking buyer flow', { eventId: String(envelope.id), error: String(error?.message ?? error) }); }
    }
    let replyHistoryPromise = null;
    const sharedReplyHistory = () => {
      replyHistoryPromise ??= loadReplyHistory(envelope, runtimeSettings);
      return replyHistoryPromise;
    };
    const shadowAgentResultPromise = (runtimeSettings.ai_reply_enabled || runtimeSettings.shadow_evaluation_enabled)
      && runtimeSettings.conversation_agent_mode === 'shadow'
      && !shadowAgentScheduler
      && conversationAgentPlanner
      ? settle((async () => planShadowConversation(envelope, quoteContext, runtimeSettings, await sharedReplyHistory()))())
      : null;
    const completedUnitQuote = completedUnitQuotePreview(envelope, quoteContext, runtimeSettings)
      ?? deliveredUnitQuoteQuestionPreview(envelope, quoteContext);
    const completedUnitQuoteAction = completedUnitQuote
      ? autoQuoteReplyAction(envelope, completedUnitQuote, autoReplyEnabled, true, runtimeSettings)
      : null;
    if (completedUnitQuoteAction) {
      const result = await executeAction(completedUnitQuoteAction);
      const completedActions = [{ action_id: completedUnitQuoteAction.action_id, ...result }];
      if (result.status === 'succeeded' && completedUnitQuote.unit_replay !== true && conversationContextStore?.markQuoted) {
        await conversationContextStore.markQuoted(envelope.tenantId, envelope.payload ?? {}, {
          unitQuoteCents: completedUnitQuote.unit_quote_cents,
          totalQuoteCents: completedUnitQuote.total_quote_cents,
          ticketCount: completedUnitQuote.ticket_count,
          cinema: completedUnitQuote.cinema,
          ...(completedUnitQuote.quote_scope ? { quoteScope: completedUnitQuote.quote_scope } : {}),
          pricingRuleVersion: completedUnitQuote.pricing_rule_version ?? quoteContext?.facts?.pricing_rule_version,
          replyDelivered: true,
        });
        await conversationContextStore.markQuoteConfirmed?.(envelope.tenantId, envelope.payload ?? {});
      }
      await eventStore.complete(record.key, record.leaseId, {
        mode: 'quote_preview_only', preview_id: null, preview_status: 'preview_ready',
        ...(completedUnitQuote.unit_replay === true ? { quote_skipped: 'unit_quote_replayed' } : {}),
        reply_preview_status: null,
        timings_ms: { recognition: 0, quote: 0, reply: 0, total: Date.now() - processingStartedAt },
        actions: completedActions,
      });
      return { status: 'completed', mode: 'quote_preview_only', preview: completedUnitQuote, actions: completedActions };
    }
    const deterministicFollowUp = quoteContext ? await quoteConversationFollowUp(envelope, quoteContext, runtimeSettings) : null;
    if (deterministicFollowUp) {
      const plannedFollowUps = Array.isArray(deterministicFollowUp) ? deterministicFollowUp : [deterministicFollowUp];
      const followUpActions = [...actions];
      let deliveredFollowUpText = '';
      for (const planned of plannedFollowUps) {
        const result = await executeAction(planned);
        followUpActions.push({ action_id: planned.action_id, ...result });
        if (result.status === 'succeeded' && typeof planned?.text === 'string' && planned.text.trim() && !hasUnresolvedReplyPlaceholder(planned.text)) {
          deliveredFollowUpText = planned.text.trim().slice(0, 1_000);
        }
        if (!['succeeded', 'submitted'].includes(result.status)) break;
      }
      const sourceState = agentStateSnapshot(quoteContext?.facts, processingStartedAt);
      await eventStore.complete(record.key, record.leaseId, {
        mode: 'quote_preview_only', preview_id: null, preview_status: null,
        quote_skipped: 'quoted_conversation_follow_up', reply_preview_status: null,
        ...(Object.keys(sourceState).length ? { agent_state_snapshot: sourceState } : {}),
        ...(deliveredFollowUpText ? { agent_reply_snapshot: { kind: 'conversation_follow_up', text: deliveredFollowUpText } } : {}),
        timings_ms: { recognition: 0, quote: 0, reply: 0, total: Date.now() - processingStartedAt },
        actions: followUpActions,
      });
      return { status: 'completed', mode: 'quote_preview_only', actions: followUpActions };
    }
    // After a verified quote, bare seat/count fragments are often split across
    // several buyer messages. They are not a new official selection or order
    // confirmation, so do not have the free-form reply model answer each part.
    const suppressGenericReply = isBareAcknowledgement(envelope.payload?.content ?? envelope.payload?.text)
      || Boolean(quoteContext && isQuotedSeatOrCountFragment(envelope, quoteContext));
    if (suppressGenericReply) {
      await eventStore.complete(record.key, record.leaseId, {
        mode: 'quote_preview_only', preview_id: null, preview_status: null,
        quote_skipped: 'typed_seat_preference_after_quote', reply_preview_status: null,
        timings_ms: { recognition: 0, quote: 0, reply: 0, total: Date.now() - processingStartedAt },
        actions: [],
      });
      return { status: 'completed', mode: 'quote_preview_only', actions: [] };
    }
    const orderLinked = quotePreviewClient ? await isOrderLinkedChat(envelope) : false;
    const quoteEnvelope = quotePreviewClient && !orderLinked ? await enrichImageWithLatestBuyerText(envelope) : envelope;
    // Recognition starts during prepare so it remains parallel with the first
    // contact platform send. Quote execution and draft claiming stay behind a
    // provider-neutral orchestration boundary with no reply capability.
    const preparedQuote = quoteOrchestrator.prepare({
      envelope, quoteEnvelope, quoteContext, runtimeSettings, orderLinked,
    });
    const canUseTwoStageQuote = preparedQuote.canUseTwoStageQuote;
    const firstContactClaimed = !orderLinked
      && autoReplyEnabled
      && runtimeSettings.ai_reply_enabled
      && typeof conversationContextStore?.claimFirstContactNotice === 'function'
      && await conversationContextStore.claimFirstContactNotice(envelope.tenantId, envelope.payload ?? {});
    // A buyer who already sent an image does not need a screenshot tutorial.
    // Claim the one-time notice slot, then use the bounded progress notice only
    // if recognition/quote is actually slow.
    if (firstContactClaimed && !firstImageUrl(quoteEnvelope.payload)) {
      const notice = withConfiguredReplyImage(autoReplyAction(
        envelope,
        configuredReply(runtimeSettings, 'first_contact_notice', '您好，请发送已标记需要购买位置的完整选座页截图并说明张数，我会按实时优惠核价。收到完整报价后请回复“确认”，再提交订单并保持待付款；仅在收到“价格已修改”后付款。核价试价座位会立即释放，不为买家保留座位。'),
        'first_contact',
        'first-contact-notice',
      ), runtimeSettings, 'first_contact_notice');
      if (notice) {
        let result;
        try {
          result = await executeAction(notice);
        } catch (error) {
          await conversationContextStore.releaseFirstContactNotice?.(envelope.tenantId, envelope.payload ?? {}).catch?.(() => false);
          throw error;
        }
        actions.push({ action_id: notice.action_id, ...result });
        firstContactNoticeSent = result.status === 'succeeded';
      }
    }
    const progressNoticeEligible = canUseTwoStageQuote
      && runtimeSettings.ai_reply_enabled === true
      && runtimeSettings.quote_enabled === true
      && !firstContactNoticeSent
      && (Boolean(firstImageUrl(envelope.payload)) || isQuoteDetailSupplement(envelope.payload) || isLocationQuoteSupplement(envelope.payload));
    const progressDelayMs = Math.max(1, Math.min(60_000, Number(quoteProgressNoticeDelayMs) || 3_000));
    const sendQuoteProcessingNotice = async () => {
      if (!progressNoticeEligible || quoteProcessingNoticeAttempted) return;
      quoteProcessingNoticeAttempted = true;
      const notice = autoReplyAction(
        envelope,
        configuredReply(runtimeSettings, 'quote_processing_notice', '收到，正在按当前信息核对万达实时场次和优惠，请稍等。'),
        'quote_processing',
        'quote-processing-notice',
      );
      if (!notice) return;
      const result = await executeAction({ ...notice, allow_plugin_followup: true });
      actions.push({ action_id: notice.action_id, ...result });
      quoteProcessingNoticeSent = result.status === 'succeeded';
    };
    const awaitQuoteStage = (promise) => awaitWithDelayedNotice(
      promise,
      progressDelayMs - (Date.now() - processingStartedAt),
      sendQuoteProcessingNotice,
    );
    const {
      quoteResult,
      quoteAttemptDeduplicated,
      recognitionReuseReason,
      circledDeliveryInstructionImage,
      recognitionDurationMs,
      quoteDurationMs,
    } = await preparedQuote.complete({ awaitStage: awaitQuoteStage });

    const preview = quoteResult.status === 'fulfilled' ? quoteResult.value : null;
    const hasVerifiedQuoteReply = typeof preview?.reply_text === 'string' && preview.reply_text.trim().length > 0;
    const agentEligible = !hasVerifiedQuoteReply
      && !quoteAttemptDeduplicated
      && !suppressGenericReply
      && !firstContactNoticeSent
      && runtimeSettings.ai_reply_enabled
      && runtimeSettings.conversation_agent_mode === 'active'
      && conversationAgentPlanner;
    const legacyReplyEligible = !hasVerifiedQuoteReply
      && !quoteAttemptDeduplicated
      && !suppressGenericReply
      && runtimeSettings.ai_reply_enabled
      && replyPreviewClient
      && runtimeSettings.conversation_agent_mode !== 'active';
    const replyStartedAt = Date.now();
    const history = agentEligible || legacyReplyEligible ? await sharedReplyHistory() : [];
    const [activeAgentResult, replyResult] = await Promise.all([
      settle(agentEligible ? runConversationAgent(envelope, quoteContext, runtimeSettings, preview, history) : null),
      settle(legacyReplyEligible ? captureReplyPreview(envelope, runtimeSettings, history) : null),
    ]);
    const agentResult = shadowAgentResultPromise ? await shadowAgentResultPromise : activeAgentResult;
    const replyDurationMs = Date.now() - replyStartedAt;
    if (quoteResult.status === 'rejected' && (!replyPreviewClient || replyResult.status === 'rejected') && (!conversationAgentPlanner || agentResult.status === 'rejected')) throw quoteResult.reason;
    if (replyResult.status === 'rejected' && agentResult.status === 'rejected' && !quotePreviewClient) throw replyResult.reason;
    const agentTurn = agentResult.status === 'fulfilled' ? agentResult.value : null;
    if ((agentEligible || shadowAgentResultPromise) && agentTurn && typeof conversationContextStore?.recordAgentTurn === 'function') {
      const lastPlan = Array.isArray(agentTurn.trace) ? agentTurn.trace.at(-1) : null;
      await conversationContextStore.recordAgentTurn(envelope.tenantId, envelope.payload ?? {}, {
        status: agentTurn.status,
        intent: lastPlan?.intent,
        confidence: lastPlan?.confidence,
        goal: lastPlan?.goal,
        action: lastPlan?.action,
        missingFields: lastPlan?.missing_fields,
      });
    }
    const conversationExperienceStatus = agentTurn
      ? await recordConversationExperience(envelope, agentTurn)
      : null;
    const replyPreview = replyResult.status === 'fulfilled' ? replyResult.value : null;
    const buyerPreview = quoteSupersessionPreview(preview, quoteContext, quoteEnvelope, runtimeSettings);
    const redundantFirstContactFollowUp = firstContactNoticeSent
      && preview?.status === 'needs_confirmation'
      && preview?.failure_code === 'text_quote_missing_fields';
    const quoteReply = redundantFirstContactFollowUp
      ? null
      : autoQuoteReplyAction(envelope, buyerPreview, autoReplyEnabled, true, runtimeSettings);
    const agentReply = !autoReplyEnabled || quoteReply || suppressGenericReply || firstContactNoticeSent || runtimeSettings.conversation_agent_mode !== 'active' || agentTurn?.status !== 'reply'
      ? null
      : autoReplyAction(envelope, agentTurn.reply, 'conversation_agent', 'conversation-agent-reply');
    const modelReply = quoteReply || agentReply || suppressGenericReply || firstContactNoticeSent
      ? null
      : autoModelReplyAction(envelope, replyPreview, autoReplyEnabled, hasActiveQuote(quoteContext?.facts));
    const inheritedImageTicketCount = recentExplicitTicketCount(quoteContext?.messages);
    const deterministicIdentityFollowUp = !quoteReply && !agentReply && !modelReply && !suppressGenericReply && !firstContactNoticeSent
      && preview?.status === 'ignored' && firstImageUrl(quoteEnvelope.payload) && inheritedImageTicketCount
      ? quoteFollowUpAction(envelope, `已收到截图和${inheritedImageTicketCount}张需求，但暂时还不能唯一匹配影院场次。请补充城市、完整影院分店名和开场时间，截图无需重发。`)
      : null;
    const safeImageFailureReply = !quoteReply && quoteResult.status === 'rejected'
      ? imageFailureReplyAction(envelope, false, autoReplyEnabled, runtimeSettings)
      : null;
    const duplicateQuoteClosure = !quoteReply && !agentReply && !modelReply && quoteAttemptDeduplicated && quoteProcessingNoticeSent
      ? duplicateQuoteClosureAction(envelope, quoteContext)
      : null;
    let replyAction = quoteReply ?? agentReply ?? modelReply ?? deterministicIdentityFollowUp ?? safeImageFailureReply ?? duplicateQuoteClosure;
    if (replyAction && (firstContactNoticeSent || quoteProcessingNoticeSent) && !quoteReply) {
      replyAction = { ...replyAction, allow_plugin_followup: true, reply_origin: 'quote_follow_up' };
    }
    if (replyAction) {
      replyAction = {
        ...replyAction,
        human_takeover_window_ms: boundedInteger(runtimeSettings.ai_reply_manual_takeover_seconds, 5, 60, 20) * 1_000,
      };
    }
    const confirmationImageUrl = firstImageUrl(quoteEnvelope.payload);
    if (
      replyAction
      && preview?.status === 'needs_confirmation'
      && confirmationImageUrl
      && conversationContextStore?.claimConfirmation
      && !await conversationContextStore.claimConfirmation(envelope.tenantId, quoteEnvelope.payload ?? {}, confirmationImageUrl)
    ) {
      replyAction = null;
    }
    let quoteReplyDelivered = false;
    let authoritativeReplyDelivered = false;
    if (replyAction) {
      const result = await executeAction(replyAction);
      actions.push({ action_id: replyAction.action_id, ...result });
      quoteReplyDelivered = replyAction.action_id === quoteReply?.action_id && result.status === 'succeeded';
      const deterministicFollowUp = [deterministicIdentityFollowUp, safeImageFailureReply, duplicateQuoteClosure]
        .some((action) => action?.action_id === replyAction.action_id);
      const authoritativeAgentReply = replyAction.action_id === agentReply?.action_id && agentTurn?.reason === 'authoritative_tool_response';
      authoritativeReplyDelivered = result.status === 'succeeded' && (quoteReplyDelivered || deterministicFollowUp || authoritativeAgentReply);
    }
    // A background refresh can produce a different price after a human has
    // taken over. Only the quote that was actually delivered to this buyer is
    // eligible to authorize later order-amount validation.
    if (quoteReplyDelivered && circledDeliveryInstructionImage && typeof conversationContextStore?.recordCircledDeliveryInstruction === 'function') {
      await conversationContextStore.recordCircledDeliveryInstruction(
        envelope.tenantId,
        quoteEnvelope.payload ?? {},
        circledDeliveryInstructionImage,
      );
    }
    if (quoteReplyDelivered && preview?.status === 'preview_ready' && conversationContextStore?.markQuoted) {
      await conversationContextStore.markQuoted(envelope.tenantId, quoteEnvelope.payload ?? {}, {
        unitQuoteCents: preview.unit_quote_cents ?? preview.quote_unit_cents,
        totalQuoteCents: preview.total_quote_cents ?? preview.quote_total_cents,
        ticketCount: preview.ticket_count ?? preview.quote_ticket_count,
        cinema: preview.recognition?.cinema,
        ...(preview.recognition?.movie ? { movie: preview.recognition.movie } : {}),
        ...(preview.recognition?.date ? { date: preview.recognition.date } : {}),
        ...(preview.recognition?.showtime ? { showtime: preview.recognition.showtime } : {}),
        ...(preview.recognition?.hall ? { hall: preview.recognition.hall } : {}),
        ...quoteCostEvidence(preview),
        ...(preview.quote_scope ? { quoteScope: preview.quote_scope } : {}),
        pricingRuleVersion: preview.pricing_rule_version,
        ...(preview.pricing_account_ref ? { pricingAccountRef: preview.pricing_account_ref } : {}),
        replyDelivered: true,
      });
    }
    await eventStore.complete(record.key, record.leaseId, {
      mode: 'quote_preview_only',
      execution_owner: runtimeSettings.execution_owner === 'agent' ? 'agent' : 'deterministic',
      preview_id: preview?.id ?? null,
      preview_status: preview?.status ?? (quoteResult.status === 'rejected' ? 'failed' : null),
      ...(quoteResult.status === 'rejected' && quoteResult.reason?.failure_code ? { vision_failure_code: quoteResult.reason.failure_code } : {}),
      ...(quoteResult.status === 'rejected' && Number.isInteger(quoteResult.reason?.http_status) ? { vision_http_status: quoteResult.reason.http_status } : {}),
      ...(quoteResult.status === 'rejected' && quoteResult.reason?.diagnostics && typeof quoteResult.reason.diagnostics === 'object' ? { vision_diagnostics: quoteResult.reason.diagnostics } : {}),
      ...(preview?.failure_code ? { quote_failure_code: preview.failure_code } : {}),
      ...(preview?.diagnostics ? { quote_diagnostics: preview.diagnostics } : {}),
      ...(cinemaMatchEvaluationSnapshot(preview) ? { cinema_match_evaluation_snapshot: cinemaMatchEvaluationSnapshot(preview) } : {}),
      ...(authoritativeReplyDelivered && replyAction?.text && !hasUnresolvedReplyPlaceholder(replyAction.text) ? {
        agent_reply_snapshot: { kind: preview?.status === 'preview_ready' ? 'quote' : 'conversation_follow_up', text: String(replyAction.text).trim().slice(0, 1_000) },
      } : {}),
      ...(quoteAttemptDeduplicated ? { quote_skipped: 'duplicate_quote_draft' } : {}),
      ...(recognitionReuseReason ? { recognition_reused: recognitionReuseReason } : {}),
      ...(preview?.semantic_source ? { semantic_fact_source: String(preview.semantic_source).slice(0, 100) } : {}),
      ...(orderLinked ? { quote_skipped: 'order_linked' } : {}),
      reply_preview_status: replyPreview?.status ?? (replyResult.status === 'rejected' ? 'failed' : null),
      ...(conversationExperienceStatus ? { conversation_experience_status: conversationExperienceStatus } : {}),
      ...(agentEligible || shadowAgentResultPromise ? {
        conversation_agent_mode: runtimeSettings.conversation_agent_mode,
        agent_turn_status: agentResult.status === 'rejected' ? 'failed' : String(agentTurn?.status ?? 'unknown'),
        agent_turn_reason: agentResult.status === 'rejected' ? 'planner_unavailable' : String(agentTurn?.reason ?? '').slice(0, 100),
        agent_intent: String(agentTurn?.trace?.at(-1)?.intent ?? '').slice(0, 32),
        agent_confidence: Number.isFinite(Number(agentTurn?.trace?.at(-1)?.confidence)) ? Number(agentTurn.trace.at(-1).confidence) : null,
        agent_actions: Array.isArray(agentTurn?.trace) ? agentTurn.trace.slice(0, 5).map((item) => String(item?.action ?? '').slice(0, 64)).filter(Boolean) : [],
      } : {}),
      timings_ms: { recognition: recognitionDurationMs, quote: quoteDurationMs, reply: replyDurationMs, total: Date.now() - processingStartedAt },
      ...(quoteStageTimings(preview) ? { quote_stage_timings_ms: quoteStageTimings(preview) } : {}),
      actions,
    });
    return { status: 'completed', mode: 'quote_preview_only', preview, replyPreview, actions };
  }

  async function quoteConversationFollowUp(envelope, quoteContext, runtimeSettings = {}) {
    if (!autoReplyEnabled) return null;
    const facts = quoteContext?.facts ?? {};
    if (!hasActiveQuote(facts)) return null;
    const payload = envelope.payload ?? {};
    const directOrderId = String(payload.orderId ?? payload.order_id ?? '').trim();
    const stage = String(facts.stage ?? '');
    if (directOrderId || (facts.order_id && !['cancelled', 'aftersale'].includes(stage))) return null;
    const buyerText = String(payload.content ?? payload.text ?? '').trim();
    if (isCurrentQuoteQuestion(buyerText)) {
      const unit = positiveCents(facts.quote_unit_cents);
      const total = positiveCents(facts.quote_total_cents);
      const count = positiveCents(facts.quote_ticket_count);
      if (total && count) {
        const amount = unit
          ? `当前有效报价为${(unit / 100).toFixed(2)}元/张，${count}张合计${(total / 100).toFixed(2)}元。`
          : `当前${count}张有效报价合计${(total / 100).toFixed(2)}元。`;
        return quoteFollowUpAction(envelope, `${amount}接受本次报价请回复“确认”；确认后再按提示提交待付款，请不要直接付款。`);
      }
    }
    const requestedCount = requestedTicketCount(buyerText);
    const quotedCount = positiveCents(facts.quote_ticket_count);
    if (requestedCount && quotedCount && requestedCount !== quotedCount) {
      await conversationContextStore?.markQuoteNeedsReview?.(envelope.tenantId, payload);
      return quoteFollowUpAction(envelope, `当前报价只核验了${quotedCount}张，已收到您需要${requestedCount}张。请先不要付款，并发送官方已选好${requestedCount}个座位的截图，我再按实时价格重新核验。`);
    }
    // Typed seats remain an artificial-selection preference only. The price has
    // already been verified, so do not query seats or run another quote.
    if (isExplicitTypedSeatChoice(buyerText)) return null;
    const requestedRow = requestedSeatRow(buyerText)
      ?? (isSeatClarificationQuestion(buyerText) ? recentRequestedSeatRow(quoteContext.messages) : null);
    if (requestedRow && typeof quotePreviewClient?.availableSeats === 'function') {
      const recognition = recognitionFromQuoteDraft(facts.quote_draft);
      if (recognition) {
        try {
          const availability = await quotePreviewClient.availableSeats({ recognition, row: requestedRow });
          const seats = Array.isArray(availability?.seats) ? availability.seats.slice(0, 30) : [];
          if (!seats.length) {
            return quoteFollowUpAction(envelope, `当前万达实时座位图中，${requestedRow}排暂未查到可选W+座位。可以换一排，我再实时查询。`);
          }
          const seatText = seats.join('、');
          if (availability.wplus_offer_available !== true) {
            return quoteFollowUpAction(envelope, `当前${requestedRow}排可选W+专享座位：${seatText}。但W+会员专属优惠已不可用，原报价不能继续，请人工确认。`);
          }
          const reply = configuredReply(runtimeSettings, 'available_wplus_seats', '可以选。当前万达实时座位图中，{排数}排可选W+座位：{可选座位}。')
            .replaceAll('{排数}', String(requestedRow))
            .replaceAll('{可选座位}', seatText);
          return withConfiguredReplyImage(quoteFollowUpAction(envelope, reply), runtimeSettings, 'available_wplus_seats');
        } catch {
          return quoteFollowUpAction(envelope, `当前暂时无法读取${requestedRow}排实时可选座位，请稍后再试。`);
        }
      }
    }
    if (isQuoteConfirmation(buyerText)) {
      const requestedCount = conflictingRecentTicketCount(quoteContext.messages, facts.quote_ticket_count);
      if (requestedCount) {
        if (typeof conversationContextStore?.markQuoteNeedsReview === 'function') {
          await conversationContextStore.markQuoteNeedsReview(envelope.tenantId, envelope.payload ?? {});
        }
        return quoteFollowUpAction(envelope, `我目前只核到 ${facts.quote_ticket_count} 个官方已选座；您需要 ${requestedCount} 张，灰色座位尚未核实可售。请发官方已选好 ${requestedCount} 个座位的截图，我再实时核价。`);
      }
      if (typeof conversationContextStore?.markQuoteConfirmed === 'function') {
        const confirmed = await conversationContextStore.markQuoteConfirmed(envelope.tenantId, envelope.payload ?? {});
        if (confirmed === false) {
          return quoteFollowUpAction(envelope, '当前报价缺少有效规则版本或送达确认，不能进入下单流程。请发送最新完整选座页，我重新实时核价。');
        }
      }
      const customGuideImage = configuredReplyImage(runtimeSettings, 'order_submit_before_payment');
      const instruction = withConfiguredReplyImage(quoteFollowUpAction(envelope, paymentSafeOrderInstruction(configuredReply(
        runtimeSettings,
        'order_submit_before_payment',
        '点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）\n提交订单后请先不要付款，等待系统确认改价成功后再付款。',
      ))), runtimeSettings, 'order_submit_before_payment');
      const guide = isQuotePurchaseIntent(buyerText) || customGuideImage ? null : orderSubmitGuideAction(envelope);
      return [instruction, guide].filter(Boolean);
    }
    if (isSeatClarificationQuestion(buyerText)) {
      const count = Number(facts.quote_ticket_count);
      const countText = Number.isInteger(count) && count > 0 ? `${count}个位置` : '图中圈选的位置';
      return quoteFollowUpAction(envelope, `我看到的是${countText}；截图尚未显示官方已选座，暂不能确认最终锁定的具体座位。确认后请提交订单并先不要付款，我核对后改价。订单付款后不支持退改签。`);
    }
    return null;
  }

  function requestedSeatRow(value) {
    const text = String(value ?? '').trim();
    const numeric = text.match(/(?:第\s*)?(\d{1,2})\s*排/u);
    if (numeric) return Number(numeric[1]);
    const chinese = text.match(/(?:第\s*)?([一二三四五六七八九十两]{1,3})\s*排/u);
    if (!chinese) return null;
    const digits = { 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
    if (chinese[1] === '十') return 10;
    if (chinese[1].startsWith('十')) return 10 + (digits[chinese[1][1]] ?? 0);
    if (chinese[1].endsWith('十')) return (digits[chinese[1][0]] ?? 0) * 10;
    return digits[chinese[1]] ?? null;
  }

  function recentRequestedSeatRow(messages) {
    const recent = Array.isArray(messages) ? messages.slice(-12).reverse() : [];
    return recent.map((message) => requestedSeatRow(message?.text)).find((row) => Number.isInteger(row)) ?? null;
  }

  function recognitionFromQuoteDraft(draft) {
    if (!draft || typeof draft !== 'object' || Number(draft.expires_at ?? 0) <= Date.now()) return null;
    const fields = draft.fields && typeof draft.fields === 'object' ? draft.fields : {};
    const value = (name) => String(fields[name]?.value ?? '').trim();
    if (!value('cinema') || !value('movie') || !/^\d{4}-\d{2}-\d{2}$/u.test(value('date')) || !/^\d{2}:\d{2}(?:-\d{2}:\d{2})?$/u.test(value('showtime'))) return null;
    return {
      image_type: 'SEAT_MAP', platform: 'UNKNOWN', container: 'SEAT_MAP',
      city: value('city') || null, cinema: value('cinema'), movie: value('movie'), date: value('date'),
      showtime: value('showtime'), hall: value('hall') || null, seat_zone_types: ['W+'],
      official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
    };
  }

  function isQuotedSeatOrCountFragment(envelope, quoteContext) {
    if (!hasActiveQuote(quoteContext?.facts ?? {})) return false;
    const value = String(envelope?.payload?.content ?? envelope?.payload?.text ?? '').trim();
    if (!value) return false;
    // Examples: “10排 7 8 座” and “2”. A later official selected-seat image
    // still enters the realtime quote path normally.
    return /^\d{1,2}\s*(?:张|座)?$/u.test(value)
      || /^\d+\s*排[\d\s、,，.．-]+(?:座|号)$/u.test(value)
      || /^(?:就|要|是)?这(?:两|2)个(?:座位|位置)[。.!！]?$/u.test(value)
      || isExplicitTypedSeatChoice(value);
  }

  async function isOrderLinkedChat(envelope) {
    const payload = envelope?.payload ?? {};
    const accountUnb = String(payload.accountUnb ?? payload.account_unb ?? '').trim();
    const chatId = String(payload.chatId ?? payload.chat_id ?? '').trim();
    if (!accountUnb || !chatId) return false;
    const contextState = await conversationContextStore?.get?.(envelope.tenantId, payload).catch?.(() => null);
    if (['cancelled', 'aftersale'].includes(String(contextState?.facts?.stage ?? ''))) return false;
    const core = coreFor(envelope.tenantId);
    const directOrderId = String(payload.orderId ?? payload.order_id ?? '').trim();
    let sessions;
    if (directOrderId) {
      sessions = [{ orderId: directOrderId }];
    } else {
      try {
        const page = await core.im.listSessions({ accountUnb, page: 1, pageSize: 100 });
        sessions = Array.isArray(page?.items) ? page.items : [];
      } catch (error) {
        // No order link was established. Do not let a session-directory outage
        // suppress a genuine new buyer image.
        logger.warn?.('[workflow] unable to list chat order links', { error: String(error?.message ?? error) });
        return false;
      }
    }
    const linked = sessions.filter((session) => (
      (directOrderId || String(session?.chatId ?? session?.chat_id ?? '').trim() === chatId)
      && Boolean(String(session?.orderId ?? session?.order_id ?? '').trim())
    ));
    if (!linked.length) return false;
    if (typeof core.orders?.get !== 'function') return true;
    for (const session of linked) {
      if (isTerminalHistoricalOrder(session)) continue;
      const orderId = String(session?.orderId ?? session?.order_id ?? '').trim();
      try {
        const order = await core.orders.get(orderId);
        if (!isTerminalHistoricalOrder(order)) return true;
      } catch (error) {
        // Once a concrete order is linked, inability to prove it closed must
        // fail closed to prevent re-quoting a possibly paid order.
        logger.warn?.('[workflow] unable to verify linked order status', { error: String(error?.message ?? error) });
        return true;
      }
    }
    return false;
  }

  async function enrichImageWithLatestBuyerText(envelope) {
    const payload = envelope.payload ?? {};
    const context = conversationContextStore ? await conversationContextStore.get(envelope.tenantId, payload) : null;
    const directImage = firstImageUrl(payload);
    if (!directImage && !isQuoteDetailSupplement(payload) && !isLocationQuoteSupplement(payload)) return envelope;
    const eventReceivedAt = Number(envelope.ts);
    const referenceTime = Number.isFinite(eventReceivedAt) ? eventReceivedAt : Date.now();
    const inheritedImage = directImage ? null : recentContextImage(context, referenceTime);
    const imageUrl = directImage ?? inheritedImage;
    if (!imageUrl) return envelope;
    const pairedImages = recentContextImages(context, referenceTime, MULTI_IMAGE_PAIR_WINDOW_MS);
    const recognitionImages = [...new Set([...pairedImages, imageUrl])].slice(-2);

    const address = messageContext(envelope);
    let latestText = '';
    if (address.account_unb && address.chat_id) {
      const page = await coreFor(envelope.tenantId).im.listMessages({ accountUnb: address.account_unb, chatId: address.chat_id, pageSize: 50 });
      // Only merge a real inbound buyer text message. Never feed a seller reply,
      // another image URL, or a system message into the quote recognizer.
      const latestBuyerText = (Array.isArray(page?.items) ? page.items : [])
        .find((item) => {
          const direction = String(item?.direction ?? '').toLowerCase();
          const text = String(item?.content ?? item?.text ?? '').trim();
          return (direction === 'inbound' || direction === 'buyer') && text && !isImageMessageText(text);
        });
      latestText = String(latestBuyerText?.content ?? latestBuyerText?.text ?? '').trim();
    }
    const contextualText = Array.isArray(context?.messages)
      ? context.messages.map((message) => String(message?.text ?? '').trim()).filter(Boolean).join('\n')
      : '';
    const draftText = activeQuoteDraftText(context?.facts?.quote_draft);
    // Put the buyer's newest supplement first. The parser uses the first
    // explicit date/time where messages conflict, while the labelled draft
    // supplies only facts collected in the current short quote round.
    const content = [String(payload.content ?? payload.text ?? '').trim(), latestText, contextualText, draftText]
      .filter(Boolean).filter((value, index, values) => values.indexOf(value) === index).join('\n').slice(0, 4_000);
    return {
      ...envelope,
      payload: { ...payload, imageUrls: recognitionImages, ...(content ? { content } : {}), ...(context?.facts?.quote_draft ? { quote_draft: context.facts.quote_draft } : {}) },
    };
  }

  async function durableAgentContextSnapshot(envelope, knownState, settings) {
    const state = knownState ?? (conversationContextStore
      ? await conversationContextStore.get(envelope.tenantId, envelope.payload ?? {})
      : { facts: {}, messages: [] });
    let history = [];
    try { history = await loadReplyHistory(envelope, settings); }
    catch (error) {
      logger.warn?.('[workflow] source-time seller history unavailable; using bounded buyer context', {
        eventId: String(envelope.id), error: String(error?.message ?? error),
      });
    }
    return {
      facts: state?.facts ?? {},
      messages: history.length ? history : Array.isArray(state?.messages) ? state.messages : [],
    };
  }

  async function planShadowConversation(envelope, quoteContext, settings, history = []) {
    const state = { ...(quoteContext ?? { facts: {} }), messages: Array.isArray(history) ? history : [] };
    const context = {
      event_id: String(envelope.id),
      tenant_id: String(envelope.tenantId),
      latest_message: String(envelope.payload?.content ?? envelope.payload?.text ?? '').trim() || '[图片或非文本消息]',
      has_image: Boolean(firstImageUrl(envelope.payload)),
      settings,
      state,
      observations: [],
      now: Date.now(),
      human_takeover: false,
    };
    const plan = guardAgentPlan(normalizeAgentPlan(await conversationAgentPlanner.plan(context)), context);
    const authorization = authorizeAgentPlan(plan, context);
    return {
      status: 'shadow',
      reason: authorization.reason,
      reply: null,
      trace: [{
        step: 1,
        action: plan.action,
        intent: plan.intent,
        confidence: plan.confidence,
        goal: plan.goal,
        missing_fields: [...plan.missing_fields],
        ...(plan.experience_candidate ? { experience_candidate: plan.experience_candidate } : {}),
      }],
    };
  }

  async function recordConversationExperience(envelope, agentTurn) {
    if (typeof backend.recordConversationExperience !== 'function') return null;
    const trace = Array.isArray(agentTurn?.trace) ? agentTurn.trace : [];
    const candidate = trace.slice().reverse().find((item) => item?.experience_candidate)?.experience_candidate;
    if (!candidate) return null;
    try {
      const result = await backend.recordConversationExperience({
        tenant_id: String(envelope.tenantId),
        event_id: String(envelope.id),
        candidate,
      }, {
        tenantId: envelope.tenantId,
        eventId: `${envelope.id}:conversation-experience`,
      });
      return ['draft_created', 'draft_updated'].includes(result?.status) ? result.status : 'rejected';
    } catch (error) {
      logger.warn?.('[workflow] conversation experience draft rejected', {
        eventId: String(envelope.id),
        error: String(error?.code ?? error?.message ?? 'unknown'),
      });
      return 'failed';
    }
  }

  async function runConversationAgent(envelope, quoteContext, settings, preview, history = []) {
    const baseState = quoteContext ?? { facts: {}, messages: [] };
    const state = { ...baseState, messages: Array.isArray(history) && history.length ? history : baseState.messages ?? [] };
    const previewReply = String(preview?.reply_text ?? '').trim();
    const previewFacts = {
      ticket_count: positiveCents(preview?.ticket_count ?? preview?.quote_ticket_count),
      unit_quote_cents: positiveCents(preview?.unit_quote_cents ?? preview?.quote_unit_cents),
      total_quote_cents: positiveCents(preview?.total_quote_cents ?? preview?.quote_total_cents),
      failure_code: String(preview?.failure_code ?? '').trim() || null,
      status: String(preview?.status ?? '').trim() || null,
    };
    const observationForPreview = (tool) => previewReply
      ? { status: 'success', tool, summary: '权威核价流程已返回买家文案', facts: previewFacts, authoritative_reply: previewReply, next_actions: ['respond'] }
      : { status: 'error', tool, summary: '本轮没有形成可发送的权威结果', facts: previewFacts, next_actions: ['handoff'], stop_reason: previewFacts.failure_code || 'authoritative_result_unavailable' };
    const recognition = preview?.recognition && typeof preview.recognition === 'object' ? preview.recognition : {};
    const identityFacts = Object.fromEntries(['city', 'cinema', 'movie', 'date', 'showtime', 'hall']
      .map((key) => [key, recognition[key] ?? state?.facts?.[key]])
      .filter(([, value]) => value != null && String(value).trim()));
    const agent = createConversationAgent({
      planner: conversationAgentPlanner,
      maxSteps: 8,
      mode: settings?.conversation_agent_mode === 'active' ? 'active' : 'shadow',
      tools: {
        async recognize_image() { return { status: 'success', tool: 'recognize_image', summary: '权威识图已完成', facts: identityFacts, next_actions: ['resolve_showtime'] }; },
        async resolve_showtime() { return preview?.status === 'preview_ready' || previewReply ? { status: 'success', tool: 'resolve_showtime', summary: '影院场次已唯一匹配', facts: identityFacts, next_actions: ['quote_realtime'] } : observationForPreview('resolve_showtime'); },
        async quote_realtime() { return observationForPreview('quote_realtime'); },
        async recognize_and_quote() { return observationForPreview('recognize_and_quote'); },
        async list_available_wplus_seats() { return observationForPreview('list_available_wplus_seats'); },
        async record_seat_preference() {
          const latestMessage = String(envelope.payload?.content ?? envelope.payload?.text ?? '').trim();
          const isTypedSeatOnly = /\d{1,2}排.{0,12}\d{1,2}(?:座|号)?/u.test(latestMessage)
            && !/(?:红点|绿点|圈|画|标)/u.test(latestMessage);
          if (isTypedSeatOnly) {
            return {
              status: 'success', tool: 'record_seat_preference', summary: '文字座位仅按人工选座信息记录', facts: { preference_recorded: true },
              authoritative_reply: '已记录您文字提供的座位信息；它不代表官方已选座或已锁座，出票时会按实时可选情况由人工处理。', next_actions: ['respond'],
            };
          }
          const instructionImage = [...(state.messages ?? [])]
            .reverse()
            .flatMap((message) => Array.isArray(message?.image_urls) ? message.image_urls : [])
            .find((value) => typeof value === 'string' && value.trim()) ?? firstImageUrl(envelope.payload);
          await conversationContextStore?.recordCircledDeliveryInstruction?.(
            envelope.tenantId,
            envelope.payload ?? {},
            instructionImage,
          );
          return {
            status: 'success', tool: 'record_seat_preference', summary: '已记录按买家原图圈选位置出票的履约指令', facts: { circled_delivery_instruction_recorded: true },
            authoritative_reply: configuredReply(settings, 'manual_delivery_preference', '已记录：出票时按您原图圈选的位置操作，无需提供具体座位号。若该位置届时不可选，会先联系您确认，不会擅自换座。'), next_actions: ['respond'],
          };
        },
        async create_manual_task({ plan }) {
          if (!manualTaskStore) return { status: 'error', tool: 'create_manual_task', summary: '人工任务队列不可用', next_actions: ['handoff'], stop_reason: 'manual_task_tool_unavailable' };
          const payload = envelope.payload ?? {};
          const created = await manualTaskStore.create({
            taskId: `agent:${String(envelope.tenantId)}:${String(envelope.id)}:manual`,
            tenantId: String(envelope.tenantId), eventId: String(envelope.id),
            accountUnb: String(payload.accountUnb ?? payload.account_unb ?? ''),
            chatId: String(payload.chatId ?? payload.chat_id ?? ''),
            peerUnb: String(payload.peerUnb ?? payload.peer_unb ?? ''),
            orderId: String(state?.facts?.order_id ?? ''),
            reasonCode: 'agent_requested_manual_review', summary: 'Agent请求人工处理', source: 'agent',
          });
          return {
            status: 'success', tool: 'create_manual_task', summary: created.created ? '已创建人工处理任务' : '人工处理任务已存在',
            facts: { manual_task_created: created.created },
            authoritative_reply: configuredReply(settings, 'manual_handoff', '当前问题无法由自动工具核验，已转人工客服在本会话继续处理。'),
            next_actions: ['respond'],
          };
        },
        async confirm_active_quote() {
          return { status: 'error', tool: 'confirm_active_quote', summary: '确认必须由确定性订单流程处理', next_actions: ['handoff'], stop_reason: 'deterministic_confirmation_required' };
        },
        async get_order_status() {
          const stage = String(state?.facts?.stage ?? '');
          const paid = ['paid', 'paid_manual_delivery', 'ticket_issued', 'ticket_sent'].includes(stage);
          return {
            status: stage ? 'success' : 'error', tool: 'get_order_status', summary: stage ? `订单阶段：${stage}` : '未找到关联订单', facts: { stage },
            ...(paid ? { authoritative_reply: configuredReply(settings, 'order_paid', '订单已付款，后续由人工出票或售后处理，不会重新核价。') } : {}),
            next_actions: stage ? ['respond'] : ['handoff'], ...(stage ? {} : { stop_reason: 'linked_order_missing' }),
          };
        },
      },
    });
    return agent.runTurn({
      event_id: String(envelope.id),
      tenant_id: String(envelope.tenantId),
      latest_message: String(envelope.payload?.content ?? envelope.payload?.text ?? '').trim() || '[图片或非文本消息]',
      has_image: Boolean(firstImageUrl(envelope.payload)),
      settings,
      state,
      observations: [],
      now: Date.now(),
      human_takeover: false,
    });
  }

  async function loadReplyHistory(envelope, settings = {}) {
    const address = messageContext(envelope);
    if (!address.account_unb || !address.chat_id) {
      throw nonRetryable('reply preview requires account_unb and chat_id');
    }
    const core = coreFor(envelope.tenantId);
    const page = await core.im.listMessages({
      accountUnb: address.account_unb,
      chatId: address.chat_id,
      pageSize: boundedInteger(settings.ai_reply_memory_depth, 5, 50, 20),
    });
    const depth = boundedInteger(settings.ai_reply_memory_depth, 5, 50, 20);
    const historyHours = boundedInteger(settings.ai_reply_memory_hours, 1, 24, 24);
    const cutoff = Date.now() - historyHours * 60 * 60 * 1_000;
    const sourceAt = Number(envelope.ts);
    const selected = (Array.isArray(page?.items) ? page.items : [])
      .slice(0, depth)
      .filter((item) => {
        const sentAt = Date.parse(String(item?.sentAt ?? item?.sent_at ?? ''));
        return (!Number.isFinite(sentAt) || sentAt >= cutoff)
          && (!Number.isFinite(sourceAt) || !Number.isFinite(sentAt) || sentAt <= sourceAt + 1_000);
      })
      .slice().reverse();
    return Promise.all(selected.map(async (message) => {
      const direction = String(message?.direction ?? '').toLowerCase();
      if (direction !== 'outbound') return toReplyHistoryMessage(message, 'buyer');
      const messageId = String(message?.messageId ?? message?.message_id ?? '').trim();
      if (!messageId || typeof eventStore?.wasSentMessage !== 'function') return toReplyHistoryMessage(message, 'unknown');
      try {
        const sentByPlugin = await eventStore.wasSentMessage(envelope.tenantId, address.chat_id, messageId);
        return toReplyHistoryMessage(message, sentByPlugin ? 'plugin' : 'external_seller');
      } catch {
        return toReplyHistoryMessage(message, 'unknown');
      }
    }));
  }

  async function captureReplyPreview(envelope, settings = {}, history = null) {
    const messages = Array.isArray(history) ? history : await loadReplyHistory(envelope, settings);
    return replyPreviewClient.capture(envelope, messages);
  }

  async function handleMessage(envelope, taskId, upsert, settings) {
    if (!settings.automation_enabled) return upsert;
    const imageUrl = firstImageUrl(envelope.payload);
    if (!imageUrl) return runAgent(envelope, taskId, false, upsert);
    if (!settings.recognition_enabled) return upsert;

    const image = await imageLoader.load(imageUrl);
    if (!settings.ai_only_mode_enabled) {
      if (typeof backend.submitOcrRecognition !== 'function') {
        return requestReview(envelope, taskId, 'backend_ocr_pipeline_unavailable');
      }
      return backend.submitOcrRecognition(taskId, {
        tenant_id: String(envelope.tenantId),
        event_id: `${envelope.id}:ocr-recognition`,
        image_data_url: dataUrl(image),
        message: String(envelope.payload?.content ?? ''),
        context: messageContext(envelope),
      }, context(envelope, 'ocr-recognition'));
    }
    if (typeof backend.submitAiVisionRecognition !== 'function') {
      return requestReview(envelope, taskId, 'backend_ai_vision_gateway_unavailable');
    }
    return backend.submitAiVisionRecognition(taskId, {
      tenant_id: String(envelope.tenantId),
      event_id: `${envelope.id}:ai-vision-recognition`,
      image_data_url: dataUrl(image),
      message: String(envelope.payload?.content ?? ''),
      context: messageContext(envelope),
    }, context(envelope, 'ai-vision-recognition'));
  }

  function runAgent(envelope, taskId, hasImage, upsert) {
    if (typeof backend.runAgent !== 'function') return upsert;
    return backend.runAgent({
      tenant_id: String(envelope.tenantId),
      event_id: `${envelope.id}:agent-run`,
      task_id: taskId,
      source_event_id: String(envelope.id),
      conversation_id: String(envelope.payload?.chatId ?? envelope.payload?.chat_id ?? envelope.payload?.peerUnb ?? envelope.payload?.peer_unb ?? taskId),
      message: String(envelope.payload?.content ?? '').trim() || (hasImage ? '[image]' : '[empty]'),
      has_image: hasImage,
      max_tool_calls: 6,
      max_run_seconds: 45,
      context: messageContext(envelope),
    }, context(envelope, 'agent-run'));
  }

  function requestReview(envelope, taskId, reason) {
    return backend.updateTaskStatus(taskId, {
      tenant_id: String(envelope.tenantId),
      event_id: `${envelope.id}:review`,
      status: 'review_required',
      error: reason,
      source_event: envelope.event,
    }, context(envelope, 'review'));
  }

  async function isOwnedShopPeer(envelope) {
    const payload = envelope?.payload ?? {};
    const peerUnb = String(payload.peerUnb ?? payload.peer_unb ?? '').trim();
    if (!peerUnb) return false;
    try {
      const shops = await coreFor(envelope.tenantId).shops.list();
      return Array.isArray(shops) && shops.some((shop) => String(shop?.unb ?? '').trim() === peerUnb);
    } catch (error) {
      // A lookup failure must not discard a genuine buyer message.
      logger.warn?.('[workflow] unable to verify whether the peer is an owned shop', { error: String(error?.message ?? error) });
      return false;
    }
  }

  async function loadRuntimeSettings(envelope) {
    if (typeof backend.getRuntimeSettings !== 'function') {
      throw nonRetryable('backend runtime settings client is unavailable');
    }
    const accountUnb = String(envelope?.payload?.accountUnb ?? envelope?.payload?.account_unb ?? '').trim();
    const response = await backend.getRuntimeSettings(accountUnb);
    const settings = response?.settings;
    if (!settings || typeof settings !== 'object' || Array.isArray(settings)) {
      throw nonRetryable('backend runtime settings response is invalid');
    }
    const executionOwner = settings.execution_owner === 'agent' ? 'agent' : 'deterministic';
    const requestedAgentMode = ['shadow', 'active'].includes(settings.conversation_agent_mode) ? settings.conversation_agent_mode : 'off';
    return Object.freeze({
      automation_enabled: settings.automation_enabled === true && settings.shop_enabled !== false && settings.execution_mode !== 'off',
      recognition_enabled: settings.recognition_enabled ?? settings.shop_features?.recognition_enabled ?? settings.automation_enabled === true,
      quote_enabled: settings.quote_enabled ?? settings.shop_features?.quote_enabled ?? settings.automation_enabled === true,
      price_change_enabled: settings.price_change_enabled ?? settings.shop_features?.price_change_enabled ?? settings.auto_price_change === true,
      ai_reply_enabled: settings.ai_reply_enabled === true,
      shadow_evaluation_enabled: settings.shadow_evaluation_enabled === true,
      conversation_agent_mode: requestedAgentMode === 'active' && executionOwner !== 'agent' ? 'shadow' : requestedAgentMode,
      execution_owner: executionOwner,
      agent_canary_enabled: settings.agent_canary_enabled === true,
      agent_canary_kill_switch: settings.agent_canary_kill_switch !== false,
      agent_canary_percentage: boundedInteger(settings.agent_canary_percentage, 1, 100, 0),
      agent_canary_approved: settings.agent_canary_approved === true,
      agent_canary_runtime_version: String(settings.agent_canary_runtime_version ?? '').trim().slice(0, 100),
      ai_only_mode_enabled: settings.ai_only_mode_enabled === true,
      ai_reply_memory_hours: boundedInteger(settings.ai_reply_memory_hours, 1, 24, 24),
      ai_reply_memory_depth: boundedInteger(settings.ai_reply_memory_depth, 5, 50, 20),
      ai_reply_delay_seconds: boundedInteger(settings.ai_reply_delay_seconds, 2, 60, 3),
      ai_reply_manual_takeover_seconds: boundedInteger(settings.ai_reply_manual_takeover_seconds, 5, 60, 20),
      reply_templates: settings.reply_templates && typeof settings.reply_templates === 'object' && !Array.isArray(settings.reply_templates)
        ? Object.freeze({ ...settings.reply_templates })
        : Object.freeze({}),
      reply_template_images: settings.reply_template_images && typeof settings.reply_template_images === 'object' && !Array.isArray(settings.reply_template_images)
        ? Object.freeze({ ...settings.reply_template_images })
        : Object.freeze({}),
    });
  }

  async function tick() {
    const record = await eventStore.claimDue();
    // Legacy delivery/reminder queues are intentionally never consumed. Ticket
    // codes, shipping, receipt reminders and ratings remain manual operations.
    if (!record) return null;
    return processClaimed(record);
  }

  return Object.freeze({ enqueueEvent, processClaimed, tick });

  async function markAiReplySentIfNeeded(action, result) {
    if (typeof backend.markAiReplySent !== 'function') return;
    const eventId = String(action.ai_reply_event_id ?? '').trim();
    const taskId = String(action.task_id ?? '').trim();
    if (!eventId || !taskId || !result || !result.message_id) return;
    try {
      await backend.markAiReplySent(eventId, {
        tenant_id: String(action.tenant_id),
        task_id: taskId,
      }, {
        tenantId: action.tenant_id,
        eventId: `${eventId}:sent`,
      });
    } catch (error) {
      console.warn('failed to mark ai reply as sent', {
        eventId,
        taskId,
        error: String(error?.message ?? error),
      });
    }
  }
}
