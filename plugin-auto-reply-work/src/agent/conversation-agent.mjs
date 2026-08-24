import { normalizeAgentPlan } from './agent-schema.mjs';
import { authorizeAgentPlan, guardAgentPlan } from './policy-engine.mjs';
import { composeAgentReply } from './response-composer.mjs';
import { isToolExecutionAllowed, validateToolObservation } from './agent-tool-contracts.mjs';

function deterministicConversationReply(plan) {
  if (plan?.action === 'ask_for_image') return '请发送当前完整选座页截图，并说明需要的张数。';
  if (plan?.action === 'ask_for_city') return '请补充影院所在城市和完整影院分店名。';
  if (plan?.action !== 'ask_for_missing_information') return null;
  const labels = { city: '城市', cinema: '完整影院分店名', movie: '影片名', date: '日期', showtime: '准确开场时间', hall: '影厅', ticket_count: '需要的张数' };
  const missing = Array.isArray(plan.missing_fields) ? [...new Set(plan.missing_fields.map((item) => labels[String(item)]).filter(Boolean))].slice(0, 4) : [];
  return missing.length ? `请补充${missing.join('、')}。` : '请补充影院、影片、日期、开场时间和需要的张数。';
}

function safeObservation(value, tool) {
  const input = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const status = ['success', 'warning', 'error'].includes(input.status) ? input.status : 'error';
  const facts = input.facts && typeof input.facts === 'object' && !Array.isArray(input.facts)
    ? Object.fromEntries(Object.entries(input.facts).slice(0, 30))
    : {};
  return Object.freeze({
    status,
    tool,
    summary: String(input.summary ?? '').trim().slice(0, 200),
    facts: Object.freeze(facts),
    authoritative_reply: String(input.authoritative_reply ?? '').trim().slice(0, 1_000),
    next_actions: Array.isArray(input.next_actions) ? input.next_actions.slice(0, 8).map((item) => String(item).slice(0, 64)) : [],
    code: String(input.code ?? input.stop_reason ?? '').trim().slice(0, 100),
    missing: Array.isArray(input.missing) ? input.missing.slice(0, 20).map((item) => String(item).slice(0, 100)) : [],
    retryable: input.retryable === true,
    stop_reason: String(input.stop_reason ?? '').trim().slice(0, 100) || null,
  });
}

export function createConversationAgent({ planner, tools = {}, policy = authorizeAgentPlan, maxSteps = 3, mode = 'shadow', allowWriteSimulation = false } = {}) {
  if (!planner || typeof planner.plan !== 'function') throw new TypeError('conversation agent planner is required');
  if (!Number.isSafeInteger(maxSteps) || maxSteps < 1 || maxSteps > 8) throw new RangeError('conversation agent maxSteps must be 1 to 8');

  async function runTurn(context, { onCheckpoint, onToolStart, onToolFinish, shouldContinue = () => true } = {}) {
    const observations = Array.isArray(context?.observations)
      ? context.observations.slice(-8).map((item) => safeObservation(item, String(item?.tool ?? 'unknown')))
      : [];
    const trace = Array.isArray(context?.trace) ? context.trace.slice(0, maxSteps).map((item, index) => ({
      step: Number.isSafeInteger(Number(item?.step)) ? Number(item.step) : index + 1,
      action: String(item?.action ?? '').slice(0, 64), intent: String(item?.intent ?? '').slice(0, 32),
      confidence: Number.isFinite(Number(item?.confidence)) ? Number(item.confidence) : 0,
      goal: String(item?.goal ?? '').slice(0, 160), missing_fields: Array.isArray(item?.missing_fields) ? item.missing_fields.slice(0, 10).map(String) : [],
    })) : [];
    let authoritativeObservation = [...observations].reverse().find((item) => item.authoritative_reply) ?? null;
    const resumedAuthoritativeReply = composeAgentReply({ plan: null, observation: authoritativeObservation, hasVerifiedQuote: true });
    if (resumedAuthoritativeReply && authoritativeObservation?.status !== 'error' && !authoritativeObservation?.stop_reason
      && authoritativeObservation?.next_actions?.includes('respond')) {
      return { status: 'reply', reason: 'authoritative_tool_response', reply: resumedAuthoritativeReply, trace };
    }
    for (let step = trace.length; step < maxSteps; step += 1) {
      if (!shouldContinue()) return { status: 'handoff', reason: 'agent_deadline_exceeded', reply: null, trace };
      let plan;
      try {
        plan = guardAgentPlan(normalizeAgentPlan(await planner.plan({ ...context, observations })), { ...context, observations });
      } catch (error) {
        if (context?.signal?.aborted) throw error;
        return { status: 'handoff', reason: 'invalid_agent_plan', reply: null, trace };
      }
      trace.push({
        step: step + 1,
        action: plan.action,
        intent: plan.intent,
        confidence: plan.confidence,
        goal: plan.goal,
        missing_fields: [...plan.missing_fields],
        ...(plan.experience_candidate ? { experience_candidate: plan.experience_candidate } : {}),
      });
      if (!shouldContinue()) return { status: 'handoff', reason: 'agent_deadline_exceeded', reply: null, trace };
      const authorization = policy(plan, { ...context, observations });
      if (authorization.status === 'stop') {
        return { status: plan.action === 'wait' ? 'silent' : 'handoff', reason: authorization.reason, reply: null, trace };
      }
      if (authorization.status !== 'allowed') {
        const prerequisiteAction = ({ recognition_required: 'recognize_image', showtime_resolution_required: 'resolve_showtime' })[authorization.reason];
        if (!prerequisiteAction) return { status: 'handoff', reason: authorization.reason, reply: null, trace };
        const observation = safeObservation({
          status: 'warning', summary: '交易工具前置条件尚未完成', facts: {},
          next_actions: [prerequisiteAction], stop_reason: null,
        }, 'policy_guard');
        validateToolObservation('policy_guard', observation, { mode });
        observations.push(observation);
        if (typeof onCheckpoint === 'function') await onCheckpoint({ trace: structuredClone(trace), observations: structuredClone(observations) });
        continue;
      }
      if (!authorization.tool) {
        if (plan.action === 'wait') return { status: 'silent', reason: 'agent_wait', reply: null, trace };
        const reply = composeAgentReply({ plan, observation: authoritativeObservation, hasVerifiedQuote: Boolean(authoritativeObservation?.authoritative_reply) })
          ?? deterministicConversationReply(plan);
        return reply
          ? { status: 'reply', reason: 'agent_response', reply, trace }
          : { status: 'handoff', reason: 'unsafe_or_empty_agent_reply', reply: null, trace };
      }
      const tool = tools[authorization.tool];
      if (typeof tool !== 'function') return { status: 'handoff', reason: 'agent_tool_unavailable', reply: null, trace };
      if (observations.some((item) => item?.tool === authorization.tool && ['success', 'warning'].includes(item?.status))) {
        return { status: 'handoff', reason: 'agent_duplicate_tool_call', reply: null, trace };
      }
      if (!isToolExecutionAllowed(authorization.tool, { mode, allowWriteSimulation })) {
        const observation = safeObservation({ status: 'error', summary: 'write tool is disabled outside active mode', stop_reason: 'shadow_write_tool_disabled' }, authorization.tool);
        observations.push(observation);
        if (typeof onCheckpoint === 'function') await onCheckpoint({ trace: structuredClone(trace), observations: structuredClone(observations) });
        return { status: 'handoff', reason: 'shadow_write_tool_disabled', reply: null, trace };
      }
      if (!shouldContinue()) return { status: 'handoff', reason: 'agent_deadline_exceeded', reply: null, trace };
      const toolCall = Object.freeze({ step: step + 1, tool: authorization.tool, plan, trace: structuredClone(trace), observations: structuredClone(observations) });
      let observation;
      let journalState = null;
      if (typeof onToolStart === 'function') {
        journalState = await onToolStart(toolCall);
        if (journalState?.state === 'replay') {
          observation = safeObservation(journalState.observation, authorization.tool);
          try { validateToolObservation(authorization.tool, observation, { mode }); }
          catch { observation = safeObservation({ status: 'error', summary: 'journaled tool result violated its contract', stop_reason: 'agent_tool_contract_violation' }, authorization.tool); }
        } else if (journalState?.state === 'unknown') observation = safeObservation({ status: 'error', summary: 'prior tool result is unknown; execution was not repeated', stop_reason: 'agent_tool_result_unknown' }, authorization.tool);
      }
      if (!observation) {
        try {
          observation = safeObservation(await tool({ context, plan, step }), authorization.tool);
          validateToolObservation(authorization.tool, observation, { mode });
        } catch (error) {
          const contractViolation = /tool contract|undeclared|observation identity|write tool is disabled/u.test(String(error?.message ?? ''));
          observation = safeObservation({
            status: 'error',
            summary: contractViolation ? 'tool result violated its declared contract' : 'tool execution failed',
            stop_reason: contractViolation ? 'agent_tool_contract_violation' : 'agent_tool_failed',
          }, authorization.tool);
        }
        if (typeof onToolFinish === 'function') await onToolFinish(toolCall, observation);
      }
      observations.push(observation);
      if (observation.authoritative_reply) authoritativeObservation = observation;
      if (typeof onCheckpoint === 'function') await onCheckpoint({ trace: structuredClone(trace), observations: structuredClone(observations) });
      if (observation.status === 'error' || observation.stop_reason) {
        return { status: 'handoff', reason: observation.stop_reason || 'agent_tool_failed', reply: observation.authoritative_reply || null, trace };
      }
      if (observation.authoritative_reply && observation.next_actions.includes('respond')) {
        const authoritativeReply = composeAgentReply({ plan: null, observation, hasVerifiedQuote: true });
        return authoritativeReply
          ? { status: 'reply', reason: 'authoritative_tool_response', reply: authoritativeReply, trace }
          : { status: 'handoff', reason: 'unsafe_or_empty_agent_reply', reply: null, trace };
      }
    }
    const finalReply = composeAgentReply({ plan: null, observation: authoritativeObservation, hasVerifiedQuote: true });
    return finalReply
      ? { status: 'reply', reason: 'authoritative_tool_response', reply: finalReply, trace }
      : { status: 'handoff', reason: 'agent_step_limit', reply: null, trace };
  }

  return Object.freeze({ runTurn });
}
