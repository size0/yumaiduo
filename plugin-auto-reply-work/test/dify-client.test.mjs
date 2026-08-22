import assert from 'node:assert/strict';
import test from 'node:test';
import { createDifyClient } from '../src/ai/dify-client.mjs';

const validResponse = {
  data: {
    status: 'succeeded',
    outputs: {
      intent: '其他', confidence: 0.91, reply_draft: '请发送完整选座页截图。',
      handoff_recommended: false, reason_code: 'needs_image', missing_fields: ['image'],
    },
  },
};

function config() {
  return {
    difyShadow: {
      url: 'https://dify.internal.example/v1/workflows/run',
      apiKey: 'app-test-secret-key',
      timeoutMs: 8_000,
    },
  };
}

test('returns null when Dify Shadow is not configured', () => {
  assert.equal(createDifyClient({}), null);
});

test('calls the blocking Workflow API with minimal redacted inputs', async () => {
  let request;
  const client = createDifyClient(config(), {
    fetchImpl: async (url, options) => {
      request = { url, options, body: JSON.parse(options.body) };
      return new Response(JSON.stringify(validResponse), { status: 200, headers: { 'content-type': 'application/json' } });
    },
  });
  const result = await client.evaluate({
    event_id: 'event-secret-id',
    tenant_id: 'tenant-secret-id',
    latest_message: '电话13800138000，链接 https://example.com，不是53元吗',
    has_image: true,
    state: {
      facts: {
        stage: 'quoted', city: '北京', cinema: '万达影城', movie: '测试电影', date: '2026-08-23', showtime: '20:00',
        quote_total_cents: 8_800, quote_unit_cents: 4_400, has_active_quote: true, has_linked_order: true,
      },
      messages: Array.from({ length: 10 }, (_, index) => ({
        role: index % 2 ? 'seller' : 'buyer', source: index % 2 ? 'external_seller' : 'buyer',
        content: `历史${index} 订单号1234567890123456 电话13800138000`,
      })),
    },
    observations: [{
      status: 'success', tool: 'read_active_quote', summary: '报价88元',
      facts: { total_quote_cents: 8_800, order_id: 'hidden' }, next_actions: ['respond'],
    }],
  });

  assert.equal(request.url, config().difyShadow.url);
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.redirect, 'error');
  assert.equal(request.options.headers.authorization, 'Bearer app-test-secret-key');
  assert.equal(request.body.response_mode, 'blocking');
  assert.match(request.body.user, /^wanda-shadow-[a-f0-9]{24}$/u);
  assert.doesNotMatch(request.body.user, /tenant-secret-id/u);
  assert.deepEqual(Object.keys(request.body.inputs).sort(), ['context_json', 'latest_message', 'observations_json', 'recent_history_json']);
  assert.match(request.body.inputs.latest_message, /\[联系方式\]/u);
  assert.match(request.body.inputs.latest_message, /\[链接\]/u);
  assert.match(request.body.inputs.latest_message, /\[金额\]/u);
  assert.doesNotMatch(JSON.stringify(request.body), /event-secret-id|tenant-secret-id|13800138000|1234567890123456|53元/u);

  const history = JSON.parse(request.body.inputs.recent_history_json);
  assert.equal(history.length, 6);
  assert.equal(history[0].content.startsWith('历史4'), true);
  const context = JSON.parse(request.body.inputs.context_json);
  assert.deepEqual(context, {
    stage: 'quoted', city: '北京', cinema: '万达影城', movie: '测试电影', date: '2026-08-23', showtime: '20:00',
    has_image: true, has_active_quote: true, has_linked_order: true,
  });
  assert.equal(JSON.stringify(request.body).includes('8800'), false);
  assert.deepEqual(JSON.parse(request.body.inputs.observations_json), [{ status: 'success', tool: 'read_active_quote', next_actions: ['respond'] }]);
  assert.deepEqual(result, validResponse.data.outputs);
});

test('fails closed with generic errors for network, HTTP, size, JSON, and schema failures', async () => {
  const networkFailure = createDifyClient(config(), {
    fetchImpl: async () => { throw new Error('socket secret'); },
  });
  await assert.rejects(() => networkFailure.evaluate({}), /^Error: Dify Shadow request failed$/u);

  const httpFailure = createDifyClient(config(), {
    fetchImpl: async () => new Response('upstream secret body', { status: 503 }),
  });
  await assert.rejects(() => httpFailure.evaluate({}), /Dify Shadow request failed with HTTP 503/u);

  const oversized = createDifyClient(config(), {
    fetchImpl: async () => new Response('x'.repeat(20_001), { status: 200 }),
  });
  await assert.rejects(() => oversized.evaluate({}), /Dify Shadow returned invalid JSON/u);

  const invalidJson = createDifyClient(config(), {
    fetchImpl: async () => new Response('{invalid', { status: 200 }),
  });
  await assert.rejects(() => invalidJson.evaluate({}), /Dify Shadow returned invalid JSON/u);

  const invalid = createDifyClient(config(), {
    fetchImpl: async () => new Response(JSON.stringify({ data: { status: 'succeeded', outputs: { reply_draft: 'x' } } }), { status: 200 }),
  });
  await assert.rejects(() => invalid.evaluate({}), /invalid AI Shadow advisory/u);
});

test('requires fetch and never exposes the configured API key', () => {
  assert.throws(() => createDifyClient(config(), { fetchImpl: null }), /fetch implementation is required/u);
  const client = createDifyClient(config(), { fetchImpl: async () => new Response('{}') });
  assert.deepEqual(Object.keys(client), ['evaluate']);
  assert.equal(JSON.stringify(client).includes('app-test-secret-key'), false);
});
