import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const REQUIRED_PERMISSIONS = Object.freeze([
  'order.read',
  'order.write.price_change',
  'account.read',
  'im.session.read',
  'im.message.read',
  'im.message.send',
]);
const REQUIRED_SUBSCRIPTIONS = Object.freeze([
  'im.message.received',
  'order.created',
  'order.price.changed',
  'order.paid',
  'order.closed',
]);
const LOG_LEVELS = new Set(['debug', 'info', 'warn', 'error']);
const SECRET_KEY = /(authorization|cookie|password|secret|token|api[-_]?key|credential)/i;
const TOKEN_TEXT = /\b(?:pdk|yp)_[A-Za-z0-9_-]+\b/g;
const BEARER_TEXT = /\bBearer\s+[^\s,;]+/gi;

export class ConfigurationError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ConfigurationError';
  }
}

function requiredString(value, name) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new ConfigurationError(`${name} is required`);
  }
  return value.trim();
}

function parseInteger(value, name, { min, max, fallback }) {
  const candidate = value === undefined || value === '' ? fallback : Number(value);
  if (!Number.isSafeInteger(candidate) || candidate < min || candidate > max) {
    throw new ConfigurationError(`${name} must be an integer between ${min} and ${max}`);
  }
  return candidate;
}

function parseUrl(value, name) {
  let parsed;
  try {
    parsed = new URL(requiredString(value, name));
  } catch (error) {
    if (error instanceof ConfigurationError) throw error;
    throw new ConfigurationError(`${name} must be a valid HTTP(S) URL`);
  }
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new ConfigurationError(`${name} must be an HTTP(S) URL without embedded credentials`);
  }
  parsed.hash = '';
  parsed.search = '';
  return parsed.toString().replace(/\/$/, '');
}

function parseSecureServiceUrl(value, name) {
  const result = parseUrl(value, name);
  const url = new URL(result);
  if (url.protocol !== 'https:' && !['localhost', '127.0.0.1', '::1'].includes(url.hostname)) {
    throw new ConfigurationError(`${name} must use HTTPS outside localhost`);
  }
  return result;
}

function parsePluginCallbackBaseUrl(value) {
  const result = parseUrl(value, 'PLUGIN_BASE_URL');
  const url = new URL(result);
  if (url.pathname !== '/') {
    throw new ConfigurationError('PLUGIN_BASE_URL must not include a path; the platform appends the manifest webhook path');
  }
  return url.origin;
}

function parsePath(value, name, fallback) {
  const result = value === undefined || value === '' ? fallback : value.trim();
  if (!result.startsWith('/') || result.startsWith('//') || result.includes('?') || result.includes('#')) {
    throw new ConfigurationError(`${name} must be an absolute URL path without query or fragment`);
  }
  return result;
}

function parseBoolean(value, name, fallback = false) {
  if (value === undefined || value === '') return fallback;
  const normalized = String(value).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'no', 'off'].includes(normalized)) return false;
  throw new ConfigurationError(`${name} must be a boolean`);
}

function parseOptionalEncryptionKey(value) {
  const encoded = String(value ?? '').trim();
  if (!encoded) return null;
  const bytes = Buffer.from(encoded, 'base64');
  if (bytes.length !== 32 || bytes.toString('base64').replace(/=+$/u, '') !== encoded.replace(/=+$/u, '')) {
    throw new ConfigurationError('CONFIG_ENCRYPTION_KEY must be a base64 encoded 32-byte key');
  }
  return bytes;
}

function parseHostAllowlist(value) {
  const source = String(value ?? '*.alicdn.com,*.alicdn.net,*.tbcdn.cn,*.myqcloud.com').split(',');
  const hosts = source.map((item) => item.trim().toLowerCase()).filter(Boolean);
  if (!hosts.length || hosts.some((item) => !/^(?:\*\.)?[a-z0-9.-]+$/u.test(item))) {
    throw new ConfigurationError('IMAGE_HOST_ALLOWLIST must contain comma-separated hostnames');
  }
  return Object.freeze([...new Set(hosts)]);
}

function sameMembers(actual, expected) {
  return Array.isArray(actual)
    && actual.length === expected.length
    && expected.every((value) => actual.includes(value));
}

export function validateManifest(manifest) {
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    throw new ConfigurationError('manifest must be a JSON object');
  }
  if (manifest.id !== 'wanda-seat-autoquote') {
    throw new ConfigurationError('manifest.id must be wanda-seat-autoquote');
  }
  if (manifest.vendor !== 'xdl') {
    throw new ConfigurationError('manifest.vendor must be xdl');
  }
  if (manifest.type !== 'plugin' || manifest.entrypoint?.server !== 'index.mjs') {
    throw new ConfigurationError('manifest plugin type or server entrypoint is invalid');
  }
  if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(manifest.version ?? '')) {
    throw new ConfigurationError('manifest.version must be a semantic version');
  }
  if (!/^\/[^?#]*$/.test(manifest.entrypoint?.webhookPath ?? '')) {
    throw new ConfigurationError('manifest.entrypoint.webhookPath must be an absolute path');
  }
  if (manifest.entrypoint?.web !== '/ui' || manifest.entrypoint?.crossOriginUi !== true) {
    throw new ConfigurationError('manifest embedded UI entrypoint is invalid');
  }
  if (!Number.isInteger(manifest.runtime?.port) || manifest.runtime.port < 1 || manifest.runtime.port > 65535) {
    throw new ConfigurationError('manifest.runtime.port must be between 1 and 65535');
  }
  if (!sameMembers(manifest.permissions, REQUIRED_PERMISSIONS)) {
    throw new ConfigurationError('manifest.permissions does not match the approved runtime scope');
  }
  if (!sameMembers(manifest.extensionPoints?.subscribes, REQUIRED_SUBSCRIPTIONS)) {
    throw new ConfigurationError('manifest subscriptions do not match the approved event set');
  }
  return manifest;
}

export async function loadManifest(projectRoot = PROJECT_ROOT) {
  const manifestPath = path.join(projectRoot, 'yumaiduo.plugin.json');
  let parsed;
  try {
    parsed = JSON.parse(await readFile(manifestPath, 'utf8'));
  } catch (error) {
    throw new ConfigurationError(`unable to read manifest: ${error.message}`);
  }
  return validateManifest(parsed);
}

export async function loadConfig({ env = process.env, projectRoot = PROJECT_ROOT, manifest } = {}) {
  const resolvedManifest = validateManifest(manifest ?? await loadManifest(projectRoot));
  const port = parseInteger(env.PORT, 'PORT', {
    min: 1,
    max: 65535,
    fallback: resolvedManifest.runtime.port,
  });
  const developerToken = requiredString(env.PLUGIN_DEVELOPER_TOKEN, 'PLUGIN_DEVELOPER_TOKEN');
  if (!/^pdk_[A-Za-z0-9_-]{8,}$/.test(developerToken)) {
    throw new ConfigurationError('PLUGIN_DEVELOPER_TOKEN must be a valid pdk_ token');
  }
  const logLevel = env.LOG_LEVEL?.trim().toLowerCase() || 'info';
  if (!LOG_LEVELS.has(logLevel)) {
    throw new ConfigurationError('LOG_LEVEL must be debug, info, warn, or error');
  }

  const configEncryptionKey = parseOptionalEncryptionKey(env.CONFIG_ENCRYPTION_KEY);
  const allowLocalUiBypass = parseBoolean(env.ALLOW_LOCAL_UI_BYPASS, 'ALLOW_LOCAL_UI_BYPASS', false);
  if (env.NODE_ENV === 'production' && allowLocalUiBypass) {
    throw new ConfigurationError('ALLOW_LOCAL_UI_BYPASS cannot be enabled in production');
  }
  if (env.NODE_ENV === 'production' && !configEncryptionKey) {
    throw new ConfigurationError('CONFIG_ENCRYPTION_KEY is required in production');
  }
  const dataDir = path.resolve(projectRoot, env.DATA_DIR?.trim() || 'data');
  if (env.NODE_ENV === 'production') {
    const allowedDataDirs = new Set([
      path.resolve(projectRoot, 'data'),
      path.resolve('/var/lib/ticket-system/wanda-ai-plugin-data'),
    ]);
    if (!allowedDataDirs.has(dataDir)) {
      throw new ConfigurationError('DATA_DIR must use the isolated Wanda AI plugin data directory in production');
    }
  }
  const temporaryUiEnabled = parseBoolean(env.TEMP_UI_ENABLED, 'TEMP_UI_ENABLED', false);
  let temporaryUi = null;
  if (temporaryUiEnabled) {
    const tenantId = requiredString(env.TEMP_UI_TENANT_ID, 'TEMP_UI_TENANT_ID');
    const userId = requiredString(env.TEMP_UI_USER_ID, 'TEMP_UI_USER_ID');
    const internalSecret = requiredString(env.TEMP_UI_INTERNAL_SECRET, 'TEMP_UI_INTERNAL_SECRET');
    const basicAuthSha256 = requiredString(env.TEMP_UI_BASIC_AUTH_SHA256, 'TEMP_UI_BASIC_AUTH_SHA256').toLowerCase();
    if (tenantId.length > 128) throw new ConfigurationError('TEMP_UI_TENANT_ID is too long');
    if (userId.length > 128) throw new ConfigurationError('TEMP_UI_USER_ID is too long');
    if (internalSecret.length < 32 || internalSecret.length > 512) {
      throw new ConfigurationError('TEMP_UI_INTERNAL_SECRET must contain 32 to 512 characters');
    }
    if (!/^[a-f0-9]{64}$/u.test(basicAuthSha256)) {
      throw new ConfigurationError('TEMP_UI_BASIC_AUTH_SHA256 must be a SHA-256 hex digest');
    }
    temporaryUi = Object.freeze({ tenantId, userId, internalSecret, basicAuthSha256 });
  }
  const quotePreviewOnly = parseBoolean(env.QUOTE_PREVIEW_ONLY, 'QUOTE_PREVIEW_ONLY', false);
  const replyPreviewEnabled = parseBoolean(env.AI_REPLY_PREVIEW_ENABLED, 'AI_REPLY_PREVIEW_ENABLED', false);
  const replyAutoSendEnabled = parseBoolean(env.AI_REPLY_AUTO_SEND_ENABLED, 'AI_REPLY_AUTO_SEND_ENABLED', false);
  if (replyAutoSendEnabled && !replyPreviewEnabled) {
    throw new ConfigurationError('AI_REPLY_AUTO_SEND_ENABLED requires AI_REPLY_PREVIEW_ENABLED=true');
  }
  let quotePreview = null;
  let replyPreview = null;
  let conversationAgent = null;
  if (quotePreviewOnly) {
    const ingestUrl = parseSecureServiceUrl(env.WANDA_V3_PREVIEW_INGEST_URL, 'WANDA_V3_PREVIEW_INGEST_URL');
    const ingestKey = requiredString(env.WANDA_V3_PREVIEW_INGEST_KEY, 'WANDA_V3_PREVIEW_INGEST_KEY');
    if (ingestKey.length < 32 || ingestKey.length > 512) {
      throw new ConfigurationError('WANDA_V3_PREVIEW_INGEST_KEY must contain 32 to 512 characters');
    }
    quotePreview = Object.freeze({
      recognizeUrl: new URL('/api/quotes/preview-recognize', ingestUrl).toString(),
      textFactUrl: new URL('/api/quotes/preview-extract-text', ingestUrl).toString(),
      quoteUrl: new URL('/api/quotes/preview-quote', ingestUrl).toString(),
      ingestKey,
    });
    if (replyPreviewEnabled) {
      replyPreview = Object.freeze({
        ingestUrl: new URL('/api/replies/preview-ingest', ingestUrl).toString(),
        ingestKey,
        autoSend: replyAutoSendEnabled,
      });
      conversationAgent = Object.freeze({
        url: new URL('/api/agents/turn', ingestUrl).toString(),
        ingestKey,
      });
    }
  }
  return Object.freeze({
    projectRoot,
    manifest: resolvedManifest,
    host: env.HOST?.trim() || '0.0.0.0',
    port,
    baseUrl: parsePluginCallbackBaseUrl(env.PLUGIN_BASE_URL ?? resolvedManifest.runtime.baseUrl),
    coreUrl: parseSecureServiceUrl(env.CORE_URL, 'CORE_URL'),
    developerToken,
    requestTimeoutMs: parseInteger(env.REQUEST_TIMEOUT_MS, 'REQUEST_TIMEOUT_MS', {
      min: 100,
      max: 120_000,
      fallback: 10_000,
    }),
    webhookAckTimeoutMs: parseInteger(env.WEBHOOK_ACK_TIMEOUT_MS, 'WEBHOOK_ACK_TIMEOUT_MS', {
      min: 100,
      max: 4_900,
      fallback: 4_500,
    }),
    maxWebhookBodyBytes: parseInteger(env.MAX_WEBHOOK_BODY_BYTES, 'MAX_WEBHOOK_BODY_BYTES', {
      min: 1_024,
      max: 10 * 1_024 * 1_024,
      fallback: 1_024 * 1_024,
    }),
    maxUiBodyBytes: parseInteger(env.MAX_UI_BODY_BYTES, 'MAX_UI_BODY_BYTES', {
      min: 1_024,
      // Image test requests are gateway-signed JSON envelopes. Keep an upper
      // bound, but allow a normal phone screenshot to be forwarded for a
      // one-off recognition test without persisting it in the plugin.
      // The backend accepts a 5 MiB image. A JSON data URL expands that image
      // by roughly one third, so the plugin ingress must allow 7 MiB.
      max: 8 * 1_024 * 1_024,
      fallback: 7 * 1_024 * 1_024,
    }),
    eventRetentionDays: parseInteger(env.EVENT_RETENTION_DAYS, 'EVENT_RETENTION_DAYS', {
      min: 1,
      max: 365,
      fallback: 30,
    }),
    workerIntervalMs: parseInteger(env.WORKER_INTERVAL_MS, 'WORKER_INTERVAL_MS', {
      min: 250,
      max: 60_000,
      fallback: 750,
    }),
    dataDir,
    configEncryptionKey,
    allowLocalUiBypass,
    temporaryUi,
    quotePreviewOnly,
    replyPreviewEnabled,
    replyAutoSendEnabled,
    quotePreview,
    replyPreview,
    conversationAgent,
    imageHostAllowlist: parseHostAllowlist(env.IMAGE_HOST_ALLOWLIST),
    logLevel,
    backend: Object.freeze({
      baseUrl: parseSecureServiceUrl(
        env.BACKEND_BASE_URL,
        'BACKEND_BASE_URL',
      ),
      bridgeKey: requiredString(
        env.BACKEND_BRIDGE_KEY,
        'BACKEND_BRIDGE_KEY',
      ),
      paths: Object.freeze({
        quotePolicy: parsePath(
          env.BACKEND_QUOTE_POLICY_PATH,
          'BACKEND_QUOTE_POLICY_PATH',
          '/api/xianyu-plugin/bridge/quote-policy',
        ),
        runtimeSettings: parsePath(
          env.BACKEND_RUNTIME_SETTINGS_PATH,
          'BACKEND_RUNTIME_SETTINGS_PATH',
          '/api/xianyu-plugin/bridge/runtime-settings',
        ),
        aiVisionPreview: parsePath(
          env.BACKEND_AI_VISION_PREVIEW_PATH,
          'BACKEND_AI_VISION_PREVIEW_PATH',
          '/api/xianyu-plugin/bridge/ai-vision-preview',
        ),
        storageUpload: parsePath(
          env.BACKEND_STORAGE_UPLOAD_PATH,
          'BACKEND_STORAGE_UPLOAD_PATH',
          '/api/storage/images',
        ),
        visionRecognize: parsePath(
          env.BACKEND_VISION_RECOGNIZE_PATH,
          'BACKEND_VISION_RECOGNIZE_PATH',
          '/api/wanda-ai/vision/recognize',
        ),
        realtimeQuote: parsePath(
          env.BACKEND_REALTIME_QUOTE_PATH,
          'BACKEND_REALTIME_QUOTE_PATH',
          '/api/wanda-ai/quote/realtime',
        ),
        shopDirectorySync: parsePath(
          env.BACKEND_SHOP_DIRECTORY_SYNC_PATH,
          'BACKEND_SHOP_DIRECTORY_SYNC_PATH',
          '/api/xianyu-plugin/bridge/shops/sync',
        ),
        upsertOrder: parsePath(env.BACKEND_UPSERT_PATH, 'BACKEND_UPSERT_PATH', '/api/xianyu-plugin/bridge/orders/upsert'),
        matchShowtime: parsePath(env.BACKEND_MATCH_PATH, 'BACKEND_MATCH_PATH', '/api/order/match'),
        taskBase: parsePath(env.BACKEND_TASK_BASE_PATH, 'BACKEND_TASK_BASE_PATH', '/api/xianyu-plugin/bridge/tasks'),
        agentRun: parsePath(env.BACKEND_AGENT_RUN_PATH, 'BACKEND_AGENT_RUN_PATH', '/api/xianyu-plugin/bridge/agent/run'),
      }),
    }),
  });
}

function redactText(value) {
  return value.replace(TOKEN_TEXT, '[REDACTED]').replace(BEARER_TEXT, 'Bearer [REDACTED]');
}

export function redactLogValue(value, seen = new WeakSet()) {
  if (typeof value === 'string') return redactText(value);
  if (value instanceof Error) {
    return { name: value.name, message: redactText(value.message) };
  }
  if (!value || typeof value !== 'object') return value;
  if (seen.has(value)) return '[Circular]';
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => redactLogValue(item, seen));
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [
    key,
    SECRET_KEY.test(key) ? '[REDACTED]' : redactLogValue(item, seen),
  ]));
}

export function createLogger({ level = 'info', sink = console } = {}) {
  if (!LOG_LEVELS.has(level)) throw new ConfigurationError('logger level is invalid');
  const threshold = ['debug', 'info', 'warn', 'error'].indexOf(level);
  const write = (entryLevel, message, fields = {}) => {
    if (['debug', 'info', 'warn', 'error'].indexOf(entryLevel) < threshold) return;
    const entry = redactLogValue({
      timestamp: new Date().toISOString(),
      level: entryLevel,
      message,
      ...fields,
    });
    const target = typeof sink[entryLevel] === 'function' ? sink[entryLevel] : sink.log;
    target.call(sink, JSON.stringify(entry));
  };
  return Object.freeze({
    debug: (message, fields) => write('debug', message, fields),
    info: (message, fields) => write('info', message, fields),
    warn: (message, fields) => write('warn', message, fields),
    error: (message, fields) => write('error', message, fields),
  });
}
