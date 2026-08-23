import { requestedTicketCount } from '../agent/ticket-request-inspector.mjs';

/**
 * Buyer-message quote pipeline boundary.
 *
 * It owns read-only recognition prefetch, bounded draft persistence,
 * duplicate-attempt claiming and the single realtime quote invocation. It does
 * not compose or deliver replies, and pricing/lock authority remains in V3.
 */
export function createQuoteOrchestrator({ quotePreviewClient = null, conversationContextStore = null, now = Date.now }) {
  if (typeof now !== 'function') throw new TypeError('now must be a function');

  function prepare({ envelope, quoteEnvelope, quoteContext, runtimeSettings = {}, orderLinked = false }) {
    const canUseTwoStageQuote = Boolean(
      quotePreviewClient
      && !orderLinked
      && runtimeSettings.recognition_enabled
      && typeof quotePreviewClient.recognize === 'function'
      && typeof quotePreviewClient.quote === 'function'
    );
    const reusedRecognition = canUseTwoStageQuote ? countOnlyRecognitionArtifact(quoteContext, envelope, now()) : null;
    const recognitionStartedAt = canUseTwoStageQuote ? now() : 0;
    let recognitionFinishedAt = 0;
    const prefetchedRecognition = canUseTwoStageQuote
      ? reusedRecognition
        ? Promise.resolve({ status: 'fulfilled', value: reusedRecognition })
        : settle(quotePreviewClient.recognize(quoteEnvelope)).then((result) => {
          recognitionFinishedAt = now();
          return result;
        })
      : null;
    let completionPromise = null;

    async function finish(awaitStage) {
      let recognitionDurationMs = 0;
      let quoteDurationMs = 0;
      let quoteResult;
      let quoteAttemptDeduplicated = false;
      let recognitionReuseReason = '';
      let circledDeliveryInstructionImage = '';

      if (canUseTwoStageQuote) {
        const recognitionResult = await awaitStage(prefetchedRecognition);
        const recognized = recognitionResult.status === 'fulfilled' ? recognitionResult.value : null;
        recognitionReuseReason = reusedRecognition
          ? 'count_only_quote_draft'
          : String(recognized?.recognition_reused ?? '').slice(0, 80);
        recognitionDurationMs = recognitionReuseReason ? 0 : (recognitionFinishedAt || now()) - recognitionStartedAt;
        const contextTicketCount = recentExplicitTicketCount(quoteContext?.messages);
        const recognizedForQuote = recognized?.status === 'recognized'
          && !positiveInteger(recognized.ticket_count)
          && contextTicketCount
          ? { ...recognized, ticket_count: contextTicketCount }
          : recognized;
        if (recognizedForQuote?.recognition?.hand_drawn_circle?.exists === true) {
          circledDeliveryInstructionImage = firstImageUrl(quoteEnvelope?.payload) ?? '';
        }
        quoteAttemptDeduplicated = recognizedForQuote?.status === 'quote_deduplicated';
        const persistableDraft = ['recognized', 'needs_confirmation'].includes(recognizedForQuote?.status)
          && recognizedForQuote?.recognition && typeof recognizedForQuote.recognition === 'object';
        if (persistableDraft && typeof conversationContextStore?.recordQuoteDraft === 'function') {
          await conversationContextStore.recordQuoteDraft(envelope.tenantId, quoteEnvelope?.payload ?? {}, {
            recognition: recognizedForQuote.recognition,
            ticketCount: recognizedForQuote.ticket_count,
            imageUrl: firstImageUrl(quoteEnvelope?.payload),
            fieldSources: recognizedForQuote.field_sources,
            recognitionArtifact: recognizedForQuote,
          });
          if (recognizedForQuote.status === 'recognized') {
            quoteAttemptDeduplicated = typeof conversationContextStore?.claimQuoteDraftAttempt === 'function'
              ? !(await conversationContextStore.claimQuoteDraftAttempt(envelope.tenantId, quoteEnvelope?.payload ?? {}))
              : false;
          }
        }
        const quoteStartedAt = now();
        const quoteOperation = quoteAttemptDeduplicated
          ? Promise.resolve({ status: 'fulfilled', value: { status: 'quote_deduplicated' } })
          : recognizedForQuote?.status === 'recognized' && runtimeSettings.quote_enabled
            ? settle(quotePreviewClient.quote(recognizedForQuote))
            : recognitionResult.status === 'rejected'
              ? Promise.resolve(recognitionResult)
              : Promise.resolve({
                status: 'fulfilled',
                value: runtimeSettings.quote_enabled ? recognizedForQuote : { status: 'quote_disabled' },
              });
        quoteResult = await awaitStage(quoteOperation);
        quoteDurationMs = now() - quoteStartedAt;
      } else {
        const quoteStartedAt = now();
        const operation = quotePreviewClient
          && !orderLinked
          && runtimeSettings.recognition_enabled
          && runtimeSettings.quote_enabled
          && typeof quotePreviewClient.capture === 'function'
          ? quotePreviewClient.capture(quoteEnvelope)
          : null;
        quoteResult = await awaitStage(settle(operation));
        quoteDurationMs = now() - quoteStartedAt;
      }

      return Object.freeze({
        quoteResult,
        quoteAttemptDeduplicated,
        recognitionReuseReason,
        circledDeliveryInstructionImage,
        recognitionDurationMs,
        quoteDurationMs,
      });
    }

    return Object.freeze({
      canUseTwoStageQuote,
      complete({ awaitStage = (promise) => promise } = {}) {
        if (typeof awaitStage !== 'function') throw new TypeError('awaitStage must be a function');
        completionPromise ??= finish(awaitStage);
        return completionPromise;
      },
    });
  }

  return Object.freeze({ prepare });
}

function settle(promise) {
  return Promise.resolve(promise)
    .then((value) => ({ status: 'fulfilled', value }))
    .catch((reason) => ({ status: 'rejected', reason }));
}

function firstImageUrl(payload = {}) {
  const urls = Array.isArray(payload?.imageUrls) ? payload.imageUrls : [];
  return urls.find((value) => typeof value === 'string' && value.trim()) ?? null;
}

function countOnlyRecognitionArtifact(quoteContext, envelope, now) {
  if (firstImageUrl(envelope?.payload)) return null;
  const content = String(envelope?.payload?.content ?? envelope?.payload?.text ?? '').replace(/\s+/gu, '').trim();
  if (!/^(?:要|需要|一共|共|买)?(?:[1-9]|1\d|20|[一二两三四五六七八九十]{1,3})(?:张|个(?:座位|位置)?)[。！!？?]*$/u.test(content)) return null;
  const count = requestedTicketCount(content);
  const draft = quoteContext?.facts?.quote_draft;
  const artifact = draft?.recognition_artifact;
  if (!Number.isInteger(count) || count < 1 || count > 20 || Number(draft?.expires_at ?? 0) <= now
    || artifact?.status !== 'recognized' || !artifact.recognition || typeof artifact.recognition !== 'object') return null;
  return { ...structuredClone(artifact), ticket_count: count };
}

function recentExplicitTicketCount(messages) {
  if (!Array.isArray(messages)) return null;
  for (const message of messages.filter((item) => item?.role === 'buyer').slice(-8).reverse()) {
    const count = requestedTicketCount(message?.text);
    if (count) return count;
  }
  return null;
}

function positiveInteger(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}
