import assert from 'node:assert/strict';
import test from 'node:test';
import {
  backendQuoteReplyText,
  quoteReplyText,
  recognitionReplyText,
  textQuoteReply,
} from '../src/quote/quote-response-presenter.mjs';

test('quote response presenter preserves authoritative backend copy and exact-seat totals', () => {
  assert.equal(backendQuoteReplyText({ reply_text: '第一行\n第二行' }), '第一行\n第二行');
  assert.equal(quoteReplyText({
    seat_quotes: [
      { seat_number: '8排9座', unit_quote_cents: 5010 },
      { seat_number: '8排10座', unit_quote_cents: 5020 },
    ],
    total_quote_cents: 10030,
  }), '按官方选座逐座实时核验：8排9座 50.10元、8排10座 50.20元；2张合计100.30元。');
});

test('quote response presenter labels text seats as unverified rather than official selection', () => {
  const reply = textQuoteReply({
    matched_cinema_name: '测试万达影城', unit_quote_cents: 5000, total_quote_cents: 10000, ticket_count: 2,
  }, { ticket_count: 2, recognition: { cinema: '买家输入影院' } });

  assert.match(reply, /测试万达影城.*50\.00元\/张.*2张合计100\.00元/u);
  assert.match(reply, /文字里的座位号未按平台选座核验/u);
});

test('quote response presenter returns bounded recognition identity without price authority', () => {
  const reply = recognitionReplyText({
    cinema: '测试万达影城', movie: '奥德赛', date: '2026-08-23', showtime: '17:15', hall: '6号厅',
    official_selection: { is_selected: true, selected_seat_numbers: ['8排9座'], selected_count: 1 },
  });
  assert.match(reply, /影院：测试万达影城/u);
  assert.match(reply, /座位：8排9座/u);
  assert.equal(/\d+\.\d+元/u.test(reply), false);
});
