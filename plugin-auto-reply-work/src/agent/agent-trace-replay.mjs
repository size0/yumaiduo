function text(value, length = 500) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, length);
}

function timestampMs(value) {
  const parsed = Date.parse(String(value ?? ''));
  return Number.isFinite(parsed) ? parsed : null;
}

function duration(start, end) {
  const left = timestampMs(start); const right = timestampMs(end);
  return left != null && right != null && right >= left ? right - left : null;
}

function sourceSummary(payload = {}) {
  const imageCount = Array.isArray(payload.imageUrls)
    ? payload.imageUrls.filter((value) => typeof value === 'string' && value.trim()).length
    : 0;
  const summary = text(payload.content ?? payload.text, 120)
    .replace(/https?:\/\/[^\s，。；！？,;]+/giu, '[链接]')
    .replace(/(?<!\d)1\d{10}(?!\d)/gu, '[手机号]')
    .replace(/(?<!\d)\d{8,}(?!\d)/gu, '[编号]');
  if (summary && imageCount) return `买家说：${summary}；并发送${imageCount}张图片`;
  if (summary) return `买家说：${summary}`;
  if (imageCount) return `买家发送${imageCount}张图片`;
  return '本轮没有可展示的买家文字或图片';
}

function buyerLabel(payload = {}) {
  const name = [payload.peerNick, payload.peerNickname, payload.buyerNick, payload.buyerNickname]
    .map((value) => text(value, 80)).find(Boolean);
  if (name) return name;
  const peer = text(payload.peerUnb ?? payload.peer_unb, 128);
  return peer ? `买家 …${peer.slice(-4)}` : '买家（身份未知）';
}

function publicFacts(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const facts = {};
  for (const [key, fact] of Object.entries(value).slice(0, 30)) {
    if (key === '_quote_input') continue;
    if (key === '_quote_delivery') {
      const delivery = fact && typeof fact === 'object' && !Array.isArray(fact) ? fact : {};
      facts.quote_delivery = Object.fromEntries([
        'type', 'unit_quote_cents', 'total_quote_cents', 'ticket_count', 'pricing_rule_version',
        'cinema', 'movie', 'date', 'showtime', 'hall', 'quote_scope', 'pricing_source',
      ].filter((field) => delivery[field] != null).map((field) => [field, structuredClone(delivery[field])]));
      continue;
    }
    facts[text(key, 64)] = structuredClone(fact);
  }
  return facts;
}

function publicObservation(value = {}) {
  return Object.freeze({
    status: ['success', 'warning', 'error'].includes(value.status) ? value.status : 'error',
    tool: text(value.tool, 64) || 'unknown', summary: text(value.summary, 200),
    facts: Object.freeze(publicFacts(value.facts)),
    authoritative_reply: text(value.authoritative_reply, 1_000),
    next_actions: Object.freeze(Array.isArray(value.next_actions) ? value.next_actions.slice(0, 8).map((item) => text(item, 64)).filter(Boolean) : []),
    stop_reason: text(value.stop_reason, 100) || null,
  });
}

function authoritativeSummary(result = {}) {
  if (result.preview_status === 'preview_ready') return '确定性系统：已形成实时报价';
  if (result.quote_failure_code) return `确定性系统安全停止：${text(result.quote_failure_code, 100)}`;
  return '确定性系统：本轮没有形成可对照的交易结果';
}

export function agentTraceReplayFrom(run, event) {
  if (!run || !event) throw new TypeError('agent trace replay source is required');
  if (String(run.tenant_id ?? '') !== String(event.envelope?.tenantId ?? '')) throw new Error('agent trace tenant mismatch');
  const toolCalls = Array.isArray(run.tool_calls) ? run.tool_calls : [];
  const callByStep = new Map(toolCalls.map((call) => [Number(call.step), call]));
  const trace = Array.isArray(run.trace) ? run.trace : Array.isArray(run.result?.trace) ? run.result.trace : [];
  const steps = trace.slice(0, 8).map((plan, index) => {
    const step = Number.isSafeInteger(Number(plan?.step)) ? Number(plan.step) : index + 1;
    const call = callByStep.get(step);
    const observation = call?.observation ? publicObservation(call.observation) : null;
    return Object.freeze({
      step, plan: Object.freeze({
        action: text(plan?.action, 64), intent: text(plan?.intent, 32),
        confidence: Number.isFinite(Number(plan?.confidence)) ? Number(plan.confidence) : null,
        goal: text(plan?.goal, 160),
        missing_fields: Object.freeze(Array.isArray(plan?.missing_fields) ? plan.missing_fields.slice(0, 10).map((item) => text(item, 64)).filter(Boolean) : []),
      }),
      tool: call ? Object.freeze({
        name: text(call.tool, 64), status: text(call.status, 32),
        started_at: call.started_at ?? null, completed_at: call.completed_at ?? null,
        duration_ms: duration(call.started_at, call.completed_at),
      }) : null,
      observation,
    });
  });
  const journaledIds = new Set(toolCalls.map((call) => String(call.observation?.tool ?? call.tool ?? '')));
  const systemObservations = (Array.isArray(run.observations) ? run.observations : [])
    .filter((observation) => observation?.tool === 'policy_guard' || !journaledIds.has(String(observation?.tool ?? '')))
    .slice(-8).map(publicObservation);
  const payload = event.envelope?.payload ?? {};
  return Object.freeze({
    run_id: String(run.run_id), event_key: String(run.event_key), tenant_id: String(run.tenant_id),
    mode: text(run.mode, 32), status: text(run.status, 32), attempts: Number(run.attempts ?? 0),
    runtime_version: text(run.result?.runtime_version ?? 'legacy', 100),
    created_at: run.created_at ?? null, updated_at: run.updated_at ?? null,
    duration_ms: duration(run.created_at, run.updated_at),
    source: Object.freeze({
      event_id: text(event.envelope?.id, 200), buyer_label: buyerLabel(payload),
      turn_summary: sourceSummary(payload), has_image: Array.isArray(payload.imageUrls) && payload.imageUrls.length > 0,
      authoritative_summary: authoritativeSummary(event.result),
    }),
    steps: Object.freeze(steps), system_observations: Object.freeze(systemObservations),
    outcome: Object.freeze({
      status: text(run.result?.status ?? run.status, 32), reason: text(run.result?.reason, 100) || null,
      authoritative_outcome: text(run.result?.authoritative_outcome ?? 'not_available', 64),
      proposed_reply: text(run.result?.proposed_reply, 500),
    }),
  });
}
