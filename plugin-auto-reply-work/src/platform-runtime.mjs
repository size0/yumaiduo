const REGISTRATION_PATH = '/api/v1/plugin/runtime/register';

function joinUrl(baseUrl, pathname) {
  return `${baseUrl.replace(/\/$/, '')}${pathname}`;
}

async function parseResponse(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    throw new Error('platform returned a non-JSON response');
  }
}

function resolveSdk(module) {
  return module?.default ?? module;
}

export function createPlatformRuntime(config, {
  fetchImpl = globalThis.fetch,
  sdk,
  sdkLoader = () => import('@yumaiduo/plugin-sdk-server'),
  logger = { info() {}, warn() {}, error() {} },
} = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');

  let sdkApi = sdk ? resolveSdk(sdk) : null;
  let pluginToken = null;
  let webhookSecret = null;
  let registeredAt = null;

  async function ensureSdk() {
    sdkApi ??= resolveSdk(await sdkLoader());
    if (typeof sdkApi?.verifyWebhookSignature !== 'function' || typeof sdkApi?.createPluginClient !== 'function') {
      throw new TypeError('@yumaiduo/plugin-sdk-server does not expose the required runtime API');
    }
    return sdkApi;
  }

  async function register() {
    await ensureSdk();
    const response = await fetchImpl(joinUrl(config.coreUrl, REGISTRATION_PATH), {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-plugin-developer-token': config.developerToken,
      },
      body: JSON.stringify({ manifest: config.manifest, baseUrl: config.baseUrl }),
      signal: AbortSignal.timeout(config.requestTimeoutMs),
    });
    const payload = await parseResponse(response);
    if (!response.ok) {
      const error = new Error(`platform registration failed with HTTP ${response.status}`);
      error.status = response.status;
      error.code = payload?.code;
      throw error;
    }
    if (typeof payload?.data?.webhookSecret !== 'string' || payload.data.webhookSecret.length < 16) {
      throw new Error('platform registration response is missing webhookSecret');
    }
    if (typeof payload?.data?.token !== 'string' || !payload.data.token.startsWith('yp_')) {
      throw new Error('platform registration response is missing plugin token');
    }
    webhookSecret = payload.data.webhookSecret;
    pluginToken = payload.data.token;
    registeredAt = new Date().toISOString();
    logger.info('plugin runtime registered', { pluginId: config.manifest.id });
    return health();
  }

  function verifyWebhook({ timestamp, signature, rawBody }) {
    if (!webhookSecret || !sdkApi) return false;
    if (typeof timestamp !== 'string' || typeof signature !== 'string' || typeof rawBody !== 'string') return false;
    try {
      return sdkApi.verifyWebhookSignature({
        secret: webhookSecret,
        timestamp,
        signature,
        rawBody,
      }) === true;
    } catch (error) {
      logger.warn('webhook signature verification failed', { error });
      return false;
    }
  }

  function verifyGateway(input) {
    return verifyWebhook(input);
  }

  function createClient(tenantId) {
    if (!pluginToken || !sdkApi) throw new Error('plugin runtime is not registered');
    if (tenantId === undefined || tenantId === null || String(tenantId).trim() === '') {
      throw new TypeError('tenantId is required');
    }
    return sdkApi.createPluginClient({
      coreUrl: config.coreUrl,
      pluginToken,
      tenantId: String(tenantId),
    });
  }

  function health() {
    return {
      ok: Boolean(pluginToken && webhookSecret),
      registered: Boolean(pluginToken && webhookSecret),
      registeredAt,
      pluginId: config.manifest.id,
    };
  }

  function stop() {
    pluginToken = null;
    webhookSecret = null;
    registeredAt = null;
  }

  return Object.freeze({ register, verifyWebhook, verifyGateway, createClient, health, stop });
}
