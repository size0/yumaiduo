import { createHash } from 'node:crypto';

const MAX_MESSAGE_LENGTH = 4_000;
const TICKET_CODE_PATTERN = /^[\p{L}\p{N}][\p{L}\p{N}_./:=-]{0,119}$/u;
const SHOWTIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/u;

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

export function normalizeFulfillmentRequest(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw new FulfillmentRequestError('fulfillment_body_invalid', 400);
  }
  const rawCodes = input.ticket_codes ?? input.ticketCodes;
  if (!Array.isArray(rawCodes) || rawCodes.length < 1 || rawCodes.length > 20) {
    throw new FulfillmentRequestError('ticket_codes_invalid');
  }
  const ticketCodes = rawCodes.map((value) => text(value, 'ticket_code', { required: true, max: 120 }));
  if (ticketCodes.some((value) => !TICKET_CODE_PATTERN.test(value))) {
    throw new FulfillmentRequestError('ticket_code_invalid');
  }
  if (new Set(ticketCodes).size !== ticketCodes.length) {
    throw new FulfillmentRequestError('ticket_codes_duplicate');
  }
  const messageText = text(input.message_text ?? input.messageText, 'message_text', { required: true, max: MAX_MESSAGE_LENGTH });
  if (ticketCodes.some((code) => !messageText.includes(code))) {
    throw new FulfillmentRequestError('message_missing_ticket_code');
  }
  const seats = input.seats ?? [];
  if (!Array.isArray(seats) || seats.length > 30) throw new FulfillmentRequestError('seats_invalid');
  const normalizedSeats = seats.map((value) => text(value, 'seat', { required: true, max: 80 }));
  const ticketUrl = text(input.ticket_url ?? input.ticketUrl, 'ticket_url', { max: 1_000 });
  if (ticketUrl && !/^https:\/\//iu.test(ticketUrl)) throw new FulfillmentRequestError('ticket_url_invalid');
  return Object.freeze({
    city: text(input.city, 'city', { required: true, max: 80 }),
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
