const ACTIONS = Object.freeze([
  'respond', 'ask_for_image', 'ask_for_city', 'ask_for_missing_information',
  'inspect_ticket_request', 'recognize_image', 'resolve_showtime', 'quote_realtime', 'read_active_quote',
  'create_manual_task', 'get_manual_task_status', 'record_seat_preference', 'confirm_quote', 'handoff', 'wait',
]);

function contract({ effect = 'read', facts = [], next = [], authoritativeReply = false }) {
  return Object.freeze({
    effect,
    allowed_fact_keys: Object.freeze([...facts]),
    allowed_next_actions: Object.freeze([...next]),
    authoritative_reply: authoritativeReply,
  });
}

const IDENTITY = ['city', 'cinema', 'movie', 'date', 'showtime', 'hall'];
const SOURCE_RESULT = ['status', 'failure_code', 'quote_succeeded'];

/**
 * Runtime-enforced tool contracts. They describe externally visible effects and
 * the only observation fields a tool may return to the planner. Identifiers,
 * order numbers and money inputs are never accepted from a model plan.
 */
export const AGENT_TOOL_CONTRACTS = Object.freeze({
  inspect_ticket_request: contract({
    facts: ['has_image', 'requested_ticket_count', 'has_typed_seat_instruction', 'has_linked_order', 'has_active_quote', 'known_identity_fields', 'missing_identity_fields'],
    next: ['recognize_image', 'respond', 'ask_for_image', 'handoff'],
  }),
  recognize_image: contract({
    facts: [...IDENTITY, 'status', 'image_type', 'requested_ticket_count', '_quote_input'],
    next: ['recognize_image', 'resolve_showtime', 'respond', 'create_manual_task', 'handoff'], authoritativeReply: true,
  }),
  resolve_showtime: contract({
    facts: [...IDENTITY, '_quote_input'], next: ['quote_realtime', 'create_manual_task', 'handoff'],
  }),
  quote_realtime: contract({
    effect: 'external_temporary_write',
    facts: [...SOURCE_RESULT, 'unit_quote_cents', 'total_quote_cents', 'ticket_count', 'pricing_rule_version', '_quote_delivery'],
    next: ['respond', 'create_manual_task', 'handoff'], authoritativeReply: true,
  }),
  read_active_quote: contract({
    facts: ['unit_quote_cents', 'total_quote_cents', 'ticket_count'], next: ['respond'], authoritativeReply: true,
  }),
  recognize_and_quote: contract({
    effect: 'external_temporary_write',
    facts: [...SOURCE_RESULT, 'unit_quote_cents', 'total_quote_cents', 'ticket_count', 'pricing_rule_version', '_quote_delivery'],
    next: ['respond', 'handoff'], authoritativeReply: true,
  }),
  list_available_wplus_seats: contract({
    facts: SOURCE_RESULT, next: ['respond', 'handoff'], authoritativeReply: true,
  }),
  record_seat_preference: contract({
    effect: 'write', facts: ['preference_recorded', 'circled_delivery_instruction_recorded'], next: ['respond', 'handoff'], authoritativeReply: true,
  }),
  confirm_active_quote: contract({
    effect: 'write', facts: ['quote_confirmed'], next: ['respond', 'handoff'], authoritativeReply: true,
  }),
  create_manual_task: contract({
    effect: 'write', facts: ['manual_task_created'], next: ['respond', 'handoff'], authoritativeReply: true,
  }),
  get_manual_task_status: contract({
    facts: ['manual_task_found', 'manual_task_status', 'manual_task_resolved'], next: ['respond', 'handoff'], authoritativeReply: true,
  }),
  read_linked_order: contract({
    facts: ['has_linked_order', 'lifecycle', 'paid', 'fulfilled'], next: ['respond', 'create_manual_task', 'handoff'], authoritativeReply: true,
  }),
  policy_guard: contract({ facts: [], next: ['recognize_image', 'resolve_showtime', 'handoff'] }),
});

export function isToolExecutionAllowed(toolValue, { mode = 'shadow', allowWriteSimulation = false } = {}) {
  const contractValue = AGENT_TOOL_CONTRACTS[String(toolValue ?? '')];
  if (!contractValue) return false;
  return contractValue.effect !== 'write' || mode === 'active' || allowWriteSimulation === true;
}

export function validateToolObservation(toolValue, observation, { mode = 'shadow' } = {}) {
  const tool = String(toolValue ?? '');
  const toolContract = AGENT_TOOL_CONTRACTS[tool];
  if (!toolContract) throw new TypeError(`agent tool contract is missing: ${tool}`);
  if (!observation || typeof observation !== 'object' || Array.isArray(observation)) throw new TypeError(`invalid tool observation: ${tool}`);
  if (String(observation.tool ?? tool) !== tool) throw new TypeError(`tool observation identity mismatch: ${tool}`);
  if (!['success', 'warning', 'error'].includes(observation.status)) throw new TypeError(`invalid tool observation status: ${tool}`);

  const facts = observation.facts && typeof observation.facts === 'object' && !Array.isArray(observation.facts) ? observation.facts : {};
  const allowedFacts = new Set(toolContract.allowed_fact_keys);
  const undeclaredFact = Object.keys(facts).find((key) => !allowedFacts.has(key));
  if (undeclaredFact) throw new TypeError(`agent tool returned undeclared fact: ${tool}.${undeclaredFact}`);

  const allowedActions = new Set(toolContract.allowed_next_actions);
  const nextActions = Array.isArray(observation.next_actions) ? observation.next_actions : [];
  const undeclaredAction = nextActions.find((action) => !ACTIONS.includes(action) || !allowedActions.has(action));
  if (undeclaredAction) throw new TypeError(`agent tool returned undeclared next action: ${tool}.${undeclaredAction}`);

  if (String(observation.authoritative_reply ?? '').trim() && !toolContract.authoritative_reply) {
    throw new TypeError(`agent tool returned an undeclared authoritative reply: ${tool}`);
  }
  const safeDisabledWrite = observation.status === 'error' && observation.stop_reason === 'shadow_write_tool_disabled';
  const safeSimulatedWrite = observation.status === 'success'
    && /影子模式/u.test(String(observation.summary ?? ''))
    && Object.values(facts).every((value) => value === false);
  if (toolContract.effect === 'write' && mode !== 'active' && !safeDisabledWrite && !safeSimulatedWrite) {
    throw new TypeError(`agent write tool is disabled outside active mode: ${tool}`);
  }
  return observation;
}
