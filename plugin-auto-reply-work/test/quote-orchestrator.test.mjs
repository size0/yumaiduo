import assert from 'node:assert/strict';
import test from 'node:test';

import { createQuoteOrchestrator } from '../src/quote/quote-orchestrator.mjs';

const envelope = {
  id: 'event-1', tenantId: '107',
  payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', imageUrls: ['https://img.example/seat.jpg'] },
};
const recognition = {
  status: 'recognized', ticket_count: 2,
  recognition: { cinema: '测试万达', movie: '测试电影', hand_drawn_circle: { exists: true } },
};

test('prepare starts readonly recognition immediately and complete advances to quote', async () => {
  const calls = [];
  const drafts = [];
  const orchestrator = createQuoteOrchestrator({
    quotePreviewClient: {
      async recognize(input) { calls.push(['recognize', input]); return recognition; },
      async quote(input) { calls.push(['quote', input]); return { status: 'preview_ready', total_quote_cents: 10_600 }; },
    },
    conversationContextStore: {
      async recordQuoteDraft(_tenant, _payload, draft) { drafts.push(draft); },
      async claimQuoteDraftAttempt() { return true; },
    },
  });

  const prepared = orchestrator.prepare({
    envelope, quoteEnvelope: envelope, quoteContext: null,
    runtimeSettings: { recognition_enabled: true, quote_enabled: true }, orderLinked: false,
  });
  await Promise.resolve();
  assert.equal(calls[0][0], 'recognize');

  const result = await prepared.complete({ awaitStage: (promise) => promise });

  assert.deepEqual(calls.map(([name]) => name), ['recognize', 'quote']);
  assert.equal(drafts.length, 1);
  assert.equal(drafts[0].imageUrl, 'https://img.example/seat.jpg');
  assert.equal(result.quoteResult.status, 'fulfilled');
  assert.equal(result.quoteResult.value.total_quote_cents, 10_600);
  assert.equal(result.circledDeliveryInstructionImage, 'https://img.example/seat.jpg');
});

test('count-only supplement reuses a bounded recognition artifact without vision', async () => {
  let recognitionCalls = 0;
  let quotedCount = null;
  const orchestrator = createQuoteOrchestrator({
    quotePreviewClient: {
      async recognize() { recognitionCalls += 1; return recognition; },
      async quote(input) { quotedCount = input.ticket_count; return { status: 'preview_ready' }; },
    },
    conversationContextStore: { async recordQuoteDraft() {}, async claimQuoteDraftAttempt() { return true; } },
  });
  const quoteContext = { facts: { quote_draft: {
    expires_at: Date.now() + 60_000,
    recognition_artifact: { ...recognition, ticket_count: null },
  } } };
  const countEnvelope = { ...envelope, payload: { ...envelope.payload, imageUrls: [], content: '3张' } };

  const result = await orchestrator.prepare({
    envelope: countEnvelope, quoteEnvelope: countEnvelope, quoteContext,
    runtimeSettings: { recognition_enabled: true, quote_enabled: true }, orderLinked: false,
  }).complete({ awaitStage: (promise) => promise });

  assert.equal(recognitionCalls, 0);
  assert.equal(quotedCount, 3);
  assert.equal(result.recognitionReuseReason, 'count_only_quote_draft');
  assert.equal(result.recognitionDurationMs, 0);
});

test('a claimed duplicate draft never calls realtime quote', async () => {
  let quoteCalls = 0;
  const orchestrator = createQuoteOrchestrator({
    quotePreviewClient: {
      async recognize() { return recognition; },
      async quote() { quoteCalls += 1; return { status: 'preview_ready' }; },
    },
    conversationContextStore: { async recordQuoteDraft() {}, async claimQuoteDraftAttempt() { return false; } },
  });

  const result = await orchestrator.prepare({
    envelope, quoteEnvelope: envelope, quoteContext: null,
    runtimeSettings: { recognition_enabled: true, quote_enabled: true }, orderLinked: false,
  }).complete({ awaitStage: (promise) => promise });

  assert.equal(quoteCalls, 0);
  assert.equal(result.quoteAttemptDeduplicated, true);
  assert.equal(result.quoteResult.value.status, 'quote_deduplicated');
});

test('legacy capture remains available when the client has no two-stage interface', async () => {
  let captures = 0;
  const orchestrator = createQuoteOrchestrator({
    quotePreviewClient: { async capture(input) { captures += 1; assert.strictEqual(input, envelope); return { status: 'ignored' }; } },
    conversationContextStore: null,
  });
  const result = await orchestrator.prepare({
    envelope, quoteEnvelope: envelope, quoteContext: null,
    runtimeSettings: { recognition_enabled: true, quote_enabled: true }, orderLinked: false,
  }).complete({ awaitStage: (promise) => promise });
  assert.equal(captures, 1);
  assert.equal(result.quoteResult.value.status, 'ignored');
});
