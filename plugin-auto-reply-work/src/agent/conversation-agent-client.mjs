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

  async function complete(input) {
    const messages = Array.isArray(input?.messages) ? input.messages.map(safeNativeMessage) : [];
    if (!messages.length) throw new TypeError('native agent messages are required');
    const nativeUrl = agent.nativeUrl || String(agent.url).replace(/\/api\/agents\/turn(?:\?.*)?$/u, '/api/agents/v2/completions');
    const response = await fetchImpl(nativeUrl, {
      method: 'POST',
      headers: { accept: 'application/json', 'content-type': 'application/json', 'x-wanda-preview-key': agent.ingestKey, 'x-yumaiduo-tenant-id': text(input?.tenant_id, 128) },
      body: JSON.stringify({
        tenant_id: text(input?.tenant_id, 128),
        conversation_id: text(input?.conversation_id, 240),
        run_id: text(input?.run_id, 240),
        messages,
        available_tools: Array.isArray(input?.available_tools)
          ? [...new Set(input.available_tools.map((value) => text(value, 64)).filter(Boolean))].slice(0, 32)
          : [],
        knowledge_scene: ['general', 'intake', 'quote_followup', 'order', 'fulfillment', 'aftersale'].includes(input?.knowledge_scene) ? input.knowledge_scene : 'general',
        knowledge_snapshot: { version: '', entries: [] },
        reasoning: safeReasoning(input?.reasoning),
      }),
      signal: input?.signal instanceof AbortSignal
        ? AbortSignal.any([input.signal, AbortSignal.timeout(245_000)])
        : AbortSignal.timeout(245_000),
    });
    if (!response.ok) throw new Error(`native conversation agent failed with HTTP ${response.status}`);
    const result = await response.json();
    if (!result || typeof result !== 'object' || Array.isArray(result) || !result.assistant) {
      throw new Error('native conversation agent returned an invalid response');
    }
    return Object.freeze({
      assistant: safeNativeMessage(result.assistant),
      finish_reason: text(result.finish_reason, 64),
      model: text(result.model, 200),
      versions: result.versions && typeof result.versions === 'object' ? structuredClone(result.versions) : {},
      usage: result.usage && typeof result.usage === 'object' ? structuredClone(result.usage) : {},
      latency_ms: Number.isFinite(Number(result.latency_ms)) ? Number(result.latency_ms) : null,
      request_id: text(result.request_id, 200),
      reasoning: result.reasoning && typeof result.reasoning === 'object' ? structuredClone(result.reasoning) : {},
    });
  }

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

  return Object.freeze({ plan, complete });
}

function safeNativeMessage(value) {
  const role = ['system', 'user', 'assistant', 'tool'].includes(value?.role) ? value.role : null;
  if (!role) throw new TypeError('invalid native agent message role');
  let content = value?.content ?? '';
  if (Array.isArray(content)) {
    content = content.slice(0, 20).map((part) => {
      if (!part || typeof part !== 'object' || Array.isArray(part)) throw new TypeError('invalid native message content');
      const type = text(part.type, 32);
      if (type === 'text') return { type, text: text(part.text, 10_000) };
      const url = text(part.image_url?.url ?? part.image_url, 2_000);
      if (type === 'image_url' && /^https:\/\//iu.test(url)) return { type, image_url: { url } };
      throw new TypeError('unsupported native message content');
    });
  } else if (content !== null) content = String(content).slice(0, 100_000);
  const message = { role, content };
  if (role === 'assistant') {
    message.tool_calls = (Array.isArray(value?.tool_calls) ? value.tool_calls : []).slice(0, 12).map((call) => ({
      id: text(call?.id, 200), type: 'function', function: {
        name: text(call?.function?.name, 64), arguments: String(call?.function?.arguments ?? '{}').slice(0, 16_000),
      },
    }));
    if (message.tool_calls.some((call) => !call.id || !call.function.name)) throw new TypeError('invalid native tool call');
  }
  if (role === 'tool') {
    message.tool_call_id = text(value?.tool_call_id, 200);
    if (!message.tool_call_id) throw new TypeError('native tool result requires tool_call_id');
  }
  if (value?.name) message.name = text(value.name, 64);
  return message;
}

function safeKnowledgeSnapshot(value) {
  const input = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  return {
    version: text(input.version, 128),
    entries: Array.isArray(input.entries) ? input.entries.slice(0, 100).map((entry) => text(entry, 4_000)).filter(Boolean) : [],
  };
}

function safeReasoning(value) {
  const input = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const max = Number(input.max_output_tokens);
  return {
    enabled: input.enabled !== false,
    effort: ['low', 'medium', 'high'].includes(input.effort) ? input.effort : 'medium',
    max_output_tokens: Number.isSafeInteger(max) && max >= 256 && max <= 4096 ? max : 1600,
  };
}
