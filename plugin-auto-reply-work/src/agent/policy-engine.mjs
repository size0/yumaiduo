const PAID_STAGES = new Set(['paid', 'paid_manual_delivery', 'ticket_issued', 'ticket_sent', 'fulfillment_exception']);

function facts(context) {
  const value = context?.state?.facts;
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function allowed(tool, reason) {
  return Object.freeze({ status: 'allowed', tool, reason });
}

function denied(reason) {
  return Object.freeze({ status: 'denied', tool: null, reason });
}

function stopped(reason) {
  return Object.freeze({ status: 'stop', tool: null, reason });
}

function hasRecentBuyerImage(messages, now) {
  if (!Array.isArray(messages)) return false;
  return messages.some((item) => {
    if (!item || typeof item !== 'object') return false;
    const direction = String(item.role ?? item.direction ?? '').toLowerCase();
    if (direction && !['buyer', 'inbound'].includes(direction)) return false;
    const rawAt = item.at ?? item.sentAt ?? item.sent_at;
    const at = Number.isFinite(Number(rawAt)) ? Number(rawAt) : Date.parse(String(rawAt ?? ''));
    if (!Number.isFinite(at) || at > now || now - at > 30_000) return false;
    const content = String(item.text ?? item.content ?? '');
    return item.image === true || (Array.isArray(item.imageUrls) && item.imageUrls.length > 0) || /https?:\/\/\S+\.(?:jpe?g|png|webp|gif)(?:\?\S*)?$/iu.test(content);
  });
}

function activeQuote(stateFacts, now) {
  return Number.isSafeInteger(stateFacts.quote_total_cents)
    && stateFacts.quote_total_cents > 0
    && Number.isSafeInteger(stateFacts.quote_ticket_count)
    && stateFacts.quote_ticket_count > 0
    && Number(stateFacts.quote_expires_at) > now;
}

export function guardAgentPlan(plan, context = {}) {
  const stateFacts = facts(context);
  const message = String(context.latest_message ?? '').replace(/\s+/gu, '').trim();
  const now = Number(context.now ?? Date.now());
  const observations = Array.isArray(context.observations) ? context.observations : [];
  const imageObservation = observations.findLast?.((item) => item?.tool === 'recognize_image' && item?.status === 'success')
    ?? [...observations].reverse().find((item) => item?.tool === 'recognize_image' && item?.status === 'success');
  const imageObserved = Boolean(imageObservation);
  const ticketImageObserved = imageObserved && ['cinema', 'movie', 'date', 'showtime', '_quote_input']
    .some((key) => imageObservation?.facts?.[key] != null);
  const showtimeObserved = observations.some((item) => item?.tool === 'resolve_showtime' && item?.status === 'success');
  const quoteObserved = observations.some((item) => ['recognize_and_quote', 'quote_realtime'].includes(item?.tool) && item?.status === 'success');
  const orderObserved = observations.some((item) => ['get_order_status', 'read_linked_order'].includes(item?.tool) && item?.status === 'success');
  const latestObservation = observations.at?.(-1) ?? observations[observations.length - 1];
  const declaredNextActions = latestObservation && latestObservation.status !== 'error' && !latestObservation.stop_reason && Array.isArray(latestObservation.next_actions)
    ? [...new Set(latestObservation.next_actions.map(String).filter(Boolean))]
    : [];
  const declaredNextAction = declaredNextActions.length === 1 ? declaredNextActions[0] : null;
  let action = declaredNextAction ?? plan.action;
  const hasLinkedOrder = Boolean(stateFacts.order_id || stateFacts.has_linked_order);
  const hasReusableRecognition = Boolean(stateFacts.quote_draft?.recognition_artifact || stateFacts.recognition_draft || stateFacts.quote_draft_recognition);
  const hasValidActiveQuote = activeQuote(stateFacts, now);
  const confirmation = /^(?:确认|确定|可以|行|好|好的|ok|OK|就这个|就这样)$/u.test(message);
  const fulfillmentQuestion = /(?:什么时候|多久|何时).{0,8}(?:出票|发货)|(?:出票|发货).{0,8}(?:了吗|没有|进度|状态)|^(?:你)?已发货$/u.test(message);
  const seatPreference = /\d{1,2}排.{0,24}\d{1,2}(?:座|号)?/u.test(message)
    || /(?:红点|绿点|圈出|圈的|画的|标出).{0,16}(?:位置|座位|两个|两位置)/u.test(message)
    || /(?:已经|已)?(?:圈好|圈了|圈过|标好|标了)(?:位置|座位)?/u.test(message);
  if (declaredNextAction) {
    // A validated tool contract with one legal next action is deterministic;
    // do not spend another tool call following a contradictory model choice.
  } else if (context.has_image === true && !imageObserved && !quoteObserved) action = 'recognize_image';
  else if (context.has_image === true && ticketImageObserved && !showtimeObserved && !quoteObserved && action !== 'quote_realtime') action = 'resolve_showtime';
  else if (context.has_image === true && ticketImageObserved && showtimeObserved && !quoteObserved) action = 'quote_realtime';
  else if (hasValidActiveQuote && confirmation) action = 'confirm_quote';
  else if (seatPreference) action = 'record_seat_preference';
  else if (hasLinkedOrder && !orderObserved && /(?:订单|拍下|付款|支付|改价|改好|进度|状态|出票|发货)/u.test(message)) action = 'get_order_status';
  else if (fulfillmentQuestion) action = 'handoff';
  else if (/^(?:谢谢|谢谢大哥|感谢|感谢大哥|辛苦了|你人真好|你人真不错).{0,16}$/u.test(message)) action = 'wait';
  else if (confirmation) action = 'wait';
  else if (hasValidActiveQuote && ['start_quote', 'ask_for_image'].includes(action) && !/(?:万达|影城|影院|\d{1,2}月\d{1,2}|\d{1,2}[:：]\d{2}|换场|换影院|换电影)/u.test(message)) action = 'wait';
  else if (context.has_image !== true && !hasReusableRecognition
    && ['start_quote', 'recognize_image', 'resolve_showtime', 'quote_realtime'].includes(action)) action = 'inspect_ticket_request';
  else if (action === 'recognize_image' && context.has_image !== true) action = hasReusableRecognition ? 'resolve_showtime' : 'inspect_ticket_request';
  else if (action === 'ask_for_image' && hasRecentBuyerImage(context.state?.messages, now)) action = 'recognize_image';
  else if (stateFacts.quote_draft && !quoteObserved && /^(?:[\u4e00-\u9fff]{2,10}市|[^\s]{2,30}(?:万达|寰映|儒意)(?:影城|影院)?)$/u.test(message)) action = 'resolve_showtime';
  return action === plan.action ? plan : Object.freeze({ ...plan, action, arguments: Object.freeze({}), reply: '', reason: 'deterministic_context_guard' });
}

export function authorizeAgentPlan(plan, context = {}) {
  const stateFacts = facts(context);
  if (context.human_takeover === true) return stopped('human_takeover');
  if (PAID_STAGES.has(String(stateFacts.stage ?? ''))) return stopped('paid_order');
  // create_manual_task is the durable, bounded realization of a handoff. It
  // may legitimately carry needs_human=true; all identifiers are injected by
  // the system tool rather than accepted from the model.
  if (plan.action === 'create_manual_task') return allowed('create_manual_task', 'manual_task_requested');
  if (plan.needs_human || plan.action === 'handoff') return stopped('agent_requested_handoff');
  if (plan.confidence < 0.6 && !['wait', 'ask_for_image', 'ask_for_city', 'ask_for_missing_information'].includes(plan.action)) {
    return denied('agent_confidence_too_low');
  }

  if (plan.action === 'inspect_ticket_request') return allowed('inspect_ticket_request', 'bounded_request_inspection');
  if (plan.action === 'start_quote') {
    if (context.settings?.recognition_enabled !== true || context.settings?.quote_enabled !== true) return denied('quote_feature_disabled');
    const hasReusableFacts = Boolean(stateFacts.recognition_draft || stateFacts.quote_draft_recognition || stateFacts.quote_draft);
    if (!context.has_image && !hasReusableFacts) return denied('quote_evidence_missing');
    return allowed('recognize_and_quote', 'legacy_quote_requested');
  }
  if (plan.action === 'recognize_image') {
    if (context.settings?.recognition_enabled !== true) return denied('recognition_feature_disabled');
    if (!context.has_image && !stateFacts.quote_draft) return denied('image_evidence_missing');
    return allowed('recognize_image', 'image_recognition_requested');
  }
  if (plan.action === 'resolve_showtime') {
    const recognized = Array.isArray(context.observations) && context.observations.some((item) => item?.tool === 'recognize_image' && item?.status === 'success');
    return recognized || stateFacts.quote_draft ? allowed('resolve_showtime', 'showtime_resolution_requested') : denied('recognition_required');
  }
  if (plan.action === 'quote_realtime') {
    if (context.settings?.quote_enabled !== true) return denied('quote_feature_disabled');
    const resolved = Array.isArray(context.observations) && context.observations.some((item) => item?.tool === 'resolve_showtime' && item?.status === 'success');
    return resolved ? allowed('quote_realtime', 'realtime_quote_requested') : denied('showtime_resolution_required');
  }
  if (plan.action === 'request_price_change') return denied('agent_price_change_not_enabled');
  if (plan.action === 'show_available_wplus_seats') {
    if (context.settings?.quote_enabled !== true) return denied('quote_feature_disabled');
    return allowed('list_available_wplus_seats', 'seat_query_requested');
  }
  if (plan.action === 'record_seat_preference') return allowed('record_seat_preference', 'circled_delivery_instruction_or_typed_seat');
  if (plan.action === 'confirm_quote') {
    const now = Number(context.now ?? Date.now());
    const valid = Number.isSafeInteger(stateFacts.quote_total_cents)
      && stateFacts.quote_total_cents > 0
      && Number.isSafeInteger(stateFacts.quote_ticket_count)
      && stateFacts.quote_ticket_count > 0
      && Number(stateFacts.quote_expires_at) > now;
    return valid ? allowed('confirm_active_quote', 'active_quote_confirmable') : denied('active_quote_missing_or_expired');
  }
  if (['get_order_status', 'read_linked_order'].includes(plan.action)) {
    return stateFacts.order_id ? allowed('read_linked_order', 'linked_order_available') : denied('linked_order_missing');
  }
  if (['respond', 'ask_for_image', 'ask_for_city', 'ask_for_missing_information', 'wait'].includes(plan.action)) {
    return allowed(null, 'conversation_only');
  }
  return denied('action_not_authorized');
}
