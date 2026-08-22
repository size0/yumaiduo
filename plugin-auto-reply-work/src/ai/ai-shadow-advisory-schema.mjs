const INTENTS = new Set(['票价咨询', '选座核价', '补充信息', '订单进度', '售后咨询', '人工接管', '其他']);
const OUTPUT_KEYS = new Set(['intent', 'confidence', 'reply_draft', 'handoff_recommended', 'reason_code', 'missing_fields']);
const MISSING_FIELDS = new Set(['image', 'city', 'cinema', 'movie', 'date', 'showtime', 'hall', 'ticket_count']);
const UNSAFE_REPLY = /(?:[¥￥]|(?:\d+(?:\.\d{1,2})?|[零一二三四五六七八九十百千万两]+)\s*(?:元|块钱?|人民币)|已(?:锁座|改价|付款|支付|出票|发货|退款)|(?:锁座|改价|付款|支付|出票|发货|退款)(?:成功|完成)|(?:有票|有余票|座位可售|可以买到)|https?:\/\/|1\d{10}|\b\d{12,}\b|\{[^{}]+\}|\$\{[^{}]+\})/iu;

/** Normalize one provider-neutral, advisory-only Shadow result. */
export function normalizeAiShadowAdvisory(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('invalid AI Shadow advisory');
  const keys = Object.keys(value);
  const undeclared = keys.find((key) => !OUTPUT_KEYS.has(key));
  if (undeclared) throw new TypeError(`undeclared AI Shadow advisory output: ${undeclared}`);
  if (keys.length !== OUTPUT_KEYS.size || [...OUTPUT_KEYS].some((key) => !Object.hasOwn(value, key))) {
    throw new TypeError('invalid AI Shadow advisory');
  }

  const intent = compactText(value.intent, 32);
  if (!INTENTS.has(intent)) throw new TypeError('invalid AI Shadow intent');
  const confidence = Number(value.confidence);
  if (typeof value.confidence !== 'number' || !Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
    throw new TypeError('invalid AI Shadow confidence');
  }
  if (typeof value.reply_draft !== 'string' || value.reply_draft.length > 500) throw new TypeError('invalid AI Shadow reply draft');
  const replyDraft = compactText(value.reply_draft, 500);
  if (UNSAFE_REPLY.test(replyDraft)) throw new TypeError('unsafe AI Shadow reply draft');
  if (typeof value.handoff_recommended !== 'boolean') throw new TypeError('invalid AI Shadow handoff recommendation');
  const reasonCode = compactText(value.reason_code, 64);
  if (!/^[a-z][a-z0-9_]{0,63}$/u.test(reasonCode)) throw new TypeError('invalid AI Shadow reason code');
  if (!Array.isArray(value.missing_fields)
    || value.missing_fields.length > 8
    || value.missing_fields.some((field) => !MISSING_FIELDS.has(String(field)))) {
    throw new TypeError('invalid AI Shadow missing fields');
  }
  return Object.freeze({
    intent,
    confidence,
    reply_draft: replyDraft,
    handoff_recommended: value.handoff_recommended,
    reason_code: reasonCode,
    missing_fields: Object.freeze([...new Set(value.missing_fields.map(String))]),
  });
}

function compactText(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}
