import { readFileSync } from 'node:fs';
import { requestedTicketCount } from './agent/ticket-request-inspector.mjs';
import { hasActiveQuote } from './quote/quote-followup-policy.mjs';
import { createBuyerReplyAction as autoReplyAction } from './reply/reply-policy.mjs';

const ORDER_SUBMIT_GUIDE_BASE64 = readFileSync(new URL('../assets/order-submit-guide.jpg', import.meta.url)).toString('base64');
const IMAGE_SUPPLEMENT_WINDOW_MS = 10 * 60 * 1_000;
const MULTI_IMAGE_PAIR_WINDOW_MS = 30 * 1_000;

const SAFE_AUTO_REPLY_MIN_CONFIDENCE = new Map([
  ['票价咨询', 0.75],
  ['选座核价', 0.75],
  ['补充信息', 0.60],
  ['其他', 0.60],
]);
// A seat-selection draft may ask for quantity or a clearer screenshot, but it
// must never assert availability before Wanda realtime verification.
const UNSAFE_MODEL_REPLY_CLAIMS = /(?:已(?:锁座|改价|出票|发货|退款)|(?:正在|马上|立即|为您).{0,16}(?:锁定座位|锁座|核对|核验|查询|查).{0,12}(?:票价|价格|库存|余票)|稍后.{0,12}(?:报价|报给您|告诉您价格)|生成.{0,8}(?:订单)?价格|马上(?:出票|发货|退款)|保证(?:有票|出票)|(?:有票|能买|可以买|可购买|可以买到))/u;
// A generic reply has no verified vision facts. It must never invent a circle,
// mark, or buyer-selected position; only the realtime quote path may describe
// those facts after it has received structured recognition.
const UNVERIFIED_SELECTION_CLAIMS = /(?:看到|按|您).{0,12}(?:圈选|圈的|圈出|标记).{0,24}(?:座位|位置)?|(?:圈选|圈的位置|标记的位置)/u;
// Only a delivered, unexpired quote with a known total and quantity may lead
// to ordering,待付款 or price-change instructions. Free-form AI text cannot
// create that authorization by itself.
const UNVERIFIED_ORDER_FLOW_INSTRUCTIONS = /(?:拍下|下单|提交订单|待付款|改价|修改价格|锁定座位|锁座|订单价格)/u;

async function deferUntilReplyDelay(record, settings, eventStore, metadata = null) {
  const delayMs = boundedInteger(settings.ai_reply_delay_seconds, 2, 60, 3) * 1_000;
  const receivedAt = Number(record.envelope?.ts) || Date.parse(String(record.envelope?.ts ?? ''));
  if (!Number.isFinite(receivedAt) || typeof eventStore?.defer !== 'function') return null;
  const remainingMs = delayMs - (Date.now() - receivedAt);
  if (remainingMs <= 0) return null;
  return eventStore.defer(record.key, record.leaseId, { delayMs: remainingMs, reason: 'buyer_message_merge_window', ...(metadata ? { metadata } : {}) });
}

function settle(promise) {
  return Promise.resolve(promise)
    .then((value) => ({ status: 'fulfilled', value }))
    .catch((reason) => ({ status: 'rejected', reason }));
}

async function awaitWithDelayedNotice(promise, delayMs, onDelay) {
  const operation = Promise.resolve(promise);
  const remaining = Number(delayMs);
  if (!Number.isFinite(remaining) || remaining <= 0) {
    await onDelay();
    return operation;
  }
  let timer = null;
  const completed = operation.then(
    (value) => ({ status: 'completed', value }),
    (error) => ({ status: 'failed', error }),
  );
  const delayed = new Promise((resolve) => {
    timer = setTimeout(() => resolve({ status: 'delayed' }), remaining);
  });
  const first = await Promise.race([completed, delayed]);
  if (timer) clearTimeout(timer);
  if (first.status === 'completed') return first.value;
  if (first.status === 'failed') throw first.error;
  await onDelay();
  return operation;
}

function autoModelReplyAction(envelope, preview, enabled, hasVerifiedQuote = false) {
  const draft = preview?.draft;
  if (!enabled || preview?.autoSend !== true || firstImageUrl(envelope.payload)) return null;
  if (!draft || draft.needs_human === true) return null;
  const intent = String(draft.intent ?? '');
  const minConfidence = SAFE_AUTO_REPLY_MIN_CONFIDENCE.get(intent);
  const reply = String(draft.reply ?? '').trim();
  if (
    minConfidence === undefined
    || Number(draft.confidence) < minConfidence
    || !reply
    || UNSAFE_MODEL_REPLY_CLAIMS.test(reply)
    || UNVERIFIED_SELECTION_CLAIMS.test(reply)
    || (!hasVerifiedQuote && UNVERIFIED_ORDER_FLOW_INSTRUCTIONS.test(reply))
  ) return null;
  return autoReplyAction(envelope, reply, 'ai_customer_service');
}

function agentStateSnapshot(value, observedAt = Date.now()) {
  const facts = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const snapshot = { observed_at: Number.isSafeInteger(Number(observedAt)) ? Number(observedAt) : Date.now() };
  for (const key of ['quote_total_cents', 'quote_ticket_count', 'quote_expires_at']) {
    const number = Number(facts[key]);
    if (Number.isSafeInteger(number) && number > 0) snapshot[key] = number;
  }
  for (const key of ['stage', 'pricing_rule_version']) {
    const item = String(facts[key] ?? '').trim().slice(0, 80);
    if (item) snapshot[key] = item;
  }
  if (facts.quote_reply_delivered === true) snapshot.quote_reply_delivered = true;
  return Object.freeze(snapshot);
}

function recentExplicitTicketCount(messages) {
  if (!Array.isArray(messages)) return null;
  for (const message of messages.filter((item) => item?.role === 'buyer').slice(-8).reverse()) {
    const count = requestedTicketCount(message?.text);
    if (count) return count;
  }
  return null;
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

function orderSubmitGuideAction(envelope) {
  const payload = envelope.payload ?? {};
  const accountUnb = String(payload.accountUnb ?? payload.account_unb ?? '').trim();
  const chatId = String(payload.chatId ?? payload.chat_id ?? '').trim();
  const peerUnb = String(payload.peerUnb ?? payload.peer_unb ?? '').trim();
  if (!accountUnb || !chatId || !peerUnb) return null;
  return {
    action_id: `${envelope.id}:order-submit-guide`,
    kind: 'send_image',
    tenant_id: String(envelope.tenantId),
    account_unb: accountUnb,
    chat_id: chatId,
    peer_unb: peerUnb,
    image_base64: ORDER_SUBMIT_GUIDE_BASE64,
    filename: 'order-submit-guide.jpg',
    content_type: 'image/jpeg',
    human_takeover_window_ms: 20_000,
  };
}

function boundedInteger(value, minimum, maximum, fallback) {
  const number = Number(value);
  return Number.isInteger(number) && number >= minimum && number <= maximum ? number : fallback;
}

function cinemaMatchEvaluationSnapshot(preview) {
  const recognition = preview?.recognition;
  if (!recognition || typeof recognition !== 'object' || Array.isArray(recognition)) return null;
  const field = (name, maximum = 160) => {
    const value = String(recognition[name] ?? '').replace(/\s+/gu, ' ').trim();
    return value ? value.slice(0, maximum) : null;
  };
  const cinema = field('cinema');
  const movie = field('movie');
  if (!cinema && !movie) return null;
  return Object.freeze({
    status: String(preview?.status ?? '').slice(0, 40),
    failure_code: preview?.failure_code ? String(preview.failure_code).slice(0, 100) : null,
    city: field('city', 80), cinema, movie, date: field('date', 32), showtime: field('showtime', 32), hall: field('hall', 80),
    matched_cinema_name: preview?.matched_cinema_name ? String(preview.matched_cinema_name).replace(/\s+/gu, ' ').trim().slice(0, 160) : null,
  });
}

function quoteStageTimings(preview) {
  const source = preview?.timings_ms;
  if (!source || typeof source !== 'object' || Array.isArray(source)) return null;
  const result = {};
  for (const key of ['account', 'match', 'realtime_seats', 'temporary_lock', 'available_offers', 'cancel', 'release_recheck', 'locked_offer', 'calculate_quote', 'total']) {
    const value = Number(source[key]);
    if (Number.isSafeInteger(value) && value >= 0 && value <= 120_000) result[key] = value;
  }
  return Object.keys(result).length ? Object.freeze(result) : null;
}

function quoteCostEvidence(preview) {
  const count = positiveCents(preview?.ticket_count ?? preview?.quote_ticket_count);
  const seatQuotes = Array.isArray(preview?.seat_quotes) ? preview.seat_quotes : [];
  const originalValues = seatQuotes.map((item) => positiveCents(item?.original_price_cents));
  const memberValues = seatQuotes.map((item) => positiveCents(item?.member_price_cents));
  const channelFeeValues = seatQuotes.map((item) => nonnegativeCents(item?.channel_fee_cents));
  const originalPriceTotalCents = seatQuotes.length > 0 && originalValues.every(Boolean)
    ? originalValues.reduce((sum, value) => sum + value, 0)
    : null;
  const memberCostTotalCents = seatQuotes.length > 0 && memberValues.every(Boolean)
    ? memberValues.reduce((sum, value) => sum + value, 0)
    : (count && positiveCents(preview?.member_unit_price_cents) ? count * positiveCents(preview.member_unit_price_cents) : null);
  const channelFeeTotalCents = nonnegativeCents(preview?.channel_fee_total_cents)
    ?? (seatQuotes.length > 0 && channelFeeValues.every((value) => value != null) ? channelFeeValues.reduce((sum, value) => sum + value, 0) : null);
  return {
    ...(memberCostTotalCents ? { memberCostTotalCents } : {}),
    ...(channelFeeTotalCents != null ? { channelFeeTotalCents } : {}),
    ...(originalPriceTotalCents ? { originalPriceTotalCents } : {}),
    ...(preview?.pricing_source ? { pricingSource: String(preview.pricing_source).slice(0, 80) } : {}),
  };
}

function positiveCents(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

function nonnegativeCents(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

function bridgeActions(envelope, result, upsert, settings) {
  const payload = envelope.payload ?? {};
  if (Array.isArray(result?.actions)) {
    return result.actions
      .map((action, index) => normalizeBackendAction(envelope, result, action, index))
      .filter(Boolean)
      .map((action) => withManualTakeoverWindow(action, settings));
  }
  const action = result?.action ?? result?.linked_action ?? upsert?.linked_action ?? null;
  const replyMessage = action?.reply_message ?? result?.reply_message ?? null;
  if (action?.modify_order_amount === true) {
    const policy = quotePolicySnapshot(result, upsert);
    const maxAutoOrderAmountCents = policy?.max_auto_order_amount_cents ?? 1_000;
    const amountCents = action.amount_cents;
    const orderId = String(result?.linked_order?.order_id ?? payload.orderId ?? payload.order_id ?? '').trim();
    if (!orderId) return [];
    return [withManualTakeoverWindow({
      action_id: `${envelope.id}:change-price`,
      kind: 'change_price',
      tenant_id: String(envelope.tenantId),
      account_unb: String(payload.accountUnb ?? payload.account_unb ?? '').trim(),
      order_id: orderId,
      price_fee: amountCents,
      transport_fee: 0,
      expected_total_cents: amountCents,
      expected_quantity: result?.task?.quantity,
      gates: {
        feature_enabled: settings.automation_enabled && settings.price_change_enabled && settings.quote_enabled,
        unique_showtime: result?.task?.status === 'price_update_pending',
        quantity_confirmed: Number(result?.task?.quantity ?? 0) > 0,
        selection_confirmed: action.requires_manual_review !== true,
        quote_valid: Number.isSafeInteger(amountCents)
          && amountCents > 0
          && policy !== null
          && amountCents <= maxAutoOrderAmountCents,
        order_linked: Boolean(orderId),
        human_takeover: false,
        max_amount_cents: maxAutoOrderAmountCents,
      },
    }, settings)];
  }
  const ocrReply = result?.processing_mode === 'ocr' || result?.reply_origin === 'ocr';
  if (!replyMessage || !settings.automation_enabled || (!ocrReply && !settings.ai_reply_enabled)) return [];
  const accountUnb = String(payload.accountUnb ?? payload.account_unb ?? '').trim();
  const chatId = String(payload.chatId ?? payload.chat_id ?? '').trim();
  const peerUnb = String(payload.peerUnb ?? payload.peer_unb ?? '').trim();
  const orderId = String(result?.linked_order?.order_id ?? payload.orderId ?? payload.order_id ?? '').trim();
  if ((!accountUnb || !chatId || !peerUnb) && !orderId) return [];
  return [withManualTakeoverWindow({
    action_id: `${envelope.id}:reply`,
    kind: 'reply',
    tenant_id: String(envelope.tenantId),
    account_unb: accountUnb,
    chat_id: chatId,
    peer_unb: peerUnb,
    order_id: orderId,
    text: String(replyMessage),
    reply_origin: ocrReply ? 'ocr' : 'agent',
  }, settings)];
}

function withManualTakeoverWindow(action, settings) {
  return {
    ...action,
    human_takeover_window_ms: boundedInteger(settings?.ai_reply_manual_takeover_seconds, 5, 60, 20) * 1_000,
  };
}

function normalizeBackendAction(envelope, result, action, index) {
  if (!action || typeof action !== 'object' || Array.isArray(action)) return null;
  const payload = envelope.payload ?? {};
  const base = {
    action_id: String(action.action_id ?? `${envelope.id}:agent-action:${index}`),
    tenant_id: String(action.tenant_id ?? envelope.tenantId),
    account_unb: String(action.account_unb ?? payload.accountUnb ?? payload.account_unb ?? ''),
    chat_id: String(action.chat_id ?? payload.chatId ?? payload.chat_id ?? ''),
    peer_unb: String(action.peer_unb ?? payload.peerUnb ?? payload.peer_unb ?? ''),
    order_id: String(action.order_id ?? result?.linked_order?.order_id ?? payload.orderId ?? payload.order_id ?? ''),
  };
  return { ...action, ...base, kind: String(action.kind ?? action.action ?? 'noop') };
}

function quotePolicySnapshot(result, upsert) {
  for (const candidate of [
    result?.task?.quote_policy_snapshot,
    result?.task?.policy_snapshot,
    upsert?.task?.quote_policy_snapshot,
    upsert?.task?.policy_snapshot,
  ]) {
    if (isValidQuotePolicy(candidate)) return candidate;
  }
  return null;
}

function isValidQuotePolicy(value) {
  return value
    && typeof value === 'object'
    && !Array.isArray(value)
    && Number.isSafeInteger(value.wplus_adjustment_cents)
    && value.wplus_adjustment_cents >= -10_000
    && value.wplus_adjustment_cents <= 10_000
    && Number.isSafeInteger(value.regular_adjustment_cents)
    && value.regular_adjustment_cents >= -10_000
    && value.regular_adjustment_cents <= 10_000
    && Number.isSafeInteger(value.max_auto_order_amount_cents)
    && value.max_auto_order_amount_cents >= 1_000
    && value.max_auto_order_amount_cents <= 1_000_000;
}

async function statusPayload(envelope, status, coreFor) {
  const payload = envelope.payload ?? {};
  const result = {
    tenant_id: String(envelope.tenantId),
    event_id: `${envelope.id}:status:${status}`,
    status,
    source_event: envelope.event,
  };
  if (status === 'awaiting_payment') {
    const amount = centsFromPayload(payload);
    if (amount != null) result.price_changed_amount_cents = amount;
  }
  if (status === 'paid') {
    let amount = centsFromPayload(payload);
    if (amount == null) {
      const orderId = String(payload.orderId ?? payload.order_id ?? '').trim();
      const order = orderId ? await coreFor(String(envelope.tenantId)).orders.get(orderId) : null;
      amount = centsFromOrder(order);
    }
    if (amount != null) result.paid_amount_cents = amount;
  }
  return result;
}

function centsFromOrder(order) {
  for (const value of [order?.paidAmount, order?.paid_amount_cents, order?.priceFee, order?.price_fee, order?.totalFee, order?.total_fee]) {
    const number = Number(value);
    if (Number.isSafeInteger(number) && number >= 0) return number;
  }
  return null;
}

function centsFromPayload(payload) {
  for (const value of [payload.priceFee, payload.price_fee, payload.paidAmount, payload.paid_amount_cents, payload.amount]) {
    const number = Number(value);
    if (Number.isSafeInteger(number) && number >= 0) return number;
  }
  return null;
}

function isImageMessageText(value) {
  return /^https:\/\/[^\s]+\.(?:png|jpe?g|webp)(?:\?[^\s]*)?$/iu.test(String(value).trim())
    || /^https:\/\/img\.alicdn\.com\//iu.test(String(value).trim());
}

function firstImageUrl(payload = {}) {
  const urls = Array.isArray(payload.imageUrls) ? payload.imageUrls : [];
  return urls.find((value) => typeof value === 'string' && value.trim()) ?? null;
}

function activeQuoteDraftText(value, now = Date.now()) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Number(value.expires_at ?? 0) <= now) return '';
  const fieldsByName = value.fields && typeof value.fields === 'object' && !Array.isArray(value.fields) ? value.fields : null;
  const fieldValue = (name) => fieldsByName ? fieldsByName[name]?.value : value[name];
  const fields = [
    ['城市', fieldValue('city')], ['影院', fieldValue('cinema')], ['影片', fieldValue('movie')], ['日期', fieldValue('date')], ['开场', fieldValue('showtime')], ['影厅', fieldValue('hall')],
  ].map(([label, field]) => {
    const text = String(field ?? '').replace(/\s+/gu, ' ').trim();
    return text ? `${label}：${text}` : '';
  }).filter(Boolean);
  const count = Number(fieldValue('ticket_count'));
  if (Number.isInteger(count) && count >= 1 && count <= 20) fields.push(`票数：${count}张`);
  return fields.join('\n');
}

function isQuoteDetailSupplement(payload) {
  const content = String(payload?.content ?? payload?.text ?? '').replace(/\s+/gu, ' ').trim();
  if (!content) return false;
  return /(?:信息(?:无误|正确)|核对无误|^确认$|[一二两三四五六七八九十\d]+\s*(?:张|个(?:座位|位置)?)|影院|影城|万达|影片|电影|场次|开场|多少(?:呢|啊|呀)?|好多钱|价格|报价|核价|[WwＷｗ]\s*[+＋]|会员|(?:广场|天地|中心|店)$|\d{1,2}:\d{2}|第?\s*(?:[一二三四五六七八九十]+|\d{1,2})\s*(?:排|行)\s*\d{1,2})/u.test(content);
}

function recentContextImages(context, now = Date.now(), windowMs = MULTI_IMAGE_PAIR_WINDOW_MS) {
  const messages = Array.isArray(context?.messages) ? context.messages : [];
  return [...new Set(messages
    .filter((message) => now - Number(message?.at ?? 0) <= windowMs)
    .flatMap((message) => Array.isArray(message?.image_urls) ? message.image_urls : [])
    .filter((value) => typeof value === 'string' && value.trim())
    .map((value) => value.trim()))];
}

function recentContextImage(context, now = Date.now()) {
  const messages = Array.isArray(context?.messages) ? context.messages : [];
  const imageMessage = messages.slice().reverse().find((message) => (
    Array.isArray(message?.image_urls) && message.image_urls.some((value) => typeof value === 'string' && value.trim())
  ));
  if (!imageMessage) return null;
  const imageUrl = imageMessage.image_urls.find((value) => typeof value === 'string' && value.trim()) ?? null;
  if (!imageUrl) return null;
  // Buyers often add city, session, quantity, or seat-color details after vision
  // finishes. Keep the image available for that bounded retry round.
  const isRecentImage = now - Number(imageMessage.at ?? 0) <= IMAGE_SUPPLEMENT_WINDOW_MS;
  const isConfirmedRound = context?.facts?.confirmation_image_url === imageUrl;
  if (!isRecentImage && !isConfirmedRound) return null;
  return imageUrl;
}

function isTerminalHistoricalOrder(order) {
  const status = order?.orderStatus ?? order?.order_status ?? order?.status;
  if (Number(status) === 4) return true;
  const label = String(order?.orderStatusText ?? order?.order_status_text ?? order?.statusText ?? '').trim();
  return /(?:交易成功|交易关闭|已关闭|已取消|退款成功|已退款)/u.test(label);
}

function messageContext(envelope) {
  const payload = envelope.payload ?? {};
  return {
    buyer_nick: String(payload.buyerNick ?? payload.buyer_nick ?? payload.buyerName ?? ''),
    order_id: String(payload.orderId ?? payload.order_id ?? ''),
    account_unb: String(payload.accountUnb ?? payload.account_unb ?? ''),
    chat_id: String(payload.chatId ?? payload.chat_id ?? ''),
    peer_unb: String(payload.peerUnb ?? payload.peer_unb ?? ''),
    platform_message_id: String(payload.messageId ?? payload.message_id ?? envelope.id),
    marketplace_conversation: String(payload.marketplaceConversation ?? payload.marketplace_conversation ?? ''),
  };
}

function toReplyHistoryMessage(message, source = 'unknown') {
  const direction = String(message?.direction ?? '').toLowerCase();
  const role = direction === 'outbound' ? 'seller' : 'buyer';
  const content = String(message?.content ?? message?.text ?? message?.body?.text ?? '').replace(/\s+/gu, ' ').trim();
  const sentAt = String(message?.sentAt ?? '').trim();
  return {
    role,
    content: content || '[图片或非文本消息]',
    source: role === 'buyer' ? 'buyer' : ['plugin', 'external_seller'].includes(source) ? source : 'unknown',
    sent_at: /^\d{4}-\d{2}-\d{2}T/u.test(sentAt) ? sentAt : undefined,
  };
}

function isDurableActiveLowRiskTurn(envelope) {
  // Full Agent owns every real buyer IM turn. Platform lifecycle events remain
  // on the deterministic transaction worker, and known system notices are
  // discarded before they can become an Agent prompt.
  return envelope?.event === 'im.message.received' && !isPlatformSystemMessage(envelope?.payload ?? {});
}

function isPlatformSystemMessage(payload = {}) {
  const content = String(payload.content ?? payload.text ?? '').replace(/\s+/gu, '').trim();
  return /^(?:买家已确认收货[，,]?交易成功|交易已关闭|订单已关闭|你关闭了订单[，,]?钱款已原路退返|快给ta一个评价吧[～~]?|我完成了评价|你已发货|你人真不错[，,]?送你闲鱼小红花)$/u.test(content)
    || /^不想宝贝被砍价\?.*message_no_bargain/iu.test(content);
}

function messageChatKey(payload = {}) {
  return [
    payload.accountUnb ?? payload.account_unb ?? '',
    payload.chatId ?? payload.chat_id ?? '',
    payload.peerUnb ?? payload.peer_unb ?? '',
  ].map((value) => String(value).trim()).join(':');
}

function dataUrl(image) {
  const bytes = image?.bytes;
  if (!bytes) throw new TypeError('image bytes are required');
  const buffer = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes);
  const contentType = String(image.contentType || 'image/png');
  return `data:${contentType};base64,${buffer.toString('base64')}`;
}

function context(envelope, suffix) {
  return { tenantId: envelope.tenantId, eventId: `${envelope.id}:${suffix}` };
}

function nonRetryable(message) {
  const error = new Error(message);
  error.retryable = false;
  return error;
}

function isImWsUnavailable(error) {
  const code = String(error?.code ?? error?.errorCode ?? '').trim();
  const message = String(error?.message ?? error ?? '');
  return code === 'E_IM_WS_UNAVAILABLE' || /账号\s*WS\s*当前不可用/u.test(message);
}

function retryDelay(attempts, error = null) {
  if (isImWsUnavailable(error)) return 10_000;
  return Math.min(300_000, 5_000 * (2 ** Math.max(0, Number(attempts) - 1)));
}

function safeLog(record, error) {
  return {
    event_id: record.envelope?.id,
    tenant_id: record.envelope?.tenantId,
    event: record.envelope?.event,
    attempts: record.attempts,
    error: String(error?.message ?? error).slice(0, 500),
  };
}

export {
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
};
