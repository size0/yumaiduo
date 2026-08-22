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
  const unavailableHistoricalEventKeys = new Set();

  async function scheduleHistoricalEvaluations() {
    if (!shadowAgentRuntime || historicalEvaluationRunning) return;
    historicalEvaluationRunning = true;
    try {
      const runs = typeof storage.agentRunStore.listEvaluationIndex === 'function'
        ? await storage.agentRunStore.listEvaluationIndex()
        : await storage.agentRunStore.list({ limit: 500 });
      const eventKeys = historicalEvaluationCandidatesFrom(runs, {
        runtimeVersion,
        batchSize: 1,
        candidateWindowSize: 20,
        excludedEventKeys: unavailableHistoricalEventKeys,
      });
      if (!eventKeys.length) return;
      const events = typeof storage.eventStore.getMany === 'function' ? await storage.eventStore.getMany(eventKeys) : [];
      const eventsByKey = new Map(events.map((event) => [
        String(event?.key ?? `${event?.envelope?.tenantId ?? ''}:${event?.envelope?.id ?? ''}`),
        event,
      ]));
      let scheduled = false;
      for (const key of eventKeys) {
        const event = eventsByKey.get(key);
        if (event?.status !== 'completed' || event.envelope?.event !== 'im.message.received') {
          unavailableHistoricalEventKeys.add(key);
          continue;
        }
        await shadowAgentRuntime.schedule(event.envelope, { mode: 'evaluation' });
        scheduled = true;
        break;
      }
      while (unavailableHistoricalEventKeys.size > 5_000) {
        unavailableHistoricalEventKeys.delete(unavailableHistoricalEventKeys.values().next().value);
      }
      if (scheduled) agentWorkerPool?.poll();
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

export function runConcurrentTicks(workflow, concurrency = 4) {
  const count = Number.isInteger(concurrency) && concurrency >= 1 && concurrency <= 16 ? concurrency : 4;
  return Promise.all(Array.from({ length: count }, async () => {
    for (let processed = 0; processed < 32; processed += 1) {
      const result = await workflow.tick();
      if (result == null) return;
    }
  }));
}

export function historicalEvaluationCandidatesFrom(runs, {
  runtimeVersion,
  target = null,
  imageTarget = 100,
  textTarget = 100,
  batchSize = 1,
  candidateWindowSize = null,
  excludedEventKeys = null,
} = {}) {
  const version = String(runtimeVersion ?? '').trim();
  if (!version) return [];
  const legacyTarget = Number.isSafeInteger(Number(target)) ? Number(target) : null;
  const boundedTarget = (value) => Math.max(1, Math.min(100, Number(value)));
  const maximumImage = boundedTarget(legacyTarget ?? (Number.isSafeInteger(Number(imageTarget)) ? imageTarget : 100));
  const maximumText = boundedTarget(legacyTarget ?? (Number.isSafeInteger(Number(textTarget)) ? textTarget : 100));
  const batch = Number.isSafeInteger(Number(batchSize)) ? Math.max(1, Math.min(20, Number(batchSize))) : 10;
  const values = Array.isArray(runs) ? runs : [];
  const evaluationPrefix = `evaluation:${version}:`;
  const runtimeVersionFor = (run) => String(run?.runtime_version ?? run?.result?.runtime_version ?? '');
  const isImage = (run) => {
    if (run?.has_image === true || run?.result?.source_snapshot?.has_image === true) return true;
    const tools = Array.isArray(run?.tool_calls) ? run.tool_calls.map((call) => String(call?.tool ?? '')) : [];
    const actions = Array.isArray(run?.result?.trace) ? run.result.trace.map((item) => String(item?.action ?? '')) : [];
    return tools.includes('recognize_image') || actions.some((action) => ['recognize_image', 'start_quote'].includes(action));
  };
  const sourceKinds = new Map(values
    .filter((run) => run?.mode === 'shadow' && run?.event_key)
    .map((run) => [String(run.event_key), isImage(run) ? 'image' : 'text']));
  const kindFor = (run) => isImage(run) || sourceKinds.get(String(run?.event_key ?? '')) === 'image' ? 'image' : 'text';
  const current = values.filter((run) => runtimeVersionFor(run) === version || String(run?.run_id ?? '').startsWith(evaluationPrefix));
  const currentEventKeys = new Set(current.map((run) => String(run?.event_key ?? '')).filter(Boolean));
  const evaluationRuns = current.filter((run) => run?.mode === 'evaluation');
  const inFlight = evaluationRuns.filter((run) => ['queued', 'processing', 'retry'].includes(run?.status)).length;
  const slots = Math.max(0, batch - inFlight);
  const requestedWindow = Number(candidateWindowSize);
  const selectionLimit = Number.isSafeInteger(requestedWindow)
    ? Math.max(slots, Math.min(20, Math.max(1, requestedWindow)))
    : slots;
  let imageRemaining = Math.max(0, maximumImage - evaluationRuns.filter((run) => kindFor(run) === 'image').length);
  let textRemaining = Math.max(0, maximumText - evaluationRuns.filter((run) => kindFor(run) === 'text').length);
  if (!slots || (!imageRemaining && !textRemaining)) return [];
  const excluded = excludedEventKeys instanceof Set ? excludedEventKeys : new Set();
  const candidates = values
    .filter((run) => run?.mode === 'shadow' && run?.event_key && !currentEventKeys.has(String(run.event_key)) && !excluded.has(String(run.event_key)))
    .sort((left, right) => Number(isImage(right)) - Number(isImage(left)) || String(right.updated_at ?? '').localeCompare(String(left.updated_at ?? '')));
  const seen = new Set();
  const selected = [];
  for (const run of candidates) {
    const key = String(run.event_key);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    const kind = kindFor(run);
    if ((kind === 'image' && imageRemaining <= 0) || (kind === 'text' && textRemaining <= 0)) continue;
    selected.push(key);
    if (kind === 'image') imageRemaining -= 1;
    else textRemaining -= 1;
    if (selected.length >= selectionLimit) break;
  }
  return selected;
}
