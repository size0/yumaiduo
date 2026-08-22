/** Own application worker timers, polling pools, startup, and graceful shutdown. */
export function createLifecycleController({
  storage,
  workflow,
  shadowAgentRuntime = null,
  agentReplyOutboxDispatcher,
  agentHumanComparisonScanner,
  workerIntervalMs,
  runtimeVersion,
  logger = console,
  createWorkerPool: workerPoolFactory = createWorkerPool,
  setIntervalFn = globalThis.setInterval,
  clearIntervalFn = globalThis.clearInterval,
} = {}) {
  if (typeof storage?.initialize !== 'function' || typeof storage?.agentRunStore?.list !== 'function' || !storage?.eventStore) {
    throw new TypeError('lifecycle storage dependencies are required');
  }
  if (typeof workflow?.tick !== 'function') throw new TypeError('workflow.tick is required');
  if (typeof agentReplyOutboxDispatcher?.tick !== 'function') throw new TypeError('agentReplyOutboxDispatcher.tick is required');
  if (typeof agentHumanComparisonScanner?.tick !== 'function') throw new TypeError('agentHumanComparisonScanner.tick is required');
  if (typeof workerPoolFactory !== 'function' || typeof setIntervalFn !== 'function' || typeof clearIntervalFn !== 'function') {
    throw new TypeError('lifecycle scheduling dependencies are required');
  }
  const intervalMs = Math.max(1, Number(workerIntervalMs) || 1_000);
  let timer = null;
  let agentTimer = null;
  let agentOutboxTimer = null;
  let humanComparisonTimer = null;
  let historicalEvaluationTimer = null;
  let humanComparisonRunning = false;
  let historicalEvaluationRunning = false;
  let workerPool = null;
  let agentWorkerPool = null;
  let agentOutboxWorkerPool = null;

  async function scheduleHistoricalEvaluations() {
    if (!shadowAgentRuntime || historicalEvaluationRunning) return;
    historicalEvaluationRunning = true;
    try {
      const runs = await storage.agentRunStore.list({ limit: 500 });
      const eventKeys = historicalEvaluationCandidatesFrom(runs, { runtimeVersion });
      if (!eventKeys.length) return;
      const events = typeof storage.eventStore.getMany === 'function' ? await storage.eventStore.getMany(eventKeys) : [];
      for (const event of events) {
        if (event?.status === 'completed' && event.envelope?.event === 'im.message.received') {
          await shadowAgentRuntime.schedule(event.envelope, { mode: 'evaluation' });
        }
      }
      agentWorkerPool?.poll();
    } catch (error) {
      logger.warn?.('[agent-evaluation] historical replay scheduling failed', { error: String(error?.message ?? error) });
    } finally {
      historicalEvaluationRunning = false;
    }
  }

  async function scanHumanComparisons() {
    if (humanComparisonRunning) return;
    humanComparisonRunning = true;
    try {
      await agentHumanComparisonScanner.tick();
    } catch (error) {
      logger.warn?.('[human-comparison] background scan failed', { error: String(error?.message ?? error) });
    } finally {
      humanComparisonRunning = false;
    }
  }

  async function start() {
    await storage.initialize();
    workerPool = workerPoolFactory(workflow, { concurrency: 4, logger });
    timer = setIntervalFn(() => workerPool?.poll(), intervalMs);
    timer.unref();
    workerPool.poll();
    if (shadowAgentRuntime) {
      agentWorkerPool = workerPoolFactory(shadowAgentRuntime, { concurrency: 1, logger });
      agentTimer = setIntervalFn(() => agentWorkerPool?.poll(), Math.max(500, intervalMs));
      agentTimer.unref();
      agentWorkerPool.poll();
    }
    agentOutboxWorkerPool = workerPoolFactory(agentReplyOutboxDispatcher, { concurrency: 1, logger });
    agentOutboxTimer = setIntervalFn(() => agentOutboxWorkerPool?.poll(), Math.max(500, intervalMs));
    agentOutboxTimer.unref();
    agentOutboxWorkerPool.poll();
    historicalEvaluationTimer = setIntervalFn(scheduleHistoricalEvaluations, 15_000);
    historicalEvaluationTimer.unref();
    void scheduleHistoricalEvaluations();
    humanComparisonTimer = setIntervalFn(scanHumanComparisons, 30_000);
    humanComparisonTimer.unref();
  }

  async function stop() {
    for (const activeTimer of [timer, agentTimer, agentOutboxTimer, humanComparisonTimer, historicalEvaluationTimer]) {
      if (activeTimer) clearIntervalFn(activeTimer);
    }
    timer = null;
    agentTimer = null;
    agentOutboxTimer = null;
    humanComparisonTimer = null;
    historicalEvaluationTimer = null;
    shadowAgentRuntime?.stop?.();
    const pool = workerPool;
    const agentPool = agentWorkerPool;
    const outboxPool = agentOutboxWorkerPool;
    workerPool = null;
    agentWorkerPool = null;
    agentOutboxWorkerPool = null;
    await Promise.all([pool?.stop(), agentPool?.stop(), outboxPool?.stop()]);
  }

  function status() {
    return Object.freeze({
      worker: timer ? 'running' : 'stopped',
      agent_worker: agentTimer ? 'running' : 'stopped',
      agent_outbox_worker: agentOutboxTimer ? 'running' : 'stopped',
      historical_evaluation_worker: historicalEvaluationTimer ? 'running' : 'stopped',
      human_comparison_worker: humanComparisonTimer ? 'running' : 'stopped',
    });
  }

  return Object.freeze({ start, stop, status });
}

export function createWorkerPool(workflow, { concurrency = 4, logger = console } = {}) {
  const limit = Number.isInteger(concurrency) && concurrency >= 1 && concurrency <= 16 ? concurrency : 4;
  const active = new Set();
  let running = true;

  function poll() {
    if (!running) return;
    while (active.size < limit) {
      let task;
      let completedWork = false;
      task = Promise.resolve()
        .then(() => workflow.tick())
        .then((result) => {
          completedWork = result != null;
          return result;
        })
        .catch((error) => {
          logger.error?.('workflow tick failed', { error });
          return null;
        })
        .finally(() => {
          active.delete(task);
          if (running && completedWork) queueMicrotask(poll);
        });
      active.add(task);
    }
  }

  async function stop() {
    running = false;
    await Promise.allSettled([...active]);
  }

  return Object.freeze({ poll, stop });
}

export function historicalEvaluationCandidatesFrom(runs, { runtimeVersion, target = 100, batchSize = 1 } = {}) {
  const version = String(runtimeVersion ?? '').trim();
  const maximum = Number.isSafeInteger(Number(target)) ? Math.max(1, Math.min(100, Number(target))) : 100;
  const batch = Number.isSafeInteger(Number(batchSize)) ? Math.max(1, Math.min(20, Number(batchSize))) : 10;
  if (!version) return [];
  const values = Array.isArray(runs) ? runs : [];
  const evaluationPrefix = `evaluation:${version}:`;
  const current = values.filter((run) => run?.result?.runtime_version === version || String(run?.run_id ?? '').startsWith(evaluationPrefix));
  const currentEventKeys = new Set(current.map((run) => String(run?.event_key ?? '')).filter(Boolean));
  const evaluationRuns = current.filter((run) => run?.mode === 'evaluation');
  const inFlight = evaluationRuns.filter((run) => ['queued', 'processing', 'retry'].includes(run?.status)).length;
  const remaining = maximum - evaluationRuns.length;
  const slots = Math.max(0, Math.min(batch - inFlight, remaining));
  if (!slots) return [];
  const score = (run) => {
    const tools = Array.isArray(run?.tool_calls) ? run.tool_calls.map((call) => String(call?.tool ?? '')) : [];
    const actions = Array.isArray(run?.result?.trace) ? run.result.trace.map((item) => String(item?.action ?? '')) : [];
    return tools.includes('recognize_image') || actions.some((action) => ['recognize_image', 'start_quote'].includes(action)) ? 1 : 0;
  };
  const seen = new Set();
  return values
    .filter((run) => run?.mode === 'shadow' && run?.event_key && !currentEventKeys.has(String(run.event_key)))
    .sort((left, right) => score(right) - score(left) || String(right.updated_at ?? '').localeCompare(String(left.updated_at ?? '')))
    .map((run) => String(run.event_key))
    .filter((key) => key && !seen.has(key) && seen.add(key))
    .slice(0, slots);
}
