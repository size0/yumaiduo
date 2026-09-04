import { join } from 'node:path';
import { createPriceChangeExecutor } from '../actions/change-order-price.mjs';
import {
  FulfillmentRequestError,
  fulfillmentFingerprint,
  fulfillmentIdentity,
  fulfillmentRequestFromTicketImage,
  normalizeFulfillmentRequest,
  ticketImageUrl,
  sameFulfillmentFingerprint,
} from '../actions/fulfillment.mjs';
import { normalizeAuthoritativeOrder, orderChangeability } from '../actions/contracts.mjs';
import { V2EventStore } from './event-store.mjs';

function text(value) { const result = String(value ?? '').trim(); return result || null; }
function wait(delayMs) { return new Promise((resolve) => setTimeout(resolve, delayMs)); }
function objectPayload(payload) { return payload && typeof payload === 'object' ? payload : {}; }
const BACKEND_EVENT_PAYLOAD_FIELDS = new Set([
  'accountUnb', 'account_unb', 'chatId', 'chat_id', 'peerUnb', 'peer_unb',
  'buyerUnb', 'buyer_unb', 'orderId', 'order_id', 'platformOrderId', 'platform_order_id',
  'itemId', 'item_id', 'orderStatus', 'order_status', 'messageType', 'message_type', 'remoteMessageId', 'remote_message_id',
  'messageId', 'message_id', 'content', 'text', 'imageUrls', 'image_urls', 'sentAtMs', 'sent_at_ms',
]);
function backendPayload(payload) {
  const source = objectPayload(payload);
  const result = {};
  for (const [key, value] of Object.entries(source)) {
    if (!BACKEND_EVENT_PAYLOAD_FIELDS.has(key)) continue;
    if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) result[key] = value;
    else if (Array.isArray(value) && value.every((item) => typeof item === 'string')) result[key] = [...value];
    else if (key === 'content' && value && typeof value === 'object' && !Array.isArray(value)) {
      const content = {};
      for (const field of ['text', 'content']) if (typeof value[field] === 'string') content[field] = value[field];
      if (Object.keys(content).length > 0) result[key] = content;
    }
  }
  return result;
}
function backendEnvelope(envelope) {
  return {
    id: text(envelope?.id), tenantId: text(envelope?.tenantId), event: text(envelope?.event),
    ts: Number(envelope?.ts), payload: backendPayload(envelope?.payload),
  };
}
function sessionFromPayload(payload) { const source = objectPayload(payload); const accountUnb = text(source.accountUnb ?? source.account_unb); const chatId = text(source.chatId ?? source.chat_id); const peerUnb = text(source.peerUnb ?? source.peer_unb); return accountUnb && chatId && peerUnb ? { accountUnb, chatId, peerUnb } : null; }
function orderIdFromPayload(payload) { const source = objectPayload(payload); return text(source.orderId ?? source.order_id ?? source.platformOrderId ?? source.platform_order_id); }
function messageTime(value) {
  for (const candidate of [value?.sentAtMs, value?.sent_at_ms, value?.timestamp, value?.sendTime, value?.createdAt, value?.created_at]) {
    const numeric = Number(candidate);
    if (Number.isFinite(numeric) && numeric > 0) return numeric;
    const parsed = Date.parse(String(candidate ?? ''));
    if (Number.isFinite(parsed)) return parsed;
  }
  return 0;
}
function latestOrderIdFromMessages(messages) {
  let selected = null;
  let selectedTime = -1;
  for (const message of messages) {
    const orderId = orderIdFromPayload(message);
    if (!orderId) continue;
    const candidateTime = messageTime(message);
    if (selected === null || candidateTime > selectedTime) {
      selected = orderId;
      selectedTime = candidateTime;
    }
  }
  return selected;
}
function pageItems(value) { return Array.isArray(value?.items) ? value.items : Array.isArray(value?.records) ? value.records : Array.isArray(value?.list) ? value.list : Array.isArray(value?.data) ? value.data : []; }
function firstText(...values) { for (const value of values) { const normalized = text(value); if (normalized) return normalized; } return null; }
function paidOrder(order) {
  const status = text(order?.order_status)?.toLowerCase() ?? '';
  return Boolean(order?.paid_at) || ['2', 'paid', 'payment_success', '已付款', '支付成功'].includes(status);
}
function shippedOrder(order) {
  const status = text(order?.order_status)?.toLowerCase() ?? '';
  return ['3', '4', '5', 'shipped', 'ticket_sent', 'completed', 'finished', '已发货', '已完成'].includes(status);
}
function fulfillmentError(code, status = 409) {
  return Object.assign(new Error(code), { code, status });
}

const ACTION_GATE_PERMITS = Object.freeze({
  send_message: ['messageSendEnabled'], send_price_change_confirmation: ['messageSendEnabled'],
  send_image: ['messageSendEnabled'], guard_unverified_order: ['messageSendEnabled'],
  change_order_price: ['xianyuRepriceEnabled'], switch_fixed: ['xianyuRepriceEnabled'],
  'quote.switch_fixed': ['xianyuRepriceEnabled'], create_liangpiao_order: ['liangpiaoOrderCreateEnabled'],
  cancel_paid_amount_mismatch: ['refundEnabled'], cancel_failed_liangpiao_source_order: ['refundEnabled'],
  cancel_order: ['refundEnabled'], 'order.cancel': ['refundEnabled'], refund_or_intercept: ['refundEnabled'],
  submit_fulfillment: ['shipEnabled', 'wandaProviderWritesEnabled'],
  send_ticket: ['shipEnabled', 'wandaProviderWritesEnabled', 'messageSendEnabled'],
});

function permit(config, name) { return config?.[name] !== false; }
function actionGateReason(config, actionType) {
  if (config?.externalWritesEnabled === false) return 'external_writes_disabled';
  for (const name of ACTION_GATE_PERMITS[actionType] ?? []) {
    if (!permit(config, name)) {
      const snake = name.replace(/[A-Z]/g, (value) => `_${value.toLowerCase()}`).replace(/_enabled$/u, '');
      return `${snake}_disabled`;
    }
  }
  return null;
}
function closedOrder(order) {
  const status = text(order?.order_status)?.toLowerCase() ?? '';
  const refund = text(order?.refund_status)?.toLowerCase() ?? '';
  return /closed|cancel|关闭|取消/u.test(status) || /refunded|refund_success|退款成功/u.test(refund);
}
function authoritativeOrderMatchesSession(order, session) {
  if (!order || !session) return false;
  return (
    firstText(order.shop_id) === firstText(session.accountUnb, session.account_unb)
    && firstText(order.buyer_id) === firstText(session.peerUnb, session.peer_unb)
    && (!order.chat_id || firstText(order.chat_id) === firstText(session.chatId, session.chat_id))
  );
}
function sameSession(left, right) {
  return Boolean(
    left && right
    && firstText(left.accountUnb, left.account_unb) === firstText(right.accountUnb, right.account_unb)
    && firstText(left.chatId, left.chat_id) === firstText(right.chatId, right.chat_id)
    && firstText(left.peerUnb, left.peer_unb) === firstText(right.peerUnb, right.peer_unb)
  );
}
function platformMessageId(value) { return firstText(value?.messageId, value?.message_id, value?.remoteMessageId, value?.id); }
function platformMessageDirection(value) {
  const raw = firstText(value?.direction, value?.sender, value?.senderType, value?.fromRole, value?.role)?.toLowerCase();
  if (['buyer', 'customer', 'user', 'inbound', 'received', 'receive', 'peer'].includes(raw)) return 'buyer';
  if (['seller', 'shop', 'staff', 'human', 'manual', 'cs', 'service', 'outbound', 'sent', 'send'].includes(raw)) return 'seller';
  return null;
}
function platformMessageText(value) {
  const content = value?.content && typeof value.content === 'object' ? value.content : {};
  return firstText(content.text, content.content, value?.text, typeof value?.content === 'string' ? value.content : null);
}
function humanSellerMessage(value) {
  if (platformMessageDirection(value) !== 'seller') return false;
  const messageType = firstText(value?.messageType, value?.message_type);
  // FishMore transaction cards (notably 14/26) can appear as outbound records even
  // though no seller typed anything. Only ordinary text/image records establish handoff.
  return !messageType || ['1', '2'].includes(messageType);
}
function buyerImageMessage(value) {
  if (platformMessageDirection(value) !== 'buyer') return false;
  const messageType = firstText(value?.messageType, value?.message_type);
  const imageUrls = value?.imageUrls ?? value?.image_urls;
  return messageType === '2' || (Array.isArray(imageUrls) && imageUrls.length > 0);
}
function declaredTicketCount(value) {
  const content = (platformMessageText(value) ?? '').toLowerCase().replace(/[\s，。！？!?、,.]/gu, '');
  const match = content.match(/(?:^|要|需要|改成|改为)([1-9]\d?|[一二三四五六七八九十两]+)(?:张|票|个)?$/u)
    ?? content.match(/([1-9]\d?|[一二三四五六七八九十两]+)(?:张|票)$/u);
  if (!match) return null;
  const token = match[1];
  if (/^\d+$/u.test(token)) {
    const count = Number(token);
    return Number.isInteger(count) && count >= 1 && count <= 20 ? count : null;
  }
  return { 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 }[token] ?? null;
}
function buyerQuoteChangingMessage(value, expectedTicketCount = null) {
  if (platformMessageDirection(value) !== 'buyer') return false;
  if (buyerImageMessage(value)) return true;
  const messageType = firstText(value?.messageType, value?.message_type);
  if (messageType && messageType !== '1') return false;
  const content = (platformMessageText(value) ?? '').toLowerCase().replace(/[\s，。！？!?、,.]/gu, '');
  if (!content) return false;
  if (/(?:取消|退款|退票|不要了|不买了|先不买|cancel|refund)/iu.test(content)) return true;
  if (/(?:换|改成|改为|座位|座号|排|场次|影院|影城|电影|影片|开场|时间|日期|今天|明天|后天)/iu.test(content)) {
    return true;
  }
  const count = declaredTicketCount(value);
  if (count !== null && Number.isInteger(expectedTicketCount) && count === expectedTicketCount) return false;
  return count !== null;
}
function baselineHasNewerBuyerMessage(messages, envelope, predicate = () => true) {
  const payload = objectPayload(envelope?.payload);
  const currentId = firstText(payload.remoteMessageId, payload.remote_message_id, payload.messageId, payload.message_id);
  if (!currentId) return false;
  const current = messages.find((item) => platformMessageId(item) === currentId);
  const currentTime = messageTime(current);
  if (!current || !currentTime) return false;
  return messages.some((item) => (
    platformMessageDirection(item) === 'buyer'
    && predicate(item)
    && platformMessageId(item) !== currentId
    && messageTime(item) > currentTime
  ));
}
function platformMessageKey(value) {
  const id = platformMessageId(value);
  if (id) return `id:${id}`;
  const createdAt = firstText(value?.createdAt, value?.created_at, value?.sendTime, value?.timestamp) ?? '';
  const body = platformMessageText(value) ?? '';
  const direction = platformMessageDirection(value) ?? '';
  return createdAt || body ? `fallback:${direction}:${createdAt}:${body}` : null;
}
function platformFailure(error) {
  const status = Number(error?.status);
  const code = firstText(error?.code, error?.errorCode);
  const rejected = Number.isInteger(status) && status >= 400 && status < 500;
  return {
    status: rejected ? 'failed' : 'unknown',
    reason: rejected ? 'platform_api_rejected' : 'platform_action_result_unknown',
    ...(Number.isInteger(status) ? { platform_status: status } : {}),
    ...(code ? { error_code: code } : {}),
  };
}
function executorEnvelope(envelope, session, order, quoteSnapshot = null) {
  const payload = envelope?.payload && typeof envelope.payload === 'object' ? structuredClone(envelope.payload) : {};
  const orderId = firstText(order?.orderId, order?.order_id, orderIdFromPayload(payload), quoteSnapshot?.order_id);
  const accountUnb = firstText(session?.accountUnb, session?.account_unb, order?.accountUnb, order?.account_unb, payload.accountUnb, payload.account_unb, quoteSnapshot?.shop_id);
  const peerUnb = firstText(session?.peerUnb, session?.peer_unb, order?.buyerUnb, order?.buyer_unb, order?.peerUnb, order?.peer_unb, payload.peerUnb, payload.peer_unb, quoteSnapshot?.buyer_id);
  const chatId = firstText(session?.chatId, session?.chat_id, order?.chatId, order?.chat_id, payload.chatId, payload.chat_id, quoteSnapshot?.chat_id);
  return {
    ...envelope,
    payload: {
      ...payload,
      ...(orderId ? { orderId } : {}),
      ...(accountUnb ? { accountUnb } : {}),
      ...(peerUnb ? { peerUnb } : {}),
      ...(chatId ? { chatId } : {}),
    },
  };
}

export function createV2Runtime({ config, platform, backend, logger = console, store = new V2EventStore(join(config.dataDir, 'events.v2.json'), config.encryptionKey) }) {
  const activeSessions = new Set();
  const priceChangeReceiptStore = store.createPriceChangeReceiptStore();
  const sentAgentMessageIds = new Set();
  const keywordImageUploadCache = new Map();
  let running = 0;
  const inFlight = new Set();
  let draining = false;
  let drainRequested = false;
  let stopped = false;
  let retryTimer = null;
  let reminderTimer = null;
  let reminderPolling = false;
  let commandTimer = null;
  let commandPolling = false;

  function requestDrain(delayMs = 0) {
    if (delayMs > 0) {
      if (retryTimer) return;
      retryTimer = setTimeout(() => { retryTimer = null; requestDrain(); }, delayMs);
      return;
    }
    drainRequested = true;
    void drain();
  }
  async function start() {
    await store.initialize(); stopped = false; requestDrain();
    if (typeof backend.claimCommands === 'function') {
      commandTimer = setInterval(() => void pollCommands(), 250);
      commandTimer.unref?.();
      void pollCommands();
    }
    if (typeof backend.claimReminders === 'function') {
      reminderTimer = setInterval(() => void pollReminders(), 10_000);
      reminderTimer.unref?.();
      void pollReminders();
    }
  }
  async function stop() {
    stopped = true;
    if (retryTimer) clearTimeout(retryTimer);
    if (reminderTimer) clearInterval(reminderTimer);
    if (commandTimer) clearInterval(commandTimer);
    retryTimer = null;
    reminderTimer = null;
    commandTimer = null;
    await Promise.allSettled([...inFlight]);
  }
  async function health() { return { ok: !stopped, running, limit: config.maxConcurrentRuns, ...(await store.health()) }; }
  async function enqueue(envelope) { const result = await store.enqueue(envelope); requestDrain(); return result; }

  async function pollCommands() {
    if (stopped || commandPolling || typeof backend.claimCommands !== 'function') return { processed: 0 };
    commandPolling = true;
    let processed = 0;
    try {
      const claimed = await backend.claimCommands(Math.max(1, config.maxConcurrentRuns));
      const commands = Array.isArray(claimed?.commands) ? claimed.commands.slice(0, config.maxConcurrentRuns) : [];
      for (const command of commands) {
        const commandId = text(command?.command_id);
        const leaseToken = text(command?.lease_token);
        const tenantId = text(command?.tenant_id);
        const eventId = text(command?.event_id);
        const action = command?.action && typeof command.action === 'object'
          ? { ...structuredClone(command.action), reconciliation_only: command.reconciliation_only === true }
          : null;
        const context = command?.context && typeof command.context === 'object' ? command.context : {};
        const envelope = context.envelope && typeof context.envelope === 'object' ? context.envelope : null;
        let result;
        try {
          if (!commandId || !leaseToken || !tenantId || !eventId || !action || !envelope) throw new Error('durable_command_invalid');
          const record = await store.getEvent(`${tenantId}:${eventId}`);
          if (!record) throw new Error('durable_command_source_event_missing');
          const client = platform.createClient(tenantId);
          let session = context.session && typeof context.session === 'object' ? context.session : sessionFromPayload(envelope.payload);
          let order = context.order && typeof context.order === 'object' ? context.order : null;
          if ((!session || !order) && (orderIdFromPayload(envelope.payload) || action?.quote_snapshot?.order_id || action?.order_id)) {
            const official = await resolveOrderContext(client, executorEnvelope(envelope, session, order, action.quote_snapshot));
            session = official.session ?? session;
            order = official.order ?? order;
          }
          result = await executeAction({
            client, mode: 'auto', session, order, action, envelope, record,
            completedActionResult: action._completed_action_result ?? null,
            baselineMessages: Array.isArray(context.recent_messages) ? context.recent_messages : [],
            baselineAvailable: Array.isArray(context.recent_messages),
          });
        } catch (error) {
          logger.error('durable command execution failed', { commandId, eventId, error });
          result = { status: 'unknown', reason: 'durable_command_execution_unknown' };
        }
        try {
          await backend.reportCommand({ commandId, leaseToken, result });
        } catch (error) {
          logger.warn('durable command result deferred to lease recovery', { commandId, eventId, error });
        }
        processed += 1;
      }
    } catch (error) {
      logger.warn('durable command claim deferred', { error });
    } finally {
      commandPolling = false;
    }
    return { processed };
  }

  async function pollReminders() {
    if (stopped || reminderPolling || typeof backend.claimReminders !== 'function') return { processed: 0 };
    reminderPolling = true;
    let processed = 0;
    try {
      const claimed = await backend.claimReminders(10);
      const tasks = Array.isArray(claimed?.tasks) ? claimed.tasks.slice(0, 10) : [];
      for (const task of tasks) {
        const taskId = text(task?.task_id);
        const leaseToken = text(task?.lease_token);
        try {
          if (!taskId || !leaseToken) throw new Error('reminder_claim_invalid');
          const tenantId = text(task.tenant_id);
          const orderId = text(task.order_id);
          const expectedSession = {
            accountUnb: text(task.shop_id), chatId: text(task.chat_id), peerUnb: text(task.buyer_id),
          };
          if (!tenantId || !orderId || !expectedSession.accountUnb || !expectedSession.chatId || !expectedSession.peerUnb) throw new Error('reminder_binding_incomplete');
          const client = platform.createClient(tenantId);
          const officialSession = sessionFromPayload(await client.im.getSessionByOrder(orderId));
          if (!sameSession(expectedSession, officialSession)) throw new Error('reminder_session_mismatch');
          const order = await client.orders.get(orderId);
          const authoritativeOrder = order ? normalizeAuthoritativeOrder(order, { sdk_tenant_id: tenantId }) : null;
          if (!authoritativeOrder || closedOrder(authoritativeOrder)) {
            await backend.completeReminder(taskId, leaseToken, { status: 'skipped', reason: 'order_closed' });
            processed += 1;
            continue;
          }
          if (task.kind === 'pre_show_text') {
            const message = text(task.message);
            if (!message) throw new Error('reminder_message_missing');
            const recent = await readRecentMessages(client, officialSession);
            const existing = recent.messages.find((item) => platformMessageDirection(item) === 'seller' && platformMessageText(item) === message);
            if (existing) {
              await backend.completeReminder(taskId, leaseToken, { status: 'sent', deduplicated: true, message_id: platformMessageId(existing) });
            } else {
              const sent = await sendMessageWithReconciliation(client, officialSession, message, { eventId: taskId, actionId: `${taskId}:send` });
              const messageId = platformMessageId(sent);
              if (messageId) sentAgentMessageIds.add(messageId);
              await backend.completeReminder(taskId, leaseToken, { status: 'sent', message_id: messageId });
            }
          } else if (task.kind === 'post_show_receipt') {
            if (authoritativeOrder.order_status === '4') {
              await backend.completeReminder(taskId, leaseToken, { status: 'skipped', reason: 'order_already_completed' });
              processed += 1;
              continue;
            }
            if (authoritativeOrder.order_status !== '3') throw new Error('remind_receipt_order_not_shipped');
            if (typeof client.orders.remindReceipt !== 'function') throw new Error('remind_receipt_unavailable');
            const response = await client.orders.remindReceipt(orderId, { idempotencyKey: text(task.idempotency_key) });
            await backend.completeReminder(taskId, leaseToken, {
              status: 'reminded', already_reminded: response?.alreadyReminded === true,
            });
          } else throw new Error('reminder_kind_unsupported');
          processed += 1;
        } catch (error) {
          logger.warn('v2 reminder deferred', { taskId, error });
          if (taskId && leaseToken && typeof backend.failReminder === 'function') {
            await backend.failReminder(taskId, leaseToken, text(error?.message) ?? 'reminder_failed').catch(() => {});
          }
        }
      }
    } catch (error) {
      logger.warn('v2 reminder poll deferred', { error });
    } finally {
      reminderPolling = false;
    }
    return { processed };
  }

  async function syncShops(client, tenantId) {
    // Shops are platform truth; a failed event-time sync never blocks event handling.
    try {
      const shops = await client.shops.list();
      const normalized = Array.isArray(shops) ? shops : [];
      await backend.syncShops(tenantId, normalized);
      return { ok: true, count: normalized.length };
    } catch (error) {
      logger.warn('v2 shop sync deferred', { tenantId, error });
      return { ok: false, count: 0, error: 'platform_shop_sync_failed' };
    }
  }

  async function syncTenantShops(tenantId) {
    const normalizedTenant = text(tenantId);
    if (!normalizedTenant) throw Object.assign(new Error('tenant_required'), { status: 401 });
    const result = await syncShops(platform.createClient(normalizedTenant), normalizedTenant);
    if (!result.ok) throw Object.assign(new Error(result.error), { status: 503 });
    return result;
  }

  async function listTenantOrders(tenantId, limit = 50) {
    const normalizedTenant = text(tenantId);
    if (!normalizedTenant) throw Object.assign(new Error('tenant_required'), { status: 401 });
    const boundedLimit = Math.max(1, Math.min(Number(limit) || 50, 100));
    const references = await store.listOrderReferences(normalizedTenant, boundedLimit);
    const client = platform.createClient(normalizedTenant);
    const orders = [];
    for (let offset = 0; offset < references.length; offset += 5) {
      const batch = references.slice(offset, offset + 5);
      const settled = await Promise.allSettled(batch.map((reference) => client.orders.get(reference.orderId)));
      for (const [index, result] of settled.entries()) {
        if (result.status !== 'fulfilled' || !result.value) continue;
        const order = result.value;
        const resolvedOrderId = text(order.orderId) ?? batch[index].orderId;
        orders.push({
          orderId: resolvedOrderId,
          accountUnb: text(order.accountUnb) ?? batch[index].accountUnb,
          orderStatus: Number.isInteger(order.orderStatus) ? order.orderStatus : null,
          orderStatusText: text(order.orderStatusText),
          itemId: text(order.itemId),
          productTitle: text(order.productTitle),
          sku: text(order.sku),
          quantity: Number.isInteger(order.quantity) ? order.quantity : null,
          payment: text(order.payment),
          postFee: text(order.postFee),
          buyerNick: text(order.buyerNick),
          payTime: text(order.payTime),
          createTime: text(order.createTime),
          observedAt: batch[index].observedAt,
          lastEvent: batch[index].event,
          fulfillment: await store.getWandaFulfillment(normalizedTenant, resolvedOrderId),
        });
      }
    }
    orders.sort((left, right) => String(right.createTime ?? right.observedAt).localeCompare(String(left.createTime ?? left.observedAt)));
    return { orders, count: orders.length, observedCount: references.length };
  }

  async function getTenantOrder(tenantId, orderId) {
    const normalizedTenant = text(tenantId);
    const normalizedOrderId = text(orderId);
    if (!normalizedTenant || !normalizedOrderId) throw Object.assign(new Error('order_not_found'), { status: 404 });
    const references = await store.listOrderReferences(normalizedTenant, 500);
    if (!references.some((reference) => reference.orderId === normalizedOrderId)) {
      throw Object.assign(new Error('order_not_found'), { status: 404 });
    }
    const order = await platform.createClient(normalizedTenant).orders.get(normalizedOrderId);
    if (!order) throw Object.assign(new Error('order_not_found'), { status: 404 });
    return {
      orderId: text(order.orderId) ?? normalizedOrderId,
      accountUnb: text(order.accountUnb),
      orderStatus: Number.isInteger(order.orderStatus) ? order.orderStatus : null,
      orderStatusText: text(order.orderStatusText),
      itemId: text(order.itemId),
      productTitle: text(order.productTitle),
      sku: text(order.sku),
      quantity: Number.isInteger(order.quantity) ? order.quantity : null,
      payment: text(order.payment),
      postFee: text(order.postFee),
      buyerNick: text(order.buyerNick),
      payTime: text(order.payTime),
      createTime: text(order.createTime),
      fulfillment: await store.getWandaFulfillment(normalizedTenant, normalizedOrderId),
    };
  }

  async function fulfillTenantOrder(tenantId, orderId, input, idempotencyKey) {
    if (config.fulfillmentEnabled !== true) throw fulfillmentError('wanda_fulfillment_disabled', 503);
    for (const actionType of ['submit_fulfillment', 'send_ticket']) {
      const gateReason = actionGateReason(config, actionType);
      if (gateReason) throw fulfillmentError(gateReason, 503);
    }
    const normalizedTenant = text(tenantId);
    const normalizedOrderId = text(orderId);
    if (!normalizedTenant || !normalizedOrderId) throw fulfillmentError('order_not_found', 404);
    const normalizedIdempotencyKey = text(idempotencyKey);
    if (!normalizedIdempotencyKey || normalizedIdempotencyKey.length < 8 || normalizedIdempotencyKey.length > 200) {
      throw new FulfillmentRequestError('idempotency_key_invalid', 400);
    }
    const existing = await store.getWandaFulfillment(normalizedTenant, normalizedOrderId);
    const client = platform.createClient(normalizedTenant);
    const rawOrder = await client.orders.get(normalizedOrderId);
    if (!rawOrder) throw fulfillmentError('order_not_found', 404);
    const imageUrl = ticketImageUrl(input);
    let fulfillmentInput = input;
    if (imageUrl) {
      if (typeof backend.recognizeFulfillmentImage !== 'function') throw fulfillmentError('ticket_image_recognition_unavailable', 503);
      const recognized = await backend.recognizeFulfillmentImage({ tenantId: normalizedTenant, imageUrl });
      fulfillmentInput = fulfillmentRequestFromTicketImage(input, recognized);
    }
    const request = normalizeFulfillmentRequest(fulfillmentInput);
    const officialSession = sessionFromPayload(await client.im.getSessionByOrder(normalizedOrderId));
    const normalizedOrder = normalizeAuthoritativeOrder(rawOrder, { sdk_tenant_id: normalizedTenant });
    const authoritativeOrder = {
      ...normalizedOrder,
      shop_id: normalizedOrder.shop_id ?? firstText(officialSession?.accountUnb, officialSession?.account_unb),
      buyer_id: normalizedOrder.buyer_id ?? firstText(officialSession?.peerUnb, officialSession?.peer_unb),
      chat_id: normalizedOrder.chat_id ?? firstText(officialSession?.chatId, officialSession?.chat_id),
    };
    if (!officialSession || !authoritativeOrderMatchesSession(authoritativeOrder, officialSession)) {
      throw fulfillmentError('order_session_identity_unverified');
    }
    for (const [field, supplied, actual] of [
      ['shop_id', request.shop_id, authoritativeOrder.shop_id],
      ['buyer_id', request.buyer_id, authoritativeOrder.buyer_id],
      ['chat_id', request.chat_id, authoritativeOrder.chat_id],
    ]) {
      if (supplied && supplied !== actual) throw fulfillmentError(`${field}_mismatch`);
    }
    const fingerprint = fulfillmentFingerprint({ tenantId: normalizedTenant, orderId: normalizedOrderId, order: authoritativeOrder, request });
    if (existing && !sameFulfillmentFingerprint(existing, fingerprint)) throw fulfillmentError('wanda_fulfillment_conflict');
    async function deliverFulfillmentMessage() {
      const recent = await readRecentMessages(client, officialSession);
      if (!recent.available) throw fulfillmentError('conversation_snapshot_unavailable', 503);
      const alreadySent = recent.messages.some((message) => (
        platformMessageDirection(message) === 'seller' && platformMessageText(message) === request.message_text
      ));
      if (!alreadySent) await sendMessageWithReconciliation(client, officialSession, request.message_text, {
        eventId: `wanda-fulfillment-${normalizedTenant}-${normalizedOrderId}`,
        actionId: `${normalizedOrderId}:fulfillment-message`,
      });
    }
    if (existing?.status === 'submitted') {
      if (existing.message_status === 'sent') return { status: 'already_submitted', order: rawOrder, fulfillment: existing };
      if (!shippedOrder(authoritativeOrder)) throw fulfillmentError('ticket_ship_status_not_verified');
      try { await deliverFulfillmentMessage(); }
      catch (error) {
        throw error?.code ? error : fulfillmentError('ticket_shipped_message_result_unknown', 503);
      }
      const repaired = { ...existing, status: 'submitted', message_status: 'sent', updated_at: new Date().toISOString() };
      await store.saveWandaFulfillment(repaired);
      return { status: 'already_submitted', order: rawOrder, fulfillment: repaired };
    }
    if (existing?.status === 'processing') throw fulfillmentError('wanda_fulfillment_in_progress', 409);
    if (existing?.status === 'unknown') throw fulfillmentError('wanda_fulfillment_unknown_manual_reconciliation');
    if (shippedOrder(authoritativeOrder)) throw fulfillmentError('order_already_shipped_unreconciled');
    if (closedOrder(authoritativeOrder)) throw fulfillmentError('order_closed_or_refunding');
    if (!paidOrder(authoritativeOrder)) throw fulfillmentError('order_not_paid');
    if (typeof client.orders.ship !== 'function') throw fulfillmentError('order_ship_unavailable', 503);

    const baseRecord = {
      ...fulfillmentIdentity({ tenantId: normalizedTenant, orderId: normalizedOrderId, order: authoritativeOrder }),
      ...request,
      request_fingerprint: fingerprint,
      idempotency_key: normalizedIdempotencyKey,
      status: 'processing',
      message_status: 'pending',
      updated_at: new Date().toISOString(),
    };
    try {
      await store.saveWandaFulfillment(baseRecord);
    } catch (error) {
      if (error?.message === 'wanda_fulfillment_conflict') throw fulfillmentError('wanda_fulfillment_conflict');
      throw error;
    }
    try {
      await client.orders.ship(normalizedOrderId, {
        ...(request.ticket_codes.length ? { ticketCode: request.ticket_codes.join('、') } : {}),
        ...(request.ticket_url ? { ticketUrl: request.ticket_url } : {}),
      });
    } catch (error) {
      await store.saveWandaFulfillment({ ...baseRecord, status: 'unknown', failure_code: 'platform_ship_result_unknown', updated_at: new Date().toISOString() });
      throw fulfillmentError('platform_ship_result_unknown', 503);
    }
    const afterShip = normalizeAuthoritativeOrder(await client.orders.get(normalizedOrderId), { sdk_tenant_id: normalizedTenant });
    if (!shippedOrder(afterShip)) {
      await store.saveWandaFulfillment({ ...baseRecord, status: 'unknown', failure_code: 'platform_ship_not_verified', updated_at: new Date().toISOString() });
      throw fulfillmentError('platform_ship_not_verified', 502);
    }
    try { await deliverFulfillmentMessage(); }
    catch (error) {
      await store.saveWandaFulfillment({ ...baseRecord, status: 'submitted', message_status: 'unknown', updated_at: new Date().toISOString() });
      throw error?.code ? error : fulfillmentError('ticket_shipped_message_result_unknown', 503);
    }
    const completed = { ...baseRecord, status: 'submitted', message_status: 'sent', updated_at: new Date().toISOString() };
    await store.saveWandaFulfillment(completed);
    return { status: 'submitted', order: await client.orders.get(normalizedOrderId), fulfillment: completed };
  }


  async function readRecentMessages(client, session) {
    if (!session) return { available: false, messages: [] };
    try {
      const page = await client.im.listMessages({ ...session, pageSize: 50 });
      return { available: true, messages: pageItems(page).slice(0, 50) };
    } catch (error) {
      logger.warn('v2 im history sync deferred', { error });
      return { available: false, messages: [] };
    }
  }

  async function sendMessageWithReconciliation(client, session, message, { eventId, actionId }) {
    const gateReason = actionGateReason(config, 'send_message');
    if (gateReason) throw fulfillmentError(gateReason, 503);
    const startedAt = Date.now();
    try {
      return await client.im.sendMessage({ ...session, text: message });
    } catch (error) {
      logger.warn('v2 message send deferred', {
        eventId, actionId, status: error?.status, errorCode: error?.code ?? error?.errorCode, error,
      });
      const delays = Number(error?.status) === 429 ? [250] : [250, 750, 1_500];
      for (const delayMs of delays) {
        await wait(delayMs);
        const latest = await readRecentMessages(client, session);
        const matching = latest.messages.find((item) => (
          platformMessageDirection(item) === 'seller'
          && platformMessageText(item) === message
          && messageTime(item) >= startedAt - 5_000
        ));
        if (matching) return { messageId: platformMessageId(matching), reconciled: true };
      }
      // HTTP 429 is an explicit rejection by the core and therefore safe to retry
      // once after proving that no matching outbound message entered history.
      if (Number(error?.status) === 429) {
        return client.im.sendMessage({ ...session, text: message });
      }
      throw error;
    }
  }

  async function conversationPreflight(
    client, session, baselineMessages, baselineAvailable,
    {
      allowNewBuyerMessages = false, blockNewBuyerImages = false,
      blockBuyerQuoteChanges = false, allowHumanSellerMessages = false,
      expectedTicketCount = null,
    } = {},
  ) {
    const latest = await readRecentMessages(client, session);
    if (!baselineAvailable) return { allowed: false, reason: 'conversation_snapshot_unavailable' };
    if (!latest.available) return { allowed: false, reason: 'conversation_preflight_unavailable' };
    const baselineIds = new Set(baselineMessages.map(platformMessageKey).filter(Boolean));
    const baselineNewestTime = Math.max(0, ...baselineMessages.map(messageTime));
    const additions = latest.messages.filter((item) => {
      const id = platformMessageKey(item);
      const occurredAt = messageTime(item);
      return id && !baselineIds.has(id) && (!occurredAt || !baselineNewestTime || occurredAt >= baselineNewestTime);
    });
    if (!allowHumanSellerMessages && additions.some((item) => (
      humanSellerMessage(item)
      && item.agent_generated !== true
      && !sentAgentMessageIds.has(platformMessageId(item))
      && !sentAgentMessageIds.has(platformMessageKey(item))
    ))) {
      return { allowed: false, reason: 'human_message_arrived_before_send' };
    }
    if (blockNewBuyerImages && additions.some(buyerImageMessage)) {
      return { allowed: false, reason: 'newer_buyer_image_arrived_before_send' };
    }
    if (blockBuyerQuoteChanges && additions.some((item) => (
      buyerQuoteChangingMessage(item, expectedTicketCount)
    ))) {
      return { allowed: false, reason: 'buyer_quote_inputs_changed_before_price_change' };
    }
    if (!allowNewBuyerMessages && additions.some((item) => platformMessageDirection(item) === 'buyer')) {
      return { allowed: false, reason: 'buyer_message_arrived_before_send' };
    }
    return { allowed: true };
  }

  async function resolveOrderContext(client, envelope) {
    const orderId = orderIdFromPayload(envelope.payload);
    const isOrderEvent = String(envelope.event ?? '').startsWith('order.');
    // For lifecycle events, the event payload is only a locator.  The
    // conversation identity must come from the official order session reread,
    // never from webhook-supplied account/buyer/chat aliases.
    let session = isOrderEvent && orderId ? null : sessionFromPayload(envelope.payload);
    const propagationDelays = isOrderEvent ? [0, 250, 750, 1_500] : [0];

    async function propagationRead(read) {
      let lastError;
      for (let attempt = 0; attempt < propagationDelays.length; attempt += 1) {
        if (propagationDelays[attempt] > 0) await wait(propagationDelays[attempt]);
        try { return { value: await read(), error: null, attempts: attempt + 1 }; }
        catch (error) { lastError = error; }
      }
      return { value: null, error: lastError, attempts: propagationDelays.length };
    }

    if ((isOrderEvent && orderId) || (!session && orderId)) {
      const resolved = await propagationRead(() => client.im.getSessionByOrder(orderId));
      session = sessionFromPayload(resolved.value);
      if (!session && resolved.error) {
        logger.warn('v2 order session lookup deferred', {
          eventId: envelope.id, event: envelope.event, attempts: resolved.attempts, error: resolved.error,
        });
      }
    }
    let order = null;
    if (orderId) {
      const resolved = await propagationRead(() => client.orders.get(orderId));
      order = resolved.value;
      if (!order && resolved.error) {
        // Order context enriches the Agent input. High-risk actions still perform
        // their own authoritative order read before changing any platform state.
        logger.warn('v2 order context lookup deferred', {
          eventId: envelope.id, event: envelope.event, attempts: resolved.attempts, error: resolved.error,
        });
      }
    }
    return { session, order };
  }

  async function drain() {
    if (draining || stopped) return;
    draining = true;
    try {
      do {
        drainRequested = false;
        while (!stopped && running < config.maxConcurrentRuns) {
          const record = await store.claim(activeSessions);
          if (!record) break;
          const job = record;
          running += 1; activeSessions.add(job.sessionKey);
          const work = process(record);
          const tracked = Promise.resolve(work)
            .catch((error) => logger.error('v2 worker failed', { sessionKey: job.sessionKey, error }))
            .finally(() => {
              running -= 1;
              activeSessions.delete(job.sessionKey);
              inFlight.delete(tracked);
              requestDrain();
            });
          inFlight.add(tracked);
        }
      } while (!stopped && drainRequested);
    } finally { draining = false; }
  }

  async function process(record) {
    const { envelope } = record;
    try {
      const client = platform.createClient(envelope.tenantId);
      await syncShops(client, envelope.tenantId);
      const resolved = await resolveOrderContext(client, envelope);
      const { session } = resolved;
      let { order } = resolved;
      const recent = await readRecentMessages(client, session);
      if (!order) {
        const recentOrderId = latestOrderIdFromMessages(recent.messages);
        if (recentOrderId) {
          try {
            order = await client.orders.get(recentOrderId);
          } catch (error) {
            logger.warn('v2 recent order context lookup deferred', { eventId: envelope.id, event: envelope.event, error });
          }
        }
      }
      const recentMessages = recent.messages.map((message) => ({
        ...message,
        ...(sentAgentMessageIds.has(platformMessageId(message)) ? { agent_generated: true } : {}),
      }));
      // Never forward the provider's raw order object. The normalized snapshot
      // contains only the fields needed for routing and deterministic rules;
      // recipient PII remains behind the platform's sensitive-data endpoint.
      const backendOrder = order
        ? normalizeAuthoritativeOrder(order, { sdk_tenant_id: envelope.tenantId })
        : null;
      const accepted = await backend.processEvent({
        envelope: backendEnvelope(envelope), session, order: backendOrder, recentMessages,
      });
      if (accepted?.accepted !== true || !text(accepted?.event_id)) throw new Error('rules_first_event_not_accepted');
      await store.complete(record.id, record.lease, { accepted: true, duplicate: accepted.duplicate === true });
      void pollCommands();
    } catch (error) {
      logger.error('v2 event failed', { eventId: envelope.id, event: envelope.event, error });
      await store.fail(record.id, record.lease, String(error?.message ?? 'unknown_error'));
    }
  }

  async function executeAction({ client, mode, session, order, action, envelope, record, completedActionResult = null, baselineMessages = [], baselineAvailable = false }) {
    const actionId = text(action?.id) ?? `${envelope.id}:action`;
    const receipt = await store.beginAction(record.id, actionId);
    const gateReason = actionGateReason(config, action?.type);
    if (gateReason) {
      const blocked = { status: 'skipped', reason: gateReason, action_attempted: false };
      await store.finishAction(record.id, actionId, blocked);
      return { actionId, ...blocked, nextActions: [] };
    }
    const isPriceChange = action?.type === 'change_order_price';
    const isPaidMismatchCancellation = action?.type === 'cancel_paid_amount_mismatch';
    const isRecoverableMutation = isPriceChange || isPaidMismatchCancellation;
    if (receipt.status === 'started' && !receipt.newlyStarted && !isRecoverableMutation) return { actionId, status: 'unknown', reason: 'previous_platform_action_result_unknown', nextActions: [] };
    if (receipt.status === 'finished' && (!isRecoverableMutation || receipt.result?.status !== 'unknown')) return { actionId, ...receipt.result, nextActions: [] };

    async function readBoundOrder(actionOrderId) {
      if (!session || !actionOrderId) return null;
      const officialSession = sessionFromPayload(await client.im.getSessionByOrder(actionOrderId));
      if (!sameSession(session, officialSession)) return null;
      const normalized = normalizeAuthoritativeOrder(await client.orders.get(actionOrderId), { sdk_tenant_id: envelope.tenantId });
      const authoritative = {
        ...normalized,
        buyer_id: normalized.buyer_id ?? firstText(officialSession.peerUnb, officialSession.peer_unb),
        chat_id: normalized.chat_id ?? firstText(officialSession.chatId, officialSession.chat_id),
      };
      if (authoritative.order_id !== actionOrderId || !authoritativeOrderMatchesSession(authoritative, session)) return null;
      return authoritative;
    }

    let result;
    try {
      if (isPriceChange) {
        const newFlowSnapshot = action.quote_snapshot?.flow_version === 'V4_NEW_FLOW_V2';
        const newFlowClaimed = newFlowSnapshot
          || action.flow_version === 'V4_NEW_FLOW_V2'
          || action.source === 'phase_9a_authorization';
        const newFlowBindingValid = !newFlowClaimed
          || (action.flow_version === 'V4_NEW_FLOW_V2'
            && action.source === 'phase_9a_authorization'
            && action.quote_snapshot?.source === 'phase_9a_authorization'
            && action.idempotency_key === action.quote_snapshot?.idempotency_key);
        if (!newFlowBindingValid) {
          result = { status: 'skipped', reason: 'new_flow_command_identity_mismatch' };
        } else if (mode !== 'auto') result = { status: 'skipped', reason: `mode_${mode}` };
        else {
          const preflight = await conversationPreflight(
            client, session, baselineMessages, baselineAvailable,
            {
              allowNewBuyerMessages: true, blockBuyerQuoteChanges: true,
              expectedTicketCount: Number.isInteger(Number(action.quote_snapshot?.confirmed_ticket_count))
                ? Number(action.quote_snapshot.confirmed_ticket_count) : null,
              allowHumanSellerMessages: false,
            },
          );
          if (!preflight.allowed) {
            result = { status: 'skipped', reason: preflight.reason };
            if (preflight.reason === 'buyer_quote_inputs_changed_before_price_change') {
              logger.warn('buyer_message_blocked_price_change', { eventId: envelope.id, actionId });
            }
          } else {
            if (action.reconciliation_only === true) {
              const actionOrderId = text(action.quote_snapshot?.order_id);
              const targetAmount = Number(action.quote_snapshot?.target_amount_cents);
              const currentOrder = await readBoundOrder(actionOrderId);
              result = currentOrder && Number.isSafeInteger(targetAmount) && currentOrder.amount_cents === targetAmount
                ? {
                    status: 'succeeded', reason_code: 'read_only_reconciliation_verified',
                    order_id: actionOrderId, target_amount_cents: targetAmount,
                    verified_amount_cents: currentOrder.amount_cents, reconciled: true,
                  }
                : {
                    status: 'unknown', reason_code: 'read_only_reconciliation_not_verified',
                    order_id: actionOrderId, target_amount_cents: targetAmount,
                    verified_amount_cents: currentOrder?.amount_cents ?? null, reconciled: true,
                  };
            } else {
              const executor = createPriceChangeExecutor({
                sdk: client,
                tenant_id: envelope.tenantId,
                receipt_store: priceChangeReceiptStore,
              });
              result = await executor.execute({
                provider_event: executorEnvelope(envelope, session, order, action.quote_snapshot),
                quote_snapshot: action.quote_snapshot,
              });
            }
            if (result?.status === 'unknown') {
              logger.warn('price_change_submitted_unverified', {
                eventId: envelope.id, actionId, orderId: result.order_id, reasonCode: result.reason_code,
              });
            }
          }
        }
      } else if (action?.type === 'guard_unverified_order') {
        if (mode !== 'auto') result = { status: 'skipped', reason: `mode_${mode}` };
        else {
          const preflight = await conversationPreflight(
            client, session, baselineMessages, baselineAvailable,
            { allowNewBuyerMessages: true, allowHumanSellerMessages: false },
          );
          if (!preflight.allowed) result = { status: 'skipped', reason: preflight.reason };
          else {
            const actionOrderId = text(action.order_id);
            const currentOrder = await readBoundOrder(actionOrderId);
            const message = text(paidOrder(currentOrder) ? action.paid_text : action.unpaid_text);
            if (!currentOrder || closedOrder(currentOrder) || !message) result = { status: 'skipped', reason: 'unverified_order_guard_not_applicable' };
            else {
              const sent = await sendMessageWithReconciliation(client, session, message, { eventId: envelope.id, actionId });
              result = { status: 'succeeded', message_id: platformMessageId(sent), order_id: actionOrderId, paid: paidOrder(currentOrder) };
            }
          }
        }
      } else if (isPaidMismatchCancellation) {
        if (mode !== 'auto') result = { status: 'skipped', reason: `mode_${mode}` };
        else {
          const preflight = await conversationPreflight(
            client, session, baselineMessages, baselineAvailable,
            { allowNewBuyerMessages: true, allowHumanSellerMessages: false },
          );
          if (!preflight.allowed) result = { status: 'skipped', reason: preflight.reason };
          else {
            const actionOrderId = text(action.order_id);
            const targetAmount = Number(action.target_amount_cents);
            const observedOrderAmount = Number(action.observed_order_amount_cents);
            const observedPaidAmount = Number(action.observed_amount_cents);
            const refundAuthorization = text(action.refund_authorization);
            let currentOrder = await readBoundOrder(actionOrderId);
            if (!currentOrder || !Number.isSafeInteger(targetAmount) || targetAmount <= 0) result = { status: 'skipped', reason: 'paid_mismatch_order_unavailable' };
            else if (currentOrder.amount_cents === targetAmount) result = { status: 'skipped', reason: 'paid_mismatch_amount_now_matches' };
            else if (
              refundAuthorization !== 'unchanged_prechange_amount'
              || !Number.isSafeInteger(observedOrderAmount) || observedOrderAmount <= 0
              || !Number.isSafeInteger(observedPaidAmount) || observedPaidAmount !== observedOrderAmount
              || currentOrder.amount_cents !== observedOrderAmount
            ) result = { status: 'skipped', reason: 'manual_price_change_or_amount_unverified' };
            else if (!paidOrder(currentOrder) && !closedOrder(currentOrder)) result = { status: 'skipped', reason: 'paid_mismatch_order_not_paid' };
            else {
              let cancelAttempted = false;
              let cancelConfirmed = closedOrder(currentOrder);
              if (!cancelConfirmed && receipt.newlyStarted) {
                try {
                  await client.orders.cancel(actionOrderId, {
                    reason: '付款金额与已核验报价不一致，自动关闭并退款',
                    idempotencyKey: `${actionId}:cancel`,
                  });
                  cancelAttempted = true;
                } catch {
                  cancelAttempted = true;
                }
              }
              if (!cancelConfirmed) {
                for (const delayMs of [250, 750, 1_500]) {
                  await wait(delayMs);
                  try { currentOrder = await readBoundOrder(actionOrderId); }
                  catch { currentOrder = null; }
                  if (closedOrder(currentOrder)) { cancelConfirmed = true; break; }
                }
              }
              const message = text(cancelConfirmed ? action.closed_text : action.refund_text);
              if (!message) result = { status: 'skipped', reason: 'paid_mismatch_recovery_text_missing' };
              else {
                const sent = await sendMessageWithReconciliation(client, session, message, { eventId: envelope.id, actionId });
                result = {
                  status: 'succeeded', message_id: platformMessageId(sent), order_id: actionOrderId,
                  target_amount_cents: targetAmount, verified_amount_cents: currentOrder?.amount_cents ?? null,
                  cancel_attempted: cancelAttempted, cancel_confirmed: cancelConfirmed,
                };
              }
            }
          }
        }
      } else if (action?.type === 'send_image') {
        if (mode !== 'auto') result = { status: 'skipped', reason: `mode_${mode}` };
        else {
          const assetId = text(action.image_asset_id);
          if (!session || !assetId || !/^ki-[0-9a-f]{40}$/u.test(assetId)) {
            result = { status: 'skipped', reason: 'invalid_keyword_image_action' };
          } else {
            const preflight = await conversationPreflight(
              client, session, baselineMessages, baselineAvailable,
              {
                allowNewBuyerMessages: false,
                allowHumanSellerMessages: false,
              },
            );
            if (!preflight.allowed) result = { status: 'skipped', reason: preflight.reason };
            else if (typeof client.im?.sendImage !== 'function') {
              result = { status: 'failed', reason: 'keyword_image_capability_unavailable' };
            } else {
              const cacheKey = `${envelope.tenantId}:${session.accountUnb}:${assetId}`;
              let uploaded = keywordImageUploadCache.get(cacheKey);
              if (!uploaded && typeof store.getKeywordImageUpload === 'function') {
                const persisted = await store.getKeywordImageUpload({
                  tenantId: envelope.tenantId, accountUnb: session.accountUnb, assetId,
                });
                if (persisted) {
                  uploaded = {
                    imageUrl: persisted.imageUrl, width: persisted.width, height: persisted.height,
                  };
                  keywordImageUploadCache.set(cacheKey, uploaded);
                }
              }
              if (!uploaded) {
                if (typeof backend.fetchKeywordImage !== 'function' || typeof client.im?.uploadImage !== 'function') {
                  result = { status: 'failed', reason: 'keyword_image_capability_unavailable' };
                } else {
                  const asset = await backend.fetchKeywordImage({ tenantId: envelope.tenantId, assetId });
                  const uploadResult = await client.im.uploadImage({
                    accountUnb: session.accountUnb,
                    filename: asset.filename || text(action.image_filename) || 'keyword-image',
                    contentType: asset.contentType,
                    data: asset.data,
                  });
                  const imageUrl = firstText(uploadResult?.imageUrl, uploadResult?.image_url, uploadResult?.url);
                  const width = Number(uploadResult?.width);
                  const height = Number(uploadResult?.height);
                  if (!imageUrl || !/^https:\/\/[^/]*alicdn\.com\//iu.test(imageUrl) || !Number.isFinite(width) || width <= 0 || !Number.isFinite(height) || height <= 0) {
                    throw new Error('keyword_image_upload_result_invalid');
                  }
                  uploaded = { imageUrl, width: Math.round(width), height: Math.round(height) };
                  keywordImageUploadCache.set(cacheKey, uploaded);
                  if (typeof store.saveKeywordImageUpload === 'function') {
                    await store.saveKeywordImageUpload({
                      tenantId: envelope.tenantId, accountUnb: session.accountUnb, assetId,
                      ...uploaded, sha256: asset.sha256,
                      expiresAt: new Date(Date.now() + 24 * 60 * 60 * 1_000).toISOString(),
                    });
                  }
                }
              }
              if (result?.status === 'failed') return finishAndReport(result);
              const sent = await client.im.sendImage({ ...session, ...uploaded });
              const messageId = platformMessageId(sent);
              if (messageId) sentAgentMessageIds.add(messageId);
              result = {
                status: 'succeeded', message_id: messageId, image_asset_id: assetId,
              };
            }
          }
        }
      } else if (action?.type !== 'send_message' && action?.type !== 'send_price_change_confirmation') result = { status: 'skipped', reason: 'unsupported_action' };
      else if (mode !== 'auto') result = { status: 'skipped', reason: `mode_${mode}` };
      else {
        const message = text(action.text);
        if (!session || !message || message.length > 1_000) result = { status: 'skipped', reason: 'invalid_reply_address_or_text' };
        else {
          const preserveOnNewBuyer = action.preserve_on_new_buyer_message === true;
          const suppressOnNewerImage = action.suppress_on_newer_image === true;
          const alreadySupersededInQueue = (
            action.type === 'send_message'
            && !preserveOnNewBuyer
            && action.rule_governed !== true
            && typeof store.hasNewerSessionEvent === 'function'
            && await store.hasNewerSessionEvent(record.id)
          );
          const alreadySupersededByImage = (
            suppressOnNewerImage
            && baselineHasNewerBuyerMessage(baselineMessages, envelope, buyerImageMessage)
          );
          const alreadySuperseded = (
            action.type === 'send_message'
            && !preserveOnNewBuyer
            && baselineHasNewerBuyerMessage(baselineMessages, envelope)
          );
          const preflight = alreadySupersededInQueue
            ? { allowed: false, reason: 'newer_session_event_already_queued' }
            : alreadySupersededByImage
              ? { allowed: false, reason: 'newer_buyer_image_already_present' }
            : alreadySuperseded
              ? { allowed: false, reason: 'buyer_message_already_newer' }
            : await conversationPreflight(
              client,
              session,
              baselineMessages,
              baselineAvailable,
              {
                // A verified price result is already bound to the authoritative
                // order read.  Ordinary buyer follow-ups must not suppress the
                // completion notice; human seller messages still hold it.
                allowNewBuyerMessages: action.type === 'send_price_change_confirmation'
                  ? true : preserveOnNewBuyer,
                blockNewBuyerImages: suppressOnNewerImage,
                allowHumanSellerMessages: false,
              },
            );
          if (!preflight.allowed) result = { status: 'skipped', reason: preflight.reason };
          // Human replies always stop automation. New buyer messages stop ordinary
          // text replies, but do not discard an image recognition already in flight.
          else if (action.type === 'send_price_change_confirmation') {
            try {
              const currentOrderId = firstText(action.order_id, order?.orderId, order?.order_id, orderIdFromPayload(envelope.payload));
              const changedOrderId = firstText(completedActionResult?.order_id);
              if (!currentOrderId || !changedOrderId) throw new Error('price_change_confirmation_order_id_missing');
              if (currentOrderId !== changedOrderId) result = { status: 'skipped', reason: 'price_change_confirmation_order_mismatch' };
              else {
                const currentOrder = await readBoundOrder(currentOrderId);
                const eligibility = orderChangeability(currentOrder);
                if (!currentOrder) result = { status: 'skipped', reason: 'price_change_confirmation_order_unavailable' };
                else if (!eligibility.allowed) result = { status: 'skipped', reason: `price_change_confirmation_${eligibility.reason_code}` };
                else {
                  const sent = await sendMessageWithReconciliation(client, session, message, { eventId: envelope.id, actionId });
                  result = { status: 'succeeded', message_id: platformMessageId(sent) };
                }
              }
            } catch {
              result = { status: 'skipped', reason: 'price_change_confirmation_order_unavailable' };
            }
          } else {
            // The platform history endpoint cannot identify whether an outbound message
            // was sent by this agent or by a human. Treating every outbound record as a
            // handoff suppresses the buyer's next message after an AI reply.
            const sent = await sendMessageWithReconciliation(client, session, message, { eventId: envelope.id, actionId });
            const messageId = platformMessageId(sent);
            if (messageId) sentAgentMessageIds.add(messageId);
            result = { status: 'succeeded', message_id: messageId };
          }
        }
      }
    } catch (error) {
      logger.warn('v2 platform action failed', { eventId: envelope.id, actionId, actionType: action?.type, error });
      result = platformFailure(error);
    }
    await store.finishAction(record.id, actionId, result);
    return { actionId, ...result, nextActions: [] };
  }

  return Object.freeze({ start, stop, health, enqueue, syncTenantShops, listTenantOrders, getTenantOrder, fulfillTenantOrder, pollCommands, pollReminders });
}
