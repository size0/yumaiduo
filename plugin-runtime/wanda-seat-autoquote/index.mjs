import { realpathSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import manifest from './yumaiduo.plugin.json' with { type: 'json' };
import { createV2BackendClient } from './src/backend/client.mjs';
import { createLogger, loadV2Config, redact } from './src/config.mjs';
import { createV2HttpServer } from './src/http/server.mjs';
import { createV2PlatformRuntime } from './src/platform/runtime.mjs';
import { createV2Runtime } from './src/runtime/event-processor.mjs';

export async function createPluginRuntime({
  env = process.env,
  fetchImpl = globalThis.fetch,
  sdk,
  sdkLoader,
  logger,
  httpServerFactory = createV2HttpServer,
} = {}) {
  const config = loadV2Config({ env, manifest });
  const log = logger ?? createLogger(config.logLevel);
  const platform = createV2PlatformRuntime(config, { fetchImpl, sdk, sdkLoader, logger: log });
  const backend = createV2BackendClient(config, { fetchImpl, logger: log });
  const runtime = createV2Runtime({ config, platform, backend, logger: log });
  let server;

  async function health() {
    return {
      ...platform.health(),
      runtime: await runtime.health(),
    };
  }

  async function start() {
    await platform.register();
    await runtime.start();
    server = httpServerFactory({
      config, platform, enqueue: runtime.enqueue, syncShops: runtime.syncTenantShops,
      listOrders: runtime.listTenantOrders, getOrder: runtime.getTenantOrder,
      fulfillOrder: runtime.fulfillTenantOrder, health, logger: log,
    });
    return server.listen();
  }

  async function stop() {
    await server?.close();
    await runtime.stop();
    platform.stop();
  }

  return Object.freeze({ config, start, stop, health });
}

export function isEntrypointPath(moduleUrl, argvPath, resolveRealPath = realpathSync) {
  if (!argvPath) return false;
  try {
    return resolveRealPath(fileURLToPath(moduleUrl)) === resolveRealPath(argvPath);
  } catch {
    return moduleUrl === pathToFileURL(argvPath).href;
  }
}

export async function main() {
  const plugin = await createPluginRuntime();
  const shutdown = async () => {
    await plugin.stop();
  };
  process.once('SIGINT', () => void shutdown());
  process.once('SIGTERM', () => void shutdown());
  await plugin.start();
}

if (isEntrypointPath(import.meta.url, process.argv[1])) {
  main().catch((error) => {
    process.stderr.write(`${JSON.stringify(redact({ level: 'error', message: 'v2 startup failed', error }))}\n`);
    process.exitCode = 1;
  });
}
