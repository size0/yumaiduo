import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const LABELS = new Set(['aligned', 'safer_than_human', 'missed_context', 'wrong_intent', 'wrong_tool', 'unsafe_claim', 'unnecessary_handoff', 'needs_policy']);
const TARGETS = new Set(['none', 'knowledge_base', 'reply_template', 'business_rule', 'tool_gate']);
const FACT_FIELDS = ['city', 'cinema', 'movie', 'date', 'showtime', 'hall', 'ticket_count', 'quote_ticket_count', 'quote_unit_cents', 'quote_total_cents', 'pricing_rule_version', 'stage', 'seat_delivery_instruction', 'has_linked_order'];
function text(value, limit) {
  return String(value ?? '').replace(/https?:\/\/\S+/giu, '[链接]').replace(/1\d{10}/gu, '[手机号]').replace(/\b\d{12,}\b/gu, '[编号]').replace(/\s+/gu, ' ').trim().slice(0, limit);
}
function boundedObject(input, fields) {
  const output = {};
  for (const field of fields) if (input?.[field] != null) output[field] = typeof input[field] === 'string' ? text(input[field], 120) : input[field];
  return output;
}
function comparison(input, now) {
  const automatic = input.automaticComparison ?? {};
  const suggested = LABELS.has(String(automatic.suggested_label)) ? String(automatic.suggested_label) : 'needs_policy';
  return {
    id: text(input.comparisonId, 160), tenant_id: text(input.tenantId, 128), source_event_id: text(input.sourceEventId, 200),
    account_unb: text(input.accountUnb, 128), chat_id: text(input.chatId, 128), peer_unb: text(input.peerUnb, 128),
    buyer_turn: { summary: text(input.buyerTurn?.summary, 500), has_image: input.buyerTurn?.has_image === true },
    conversation_facts: boundedObject(input.conversationFacts, FACT_FIELDS),
    quote_order_state: boundedObject(input.quoteOrderState, ['quote_status', 'order_lifecycle', 'paid', 'fulfilled']),
    agent: { intent: text(input.agent?.intent, 32), action: text(input.agent?.action, 64), proposed_reply: text(input.agent?.proposed_reply, 500), goal: text(input.agent?.goal, 160) },
    human: { reply: text(input.human?.reply, 500), intent: text(input.human?.intent, 32), expected_action: text(input.human?.expected_action, 64) },
    outcome: boundedObject(input.outcome, ['stage', 'quote_status', 'order_lifecycle', 'paid', 'fulfilled']),
    automatic_comparison: {
      intent_aligned: typeof automatic.intent_aligned === 'boolean' ? automatic.intent_aligned : null,
      tool_aligned: typeof automatic.tool_aligned === 'boolean' ? automatic.tool_aligned : null,
      facts_aligned: typeof automatic.facts_aligned === 'boolean' ? automatic.facts_aligned : null,
      risk_aligned: typeof automatic.risk_aligned === 'boolean' ? automatic.risk_aligned : null,
      reply_strategy_aligned: typeof automatic.reply_strategy_aligned === 'boolean' ? automatic.reply_strategy_aligned : null,
      suggested_label: suggested,
    },
    review: { status: 'unreviewed', label: null, target: 'none', note: '', reviewed_at: null },
    created_at: now, updated_at: now,
  };
}

export class AgentHumanComparisonStore {
  constructor(file, { now = () => Date.now() } = {}) { this.file = file; this.now = now; this.chain = Promise.resolve(); }
  async capture(input) {
    const candidate = comparison(input, this.now());
    if (!candidate.id || !candidate.tenant_id || !candidate.human.reply) throw new TypeError('invalid human comparison');
    return this.#mutate((state) => {
      const existing = state[candidate.id];
      if (existing) {
        if (existing.review?.status === 'unreviewed') {
          state[candidate.id] = {
            ...candidate,
            created_at: existing.created_at,
            review: existing.review,
            updated_at: this.now(),
          };
        } else {
          existing.outcome = candidate.outcome;
          existing.quote_order_state = candidate.quote_order_state;
          existing.updated_at = this.now();
        }
        return { created: false, record: structuredClone(state[candidate.id]) };
      }
      state[candidate.id] = candidate;
      return { created: true, record: structuredClone(candidate) };
    });
  }
  async list({ tenantId, status, limit = 100 } = {}) {
    const state = await this.#read();
    return Object.values(state).filter((item) => (!tenantId || item.tenant_id === String(tenantId)) && (!status || item.review?.status === status))
      .sort((left, right) => Number(right.updated_at) - Number(left.updated_at)).slice(0, Math.max(1, Math.min(500, Number(limit)))).map((item) => structuredClone(item));
  }
  async review(tenantId, id, input = {}) {
    const label = String(input.label ?? ''); const target = String(input.target ?? 'none'); const note = text(input.note, 500);
    if (!LABELS.has(label) || !TARGETS.has(target)) throw new TypeError('invalid human comparison review');
    return this.#mutate((state) => {
      const record = state[String(id)];
      if (!record || record.tenant_id !== String(tenantId)) throw new Error('human comparison not found');
      record.review = { status: 'reviewed', label, target, note, reviewed_at: this.now() }; record.updated_at = this.now();
      return structuredClone(record);
    });
  }
  async health() { const records = await this.list({ limit: 500 }); return { counts: { total: records.length, unreviewed: records.filter((item) => item.review.status === 'unreviewed').length } }; }
  async #read() { try { const value = JSON.parse(await readFile(this.file, 'utf8')); return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; } catch (error) { if (error?.code === 'ENOENT') return {}; throw error; } }
  async #mutate(fn) { const operation = this.chain.then(async () => { const state = await this.#read(); const result = fn(state); await mkdir(dirname(this.file), { recursive: true }); const temporary = `${this.file}.tmp`; await writeFile(temporary, JSON.stringify(state), { encoding: 'utf8', mode: 0o600 }); await rename(temporary, this.file); return result; }); this.chain = operation.catch(() => {}); return operation; }
}
