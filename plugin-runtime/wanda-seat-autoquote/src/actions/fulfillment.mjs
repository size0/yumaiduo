import { createHash } from 'node:crypto';

const MAX_MESSAGE_LENGTH = 4_000;
const TICKET_CODE_PATTERN = /^[\p{L}\p{N}][\p{L}\p{N}_./:=-]{0,119}$/u;
const SHOWTIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/u;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/u;
const IMAGE_HOSTS = ['alicdn.com', 'tbcdn.cn'];

export class FulfillmentRequestError extends Error {
  constructor(code, status = 422) {
    super(code);
    this.name = 'FulfillmentRequestError';
    this.code = code;
    this.status = status;
  }
}

function text(value, field, { required = false, max = 240 } = {}) {
  const result = String(value ?? '').trim();
  if (required && !result) throw new FulfillmentRequestError(`${field}_required`);
  if (result.length > max) throw new FulfillmentRequestError(`${field}_too_long`);
  return result || null;
}

function showtime(value, field, required = false) {
  const result = text(value, field, { required, max: 5 });
  if (result && !SHOWTIME_PATTERN.test(result)) throw new FulfillmentRequestError(`${field}_invalid`);
  return result;
}
function date(value, field) {
  const result = text(value, field, { max: 10 });
  if (result && !DATE_PATTERN.test(result)) throw new FulfillmentRequestError(`${field}_invalid`);
  return result;
}
function code(value) {
  return text(value, 'ticket_code', { required: true, max: 120 }).replace(/\s+/gu, '');
}
function compactCode(value) { return code(value); }

export function ticketImageUrl(input) {
  const value = text(input?.ticket_image_url ?? input?.ticketImageUrl ?? input?.image_url ?? input?.imageUrl, 'ticket_image_url', { max: 2_048 });
  if (!value) return null;
  let parsed;
  try { parsed = new URL(value); } catch { throw new FulfillmentRequestError('ticket_image_url_invalid'); }
  const hostname = parsed.hostname.toLowerCase().replace(/\.$/u, '');
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.port && parsed.port !== '443'
    || !IMAGE_HOSTS.some((suffix) => hostname === suffix || hostname.endsWith(`.${suffix}`))) {
    throw new FulfillmentRequestError('ticket_image_url_invalid');
  }
  return value;
}

export function fulfillmentRequestFromTicketImage(input, recognized) {
  if (!recognized || typeof recognized !== 'object') throw new FulfillmentRequestError('ticket_image_recognition_invalid', 502);
  const ticketCodes = Array.isArray(recognized.ticket_codes) ? recognized.ticket_codes.map(compactCode) : [];
  if (!ticketCodes.length) throw new FulfillmentRequestError('ticket_code_not_recognized', 422);
  const dateMatch = String(recognized.date || recognized.date_text || '').match(/(\d{4})[/-](\d{1,2})[/-](\d{1,2})/u);
  const showDate = dateMatch ? `${dateMatch[1]}-${dateMatch[2].padStart(2, '0')}-${dateMatch[3].padStart(2, '0')}` : null;
  if (!showDate) throw new FulfillmentRequestError('ticket_date_not_recognized', 422);
  const seats = Array.isArray(recognized.selected_seats)
    ? recognized.selected_seats.map((item) => typeof item === 'string' ? item : item?.seat_number).filter(Boolean)
    : [];
  const mapped = {
    ...input,
    city: recognized.city,
    movie_name: recognized.movie_name,
    cinema_name: recognized.cinema_name,
    show_date: showDate,
    showtime_start: recognized.showtime_start,
    showtime_end: recognized.showtime_end,
    hall_name: recognized.hall_name,
    seats,
    ticket_codes: ticketCodes,
  };
  const suppliedMessage = String(mapped.message_text ?? mapped.messageText ?? '').trim();
  if (!suppliedMessage || ticketCodes.some((ticketCode) => !suppliedMessage.includes(ticketCode))) {
    mapped.message_text = [
      `电影：${mapped.movie_name}`,
      `影院：${mapped.cinema_name}`,
      `场次：${mapped.show_date || ''} ${mapped.showtime_start}${mapped.showtime_end ? `-${mapped.showtime_end}` : ''}`.trim(),
      `影厅：${mapped.hall_name}`,
      seats.length ? `座位：${seats.join('、')}` : null,
      `取票码：${ticketCodes.join('、')}`,
    ].filter(Boolean).join('\\n');
  }
  delete mapped.ticket_image_url;
  delete mapped.ticketImageUrl;
  delete mapped.image_url;
  delete mapped.imageUrl;
  return mapped;
}

export function normalizeFulfillmentRequest(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw new FulfillmentRequestError('fulfillment_body_invalid', 400);
  }
  const rawCodes = input.ticket_codes ?? input.ticketCodes ?? [];
  if (!Array.isArray(rawCodes) || rawCodes.length > 20) {
    throw new FulfillmentRequestError('ticket_codes_invalid');
  }
  const ticketCodes = rawCodes.map(code);
  if (ticketCodes.some((value) => !TICKET_CODE_PATTERN.test(value))) {
    throw new FulfillmentRequestError('ticket_code_invalid');
  }
  if (new Set(ticketCodes).size !== ticketCodes.length) {
    throw new FulfillmentRequestError('ticket_codes_duplicate');
  }
  const ticketUrl = text(input.ticket_url ?? input.ticketUrl, 'ticket_url', { max: 1_000 });
  if (ticketUrl && !/^https:\/\//iu.test(ticketUrl)) throw new FulfillmentRequestError('ticket_url_invalid');
  if (ticketCodes.length === 0 && !ticketUrl) throw new FulfillmentRequestError('ticket_codes_or_url_required');
  const messageText = text(input.message_text ?? input.messageText, 'message_text', { required: true, max: MAX_MESSAGE_LENGTH });
  const compactMessageText = messageText.replace(/\s+/gu, '');
  if (ticketCodes.some((code) => !compactMessageText.includes(code))) {
    throw new FulfillmentRequestError('message_missing_ticket_code');
  }
  const seats = input.seats ?? [];
  if (!Array.isArray(seats) || seats.length > 30) throw new FulfillmentRequestError('seats_invalid');
  const normalizedSeats = seats.map((value) => text(value, 'seat', { required: true, max: 80 }));
  return Object.freeze({
    city: text(input.city, 'city', { required: true, max: 80 }),
    show_date: date(input.show_date ?? input.showDate, 'show_date'),
    movie_name: text(input.movie_name ?? input.movieName, 'movie_name', { required: true, max: 160 }),
    cinema_name: text(input.cinema_name ?? input.cinemaName, 'cinema_name', { required: true, max: 240 }),
    showtime_start: showtime(input.showtime_start ?? input.showtimeStart, 'showtime_start', true),
    showtime_end: showtime(input.showtime_end ?? input.showtimeEnd, 'showtime_end'),
    hall_name: text(input.hall_name ?? input.hallName, 'hall_name', { required: true, max: 120 }),
    seats: Object.freeze(normalizedSeats),
    ticket_codes: Object.freeze(ticketCodes),
    ticket_url: ticketUrl,
    message_text: messageText,
    shop_id: text(input.shop_id ?? input.shopId, 'shop_id', { max: 160 }),
    buyer_id: text(input.buyer_id ?? input.buyerId, 'buyer_id', { max: 160 }),
    chat_id: text(input.chat_id ?? input.chatId, 'chat_id', { max: 160 }),
  });
}

export function fulfillmentFingerprint({ tenantId, orderId, order, request }) {
  const material = {
    tenant_id: String(tenantId ?? '').trim(),
    order_id: String(orderId ?? '').trim(),
    official: {
      shop_id: String(order?.shop_id ?? '').trim(),
      buyer_id: String(order?.buyer_id ?? '').trim(),
      chat_id: String(order?.chat_id ?? '').trim(),
    },
    request,
  };
  return createHash('sha256').update(JSON.stringify(material)).digest('hex');
}

export function fulfillmentIdentity({ tenantId, orderId, order }) {
  return {
    tenant_id: String(tenantId ?? '').trim(),
    order_id: String(orderId ?? '').trim(),
    shop_id: String(order?.shop_id ?? '').trim(),
    buyer_id: String(order?.buyer_id ?? '').trim(),
    chat_id: String(order?.chat_id ?? '').trim(),
  };
}

export function sameFulfillmentFingerprint(record, fingerprint) {
  return String(record?.request_fingerprint ?? '') === String(fingerprint ?? '') && Boolean(fingerprint);
}
