import { NATIVE_AGENT_TOOL_NAMES, nativeToolIsReadOnly, nativeToolsCanRunInParallel, resolveNativeTool } from './native-tool-registry.mjs';

function text(value, maximum = 1_000) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maximum);
}

function safeFacts(value, depth = 0) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || depth > 3) return {};
  return Object.fromEntries(Object.entries(value).slice(0, 50).map(([key, item]) => {
    if (item == null) return [text(key, 64), null];
    if (typeof item === 'string') return [text(key, 64), text(item, 2_000)];
    if (typeof item === 'boolean' || Number.isFinite(item)) return [text(key, 64), item];
    if (Array.isArray(item)) return [text(key, 64), item.slice(0, 50).map((entry) => (
      entry && typeof entry === 'object' ? safeFacts(entry, depth + 1) : typeof entry === 'string' ? text(entry, 500) : entry
    ))];
    return [text(key, 64), safeFacts(item, depth + 1)];
  }).filter(([key]) => key));
}

export function normalizeNativeObservation(value, tool) {
  const input = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const status = ['success', 'warning', 'error', 'pending'].includes(input.status) ? input.status : 'error';
  const facts = safeFacts(input.facts);
  const authoritativeReply = text(input.authoritative_reply, 2_000);
  if (authoritativeReply) facts.verified_reply = authoritativeReply;
  return Object.freeze({
    status,
    code: text(input.code ?? input.stop_reason ?? input.facts?.failure_code ?? `${tool}_${status}`, 100),
    summary: text(input.summary, 500) || `${tool} returned ${status}`,
    facts: Object.freeze(facts),
    missing: Object.freeze(Array.isArray(input.missing) ? input.missing.slice(0, 20).map((item) => text(item, 100)).filter(Boolean) : []),
    retryable: input.retryable === true,
  });
}

function projectionMessage(context) {
  const facts = safeFacts(context?.state?.facts);
  if (!Object.keys(facts).length) return null;
  return {
    role: 'user',
    content: `当前会话权威投影（每轮刷新；交易事实优先于历史文本）：${JSON.stringify(facts)}`,
  };
}

function historyMessages(context) {
  const history = Array.isArray(context?.state?.messages) ? context.state.messages : [];
  const messages = history.map((item) => {
    if (item?.role === 'tool') return item.tool_call_id ? {
      role: 'tool', content: String(item.content ?? '').slice(0, 100_000),
      tool_call_id: text(item.tool_call_id, 200), ...(item.name ? { name: text(item.name, 64) } : {}),
    } : null;
    if (item?.role === 'assistant' && Array.isArray(item.tool_calls)) return {
      role: 'assistant', content: String(item.content ?? '').slice(0, 10_000), tool_calls: structuredClone(item.tool_calls.slice(0, 12)),
    };
    const role = ['seller', 'assistant'].includes(item?.role) ? 'assistant' : 'user';
    const contentText = text(item?.content ?? item?.text, 10_000) || (item?.image === true ? '[图片]' : '');
    const imageUrls = Array.isArray(item?.image_urls)
      ? item.image_urls.map((url) => text(url, 2_000)).filter((url) => /^https:\/\//iu.test(url)).slice(0, 4)
      : [];
    if (role === 'user' && imageUrls.length) {
      return { role, content: [
        ...(contentText ? [{ type: 'text', text: contentText }] : []),
        ...imageUrls.map((url) => ({ type: 'image_url', image_url: { url } })),
      ] };
    }
    return contentText ? { role, content: contentText } : null;
  }).filter(Boolean);
  const latest = text(context?.latest_message, 10_000) || '[图片或非文本消息]';
  const last = messages.at(-1);
  const lastText = typeof last?.content === 'string'
    ? last.content
    : Array.isArray(last?.content) ? String(last.content.find((part) => part?.type === 'text')?.text ?? '') : '';
  if (last?.role !== 'user' || lastText !== latest) messages.push({ role: 'user', content: latest });

  // Keep a dynamic character budget rather than a fixed message count. Durable
  // storage remains complete; older raw turns are folded behind the current
  // facts/preferences projection supplied in projectionMessage().
  const budget = 80_000;
  let used = 0;
  let start = messages.length;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const size = JSON.stringify(messages[index]).length;
    if (used + size > budget) break;
    used += size;
    start = index;
  }
  start = Math.max(start, messages.length - 200);
  while (start < messages.length && messages[start]?.role === 'tool') start += 1;
  const recent = messages.slice(start);
  if (start > 0) recent.unshift({
    role: 'user',
    content: `较早的${start}条原始消息已滚动折叠；买家需求、偏好、未完成事项及当前交易事实保留在本轮权威投影中。`,
  });
  return recent;
}

function resumedToolMessages(observations) {
  return (Array.isArray(observations) ? observations : []).flatMap((observation, index) => {
    const tool = text(observation?.tool, 64) || 'unknown';
    const callId = `resume-${index + 1}`;
    const normalized = normalizeNativeObservation(observation, tool);
    return [
      { role: 'assistant', content: '', tool_calls: [{ id: callId, type: 'function', function: { name: tool, arguments: '{}' } }] },
      { role: 'tool', tool_call_id: callId, name: tool, content: JSON.stringify(normalized) },
    ];
  });
}

function chineseInteger(value) {
  const digits = { 零: 0, 〇: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
  const units = { 十: 10, 百: 100, 千: 1_000, 万: 10_000 };
  let total = 0; let section = 0; let number = 0;
  for (const character of String(value ?? '')) {
    if (Object.hasOwn(digits, character)) { number = digits[character]; continue; }
    const unit = units[character];
    if (!unit) return null;
    if (unit === 10_000) { section = (section + number) * unit; total += section; section = 0; number = 0; }
    else { section += (number || 1) * unit; number = 0; }
  }
  const result = total + section + number;
  return Number.isSafeInteger(result) ? result : null;
}

function authoritativeTransactionFacts(context, observations) {
  const merged = { ...safeFacts(context?.state?.facts) };
  for (const observation of Array.isArray(observations) ? observations : []) Object.assign(merged, safeFacts(observation?.facts));
  return merged;
}

export function transactionFactConflicts(replyValue, context, observations = []) {
  const reply = String(replyValue ?? '');
  const facts = authoritativeTransactionFacts(context, observations);
  const conflicts = [];
  const observedAuthoritativeAmount = (Array.isArray(observations) ? observations : []).some((item) => item?.status === 'success'
    && [item?.facts?.quote_total_cents, item?.facts?.total_quote_cents, item?.facts?.unit_quote_cents].some((value) => Number.isSafeInteger(Number(value))));
  const quoteStillActive = Number(facts.quote_expires_at ?? 0) > Number(context?.now ?? Date.now())
    || facts.quote_succeeded === true || facts.price_change_requested === true || observedAuthoritativeAmount;
  const allowedAmounts = new Set((quoteStillActive ? [
    facts.quote_unit_cents, facts.unit_quote_cents, facts.quote_total_cents, facts.total_quote_cents,
    facts.payment_cents, facts.verified_amount_cents,
  ] : [facts.payment_cents, facts.verified_amount_cents]).map(Number).filter((value) => Number.isSafeInteger(value) && value >= 0));
  for (const match of reply.matchAll(/(?:^|[^\d])([0-9]+(?:\.[0-9]{1,2})?)\s*元/gu)) {
    const cents = Math.round(Number(match[1]) * 100);
    if (!allowedAmounts.has(cents)) conflicts.push({ field: 'amount_cents', stated: cents, allowed: [...allowedAmounts] });
  }
  for (const match of reply.matchAll(/([零〇一二两三四五六七八九十百千万]+)\s*元/gu)) {
    const yuan = chineseInteger(match[1]);
    if (yuan != null && !allowedAmounts.has(yuan * 100)) conflicts.push({ field: 'amount_cents', stated: yuan * 100, allowed: [...allowedAmounts] });
  }
  const authoritativeCount = Number(facts.quote_ticket_count ?? facts.ticket_count);
  if (Number.isSafeInteger(authoritativeCount) && authoritativeCount > 0) {
    for (const match of reply.matchAll(/(?:^|[^\d])([1-9][0-9]?)\s*张/gu)) {
      const stated = Number(match[1]);
      if (stated !== authoritativeCount) conflicts.push({ field: 'ticket_count', stated, allowed: [authoritativeCount] });
    }
  }
  if (Number.isSafeInteger(authoritativeCount) && authoritativeCount > 0) {
    for (const match of reply.matchAll(/([零〇一二两三四五六七八九十]+)\s*张/gu)) {
      const stated = chineseInteger(match[1]);
      if (stated != null && stated !== authoritativeCount) conflicts.push({ field: 'ticket_count', stated, allowed: [authoritativeCount] });
    }
  }
  const lifecycle = text(facts.lifecycle ?? facts.stage, 64);
  if ((facts.paid === false || ['unpaid', 'waiting_payment'].includes(lifecycle)) && /(?:已经|已)付款/u.test(reply)) {
    conflicts.push({ field: 'paid', stated: true, allowed: [false] });
  }
  if ((facts.paid === true || ['paid', 'shipped', 'completed', 'paid_manual_delivery'].includes(lifecycle)) && /(?:尚未|还没|未)付款/u.test(reply)) {
    conflicts.push({ field: 'paid', stated: false, allowed: [true] });
  }
  if (/(?:已经|已|正在).{0,4}(?:锁座|锁定座位|占座|预留座位|保留座位)|锁好了/u.test(reply)) conflicts.push({ field: 'seat_lock', stated: true, allowed: [false] });
  if (/(?:报价已经确认|报价已确认|已确认报价)/u.test(reply) && facts.quote_confirmed !== true) {
    conflicts.push({ field: 'quote_confirmed', stated: true, allowed: [false] });
  }
  if (/(?:当前|现在|这场|这些?位置).{0,6}(?:有票|有座|可以买|能买|可购买|可以买到)|(?:确认|确定)(?:有票|可购买)/u.test(reply)
    && !(quoteStillActive || Number(facts.available_count) > 0 || facts.wplus_offer_available === true)) {
    conflicts.push({ field: 'availability', stated: true, allowed: [false] });
  }
  const paymentPermission = /(?:可以|可|现在|请|直接|放心)\s*付款|付款(?:即可|吧)/u.test(reply)
    && !/(?:不可以|不可|不能|不要|请勿|先别|暂缓)\s*付款/u.test(reply);
  if (paymentPermission && !['waiting_payment', 'price_changed'].includes(lifecycle)) {
    conflicts.push({ field: 'payment_authorization', stated: true, allowed: [false] });
  }
  if (/(?:已经改价|已改价|价格已修改|改价成功|价格改好了)/u.test(reply)
    && !['waiting_payment', 'price_changed'].includes(lifecycle)) {
    conflicts.push({ field: 'price_changed', stated: true, allowed: [false] });
  }
  if (/(?:已经出票|已出票)/u.test(reply) && !['ticket_issued', 'ticket_sent', 'shipped', 'completed'].includes(lifecycle)) {
    conflicts.push({ field: 'ticket_issued', stated: true, allowed: [false] });
  }
  if (/(?:已经发货|已发货)/u.test(reply) && !['ticket_sent', 'shipped', 'completed'].includes(lifecycle)) {
    conflicts.push({ field: 'fulfilled', stated: true, allowed: [false] });
  }
  return conflicts;
}

function parseArguments(value) {
  try {
    const parsed = JSON.parse(String(value ?? '{}'));
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

export function createModelDrivenAgentLoop({ model, tools = {}, maxToolCalls = 12 } = {}) {
  if (typeof model?.complete !== 'function') throw new TypeError('native agent model is required');
  if (!Number.isSafeInteger(maxToolCalls) || maxToolCalls < 1 || maxToolCalls > 24) throw new RangeError('maxToolCalls must be 1 to 24');

  async function runTurn(context, { onCheckpoint, onToolStart, onToolFinish, shouldContinue = () => true } = {}) {
    const observations = Array.isArray(context?.observations) ? [...context.observations] : [];
    const trace = Array.isArray(context?.trace) ? [...context.trace] : [];
    const messages = [projectionMessage(context), ...historyMessages(context), ...resumedToolMessages(observations)].filter(Boolean);
    let toolCallCount = trace.length;
    let factRepairAttempts = 0;
    const blockedWrites = new Set(observations.filter((item) => !nativeToolIsReadOnly(item?.tool)
      && (item?.status === 'pending' || ['tool_write_result_unknown', 'write_retry_blocked'].includes(item?.code))).map((item) => String(item.tool)));
    let lastMetadata = null;

    while (toolCallCount < maxToolCalls) {
      if (!shouldContinue()) return { status: 'handoff', reason: 'agent_deadline_exceeded', reply: null, trace, metadata: lastMetadata };
      const completion = await model.complete({
        tenant_id: context.tenant_id,
        conversation_id: context.conversation_id,
        run_id: context.run_id,
        messages,
        available_tools: NATIVE_AGENT_TOOL_NAMES,
        knowledge_scene: context.knowledge_scene ?? 'general',
        knowledge_snapshot: { version: '', entries: [] },
        reasoning: context.reasoning ?? { enabled: true, effort: 'medium', max_output_tokens: 1600 },
        signal: context.signal,
      });
      if (!shouldContinue()) return { status: 'handoff', reason: 'agent_deadline_exceeded', reply: null, trace, metadata: lastMetadata, messages };
      lastMetadata = {
        model: completion.model, versions: completion.versions, usage: completion.usage,
        latency_ms: completion.latency_ms, request_id: completion.request_id,
      };
      const assistant = completion.assistant;
      const calls = Array.isArray(assistant?.tool_calls) ? assistant.tool_calls : [];
      messages.push({ role: 'assistant', content: assistant?.content ?? '', ...(calls.length ? { tool_calls: calls } : {}) });
      if (!calls.length) {
        const reply = text(assistant?.content, 4_000);
        if (!reply) return { status: 'handoff', reason: 'empty_model_response', reply: null, trace, metadata: lastMetadata, messages };
        if (!shouldContinue()) return { status: 'handoff', reason: 'agent_deadline_exceeded', reply: null, trace, metadata: lastMetadata, messages };
        const conflicts = transactionFactConflicts(reply, context, observations);
        if (conflicts.length && factRepairAttempts === 0) {
          factRepairAttempts += 1;
          messages.push({
            role: 'user',
            content: JSON.stringify({
              status: 'error', code: 'fact_check_failed', summary: '回复中的显式交易事实与最新权威结果冲突，请基于工具事实自行修正一次。',
              facts: { conflicts }, missing: [], retryable: true,
            }),
          });
          continue;
        }
        if (conflicts.length) return { status: 'handoff', reason: 'fact_check_failed_twice', reply: null, trace, metadata: lastMetadata, messages };
        return { status: 'reply', reason: factRepairAttempts ? 'model_response_fact_repaired' : 'model_response', reply, trace, metadata: lastMetadata, messages };
      }
      if (toolCallCount + calls.length > maxToolCalls) {
        return { status: 'handoff', reason: 'agent_tool_call_limit', reply: null, trace, metadata: lastMetadata, messages };
      }

      const executeCall = async (call) => {
        toolCallCount += 1;
        const step = toolCallCount;
        const tool = text(call?.function?.name, 64);
        const args = parseArguments(call?.function?.arguments);
        trace.push({ step, action: tool, intent: '', confidence: null, goal: '', missing_fields: [] });
        const invocation = { step, tool, call_id: text(call?.id, 200), arguments: args, trace: structuredClone(trace), observations: structuredClone(observations) };
        let observation;
        let journal;
        if (!nativeToolIsReadOnly(tool) && blockedWrites.has(tool)) {
          observation = normalizeNativeObservation({ status: 'error', code: 'write_retry_blocked', summary: '该写工具已有待确认或未知结果，为避免重复副作用已禁止再次执行', retryable: false }, tool);
          observations.push({ ...observation, tool });
          messages.push({ role: 'tool', tool_call_id: text(call?.id, 200), name: tool, content: JSON.stringify(observation) });
          return;
        }
        if (typeof onToolStart === 'function') journal = await onToolStart(invocation);
        if (journal?.state === 'replay') observation = normalizeNativeObservation(journal.observation, tool);
        else if (journal?.state === 'unknown') observation = normalizeNativeObservation({ status: 'error', code: 'tool_result_unknown', summary: '上次工具执行结果未知，为避免重复写入未再次执行', retryable: false }, tool);
        else {
          const implementation = resolveNativeTool(tools, tool);
          if (!implementation) observation = normalizeNativeObservation({ status: 'error', code: 'tool_unavailable', summary: `工具 ${tool} 当前不可用`, retryable: false }, tool);
          else {
            try {
              observation = normalizeNativeObservation(await implementation({ context, arguments: args, step, call_id: call?.id }), tool);
            } catch (error) {
              const writeShaped = !nativeToolIsReadOnly(tool);
              observation = normalizeNativeObservation({
                status: 'error', code: writeShaped ? 'tool_write_result_unknown' : 'tool_execution_failed',
                summary: writeShaped ? '写工具执行结果未知，为避免重复副作用已停止重试' : text(error?.message, 300) || '工具执行失败',
                retryable: !writeShaped,
              }, tool);
              if (writeShaped) blockedWrites.add(tool);
            }
          }
          if (!nativeToolIsReadOnly(tool) && ['pending', 'success'].includes(observation.status)) blockedWrites.add(tool);
          if (typeof onToolFinish === 'function') await onToolFinish(invocation, { ...observation, tool });
        }
        observations.push({ ...observation, tool });
        messages.push({ role: 'tool', tool_call_id: text(call?.id, 200), name: tool, content: JSON.stringify(observation) });
      };

      // A model may batch independent reads. Any side effect keeps the entire
      // batch serial so write ordering and journal state remain unambiguous.
      if (nativeToolsCanRunInParallel(calls)) await Promise.all(calls.map(executeCall));
      else for (const call of calls) await executeCall(call);
      if (typeof onCheckpoint === 'function') await onCheckpoint({ trace: structuredClone(trace), observations: structuredClone(observations) });
    }
    return { status: 'handoff', reason: 'agent_tool_call_limit', reply: null, trace, metadata: lastMetadata, messages };
  }

  return Object.freeze({ runTurn });
}
