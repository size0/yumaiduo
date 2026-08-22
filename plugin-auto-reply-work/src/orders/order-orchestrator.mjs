import { classifyPriceChangeError } from '../price-change-error.mjs';
import { requestedTicketCount } from '../agent/ticket-request-inspector.mjs';
import { EVENT_ROUTE_KIND } from '../event-router.mjs';
import { isQuoteConfirmation } from '../conversation/message-classifier.mjs';
import { hasActiveQuote } from '../quote/quote-followup-policy.mjs';
import {
  configuredReply,
  createQuoteFollowUpAction,
  withConfiguredReplyImage,
} from '../reply/reply-policy.mjs';

const CLAIMED_ORDER_ROUTE_KINDS = new Set([
  EVENT_ROUTE_KIND.ORDER_CREATED,
  EVENT_ROUTE_KIND.ORDER_PAID,
  EVENT_ROUTE_KIND.ORDER_PRICE_CHANGED,
]);

/**
 * Owns deterministic order lifecycle handling. The orchestrator may execute
 * only already-gated actions; it never asks a model for amounts, identifiers,
 * payment state, or lifecycle transitions.
 */
export function createOrderOrchestrator({
  backend,
  coreFor,
  eventStore,
  conversationContextStore,
  executeAction,
  logger = console,
} = {}) {
  if (!backend || typeof coreFor !== 'function' || !eventStore || typeof executeAction !== 'function') {
    throw new TypeError('order orchestrator dependencies are required');
  }

  async function process(record, eventRoute) {
    if (!CLAIMED_ORDER_ROUTE_KINDS.has(eventRoute?.kind)) return null;
    if (eventRoute.kind === EVENT_ROUTE_KIND.ORDER_CREATED) return processOrderCreated(record);
    if (eventRoute.kind === EVENT_ROUTE_KIND.ORDER_PAID) return processOrderPaid(record);
    return processOrderPriceChanged(record);
  }

  async function updateConversationStage(envelope, eventRoute) {
    if (!conversationContextStore) return;
    const orderId = orderIdFrom(envelope);
    if (!orderId) return;
    const getSessionByOrder = coreFor(envelope.tenantId).im?.getSessionByOrder;
    if (typeof getSessionByOrder !== 'function') return;
    try {
      const session = await getSessionByOrder(orderId);
      const payload = sessionPayload(session);
      if (!hasReplyAddress(payload)) return;
      const settings = await loadRuntimeSettingsForAccount(envelope.tenantId, payload.accountUnb);
      if (!settings.automation_enabled) return;
      // Manual, stale, or unrelated price changes must be verified by
      // processOrderPriceChanged before the chat can enter waiting_payment.
      if (eventRoute?.kind === EVENT_ROUTE_KIND.ORDER_PRICE_CHANGED) return;
      if (eventRoute?.kind === EVENT_ROUTE_KIND.ORDER_CREATED && typeof conversationContextStore.bindOrder === 'function') {
        await conversationContextStore.bindOrder(envelope.tenantId, payload, orderId, envelope.payload ?? {});
        return;
      }
      if (eventRoute?.conversation_stage && typeof conversationContextStore.setOrderStage === 'function') {
        await conversationContextStore.setOrderStage(envelope.tenantId, payload, eventRoute.conversation_stage, orderId);
      }
    } catch (error) {
      logger.warn?.('[workflow] unable to update order conversation stage', { error: String(error?.message ?? error) });
    }
  }

  async function processOrderCreated(record) {
    const orderContext = await loadOrderContext(record.envelope);
    if (!orderContext) return completeIgnored(record, 'order_session_or_quote_missing');
    const { session, orderId, messages } = orderContext;
    let { facts } = orderContext;
    const contextPayload = sessionPayload(session);
    const conflictingCount = conflictingRecentTicketCount(messages, facts.quote_ticket_count);
    if (conflictingCount) {
      await conversationContextStore.markOrderException?.(record.envelope.tenantId, contextPayload, 'ticket_count_conflict', orderId);
      const warning = createQuoteFollowUpAction(
        withSessionPayload(record.envelope, contextPayload),
        `当前订单关联的报价只核验了${facts.quote_ticket_count}张，但已收到您需要${conflictingCount}张。请先不要付款，并发送官方已选好${conflictingCount}个座位的截图，已转人工处理。`,
      );
      const warningResult = warning ? await executeAction(warning) : { status: 'skipped', reason: 'reply_address_missing' };
      const actions = warning ? [{ action_id: warning.action_id, ...warningResult }] : [];
      await eventStore.complete(record.key, record.leaseId, {
        mode: 'quote_preview_only',
        order_price_change: { status: 'blocked', reason: 'ticket_count_conflict' },
        actions,
      });
      return { status: 'completed', mode: 'quote_preview_only', actions };
    }

    // order.created can race ahead of the delayed message worker. Only a
    // persisted bounded confirmation phrase may bridge that race.
    if (!facts.quote_confirmed && hasActiveQuote(facts) && hasRecentQuotePurchaseIntent(messages)) {
      await conversationContextStore.markQuoteConfirmed?.(record.envelope.tenantId, contextPayload);
      facts = { ...facts, quote_confirmed: true };
    }
    if (!facts.quote_confirmed || !hasActiveQuote(facts)) return completeIgnored(record, 'buyer_quote_not_confirmed_or_expired');

    const settings = await loadRuntimeSettingsForAccount(record.envelope.tenantId, session.accountUnb);
    if (!settings.automation_enabled || !settings.price_change_enabled) return completeIgnored(record, 'auto_price_change_disabled');
    const total = positiveCents(facts.quote_total_cents);
    const quantity = positiveCents(facts.quote_ticket_count);
    if (!total || !quantity) return completeIgnored(record, 'quote_terms_missing');

    const action = {
      action_id: `${record.envelope.id}:quoted-order-price-change`,
      kind: 'change_price',
      tenant_id: String(record.envelope.tenantId),
      account_unb: String(session.accountUnb),
      order_id: orderId,
      price_fee: total,
      transport_fee: 0,
      expected_total_cents: total,
      expected_quantity: quantity,
      order_quantity_policy: 'listing_unit',
      gates: {
        feature_enabled: true,
        unique_showtime: true,
        quantity_confirmed: true,
        selection_confirmed: true,
        quote_valid: true,
        order_linked: true,
        human_takeover: false,
        max_amount_cents: 200_000,
      },
    };
    try {
      const result = await executeAction(action);
      if (result.status === 'skipped' && result.reason === 'price_change_gate_failed') {
        const failure = Array.isArray(result.failures) && result.failures.length ? String(result.failures[0]) : 'unknown';
        await conversationContextStore.markOrderException?.(
          record.envelope.tenantId,
          contextPayload,
          `price_change_gate_${failure}`.slice(0, 100),
          orderId,
        );
      }
      const actions = [{ action_id: action.action_id, ...result }];
      await eventStore.complete(record.key, record.leaseId, { mode: 'quote_preview_only', order_price_change: result, actions });
      return { status: 'completed', mode: 'quote_preview_only', actions };
    } catch (error) {
      const failure = classifyPriceChangeError(error);
      if (!failure.terminal) throw error;
      const message = failure.kind === 'authorization_failed'
        ? configuredReply(settings, 'price_change_authorization_failed', '平台订单改价授权失败，请先不要付款，已转人工核查。')
        : configuredReply(settings, 'order_price_change_failed', '当前订单金额无法自动修改，请先不要付款，已转人工处理。');
      const failureAction = createQuoteFollowUpAction(withSessionPayload(record.envelope, contextPayload), message);
      const failureResult = failureAction ? await executeAction(failureAction) : { status: 'skipped', reason: 'reply_address_missing' };
      await conversationContextStore.markOrderException?.(record.envelope.tenantId, contextPayload, failure.code, orderId);
      await eventStore.complete(record.key, record.leaseId, {
        mode: 'quote_preview_only',
        order_price_change: {
          status: failure.kind === 'authorization_failed' ? 'blocked' : 'rejected',
          code: failure.code,
          diagnostics: failure.diagnostics,
        },
        actions: [
          { action_id: action.action_id, status: failure.kind === 'authorization_failed' ? 'blocked' : 'rejected', code: failure.code },
          ...(failureAction ? [{ action_id: failureAction.action_id, ...failureResult }] : []),
        ],
      });
      return { status: 'completed', mode: 'quote_preview_only' };
    }
  }

  async function processOrderPaid(record) {
    const orderContext = await loadOrderContext(record.envelope);
    if (!orderContext) return completeIgnored(record, 'order_session_or_quote_missing');
    const { session, facts, orderId, core } = orderContext;
    const expected = positiveCents(facts.quote_total_cents);
    const recordedQuantity = positiveCents(facts.quote_ticket_count);
    const hasPluginQuoteEvidence = Boolean(
      positiveCents(facts.quote_unit_cents)
      || expected
      || recordedQuantity
      || Number(facts.quote_expires_at) > 0
      || String(facts.quote_record_id ?? '').trim()
    );
    if (!hasPluginQuoteEvidence) return completeIgnored(record, 'plugin_quote_missing');

    const settings = await loadRuntimeSettingsForAccount(record.envelope.tenantId, session.accountUnb);
    if (!settings.automation_enabled) return completeIgnored(record, 'shop_automation_disabled');
    const contextPayload = sessionPayload(session);
    let order;
    try {
      order = await core.orders.get(orderId);
    } catch {
      await conversationContextStore.markOrderException?.(record.envelope.tenantId, contextPayload, 'paid_order_read_failed', orderId);
      return completeIgnored(record, 'paid_order_read_failed');
    }

    const actual = orderTotalCents(order);
    const confirmedActiveQuote = facts.quote_confirmed === true && hasActiveQuote(facts);
    const actions = [];
    const followUpEnvelope = withSessionPayload(record.envelope, contextPayload);
    if (!confirmedActiveQuote || !expected || !actual || actual !== expected) {
      const reason = !confirmedActiveQuote
        ? 'paid_quote_unconfirmed_or_expired'
        : (expected && actual ? 'paid_amount_mismatch' : 'paid_amount_unverifiable');
      await conversationContextStore.markOrderException?.(record.envelope.tenantId, contextPayload, reason, orderId);
      const templateKey = reason === 'paid_quote_unconfirmed_or_expired' ? 'paid_quote_unconfirmed' : 'paid_amount_mismatch';
      const action = withConfiguredReplyImage(createQuoteFollowUpAction(
        followUpEnvelope,
        reason === 'paid_quote_unconfirmed_or_expired'
          ? configuredReply(settings, templateKey, '订单已付款，但未找到有效确认报价；请勿重复下单，联系人工处理。')
          : configuredReply(settings, templateKey, '订单金额与本次核验报价不一致；如已支付，请勿重复下单，联系人工处理。'),
      ), settings, templateKey);
      if (action) actions.push({ action_id: action.action_id, ...(await executeAction(action)) });
    } else {
      // A current confirmed quote equal to the authoritative paid amount is the
      // only path into manual delivery.
      await conversationContextStore.setOrderStage?.(record.envelope.tenantId, contextPayload, 'paid_manual_delivery', orderId);
      const action = withConfiguredReplyImage(createQuoteFollowUpAction(
        followUpEnvelope,
        configuredReply(settings, 'paid_manual_delivery', '已收到付款，请稍等人工出票。订单已付款，不会重新核价。'),
      ), settings, 'paid_manual_delivery');
      if (action) actions.push({ action_id: action.action_id, ...(await executeAction(action)) });
    }
    await eventStore.complete(record.key, record.leaseId, { mode: 'quote_preview_only', paid_amount_checked: true, actions });
    return { status: 'completed', mode: 'quote_preview_only', actions };
  }

  async function processOrderPriceChanged(record) {
    const orderContext = await loadOrderContext(record.envelope);
    if (!orderContext) return completeIgnored(record, 'order_session_or_quote_missing');
    const { session, facts, orderId, core } = orderContext;
    const settings = await loadRuntimeSettingsForAccount(record.envelope.tenantId, session.accountUnb);
    if (!settings.automation_enabled) return completeIgnored(record, 'shop_automation_disabled');
    if (!facts.quote_confirmed || !hasActiveQuote(facts)) return completeIgnored(record, 'buyer_quote_not_confirmed_or_expired');
    const total = positiveCents(facts.quote_total_cents);
    if (!total) return completeIgnored(record, 'quote_terms_missing');

    const contextPayload = sessionPayload(session);
    let order;
    try {
      order = await core.orders.get(orderId);
    } catch {
      await conversationContextStore.markOrderException?.(record.envelope.tenantId, contextPayload, 'price_changed_order_read_failed', orderId);
      return completeIgnored(record, 'price_changed_order_read_failed');
    }
    const actual = orderTotalCents(order);
    if (!actual || actual !== total) {
      await conversationContextStore.markOrderException?.(record.envelope.tenantId, contextPayload, 'price_changed_amount_mismatch', orderId);
      return completeIgnored(record, 'price_changed_amount_mismatch');
    }

    await conversationContextStore.setOrderStage?.(record.envelope.tenantId, contextPayload, 'waiting_payment', orderId);
    const action = createQuoteFollowUpAction(
      withSessionPayload(record.envelope, contextPayload),
      `价格已修改为${(total / 100).toFixed(2)}元，请核对后付款。订单付款后不支持退改签。`,
    );
    if (action && String(record.envelope.payload?.operatorId ?? record.envelope.payload?.operator_id ?? '') === 'plugin:wanda-seat-autoquote') {
      action.ignore_platform_price_change_notice = true;
    }
    const result = action ? await executeAction(action) : { status: 'skipped', reason: 'reply_address_missing' };
    const actions = action ? [{ action_id: action.action_id, ...result }] : [];
    await eventStore.complete(record.key, record.leaseId, {
      mode: 'quote_preview_only',
      price_changed_notified: result.status === 'succeeded',
      verified_amount_cents: actual,
      actions,
    });
    return { status: 'completed', mode: 'quote_preview_only', actions };
  }

  async function loadOrderContext(envelope) {
    if (!conversationContextStore) return null;
    const orderId = orderIdFrom(envelope);
    if (!orderId) return null;
    const core = coreFor(envelope.tenantId);
    const session = await core.im?.getSessionByOrder?.(orderId);
    if (!session?.accountUnb || !session?.chatId || !session?.peerUnb) return null;
    const context = await conversationContextStore.get(envelope.tenantId, sessionPayload(session));
    return {
      core,
      session,
      facts: context?.facts ?? {},
      messages: Array.isArray(context?.messages) ? context.messages : [],
      orderId,
    };
  }

  async function completeIgnored(record, reason) {
    await eventStore.complete(record.key, record.leaseId, {
      mode: 'quote_preview_only',
      ignored_event: record.envelope.event,
      reason,
      actions: [],
    });
    return { status: 'completed', mode: 'quote_preview_only', actions: [] };
  }

  async function loadRuntimeSettingsForAccount(tenantId, accountUnb) {
    const response = await backend.getRuntimeSettings(accountUnb);
    const settings = response?.settings ?? {};
    return Object.freeze({
      automation_enabled: settings.automation_enabled === true && settings.shop_enabled !== false && settings.execution_mode !== 'off',
      price_change_enabled: settings.shop_features?.price_change_enabled ?? settings.auto_price_change === true,
    });
  }

  return Object.freeze({ process, updateConversationStage });
}

function orderIdFrom(envelope) {
  return String(envelope?.payload?.orderId ?? envelope?.payload?.order_id ?? '').trim();
}

function sessionPayload(session = {}) {
  return {
    accountUnb: session.accountUnb ?? session.account_unb,
    chatId: session.chatId ?? session.chat_id,
    peerUnb: session.peerUnb ?? session.peer_unb,
  };
}

function hasReplyAddress(value = {}) {
  const payload = sessionPayload(value);
  return Boolean(payload.accountUnb && payload.chatId && payload.peerUnb);
}

function withSessionPayload(envelope, payload) {
  return { ...envelope, payload: { ...(envelope.payload ?? {}), ...payload } };
}

function positiveCents(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

function orderTotalCents(order) {
  for (const value of [order?.paidAmount, order?.paid_amount_cents, order?.payment, order?.priceFee, order?.price_fee, order?.totalFee, order?.total_fee]) {
    const cents = positiveCents(value);
    if (cents) return cents;
  }
  return null;
}

function hasRecentQuotePurchaseIntent(messages) {
  return Array.isArray(messages)
    && messages.filter((message) => message?.role === 'buyer').slice(-6)
      .some((message) => isQuoteConfirmation(message?.text));
}

function conflictingRecentTicketCount(messages, quotedCount) {
  const quoteCount = Number(quotedCount);
  if (!Number.isInteger(quoteCount) || quoteCount < 1 || !Array.isArray(messages)) return null;
  const recentBuyerMessages = messages.filter((message) => message?.role === 'buyer').slice(-6).reverse();
  for (const message of recentBuyerMessages) {
    const requested = requestedTicketCount(message?.text);
    if (requested && requested !== quoteCount) return requested;
  }
  return null;
}
