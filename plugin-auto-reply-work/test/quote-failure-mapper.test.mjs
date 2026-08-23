import assert from 'node:assert/strict';
import test from 'node:test';
import {
  quoteFailure,
  quoteFailureReplyText,
  safeMatchCandidate,
} from '../src/quote/quote-failure-mapper.mjs';

test('quote failure mapper keeps only bounded operator-safe diagnostics', () => {
  const result = quoteFailure({
    code: 'temporary_lock_release_unverified',
    reply_text: 'generic',
    diagnostics: {
      failure_step: 'locked_offer', safe_error_code: 'temporary_lock_release_unverified', upstream_status: 502,
      requested_match: { city: '牡丹江', cinema: '牡丹江万达广场店', movie: '奥德赛', date: '2026-08-23', showtime: '17:15', hall: '6号IMAX厅', token: 'forbidden' },
      match: { result_count: 1, cinema: '牡丹江万达广场店', showtime: '17:15', raw: 'forbidden' },
      realtime_areas: [{ area_code: '36', label: 'W+专享', available_seat_count: 16, token: 'forbidden' }],
      raw_response: 'forbidden',
    },
  }, 502);

  assert.equal(result.code, 'temporary_lock_release_unverified');
  assert.equal(result.diagnostics.failure_step, 'locked_offer');
  assert.equal(result.diagnostics.realtime_areas[0].available_seat_count, 16);
  assert.equal(JSON.stringify(result).includes('forbidden'), false);
  assert.match(quoteFailureReplyText(result.code), /不代表该场会员座都不可售/u);
});

test('model match candidates expose only bounded identity differences and never seat facts', () => {
  const original = {
    cinema: '测试万达影城', movie: '奥德赛', date: '2026-08-23', showtime: '17:15', hall: '6号厅',
    official_selection: { is_selected: true, selected_seat_numbers: ['8排9座'], selected_count: 1 },
  };

  const candidate = safeMatchCandidate({
    city: '牡丹江', cinema: '另一家影城', movie: '奥德赛', date: '2026-08-23', showtime: '17:15', hall: '8号厅',
    seats: ['1排1座'], total_price: 1, token: 'forbidden',
  }, original);
  assert.deepEqual(candidate, { city: '牡丹江', cinema: '另一家影城', hall: '8号厅' });
  assert.equal(JSON.stringify(candidate).includes('1排1座'), false);
  assert.equal(safeMatchCandidate({ cinema: '测试万达影城', movie: '奥德赛', date: '2026-08-23', showtime: '17:15', hall: '6号厅' }, original), null);
});
