const ACTIONS = new Set([
  'respond',
  'ask_for_image',
  'ask_for_city',
  'ask_for_missing_information',
  'start_quote',
  'recognize_image',
  'resolve_showtime',
  'quote_realtime',
  'request_price_change',
  'create_manual_task',
  'show_available_wplus_seats',
  'record_seat_preference',
  'confirm_quote',
  'get_order_status',
  'read_linked_order',
  'inspect_ticket_request',
  'handoff',
  'wait',
]);
const INTENTS = new Set(['票价咨询', '选座核价', '补充信息', '订单进度', '售后咨询', '人工接管', '其他']);
const FORBIDDEN_ARGUMENT = /(?:amount|price|discount|fee|total|order_id|token|secret|authorization|cookie)/iu;
const EXPERIENCE_TOPICS = new Set(['问候与结束语', '图片要求', '服务范围', '服务流程', '沟通方式']);
const EXPERIENCE_OUTCOMES = new Set(['buyer_acknowledged', 'buyer_progressed']);
const UNSAFE_EXPERIENCE = /(?:[0-9０-９]|[零一二三四五六七八九十百千万]{2,}|元|块钱|价格|优惠|折扣|会员价|订单|付款|支付|改价|出票|发货|退款|库存|余票|可售|微信|手机号|电话|https?:\/\/|www\.|@)/iu;

function boundedText(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

function normalizeExperience(value) {
  if (value == null) return null;
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('unsafe conversation experience');
  const topic = boundedText(value.topic, 32);
  const questionPattern = boundedText(value.question_pattern, 120);
  const responseGuidance = boundedText(value.response_guidance, 300);
  const exampleReply = boundedText(value.example_reply, 300);
  const outcomeSignal = boundedText(value.outcome_signal, 32);
  const confidence = Number(value.confidence);
  const combined = `${questionPattern} ${responseGuidance} ${exampleReply}`;
  if (!EXPERIENCE_TOPICS.has(topic)
    || questionPattern.length < 5
    || responseGuidance.length < 5
    || !exampleReply
    || !EXPERIENCE_OUTCOMES.has(outcomeSignal)
    || !Number.isFinite(confidence)
    || confidence < 0.85
    || confidence > 1
    || UNSAFE_EXPERIENCE.test(combined)) {
    throw new TypeError('unsafe conversation experience');
  }
  return Object.freeze({
    topic,
    question_pattern: questionPattern,
    response_guidance: responseGuidance,
    example_reply: exampleReply,
    outcome_signal: outcomeSignal,
    confidence,
  });
}

function normalizeArguments(value, depth = 0) {
  if (value == null) return {};
  if (!value || typeof value !== 'object' || Array.isArray(value) || depth > 2) throw new TypeError('invalid agent arguments');
  const result = {};
  for (const [rawKey, rawValue] of Object.entries(value).slice(0, 12)) {
    const key = boundedText(rawKey, 64);
    if (!key) continue;
    if (FORBIDDEN_ARGUMENT.test(key)) throw new TypeError(`forbidden agent argument: ${key}`);
    if (typeof rawValue === 'string') result[key] = boundedText(rawValue, 160);
    else if (typeof rawValue === 'boolean') result[key] = rawValue;
    else if (Number.isSafeInteger(rawValue) && Math.abs(rawValue) <= 100) result[key] = rawValue;
    else if (Array.isArray(rawValue)) result[key] = rawValue.slice(0, 10).map((item) => boundedText(item, 80)).filter(Boolean);
    else if (rawValue && typeof rawValue === 'object') result[key] = normalizeArguments(rawValue, depth + 1);
  }
  return result;
}

export function normalizeAgentPlan(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('invalid agent plan');
  const action = boundedText(value.action, 64);
  if (!ACTIONS.has(action)) throw new TypeError(`unsupported agent action: ${action || 'empty'}`);
  const intent = boundedText(value.intent, 32);
  if (!INTENTS.has(intent)) throw new TypeError(`unsupported agent intent: ${intent || 'empty'}`);
  const confidence = Number(value.confidence);
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) throw new TypeError('invalid agent confidence');
  const missingFields = Array.isArray(value.missing_fields)
    ? value.missing_fields.slice(0, 10).map((item) => boundedText(item, 64)).filter(Boolean)
    : [];
  const experienceCandidate = normalizeExperience(value.experience_candidate);
  return Object.freeze({
    intent,
    confidence,
    goal: boundedText(value.goal, 160),
    action,
    arguments: Object.freeze(normalizeArguments(value.arguments)),
    missing_fields: Object.freeze(missingFields),
    reply: boundedText(value.reply, 500),
    needs_human: value.needs_human === true,
    reason: boundedText(value.reason, 160),
    ...(experienceCandidate ? { experience_candidate: experienceCandidate } : {}),
  });
}
