import { randomUUID } from 'node:crypto';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const STORE_VERSION = 1;
const CLAIMABLE = new Set(['queued', 'retry']);

function emptyState() { return { version: STORE_VERSION, revision: 0, runs: {} }; }
function clone(value) { return structuredClone(value); }
function bounded(value, length) { return String(value ?? '').trim().slice(0, length); }
function modePriority(mode) { return ({ active: 0, shadow: 1, evaluation: 2 })[mode] ?? 3; }

function sanitizeTrace(value) {
  if (!Array.isArray(value) || value.length > 8) throw new TypeError('too many agent trace steps');
  return value.map((item, index) => ({
    step: Number.isSafeInteger(Number(item?.step)) ? Number(item.step) : index + 1,
    action: bounded(item?.action, 64), intent: bounded(item?.intent, 32),
    confidence: Number.isFinite(Number(item?.confidence)) ? Number(item.confidence) : null,
    goal: bounded(item?.goal, 160), missing_fields: Array.isArray(item?.missing_fields) ? item.missing_fields.slice(0, 10).map((field) => bounded(field, 64)).filter(Boolean) : [],
  }));
}

function sanitizeObservations(value) {
  if (!Array.isArray(value) || value.length > 8) throw new TypeError('too many observations');
  return value.map((item) => {
    const facts = item?.facts && typeof item.facts === 'object' && !Array.isArray(item.facts)
      ? Object.fromEntries(Object.entries(item.facts).slice(0, 30).map(([key, fact]) => [bounded(key, 64), fact]))
      : {};
    if (JSON.stringify(facts).length > 6_000) throw new TypeError('agent observation facts are too large');
    return {
      status: ['success', 'warning', 'error'].includes(item?.status) ? item.status : 'error',
      tool: bounded(item?.tool, 64) || 'unknown', summary: bounded(item?.summary, 200), facts,
      authoritative_reply: bounded(item?.authoritative_reply, 1_000),
      next_actions: Array.isArray(item?.next_actions) ? item.next_actions.slice(0, 8).map((action) => bounded(action, 64)).filter(Boolean) : [],
      stop_reason: bounded(item?.stop_reason, 100) || null,
    };
  });
}

function external(record) {
  const toolCalls = Object.values(record.toolCalls ?? {}).sort((left, right) => Number(left.step) - Number(right.step));
  return Object.freeze({
    run_id: record.runId, event_key: record.eventKey, tenant_id: record.tenantId, mode: record.mode,
    status: record.status, attempts: record.attempts, available_at: record.availableAt,
    lease_id: record.leaseId, lease_until: record.leaseUntil, deadline_at: Number(record.deadlineAt) || null,
    trace: clone(record.trace ?? []), observations: clone(record.observations ?? []), tool_calls: clone(toolCalls), result: clone(record.result),
    last_error: clone(record.lastError), created_at: record.createdAt, updated_at: record.updatedAt,
  });
}

export class AgentRunStore {
  #file; #now; #state = null; #chain = Promise.resolve();
  constructor(file, { now = () => Date.now() } = {}) { this.#file = file; this.#now = now; }

  async initialize() {
    await mkdir(dirname(this.#file), { recursive: true });
    try { this.#state = await this.#readDisk(); }
    catch (error) { if (error?.code !== 'ENOENT') throw error; this.#state = emptyState(); await this.#write(this.#state); }
  }

  async enqueue({ runId, eventKey, tenantId, mode = 'shadow', deadlineMs = 180_000 }) {
    const id = bounded(runId, 240); const event = bounded(eventKey, 240); const tenant = bounded(tenantId, 128);
    const deadline = Number(deadlineMs);
    if (!id || !event || !tenant || !['shadow', 'active', 'evaluation'].includes(mode)) throw new TypeError('invalid agent run');
    if (!Number.isSafeInteger(deadline) || deadline < 30_000 || deadline > 600_000) throw new TypeError('agent run deadlineMs must be between 30000 and 600000');
    return this.#mutate((state) => {
      if (state.runs[id]) return { created: false, run: external(state.runs[id]) };
      const now = this.#now();
      const timestamp = new Date(now).toISOString();
      state.runs[id] = { runId: id, eventKey: event, tenantId: tenant, mode, status: 'queued', attempts: 0, availableAt: now, leaseId: null, leaseUntil: null, deadlineAt: now + deadline, trace: [], observations: [], toolCalls: {}, result: null, lastError: null, createdAt: timestamp, updatedAt: timestamp };
      return { created: true, run: external(state.runs[id]) };
    });
  }

  async enqueueMany(inputs) {
    if (!Array.isArray(inputs) || inputs.length > 500) throw new TypeError('invalid agent run batch');
    const normalized = inputs.map(({ runId, eventKey, tenantId, mode = 'evaluation', deadlineMs = 180_000 } = {}) => {
      const id = bounded(runId, 240); const event = bounded(eventKey, 240); const tenant = bounded(tenantId, 128); const deadline = Number(deadlineMs);
      if (!id || !event || !tenant || !['shadow', 'active', 'evaluation'].includes(mode)) throw new TypeError('invalid agent run');
      if (!Number.isSafeInteger(deadline) || deadline < 30_000 || deadline > 600_000) throw new TypeError('agent run deadlineMs must be between 30000 and 600000');
      return { id, event, tenant, mode, deadline };
    });
    return this.#mutate((state) => {
      let created = 0; let existing = 0;
      for (const input of normalized) {
        if (state.runs[input.id]) { existing += 1; continue; }
        const now = this.#now(); const timestamp = new Date(now).toISOString();
        state.runs[input.id] = { runId: input.id, eventKey: input.event, tenantId: input.tenant, mode: input.mode, status: 'queued', attempts: 0, availableAt: now, leaseId: null, leaseUntil: null, deadlineAt: now + input.deadline, trace: [], observations: [], toolCalls: {}, result: null, lastError: null, createdAt: timestamp, updatedAt: timestamp };
        created += 1;
      }
      return { created, existing };
    });
  }

  async claimDue({ leaseMs = 60_000 } = {}) {
    const now = this.#now();
    const result = await this.#mutate((state) => {
      let expired = false;
      for (const run of Object.values(state.runs)) {
        const due = (CLAIMABLE.has(run.status) && run.availableAt <= now) || (run.status === 'processing' && run.leaseUntil <= now);
        if (due && Number(run.deadlineAt) > 0 && Number(run.deadlineAt) <= now) {
          run.status = 'timed_out'; run.leaseId = null; run.leaseUntil = null;
          run.result = { status: 'handoff', reason: 'agent_deadline_exceeded' };
          run.updatedAt = new Date(now).toISOString(); expired = true;
        }
      }
      const candidate = Object.values(state.runs)
        .filter((run) => (CLAIMABLE.has(run.status) && run.availableAt <= now) || (run.status === 'processing' && run.leaseUntil <= now))
        // Live turns always take precedence; historical replay is best-effort.
        .sort((a, b) => modePriority(a.mode) - modePriority(b.mode) || a.availableAt - b.availableAt || a.createdAt.localeCompare(b.createdAt))[0];
      if (!candidate) return expired ? { noClaim: true } : null;
      candidate.status = 'processing'; candidate.attempts += 1; candidate.leaseId = randomUUID(); candidate.leaseUntil = now + leaseMs; candidate.updatedAt = new Date(now).toISOString();
      return external(candidate);
    }, (value) => value !== null);
    return result?.noClaim === true ? null : result;
  }

  async beginTool(runId, leaseId, input, { leaseMs = 60_000 } = {}) {
    const callId = bounded(input?.callId, 300); const tool = bounded(input?.tool, 64); const step = Number(input?.step);
    if (!callId || !tool || !Number.isSafeInteger(step) || step < 1 || step > 8) throw new TypeError('invalid agent tool call');
    return this.#finish(runId, leaseId, (run) => {
      run.toolCalls ??= {};
      const existing = run.toolCalls[callId];
      if (existing) return existing.status === 'completed'
        ? { state: 'replay', observation: clone(existing.observation) }
        : { state: 'unknown' };
      if (Object.keys(run.toolCalls).length >= 8) throw new TypeError('too many agent tool calls');
      run.trace = sanitizeTrace(input?.trace ?? []); run.observations = sanitizeObservations(input?.observations ?? []);
      run.toolCalls[callId] = { call_id: callId, step, tool, status: 'pending', started_at: new Date(this.#now()).toISOString(), completed_at: null, observation: null };
      run.leaseUntil = this.#now() + Math.max(15_000, Number(leaseMs));
      return { state: 'started' };
    }, { release: false });
  }

  async completeTool(runId, leaseId, callIdValue, observation, { leaseMs = 60_000 } = {}) {
    const callId = bounded(callIdValue, 300);
    return this.#finish(runId, leaseId, (run) => {
      const call = run.toolCalls?.[callId];
      if (!call || call.status !== 'pending') throw new Error(`agent tool call is not pending: ${callId}`);
      call.status = 'completed'; call.observation = sanitizeObservations([observation])[0]; call.completed_at = new Date(this.#now()).toISOString();
      run.observations = sanitizeObservations([...(run.observations ?? []), call.observation].slice(-8));
      run.leaseUntil = this.#now() + Math.max(15_000, Number(leaseMs));
      return { state: 'completed', observation: clone(call.observation) };
    }, { release: false });
  }

  async checkpoint(runId, leaseId, input, { leaseMs = 60_000 } = {}) {
    return this.#finish(runId, leaseId, (run) => {
      run.trace = sanitizeTrace(input?.trace ?? []); run.observations = sanitizeObservations(input?.observations ?? []);
      run.leaseUntil = this.#now() + Math.max(15_000, Number(leaseMs));
    }, { release: false });
  }

  async renewLease(runId, leaseId, { leaseMs = 60_000 } = {}) {
    return this.#finish(runId, leaseId, (run) => {
      run.leaseUntil = this.#now() + Math.max(15_000, Number(leaseMs));
    }, { release: false });
  }

  async complete(runId, leaseId, result = null) {
    return this.#finish(runId, leaseId, (run) => { run.status = 'completed'; run.result = result == null ? null : clone(result); run.lastError = null; });
  }

  async timeout(runId, leaseId, result = { status: 'handoff', reason: 'agent_deadline_exceeded' }) {
    return this.#finish(runId, leaseId, (run) => { run.status = 'timed_out'; run.result = clone(result); run.lastError = null; });
  }

  async defer(runId, leaseId, { delayMs = 2_000, reason = 'source_event_pending' } = {}) {
    return this.#finish(runId, leaseId, (run) => { run.status = 'queued'; run.availableAt = this.#now() + Math.max(500, Number(delayMs)); run.result = { deferred: bounded(reason, 100) }; });
  }

  async retry(runId, leaseId, error, { delayMs = 5_000, maxAttempts = 4 } = {}) {
    return this.#finish(runId, leaseId, (run) => { run.status = run.attempts >= maxAttempts ? 'failed' : 'retry'; run.availableAt = this.#now() + Math.max(1_000, Number(delayMs)); run.lastError = { name: bounded(error?.name, 80), message: bounded(error?.message ?? error, 300) }; });
  }

  async get(runId) { const state = await this.#read(); return state.runs[String(runId)] ? external(state.runs[String(runId)]) : null; }
  async list({ tenantId, limit = 100 } = {}) { const state = await this.#read(); return Object.values(state.runs).filter((run) => !tenantId || run.tenantId === String(tenantId)).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, Math.max(1, Math.min(500, Number(limit)))).map(external); }
  async health() { const state = await this.#read(); const counts = {}; for (const run of Object.values(state.runs)) counts[run.status] = (counts[run.status] ?? 0) + 1; return { revision: state.revision, runCounts: counts }; }

  async #finish(runId, leaseId, update, { release = true } = {}) {
    return this.#mutate((state) => {
      const run = state.runs[String(runId)];
      if (!run || run.status !== 'processing' || run.leaseId !== leaseId) throw new Error(`agent run lease mismatch: ${runId}`);
      const operationResult = update(run); if (release) { run.leaseId = null; run.leaseUntil = null; } run.updatedAt = new Date(this.#now()).toISOString(); return operationResult ?? external(run);
    });
  }

  async #mutate(operation, shouldWrite = () => true) {
    const pending = this.#chain.then(async () => { const state = await this.#read(); const result = operation(state); if (!shouldWrite(result)) return result; state.revision += 1; await this.#write(state); return result; });
    this.#chain = pending.catch(() => { this.#state = null; }); return pending;
  }
  async #read() { this.#state ??= await this.#readDisk(); return this.#state; }
  async #readDisk() { const state = JSON.parse(await readFile(this.#file, 'utf8')); if (state?.version !== STORE_VERSION || !state.runs || typeof state.runs !== 'object') throw new Error('unsupported or corrupt agent run store'); return state; }
  async #write(state) { const temporary = `${this.#file}.tmp`; await writeFile(temporary, JSON.stringify(state), { encoding: 'utf8', mode: 0o600 }); await rename(temporary, this.#file); this.#state = state; }
}
