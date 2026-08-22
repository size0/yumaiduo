import { realpathSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createBackendClient } from './src/backend-client.mjs';
import { createLogger, loadConfig, redactLogValue } from './src/config.mjs';
import { createHttpServer } from './src/http-server.mjs';
import { createPlatformRuntime } from './src/platform-runtime.mjs';

export class ApplicationContractError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ApplicationContractError';
  }
}

export async function resolveApplicationFactory(injectedFactory) {
  if (injectedFactory !== undefined) {
    if (typeof injectedFactory !== 'function') throw new ApplicationContractError('createApplication must be a function');
    return injectedFactory;
  }
  try {
    const module = await import('./src/application.mjs');
    if (typeof module.createApplication !== 'function') {
      throw new ApplicationContractError('src/application.mjs must export createApplication');
    }
    return module.createApplication;
  } catch (error) {
    if (error instanceof ApplicationContractError) throw error;
    throw new ApplicationContractError(
      'business application is unavailable; provide createApplication or add src/application.mjs exporting createApplication',
    );
  }
}

function validateApplication(application) {
  for (const method of ['enqueueEvent', 'start', 'stop', 'health']) {
    if (typeof application?.[method] !== 'function') {
      throw new ApplicationContractError(`createApplication must return a ${method} function`);
    }
  }
  return application;
}

export async function createPluginRuntime({
  env = process.env,
  projectRoot,
  manifest,
  createApplication,
  fetchImpl = globalThis.fetch,
  sdk,
  sdkLoader,
  logger: injectedLogger,
  httpServerFactory = createHttpServer,
} = {}) {
  const config = await loadConfig({ env, projectRoot, manifest });
  const logger = injectedLogger ?? createLogger({ level: config.logLevel });
  const platformRuntime = createPlatformRuntime(config, { fetchImpl, sdk, sdkLoader, logger });
  const backendClient = createBackendClient(config, { fetchImpl, logger });
  let application = null;
  let httpServer = null;
  let started = false;

  async function health() {
    const platform = platformRuntime.health();
    if (!application) return { ...platform, ok: false, application: 'not_started' };
    try {
      const applicationHealth = await application.health();
      return {
        ...platform,
        ok: platform.registered && applicationHealth?.ok !== false,
        application: applicationHealth ?? { ok: true },
      };
    } catch (error) {
      logger.error('application health check failed', { error });
      return { ...platform, ok: false, application: { ok: false } };
    }
  }

  async function start() {
    if (started) return httpServer?.address() ?? null;
    const applicationFactory = await resolveApplicationFactory(createApplication);
    try {
      await platformRuntime.register();
      application = validateApplication(await applicationFactory({ config, platformRuntime, backendClient }));
      await application.start();
      httpServer = httpServerFactory({
        config,
        platformRuntime,
        enqueueEvent: application.enqueueEvent,
        handleHttpRequest: application.handleHttpRequest,
        health,
        logger,
      });
      const address = await httpServer.listen();
      started = true;
      logger.info('plugin runtime listening', { host: config.host, port: config.port });
      return address;
    } catch (error) {
      if (httpServer) await httpServer.close().catch(() => {});
      if (application) await application.stop().catch(() => {});
      platformRuntime.stop();
      application = null;
      httpServer = null;
      throw error;
    }
  }

  async function stop() {
    if (httpServer) await httpServer.close();
    if (application) await application.stop();
    platformRuntime.stop();
    httpServer = null;
    application = null;
    started = false;
    logger.info('plugin runtime stopped', { pluginId: config.manifest.id });
  }

  return Object.freeze({ config, platformRuntime, backendClient, start, stop, health });
}

export async function main() {
  const runtime = await createPluginRuntime();
  const shutdown = async (signal) => {
    try {
      await runtime.stop();
      process.exitCode = 0;
    } catch (error) {
      process.stderr.write(`${JSON.stringify(redactLogValue({ level: 'error', message: 'shutdown failed', signal, error }))}\n`);
      process.exitCode = 1;
    }
  };
  process.once('SIGINT', () => void shutdown('SIGINT'));
  process.once('SIGTERM', () => void shutdown('SIGTERM'));
  await runtime.start();
}

export function isEntrypointPath(moduleUrl, argvPath, resolveRealPath = realpathSync) {
  if (!argvPath) return false;
  try {
    return resolveRealPath(fileURLToPath(moduleUrl)) === resolveRealPath(argvPath);
  } catch {
    return moduleUrl === pathToFileURL(argvPath).href;
  }
}

const isEntrypoint = isEntrypointPath(import.meta.url, process.argv[1]);
if (isEntrypoint) {
  main().catch((error) => {
    process.stderr.write(`${JSON.stringify(redactLogValue({ level: 'error', message: 'runtime startup failed', error }))}\n`);
    process.exitCode = 1;
  });
}
