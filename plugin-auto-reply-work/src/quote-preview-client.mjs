import { createHash } from 'node:crypto';
import { cinemaHintFromSupplement, cityHintFromSupplement } from './quote-supplement.mjs';
import { parseTextQuoteRequest, semanticQuoteFacts, ticketCountFromText } from './quote/quote-text-facts.mjs';
import {
  canUseTextQuoteAfterIgnoredImage, fieldSourcesForRecognition, isQuoteEligibleRecognition,
  mergeImageRecognitions, mergeRecognitionWithTextFacts, repeatedQuoteDraft, reusableSameImageRecognition,
  textFactsWithQuoteDraft,
} from './quote/quote-recognition-fusion.mjs';
import { quoteFailure, quoteFailureReplyText, safeMatchCandidate } from './quote/quote-failure-mapper.mjs';
import { backendQuoteReplyText, quoteReplyText, recognitionReplyText, textQuoteReply } from './quote/quote-response-presenter.mjs';

export { parseTextQuoteRequest } from './quote/quote-text-facts.mjs';

function text(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

function multilineText(value, maxLength) {
  return String(value ?? '')
    .replace(/\r\n?/gu, '\n')
    .replace(/[^\S\n]+/gu, ' ')
    .replace(/ *\n */gu, '\n')
    .replace(/\n{3,}/gu, '\n\n')
    .trim()
    .slice(0, maxLength);
}

function buyerLabel(payload) {
  const nick = text(payload?.buyerNick ?? payload?.buyer_nick ?? payload?.buyerName, 48);
  if (!nick) return '匿名买家';
  if (nick.length <= 2) return `${nick[0]}*`;
  return `${nick[0]}${'*'.repeat(Math.min(4, nick.length - 2))}${nick.at(-1)}`;
}

async function importImageForRecognition(sourceUrl, envelope, imageLoader, backendClient, loadedImage = null) {
  // IM image URLs can be short-lived or require CDN-specific request handling.
  // Import once through the plugin's SSRF-protected loader, then let V3 read the
  // stable COS object instead of downloading the buyer's original URL again.
  if (!imageLoader || !backendClient) return sourceUrl;
  const image = loadedImage ?? await imageLoader.load(sourceUrl);
  const uploaded = await backendClient.uploadTestImage({
    bytes: image.bytes,
    contentType: image.contentType,
    filename: `inbound-${text(envelope?.id, 80) || 'image'}`,
  }, {
    tenantId: text(envelope?.tenantId, 128),
    eventId: text(envelope?.id, 128),
  });
  const imageUrl = text(uploaded?.url, 2_000);
  if (!imageUrl) throw new Error('image import did not return a COS URL');
  return imageUrl;
}

function recognitionImageUrls(payload) {
  const values = Array.isArray(payload?.imageUrls) ? payload.imageUrls : [];
  return [...new Set(values.filter((item) => typeof item === 'string' && item.trim()).map((item) => item.trim()))].slice(-2);
}

function firstImageUrl(payload) {
  const value = recognitionImageUrls(payload)[0];
  if (value) return value.trim();

  const textUrl = text(payload?.content ?? payload?.text, 2_000);
  try {
    const parsed = new URL(textUrl);
    if (!['http:', 'https:'].includes(parsed.protocol)) return null;
    if (/\.(?:jpe?g|png|webp)(?:$|[?#])/iu.test(parsed.pathname)) return parsed.toString();
  } catch {
    // The message is normal text rather than an image URL.
  }
  return null;
}

export function createQuotePreviewClient(config, { fetchImpl = globalThis.fetch, imageLoader = null, backendClient = null } = {}) {
  const preview = config?.quotePreview;
  if (!preview) return null;
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');

  const usesTwoStageApi = Boolean(preview.recognizeUrl && preview.quoteUrl);
  const recognitionCache = new Map();
  const recognitionInFlight = new Map();
  const recognitionCacheTtlMs = 5 * 60_000;
  const maximumRecognitionCacheEntries = 100;
  const maximumConcurrentVisionRequests = 4;
  let activeVisionRequests = 0;
  const visionWaiters = [];

  async function withVisionSlot(operation) {
    if (activeVisionRequests >= maximumConcurrentVisionRequests) {
      await new Promise((resolve) => visionWaiters.push(resolve));
    }
    activeVisionRequests += 1;
    try { return await operation(); }
    finally {
      activeVisionRequests -= 1;
      visionWaiters.shift()?.();
    }
  }

  function pruneRecognitionCache(now = Date.now()) {
    for (const [key, entry] of recognitionCache) if (entry.expiresAt <= now) recognitionCache.delete(key);
    while (recognitionCache.size > maximumRecognitionCacheEntries) recognitionCache.delete(recognitionCache.keys().next().value);
  }

  async function recognizeSource(recognitionSource, index, recognitionSources, envelope, payload) {
    const account = text(payload?.accountUnb ?? payload?.account_unb, 128);
    const tenant = text(envelope?.tenantId, 128);
    const message = text(payload?.content ?? payload?.text, 1_000);
    let loadedImage = null;
    let cacheKey = '';
    if (account && imageLoader && backendClient) {
      loadedImage = await imageLoader.load(recognitionSource);
      const imageHash = createHash('sha256').update(loadedImage.bytes).digest('hex');
      const inputHash = createHash('sha256').update(`${message}\u001f${preview.recognizeUrl}`).digest('hex');
      cacheKey = `${tenant}:${account}:${imageHash}:${inputHash}`;
      pruneRecognitionCache();
      const cached = recognitionCache.get(cacheKey);
      if (cached) return { recognition: structuredClone(cached.recognition), reused: true };
      const pending = recognitionInFlight.get(cacheKey);
      if (pending) return { recognition: structuredClone(await pending), reused: true };
    }
    const operation = (async () => {
      const imageUrl = await importImageForRecognition(recognitionSource, envelope, imageLoader, backendClient, loadedImage);
      const response = await withVisionSlot(() => fetchJson(preview.recognizeUrl, {
        event_id: text(recognitionSources.length > 1 ? `${envelope?.id}:image-${index + 1}` : envelope?.id, 128),
        tenant_id: tenant,
        buyer_label: buyerLabel(payload),
        message_text: message,
        image_url: imageUrl,
        ...(ticketCountFromText(payload?.content ?? payload?.text) ? { ticket_count: ticketCountFromText(payload?.content ?? payload?.text) } : {}),
      }, 'quote preview recognition'));
      const item = response?.recognition;
      if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error('quote preview recognition returned an invalid response');
      if (cacheKey) {
        recognitionCache.set(cacheKey, { recognition: structuredClone(item), expiresAt: Date.now() + recognitionCacheTtlMs });
        pruneRecognitionCache();
      }
      return item;
    })();
    if (cacheKey) recognitionInFlight.set(cacheKey, operation);
    try { return { recognition: await operation, reused: false }; }
    finally { if (cacheKey && recognitionInFlight.get(cacheKey) === operation) recognitionInFlight.delete(cacheKey); }
  }

  async function extractSemanticTextFacts(envelope, payload, hasImage) {
    const rawMessage = multilineText(payload?.content ?? payload?.text, 4_000);
    const message = rawMessage.replace(/https?:\/\/[^\s,，。！？、;；]+/giu, ' ').replace(/\s+/gu, ' ').trim();
    if (!preview.textFactUrl || !message) return null;
    try {
      const response = await fetchJson(preview.textFactUrl, {
        event_id: text(envelope?.id, 200) || 'unknown-event', tenant_id: text(envelope?.tenantId, 128) || 'unknown-tenant',
        message_text: message, observed_at: Number.isSafeInteger(Number(envelope?.ts)) && Number(envelope.ts) >= 0 ? Number(envelope.ts) : Date.now(),
      }, 'quote text fact extraction');
      return semanticQuoteFacts(response, { hasImage });
    } catch {
      return null;
    }
  }

  async function recognize(envelope) {
    const payload = envelope?.payload ?? {};
    const sourceImageUrls = recognitionImageUrls(payload);
    const sourceImageUrl = sourceImageUrls.at(-1) ?? firstImageUrl(payload);
    const fallbackText = parseTextQuoteRequest(payload?.content ?? payload?.text, new Date(Number(envelope?.ts) || Date.now()));
    const semanticText = await extractSemanticTextFacts(envelope, payload, Boolean(sourceImageUrl));
    const parsedText = semanticText ?? fallbackText;
    const textFacts = textFactsWithQuoteDraft(parsedText, payload?.quote_draft, payload?.content ?? payload?.text);
    if (repeatedQuoteDraft(payload?.quote_draft, textFacts, sourceImageUrl)) {
      return Object.freeze({ status: 'quote_deduplicated', tenant_id: text(envelope?.tenantId, 128) });
    }
    const reusedRecognition = reusableSameImageRecognition(payload?.quote_draft, sourceImageUrl);
    if (reusedRecognition) {
      const recognition = mergeRecognitionWithTextFacts(reusedRecognition.recognition, textFacts, payload?.content ?? payload?.text);
      return Object.freeze({
        ...reusedRecognition, tenant_id: text(envelope?.tenantId, 128), recognition,
        ticket_count: textFacts?.ticket_count ?? reusedRecognition.ticket_count,
        field_sources: { ...(reusedRecognition.field_sources ?? {}), ...(textFacts?.field_sources ?? {}) },
      });
    }
    if (!sourceImageUrl) {
      return textFacts.status === 'recognized'
        ? Object.freeze({ ...textFacts, tenant_id: text(envelope?.tenantId, 128) })
        : textFacts;
    }
    try {
      const recognitionResults = [];
      const recognitionSources = sourceImageUrls.length ? sourceImageUrls : [sourceImageUrl];
      const outcomes = await Promise.allSettled(recognitionSources.map((recognitionSource, index) => (
        recognizeSource(recognitionSource, index, recognitionSources, envelope, payload)
      )));
      let contentHashReused = false;
      for (const [index, outcome] of outcomes.entries()) {
        if (outcome.status === 'fulfilled') {
          recognitionResults.push(outcome.value.recognition);
          contentHashReused ||= outcome.value.reused;
        }
        // The newest image is the authoritative quote image. An older venue
        // detail is only a bounded identity supplement and may fail safely.
        else if (index === outcomes.length - 1) throw outcome.reason;
      }
      const recognition = mergeImageRecognitions(recognitionResults);
      if (!recognition) throw new Error('quote preview recognition returned no usable image result');
      const hasSelectedConfirmationCard = recognition.image_type === 'ORDER_CONFIRM'
        && recognition?.official_selection?.is_selected === true
        && Array.isArray(recognition?.official_selection?.selected_seat_numbers)
        && recognition.official_selection.selected_seat_numbers.length > 0;
      if (recognition.image_type !== 'SEAT_MAP' && !hasSelectedConfirmationCard) {
        // A buyer may attach a showtime header, chat image, or a second image
        // after sending complete quote facts. Keep that independently
        // verifiable text route available, but never override a recognised
        // non-Wanda cinema or an order-confirmation image.
        if (textFacts.status === 'recognized' && canUseTextQuoteAfterIgnoredImage(recognition)) {
          return Object.freeze({ ...textFacts, tenant_id: text(envelope?.tenantId, 128), vision_fallback: true });
        }
        return Object.freeze({ status: 'ignored', recognition });
      }
      const fusedRecognition = mergeRecognitionWithTextFacts(recognition, textFacts, payload?.content ?? payload?.text);
      return Object.freeze({
        status: 'recognized',
        tenant_id: text(envelope?.tenantId, 128),
        ticket_count: textFacts.ticket_count ?? ticketCountFromText(payload?.content ?? payload?.text),
        recognition: fusedRecognition,
        field_sources: fieldSourcesForRecognition(fusedRecognition, textFacts, recognition, payload?.content ?? payload?.text),
        recognition_reply_text: recognitionReplyText(fusedRecognition),
        ...(textFacts.semantic_source ? { semantic_source: textFacts.semantic_source } : {}),
        ...(contentHashReused ? { recognition_reused: 'same_image_content_hash' } : {}),
      });
    } catch (error) {
      // A complete buyer-supplied text record is independently verifiable by
      // the Wanda matcher. Do not let a transient vision failure block it.
      if (textFacts.status === 'recognized') return Object.freeze({ ...textFacts, tenant_id: text(envelope?.tenantId, 128), vision_fallback: true });
      throw error;
    }
  }

  async function resolveShowtime(recognized) {
    if (recognized?.status !== 'recognized') return recognized;
    const url = preview.quoteUrl.replace(/\/preview-quote(?:\?.*)?$/u, '/preview-resolve-showtime');
    if (url === preview.quoteUrl) throw new Error('showtime resolution endpoint is unavailable');
    const result = await fetchJson(url, { recognition: recognized.recognition }, 'quote preview showtime resolution');
    if (!result?.recognition || typeof result.recognition !== 'object' || Array.isArray(result.recognition)) throw new Error('showtime resolution returned an invalid response');
    return Object.freeze({ ...recognized, status: 'resolved', recognition: result.recognition, matched_cinema_name: text(result.matched_cinema_name, 300) || null });
  }

  async function quote(recognized) {
    if (!['recognized', 'resolved'].includes(recognized?.status)) return recognized;
    let attempt = recognized;
    let outcome = await requestQuote(attempt);
    if (!outcome.response.ok && quoteFailure(outcome.result?.detail, outcome.response.status).code === 'showtime_not_unique') {
      for (const candidate of await resolveMatchCandidates(recognized)) {
        attempt = { ...recognized, recognition: { ...recognized.recognition, ...candidate } };
        outcome = await requestQuote(attempt);
        if (outcome.response.ok) break;
      }
    }
    if (!outcome.response.ok) {
      const failure = quoteFailure(outcome.result?.detail, outcome.response.status);
      const contextualReply = ['showtime_not_found', 'showtime_not_unique', 'cinema_catalog_not_unique', 'official_selection_unverifiable', 'temporary_lock_release_unverified'].includes(failure.code)
        ? quoteFailureReplyText(failure.code, attempt.recognition)
        : '';
      return Object.freeze({ status: 'quote_failed', recognition: attempt.recognition, recognition_reply_text: attempt.recognition_reply_text, failure_code: failure.code, ...(attempt.semantic_source ? { semantic_source: attempt.semantic_source } : {}), ...(failure.diagnostics ? { diagnostics: failure.diagnostics } : {}), reply_text: contextualReply || failure.replyText || quoteFailureReplyText(failure.code, attempt.recognition) });
    }
    const result = outcome.result;
    if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('quote preview quote returned an invalid response');
    const matchedCinema = text(result.matched_cinema_name, 160);
    const recognition = matchedCinema ? { ...attempt.recognition, cinema: matchedCinema } : attempt.recognition;
    if (result.buyer_app_purchase_recommended === true) {
      return Object.freeze({
        status: 'quote_not_competitive',
        recognition,
        recognition_reply_text: recognitionReplyText(recognition),
        reply_text: backendQuoteReplyText(result),
      });
    }
    return Object.freeze({ ...result, status: 'preview_ready', ...(recognized.text_quote ? { text_quote: true } : {}), ...(recognized.semantic_source ? { semantic_source: recognized.semantic_source } : {}), recognition, recognition_reply_text: recognitionReplyText(recognition), reply_text: recognized.text_quote ? textQuoteReply(result, { ...recognized, recognition }) : (backendQuoteReplyText(result) || quoteReplyText(result, recognition)) });
  }

  async function requestQuote(recognized) {
    const response = await fetchImpl(preview.quoteUrl, { method: 'POST', headers: previewHeaders(preview.ingestKey), body: JSON.stringify({ tenant_id: recognized.tenant_id, recognition: recognized.recognition, ...(recognized.ticket_count ? { ticket_count: recognized.ticket_count } : {}) }), signal: AbortSignal.timeout(90_000) });
    return { response, result: await response.json().catch(() => null) };
  }

  async function resolveMatchCandidates(recognized) {
    const url = preview.quoteUrl.replace(/\/preview-quote(?:\?.*)?$/u, '/preview-resolve-candidates');
    if (url === preview.quoteUrl) return [];
    try {
      const response = await fetchImpl(url, { method: 'POST', headers: previewHeaders(preview.ingestKey), body: JSON.stringify({ recognition: recognized.recognition }), signal: AbortSignal.timeout(20_000) });
      const result = await response.json();
      return response.ok && Array.isArray(result?.candidates) ? result.candidates.slice(0, 2).map((candidate) => safeMatchCandidate(candidate, recognized.recognition)).filter(Boolean) : [];
    } catch { return []; }
  }

  async function availableSeats({ recognition, row = null }) {
    const requestedRow = row == null ? null : Number(row);
    if (!recognition || typeof recognition !== 'object'
      || (requestedRow !== null && (!Number.isInteger(requestedRow) || requestedRow < 1 || requestedRow > 99))) {
      throw new TypeError('valid recognition and optional row are required');
    }
    const url = preview.quoteUrl.replace(/\/preview-quote(?:\?.*)?$/u, '/preview-available-seats');
    if (url === preview.quoteUrl) throw new Error('available seat endpoint is unavailable');
    const result = await fetchJson(url, { recognition, row: requestedRow }, 'W+ available seat lookup');
    const seatPattern = requestedRow === null ? /^\d{1,2}排\d{1,3}座$/u : new RegExp(`^${requestedRow}排\\d{1,3}座$`, 'u');
    const seats = Array.isArray(result.seats)
      ? result.seats.map((seat) => text(seat, 80)).filter((seat) => seatPattern.test(seat)).slice(0, 30)
      : [];
    return Object.freeze({
      row: requestedRow,
      seats,
      available_count: Number.isInteger(result.available_count) && result.available_count >= seats.length ? result.available_count : seats.length,
      wplus_offer_available: result.wplus_offer_available === true,
      matched_cinema_name: text(result.matched_cinema_name, 160) || null,
    });
  }

  async function capture(envelope) {
    if (usesTwoStageApi) return quote(await recognize(envelope));
    const payload = envelope?.payload ?? {};
    const sourceImageUrl = firstImageUrl(payload);
    if (!sourceImageUrl) return { status: 'ignored_no_image' };
    const imageUrl = await importImageForRecognition(sourceImageUrl, envelope, imageLoader, backendClient);
    const result = await fetchJson(preview.ingestUrl, {
      event_id: text(envelope?.id, 128),
      tenant_id: text(envelope?.tenantId, 128),
      buyer_label: buyerLabel(payload),
      message_text: text(payload?.content ?? payload?.text, 1_000),
      image_url: imageUrl,
      ...(ticketCountFromText(payload?.content ?? payload?.text) ? { ticket_count: ticketCountFromText(payload?.content ?? payload?.text) } : {}),
    }, 'quote preview ingestion');
    return Object.freeze({ ...result, reply_text: multilineText(result.reply_text, 500) });
  }

  async function fetchJson(url, body, label) {
    const response = await fetchImpl(url, {
      method: 'POST',
      headers: previewHeaders(preview.ingestKey),
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(90_000),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      const detail = body?.detail;
      const failureCode = typeof detail === 'string' ? detail : typeof detail?.code === 'string' ? detail.code : null;
      const error = new Error(`${label} failed with HTTP ${response.status}`);
      error.failure_code = failureCode;
      error.http_status = response.status;
      error.diagnostics = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail.diagnostics ?? null : null;
      throw error;
    }
    const result = await response.json();
    if (!result || typeof result !== 'object' || Array.isArray(result)) {
      throw new Error(`${label} returned an invalid response`);
    }
    return result;
  }

  return Object.freeze({ recognize, resolveShowtime, resolveCandidates: resolveMatchCandidates, quote, availableSeats, capture });
}

function previewHeaders(ingestKey) {
  return {
    accept: 'application/json',
    'content-type': 'application/json',
    'x-wanda-preview-key': ingestKey,
  };
}

