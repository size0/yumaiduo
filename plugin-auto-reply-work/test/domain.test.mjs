import assert from 'node:assert/strict';
import test from 'node:test';

import {
  SelectionMode,
  calculateQuote,
  isAutomaticVisionDecision,
  validatePriceChangeGate,
  validateVisionResult,
  visionResultToMatchInput,
} from '../src/domain.mjs';

test('W+ marked seats use original price minus 2.9 yuan per ticket', () => {
  const quote = calculateQuote({
    selectionMode: SelectionMode.WPLUS_MARKED,
    quantity: 3,
    wplusOriginalUnitCents: 4290,
    settings: { wplus_adjustment_cents: -290 },
  });

  assert.deepEqual(quote.unitQuotesCents, [4000, 4000, 4000]);
  assert.equal(quote.totalCents, 12000);
  assert.equal(quote.priceSource, 'wanda_wplus_original_adjusted');
});

test('regular selected seats use each official W+ price plus 1 yuan', () => {
  const quote = calculateQuote({
    selectionMode: SelectionMode.REGULAR_SELECTED,
    quantity: 2,
    officialQuotationItems: [
      { seatName: '6排7座', payPrice: 3790 },
      { seatName: '6排6座', payPrice: 4090 },
    ],
    settings: { regular_adjustment_cents: 100 },
  });

  assert.deepEqual(quote.unitQuotesCents, [3890, 4190]);
  assert.equal(quote.totalCents, 8080);
  assert.equal(quote.priceSource, 'wanda_official_wplus_adjusted');
});

test('unknown or mixed selection cannot produce a final quote', () => {
  assert.throws(() => calculateQuote({
    selectionMode: SelectionMode.UNKNOWN,
    quantity: 1,
  }), /cannot produce/);
});

test('vision result is schema checked before it can enter automation', () => {
  const vision = validateVisionResult({
    selection_mode: 'regular_selected',
    confidence: 0.96,
    seat_names: ['6排7座', '6排6座', '6排7座'],
    selected_count: 2,
    has_annotation: false,
    annotation_bbox: null,
    evidence: ['底部显示两个明确座位'],
  });

  assert.equal(vision.selectionMode, SelectionMode.REGULAR_SELECTED);
  assert.deepEqual(vision.seatNames, ['6排7座', '6排6座']);
  assert.equal(isAutomaticVisionDecision(vision, 0.95), true);
});

test('no-seat model output may use selected_count zero without failing the batch', () => {
  const vision = validateVisionResult({
    selection_mode: 'NONE', confidence: 0.98, seat_names: [], selected_count: 0,
    has_annotation: false, annotation_bbox: null, evidence: ['没有手绘圈选或原生已选座卡片'],
  });
  assert.equal(vision.selectedCount, null);
  assert.equal(isAutomaticVisionDecision(vision, 0.9), false);
});

test('regular selected seats are automatic only when every exact seat name is present', () => {
  const incomplete = validateVisionResult({
    selection_mode: 'REGULAR_SELECTED', confidence: 0.98, seat_names: ['6排7座'], selected_count: 2,
    has_annotation: false, annotation_bbox: null, evidence: ['底部有两个原生座位卡片'],
  });
  assert.equal(isAutomaticVisionDecision(incomplete, 0.9), false);
});

test('verified vision facts are converted to the Wanda match contract without invented values', () => {
  const vision = validateVisionResult({
    selection_mode: 'wplus_marked',
    confidence: 0.95,
    seat_names: [],
    selected_count: 2,
    has_annotation: true,
    annotation_bbox: { x: 0.2, y: 0.3, width: 0.4, height: 0.2 },
    evidence: ['橙色手绘圈选'],
    city: null,
    cinema_name: '莆田秀屿万达广场店',
    movie_name: '蝴蝶侠：崭新之日',
    showtime_text: '今天 19:20',
    original_unit_price_cents: 4290,
  });

  const match = visionResultToMatchInput(vision);
  assert.equal(match.auto_select_seats, false);
  assert.equal(match.hints.city, null);
  assert.equal(match.hints.cinema, '莆田秀屿万达广场店');
  assert.match(match.text, /电影：蝴蝶侠：崭新之日/u);
});

test('price change gate fails closed and reports every unmet condition', () => {
  const decision = validatePriceChangeGate({
    featureEnabled: true,
    uniqueShowtime: true,
    quantityConfirmed: false,
    selectionConfirmed: true,
    quoteValid: false,
    orderLinked: true,
    orderOwned: true,
    orderQuantityMatches: true,
    quoteAmountMatches: true,
    transportFeeSupported: true,
    orderUnpaid: true,
    humanTakeover: false,
    targetAmountCents: 8000,
  });

  assert.equal(decision.allowed, false);
  assert.deepEqual(decision.failures, ['quantity_not_confirmed', 'quote_expired']);
});

test('price change gate blocks amounts above the tenant safety limit', () => {
  const decision = validatePriceChangeGate({
    featureEnabled: true,
    uniqueShowtime: true,
    quantityConfirmed: true,
    selectionConfirmed: true,
    quoteValid: true,
    orderLinked: true,
    orderOwned: true,
    orderQuantityMatches: true,
    quoteAmountMatches: true,
    transportFeeSupported: true,
    orderUnpaid: true,
    humanTakeover: false,
    targetAmountCents: 250_000,
    maxTargetAmountCents: 200_000,
  });
  assert.deepEqual(decision.failures, ['target_amount_exceeds_limit']);
});
