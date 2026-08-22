const SAFE_FACT_KEYS = new Set([
  'stage', 'city', 'cinema', 'movie', 'date', 'showtime', 'hall', 'seat_numbers', 'seat_preference',
  'ticket_count', 'quote_ticket_count', 'quote_unit_cents', 'quote_total_cents', 'quote_expires_at', 'quote_scope',
  'quote_confirmed', 'quote_draft', 'recognition_draft', 'quote_draft_recognition', 'available_wplus_seats', 'failure_code',
  'agent_intent', 'agent_confidence', 'agent_goal', 'agent_pending_action', 'agent_missing_fields', 'agent_last_status',
  'preference_recorded', 'circled_delivery_instruction_recorded', 'has_linked_order', 'unit_quote_cents', 'total_quote_cents', 'status',
  'lifecycle', 'paid', 'fulfilled', 'has_image', 'requested_ticket_count', 'has_typed_seat_instruction', 'has_active_quote',
  'known_identity_fields', 'missing_identity_fields',
]);

function text(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

function safeValue(value, depth = 0) {
  if (depth > 2 || value == null) return null;
  if (typeof value === 'string') return text(value, 300);
  if (typeof value === 'boolean') return value;
  if (Number.isFinite(value)) return Number(value);
  if (Array.isArray(value)) return value.slice(0, 20).map((item) => safeValue(item, depth + 1)).filter((item) => item !== null);
  if (typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).slice(0, 30).map(([key, item]) => [text(key, 64), safeValue(item, depth + 1)]).filter(([key, item]) => key && item !== null));
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
  return Object.keys(safeFields).length ? { fields: safeFields } : null;
}

function safeFactObject(value) {
  const facts = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const result = {};
  for (const [key, item] of Object.entries(facts)) {
    if (!SAFE_FACT_KEYS.has(key)) continue;
    const normalized = key === 'quote_draft' ? safeQuoteDraft(item) : safeValue(item);
    if (normalized !== null) result[key] = normalized;
  }
  return result;
}

function safeFacts(state) {
  const facts = state?.facts && typeof state.facts === 'object' && !Array.isArray(state.facts) ? state.facts : {};
  const result = {};
  for (const [key, value] of Object.entries(facts)) {
    if (!SAFE_FACT_KEYS.has(key)) continue;
    const normalized = key === 'quote_draft' ? safeQuoteDraft(value) : safeValue(value);
    if (normalized !== null) result[key] = normalized;
  }
  if (facts.order_id) result.has_linked_order = true;
  return result;
}

function safeHistory(state, latestMessage) {
  const entries = Array.isArray(state?.messages) ? state.messages : [];
  const history = entries.slice(-20).map((item) => {
    const role = item?.role === 'seller' ? 'seller' : 'buyer';
    const requestedSource = text(item?.source, 32);
    const source = role === 'buyer'
      ? 'buyer'
      : ['plugin', 'external_seller', 'unknown'].includes(requestedSource) ? requestedSource : 'unknown';
    return { role, content: text(item?.content ?? item?.text, 1_000), source };
  }).filter((item) => item.content);
  if (!history.some((item) => item.role === 'buyer' && item.content === latestMessage)) history.push({ role: 'buyer', content: latestMessage, source: 'buyer' });
  return history.slice(-20);
}

function safeObservations(values) {
  if (!Array.isArray(values)) return [];
  return values.slice(-8).map((item) => ({
    status: ['success', 'warning', 'error'].includes(item?.status) ? item.status : 'error',
    tool: text(item?.tool, 64) || 'unknown',
    summary: text(item?.summary, 200),
    facts: safeFactObject(item?.facts),
    next_actions: Array.isArray(item?.next_actions) ? item.next_actions.slice(0, 8).map((action) => text(action, 64)).filter(Boolean) : [],
    ...(item?.stop_reason ? { stop_reason: text(item.stop_reason, 100) } : {}),
  }));
}

export function createConversationAgentClient(config, { fetchImpl = globalThis.fetch } = {}) {
  const agent = config?.conversationAgent;
  if (!agent) return null;
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');

  async function plan(input) {
    const latestMessage = text(input?.latest_message, 1_000) || '[图片或非文本消息]';
    const response = await fetchImpl(agent.url, {
      method: 'POST',
      headers: { accept: 'application/json', 'content-type': 'application/json', 'x-wanda-preview-key': agent.ingestKey },
      body: JSON.stringify({
        event_id: text(input?.event_id, 200),
        tenant_id: text(input?.tenant_id, 128),
        latest_message: latestMessage,
        history: safeHistory(input?.state, latestMessage),
        state: { stage: text(input?.state?.facts?.stage, 64), facts: safeFacts(input?.state) },
        observations: safeObservations(input?.observations),
        has_image: input?.has_image === true,
      }),
      signal: input?.signal instanceof AbortSignal
        ? AbortSignal.any([input.signal, AbortSignal.timeout(65_000)])
        : AbortSignal.timeout(65_000),
    });
    if (!response.ok) throw new Error(`conversation agent failed with HTTP ${response.status}`);
    const result = await response.json();
    if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('conversation agent returned an invalid response');
    if (result.status === 'failed') throw new Error(`conversation agent planning failed: ${text(result.failure_code, 100) || 'unknown'}`);
    if (result.status !== 'planned' || !result.plan || typeof result.plan !== 'object' || Array.isArray(result.plan)) {
      throw new Error('conversation agent returned an invalid response');
    }
    return result.plan;
  }

  return Object.freeze({ plan });
}
