import { validatePriceChangeGate } from './domain.mjs';
import { classifyPriceChangeError } from './price-change-error.mjs';

const UNPAID_ORDER_STATUSES = new Set([1, '1', 'UNPAID', 'WAIT_BUYER_PAY', 'WAITING_PAYMENT']);
const MAX_PRICE_CHANGE_CENTS = 10_000_000_000;
const PRICE_CHANGE_READINESS_RETRY_DELAYS_MS = Object.freeze([2_000, 5_000]);
const REPLY_IMAGE_UPLOAD_CACHE_TTL_MS = 30 * 60 * 1_000;
const MAX_REPLY_IMAGE_UPLOAD_CACHE_ENTRIES = 100;

export class UnknownActionResultError extends Error {
  constructor(message, cause) {
    super(message, { cause });
    this.name = 'UnknownActionResultError';
  }
}

export function validateBackendAction(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw new TypeError('backend action must be an object');
  }
  const rawKind = String(input.kind ?? '').trim();
  const kind = rawKind === 'send_message' ? 'reply' : rawKind;
  // This plugin may recognize, reply and change an unpaid order amount only.
  // Shipping, fake shipping, receipt reminders and ratings are intentionally
  // excluded even if a future manifest accidentally gains broader scopes.
  if (!['reply', 'reply_with_image', 'send_image', 'change_price', 'noop'].includes(kind)) {
    throw new TypeError(`unsupported backend action: ${kind || '(empty)'}`);
  }
  const actionId = String(input.action_id ?? '').trim();
  if (!actionId) throw new TypeError('action_id is required');
  const expiresAt = input.expires_at ? Date.parse(input.expires_at) : null;
  if (input.expires_at && !Number.isFinite(expiresAt)) {
    throw new TypeError('expires_at must be RFC3339');
  }
  return Object.freeze({ ...input, action_id: actionId, kind, expiresAt });
}

export function createActionExecutor({ coreFor, messageRegistry, imageLoader = null, now = Date.now, sleep = defaultSleep }) {
  if (typeof coreFor !== 'function') throw new TypeError('coreFor is required');
  if (imageLoader != null && typeof imageLoader?.load !== 'function') throw new TypeError('imageLoader.load is required');
  if (typeof sleep !== 'function') throw new TypeError('sleep must be a function');
  const replyImageUploadCache = new Map();

  async function execute(input) {
    const action = validateBackendAction(input);
    if (action.expiresAt != null && action.expiresAt <= now()) {
      return { status: 'skipped', reason: 'action_expired' };
    }
    if (action.kind === 'noop') return { status: 'skipped', reason: action.reason ?? 'noop' };
    const core = coreFor(String(action.tenant_id));
    if (action.kind === 'reply') return executeReply(core, action);
    if (action.kind === 'reply_with_image') return executeReplyWithImage(core, action);
    if (action.kind === 'send_image') return executeSendImage(core, action);
    if (action.kind === 'change_price') return executePriceChange(core, action);
    throw new TypeError(`unsupported backend action: ${action.kind}`);
  }

  async function executeReplyWithImage(core, action) {
    const replyResult = await executeReply(core, { ...action, kind: 'reply' });
    if (replyResult.status !== 'succeeded') return replyResult;
    try {
      const imageResult = await executeSendImage(core, {
        ...action,
        action_id: `${action.action_id}:image`,
        kind: 'send_image',
      });
      return {
        ...replyResult,
        image_status: imageResult.status,
        ...(imageResult.message_id ? { image_message_id: imageResult.message_id } : {}),
        ...(imageResult.reason ? { image_reason: imageResult.reason } : {}),
      };
    } catch {
      // Text has already been sent. Do not retry the combined action and risk
      // duplicating a buyer-facing reply; expose the attachment failure for audit.
      return { ...replyResult, image_status: 'failed' };
    }
  }

  async function executeReply(core, action) {
    const address = await resolveReplyAddress(core, action);
    if (!address) throw new TypeError('reply action requires account_unb, chat_id and peer_unb');
    const text = String(action.text ?? '').trim();
    if (!text || text.length > 1_000) throw new TypeError('reply text must contain 1 to 1000 characters');

    const history = await core.im.listMessages({
      accountUnb: address.accountUnb,
      chatId: address.chatId,
      pageSize: 20,
    });
    const messages = Array.isArray(history?.items) ? history.items : [];
    const latest = messages[0] ?? null;
    if (latest?.direction === 'outbound') {
      const sentByThisPlugin = await messageRegistry.wasSentMessage(action.tenant_id, address.chatId, latest.messageId);
      const latestText = String(latest.content ?? latest.text ?? '').trim();
      if (sentByThisPlugin && latestText && latestText === text) return { status: 'skipped', reason: 'duplicate_reply' };
      const isVerifiedQuoteFollowup = action.allow_plugin_followup === true
        && ['verified_quote', 'quote_follow_up', 'quote_processing'].includes(String(action.reply_origin));
      // Recognition and its deterministic realtime result are a bounded pair
      // from one buyer event. Permit the second, price/follow-up message only
      // after our own recognition message; all ordinary replies stay deduped.
      if (sentByThisPlugin && !isVerifiedQuoteFollowup) return { status: 'skipped', reason: 'duplicate_reply' };
    }
    if (await hasRecentHumanTakeover(
      messages,
      action.tenant_id,
      address.chatId,
      messageRegistry,
      now(),
      action.human_takeover_window_ms,
      { ignorePlatformPriceChangeNotice: action.ignore_platform_price_change_notice === true },
    )) {
      return { status: 'skipped', reason: 'human_takeover' };
    }

    const result = await core.im.sendMessage({ ...address, text });
    if (result?.messageId) {
      await messageRegistry.recordSentMessage(action.tenant_id, address.chatId, result.messageId);
    }
    return { status: 'succeeded', message_id: result?.messageId ?? null };
  }

  async function executeSendImage(core, action) {
    const address = await resolveReplyAddress(core, action);
    if (!address) throw new TypeError('send_image action requires account_unb, chat_id and peer_unb');
    const history = await core.im.listMessages({
      accountUnb: address.accountUnb,
      chatId: address.chatId,
      pageSize: 20,
    });
    const messages = Array.isArray(history?.items) ? history.items : [];
    if (await hasRecentHumanTakeover(messages, action.tenant_id, address.chatId, messageRegistry, now(), action.human_takeover_window_ms)) {
      return { status: 'skipped', reason: 'human_takeover' };
    }
    let imageUrl = String(action.image_url ?? action.imageUrl ?? '').trim();
    let width = integerOrNull(action.width);
    let height = integerOrNull(action.height);
    let data = null;
    let contentType = String(action.content_type ?? action.contentType ?? 'image/jpeg');
    let filename = String(action.filename ?? 'wanda-ticket.jpg').slice(0, 180);
    const sourceImageUrl = imageUrl;
    const cacheKey = sourceImageUrl ? `${address.accountUnb}\n${sourceImageUrl}` : '';
    const cachedUpload = cacheKey ? replyImageUploadCache.get(cacheKey) : null;
    if (cachedUpload && cachedUpload.expires_at > now()) {
      imageUrl = cachedUpload.image_url;
      width = width ?? cachedUpload.width;
      height = height ?? cachedUpload.height;
    } else if (imageUrl && imageLoader) {
      if (cacheKey) replyImageUploadCache.delete(cacheKey);
      const downloaded = await imageLoader.load(imageUrl);
      data = downloaded.bytes;
      contentType = downloaded.contentType;
      filename = String(action.filename ?? replyImageFilename(contentType)).slice(0, 180);
      imageUrl = '';
    } else if (!imageUrl) {
      data = decodeBase64Image(action.image_base64 ?? action.imageDataBase64);
      if (!data) throw new TypeError('send_image action requires image_url or image_base64');
    }
    if (data) {
      const uploaded = await core.im.uploadImage({ accountUnb: address.accountUnb, filename, contentType, data });
      imageUrl = String(uploaded?.imageUrl ?? uploaded?.image_url ?? '').trim();
      width = width ?? integerOrNull(uploaded?.width);
      height = height ?? integerOrNull(uploaded?.height);
      if (cacheKey) {
        replyImageUploadCache.set(cacheKey, {
          image_url: imageUrl,
          width,
          height,
          expires_at: now() + REPLY_IMAGE_UPLOAD_CACHE_TTL_MS,
        });
        while (replyImageUploadCache.size > MAX_REPLY_IMAGE_UPLOAD_CACHE_ENTRIES) {
          replyImageUploadCache.delete(replyImageUploadCache.keys().next().value);
        }
      }
    }
    if (!imageUrl) throw new TypeError('uploaded image did not return imageUrl');
    const result = await core.im.sendImage({
      ...address,
      imageUrl,
      ...(width ? { width } : {}),
      ...(height ? { height } : {}),
    });
    if (result?.messageId) {
      await messageRegistry.recordSentMessage(action.tenant_id, address.chatId, result.messageId);
    }
    return { status: 'succeeded', message_id: result?.messageId ?? null, image_url: imageUrl };
  }

  async function executePriceChange(core, action) {
    const orderId = String(action.order_id ?? '').trim();
    if (!orderId) throw new TypeError('change_price action requires order_id');
    const priceFee = requireCents(action.price_fee, 'price_fee');
    const transportFee = requireCents(action.transport_fee ?? 0, 'transport_fee');
    let humanTakeover = await detectHumanTakeover(core, action, messageRegistry);
    let order = await core.orders.get(orderId);
    let review = preflightPriceChange(action, order, { humanTakeover });

    for (let attempt = 0; ; attempt += 1) {
      const { gate, targetTotal, currentTotal } = review;
      if (!gate.allowed) {
        return { status: 'skipped', reason: 'price_change_gate_failed', failures: gate.failures, review };
      }
      if (currentTotal === targetTotal) {
        return { status: 'succeeded', reconciled: true, amount_cents: targetTotal };
      }

      try {
        await core.orders.changePrice(orderId, { priceFee, transportFee });
        return { status: 'submitted', amount_cents: targetTotal };
      } catch (error) {
        attachPriceChangeReview(error, review);
        const failure = classifyPriceChangeError(error);

        // order.created can arrive before the upstream order becomes writable.
        // Retry this one explicit no-write rejection only, with bounded waits
        // and a complete authoritative preflight before every attempt.
        if (
          failure.code === 'CANNOT_MODIFY_FEE'
          && attempt < PRICE_CHANGE_READINESS_RETRY_DELAYS_MS.length
        ) {
          await sleep(PRICE_CHANGE_READINESS_RETRY_DELAYS_MS[attempt]);
          humanTakeover = await detectHumanTakeover(core, action, messageRegistry);
          order = await core.orders.get(orderId);
          review = preflightPriceChange(action, order, { humanTakeover });
          continue;
        }

        if (failure.terminal) {
          error.retryable = false;
          throw error;
        }
        if (!isUnknownNetworkResult(error)) throw error;
        const reconciledOrder = await core.orders.get(orderId).catch(() => null);
        if (currentOrderTotal(reconciledOrder) === targetTotal) {
          return { status: 'succeeded', reconciled: true, amount_cents: targetTotal };
        }
        throw new UnknownActionResultError('price change result is unknown; reconciliation required', error);
      }
    }
  }

  return Object.freeze({ execute });
}

export function preflightPriceChange(action, order, { humanTakeover = false } = {}) {
  const priceFee = requireCents(action?.price_fee, 'price_fee');
  const transportFee = requireCents(action?.transport_fee ?? 0, 'transport_fee');
  const targetTotal = safeTotalCents(priceFee, transportFee);
  const expectedTotal = requireCents(action?.expected_total_cents, 'expected_total_cents');
  const expectedQuantity = requirePositiveInteger(action?.expected_quantity, 'expected_quantity');
  const expectedAccountUnb = String(action?.account_unb ?? '').trim();
  if (!expectedAccountUnb) throw new TypeError('change_price action requires account_unb');
  const currentTotal = currentOrderTotal(order);
  const currentTransport = currentOrderTransport(order);
  const gate = validatePriceChangeGate({
    featureEnabled: action?.gates?.feature_enabled === true,
    uniqueShowtime: action?.gates?.unique_showtime === true,
    quantityConfirmed: action?.gates?.quantity_confirmed === true,
    selectionConfirmed: action?.gates?.selection_confirmed === true,
    quoteValid: action?.gates?.quote_valid === true,
    orderLinked: action?.gates?.order_linked === true,
    orderOwned: String(order?.accountUnb ?? order?.account_unb ?? '').trim() === expectedAccountUnb,
    // A listing unit can represent a ticket bundle. Quote ticket count must be
    // verified independently, but it is not required to equal the marketplace
    // product-unit quantity when the quote workflow explicitly declares it.
    orderQuantityMatches: action?.order_quantity_policy === 'listing_unit'
      ? Number.isSafeInteger(Number(order?.quantity)) && Number(order.quantity) >= 1
      : Number(order?.quantity) === expectedQuantity,
    quoteAmountMatches: expectedTotal === targetTotal,
    transportFeeSupported: transportFee === 0 && (currentTransport == null || currentTransport === 0),
    orderUnpaid: UNPAID_ORDER_STATUSES.has(order?.orderStatus ?? order?.status),
    humanTakeover: action?.gates?.human_takeover === true || humanTakeover,
    targetAmountCents: targetTotal,
    maxTargetAmountCents: action?.gates?.max_amount_cents ?? 200_000,
  });
  return Object.freeze({
    gate,
    currentTotal,
    currentTransport,
    targetTotal,
    targetPrice: priceFee,
    targetTransport: transportFee,
    expectedQuantity,
  });
}

function attachPriceChangeReview(error, review) {
  if (!error || typeof error !== 'object') return;
  const { currentTotal, targetTotal, currentTransport } = review;
  const direction = currentTotal == null
    ? null
    : targetTotal > currentTotal
      ? 'increase'
      : targetTotal < currentTotal
        ? 'decrease'
        : 'unchanged';
  try {
    error.priceChangeReview = Object.freeze({
      ...(currentTotal != null ? { current_total_cents: currentTotal } : {}),
      target_total_cents: targetTotal,
      ...(currentTransport != null ? { current_transport_cents: currentTransport } : {}),
      ...(direction ? { direction } : {}),
    });
  } catch {
    // Classification still works if a third-party error object is frozen.
  }
}

function defaultSleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function detectHumanTakeover(core, action, messageRegistry) {
  if (typeof core.im?.getSessionByOrder !== 'function' || typeof core.im?.listMessages !== 'function') return false;
  const session = await core.im.getSessionByOrder(String(action.order_id));
  const address = session && replyAddress({
    account_unb: session.accountUnb ?? session.account_unb,
    chat_id: session.chatId ?? session.chat_id,
    peer_unb: session.peerUnb ?? session.peer_unb,
  });
  if (!address) return false;
  const history = await core.im.listMessages({ accountUnb: address.accountUnb, chatId: address.chatId, pageSize: 20 });
  // A historical message without a provider timestamp cannot prove a *recent*
  // takeover. It must not block an otherwise verified unpaid order forever.
  return hasRecentHumanTakeover(Array.isArray(history?.items) ? history.items : [], action.tenant_id, address.chatId, messageRegistry, Date.now(), action.human_takeover_window_ms, { requireTimestamp: true });
}

async function hasRecentHumanTakeover(messages, tenantId, chatId, messageRegistry, now, configuredWindowMs = 20_000, { requireTimestamp = false, ignorePlatformPriceChangeNotice = false } = {}) {
  for (const message of messages) {
    if (String(message?.direction).toLowerCase() !== 'outbound') continue;
    if (await messageRegistry.wasSentMessage(tenantId, chatId, message.messageId)) continue;
    // A successful plugin price change causes Xianyu to insert its own outbound
    // order card immediately before the plugin receives order.price.changed.
    // That card is not a seller takeover. Ignore it only for the verified
    // plugin-originated price-change acknowledgement; ordinary replies and
    // human text/image messages retain the normal takeover gate.
    if (ignorePlatformPriceChangeNotice && isPlatformPriceChangeNotice(message)) continue;
    const sentAt = Date.parse(String(message?.sentAt ?? message?.sent_at ?? ''));
    const windowMs = Number.isInteger(configuredWindowMs) && configuredWindowMs >= 5_000 && configuredWindowMs <= 60_000
      ? configuredWindowMs : 20_000;
    if (!Number.isFinite(sentAt)) {
      if (!requireTimestamp) return true;
      continue;
    }
    if (now >= sentAt && now - sentAt < windowMs) return true;
  }
  return false;
}

function isPlatformPriceChangeNotice(message) {
  const messageType = Number(message?.messageType ?? message?.message_type ?? message?.type);
  const content = String(message?.content ?? message?.text ?? message?.body?.text ?? '').replace(/\s+/gu, '').trim();
  if (messageType === 26) return true;
  return /^(?:你|我|卖家)?已?(?:修改|调整)(?:了)?(?:订单)?(?:价格|金额)/u.test(content)
    || /^(?:订单)?(?:价格|金额)已(?:修改|调整)/u.test(content);
}

async function resolveReplyAddress(core, action) {
  const direct = replyAddress(action);
  if (direct) return direct;
  const orderId = String(action.order_id ?? '').trim();
  if (!orderId || typeof core.im?.getSessionByOrder !== 'function') return null;
  const session = await core.im.getSessionByOrder(orderId);
  if (!session) return null;
  return replyAddress({
    account_unb: session.accountUnb ?? session.account_unb,
    chat_id: session.chatId ?? session.chat_id,
    peer_unb: session.peerUnb ?? session.peer_unb,
  });
}

function replyAddress(action) {
  const accountUnb = String(action.account_unb ?? '').trim();
  const chatId = String(action.chat_id ?? '').trim();
  const peerUnb = String(action.peer_unb ?? '').trim();
  return accountUnb && chatId && peerUnb ? { accountUnb, chatId, peerUnb } : null;
}

function requireCents(value, name) {
  const amount = Number(value);
  if (!Number.isSafeInteger(amount) || amount < 0 || amount > MAX_PRICE_CHANGE_CENTS) {
    throw new TypeError(`${name} must be a non-negative integer no greater than 10000000000`);
  }
  return amount;
}

function requirePositiveInteger(value, name) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 1 || number > 20) {
    throw new TypeError(`${name} must be an integer between 1 and 20`);
  }
  return number;
}

function safeTotalCents(priceFee, transportFee) {
  const total = priceFee + transportFee;
  if (!Number.isSafeInteger(total) || total > MAX_PRICE_CHANGE_CENTS) {
    throw new TypeError('price_fee and transport_fee total must be no greater than 10000000000');
  }
  return total;
}

function integerOrNull(value) {
  const amount = Number(value);
  return Number.isSafeInteger(amount) && amount > 0 ? amount : null;
}

function replyImageFilename(contentType) {
  if (contentType === 'image/png') return 'wanda-reply-image.png';
  if (contentType === 'image/webp') return 'wanda-reply-image.webp';
  return 'wanda-reply-image.jpg';
}

function decodeBase64Image(value) {
  const text = String(value ?? '').trim();
  if (!text) return null;
  const payload = text.includes(',') ? text.slice(text.indexOf(',') + 1) : text;
  return Buffer.from(payload, 'base64');
}

function currentOrderTotal(order) {
  // The documented plugin order view exposes payment/postFee as string cents.
  // Legacy aliases remain only to reconcile older platform mirrors.
  for (const value of [order?.payment, order?.paidAmount, order?.paid_amount_cents, order?.priceFee, order?.price_fee, order?.totalFee, order?.total_fee]) {
    const amount = Number(value);
    if (Number.isSafeInteger(amount) && amount >= 0) return amount;
  }
  return null;
}

function currentOrderTransport(order) {
  for (const value of [order?.postFee, order?.post_fee, order?.transportFee, order?.transport_fee]) {
    const amount = Number(value);
    if (Number.isSafeInteger(amount) && amount >= 0) return amount;
  }
  return null;
}

function isUnknownNetworkResult(error) {
  return error?.name === 'AbortError'
    || error?.name === 'TimeoutError'
    || error?.code === 'ECONNRESET'
    || error?.code === 'ETIMEDOUT'
    || error instanceof TypeError;
}

