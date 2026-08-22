import { randomUUID } from 'node:crypto';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const STORE_VERSION = 1;
function emptyState() { return { version: STORE_VERSION, revision: 0, entries: {} }; }
function bounded(value, length) { return String(value ?? '').trim().slice(0, length); }
function clone(value) { return structuredClone(value); }
function positiveInteger(value) { const number = Number(value); return Number.isSafeInteger(number) && number > 0 ? number : null; }
function nonnegativeInteger(value) { const number = Number(value); return Number.isSafeInteger(number) && number >= 0 ? number : null; }

function boundedHttpsUrl(value) {
  const candidate = bounded(value, 2_000);
  if (!candidate) return '';
  try { const url = new URL(candidate); return url.protocol === 'https:' ? url.toString().slice(0, 2_000) : ''; }
  catch { return ''; }
}

function quoteDelivery(value) {
  if (value == null) return null;
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.type !== 'quote') throw new TypeError('invalid outbox delivery metadata');
  const total = positiveInteger(value.total_quote_cents); const count = positiveInteger(value.ticket_count);
  const pricingRuleVersion = bounded(value.pricing_rule_version, 80);
  if (!total || !count || !pricingRuleVersion) throw new TypeError('quote delivery requires total, count, and pricing rule version');
  return {
    type: 'quote', unit_quote_cents: positiveInteger(value.unit_quote_cents), total_quote_cents: total, ticket_count: count,
    pricing_rule_version: pricingRuleVersion, cinema: bounded(value.cinema, 160), movie: bounded(value.movie, 160), date: bounded(value.date, 32),
    showtime: bounded(value.showtime, 32), hall: bounded(value.hall, 80), quote_scope: ['area_probe', 'exact_seats'].includes(String(value.quote_scope)) ? String(value.quote_scope) : '',
    member_cost_total_cents: positiveInteger(value.member_cost_total_cents), original_price_total_cents: positiveInteger(value.original_price_total_cents), channel_fee_total_cents: nonnegativeInteger(value.channel_fee_total_cents), pricing_source: bounded(value.pricing_source, 100),
    circled_delivery_image_url: boundedHttpsUrl(value.circled_delivery_image_url),
  };
}

function external(entry) {
  return Object.freeze({
    action_id: entry.actionId, run_id: entry.runId, tenant_id: entry.tenantId, mode: entry.mode,
    account_unb: entry.accountUnb, chat_id: entry.chatId, peer_unb: entry.peerUnb, text: entry.text,
    status: entry.status, attempts: entry.attempts, available_at: entry.availableAt,
    lease_id: entry.leaseId, lease_until: entry.leaseUntil,
    platform_message_id: entry.platformMessageId, delivery: clone(entry.delivery ?? null), last_error: entry.lastError,
    created_at: entry.createdAt, updated_at: entry.updatedAt,
  });
}

export class AgentReplyOutboxStore {
  #file; #now; #state = null; #chain = Promise.resolve();
  constructor(file, { now = () => Date.now() } = {}) { this.#file = file; this.#now = now; }

  async initialize() {
    await mkdir(dirname(this.#file), { recursive: true });
    try { this.#state = await this.#readDisk(); }
    catch (error) { if (error?.code !== 'ENOENT') throw error; this.#state = emptyState(); await this.#write(this.#state); }
  }

  async enqueue(input) {
    const actionId = bounded(input?.actionId, 300); const runId = bounded(input?.runId, 240); const tenantId = bounded(input?.tenantId, 128);
    const accountUnb = bounded(input?.accountUnb, 128); const chatId = bounded(input?.chatId, 128); const peerUnb = bounded(input?.peerUnb, 128);
    const text = bounded(input?.text, 1_000); const mode = bounded(input?.mode, 16); const delivery = quoteDelivery(input?.delivery);
    if (mode !== 'active') throw new TypeError('reply outbox accepts only active agent runs');
    if (!actionId || !runId || !tenantId || !accountUnb || !chatId || !peerUnb || !text) throw new TypeError('invalid agent reply outbox entry');
    return this.#mutate((state) => {
      const existing = state.entries[actionId];
      if (existing) return { created: false, entry: external(existing) };
      const now = this.#now(); const timestamp = new Date(now).toISOString();
      const entry = { actionId, runId, tenantId, mode, accountUnb, chatId, peerUnb, text, delivery, status: 'pending', attempts: 0, availableAt: now, leaseId: null, leaseUntil: null, platformMessageId: null, lastError: null, createdAt: timestamp, updatedAt: timestamp };
      state.entries[actionId] = entry;
      return { created: true, entry: external(entry) };
    });
  }

  async claimDue({ leaseMs = 30_000 } = {}) {
    const now = this.#now();
    const result = await this.#mutate((state) => {
      let changed = false;
      for (const entry of Object.values(state.entries)) {
        if (entry.status === 'sending' && Number(entry.leaseUntil) <= now) {
          entry.status = 'unknown'; entry.leaseId = null; entry.leaseUntil = null; entry.lastError = 'send_result_unknown'; entry.updatedAt = new Date(now).toISOString(); changed = true;
        } else if (entry.status === 'committing' && Number(entry.leaseUntil) <= now) {
          entry.status = 'sent_pending_commit'; entry.availableAt = now; entry.leaseId = null; entry.leaseUntil = null; entry.lastError = 'delivery_commit_lease_expired'; entry.updatedAt = new Date(now).toISOString(); changed = true;
        }
      }
      const candidate = Object.values(state.entries).filter((entry) => ['pending', 'sent_pending_commit'].includes(entry.status) && entry.availableAt <= now).sort((a, b) => a.availableAt - b.availableAt || a.createdAt.localeCompare(b.createdAt))[0];
      if (!candidate) return changed ? { noClaim: true } : null;
      candidate.status = candidate.status === 'sent_pending_commit' ? 'committing' : 'sending'; candidate.attempts += 1; candidate.leaseId = randomUUID(); candidate.leaseUntil = now + Math.max(10_000, Number(leaseMs)); candidate.updatedAt = new Date(now).toISOString();
      return external(candidate);
    }, (value) => value !== null);
    return result?.noClaim === true ? null : result;
  }

  async markSent(actionIdValue, leaseId, platformMessageIdValue) {
    const actionId = bounded(actionIdValue, 300); const platformMessageId = bounded(platformMessageIdValue, 240);
    if (!platformMessageId) throw new TypeError('platform message id is required');
    return this.#finish(actionId, leaseId, (entry) => { entry.status = entry.delivery?.type === 'quote' ? 'sent_pending_commit' : 'sent'; entry.platformMessageId = platformMessageId; entry.availableAt = this.#now(); entry.lastError = null; }, 'sending');
  }

  async markCommitted(actionIdValue, leaseId) {
    return this.#finish(bounded(actionIdValue, 300), leaseId, (entry) => { entry.status = 'sent'; entry.lastError = null; }, 'committing');
  }

  async deferCommit(actionIdValue, leaseId, reason = 'delivery_commit_failed', { delayMs = 5_000 } = {}) {
    return this.#finish(bounded(actionIdValue, 300), leaseId, (entry) => { entry.status = 'sent_pending_commit'; entry.availableAt = this.#now() + Math.max(1_000, Number(delayMs)); entry.lastError = bounded(reason, 100) || 'delivery_commit_failed'; }, 'committing');
  }

  async markUnknown(actionIdValue, leaseId, reason = 'send_result_unknown') {
    return this.#finish(bounded(actionIdValue, 300), leaseId, (entry) => { entry.status = 'unknown'; entry.lastError = bounded(reason, 100) || 'send_result_unknown'; });
  }

  async markSkipped(actionIdValue, leaseId, reason = 'safely_skipped') {
    return this.#finish(bounded(actionIdValue, 300), leaseId, (entry) => { entry.status = 'skipped'; entry.lastError = bounded(reason, 100) || 'safely_skipped'; });
  }

  async get(actionId) { const state = await this.#read(); return state.entries[String(actionId)] ? external(state.entries[String(actionId)]) : null; }
  async list({ tenantId, status, limit = 100 } = {}) {
    const state = await this.#read();
    return Object.values(state.entries).filter((entry) => (!tenantId || entry.tenantId === String(tenantId)) && (!status || entry.status === status)).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, Math.max(1, Math.min(500, Number(limit)))).map(external);
  }
  async health() { const state = await this.#read(); const counts = {}; for (const entry of Object.values(state.entries)) counts[entry.status] = (counts[entry.status] ?? 0) + 1; return { revision: state.revision, counts }; }

  async #finish(actionId, leaseId, update, expectedStatus = 'sending') {
    return this.#mutate((state) => {
      const entry = state.entries[actionId];
      if (!entry || entry.status !== expectedStatus || entry.leaseId !== leaseId) throw new Error(`agent reply outbox lease mismatch: ${actionId}`);
      update(entry); entry.leaseId = null; entry.leaseUntil = null; entry.updatedAt = new Date(this.#now()).toISOString(); return external(entry);
    });
  }
  async #mutate(operation, shouldWrite = () => true) {
    const pending = this.#chain.then(async () => { const state = await this.#read(); const result = operation(state); if (!shouldWrite(result)) return result; state.revision += 1; await this.#write(state); return result; });
    this.#chain = pending.catch(() => { this.#state = null; }); return pending;
  }
  async #read() { this.#state ??= await this.#readDisk(); return this.#state; }
  async #readDisk() { const state = JSON.parse(await readFile(this.#file, 'utf8')); if (state?.version !== STORE_VERSION || !state.entries || typeof state.entries !== 'object') throw new Error('unsupported or corrupt agent reply outbox'); return state; }
  async #write(state) { const temporary = `${this.#file}.tmp`; await writeFile(temporary, JSON.stringify(state), { encoding: 'utf8', mode: 0o600 }); await rename(temporary, this.#file); this.#state = state; }
}
