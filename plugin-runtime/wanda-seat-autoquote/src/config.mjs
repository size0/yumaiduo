const SECRET_FIELDS = /key|token|secret|authorization/i;

export class ConfigurationError extends Error {}

function required(env, name) {
  const value = String(env[name] ?? '').trim();
  if (!value) throw new ConfigurationError(`${name} is required`);
  return value;
}

function url(env, name) {
  const value = required(env, name).replace(/\/$/, '');
  let parsed;
  try { parsed = new URL(value); } catch { throw new ConfigurationError(`${name} must be a valid URL`); }
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new ConfigurationError(`${name} must use HTTP(S)`);
  return value;
}

function loopbackUrl(env, name, fallback) {
  const source = String(env[name] ?? fallback).trim();
  const value = source.replace(/\/$/, '');
  let parsed;
  try { parsed = new URL(value); } catch { throw new ConfigurationError(`${name} must be a valid URL`); }
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new ConfigurationError(`${name} must use HTTP(S)`);
  if (!['127.0.0.1', 'localhost', '::1'].includes(parsed.hostname)) {
    throw new ConfigurationError(`${name} must use a loopback host`);
  }
  return value;
}

function integer(env, name, fallback, min, max) {
  const value = Number(env[name] ?? fallback);
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new ConfigurationError(`${name} must be an integer between ${min} and ${max}`);
  }
  return value;
}

function encryptionKey(env) {
  const encoded = required(env, 'CONFIG_ENCRYPTION_KEY');
  const key = Buffer.from(encoded, 'base64');
  if (key.length !== 32) throw new ConfigurationError('CONFIG_ENCRYPTION_KEY must be a base64-encoded 32-byte key');
  return key;
}

export function loadV2Config({ env, manifest }) {
  if (manifest?.id !== 'wanda-seat-autoquote') throw new ConfigurationError('manifest.id must remain wanda-seat-autoquote');
  if (!Array.isArray(manifest?.extensionPoints?.subscribes)) throw new ConfigurationError('manifest subscriptions are required');
  const dataDir = String(env.WANDA_AI_V2_DATA_DIR ?? 'data/v2').trim();
  if (!dataDir || /plugin[-_]?bridge|xianyu/i.test(dataDir)) throw new ConfigurationError('WANDA_AI_V2_DATA_DIR must be isolated');
  return Object.freeze({
    manifest,
    coreUrl: url(env, 'CORE_URL'),
    developerToken: required(env, 'PLUGIN_DEVELOPER_TOKEN'),
    baseUrl: url(env, 'PLUGIN_BASE_URL'),
    host: String(env.HOST ?? '127.0.0.1'),
    port: integer(env, 'PORT', manifest.runtime?.port ?? 4003, 1, 65535),
    requestTimeoutMs: integer(env, 'REQUEST_TIMEOUT_MS', 90_000, 5_000, 90_000),
    maxWebhookBodyBytes: integer(env, 'MAX_WEBHOOK_BODY_BYTES', 1_048_576, 1_024, 10_485_760),
    maxUiBodyBytes: integer(env, 'WANDA_V4_UI_MAX_BODY_BYTES', 30_000_000, 1_024, 40_000_000),
    maxConcurrentRuns: integer(env, 'WANDA_AI_V2_MAX_CONCURRENT_RUNS', 12, 1, 100),
    dataDir,
    encryptionKey: encryptionKey(env),
    logLevel: ['debug', 'info', 'warn', 'error'].includes(env.LOG_LEVEL) ? env.LOG_LEVEL : 'info',
    v4BackendUrl: loopbackUrl(env, 'WANDA_V4_BACKEND_URL', 'http://127.0.0.1:8012'),
    backend: Object.freeze({
      baseUrl: url(env, 'WANDA_AI_V2_BACKEND_URL'),
      sharedSecret: required(env, 'WANDA_AI_V2_BRIDGE_KEY'),
      processPath: '/api/wanda-ai-v2/plugin/events/process',
      shopsPath: '/api/wanda-ai-v2/plugin/shops/sync',
    }),
  });
}

export function redact(value, seen = new WeakSet()) {
  if (typeof value === 'string') return value.replace(/Bearer\s+\S+/gi, 'Bearer [REDACTED]');
  if (value instanceof Error) return { name: value.name, message: redact(value.message) };
  if (!value || typeof value !== 'object') return value;
  if (seen.has(value)) return '[Circular]';
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => redact(item, seen));
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, SECRET_FIELDS.test(key) ? '[REDACTED]' : redact(item, seen)]));
}

export function createLogger(level = 'info', sink = console) {
  const threshold = ['debug', 'info', 'warn', 'error'].indexOf(level);
  const write = (entryLevel, message, fields = {}) => {
    if (['debug', 'info', 'warn', 'error'].indexOf(entryLevel) < threshold) return;
    const output = JSON.stringify(redact({ time: new Date().toISOString(), level: entryLevel, message, ...fields }));
    (sink[entryLevel] ?? sink.log).call(sink, output);
  };
  return Object.freeze({ debug: (m, f) => write('debug', m, f), info: (m, f) => write('info', m, f), warn: (m, f) => write('warn', m, f), error: (m, f) => write('error', m, f) });
}
