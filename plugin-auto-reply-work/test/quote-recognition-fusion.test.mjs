import assert from 'node:assert/strict';
import test from 'node:test';
import {
  mergeImageRecognitions,
  mergeRecognitionWithTextFacts,
  textFactsWithQuoteDraft,
} from '../src/quote/quote-recognition-fusion.mjs';

test('recognition fusion preserves official image seats while filling omitted identity facts from text', () => {
  const officialSelection = {
    is_selected: true,
    selected_seat_numbers: ['9排14座', '9排15座'],
    selected_count: 2,
  };
  const result = mergeRecognitionWithTextFacts({
    image_type: 'SEAT_MAP',
    cinema: '万达影城（世茂杜比影院店）',
    official_selection: officialSelection,
  }, {
    status: 'recognized',
    recognition: {
      city: '济南', cinema: '买家猜测影院', movie: '奥德赛', date: '2026-08-23', showtime: '12:35',
      official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
    },
  });

  assert.equal(result.city, '济南');
  assert.equal(result.cinema, '万达影城（世茂杜比影院店）');
  assert.equal(result.movie, '奥德赛');
  assert.deepEqual(result.official_selection, officialSelection);
});

test('recognition fusion keeps the latest quote-eligible image as seat authority', () => {
  const result = mergeImageRecognitions([
    { image_type: 'DETAIL', cinema: '测试万达影城', movie: '奥德赛', hall: '6号厅' },
    {
      image_type: 'SEAT_MAP', showtime: '17:15',
      official_selection: { is_selected: true, selected_seat_numbers: ['8排9座'], selected_count: 1 },
    },
  ]);

  assert.equal(result.image_type, 'SEAT_MAP');
  assert.equal(result.cinema, '测试万达影城');
  assert.equal(result.movie, '奥德赛');
  assert.deepEqual(result.official_selection.selected_seat_numbers, ['8排9座']);
});

test('quote draft fusion never replaces official selected seats with typed text seats', () => {
  const result = textFactsWithQuoteDraft({
    status: 'recognized', ticket_count: 2,
    recognition: {
      cinema: '测试万达影城', movie: '奥德赛', date: '2026-08-23', showtime: '17:15',
      official_selection: { is_selected: false, selected_seat_numbers: ['8排10座', '8排11座'], selected_count: 0 },
    },
  }, {
    expires_at: Date.now() + 60_000,
    recognition_artifact: {
      recognition: {
        image_type: 'SEAT_MAP', cinema: '测试万达影城', movie: '奥德赛', date: '2026-08-23', showtime: '17:15',
        official_selection: { is_selected: true, selected_seat_numbers: ['8排9座'], selected_count: 1 },
      },
    },
    fields: { ticket_count: { value: 1, source: 'image' } },
  }, '改成8排10座、8排11座');

  assert.deepEqual(result.recognition.official_selection.selected_seat_numbers, ['8排9座']);
});
