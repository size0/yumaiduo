import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const STORE_VERSION = 2;
const STATUSES = new Set(['open', 'in_progress', 'resolved']);
const PRIORITIES = new Set(['low', 'normal', 'high', 'urgent']);
const SOURCES = new Set(['agent', 'agent_runtime', 'deterministic']);
function emptyState() { return { version: STORE_VERSION, revision: 0, tasks: {} }; }
function bounded(value, length) { return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, length); }
function labels(value) {
  if (!Array.isArray(value) || value.length > 8) throw new TypeError('invalid manual task labels');
  const result = [...new Set(value.map((item) => bounded(item, 32)).filter(Boolean))];
  if (result.length > 8) throw new TypeError('invalid manual task labels');
  return result;
}
function dueAt(value) {
  if (value == null || value === '') return null;
  const parsed = Date.parse(String(value));
  if (!Number.isFinite(parsed)) throw new TypeError('invalid manual task dueAt');
  return new Date(parsed).toISOString();
}
function normalizeTask(task) {
  return {
    ...task,
    assignee: bounded(task.assignee, 128) || null,
    priority: PRIORITIES.has(task.priority) ? task.priority : 'normal',
    labels: Array.isArray(task.labels) ? task.labels.slice(0, 8).map((item) => bounded(item, 32)).filter(Boolean) : [],
    dueAt: dueAt(task.dueAt),
    notes: Array.isArray(task.notes) ? task.notes.slice(-20).map((note) => ({
      id: bounded(note?.id, 160), author: bounded(note?.author, 128) || 'unknown', content: bounded(note?.content, 500), at: note?.at ?? null,
    })).filter((note) => note.id && note.content) : [],
    startedAt: task.startedAt ?? null,
  };
}
function external(taskValue, now = Date.now()) {
  const task = normalizeTask(taskValue);
  const due = task.dueAt ? Date.parse(task.dueAt) : null;
  const slaStatus = task.status === 'resolved' ? 'completed' : due != null && due < now ? 'overdue' : due != null ? 'within_sla' : 'not_set';
  return Object.freeze({
    task_id: task.taskId, tenant_id: task.tenantId, event_id: task.eventId,
    account_unb: task.accountUnb, chat_id: task.chatId, peer_unb: task.peerUnb,
    order_id: task.orderId || null, reason_code: task.reasonCode, summary: task.summary,
    source: task.source, status: task.status, assignee: task.assignee, priority: task.priority,
    labels: Object.freeze([...task.labels]), due_at: task.dueAt, sla_status: slaStatus,
    notes: Object.freeze(task.notes.map((note) => Object.freeze({ ...note }))),
    created_at: task.createdAt, started_at: task.startedAt, updated_at: task.updatedAt, resolved_at: task.resolvedAt,
  });
}

export class AgentManualTaskStore {
  #file; #now; #state = null; #chain = Promise.resolve();
  constructor(file, { now = () => Date.now() } = {}) { this.#file = file; this.#now = now; }
  async initialize() {
    await mkdir(dirname(this.#file), { recursive: true });
    try { this.#state = await this.#readDisk(); }
    catch (error) { if (error?.code !== 'ENOENT') throw error; this.#state = emptyState(); await this.#write(this.#state); }
  }
  async create(input) {
    const taskId = bounded(input?.taskId, 300); const tenantId = bounded(input?.tenantId, 128); const eventId = bounded(input?.eventId, 240);
    const accountUnb = bounded(input?.accountUnb, 128); const chatId = bounded(input?.chatId, 128); const peerUnb = bounded(input?.peerUnb, 128);
    const orderId = bounded(input?.orderId, 128); const reasonCode = bounded(input?.reasonCode, 100); const summary = bounded(input?.summary, 200); const source = bounded(input?.source, 32);
    const priority = input?.priority == null ? 'normal' : bounded(input.priority, 16); const taskLabels = input?.labels == null ? [] : labels(input.labels); const taskDueAt = dueAt(input?.dueAt);
    if (!taskId || !tenantId || !eventId || !accountUnb || !chatId || !peerUnb || !reasonCode || !summary || !SOURCES.has(source) || !PRIORITIES.has(priority)) throw new TypeError('invalid manual task');
    return this.#mutate((state) => {
      const existing = state.tasks[taskId];
      if (existing) return { created: false, task: external(existing, this.#now()) };
      const timestamp = new Date(this.#now()).toISOString();
      const task = { taskId, tenantId, eventId, accountUnb, chatId, peerUnb, orderId, reasonCode, summary, source, status: 'open', assignee: null, priority, labels: taskLabels, dueAt: taskDueAt, notes: [], startedAt: null, createdAt: timestamp, updatedAt: timestamp, resolvedAt: null };
      state.tasks[taskId] = task;
      return { created: true, task: external(task, this.#now()) };
    });
  }
  async update(tenantIdValue, taskIdValue, input = {}, { actorId = '' } = {}) {
    const tenantId = bounded(tenantIdValue, 128); const taskId = bounded(taskIdValue, 300);
    if (!input || typeof input !== 'object' || Array.isArray(input)) throw new TypeError('invalid manual task update');
    const allowed = new Set(['status', 'assignee', 'priority', 'labels', 'due_at', 'note']);
    if (Object.keys(input).some((key) => !allowed.has(key))) throw new TypeError('invalid manual task update');
    const requestedStatus = Object.hasOwn(input, 'status') ? bounded(input.status, 32) : null;
    const requestedPriority = Object.hasOwn(input, 'priority') ? bounded(input.priority, 16) : null;
    if (requestedStatus && !STATUSES.has(requestedStatus)) throw new TypeError('invalid manual task status');
    if (requestedPriority && !PRIORITIES.has(requestedPriority)) throw new TypeError('invalid manual task priority');
    const requestedLabels = Object.hasOwn(input, 'labels') ? labels(input.labels) : null;
    const requestedDueAt = Object.hasOwn(input, 'due_at') ? dueAt(input.due_at) : undefined;
    const note = Object.hasOwn(input, 'note') ? bounded(input.note, 500) : '';
    const actor = bounded(actorId, 128) || 'unknown';
    return this.#mutate((state) => {
      const task = state.tasks[taskId];
      if (!task || task.tenantId !== tenantId) throw new Error(`manual task not found: ${taskId}`);
      Object.assign(task, normalizeTask(task));
      if (task.status === 'resolved' && requestedStatus && requestedStatus !== 'resolved') throw new Error('resolved manual task cannot be reopened');
      const now = this.#now(); const timestamp = new Date(now).toISOString();
      if (requestedStatus) {
        task.status = requestedStatus;
        if (requestedStatus === 'in_progress' && !task.startedAt) task.startedAt = timestamp;
        if (requestedStatus === 'resolved' && !task.resolvedAt) task.resolvedAt = timestamp;
      }
      if (Object.hasOwn(input, 'assignee')) task.assignee = bounded(input.assignee, 128) || null;
      else if (requestedStatus === 'in_progress' && !task.assignee && actor !== 'unknown') task.assignee = actor;
      if (requestedPriority) task.priority = requestedPriority;
      if (requestedLabels) task.labels = requestedLabels;
      if (requestedDueAt !== undefined) task.dueAt = requestedDueAt;
      if (note) {
        task.notes.push({ id: `${now}:${task.notes.length + 1}`, author: actor, content: note, at: timestamp });
        task.notes = task.notes.slice(-20);
      }
      task.updatedAt = timestamp;
      return external(task, now);
    });
  }
  async resolve(tenantIdValue, taskIdValue) {
    return this.update(tenantIdValue, taskIdValue, { status: 'resolved' });
  }
  async get(tenantIdValue, taskIdValue) {
    const state = await this.#read(); const task = state.tasks[String(taskIdValue)];
    return task && task.tenantId === String(tenantIdValue) ? external(task, this.#now()) : null;
  }
  async list({ tenantId, status, assignee, priority, label, limit = 100 } = {}) {
    const state = await this.#read();
    return Object.values(state.tasks).filter((taskValue) => {
      const task = normalizeTask(taskValue);
      return (!tenantId || task.tenantId === String(tenantId)) && (!status || task.status === status)
        && (!assignee || task.assignee === String(assignee)) && (!priority || task.priority === priority)
        && (!label || task.labels.includes(String(label)));
    }).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, Math.max(1, Math.min(500, Number(limit)))).map((task) => external(task, this.#now()));
  }
  async health() { const state = await this.#read(); const counts = {}; for (const task of Object.values(state.tasks)) counts[task.status] = (counts[task.status] ?? 0) + 1; return { revision: state.revision, counts }; }
  async #mutate(operation) {
    const pending = this.#chain.then(async () => { const state = await this.#read(); const result = operation(state); state.version = STORE_VERSION; state.revision += 1; await this.#write(state); return result; });
    this.#chain = pending.catch(() => { this.#state = null; }); return pending;
  }
  async #read() { this.#state ??= await this.#readDisk(); return this.#state; }
  async #readDisk() {
    const state = JSON.parse(await readFile(this.#file, 'utf8'));
    if (![1, STORE_VERSION].includes(state?.version) || !state.tasks || typeof state.tasks !== 'object') throw new Error('unsupported or corrupt agent manual task store');
    if (state.version === 1) return { version: STORE_VERSION, revision: Number(state.revision) || 0, tasks: Object.fromEntries(Object.entries(state.tasks).map(([id, task]) => [id, normalizeTask(task)])) };
    return state;
  }
  async #write(state) { const temporary = `${this.#file}.tmp`; await writeFile(temporary, JSON.stringify(state), { encoding: 'utf8', mode: 0o600 }); await rename(temporary, this.#file); this.#state = state; }
}
