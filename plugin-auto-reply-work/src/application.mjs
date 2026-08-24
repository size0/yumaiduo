import { createStorageBundle } from './bootstrap/create-storage-bundle.mjs';
import { createAgentRuntimeBundle } from './bootstrap/create-agent-runtime-bundle.mjs';
import { createLifecycleController } from './bootstrap/create-lifecycle-controller.mjs';
import { createImageLoader } from './image-loader.mjs';
import { createUiHandler } from './ui-handler.mjs';
import { createQuotePreviewClient } from './quote-preview-client.mjs';
import { createReplyPreviewClient } from './reply-preview-client.mjs';
import { AGENT_RUNTIME_VERSION } from './agent/shadow-agent-runtime.mjs';
import { releaseGuardMetricsFrom } from './agent/release-guard-metrics.mjs';
import { createWorkflow } from './workflow.mjs';
import { createOperatorApi } from './admin/create-operator-api.mjs';
export { createWorkerPool, historicalEvaluationCandidatesFrom, runConcurrentTicks } from './bootstrap/create-lifecycle-controller.mjs';
export { uniqueAgentEvaluationRuns } from './admin/create-operator-api.mjs';
export {
  agentEvaluationTurnSummary,
  conversationLearningSummaryFrom,
  eventDiagnosticSummary,
  operationalLogStatus,
  quoteDiagnosticSummary,
  ticketIssuanceFromXianyuOrder,
} from './admin/operator-presenters.mjs';

export async function createApplication({ config, platformRuntime, backendClient, logger = console }) {
  const storage = createStorageBundle(config);
  const {
    eventStore,
    conversationContextStore,
    agentRunStore,
    agentReplyOutboxStore,
    agentManualTaskStore,
    agentHumanComparisonStore,
  } = storage;
  const imageLoader = createImageLoader({ allowlist: config.imageHostAllowlist });
  const quotePreviewClient = createQuotePreviewClient(config);
  const replyPreviewClient = createReplyPreviewClient(config);
  const operatorApi = createOperatorApi({ config, platformRuntime, backendClient, storage, logger });
  const agentRuntime = createAgentRuntimeBundle({
    config,
    platformRuntime,
    storage,
    quotePreviewClient,
    getSettings: operatorApi.getSettings,
    logger,
  });
  const {
    conversationAgentPlanner,
    agentHumanComparisonScanner,
    agentReplyOutboxDispatcher,
    shadowAgentRuntime,
  } = agentRuntime;
  const workflow = createWorkflow({
    backend: backendClient,
    coreFor: (tenantId) => platformRuntime.createClient(tenantId),
    eventStore,
    conversationContextStore,
    imageLoader,
    quotePreviewClient,
    replyPreviewClient,
    conversationAgentPlanner,
    shadowAgentScheduler: shadowAgentRuntime,
    manualTaskStore: agentManualTaskStore,
    autoReplyEnabled: config.replyAutoSendEnabled,
    logger,
  });
  const lifecycle = createLifecycleController({
    storage,
    workflow,
    shadowAgentRuntime,
    agentReplyOutboxDispatcher,
    agentHumanComparisonScanner,
    workerIntervalMs: config.workerIntervalMs,
    runtimeVersion: AGENT_RUNTIME_VERSION,
    logger,
  });
  async function start() {
    await lifecycle.start();
  }

  async function stop() {
    await lifecycle.stop();
  }

  async function health() {
    const [queue, agentQueue, agentOutbox, manualTasks, humanComparisons, releaseSettings, recentRuns, recentOutbox] = await Promise.all([
      eventStore.health(), agentRunStore.health(), agentReplyOutboxStore.health(), agentManualTaskStore.health(), agentHumanComparisonStore.health(),
      operatorApi.getSettings('__release_health__'), agentRunStore.list({ limit: 100 }), agentReplyOutboxStore.list({ limit: 100 }),
    ]);
    const workerStatus = lifecycle.status();
    const workers = [workerStatus.worker, workerStatus.agent_worker, workerStatus.agent_outbox_worker, workerStatus.historical_evaluation_worker, workerStatus.human_comparison_worker];
    return {
      ok: workers.every((worker) => worker === 'running'),
      agent_runtime_version: AGENT_RUNTIME_VERSION,
      native_complete: typeof conversationAgentPlanner?.complete === 'function',
      source_commit: config.releaseMetadata?.sourceCommit ?? '',
      manifest_sha256: config.releaseMetadata?.manifestSha256 ?? '',
      artifact_sha256: config.releaseMetadata?.artifactSha256 ?? '',
      release_id: String(releaseSettings?.agent_release_id ?? ''),
      release_evidence_id: String(releaseSettings?.agent_release_evidence_id ?? ''),
      release_generation: Number(releaseSettings?.release_generation) || null,
      conversation_agent_mode: String(releaseSettings?.conversation_agent_mode ?? 'shadow'),
      release_guard_metrics: releaseGuardMetricsFrom(recentRuns, recentOutbox, releaseSettings?.release_generation),
      worker: workerStatus.worker,
      agent_worker: workerStatus.agent_worker,
      agent_outbox_worker: workerStatus.agent_outbox_worker,
      queue,
      agent_queue: agentQueue,
      agent_outbox: agentOutbox,
      manual_tasks: manualTasks,
      historical_evaluation_worker: workerStatus.historical_evaluation_worker,
      human_comparison_worker: workerStatus.human_comparison_worker,
      human_comparisons: humanComparisons,
    };
  }

  const uiHandler = createUiHandler({
    config,
    platformRuntime,
    logger,
    api: operatorApi,
  });

  return Object.freeze({
    enqueueEvent: workflow.enqueueEvent,
    start,
    stop,
    health,
    handleHttpRequest: uiHandler,
  });
}
