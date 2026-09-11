const REGISTER_PATH = '/api/v1/plugin/runtime/register';

function sdkModule(module) { return module?.default ?? module; }

export function createV2PlatformRuntime(config, { fetchImpl = globalThis.fetch, sdk, sdkLoader = () => import('@yumaiduo/plugin-sdk-server'), logger = console } = {}) {
  let api = sdk ? sdkModule(sdk) : null;
  let token = null;
  let webhookSecret = null;
  let registeredAt = null;

  async function sdkApi() {
    api ??= sdkModule(await sdkLoader());
    if (typeof api?.createPluginClient !== 'function' || typeof api?.verifyWebhookSignature !== 'function') {
      throw new Error('official plugin SDK is unavailable');
    }
    return api;
  }

  async function register() {
    await sdkApi();
    const response = await fetchImpl(`${config.coreUrl}${REGISTER_PATH}`, {
      method: 'POST', headers: { 'content-type': 'application/json', 'x-plugin-developer-token': config.developerToken },
      body: JSON.stringify({ manifest: config.manifest, baseUrl: config.baseUrl }), signal: AbortSignal.timeout(config.requestTimeoutMs),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload?.data?.token || !payload?.data?.webhookSecret) throw new Error(`plugin registration failed: ${response.status}`);
    token = payload.data.token;
    webhookSecret = payload.data.webhookSecret;
    registeredAt = new Date().toISOString();
    logger.info('v2 registered', { pluginId: config.manifest.id });
  }

  function verifySignedRequest({ timestamp, signature, rawBody }) {
    if (!api || !webhookSecret || !timestamp || !signature) return false;
    return api.verifyWebhookSignature({ secret: webhookSecret, timestamp, signature, rawBody }) === true;
  }

  function verifyWebhook(request) { return verifySignedRequest(request); }
  function verifyGateway({ pluginId, tenantId, userId, timestamp, signature, rawBody }) {
    if (pluginId !== config.manifest.id || !tenantId || !userId) return false;
    return verifySignedRequest({ timestamp, signature, rawBody });
  }

  function createClient(tenantId) {
    if (!token) throw new Error('plugin is not registered');
    return api.createPluginClient({ coreUrl: config.coreUrl, pluginToken: token, tenantId: String(tenantId) });
  }

  function health() { return { ok: Boolean(token && webhookSecret), registered: Boolean(token && webhookSecret), registeredAt, pluginId: config.manifest.id }; }
  function stop() { token = null; webhookSecret = null; registeredAt = null; }
  return Object.freeze({ register, verifyWebhook, verifyGateway, createClient, health, stop });
}
