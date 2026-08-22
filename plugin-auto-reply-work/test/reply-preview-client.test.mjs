import assert from 'node:assert/strict';
import test from 'node:test';
import { createReplyPreviewClient } from '../src/reply-preview-client.mjs';

test('reply preview client sends the last twenty role-tagged messages without reply addressing data', async () => {
  let request;
  const client = createReplyPreviewClient({
    replyPreview: { ingestUrl: 'http://127.0.0.1:8010/api/replies/preview-ingest', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(_url, input) {
      request = JSON.parse(input.body);
      return new Response(JSON.stringify({ status: 'preview_ready' }), { status: 200 });
    },
  });

  await client.capture({
    id: 'event-1', tenantId: 'tenant-1',
    payload: { accountUnb: 'shop-private', chatId: 'chat-private', peerUnb: 'buyer-private', buyerNick: '买家小王', content: '两张还有吗' },
  }, [
    { role: 'seller', content: '您好，请问需要几张？', sent_at: '2026-08-16T01:00:00.000Z' },
    { role: 'buyer', content: '两张还有吗', sent_at: '2026-08-16T01:01:00.000Z' },
  ]);

  assert.deepEqual(request, {
    event_id: 'event-1', tenant_id: 'tenant-1', buyer_label: '买**王', latest_message: '两张还有吗',
    history: [
      { role: 'seller', content: '您好，请问需要几张？', sent_at: '2026-08-16T01:00:00.000Z' },
      { role: 'buyer', content: '两张还有吗', sent_at: '2026-08-16T01:01:00.000Z' },
    ],
  });
  assert.equal(JSON.stringify(request).includes('shop-private'), false);
  assert.equal(JSON.stringify(request).includes('chat-private'), false);
  assert.equal(JSON.stringify(request).includes('buyer-private'), false);
});
