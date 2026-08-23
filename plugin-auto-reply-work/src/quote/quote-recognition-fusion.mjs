import { cinemaHintFromSupplement, cityHintFromSupplement } from '../quote-supplement.mjs';
import { ticketCountFromText } from './quote-text-facts.mjs';

function text(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

function presentText(value) {
  return typeof value === 'string' && value.trim() ? value : null;
}

export function mergeRecognitionWithTextFacts(recognition, parsedText, content = '') {
  const textRecognition = parsedText?.status === 'recognized' && parsedText?.recognition
    ? parsedText.recognition
    : {};
  // Explicit bounded location supplements may fill facts omitted by the
  // screenshot. They never alter platform, prices, or official seat selection.
  const city = presentText(recognition?.city) ?? presentText(textRecognition.city) ?? presentText(cityHintFromSupplement(content));
  const imageCinema = presentText(recognition?.cinema);
  const cinemaHint = presentText(cinemaHintFromSupplement(content));
  const genericImageCinema = /^(?:万达|万达影城|万达影院)$/u.test(String(imageCinema ?? '').replace(/[（）()\s]/gu, ''));
  const cinema = imageCinema && !genericImageCinema
    ? imageCinema
    : presentText(textRecognition.cinema) ?? cinemaHint ?? imageCinema;
  return {
    ...recognition,
    ...(city ? { city } : {}),
    ...(cinema ? { cinema } : {}),
    ...(presentText(recognition?.movie) ?? presentText(textRecognition.movie) ? { movie: presentText(recognition?.movie) ?? textRecognition.movie } : {}),
    ...(recognition?.date ?? textRecognition.date ? { date: recognition?.date ?? textRecognition.date } : {}),
    ...(presentText(recognition?.showtime) ?? presentText(textRecognition.showtime) ? { showtime: presentText(recognition?.showtime) ?? textRecognition.showtime } : {}),
    ...(presentText(recognition?.hall) ?? presentText(textRecognition.hall) ? { hall: presentText(recognition?.hall) ?? textRecognition.hall } : {}),
  };
}

function quoteDraftFacts(value) {
  const fields = value?.fields && typeof value.fields === 'object' && !Array.isArray(value.fields) ? value.fields : {};
  const artifact = value?.recognition_artifact?.recognition;
  const artifactRecognition = artifact && typeof artifact === 'object' && !Array.isArray(artifact) ? artifact : {};
  const field = (name, maximum) => {
    const item = fields[name];
    const raw = item && typeof item === 'object' && !Array.isArray(item) ? item.value : '';
    return text(raw || artifactRecognition[name], maximum);
  };
  const officialSelection = artifactRecognition.official_selection && typeof artifactRecognition.official_selection === 'object'
    && !Array.isArray(artifactRecognition.official_selection) ? artifactRecognition.official_selection : null;
  const storedTicketCount = Number(fields.ticket_count?.value);
  const officialTicketCount = officialSelection?.is_selected === true
    ? Number(officialSelection.selected_count || officialSelection.selected_seat_numbers?.length)
    : null;
  const ticketCount = Number.isInteger(storedTicketCount) && storedTicketCount >= 1 && storedTicketCount <= 20
    ? storedTicketCount
    : Number.isInteger(officialTicketCount) && officialTicketCount >= 1 && officialTicketCount <= 20 ? officialTicketCount : null;
  return {
    recognition: {
      ...artifactRecognition,
      ...(field('city', 80) ? { city: field('city', 80) } : {}),
      ...(field('cinema', 160) ? { cinema: field('cinema', 160) } : {}),
      ...(field('movie', 160) ? { movie: field('movie', 160) } : {}),
      ...(field('date', 32) ? { date: field('date', 32) } : {}),
      ...(field('showtime', 32) ? { showtime: field('showtime', 32) } : {}),
      ...(field('hall', 80) ? { hall: field('hall', 80) } : {}),
      ...(officialSelection ? { official_selection: officialSelection } : {}),
    },
    ticket_count: ticketCount,
    field_sources: Object.fromEntries(['city', 'cinema', 'movie', 'date', 'showtime', 'hall', 'ticket_count'].map((name) => [name, text(fields[name]?.source, 40) || (name === 'ticket_count' && officialTicketCount ? 'image' : 'conversation_draft')]).filter(([name, source]) => name === 'ticket_count' ? ticketCount : source !== 'conversation_draft' || Boolean(field(name, name === 'hall' ? 80 : 160)))),
  };
}

function timeSupplement(value) {
  const match = String(value ?? '').match(/((?:[01]?\d|2[0-3]))\s*[:：∶.]\s*([0-5]\d)/u);
  return match ? `${String(match[1]).padStart(2, '0')}:${match[2]}` : '';
}

function movieSupplement(value) {
  const match = String(value ?? '').match(/(?:^|\n)\s*(?:影片|电影|片名)\s*[:：]\s*([^\n]{1,160})/u);
  return text(match?.[1], 160);
}

export function textFactsWithQuoteDraft(parsedText, quoteDraft, content) {
  const draft = quoteDraftFacts(quoteDraft);
  const parsed = parsedText?.status === 'recognized' ? parsedText : null;
  const city = cityHintFromSupplement(content);
  const cinemaSupplement = cinemaHintFromSupplement(content);
  const existingCinema = parsed?.recognition?.cinema || draft.recognition.cinema;
  const genericCinema = /^(?:万达|万达影城|万达影院)$/u.test(String(existingCinema ?? '').replace(/[（）()\s]/gu, ''));
  const supplement = {
    city,
    cinema: cinemaSupplement && (!existingCinema || genericCinema) ? cinemaSupplement : '',
    movie: movieSupplement(content),
    showtime: timeSupplement(content),
    hall: text(String(content ?? '').match(/第?\s*\d{1,2}\s*号?\s*(?:放映厅|影厅|厅)/u)?.[0].replace(/\s+/gu, ''), 80),
  };
  const source = (name, value) => parsed?.recognition?.[name]
    ? parsed.field_sources?.[name] || 'buyer_text'
    : supplement[name] ? 'buyer_text' : draft.field_sources[name] || '';
  const draftOfficialSelection = draft.recognition.official_selection && typeof draft.recognition.official_selection === 'object'
    && !Array.isArray(draft.recognition.official_selection) ? draft.recognition.official_selection : null;
  const recognition = {
    ...draft.recognition,
    ...(draft.recognition.city || parsed?.recognition?.city || supplement.city ? { city: parsed?.recognition?.city || supplement.city || draft.recognition.city } : {}),
    ...(draft.recognition.cinema || parsed?.recognition?.cinema || supplement.cinema ? { cinema: parsed?.recognition?.cinema || supplement.cinema || draft.recognition.cinema } : {}),
    ...(draft.recognition.movie || parsed?.recognition?.movie || supplement.movie ? { movie: parsed?.recognition?.movie || supplement.movie || draft.recognition.movie } : {}),
    ...(draft.recognition.date || parsed?.recognition?.date ? { date: parsed?.recognition?.date || draft.recognition.date } : {}),
    ...(draft.recognition.showtime || parsed?.recognition?.showtime || supplement.showtime ? { showtime: parsed?.recognition?.showtime || supplement.showtime || draft.recognition.showtime } : {}),
    ...(draft.recognition.hall || parsed?.recognition?.hall || supplement.hall ? { hall: parsed?.recognition?.hall || supplement.hall || draft.recognition.hall } : {}),
    official_selection: draftOfficialSelection?.is_selected === true
      ? draftOfficialSelection
      : parsed?.recognition?.official_selection ?? draftOfficialSelection ?? { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
  };
  const ticketCount = parsed?.ticket_count ?? ticketCountFromText(content) ?? draft.ticket_count;
  const complete = recognition.cinema && recognition.movie && recognition.date && recognition.showtime;
  if (!complete) return parsedText;
  return Object.freeze({
    status: 'recognized', text_quote: true, ticket_count: ticketCount,
    ...(parsed?.semantic_source ? { semantic_source: parsed.semantic_source } : {}),
    recognition: { image_type: 'UNKNOWN', ...recognition },
    field_sources: Object.fromEntries([
      ...['city', 'cinema', 'movie', 'date', 'showtime', 'hall'].map((name) => [name, source(name, recognition[name])]).filter(([, value]) => value),
      ...(Number.isInteger(ticketCount) && ticketCount > 0 ? [['ticket_count', parsed?.ticket_count ? 'buyer_text' : draft.field_sources.ticket_count || 'buyer_text']] : []),
    ]),
  });
}

export function fieldSourcesForRecognition(recognition, textFacts, visionRecognition, content = '') {
  const sources = {};
  const supplementHints = { city: cityHintFromSupplement(content), cinema: cinemaHintFromSupplement(content) };
  for (const field of ['city', 'cinema', 'movie', 'date', 'showtime', 'hall']) {
    if (presentText(recognition?.[field])) sources[field] = presentText(visionRecognition?.[field])
      ? 'image'
      : textFacts?.field_sources?.[field] || (supplementHints[field] ? 'buyer_text' : 'image_or_text');
  }
  const ticketCount = Number(textFacts?.ticket_count);
  if (Number.isInteger(ticketCount) && ticketCount > 0) sources.ticket_count = textFacts?.field_sources?.ticket_count || 'buyer_text';
  return sources;
}

export function reusableSameImageRecognition(quoteDraft, sourceImageUrl) {
  if (!sourceImageUrl || Number(quoteDraft?.expires_at ?? 0) <= Date.now()
    || text(quoteDraft?.last_image, 2_000) !== sourceImageUrl
    || quoteDraft?.recognition_artifact?.status !== 'recognized') return null;
  const draft = quoteDraftFacts(quoteDraft);
  if (!isQuoteEligibleRecognition(draft.recognition)) return null;
  return Object.freeze({
    status: 'recognized', ticket_count: draft.ticket_count, recognition_reused: 'same_image_quote_draft',
    recognition: Object.freeze(structuredClone(draft.recognition)),
    field_sources: Object.freeze({ ...draft.field_sources }),
  });
}

export function repeatedQuoteDraft(quoteDraft, textFacts, sourceImageUrl) {
  const draft = quoteDraftFacts(quoteDraft);
  const lastAttempt = text(quoteDraft?.last_attempt_fingerprint, 2_500);
  const lastImage = text(quoteDraft?.last_image, 2_000);
  if (!lastAttempt || textFacts?.status !== 'recognized' || (sourceImageUrl && sourceImageUrl !== lastImage)) return false;
  const values = ['cinema', 'movie', 'date', 'showtime', 'hall'].map((name) => text(textFacts.recognition?.[name], name === 'hall' ? 80 : 160));
  const ticketCount = Number(textFacts.ticket_count);
  if (values.some((value) => !value) || !Number.isInteger(ticketCount) || ticketCount < 1 || ticketCount > 20) return false;
  const fingerprint = [...values, String(ticketCount), lastImage].join('\u001f');
  return fingerprint === lastAttempt;
}

export function canUseTextQuoteAfterIgnoredImage(recognition) {
  const platform = text(recognition?.platform, 40).toUpperCase();
  const cinema = text(recognition?.cinema, 160);
  // An identified non-Wanda screenshot must remain a refusal, and an order
  // confirmation must never be turned into a new quote by nearby text.
  return recognition?.image_type !== 'ORDER_CONFIRM'
    && !['MAOYAN', 'TAOPIAOPIAO'].includes(platform)
    && (!cinema || cinema.includes('万达'));
}

export function isQuoteEligibleRecognition(recognition) {
  const selected = recognition?.official_selection;
  return recognition?.image_type === 'SEAT_MAP'
    || (recognition?.image_type === 'ORDER_CONFIRM'
      && selected?.is_selected === true
      && Array.isArray(selected?.selected_seat_numbers)
      && selected.selected_seat_numbers.length > 0);
}

export function mergeImageRecognitions(recognitions) {
  if (!Array.isArray(recognitions) || !recognitions.length) return null;
  let primaryIndex = recognitions.length - 1;
  for (let index = recognitions.length - 1; index >= 0; index -= 1) {
    if (isQuoteEligibleRecognition(recognitions[index])) { primaryIndex = index; break; }
  }
  const merged = { ...recognitions[primaryIndex] };
  for (const [index, support] of recognitions.entries()) {
    if (index === primaryIndex || !support || typeof support !== 'object') continue;
    for (const field of ['city', 'cinema', 'cinema_address_hint', 'movie', 'date', 'showtime', 'hall', 'language_format']) {
      if (!presentText(merged[field]) && presentText(support[field])) merged[field] = support[field];
    }
  }
  return merged;
}
