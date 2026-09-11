import { createCipheriv, createDecipheriv, randomBytes, randomUUID } from 'node:crypto';
import { mkdir, readdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const JOB_ID = /^[a-f0-9-]{36}$/u;
const ACTIVE_STATUSES = new Set(['queued', 'processing']);
const RETENTION_MS = 60_000;
const PROCESSING_TTL_MS = 15 * 60_000;

function seal(value, key) {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  const encrypted = Buffer.concat([cipher.update(JSON.stringify(value), 'utf8'), cipher.final()]);
  return {
    iv: iv.toString('base64'),
    tag: cipher.getAuthTag().toString('base64'),
    data: encrypted.toString('base64'),
  };
}

function open(value, key) {
  const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(value.iv, 'base64'));
  decipher.setAuthTag(Buffer.from(value.tag, 'base64'));
  return JSON.parse(Buffer.concat([
    decipher.update(Buffer.from(value.data, 'base64')),
    decipher.final(),
  ]).toString('utf8'));
}

function jobPath(directory, id) { return join(directory, `${id}.json`); }
function nowIso() { return new Date().toISOString(); }
function expired(record, now = Date.now()) { return Date.parse(record.expiresAt) <= now; }

export class V4UiJobStore {
  #directory;
  #key;
  #maxJobs;
  #writes = Promise.resolve();

  constructor(directory, encryptionKey, { maxJobs = 64 } = {}) {
    this.#directory = directory;
    this.#key = encryptionKey;
    this.#maxJobs = maxJobs;
  }

  async initialize() {
    await mkdir(this.#directory, { recursive: true });
    await this.#mutate(async () => {
      const records = await this.#records();
      const now = Date.now();
      for (const record of records) {
        if (expired(record, now)) {
          await rm(jobPath(this.#directory, record.id), { force: true });
          continue;
        }
        if (record.status === 'processing') {
          record.status = 'queued';
          record.lease = null;
          record.expiresAt = new Date(now + PROCESSING_TTL_MS).toISOString();
          record.updatedAt = nowIso();
          await this.#write(record);
        }
      }
    });
  }

  async enqueue({ tenantId, userId, rawBody, contentType }) {
    const tenant = String(tenantId ?? '').trim();
    const user = String(userId ?? '').trim();
    if (!tenant || !user || typeof rawBody !== 'string') throw new TypeError('ui_job_identity_or_body_invalid');
    return this.#mutate(async () => {
      const records = await this.#records();
      for (const record of records) if (expired(record)) await rm(jobPath(this.#directory, record.id), { force: true });
      const activeCount = records.filter((record) => ACTIVE_STATUSES.has(record.status) && !expired(record)).length;
      if (activeCount >= this.#maxJobs) throw Object.assign(new Error('too_many_v4_jobs'), { status: 503 });
      const id = randomUUID();
      const now = Date.now();
      const record = {
        id,
        tenantId: tenant,
        userId: user,
        status: 'queued',
        attempts: 0,
        lease: null,
        request: { rawBody, contentType: String(contentType ?? '') },
        result: null,
        createdAt: new Date(now).toISOString(),
        updatedAt: new Date(now).toISOString(),
        expiresAt: new Date(now + PROCESSING_TTL_MS).toISOString(),
      };
      await this.#write(record, true);
      return { id, status: record.status };
    });
  }

  async claim() {
    return this.#mutate(async () => {
      const records = (await this.#records()).sort((left, right) => left.createdAt.localeCompare(right.createdAt));
      for (const record of records) {
        if (expired(record)) {
          await rm(jobPath(this.#directory, record.id), { force: true });
          continue;
        }
        if (record.status !== 'queued') continue;
        record.status = 'processing';
        record.attempts += 1;
        record.lease = randomUUID();
        record.updatedAt = nowIso();
        await this.#write(record);
        return {
          id: record.id,
          tenantId: record.tenantId,
          userId: record.userId,
          rawBody: record.request.rawBody,
          contentType: record.request.contentType,
          attempts: record.attempts,
          lease: record.lease,
        };
      }
      return null;
    });
  }

  async complete(id, lease, result) {
    return this.#finish(id, lease, {
      status: 'completed',
      result: {
        status: Number(result?.status) || 502,
        contentType: String(result?.contentType ?? 'application/octet-stream'),
        bodyBase64: Buffer.from(result?.body ?? []).toString('base64'),
      },
    });
  }

  async fail(id, lease) {
    return this.#finish(id, lease, {
      status: 'completed',
      result: {
        status: 503,
        contentType: 'application/json; charset=utf-8',
        bodyBase64: Buffer.from(JSON.stringify({ ok: false, error: 'temporarily_unavailable' })).toString('base64'),
      },
    });
  }

  async get(id, { tenantId, userId } = {}) {
    if (!JOB_ID.test(String(id ?? ''))) return null;
    const record = await this.#read(String(id));
    if (!record || record.tenantId !== String(tenantId ?? '') || record.userId !== String(userId ?? '') || expired(record)) return null;
    if (record.status !== 'completed' || !record.result) return { id: record.id, status: 'processing' };
    return { id: record.id, status: 'completed', result: {
      status: record.result.status,
      contentType: record.result.contentType,
      body: Buffer.from(record.result.bodyBase64, 'base64'),
    } };
  }

  async #finish(id, lease, update) {
    return this.#mutate(async () => {
      const record = await this.#read(id);
      if (!record || record.status !== 'processing' || record.lease !== lease) throw new Error('ui_job_lease_mismatch');
      Object.assign(record, update, { lease: null, updatedAt: nowIso(), expiresAt: new Date(Date.now() + RETENTION_MS).toISOString() });
      await this.#write(record);
      return { id: record.id, status: record.status };
    });
  }

  async #records() {
    const names = (await readdir(this.#directory)).filter((name) => JOB_ID.test(name.slice(0, -5)) && name.endsWith('.json'));
    return Promise.all(names.map(async (name) => open(JSON.parse(await readFile(join(this.#directory, name), 'utf8')), this.#key)));
  }

  async #read(id) {
    if (!JOB_ID.test(String(id ?? ''))) return null;
    try { return open(JSON.parse(await readFile(jobPath(this.#directory, id), 'utf8')), this.#key); }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }

  async #write(record, exclusive = false) {
    const target = jobPath(this.#directory, record.id);
    const temporary = `${target}.${process.pid}.${randomUUID()}.tmp`;
    await writeFile(temporary, JSON.stringify(seal(record, this.#key)), { encoding: 'utf8', mode: 0o600 });
    if (exclusive) {
      try {
        await rename(temporary, target);
        return;
      } catch (error) {
        await rm(temporary, { force: true });
        throw error;
      }
    }
    await rename(temporary, target);
  }

  async #mutate(work) {
    const next = this.#writes.then(work);
    this.#writes = next.catch(() => undefined);
    return next;
  }
}
