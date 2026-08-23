import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { hasRequiredPricingAccountEvidence } from './quote/quote-evidence-policy.mjs';

const MAX_MESSAGES = 50;
const MEMORY_MS = 24 * 60 * 60 * 1_000;
const QUOTE_DRAFT_TTL_MS = 10 * 60 * 1_000;
const FIRST_CONTACT_WINDOW_MS = 2 * 60 * 1_000;
const FULFILLMENT_STAGES = new Set(['paid_manual_delivery', 'ticket_issued', 'ticket_sent', 'fulfillment_exception']);
const FULFILLMENT_UPDATES = new Set(['ticket_issued', 'ticket_sent', 'fulfillment_exception']);
const FULFILLMENT_TRANSITIONS = Object.freeze({
  paid_manual_delivery: new Set(['ticket_issued', 'fulfillment_exception']),
  ticket_issued: new Set(['ticket_sent', 'fulfillment_exception']),
  ticket_sent: new Set(['fulfillment_exception']),
  fulfillment_exception: new Set(['ticket_issued', 'ticket_sent']),
});

export class ConversationContextStore {
  constructor(file, { now = () => Date.now() } = {}) { this.file = file; this.now = now; this.chain = Promise.resolve(); }
  async add(tenantId, payload, receivedAt = null) {
    return this.#mutate((state) => {
      const key = [tenantId, payload.accountUnb, payload.chatId, payload.peerUnb].map((v) => String(v ?? '')).join(':');
      const isNewConversation = !state[key];
      const current = state[key] ?? { facts: {}, messages: [] };
      current.address = {
        tenant_id: String(tenantId ?? ''), account_unb: String(payload.accountUnb ?? payload.account_unb ?? ''),
        chat_id: String(payload.chatId ?? payload.chat_id ?? ''), peer_unb: String(payload.peerUnb ?? payload.peer_unb ?? ''),
      };
      const imageUrls = Array.isArray(payload.imageUrls)
        ? payload.imageUrls.filter((value) => typeof value === 'string' && value.trim()).map((value) => value.trim()).slice(0, 4)
        : [];
      const candidateAt = Number(receivedAt);
      const messageAt = Number.isFinite(candidateAt) && candidateAt > 0 ? candidateAt : this.now();
      const previousLatestAt = Math.max(0, ...current.messages.map((item) => Number(item?.at) || 0));
      const message = { at: messageAt, role: 'buyer', text: String(payload.content ?? payload.text ?? '').slice(0, 1000), image: imageUrls.length > 0, image_urls: imageUrls };
      current.messages = [...current.messages, message]
        .filter((x) => this.now() - x.at <= MEMORY_MS)
        .sort((left, right) => left.at - right.at)
        .slice(-MAX_MESSAGES);
      const buyerName = buyerDisplayName(payload);
      current.facts = {
        ...current.facts,
        ...(isNewConversation ? { first_seen_at: messageAt } : {}),
        ...(buyerName ? { buyer_name: buyerName } : {}),
        ...(messageAt >= previousLatestAt ? extractFacts(message.text) : {}),
      };
      state[key] = current;
      return structuredClone(current);
    });
  }
  async get(tenantId, payload) { const state = await this.#read(); return state[conversationKey(tenantId, payload)] ?? { facts: {}, messages: [] }; }
  async listRecent({ tenantId = null, limit = 20 } = {}) {
    const state = await this.#read();
    return Object.entries(state).map(([key, current]) => {
      const parts = key.split(':');
      const address = current.address ?? { tenant_id: parts[0], account_unb: parts[1], chat_id: parts[2], peer_unb: parts.slice(3).join(':') };
      return {
        tenant_id: String(address.tenant_id ?? ''), account_unb: String(address.account_unb ?? ''), chat_id: String(address.chat_id ?? ''), peer_unb: String(address.peer_unb ?? ''),
        last_message_at: Math.max(0, ...(current.messages ?? []).map((item) => Number(item?.at) || 0)),
        facts: structuredClone(current.facts ?? {}), messages: structuredClone(current.messages ?? []),
      };
    }).filter((item) => (!tenantId || item.tenant_id === String(tenantId)) && item.account_unb && item.chat_id && item.peer_unb)
      .sort((left, right) => right.last_message_at - left.last_message_at).slice(0, Math.max(1, Math.min(100, Number(limit))));
  }
  async recordAgentTurn(tenantId, payload, input = {}) {
    const intent = String(input.intent ?? '').replace(/\s+/gu, ' ').trim().slice(0, 32);
    const confidence = Number(input.confidence);
    const goal = String(input.goal ?? '').replace(/\s+/gu, ' ').trim().slice(0, 160);
    const action = String(input.action ?? '').trim().slice(0, 64);
    const status = String(input.status ?? '').trim().slice(0, 32);
    const missingFields = Array.isArray(input.missingFields)
      ? input.missingFields.slice(0, 10).map((item) => String(item ?? '').replace(/\s+/gu, ' ').trim().slice(0, 64)).filter(Boolean)
      : [];
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      current.facts = {
        ...current.facts,
        agent_intent: intent,
        agent_confidence: Number.isFinite(confidence) && confidence >= 0 && confidence <= 1 ? confidence : null,
        agent_goal: goal,
        agent_pending_action: action,
        agent_missing_fields: missingFields,
        agent_last_status: status,
        agent_updated_at: this.now(),
      };
      state[key] = current;
      return true;
    });
  }
  async recordQuoteDraft(tenantId, payload, input = {}) {
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      if ([...FULFILLMENT_STAGES, 'aftersale', 'cancelled'].includes(current.facts.stage)) return null;
      const previous = activeQuoteDraft(current.facts.quote_draft, this.now()) ?? {};
      const draft = normalizeQuoteDraft(input, previous, this.now());
      if (!draft) return null;
      current.facts = { ...current.facts, quote_draft: draft };
      state[key] = current;
      return structuredClone(draft);
    });
  }
  async claimQuoteDraftAttempt(tenantId, payload) {
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      const draft = activeQuoteDraft(current.facts.quote_draft, this.now());
      if (!draft) return false;
      const fingerprint = quoteDraftFingerprint(draft);
      if (!fingerprint || draft.last_attempt_fingerprint === fingerprint) return false;
      current.facts = {
        ...current.facts,
        quote_draft: { ...draft, state: 'matching', last_attempt_fingerprint: fingerprint, last_attempt_at: this.now() },
      };
      state[key] = current;
      return true;
    });
  }
  async markQuoted(tenantId, payload, { validForMs = 10 * 60 * 1_000, unitQuoteCents = null, totalQuoteCents = null, ticketCount = null, cinema = null, movie = null, date = null, showtime = null, hall = null, quoteScope = null, memberCostTotalCents = null, originalPriceTotalCents = null, channelFeeTotalCents = null, pricingSource = null, pricingAccountRef = null, pricingRuleVersion = null, replyDelivered = false, deliveryActionId = null, platformMessageId = null, circledDeliveryImageUrl = null } = {}) {
    const validFor = Number(validForMs);
    if (!Number.isFinite(validFor) || validFor <= 0) throw new TypeError('validForMs must be positive');
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      const safeDeliveryActionId = String(deliveryActionId ?? '').trim().slice(0, 300);
      const safePlatformMessageId = String(platformMessageId ?? '').trim().slice(0, 240);
      const safeCircledImageUrl = safeHttpsUrl(circledDeliveryImageUrl, 2_000);
      if (safeDeliveryActionId && current.facts.quote_delivery_action_id === safeDeliveryActionId) return structuredClone(current);
      const snapshot = {};
      if (Number.isSafeInteger(unitQuoteCents) && unitQuoteCents > 0) snapshot.quote_unit_cents = unitQuoteCents;
      if (Number.isSafeInteger(totalQuoteCents) && totalQuoteCents > 0) snapshot.quote_total_cents = totalQuoteCents;
      if (Number.isSafeInteger(ticketCount) && ticketCount > 0) snapshot.quote_ticket_count = ticketCount;
      const canonicalCinema = String(cinema ?? '').replace(/\s+/gu, ' ').trim().slice(0, 160);
      if (canonicalCinema) snapshot.cinema = canonicalCinema;
      const safeScope = ['area_probe', 'exact_seats'].includes(String(quoteScope)) ? String(quoteScope) : '';
      if (safeScope) snapshot.quote_scope = safeScope;
      const pricingVersion = String(pricingRuleVersion ?? '').trim().slice(0, 80);
      if (pricingVersion) snapshot.pricing_rule_version = pricingVersion;
      const safePricingSource = String(pricingSource ?? '').replace(/\s+/gu, ' ').trim().slice(0, 80);
      if (safePricingSource) snapshot.pricing_source = safePricingSource;
      const safePricingAccountRef = String(pricingAccountRef ?? '').trim();
      if (/^[a-f0-9]{32}$/u.test(safePricingAccountRef)) snapshot.pricing_account_ref = safePricingAccountRef;
      if (replyDelivered === true) snapshot.quote_reply_delivered = true;
      const at = this.now();
      const priorHistory = Array.isArray(current.facts.quote_history)
        ? current.facts.quote_history.slice(-99).map((item) => ({ ...item }))
        : [];
      const priorQuote = priorHistory.at(-1) ?? {};
      if (['quoted', 'quote_confirmed'].includes(priorQuote.stage)) priorHistory[priorHistory.length - 1].stage = 'quote_replaced';
      const quoteRecord = normalizeQuoteRecord({
        id: `quote-${at}-${priorHistory.length}`,
        quoted_at: at,
        expires_at: at + validFor,
        stage: 'quoted',
        cinema: canonicalCinema || priorQuote.cinema,
        movie: movie || priorQuote.movie,
        date: date || priorQuote.date,
        showtime: showtime || priorQuote.showtime,
        hall: hall || priorQuote.hall,
        quote_scope: safeScope || priorQuote.quote_scope,
        quote_unit_cents: snapshot.quote_unit_cents,
        quote_total_cents: snapshot.quote_total_cents,
        quote_ticket_count: snapshot.quote_ticket_count,
        member_cost_total_cents: memberCostTotalCents,
        original_price_total_cents: originalPriceTotalCents,
        channel_fee_total_cents: channelFeeTotalCents,
        pricing_source: pricingSource,
        pricing_account_ref: snapshot.pricing_account_ref,
        pricing_rule_version: pricingVersion || null,
        quote_reply_delivered: replyDelivered === true,
      });
      current.facts = {
        ...current.facts, ...snapshot, stage: 'quoted', quote_confirmed: false, quote_expires_at: at + validFor,
        ...(quoteRecord ? { quote_record_id: quoteRecord.id, quote_history: [...priorHistory, quoteRecord] } : {}),
        ...(safeDeliveryActionId ? { quote_delivery_action_id: safeDeliveryActionId } : {}),
        ...(safePlatformMessageId ? { quote_reply_platform_message_id: safePlatformMessageId } : {}),
        ...(safeCircledImageUrl ? {
          seat_delivery_instruction: 'buyer_circled_image', seat_delivery_instruction_recorded_at: at,
          seat_delivery_instruction_image_url: safeCircledImageUrl,
        } : {}),
      };
      state[key] = current;
      return structuredClone(current);
    });
  }
  async markQuoteConfirmed(tenantId, payload) {
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      const facts = current.facts;
      const confirmable = Number(facts.quote_expires_at ?? 0) >= this.now()
        && Number.isSafeInteger(facts.quote_total_cents) && facts.quote_total_cents > 0
        && Number.isSafeInteger(facts.quote_ticket_count) && facts.quote_ticket_count > 0
        && Boolean(String(facts.pricing_rule_version ?? '').trim())
        && facts.quote_reply_delivered === true
        && hasRequiredPricingAccountEvidence(facts);
      if (!confirmable) return false;
      current.facts = updateLatestQuoteRecord(
        { ...current.facts, stage: 'quote_confirmed', quote_confirmed: true },
        { stage: 'quote_confirmed', confirmed_at: this.now() },
      );
      state[key] = current;
      return true;
    });
  }
  async markQuoteNeedsReview(tenantId, payload) {
    return this.markOrderException(tenantId, payload, 'ticket_count_conflict');
  }
  async markOrderException(tenantId, payload, reason, orderId = null) {
    const safeReason = String(reason ?? '').trim().slice(0, 100);
    const safeOrderId = String(orderId ?? '').trim().slice(0, 128);
    if (!safeReason) throw new TypeError('exception reason is required');
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      current.facts = updateLatestQuoteRecord({
        ...current.facts,
        stage: 'exception_review',
        quote_confirmed: false,
        exception_reason: safeReason,
        ...(safeOrderId ? { order_id: safeOrderId } : {}),
      }, { stage: 'exception_review', exception_reason: safeReason, ...(safeOrderId ? { order_id: safeOrderId } : {}) });
      state[key] = current;
      return structuredClone(current);
    });
  }
  async bindOrder(tenantId, payload, orderId, order = {}) {
    const orderIdText = String(orderId ?? '').trim();
    if (!orderIdText) return false;
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      if ([...FULFILLMENT_STAGES, 'aftersale'].includes(current.facts.stage)) return true;
      if (Number(current.facts.quote_expires_at ?? 0) < this.now()) {
        current.facts = { ...current.facts, stage: 'quote_expired' };
        state[key] = current;
        return false;
      }
      // Marketplace listing units and their initial listing price are not the
      // quoted ticket count or final verified total. The executor re-reads the
      // authoritative unpaid order and applies all price-change gates.
      void order;
      current.facts = updateLatestQuoteRecord(
        { ...current.facts, order_id: orderIdText },
        { order_id: orderIdText, order_created_at: this.now() },
      );
      state[key] = current;
      return true;
    });
  }
  async setOrderStage(tenantId, payload, stage, orderId) {
    const safeStage = String(stage ?? '').trim();
    const order = String(orderId ?? '').trim();
    if (!safeStage || !order) throw new TypeError('stage and orderId are required');
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      if (FULFILLMENT_STAGES.has(current.facts.stage) && safeStage === 'paid_manual_delivery') {
        return structuredClone(current);
      }
      current.facts = updateLatestQuoteRecord(
        { ...current.facts, stage: safeStage, order_id: order },
        { stage: safeStage, order_id: order, ...(safeStage === 'paid_manual_delivery' ? { paid_succeeded_at: this.now() } : {}) },
      );
      state[key] = current;
      return structuredClone(current);
    });
  }
  async updateFulfillment(tenantId, orderId, status) {
    const tenant = String(tenantId ?? '').trim();
    const order = String(orderId ?? '').trim().slice(0, 128);
    const nextStatus = String(status ?? '').trim();
    if (!FULFILLMENT_UPDATES.has(nextStatus)) throw new TypeError('invalid_fulfillment_status');
    if (!order) throw new TypeError('order_not_found');
    return this.#mutate((state) => {
      const matches = Object.entries(state).filter(([key, value]) => (
        key.startsWith(`${tenant}:`) && String(value?.facts?.order_id ?? '') === order
      ));
      if (matches.length !== 1) throw new TypeError(matches.length ? 'order_not_unique' : 'order_not_found');
      const [key, current] = matches[0];
      if (!FULFILLMENT_STAGES.has(current?.facts?.stage)) throw new TypeError('order_not_paid_for_fulfillment');
      if (current.facts.stage === nextStatus) {
        return Object.freeze({
          order_id: order,
          stage: nextStatus,
          fulfillment_updated_at: Number(current.facts.fulfillment_updated_at ?? 0) || null,
          fulfillment_history: structuredClone(Array.isArray(current.facts.fulfillment_history) ? current.facts.fulfillment_history : []),
        });
      }
      if (!FULFILLMENT_TRANSITIONS[current.facts.stage]?.has(nextStatus)) {
        throw new TypeError('invalid_fulfillment_transition');
      }
      const at = this.now();
      const history = Array.isArray(current.facts.fulfillment_history)
        ? current.facts.fulfillment_history.slice(-49)
        : [];
      const entry = { at, status: nextStatus, source: 'authenticated_ui' };
      current.facts = updateLatestQuoteRecord({
        ...current.facts,
        stage: nextStatus,
        fulfillment_updated_at: at,
        fulfillment_history: [...history, entry],
      }, { stage: nextStatus, fulfillment_updated_at: at });
      state[key] = current;
      return Object.freeze({ order_id: order, stage: nextStatus, fulfillment_updated_at: at, fulfillment_history: structuredClone(current.facts.fulfillment_history) });
    });
  }
  async listOperations(tenantId) {
    const state = await this.#read();
    const prefix = `${String(tenantId)}:`;
    const operationalStages = new Set(['quoted', 'quote_confirmed', 'waiting_payment', ...FULFILLMENT_STAGES, 'aftersale', 'exception_review', 'quote_expired']);
    return Object.entries(state)
      .filter(([key, value]) => key.startsWith(prefix) && operationalStages.has(value?.facts?.stage))
      .map(([key, value]) => operationItem(key, value))
      .sort((left, right) => Number(right.updated_at ?? 0) - Number(left.updated_at ?? 0));
  }
  async listQuoteRecords(tenantId) {
    const state = await this.#read();
    const prefix = `${String(tenantId)}:`;
    const records = Object.entries(state).flatMap(([key, value]) => {
      if (!key.startsWith(prefix)) return [];
      const [, account_unb, chat_id, peer_unb] = key.split(':');
      const history = Array.isArray(value?.facts?.quote_history) ? value.facts.quote_history : [];
      return history.map((record) => ({
        ...record,
        stage: ['quoted', 'quote_confirmed'].includes(record.stage) && Number(record.expires_at) <= this.now() ? 'quote_expired' : record.stage,
        account_unb,
        chat_id,
        peer_unb,
        buyer_label: buyerNameFromFacts(value.facts, peer_unb),
      }));
    });
    const aggregates = new Map();
    for (const record of records) {
      const key = screeningKey(record);
      const aggregate = aggregates.get(key) ?? { samples: 0, orders: 0, paid: 0 };
      aggregate.samples += 1;
      if (record.order_id) aggregate.orders += 1;
      if (record.paid_succeeded_at) aggregate.paid += 1;
      aggregates.set(key, aggregate);
    }
    return records.map((record) => {
      const aggregate = aggregates.get(screeningKey(record));
      return {
        ...record,
        screening_sample_size: aggregate.samples,
        screening_order_created_count: aggregate.orders,
        screening_paid_success_count: aggregate.paid,
        screening_order_success_rate: percentage(aggregate.orders, aggregate.samples),
        screening_paid_success_rate: percentage(aggregate.paid, aggregate.samples),
      };
    }).sort((left, right) => Number(right.quoted_at ?? 0) - Number(left.quoted_at ?? 0));
  }
  async listTicketOrders(tenantId) {
    const state = await this.#read();
    const prefix = `${String(tenantId)}:`;
    return Object.entries(state)
      .filter(([key, value]) => key.startsWith(prefix) && [...FULFILLMENT_STAGES, 'aftersale', 'exception_review'].includes(value?.facts?.stage))
      .map(([key, value]) => {
        const [, account_unb, chat_id, peer_unb] = key.split(':');
        const stage = operationalStage(value.facts);
        return {
          account_unb,
          chat_id,
          peer_unb,
          buyer_label: buyerNameFromFacts(value.facts, peer_unb),
          stage,
          order_id: String(value.facts.order_id ?? ''),
          exception_reason: stage === 'paid_unmanaged' ? '' : String(value.facts.exception_reason ?? ''),
          seat_delivery_instruction: value.facts.seat_delivery_instruction === 'buyer_circled_image'
            ? '按买家原图圈选位置出票'
            : '',
          seat_delivery_image_recorded: Boolean(value.facts.seat_delivery_instruction_image_url),
          fulfillment_updated_at: Number(value.facts.fulfillment_updated_at ?? 0) || null,
          fulfillment_history: Array.isArray(value.facts.fulfillment_history) ? value.facts.fulfillment_history.slice(-20) : [],
          updated_at: Number(value.facts.fulfillment_updated_at ?? value.messages?.at?.(-1)?.at ?? 0) || null,
        };
      })
      .sort((left, right) => Number(right.updated_at ?? 0) - Number(left.updated_at ?? 0));
  }
  async claimFirstContactNotice(tenantId, payload) {
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key];
      const firstSeenAt = Number(current?.facts?.first_seen_at ?? 0);
      if (!current || !firstSeenAt || this.now() - firstSeenAt > FIRST_CONTACT_WINDOW_MS || current.facts.first_contact_notice_at) return false;
      current.facts = { ...current.facts, first_contact_notice_at: this.now() };
      state[key] = current;
      return true;
    });
  }
  async releaseFirstContactNotice(tenantId, payload) {
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key];
      if (!current?.facts?.first_contact_notice_at) return false;
      current.facts = { ...current.facts };
      delete current.facts.first_contact_notice_at;
      state[key] = current;
      return true;
    });
  }
  async claimQuoteProcessingReceipt(tenantId, payload) {
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key];
      const firstSeenAt = Number(current?.facts?.first_seen_at ?? 0);
      if (!current || !firstSeenAt || this.now() - firstSeenAt > FIRST_CONTACT_WINDOW_MS || current.facts.quote_processing_receipt_at) return false;
      current.facts = { ...current.facts, quote_processing_receipt_at: this.now() };
      state[key] = current;
      return true;
    });
  }
  async releaseQuoteProcessingReceipt(tenantId, payload) {
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key];
      if (!current?.facts?.quote_processing_receipt_at) return false;
      current.facts = { ...current.facts };
      delete current.facts.quote_processing_receipt_at;
      state[key] = current;
      return true;
    });
  }
  async claimConfirmation(tenantId, payload, imageUrl) {
    const image = String(imageUrl ?? '').trim();
    if (!image) return false;
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      if (current.facts.confirmation_image_url === image) return false;
      current.facts = { ...current.facts, confirmation_image_url: image, confirmation_image_recorded_at: this.now() };
      delete current.facts.purchase_info_confirmed_image_url;
      delete current.facts.purchase_info_confirmed_at;
      state[key] = current;
      return true;
    });
  }
  async confirmPurchaseInfo(tenantId, payload, imageUrl) {
    const image = String(imageUrl ?? '').trim();
    if (!image) return false;
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      if (current.facts.confirmation_image_url !== image) return false;
      current.facts = {
        ...current.facts,
        purchase_info_confirmed_image_url: image,
        purchase_info_confirmed_at: this.now(),
      };
      state[key] = current;
      return true;
    });
  }
  async recordCircledDeliveryInstruction(tenantId, payload, imageUrl = '') {
    const image = String(imageUrl ?? '').trim();
    return this.#mutate((state) => {
      const key = conversationKey(tenantId, payload);
      const current = state[key] ?? { facts: {}, messages: [] };
      current.facts = {
        ...current.facts,
        seat_delivery_instruction: 'buyer_circled_image',
        seat_delivery_instruction_recorded_at: this.now(),
        ...(image ? { seat_delivery_instruction_image_url: image.slice(0, 2_000) } : {}),
      };
      state[key] = current;
      return true;
    });
  }
  async #mutate(fn) {
    const pending = this.chain.then(async () => {
      const state = await this.#read();
      const value = fn(state);
      await this.#write(state);
      return value;
    });
    this.chain = pending.catch(() => undefined);
    return pending;
  }
  async #read() {
    try {
      return sanitizeConversationState(JSON.parse(await readFile(this.file, 'utf8')), this.now());
    } catch (e) {
      if (e.code === 'ENOENT') return {};
      throw e;
    }
  }
  async #write(state) { await mkdir(dirname(this.file), { recursive: true }); const tmp = `${this.file}.tmp`; await writeFile(tmp, JSON.stringify(state), 'utf8'); await rename(tmp, this.file); }
}

function sanitizeConversationState(value, now) {
  const state = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  for (const current of Object.values(state)) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) continue;
    current.messages = (Array.isArray(current.messages) ? current.messages : [])
      .filter((item) => Number.isFinite(Number(item?.at)) && now - Number(item.at) <= MEMORY_MS)
      .sort((left, right) => Number(left.at) - Number(right.at))
      .slice(-MAX_MESSAGES);
    const facts = current.facts && typeof current.facts === 'object' && !Array.isArray(current.facts) ? { ...current.facts } : {};
    if (Number(facts.quote_draft?.expires_at ?? 0) <= now) delete facts.quote_draft;
    const quoteActive = Number(facts.quote_expires_at ?? 0) > now;
    const fulfillmentNeedsImage = ['paid_manual_delivery', 'ticket_issued', 'fulfillment_exception'].includes(String(facts.stage ?? ''));
    const confirmationFresh = Number(facts.confirmation_image_recorded_at ?? 0) > 0
      && now - Number(facts.confirmation_image_recorded_at) <= MEMORY_MS;
    if (!quoteActive && !fulfillmentNeedsImage && !confirmationFresh) {
      delete facts.confirmation_image_url;
      delete facts.confirmation_image_recorded_at;
    }
    if (!facts.confirmation_image_url) {
      delete facts.confirmation_image_recorded_at;
      delete facts.purchase_info_confirmed_image_url;
      delete facts.purchase_info_confirmed_at;
    }
    if (!current.messages.length) delete facts.buyer_name;
    current.facts = facts;
  }
  return state;
}

function conversationKey(tenantId, payload) {
  return [tenantId, payload?.accountUnb, payload?.chatId, payload?.peerUnb].map((v) => String(v ?? '')).join(':');
}

function operationItem(key, value) {
  const [, account_unb, chat_id, peer_unb] = key.split(':');
  const facts = value?.facts ?? {};
  const messages = Array.isArray(value?.messages) ? value.messages : [];
  const last = messages.at(-1) ?? {};
  const stage = operationalStage(facts);
  return {
    account_unb,
    chat_id,
    peer_unb,
    buyer_label: buyerNameFromFacts(facts, peer_unb),
    stage,
    order_id: String(facts.order_id ?? ''),
    cinema: String(facts.cinema ?? '').slice(0, 160),
    quote_scope: ['area_probe', 'exact_seats'].includes(String(facts.quote_scope)) ? String(facts.quote_scope) : '',
    quote_unit_cents: positiveCentsOrNull(facts.quote_unit_cents),
    quote_total_cents: positiveCentsOrNull(facts.quote_total_cents),
    quote_ticket_count: positiveCentsOrNull(facts.quote_ticket_count),
    quote_expires_at: Number(facts.quote_expires_at ?? 0) || null,
    seats: Array.isArray(facts.seats) ? facts.seats.map(String).slice(0, 20) : [],
    last_message: String(last.text ?? '').slice(0, 200),
    updated_at: Number(last.at ?? 0) || null,
    next_action: nextActionForStage(stage),
  };
}

function operationalStage(facts = {}) {
  const stage = String(facts.stage ?? '');
  const hasRecordedQuote = positiveCentsOrNull(facts.quote_total_cents)
    && positiveCentsOrNull(facts.quote_ticket_count)
    && Number(facts.quote_expires_at) > 0;
  if (stage === 'exception_review' && facts.exception_reason === 'paid_quote_unconfirmed_or_expired' && !hasRecordedQuote) {
    return 'paid_unmanaged';
  }
  return stage;
}

function nextActionForStage(stage) {
  return ({
    quoted: '等待买家确认报价', quote_confirmed: '等待买家提交订单', waiting_payment: '等待买家付款',
    paid_manual_delivery: '人工出票并发送票务信息', ticket_issued: '人工发送票码后回写', ticket_sent: '人工出票已完成', fulfillment_exception: '人工复核出票异常', aftersale: '人工处理售后',
    exception_review: '核对异常并转人工处理', paid_unmanaged: '非本插件报价订单，无需自动处理', quote_expired: '请重新核验实时价格',
  })[stage] ?? '人工核对会话状态';
}

function normalizeQuoteRecord(input) {
  const quoteTotal = positiveCentsOrNull(input?.quote_total_cents);
  const ticketCount = positiveCentsOrNull(input?.quote_ticket_count);
  if (!quoteTotal || !ticketCount) return null;
  const date = /^\d{4}-\d{2}-\d{2}$/u.test(String(input?.date ?? '')) ? String(input.date) : '';
  const showtime = /^\d{2}:\d{2}(?:-\d{2}:\d{2})?$/u.test(String(input?.showtime ?? '')) ? String(input.showtime) : '';
  return {
    id: boundedText(input.id, 80),
    quoted_at: Number(input.quoted_at) || 0,
    expires_at: Number(input.expires_at) || 0,
    stage: boundedText(input.stage, 40) || 'quoted',
    cinema: boundedText(input.cinema, 160),
    movie: boundedText(input.movie, 160),
    date,
    showtime,
    hall: boundedText(input.hall, 80),
    quote_scope: ['area_probe', 'exact_seats'].includes(String(input.quote_scope)) ? String(input.quote_scope) : '',
    quote_unit_cents: positiveCentsOrNull(input.quote_unit_cents),
    quote_total_cents: quoteTotal,
    quote_ticket_count: ticketCount,
    member_cost_total_cents: positiveCentsOrNull(input.member_cost_total_cents),
    original_price_total_cents: positiveCentsOrNull(input.original_price_total_cents),
    channel_fee_total_cents: nonnegativeCentsOrNull(input.channel_fee_total_cents),
    pricing_source: boundedText(input.pricing_source, 80),
    ...(/^[a-f0-9]{32}$/u.test(String(input.pricing_account_ref ?? '')) ? { pricing_account_ref: String(input.pricing_account_ref) } : {}),
  };
}

function updateLatestQuoteRecord(facts, patch) {
  const history = Array.isArray(facts?.quote_history) ? facts.quote_history.slice(-100) : [];
  if (!history.length) return facts;
  const targetId = String(facts.quote_record_id ?? '');
  const index = targetId ? history.findLastIndex((item) => item?.id === targetId) : history.length - 1;
  if (index < 0) return facts;
  history[index] = { ...history[index], ...patch };
  return { ...facts, quote_history: history };
}

function screeningKey(record) {
  return [record?.cinema, record?.movie, record?.date, record?.showtime, record?.hall]
    .map((value) => boundedText(value, 160).toLocaleLowerCase('zh-CN'))
    .join('\u001f');
}

function percentage(numerator, denominator) {
  return denominator > 0 ? Number(((numerator / denominator) * 100).toFixed(1)) : null;
}

function positiveCentsOrNull(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

function nonnegativeCentsOrNull(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

function buyerDisplayName(payload) {
  const name = [
    payload?.peerNick, payload?.peerNickname, payload?.peer_nick,
    payload?.buyerNick, payload?.buyerNickname, payload?.buyer_nick, payload?.buyerName,
  ].map(normalizeBuyerName).find(Boolean);
  return name ?? '';
}

function buyerNameFromFacts(facts, peerUnb) {
  const name = normalizeBuyerName(facts?.buyer_name);
  return name || `买家 ${String(peerUnb ?? '').trim() || 'ID 未知'}`;
}

function normalizeBuyerName(value) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, 80);
}

function extractFacts(text) {
  const seats = [];
  // Capture repeated full coordinates before the compact-list parser. This
  // keeps “7排9座，7排10座” from treating the second row number as seat 7.
  for (const match of text.matchAll(/(\d{1,2})\s*排\s*(\d{1,3})\s*(?:座|号)/gu)) {
    const seat = `${Number(match[1])}排${Number(match[2])}座`;
    if (!seats.includes(seat)) seats.push(seat);
  }
  const seatListPattern = /(\d{1,2})\s*排\s*((?:\d{1,3}(?:\s*座)?)(?:(?:\s*(?:和|及|、|，|,|至|-|~)\s*|\s+)\d{1,3}(?:\s*座)?)*)/gu;
  for (const match of text.matchAll(seatListPattern)) {
    const row = match[1];
    const seatList = match[2];
    const seatListOffset = Number(match.index) + match[0].indexOf(seatList);
    for (const numberMatch of seatList.matchAll(/\d{1,3}/gu)) {
      const absoluteEnd = seatListOffset + Number(numberMatch.index) + numberMatch[0].length;
      // Do not turn a nearby count, date, time or amount into a seat merely
      // because it follows an otherwise valid row/seat phrase.
      if (/^\s*(?:排|行|张|月|日|时|分|[:：]|元)/u.test(text.slice(absoluteEnd))) continue;
      const seat = `${row}排${Number(numberMatch[0])}座`;
      if (!seats.includes(seat)) seats.push(seat);
    }
  }
  const count = text.match(/(?:一共|共|要|需要)?\s*([1-9]|1\d|20)\s*张/);
  return { ...(seats.length ? { seats } : {}), ...(count ? { ticket_count: Number(count[1]) } : {}) };
}

function activeQuoteDraft(value, now) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return Number(value.expires_at ?? 0) > now ? value : null;
}

function boundedText(value, maximum) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maximum);
}

function normalizeQuoteDraft(input, previous, now) {
  const recognition = input?.recognition && typeof input.recognition === 'object' ? input.recognition : {};
  const sources = input?.fieldSources && typeof input.fieldSources === 'object' ? input.fieldSources : {};
  const candidate = {
    city: boundedText(recognition.city, 80),
    cinema: boundedText(recognition.cinema, 160),
    movie: boundedText(recognition.movie, 160),
    date: /^\d{4}-\d{2}-\d{2}$/u.test(String(recognition.date ?? '')) ? String(recognition.date) : '',
    showtime: /^\d{2}:\d{2}(?:-\d{2}:\d{2})?$/u.test(String(recognition.showtime ?? '')) ? String(recognition.showtime) : '',
    hall: boundedText(recognition.hall, 80),
    image_url: /^https:\/\//iu.test(String(input?.imageUrl ?? '')) ? String(input.imageUrl).slice(0, 2_000) : '',
  };
  const ticketCount = Number(input?.ticketCount);
  const previousFields = previous?.fields && typeof previous.fields === 'object' ? previous.fields : {};
  const field = (name, value, fallback, source, confidence) => {
    const prior = previousFields[name] && typeof previousFields[name] === 'object' ? previousFields[name] : null;
    const actual = value || fallback || prior?.value || '';
    if (actual === '') return null;
    const actualSource = value ? normalizeSource(sources[name], source) : normalizeSource(prior?.source, 'conversation_draft');
    return { value: actual, source: actualSource, confidence: sourceConfidence(name, actualSource, prior?.confidence ?? confidence) };
  };
  const fields = Object.fromEntries([
    ['city', field('city', candidate.city, boundedText(previous.city, 80), 'image_or_text', 0.8)],
    ['cinema', field('cinema', candidate.cinema, boundedText(previous.cinema, 160), 'image_or_text', 0.8)],
    ['movie', field('movie', candidate.movie, boundedText(previous.movie, 160), 'image_or_text', 0.8)],
    ['date', field('date', candidate.date, String(previous.date ?? ''), 'image_or_text', 0.8)],
    ['showtime', field('showtime', candidate.showtime, String(previous.showtime ?? ''), 'image_or_text', 0.8)],
    ['hall', field('hall', candidate.hall, boundedText(previous.hall, 80), 'image_or_text', 0.8)],
    ['ticket_count', field('ticket_count', Number.isInteger(ticketCount) && ticketCount >= 1 && ticketCount <= 20 ? ticketCount : null, Number.isInteger(previous.ticket_count) ? previous.ticket_count : null, 'buyer_text', 0.8)],
  ].filter(([, value]) => value));
  const lastImage = candidate.image_url || boundedText(previous.last_image ?? previous.image_url, 2_000);
  if (!Object.keys(fields).length && !lastImage) return null;
  const recognitionArtifact = normalizeRecognitionArtifact(input?.recognitionArtifact) ?? previous?.recognition_artifact ?? null;
  const draft = {
    fields,
    ...(lastImage ? { last_image: lastImage } : {}),
    ...(recognitionArtifact ? { recognition_artifact: recognitionArtifact } : {}),
    state: input?.state === 'collecting' ? 'collecting' : 'matching',
    updated_at: now,
    expires_at: now + QUOTE_DRAFT_TTL_MS,
  };
  if (previous?.last_attempt_fingerprint && previous.last_attempt_fingerprint === quoteDraftFingerprint(draft)) {
    draft.last_attempt_fingerprint = previous.last_attempt_fingerprint;
    draft.last_attempt_at = Number(previous.last_attempt_at) || now;
  }
  return draft;
}

function normalizeRecognitionArtifact(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.status !== 'recognized') return null;
  function safe(item, depth = 0) {
    if (depth > 4 || item == null) return null;
    if (typeof item === 'string') return boundedText(item, 500);
    if (typeof item === 'boolean' || Number.isFinite(item)) return item;
    if (Array.isArray(item)) return item.slice(0, 50).map((entry) => safe(entry, depth + 1)).filter((entry) => entry !== null);
    if (typeof item === 'object') return Object.fromEntries(Object.entries(item).slice(0, 50)
      .filter(([key]) => !/(?:token|cookie|authorization|secret|api.?key)/iu.test(key))
      .map(([key, entry]) => [boundedText(key, 80), safe(entry, depth + 1)]).filter(([key, entry]) => key && entry !== null));
    return null;
  }
  const candidate = {
    status: 'recognized',
    ...(value.tenant_id ? { tenant_id: boundedText(value.tenant_id, 128) } : {}),
    ...(Number.isInteger(Number(value.ticket_count)) ? { ticket_count: Number(value.ticket_count) } : {}),
    recognition: safe(value.recognition),
    ...(value.field_sources ? { field_sources: safe(value.field_sources) } : {}),
  };
  return candidate.recognition && JSON.stringify(candidate).length <= 5_000 ? candidate : null;
}

function safeHttpsUrl(value, maxLength) {
  const candidate = String(value ?? '').trim().slice(0, maxLength);
  if (!candidate) return '';
  try { const parsed = new URL(candidate); return parsed.protocol === 'https:' ? parsed.toString().slice(0, maxLength) : ''; }
  catch { return ''; }
}

function normalizeSource(value, fallback) {
  return ['ai_text', 'buyer_text', 'image', 'typed_seats', 'conversation_draft', 'image_or_text'].includes(value) ? value : fallback;
}

function sourceConfidence(field, source, fallback) {
  if (source === 'typed_seats') return 0.9;
  if (source === 'ai_text') return ['date', 'showtime'].includes(field) ? 0.95 : 0.9;
  if (source === 'buyer_text' && ['date', 'showtime'].includes(field)) return 1;
  if (source === 'buyer_text' && ['city', 'cinema'].includes(field)) return 0.96;
  if (source === 'buyer_text' && field === 'movie') return 0.82;
  if (source === 'buyer_text' && field === 'hall') return 0.95;
  if (source === 'image') return 0.9;
  return Number.isFinite(Number(fallback)) ? Number(fallback) : 0.8;
}

function quoteDraftFingerprint(draft) {
  const fields = draft?.fields && typeof draft.fields === 'object' ? draft.fields : {};
  const value = (field) => String(fields[field]?.value ?? '').trim();
  return [value('city'), value('cinema'), value('movie'), value('date'), value('showtime'), value('hall'), value('ticket_count'), String(draft?.last_image ?? '').trim()].join('\u001f');
}
