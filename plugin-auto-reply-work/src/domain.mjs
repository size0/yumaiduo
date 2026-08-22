export const SelectionMode = Object.freeze({
  WPLUS_MARKED: 'WPLUS_MARKED',
  REGULAR_SELECTED: 'REGULAR_SELECTED',
  NONE: 'NONE',
  MIXED: 'MIXED',
  UNKNOWN: 'UNKNOWN',
});

export const TaskStatus = Object.freeze({
  RECEIVED: 'RECEIVED',
  RECOGNIZING: 'RECOGNIZING',
  NEEDS_SEAT_MARK: 'NEEDS_SEAT_MARK',
  NEEDS_QUANTITY: 'NEEDS_QUANTITY',
  QUOTED: 'QUOTED',
  WAITING_ORDER: 'WAITING_ORDER',
  PRICE_CHANGE_PENDING: 'PRICE_CHANGE_PENDING',
  WAITING_PAYMENT: 'WAITING_PAYMENT',
  PAID: 'PAID',
  REVIEW_REQUIRED: 'REVIEW_REQUIRED',
  FAILED: 'FAILED',
  CLOSED: 'CLOSED',
});

const AUTO_SELECTION_MODES = new Set([
  SelectionMode.WPLUS_MARKED,
  SelectionMode.REGULAR_SELECTED,
]);

function integer(value, name, { min = 0, max = 1_000_000_000 } = {}) {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new TypeError(`${name} must be an integer between ${min} and ${max}`);
  }
  return value;
}

function uniqueStrings(value, limit = 20) {
  if (!Array.isArray(value)) return [];
  const result = [];
  for (const item of value) {
    const text = String(item ?? '').trim();
    if (text && !result.includes(text)) result.push(text);
    if (result.length >= limit) break;
  }
  return result;
}

function optionalText(value, maxLength = 200) {
  if (value == null) return null;
  const text = String(value).replace(/\s+/gu, ' ').trim();
  return text ? text.slice(0, maxLength) : null;
}

export function validateVisionResult(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw new TypeError('vision result must be an object');
  }
  const selectionMode = String(input.selection_mode ?? '').trim().toUpperCase();
  if (!Object.values(SelectionMode).includes(selectionMode)) {
    throw new TypeError('vision selection_mode is invalid');
  }
  const confidence = Number(input.confidence);
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
    throw new TypeError('vision confidence must be between 0 and 1');
  }
  const rawSelectedCount = input.selected_count == null
    ? null
    : integer(Number(input.selected_count), 'selected_count', { min: 0, max: 20 });
  const selectedCount = rawSelectedCount === 0 ? null : rawSelectedCount;
  const annotationBox = input.annotation_bbox == null
    ? null
    : validateNormalizedBox(input.annotation_bbox);

  return Object.freeze({
    selectionMode,
    confidence,
    seatNames: uniqueStrings(input.seat_names),
    selectedCount,
    hasAnnotation: Boolean(input.has_annotation),
    annotationBox,
    evidence: uniqueStrings(input.evidence, 10),
    city: optionalText(input.city, 80),
    cinemaName: optionalText(input.cinema_name, 200),
    movieName: optionalText(input.movie_name, 200),
    showtimeText: optionalText(input.showtime_text, 100),
    hallName: optionalText(input.hall_name, 100),
    originalUnitPriceCents: input.original_unit_price_cents == null
      ? null
      : integer(Number(input.original_unit_price_cents), 'original_unit_price_cents', { min: 1, max: 1_000_000 }),
    promptVersion: String(input.prompt_version ?? '').trim(),
    model: String(input.model ?? '').trim(),
  });
}

export function visionResultToMatchInput(vision) {
  const lines = [
    vision.city && `城市：${vision.city}`,
    vision.cinemaName && `影院：${vision.cinemaName}`,
    vision.movieName && `电影：${vision.movieName}`,
    vision.showtimeText && `场次：${vision.showtimeText}`,
    vision.hallName && `影厅：${vision.hallName}`,
    vision.seatNames.length && `座位：${vision.seatNames.join('、')}`,
  ].filter(Boolean);
  return Object.freeze({
    text: lines.join('\n'),
    mode: 'template',
    auto_select_seats: false,
    hints: Object.freeze({
      city: vision.city,
      cinema: vision.cinemaName,
      movie: vision.movieName,
      showtime: vision.showtimeText,
      seats: vision.seatNames,
    }),
  });
}

function validateNormalizedBox(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError('annotation_bbox must be an object');
  }
  const box = {};
  for (const name of ['x', 'y', 'width', 'height']) {
    const number = Number(value[name]);
    if (!Number.isFinite(number) || number < 0 || number > 1) {
      throw new TypeError(`annotation_bbox.${name} must be between 0 and 1`);
    }
    box[name] = number;
  }
  if (box.x + box.width > 1.000001 || box.y + box.height > 1.000001) {
    throw new TypeError('annotation_bbox exceeds image bounds');
  }
  return Object.freeze(box);
}

export function isAutomaticVisionDecision(vision, minimumConfidence = 0.9) {
  if (!AUTO_SELECTION_MODES.has(vision.selectionMode) || vision.confidence < minimumConfidence) return false;
  if (vision.selectionMode === SelectionMode.WPLUS_MARKED) {
    return vision.hasAnnotation && Number.isSafeInteger(vision.selectedCount) && vision.selectedCount > 0;
  }
  return vision.seatNames.length > 0
    && vision.selectedCount === vision.seatNames.length;
}

export function calculateQuote(input) {
  const quantity = integer(Number(input.quantity), 'quantity', { min: 1, max: 20 });
  const wplusAdjustmentCents = integer(
    Number(input.settings?.wplus_adjustment_cents ?? -290),
    'wplus_adjustment_cents',
    { min: -100_000, max: 100_000 },
  );
  const regularAdjustmentCents = integer(
    Number(input.settings?.regular_adjustment_cents ?? 100),
    'regular_adjustment_cents',
    { min: -100_000, max: 100_000 },
  );

  if (input.selectionMode === SelectionMode.WPLUS_MARKED) {
    const originalUnit = integer(
      Number(input.wplusOriginalUnitCents),
      'wplusOriginalUnitCents',
      { min: 1 },
    );
    const unitQuote = originalUnit + wplusAdjustmentCents;
    if (unitQuote <= 0) throw new RangeError('W+ quote must be positive');
    return Object.freeze({
      selectionMode: input.selectionMode,
      quantity,
      unitQuotesCents: Array(quantity).fill(unitQuote),
      totalCents: unitQuote * quantity,
      priceSource: 'wanda_wplus_original_adjusted',
    });
  }

  if (input.selectionMode === SelectionMode.REGULAR_SELECTED) {
    if (!Array.isArray(input.officialQuotationItems) || input.officialQuotationItems.length !== quantity) {
      throw new RangeError('official quotation items must match confirmed quantity');
    }
    const unitQuotesCents = input.officialQuotationItems.map((item, index) => {
      const memberPrice = integer(
        Number(item?.payPrice ?? item?.memberPrice ?? item?.wplusPrice),
        `officialQuotationItems[${index}].payPrice`,
        { min: 1 },
      );
      return memberPrice + regularAdjustmentCents;
    });
    if (unitQuotesCents.some((value) => value <= 0)) {
      throw new RangeError('regular seat quote must be positive');
    }
    return Object.freeze({
      selectionMode: input.selectionMode,
      quantity,
      unitQuotesCents,
      totalCents: unitQuotesCents.reduce((sum, value) => sum + value, 0),
      priceSource: 'wanda_official_wplus_adjusted',
    });
  }

  throw new RangeError(`selection mode ${input.selectionMode} cannot produce a final quote`);
}

export function validatePriceChangeGate(input) {
  const failures = [];
  if (!input.featureEnabled) failures.push('price_change_disabled');
  if (!input.uniqueShowtime) failures.push('showtime_not_unique');
  if (!input.quantityConfirmed) failures.push('quantity_not_confirmed');
  if (!input.selectionConfirmed) failures.push('selection_not_confirmed');
  if (!input.quoteValid) failures.push('quote_expired');
  if (!input.orderLinked) failures.push('order_not_linked');
  if (!input.orderOwned) failures.push('order_account_mismatch');
  if (!input.orderQuantityMatches) failures.push('order_quantity_mismatch');
  if (!input.quoteAmountMatches) failures.push('quote_amount_mismatch');
  if (!input.transportFeeSupported) failures.push('transport_fee_not_supported');
  if (!input.orderUnpaid) failures.push('order_not_unpaid');
  if (input.humanTakeover) failures.push('human_takeover');
  const targetAmountCents = integer(Number(input.targetAmountCents), 'targetAmountCents', { min: 1, max: 10_000_000_000 });
  const maxTargetAmountCents = integer(
    Number(input.maxTargetAmountCents ?? 200_000),
    'maxTargetAmountCents',
    { min: 1_000, max: 1_000_000 },
  );
  if (targetAmountCents > maxTargetAmountCents) failures.push('target_amount_exceeds_limit');
  return Object.freeze({ allowed: failures.length === 0, failures });
}
