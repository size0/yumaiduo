import { createCipheriv, createDecipheriv, createHash, randomBytes, randomUUID } from 'node:crypto';
import { copyFile, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

function blank() { return { version: 2, events: {}, priceChangeReceipts: {}, actionReports: {}, keywordImageUploads: {}, wandaFulfillments: {}, selfMessages: {} }; }
function key(envelope) { return `${String(envelope.tenantId)}:${String(envelope.id)}`; }
function chatKey(envelope) { const p = envelope.payload ?? {}; return [p.accountUnb ?? p.account_unb, p.chatId ?? p.chat_id, p.peerUnb ?? p.peer_unb].map((value) => String(value ?? '').trim()).join(':'); }
function seal(value, keyBytes) { const iv = randomBytes(12); const cipher = createCipheriv('aes-256-gcm', keyBytes, iv); const encrypted = Buffer.concat([cipher.update(JSON.stringify(value), 'utf8'), cipher.final()]); return { iv: iv.toString('base64'), tag: cipher.getAuthTag().toString('base64'), data: encrypted.toString('base64') }; }
function open(value, keyBytes) { const decipher = createDecipheriv('aes-256-gcm', keyBytes, Buffer.from(value.iv, 'base64')); decipher.setAuthTag(Buffer.from(value.tag, 'base64')); return JSON.parse(Buffer.concat([decipher.update(Buffer.from(value.data, 'base64')), decipher.final()]).toString('utf8')); }
function receiptKey(receipt) { const value = String(receipt?.idempotency_key ?? '').trim(); if (!value) throw new TypeError('price change receipt idempotency_key is required'); return value; }
function keywordImageUploadKey({ tenantId, accountUnb, assetId }) {
  const values = [tenantId, accountUnb, assetId].map((value) => String(value ?? '').trim());
  if (values.some((value) => !value)) throw new TypeError('keyword image upload identity is required');
  return createHash('sha256').update(values.join('\0')).digest('hex');
}
function actionReportKey(eventStoreId, actionId) {
  const event = String(eventStoreId ?? '').trim();
  const action = String(actionId ?? '').trim();
  if (!event || !action) throw new TypeError('action report event and action ids are required');
  return `${event}\u0000${action}`;
}
function fulfillmentKey(tenantId, orderId) {
  const tenant = String(tenantId ?? '').trim();
  const order = String(orderId ?? '').trim();
  if (!tenant || !order) throw new TypeError('fulfillment tenant and order ids are required');
  return `${tenant}:${order}`;
}
function selfMessageKey(messageId) { return createHash('sha256').update(String(messageId)).digest('hex'); }

export class V2EventStore {
  #file; #key; #writes = Promise.resolve();
  constructor(file, encryptionKey) { this.#file = file; this.#key = encryptionKey; }
  async initialize() {
    await mkdir(dirname(this.#file), { recursive: true });
    try { await this.#read(); } catch (error) { if (error.code !== 'ENOENT') throw error; await this.#write(blank()); }
    await this.#mutate((state) => {
      state.selfMessages ??= {};
      const now = new Date().toISOString();
      for (const record of Object.values(state.events)) {
        if (record.status !== 'processing') continue;
        record.status = 'queued';
        record.lease = null;
        record.updatedAt = now;
        record.result = { reason: 'recovered_after_restart' };
      }
      for (const report of Object.values(state.actionReports)) {
        if (report.status === 'sending') report.status = 'pending';
        if (report.status === 'applying_follow_up') report.status = 'follow_up_pending';
        if (!['pending', 'follow_up_pending'].includes(report.status)) continue;
        report.lease = null;
        report.nextAttemptAt = null;
        report.updatedAt = now;
      }
      return null;
    });
  }
  async listSelfMessageIds() {
    const state = await this.#read();
    return Object.values(state.selfMessages ?? {}).map((value) => open(value, this.#key).messageId).filter(Boolean);
  }
  async saveSelfMessage(value) {
    const messageId = String(value?.messageId ?? '').trim();
    if (!messageId) throw new TypeError('self message id is required');
    const record = {
      messageId,
      tenantId: String(value?.tenantId ?? '').trim() || null,
      accountUnb: String(value?.accountUnb ?? '').trim() || null,
      peerUnb: String(value?.peerUnb ?? '').trim() || null,
      chatId: String(value?.chatId ?? '').trim() || null,
      eventId: String(value?.eventId ?? '').trim() || null,
      actionId: String(value?.actionId ?? '').trim() || null,
      savedAt: new Date().toISOString(),
    };
    return this.#mutate((state) => {
      state.selfMessages ??= {};
      state.selfMessages[selfMessageKey(messageId)] = seal(record, this.#key);
      const ids = Object.keys(state.selfMessages);
      for (const oldId of ids.slice(0, Math.max(0, ids.length - 2_000))) delete state.selfMessages[oldId];
      return structuredClone(record);
    });
  }
  async getKeywordImageUpload(identity) {
    const id = keywordImageUploadKey(identity);
    const state = await this.#read();
    const protectedValue = state.keywordImageUploads[id];
    if (!protectedValue) return null;
    const value = open(protectedValue, this.#key);
    if (!value?.expiresAt || Date.parse(value.expiresAt) <= Date.now()) return null;
    return structuredClone(value);
  }
  async getWandaFulfillment(tenantId, orderId) {
    const state = await this.#read();
    const protectedValue = state.wandaFulfillments[fulfillmentKey(tenantId, orderId)];
    return protectedValue ? open(protectedValue, this.#key) : null;
  }
  async saveWandaFulfillment(value) {
    const key = fulfillmentKey(value?.tenant_id, value?.order_id);
    const fingerprint = String(value?.request_fingerprint ?? '').trim();
    if (!fingerprint) throw new TypeError('fulfillment request fingerprint is required');
    return this.#mutate((state) => {
      const existing = state.wandaFulfillments[key];
      if (existing) {
        const current = open(existing, this.#key);
        if (current.request_fingerprint !== fingerprint) throw new Error('wanda_fulfillment_conflict');
        state.wandaFulfillments[key] = seal(structuredClone(value), this.#key);
        return { created: false, record: structuredClone(value) };
      }
      state.wandaFulfillments[key] = seal(structuredClone(value), this.#key);
      return { created: true, record: structuredClone(value) };
    });
  }
  async saveKeywordImageUpload(value) {
    const id = keywordImageUploadKey(value);
    const imageUrl = String(value?.imageUrl ?? '').trim();
    const width = Number(value?.width);
    const height = Number(value?.height);
    const expiresAt = String(value?.expiresAt ?? '').trim();
    if (!/^https:\/\/[^/]*alicdn\.com\//iu.test(imageUrl) || !Number.isFinite(width) || width <= 0 || !Number.isFinite(height) || height <= 0 || !Number.isFinite(Date.parse(expiresAt))) {
      throw new TypeError('keyword image upload value is invalid');
    }
    const copy = {
      tenantId: String(value.tenantId), accountUnb: String(value.accountUnb), assetId: String(value.assetId),
      imageUrl, width: Math.round(width), height: Math.round(height),
      sha256: value.sha256 ? String(value.sha256) : null, expiresAt,
    };
    return this.#mutate((state) => {
      state.keywordImageUploads[id] = seal(copy, this.#key);
      return structuredClone(copy);
    });
  }
  async enqueue(envelope) {
    if (!envelope?.id || !envelope?.tenantId || !envelope?.event) throw new TypeError('event id, tenantId and event are required');
    return this.#mutate((state) => {
      const id = key(envelope); if (state.events[id]) return { created: false, record: external(state.events[id], this.#key) };
      const now = new Date().toISOString();
      state.events[id] = { id, event: seal(envelope, this.#key), sessionKey: chatKey(envelope), status: 'queued', createdAt: now, updatedAt: now, attempts: 0, lease: null, result: null, actions: {} };
      // Preserve every buyer event. Rapid text often carries separate durable
      // facts (cinema, quantity, confirmation); platform history can lag the
      // webhook, so cancelling an older queued text can permanently lose it.
      // Stale outward replies are still suppressed by the send preflight.
      return { created: true, record: external(state.events[id], this.#key) };
    });
  }
  async claim(activeSessions) { return this.#mutate((state) => { const record = Object.values(state.events).find((item) => item.status === 'queued' && !activeSessions.has(item.sessionKey)); if (!record) return null; record.status = 'processing'; record.attempts += 1; record.lease = randomUUID(); record.updatedAt = new Date().toISOString(); return external(record, this.#key); }); }
  async complete(id, lease, result) { return this.#finish(id, lease, 'completed', result); }
  async fail(id, lease, reason) { return this.#finish(id, lease, 'failed', { reason }); }
  async beginAction(eventId, actionId) {
    return this.#mutate((state) => {
      const event = state.events[eventId]; if (!event) throw new Error('event missing for action');
      event.actions ??= {};
      const existing = event.actions[actionId];
      if (existing) return { ...structuredClone(existing), newlyStarted: false };
      const action = { status: 'started', updatedAt: new Date().toISOString(), result: null };
      event.actions[actionId] = action;
      return { ...structuredClone(action), newlyStarted: true };
    });
  }
  async finishAction(eventId, actionId, result) {
    return this.#mutate((state) => {
      const event = state.events[eventId]; if (!event) throw new Error('event missing for action');
      event.actions ??= {};
      event.actions[actionId] = { status: 'finished', updatedAt: new Date().toISOString(), result: structuredClone(result) };
      return structuredClone(event.actions[actionId]);
    });
  }
  async enqueueActionReport({ eventStoreId, sessionKey, tenantId, eventId, actionId, result, mode, baselineMessages = [], baselineAvailable = false }) {
    return this.#mutate((state) => {
      if (!state.events[eventStoreId]) throw new Error('event missing for action report');
      const id = actionReportKey(eventStoreId, actionId);
      const existing = state.actionReports[id];
      if (existing) {
        const previous = externalActionReport(existing, this.#key);
        const resultChanged = JSON.stringify(previous.payload.result) !== JSON.stringify(result);
        if (existing.status === 'delivered' && resultChanged) {
          const now = new Date().toISOString();
          existing.payload = seal({ eventStoreId, tenantId, eventId, actionId, result: structuredClone(result), mode, baselineMessages: structuredClone(baselineMessages), baselineAvailable }, this.#key);
          existing.status = 'pending';
          existing.attempts = 0;
          existing.lease = null;
          existing.nextAttemptAt = null;
          existing.lastError = null;
          existing.response = null;
          existing.updatedAt = now;
          return { created: true, report: externalActionReport(existing, this.#key) };
        }
        return { created: false, report: previous };
      }
      const now = new Date().toISOString();
      const payload = { eventStoreId, tenantId, eventId, actionId, result: structuredClone(result), mode, baselineMessages: structuredClone(baselineMessages), baselineAvailable };
      state.actionReports[id] = {
        id,
        sessionKey: String(sessionKey ?? ''),
        payload: seal(payload, this.#key),
        status: 'pending',
        attempts: 0,
        lease: null,
        nextAttemptAt: null,
        lastError: null,
        response: null,
        createdAt: now,
        updatedAt: now,
      };
      return { created: true, report: externalActionReport(state.actionReports[id], this.#key) };
    });
  }
  async claimActionReport(activeSessions, requestedId = null) {
    return this.#mutate((state) => {
      const now = Date.now();
      const report = Object.values(state.actionReports).find((item) => (
        ['pending', 'follow_up_pending'].includes(item.status)
        && (!requestedId || item.id === requestedId)
        && !activeSessions.has(item.sessionKey)
        && (!item.nextAttemptAt || Date.parse(item.nextAttemptAt) <= now)
      ));
      if (!report) return null;
      report.status = report.status === 'follow_up_pending' ? 'applying_follow_up' : 'sending';
      report.attempts += 1;
      report.lease = randomUUID();
      report.updatedAt = new Date().toISOString();
      return externalActionReport(report, this.#key);
    });
  }
  async completeActionReport(id, lease, response) {
    return this.#mutate((state) => {
      const report = state.actionReports[id];
      if (!report || report.lease !== lease || report.status !== 'sending') throw new Error('action report lease mismatch');
      const copy = structuredClone(response ?? {});
      report.status = Array.isArray(copy.actions) && copy.actions.length > 0 ? 'follow_up_pending' : 'delivered';
      report.lease = null;
      report.nextAttemptAt = null;
      report.lastError = null;
      report.response = seal(copy, this.#key);
      report.updatedAt = new Date().toISOString();
      return externalActionReport(report, this.#key);
    });
  }
  async completeActionReportFollowUp(id, lease) {
    return this.#mutate((state) => {
      const report = state.actionReports[id];
      if (!report || report.lease !== lease || report.status !== 'applying_follow_up') throw new Error('action report follow-up lease mismatch');
      report.status = 'delivered';
      report.lease = null;
      report.nextAttemptAt = null;
      report.lastError = null;
      report.updatedAt = new Date().toISOString();
      return externalActionReport(report, this.#key);
    });
  }
  async deferActionReport(id, lease, reason) {
    return this.#mutate((state) => {
      const report = state.actionReports[id];
      if (!report || report.lease !== lease || report.status !== 'sending') throw new Error('action report lease mismatch');
      const delayMs = Math.min(30_000, 1_000 * (2 ** Math.min(report.attempts - 1, 5)));
      report.status = 'pending';
      report.lease = null;
      report.nextAttemptAt = new Date(Date.now() + delayMs).toISOString();
      report.lastError = String(reason ?? 'action_report_failed').slice(0, 160);
      report.updatedAt = new Date().toISOString();
      return { report: externalActionReport(report, this.#key), delayMs };
    });
  }
  async deferActionReportFollowUp(id, lease, reason) {
    return this.#mutate((state) => {
      const report = state.actionReports[id];
      if (!report || report.lease !== lease || report.status !== 'applying_follow_up') throw new Error('action report follow-up lease mismatch');
      const delayMs = Math.min(30_000, 1_000 * (2 ** Math.min(report.attempts - 1, 5)));
      report.status = 'follow_up_pending';
      report.lease = null;
      report.nextAttemptAt = new Date(Date.now() + delayMs).toISOString();
      report.lastError = String(reason ?? 'action_report_follow_up_failed').slice(0, 160);
      report.updatedAt = new Date().toISOString();
      return { report: externalActionReport(report, this.#key), delayMs };
    });
  }
  async getEvent(id) {
    const state = await this.#read();
    const record = state.events[id];
    return record ? external(record, this.#key) : null;
  }
  async hasNewerSessionEvent(id) {
    const state = await this.#read();
    const records = Object.values(state.events);
    const index = records.findIndex((record) => record.id === id);
    if (index < 0) return false;
    const source = records[index];
    return records.slice(index + 1).some((record) => (
      record.sessionKey === source.sessionKey && record.status !== 'cancelled'
    ));
  }
  async listOrderReferences(tenantId, limit = 100) {
    const tenant = String(tenantId ?? '').trim();
    if (!tenant) return [];
    const boundedLimit = Math.max(1, Math.min(Number(limit) || 100, 500));
    const state = await this.#read();
    const records = Object.values(state.events).sort((left, right) => String(right.updatedAt).localeCompare(String(left.updatedAt)));
    const seen = new Set();
    const result = [];
    for (const record of records) {
      let envelope;
      try { envelope = open(record.event, this.#key); } catch { continue; }
      if (String(envelope?.tenantId ?? '') !== tenant) continue;
      const payload = envelope?.payload ?? {};
      const orderId = String(payload.orderId ?? payload.order_id ?? payload.platformOrderId ?? payload.platform_order_id ?? '').trim();
      if (!orderId || seen.has(orderId)) continue;
      seen.add(orderId);
      result.push({
        orderId,
        observedAt: record.updatedAt,
        event: String(envelope.event ?? ''),
        accountUnb: String(payload.accountUnb ?? payload.account_unb ?? '').trim() || null,
      });
      if (result.length >= boundedLimit) break;
    }
    return result;
  }
  createPriceChangeReceiptStore() {
    return Object.freeze({
      claim: (initialReceipt) => this.#claimPriceChangeReceipt(initialReceipt),
      save: (receipt) => this.#savePriceChangeReceipt(receipt),
    });
  }
  async health() {
    const state = await this.#read();
    const counts = {};
    const actionReportCounts = {};
    for (const event of Object.values(state.events)) counts[event.status] = (counts[event.status] ?? 0) + 1;
    for (const report of Object.values(state.actionReports)) actionReportCounts[report.status] = (actionReportCounts[report.status] ?? 0) + 1;
    return { counts, actionReportCounts };
  }
  async #finish(id, lease, status, result) { return this.#mutate((state) => { const record = state.events[id]; if (!record || record.lease !== lease) throw new Error('event lease mismatch'); record.status = status; record.lease = null; record.result = result; record.updatedAt = new Date().toISOString(); return external(record, this.#key); }); }
  async #claimPriceChangeReceipt(initialReceipt) {
    const idempotencyKey = receiptKey(initialReceipt);
    return this.#mutate((state) => {
      const existing = state.priceChangeReceipts[idempotencyKey];
      if (existing) return { created: false, receipt: open(existing, this.#key) };
      const receipt = structuredClone(initialReceipt);
      state.priceChangeReceipts[idempotencyKey] = seal(receipt, this.#key);
      return { created: true, receipt: structuredClone(receipt) };
    });
  }
  async #savePriceChangeReceipt(receipt) {
    const idempotencyKey = receiptKey(receipt);
    return this.#mutate((state) => {
      if (!state.priceChangeReceipts[idempotencyKey]) throw new Error('price change receipt must be claimed before save');
      const copy = structuredClone(receipt);
      state.priceChangeReceipts[idempotencyKey] = seal(copy, this.#key);
      return structuredClone(copy);
    });
  }
  async #mutate(work) { const next = this.#writes.then(async () => { const state = await this.#read(); const result = work(state); await this.#write(state); return result; }); this.#writes = next.catch(() => undefined); return next; }
  async #read() {
    const state = JSON.parse(await readFile(this.#file, 'utf8'));
    if (state.version !== 2 || !state.events) throw new Error('invalid v2 event store');
    state.priceChangeReceipts ??= {};
    if (!state.priceChangeReceipts || typeof state.priceChangeReceipts !== 'object' || Array.isArray(state.priceChangeReceipts)) throw new Error('invalid v2 price change receipts');
    state.actionReports ??= {};
    if (!state.actionReports || typeof state.actionReports !== 'object' || Array.isArray(state.actionReports)) throw new Error('invalid v2 action reports');
    state.keywordImageUploads ??= {};
    if (!state.keywordImageUploads || typeof state.keywordImageUploads !== 'object' || Array.isArray(state.keywordImageUploads)) throw new Error('invalid v2 keyword image uploads');
    state.wandaFulfillments ??= {};
    if (!state.wandaFulfillments || typeof state.wandaFulfillments !== 'object' || Array.isArray(state.wandaFulfillments)) throw new Error('invalid v2 Wanda fulfillments');
    return state;
  }
  async #write(state) {
    const temporary = `${this.#file}.${process.pid}.${randomUUID()}.tmp`;
    await writeFile(temporary, JSON.stringify(state), { encoding: 'utf8', mode: 0o600 });
    for (let attempt = 0; attempt < 10; attempt += 1) {
      try { await rename(temporary, this.#file); return; }
      catch (error) {
        const retryable = ['EPERM', 'EACCES', 'EBUSY'].includes(error?.code);
        if (!retryable) throw error;
        if (attempt === 9) {
          if (process.platform !== 'win32') throw error;
          // Windows may keep denying replacement of an existing file after all
          // handles are closed. Writes are serialized, so copying is a safe local fallback.
          await copyFile(temporary, this.#file);
          await rm(temporary, { force: true });
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, Math.min(250, 25 * (2 ** attempt))));
      }
    }
  }
}

function external(record, encryptionKey) { return { ...structuredClone(record), envelope: open(record.event, encryptionKey) }; }
function externalActionReport(report, encryptionKey) {
  return {
    ...structuredClone(report),
    payload: open(report.payload, encryptionKey),
    response: report.response ? open(report.response, encryptionKey) : null,
  };
}
