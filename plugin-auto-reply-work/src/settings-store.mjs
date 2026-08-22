import { randomUUID } from 'node:crypto';
import { mkdir, open, readFile, rename } from 'node:fs/promises';
import { dirname } from 'node:path';
import { decryptText, encryptText } from './secret-box.mjs';

const STORE_VERSION = 1;

export const DEFAULT_SETTINGS = Object.freeze({
  automation_enabled: true,
  recognition_enabled: true,
  quote_enabled: true,
  price_change_enabled: true,
  ai_reply_enabled: true,
  minimum_confidence: 0.9,
  ai_base_url: 'https://api.openai.com',
  ai_model: 'gpt-4.1-mini',
});

function emptyState() {
  return { version: STORE_VERSION, revision: 0, tenants: {} };
}

function tenantKey(value) {
  const key = String(value ?? '').trim();
  if (!key || key.length > 128) throw new TypeError('tenantId is invalid');
  return key;
}

function boolean(value, name) {
  if (typeof value !== 'boolean') throw new TypeError(`${name} must be a boolean`);
  return value;
}

function modelName(value) {
  const result = String(value ?? '').trim();
  if (!result || result.length > 120 || !/^[A-Za-z0-9._:/-]+$/u.test(result)) {
    throw new TypeError('ai_model is invalid');
  }
  return result;
}

function aiBaseUrl(value) {
  const result = new URL(String(value ?? '').trim());
  if (result.username || result.password || result.search || result.hash) {
    throw new TypeError('ai_base_url cannot contain credentials, query, or fragment');
  }
  if (result.protocol !== 'https:' && !['localhost', '127.0.0.1', '::1'].includes(result.hostname)) {
    throw new TypeError('ai_base_url must use HTTPS outside localhost');
  }
  return result.toString().replace(/\/$/u, '');
}

function storedSettings(record = {}) {
  const settings = Object.fromEntries(
    Object.keys(DEFAULT_SETTINGS).map((name) => [name, record[name] ?? DEFAULT_SETTINGS[name]]),
  );
  const encryptedKey = record.ai_api_key;
  if (
    encryptedKey?.version === 1
    && typeof encryptedKey.iv === 'string'
    && typeof encryptedKey.tag === 'string'
    && typeof encryptedKey.ciphertext === 'string'
  ) {
    settings.ai_api_key = encryptedKey;
  }
  if (typeof record.updated_at === 'string' && record.updated_at) settings.updated_at = record.updated_at;
  return settings;
}

function publicSettings(record) {
  const settings = storedSettings(record);
  const encryptedKey = settings.ai_api_key;
  delete settings.ai_api_key;
  return Object.freeze({
    ...settings,
    ai_key_configured: Boolean(encryptedKey),
  });
}

export class FileSettingsStore {
  #file;
  #encryptionKey;
  #writeChain = Promise.resolve();

  constructor(file, { encryptionKey = null } = {}) {
    this.#file = file;
    this.#encryptionKey = encryptionKey;
  }

  async initialize() {
    await mkdir(dirname(this.#file), { recursive: true });
    try {
      await this.#read();
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
      await this.#atomicWrite(emptyState());
    }
  }

  async get(tenantId) {
    const state = await this.#read();
    return publicSettings(state.tenants[tenantKey(tenantId)]);
  }

  async update(tenantId, patch) {
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) {
      throw new TypeError('settings patch must be an object');
    }
    const key = tenantKey(tenantId);
    return this.#mutate((state) => {
      const next = storedSettings(state.tenants[key]);
      for (const name of [
        'automation_enabled',
        'recognition_enabled',
        'quote_enabled',
        'price_change_enabled',
        'ai_reply_enabled',
      ]) {
        if (Object.hasOwn(patch, name)) next[name] = boolean(patch[name], name);
      }
      if (Object.hasOwn(patch, 'minimum_confidence')) {
        const confidence = Number(patch.minimum_confidence);
        if (!Number.isFinite(confidence) || confidence < 0.5 || confidence > 1) {
          throw new TypeError('minimum_confidence must be between 0.5 and 1');
        }
        next.minimum_confidence = confidence;
      }
      if (Object.hasOwn(patch, 'ai_base_url')) next.ai_base_url = aiBaseUrl(patch.ai_base_url);
      if (Object.hasOwn(patch, 'ai_model')) next.ai_model = modelName(patch.ai_model);
      if (patch.clear_ai_api_key === true) delete next.ai_api_key;
      if (Object.hasOwn(patch, 'ai_api_key')) {
        const apiKey = String(patch.ai_api_key ?? '').trim();
        if (!apiKey || apiKey.length > 4_096) throw new TypeError('ai_api_key is invalid');
        if (!this.#encryptionKey) throw new Error('CONFIG_ENCRYPTION_KEY is required before saving merchant API keys');
        next.ai_api_key = encryptText(apiKey, this.#encryptionKey);
      }
      next.updated_at = new Date().toISOString();
      state.tenants[key] = next;
      return publicSettings(next);
    });
  }

  async getAiCredentials(tenantId) {
    const state = await this.#read();
    const record = storedSettings(state.tenants[tenantKey(tenantId)]);
    return Object.freeze({
      baseUrl: record.ai_base_url ?? DEFAULT_SETTINGS.ai_base_url,
      model: record.ai_model ?? DEFAULT_SETTINGS.ai_model,
      apiKey: record.ai_api_key ? decryptText(record.ai_api_key, this.#encryptionKey) : null,
    });
  }

  async #mutate(operation) {
    const pending = this.#writeChain.then(async () => {
      const state = await this.#read();
      const result = operation(state);
      state.revision += 1;
      await this.#atomicWrite(state);
      return result;
    });
    this.#writeChain = pending.catch(() => undefined);
    return pending;
  }

  async #read() {
    const state = JSON.parse(await readFile(this.#file, 'utf8'));
    if (state?.version !== STORE_VERSION || !state.tenants || typeof state.tenants !== 'object') {
      throw new Error('unsupported or corrupt settings store');
    }
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
