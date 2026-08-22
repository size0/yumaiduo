const IDENTITY_FIELDS = Object.freeze(['cinema', 'movie', 'date', 'showtime']);
const CHINESE_COUNTS = Object.freeze({ 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 });

export function requestedTicketCount(value) {
  const text = String(value ?? '');
  const explicitArabic = text.match(/(?:^|[^\d])(\d{1,2})\s*(?:张|个(?:座位)?)(?:$|[^\d])/u);
  if (explicitArabic) return boundedCount(explicitArabic[1]);
  const explicitChinese = text.match(/([一二三四五六七八九十两])\s*(?:张|个(?:座位)?)/u);
  if (explicitChinese) return CHINESE_COUNTS[explicitChinese[1]] ?? null;
  if (/(?:第\s*)?(?:\d{1,2}|[一二三四五六七八九十两]{1,3})\s*(?:排|行)/u.test(text)) return null;
  const bareArabic = text.match(/(?:^|[^\d])(\d{1,2})\s*座(?:$|[^\d])/u);
  if (bareArabic) return boundedCount(bareArabic[1]);
  const bareChinese = text.match(/([一二三四五六七八九十两])\s*座/u);
  return bareChinese ? (CHINESE_COUNTS[bareChinese[1]] ?? null) : null;
}

export function inspectTicketRequest({ message, hasImage = false, facts = {}, now = Date.now() } = {}) {
  const safeFacts = facts && typeof facts === 'object' && !Array.isArray(facts) ? facts : {};
  const known = IDENTITY_FIELDS.filter((field) => safeText(safeFacts[field]));
  const requested = requestedTicketCount(message);
  const expiresAt = Number(safeFacts.quote_expires_at ?? 0);
  const activeQuote = Number.isSafeInteger(Number(safeFacts.quote_total_cents))
    && Number(safeFacts.quote_total_cents) > 0
    && Number.isSafeInteger(Number(safeFacts.quote_ticket_count))
    && Number(safeFacts.quote_ticket_count) > 0
    && expiresAt > Number(now);
  return Object.freeze({
    has_image: hasImage === true,
    requested_ticket_count: requested,
    has_typed_seat_instruction: /(?:第\s*)?(?:\d{1,2}|[一二三四五六七八九十两]{1,3})\s*(?:排|行)/u.test(String(message ?? '')),
    has_linked_order: Boolean(safeText(safeFacts.order_id) || safeFacts.has_linked_order === true),
    has_active_quote: activeQuote,
    known_identity_fields: Object.freeze(known),
    missing_identity_fields: Object.freeze(IDENTITY_FIELDS.filter((field) => !known.includes(field))),
  });
}

function boundedCount(value) {
  const count = Number(value);
  return Number.isSafeInteger(count) && count >= 1 && count <= 20 ? count : null;
}

function safeText(value) { return String(value ?? '').trim(); }
