import assert from 'node:assert/strict';
import test from 'node:test';
import {
  FulfillmentRequestError,
  fulfillmentFingerprint,
  fulfillmentRequestFromTicketImage,
  normalizeFulfillmentRequest,
  sameFulfillmentFingerprint,
  ticketImageUrl,
} from '../src/actions/fulfillment.mjs';

function request(overrides = {}) {
  return {
    city: '昆明', movie_name: '奥德赛', cinema_name: '昆明西山万达广场店',
    showtime_start: '20:10', showtime_end: '22:50', hall_name: 'IMAX厅',
    seats: ['5排6座'], ticket_codes: ['WANDA-001'],
    message_text: '电影：奥德赛\n取票码：WANDA-001', ...overrides,
  };
}

function errorCode(work) {
  try { work(); } catch (error) { return error.code; }
  return null;
}

test('fulfillment request requires authoritative movie and showtime facts', () => {
  const normalized = normalizeFulfillmentRequest(request());
  assert.equal(normalized.movie_name, '奥德赛');
  assert.equal(normalized.showtime_start, '20:10');
  assert.deepEqual(normalized.ticket_codes, ['WANDA-001']);
  assert.equal(errorCode(() => normalizeFulfillmentRequest(request({ movie_name: '' }))), 'movie_name_required');
  assert.equal(errorCode(() => normalizeFulfillmentRequest(request({ showtime_start: '25:00' }))), 'showtime_start_invalid');
});

test('ticket fulfillment image maps authoritative facts and normalizes the printed code', () => {
  const source = {
    ticket_image_url: 'https://img.alicdn.com/ticket.png',
    shop_id: 'shop-1', buyer_id: 'buyer-1', chat_id: 'chat-1',
  };
  assert.equal(ticketImageUrl(source), source.ticket_image_url);
  const mapped = fulfillmentRequestFromTicketImage(source, {
    city: '运城', movie_name: '八仙！', cinema_name: '运城万达广场店',
    date: '2026-08-31', showtime_start: '15:30', showtime_end: '17:54',
    hall_name: '9号4DX厅', selected_seats: ['5排6座', '5排7座'],
    ticket_codes: ['2071 1100 0167 90'],
  });
  const normalized = normalizeFulfillmentRequest(mapped);
  assert.equal(normalized.show_date, '2026-08-31');
  assert.deepEqual(normalized.ticket_codes, ['20711100016790']);
  assert.match(normalized.message_text, /运城万达广场店/u);
  assert.match(normalized.message_text, /20711100016790/u);
});

test('ticket fulfillment image URL is restricted to approved HTTPS CDNs', () => {
  assert.equal(ticketImageUrl({ ticket_image_url: 'https://img.alicdn.com/ticket.png' }), 'https://img.alicdn.com/ticket.png');
  assert.equal(errorCode(() => ticketImageUrl({ ticket_image_url: 'http://img.alicdn.com/ticket.png' })), 'ticket_image_url_invalid');
  assert.equal(errorCode(() => ticketImageUrl({ ticket_image_url: 'https://evil.example/ticket.png' })), 'ticket_image_url_invalid');
});

test('fulfillment request rejects duplicate or unverified ticket codes', () => {
  assert.equal(errorCode(() => normalizeFulfillmentRequest(request({ ticket_codes: ['A-1', 'A-1'], message_text: 'A-1' }))), 'ticket_codes_duplicate');
  assert.equal(errorCode(() => normalizeFulfillmentRequest(request({ ticket_codes: ['A-1', 'B-2'], message_text: 'A-1' }))), 'message_missing_ticket_code');
  assert.equal(errorCode(() => normalizeFulfillmentRequest(request({ ticket_codes: ['A@1'] }))), 'ticket_code_invalid');
  assert.equal(errorCode(() => normalizeFulfillmentRequest(request({ ticket_url: 'http://unsafe.test/ticket' }))), 'ticket_url_invalid');
});

test('fulfillment accepts printed ticket codes with display spacing', () => {
  const normalized = normalizeFulfillmentRequest(request({
    ticket_codes: ['2079 5600 0622 50'], message_text: '电影：奥德赛\n取票码：2079 5600 0622 50',
  }));
  assert.deepEqual(normalized.ticket_codes, ['20795600062250']);
});

test('fulfillment accepts a QR ticket image without requiring text ticket codes', () => {
  const normalized = normalizeFulfillmentRequest(request({
    ticket_codes: [], ticket_url: 'https://wd.xdw0.cn/api/xianyu-plugin/delivery-assets/qr.png',
    message_text: '电影：奥德赛\n二维码取票码已上传，请查收。',
  }));
  assert.deepEqual(normalized.ticket_codes, []);
  assert.equal(normalized.ticket_url, 'https://wd.xdw0.cn/api/xianyu-plugin/delivery-assets/qr.png');
  assert.equal(errorCode(() => normalizeFulfillmentRequest(request({ ticket_codes: [], message_text: '没有二维码' }))), 'ticket_codes_or_url_required');
});

test('fulfillment fingerprint is unique to the tenant, order, identity and ticket facts', () => {
  const order = { shop_id: 'shop-1', buyer_id: 'buyer-1', chat_id: 'chat-1' };
  const first = fulfillmentFingerprint({ tenantId: 'tenant-1', orderId: 'order-1', order, request: normalizeFulfillmentRequest(request()) });
  assert.equal(sameFulfillmentFingerprint({ request_fingerprint: first }, first), true);
  assert.notEqual(first, fulfillmentFingerprint({ tenantId: 'tenant-1', orderId: 'order-2', order, request: normalizeFulfillmentRequest(request()) }));
  assert.notEqual(first, fulfillmentFingerprint({ tenantId: 'tenant-1', orderId: 'order-1', order, request: normalizeFulfillmentRequest(request({ ticket_codes: ['WANDA-002'], message_text: '取票码：WANDA-002' })) }));
  assert.equal(sameFulfillmentFingerprint({}, first), false);
  assert.throws(() => normalizeFulfillmentRequest(null), FulfillmentRequestError);
});
