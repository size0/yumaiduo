import { normalizeAgentPlan } from '../agent/agent-schema.mjs';
import { normalizeAiShadowAdvisory } from './ai-shadow-advisory-schema.mjs';

const SENSITIVE_NESTED_KEY = /(?:token|secret|authorization|cookie|api[_-]?key|phone|mobile|order[_-]?id)/iu;
const SAFE_FACT_KEYS = new Set([
  'stage', 'city', 'cinema', 'movie', 'date', 'showtime', 'hall', 'seat_numbers', 'seat_preference',
  'ticket_count', 'quote_ticket_count', 'quote_unit_cents', 'quote_total_cents', 'quote_expires_at', 'quote_scope',
  'quote_confirmed', 'quote_draft', 'recognition_draft', 'quote_draft_recognition', 'available_wplus_seats', 'failure_code',
  'agent_intent', 'agent_confidence', 'agent_goal', 'agent_pending_action', 'agent_missing_fields', 'agent_last_status',
  'preference_recorded', 'circled_delivery_instruction_recorded', 'has_linked_order', 'unit_quote_cents', 'total_quote_cents', 'status',
  'lifecycle', 'paid', 'fulfilled', 'has_image', 'requested_ticket_count', 'has_typed_seat_instruction', 'has_active_quote',
  'known_identity_fields', 'missing_identity_fields',
]);

/**
 * Provider-neutral AI planning boundary. It exposes no send, tool, order, or
 * pricing capability. All providers receive only a bounded source-time
 * snapshot and must return a typed Agent plan that is still subject to the
 * deterministic policy engine and tool contracts.
 */
export function createAiOrchestrator({ primaryProvider, shadowProvider = null } = {}) {
  if (primaryProvider == null) return null;
  if (typeof primaryProvider?.plan !== 'function') throw new TypeError('primary AI provider must implement plan');
  if (shadowProvider != null && typeof shadowProvider?.evaluate !== 'function') {
    throw new TypeError('Shadow AI provider must implement evaluate');
  }

  async function plan(input) {
    const snapshot = boundedSourceSnapshot(input);
    return normalizeAgentPlan(await primaryProvider.plan(snapshot));
  }

  if (!shadowProvider) return Object.freeze({ plan });
  async function evaluateShadow(input) {
    const snapshot = boundedSourceSnapshot(input);
    return normalizeAiShadowAdvisory(await shadowProvider.evaluate(snapshot));
  }
  return Object.freeze({ plan, evaluateShadow });
}

function boundedSourceSnapshot(input = {}) {
  const latestMessage = boundedText(input?.latest_message, 1_000) || '[图片或非文本消息]';
  const snapshot = {
    event_id: boundedText(input?.event_id, 200),
    tenant_id: boundedText(input?.tenant_id, 128),
    latest_message: latestMessage,
    has_image: input?.has_image === true,
    state: Object.freeze({
      facts: safeFacts(input?.state),
      messages: safeMessages(input?.state),
    }),
    observations: safeObservations(input?.observations),
    ...(isAbortSignal(input?.signal) ? { signal: input.signal } : {}),
  };
  return Object.freeze(snapshot);
}

function boundedText(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

function safeValue(value, depth = 0) {
  if (depth > 2 || value == null) return null;
  if (typeof value === 'string') return boundedText(value, 300);
  if (typeof value === 'boolean') return value;
  if (Number.isFinite(value)) return Number(value);
  if (Array.isArray(value)) {
    return Object.freeze(value.slice(0, 20).map((item) => safeValue(item, depth + 1)).filter((item) => item !== null));
  }
  if (typeof value === 'object') {
    return Object.freeze(Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !SENSITIVE_NESTED_KEY.test(key))
        .slice(0, 30)
        .map(([key, item]) => [boundedText(key, 64), safeValue(item, depth + 1)])
        .filter(([key, item]) => key && item !== null),
    ));
  }
  return null;
}

function safeQuoteDraft(value) {
  const fields = value?.fields && typeof value.fields === 'object' && !Array.isArray(value.fields) ? value.fields : {};
  const safeFields = {};
  for (const key of ['city', 'cinema', 'movie', 'date', 'showtime', 'hall', 'ticket_count']) {
    const candidate = fields[key]?.value ?? fields[key];
    const normalized = safeValue(candidate);
    if (normalized !== null && normalized !== '') safeFields[key] = normalized;
  }
  return Object.keys(safeFields).length ? Object.freeze({ fields: Object.freeze(safeFields) }) : null;
}

function safeFactObject(value) {
  const facts = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const result = {};
  for (const [key, item] of Object.entries(facts)) {
    if (!SAFE_FACT_KEYS.has(key)) continue;
    const normalized = key === 'quote_draft' ? safeQuoteDraft(item) : safeValue(item);
    if (normalized !== null) result[key] = normalized;
  }
  return Object.freeze(result);
}

function safeFacts(state) {
  const facts = state?.facts && typeof state.facts === 'object' && !Array.isArray(state.facts) ? state.facts : {};
  const result = { ...safeFactObject(facts) };
  if (facts.order_id) result.has_linked_order = true;
  return Object.freeze(result);
}

function safeMessages(state) {
  const entries = Array.isArray(state?.messages) ? state.messages : [];
  return Object.freeze(entries.slice(-20).map((item) => {
    const role = item?.role === 'seller' ? 'seller' : 'buyer';
    const requestedSource = boundedText(item?.source, 32);
    const source = role === 'buyer'
      ? 'buyer'
      : ['plugin', 'external_seller', 'unknown'].includes(requestedSource) ? requestedSource : 'unknown';
    return Object.freeze({ role, content: boundedText(item?.content ?? item?.text, 1_000), source });
  }).filter((item) => item.content));
}

function safeObservations(values) {
  if (!Array.isArray(values)) return Object.freeze([]);
  return Object.freeze(values.slice(-8).map((item) => Object.freeze({
    status: ['success', 'warning', 'error'].includes(item?.status) ? item.status : 'error',
    tool: boundedText(item?.tool, 64) || 'unknown',
    summary: boundedText(item?.summary, 200),
    facts: safeFactObject(item?.facts),
    next_actions: Object.freeze(Array.isArray(item?.next_actions)
      ? item.next_actions.slice(0, 8).map((action) => boundedText(action, 64)).filter(Boolean)
      : []),
    ...(item?.stop_reason ? { stop_reason: boundedText(item.stop_reason, 100) } : {}),
  })));
}

function isAbortSignal(value) {
  return typeof AbortSignal !== 'undefined' && value instanceof AbortSignal;
}
