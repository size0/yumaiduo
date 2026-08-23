import assert from 'node:assert/strict';
import test from 'node:test';
import {
  parseTextQuoteRequest,
  semanticQuoteFacts,
  ticketCountFromText,
} from '../src/quote/quote-text-facts.mjs';

test('quote text fact module owns deterministic fallback parsing without transport dependencies', () => {
  const result = parseTextQuoteRequest(
    '济南世贸万达影城今日12:35开场的奥德赛这两个位置还有票吗',
    new Date('2026-08-23T03:00:00.000Z'),
  );

  assert.equal(result.status, 'needs_confirmation');
  assert.equal(result.ticket_count, 2);
  assert.equal(result.recognition.city, '济南');
  assert.equal(result.recognition.cinema, '济南世贸万达影城');
  assert.equal(result.recognition.movie, '奥德赛');
  assert.equal(result.recognition.date, '2026-08-23');
  assert.equal(result.recognition.showtime, '12:35');
  assert.deepEqual(result.missing_fields, ['这几个位置的完整选座截图']);
});

test('quote text fact module validates bounded AI facts before they enter recognition context', () => {
  const result = semanticQuoteFacts({
    status: 'extracted',
    extractor_version: 'wanda-quote-fact-extractor-v1',
    facts: {
      quote_intent: true,
      city: '济南', cinema: '世贸万达影城', movie: '奥德赛', date: '2026-08-23', showtime: '12:35', hall: null,
      ticket_count: 2, seat_numbers: [], requested_row: 8, refers_to_image_positions: true, confidence: 0.95,
    },
  }, { hasImage: true });

  assert.equal(result.status, 'recognized');
  assert.equal(result.semantic_source, 'wanda-quote-fact-extractor-v1');
  assert.equal(result.ticket_count, 2);
  assert.equal(result.requested_row, 8);
  assert.equal(result.field_sources.city, 'ai_text');
  assert.equal(result.recognition.official_selection.is_selected, false);
});

test('quote text fact module rejects low-confidence AI facts and never turns one seat coordinate into ticket quantity', () => {
  assert.equal(semanticQuoteFacts({
    status: 'extracted',
    facts: { quote_intent: true, confidence: 0.79 },
  }), null);
  assert.equal(ticketCountFromText('想要5排6座'), 1);
});
