import assert from 'node:assert/strict';
import test from 'node:test';
import { createV2BackendClient } from '../src/backend/client.mjs';

test('fulfillment image recognition is tenant-bound and returns structured facts', async () => {
  const calls = [];
  const client = createV2BackendClient({
    v4BackendUrl: 'http://127.0.0.1:8012',
    requestTimeoutMs: 5_000,
    backend: { baseUrl: 'http://backend.test', sharedSecret: 'secret' },
  }, {
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({ data: {
        city: '运城', movie_name: '八仙！', ticket_codes: ['20711100016790'],
      } }), { status: 200, headers: { 'content-type': 'application/json' } });
    },
  });

  const result = await client.recognizeFulfillmentImage({
    tenantId: '107',
    imageUrl: 'https://img.alicdn.com/ticket.png',
  });

  assert.equal(result.movie_name, '八仙！');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'http://127.0.0.1:8012/api/ticket-images/recognize');
  assert.equal(calls[0].options.headers['x-wanda-tenant-id'], '107');
  assert.deepEqual(JSON.parse(calls[0].options.body), { image_url: 'https://img.alicdn.com/ticket.png' });
});
