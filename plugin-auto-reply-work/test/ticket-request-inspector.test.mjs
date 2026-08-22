import assert from 'node:assert/strict';
import test from 'node:test';
import { inspectTicketRequest, requestedTicketCount } from '../src/agent/ticket-request-inspector.mjs';

test('ticket request inspection never treats a seat coordinate as ticket quantity', () => {
  assert.equal(requestedTicketCount('要6排16，麻烦了'), null);
  assert.deepEqual(inspectTicketRequest({ message: '要6排16，麻烦了', hasImage: false, facts: {} }), {
    has_image: false, requested_ticket_count: null, has_typed_seat_instruction: true,
    has_linked_order: false, has_active_quote: false,
    known_identity_fields: [], missing_identity_fields: ['cinema', 'movie', 'date', 'showtime'],
  });
});

test('ticket request inspection returns only bounded deterministic facts', () => {
  assert.equal(requestedTicketCount('两张，7排中间两位'), 2);
  assert.deepEqual(inspectTicketRequest({
    message: '两张，7排中间两位', hasImage: true,
    facts: { cinema: '测试万达', movie: '测试电影', date: '2026-08-22', showtime: '10:00', order_id: 'secret-order', quote_total_cents: 10000, quote_ticket_count: 2, quote_expires_at: Date.now() + 60_000 },
    now: Date.now(),
  }), {
    has_image: true, requested_ticket_count: 2, has_typed_seat_instruction: true,
    has_linked_order: true, has_active_quote: true,
    known_identity_fields: ['cinema', 'movie', 'date', 'showtime'], missing_identity_fields: [],
  });
});
