import { randomUUID } from 'node:crypto';
import { mkdir, open, readFile, rename } from 'node:fs/promises';
import { dirname } from 'node:path';
import { decryptText, encryptText } from './secret-box.mjs';

const STORE_VERSION = 1;
const CLAIMABLE = new Set(['queued', 'retry']);

function emptyState() {
  return { version: STORE_VERSION, revision: 0, events: {}, sentMessages: {} };
}

function clone(value) {
  return structuredClone(value);
}

function eventKey(envelope) {
  return `${String(envelope.tenantId)}:${String(envelope.id)}`;
}

function messageId(envelope) {
  if (envelope?.event !== 'im.message.received') return '';
  return String(envelope?.payload?.messageId ?? envelope?.payload?.message_id ?? '').trim();
}

function validateEnvelope(envelope) {
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) {
    throw new TypeError('event envelope must be an object');
  }
  if (!String(envelope.id ?? '').trim()) throw new TypeError('event id is required');
  if (!String(envelope.tenantId ?? '').trim()) throw new TypeError('tenantId is required');
  if (!String(envelope.event ?? '').trim()) throw new TypeError('event name is required');
  if (!Number.isFinite(Number(envelope.ts))) throw new TypeError('event ts is required');
  if (envelope.payload != null && (typeof envelope.payload !== 'object' || Array.isArray(envelope.payload))) {
    throw new TypeError('event payload must be an object');
  }
}

export class FileEventStore {
  #file;
  #encryptionKey;
  #retentionMs;
  #state = null;
  #readPromise = null;
  #writeChain = Promise.resolve();

  constructor(file, { encryptionKey = null, retentionMs = 30 * 24 * 60 * 60 * 1_000 } = {}) {
    this.#file = file;
    this.#encryptionKey = encryptionKey;
    this.#retentionMs = retentionMs;
  }

  async initialize() {
    await mkdir(dirname(this.#file), { recursive: true });
    try {
      this.#state = await this.#readFromDisk();
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
      this.#state = emptyState();
      await this.#atomicWrite(this.#state);
    }
  }

  async enqueue(envelope, { availableAt = Date.now() } = {}) {
    validateEnvelope(envelope);
    return this.#mutate((state) => {
      const key = eventKey(envelope);
      const existing = state.events[key];
      if (existing) return { created: false, event: externalRecord(existing, this.#encryptionKey) };
      const platformMessageId = messageId(envelope);
      if (platformMessageId) {
        const duplicate = Object.values(state.events).find((record) => {
          const stored = externalRecord(record, this.#encryptionKey).envelope;
          return String(stored.tenantId) === String(envelope.tenantId)
            && stored.event === 'im.message.received'
            && messageId(stored) === platformMessageId;
        });
        if (duplicate) return { created: false, event: externalRecord(duplicate, this.#encryptionKey) };
      }
      const now = new Date().toISOString();
      const record = {
        key,
        envelope: sealEnvelope(envelope, this.#encryptionKey),
        status: 'queued',
        attempts: 0,
        availableAt: Number(availableAt),
        leaseId: null,
        leaseUntil: null,
        lastError: null,
        result: null,
        createdAt: now,
        updatedAt: now,
      };
      state.events[key] = record;
      return { created: true, event: externalRecord(record, this.#encryptionKey) };
    });
  }

  async cancelPendingChatMessages(tenantId, chatKey, exceptEventId) {
    const normalizedTenant = String(tenantId ?? '');
    const normalizedChat = String(chatKey ?? '');
    const exceptKey = `${normalizedTenant}:${String(exceptEventId ?? '')}`;
    if (!normalizedTenant || !normalizedChat) return { cancelled: 0 };
    return this.#mutate((state) => {
      let cancelled = 0;
      for (const [key, record] of Object.entries(state.events)) {
        if (key === exceptKey || (!CLAIMABLE.has(record.status) && record.status !== 'processing')) continue;
        const envelope = externalRecord(record, this.#encryptionKey).envelope;
        if (envelope.event !== 'im.message.received') continue;
        if (messageChatKey(envelope.payload ?? {}) !== normalizedChat) continue;
        if (record.status === 'processing') {
          // A first-image receipt may be in flight before the merge window.
          // Preserve its lease, but prevent stale transaction facts from
          // resuming after a newer quote supplement arrives.
          record.supersededByEventId = String(exceptEventId ?? '').slice(0, 180);
          record.updatedAt = new Date().toISOString();
          cancelled += 1;
          continue;
        }
        const actions = Array.isArray(record.result?.actions) ? clone(record.result.actions) : [];
        record.status = 'cancelled';
        record.result = { skipped: 'superseded_by_newer_buyer_message', ...(actions.length ? { actions } : {}) };
        record.updatedAt = new Date().toISOString();
        cancelled += 1;
      }
      return { cancelled };
    });
  }

  async claimDue({ now = Date.now(), leaseMs = 60_000 } = {}) {
    return this.#mutate((state) => {
      const activeChats = new Set(Object.values(state.events)
        .filter((record) => record.status === 'processing' && Number(record.leaseUntil ?? 0) > now)
        .map((record) => externalRecord(record, this.#encryptionKey).envelope)
        .filter((envelope) => envelope.event === 'im.message.received')
        .map((envelope) => messageChatKey(envelope.payload ?? {}))
        .filter(Boolean));
      const candidate = Object.values(state.events)
        .filter((record) => {
          const due = (CLAIMABLE.has(record.status) && Number(record.availableAt) <= now)
            || (record.status === 'processing' && Number(record.leaseUntil ?? 0) <= now);
          if (!due) return false;
          const envelope = externalRecord(record, this.#encryptionKey).envelope;
          return envelope.event !== 'im.message.received' || !activeChats.has(messageChatKey(envelope.payload ?? {}));
        })
        .sort((left, right) => (
          Number(left.availableAt) - Number(right.availableAt)
          || String(left.createdAt).localeCompare(String(right.createdAt))
        ))[0];
      if (!candidate) return null;
      candidate.status = 'processing';
      candidate.leaseId = randomUUID();
      candidate.leaseUntil = now + leaseMs;
      candidate.attempts += 1;
      candidate.updatedAt = new Date().toISOString();
      return externalRecord(candidate, this.#encryptionKey);
    }, { shouldWrite: (result) => result !== null });
  }

  async complete(key, leaseId, result = null) {
    return this.#finishClaim(key, leaseId, (record) => {
      record.status = 'completed';
      record.result = result == null ? null : clone(result);
      record.lastError = null;
    });
  }

  async defer(key, leaseId, { delayMs = 1_000, reason = 'deferred', metadata = null } = {}) {
    return this.#finishClaim(key, leaseId, (record) => {
      const safeMetadata = metadata && typeof metadata === 'object' && !Array.isArray(metadata) ? clone(metadata) : {};
      if (record.supersededByEventId) {
        record.status = 'cancelled';
        record.result = { skipped: 'superseded_by_newer_buyer_message', ...safeMetadata };
      } else {
        record.status = 'queued';
        record.availableAt = Date.now() + Math.max(1_000, Number(delayMs));
        record.result = { deferred: String(reason).slice(0, 80), ...safeMetadata };
      }
      delete record.supersededByEventId;
      record.lastError = null;
    });
  }

  async retry(key, leaseId, error, { delayMs = 5_000, maxAttempts = 8 } = {}) {
    return this.#finishClaim(key, leaseId, (record) => {
      const exhausted = record.attempts >= maxAttempts;
      record.status = exhausted ? 'failed' : 'retry';
      record.availableAt = Date.now() + Math.max(1_000, Number(delayMs));
      record.lastError = safeError(error);
    });
  }

  async markUnknown(key, leaseId, error) {
    return this.#finishClaim(key, leaseId, (record) => {
      record.status = 'unknown';
      record.lastError = safeError(error);
    });
  }

  async fail(key, leaseId, error) {
    return this.#finishClaim(key, leaseId, (record) => {
      record.status = 'failed';
      record.lastError = safeError(error);
    });
  }

  async get(key) {
    const state = await this.#read();
    return state.events[key] ? externalRecord(state.events[key], this.#encryptionKey) : null;
  }

  async getMany(keys) {
    if (!Array.isArray(keys)) throw new TypeError('event keys must be an array');
    const selected = [...new Set(keys.map((key) => String(key ?? '').trim()).filter(Boolean))].slice(0, 500);
    const state = await this.#read();
    return selected.map((key) => state.events[key] ? externalRecord(state.events[key], this.#encryptionKey) : null).filter(Boolean);
  }

  async list({ status, limit = 100 } = {}) {
    const state = await this.#read();
    return Object.values(state.events)
      .filter((record) => !status || record.status === status)
      .sort((left, right) => String(right.updatedAt).localeCompare(String(left.updatedAt)))
      .slice(0, Math.max(1, Math.min(500, Number(limit))))
      .map((record) => externalRecord(record, this.#encryptionKey));
  }

  async health() {
    const state = await this.#read();
    const counts = {};
    for (const event of Object.values(state.events)) {
      counts[event.status] = (counts[event.status] ?? 0) + 1;
    }
    return { revision: state.revision, eventCounts: counts };
  }

  async recordSentMessage(tenantId, chatId, messageId) {
    const id = String(messageId ?? '').trim();
    if (!id) return;
    await this.#mutate((state) => {
      const key = `${String(tenantId)}:${String(chatId)}:${id}`;
      state.sentMessages[key] = new Date().toISOString();
      const entries = Object.entries(state.sentMessages);
      if (entries.length > 5_000) {
        entries
          .sort((left, right) => String(left[1]).localeCompare(String(right[1])))
          .slice(0, entries.length - 5_000)
          .forEach(([oldKey]) => delete state.sentMessages[oldKey]);
      }
    });
  }

  async wasSentMessage(tenantId, chatId, messageId) {
    const state = await this.#read();
    const key = `${String(tenantId)}:${String(chatId)}:${String(messageId ?? '')}`;
    return Boolean(state.sentMessages[key]);
  }

  async #finishClaim(key, leaseId, update) {
    return this.#mutate((state) => {
      const record = state.events[key];
      if (!record) throw new Error(`event not found: ${key}`);
      if (record.status !== 'processing' || record.leaseId !== leaseId) {
        throw new Error(`event lease mismatch: ${key}`);
      }
      update(record);
      delete record.supersededByEventId;
      record.leaseId = null;
      record.leaseUntil = null;
      record.updatedAt = new Date().toISOString();
      return externalRecord(record, this.#encryptionKey);
    });
  }

  async #mutate(operation, { shouldWrite = () => true } = {}) {
    const pending = this.#writeChain.then(async () => {
      const state = await this.#read().catch((error) => {
        if (error?.code === 'ENOENT') return emptyState();
        throw error;
      });
      const result = operation(state);
      if (!shouldWrite(result)) return result;
      pruneExpired(state, this.#retentionMs);
      state.revision += 1;
      await this.#atomicWrite(state);
      return result;
    });
    this.#writeChain = pending.catch(() => { this.#state = null; });
    return pending;
  }

  async #read() {
    if (this.#state) return this.#state;
    // Coalesce concurrent cold reads. Without this guard, a diagnostic or UI
    // request issuing many get() calls before initialize() completes can read
    // and parse the entire encrypted event file once per key, exhausting a
    // small production host.
    this.#readPromise ??= this.#readFromDisk()
      .then((state) => { this.#state = state; return state; })
      .finally(() => { this.#readPromise = null; });
    return this.#readPromise;
  }

  async #readFromDisk() {
    const raw = await readFile(this.#file, 'utf8');
    const state = JSON.parse(raw);
    if (state?.version !== STORE_VERSION || !state.events || typeof state.events !== 'object') {
      throw new Error('unsupported or corrupt event store');
    }
    state.sentMessages ??= {};
    return state;
  }

  async #atomicWrite(state) {
    const temporary = `${this.#file}.${process.pid}.${randomUUID()}.tmp`;
    const handle = await open(temporary, 'wx', 0o600);
    try {
      await handle.writeFile(`${JSON.stringify(state)}\n`, 'utf8');
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(temporary, this.#file);
  }
}

function safeError(error) {
  const value = String(error?.message ?? error ?? 'unknown error');
  return value
    .replace(/Bearer\s+[^\s]+/giu, 'Bearer [REDACTED]')
    .replace(/(?:sk|yp|pdk)_[A-Za-z0-9._-]+/gu, '[REDACTED]')
    .slice(0, 1_000);
}

function sealEnvelope(envelope, encryptionKey) {
  const result = clone(envelope);
  if (!encryptionKey) return result;
  result.payloadEncrypted = encryptText(JSON.stringify(result.payload ?? {}), encryptionKey);
  delete result.payload;
  return result;
}

function externalRecord(record, encryptionKey) {
  const result = clone(record);
  if (!result.envelope?.payloadEncrypted) return result;
  if (!encryptionKey) throw new Error('CONFIG_ENCRYPTION_KEY is required to read encrypted events');
  result.envelope.payload = JSON.parse(decryptText(result.envelope.payloadEncrypted, encryptionKey));
  delete result.envelope.payloadEncrypted;
  return result;
}

function pruneExpired(state, retentionMs) {
  const cutoff = Date.now() - retentionMs;
  const terminal = new Set(['completed', 'failed', 'unknown', 'cancelled']);
  for (const [key, record] of Object.entries(state.events)) {
    if (terminal.has(record.status) && Date.parse(record.updatedAt) < cutoff) delete state.events[key];
  }
  for (const [key, timestamp] of Object.entries(state.sentMessages)) {
    if (Date.parse(timestamp) < cutoff) delete state.sentMessages[key];
  }
}

function messageChatKey(payload = {}) {
  return [
    payload.accountUnb ?? payload.account_unb ?? '',
    payload.chatId ?? payload.chat_id ?? '',
    payload.peerUnb ?? payload.peer_unb ?? '',
  ].map((value) => String(value).trim()).join(':');
}
