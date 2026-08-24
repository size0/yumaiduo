import test from 'node:test';
import assert from 'node:assert/strict';
import { createConversationAgentClient } from '../src/agent/conversation-agent-client.mjs';

test('conversation agent client sends only bounded state and returns a typed plan payload', async () => {
  let sent;
  const client = createConversationAgentClient({
    conversationAgent: { url: 'http://127.0.0.1:8010/api/agents/turn', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(url, options) {
      assert.equal(String(url), 'http://127.0.0.1:8010/api/agents/turn');
      assert.equal(options.headers['x-wanda-preview-key'], 'a'.repeat(32));
      sent = JSON.parse(options.body);
      return new Response(JSON.stringify({ status: 'planned', plan: {
        intent: '补充信息', confidence: 0.9, goal: '补充城市', action: 'ask_for_city', arguments: {},
        missing_fields: ['城市'], reply: '请问是哪个城市？', needs_human: false, reason: '影院不唯一',
      } }), { status: 200 });
    },
  });
  const plan = await client.plan({
    event_id: 'event-1', tenant_id: 'tenant-1', latest_message: '哪个店', has_image: false,
    state: { facts: { stage: 'collecting_information', quote_total_cents: 10_000, token: 'must-not-leave', quote_draft: { fields: { cinema: { value: '测试万达' } }, last_image: 'must-not-leave' } }, messages: [{ role: 'seller', content: '请发送完整选座页截图。', source: 'external_seller' }, { role: 'buyer', content: '哪个店' }] },
    observations: [{ status: 'success', tool: 'recognize_and_quote', summary: 'ok', facts: { total_quote_cents: 10_000, token: 'must-not-leave', _quote_input: { recognition: { cinema: 'hidden-artifact' } } }, next_actions: ['respond'] }],
  });
  assert.equal(plan.action, 'ask_for_city');
  assert.equal(sent.state.facts.token, undefined);
  assert.equal(sent.state.facts.quote_total_cents, 10_000);
  assert.deepEqual(sent.state.facts.quote_draft, { fields: { cinema: '测试万达' } });
  assert.equal(sent.observations[0].facts.token, undefined);
  assert.equal(sent.observations[0].facts._quote_input, undefined);
  assert.equal(sent.observations[0].facts.total_quote_cents, 10_000);
  assert.deepEqual(sent.history, [
    { role: 'seller', content: '请发送完整选座页截图。', source: 'external_seller' },
    { role: 'buyer', content: '哪个店', source: 'buyer' },
  ]);
});

test('conversation agent client forwards native standard messages and tool calls without planner JSON', async () => {
  let sent;
  const client = createConversationAgentClient({ conversationAgent: {
    url: 'http://127.0.0.1:8010/api/agents/turn',
    nativeUrl: 'http://127.0.0.1:8010/api/agents/v2/completions',
    ingestKey: 'a'.repeat(32),
  } }, {
    async fetchImpl(url, options) {
      assert.equal(String(url), 'http://127.0.0.1:8010/api/agents/v2/completions');
      sent = JSON.parse(options.body);
      return new Response(JSON.stringify({
        assistant: { role: 'assistant', content: '', tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'read_active_quote', arguments: '{}' } }] },
        finish_reason: 'tool_calls', model: 'reasoning-model',
        versions: { prompt: 'p1', knowledge: 'k1', tools: 't1' },
        usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 }, latency_ms: 8, request_id: 'request-1',
      }), { status: 200 });
    },
  });
  const result = await client.complete({
    tenant_id: 'tenant-1', conversation_id: 'conversation-1', run_id: 'run-1',
    messages: [{ role: 'user', content: '这个多少钱' }],
    available_tools: ['read_active_quote'], reasoning: { enabled: true, effort: 'high', max_output_tokens: 1600 },
  });
  assert.deepEqual(sent.messages, [{ role: 'user', content: '这个多少钱' }]);
  assert.deepEqual(sent.available_tools, ['read_active_quote']);
  assert.equal(sent.reasoning.max_output_tokens, 1600);
  assert.equal(result.assistant.tool_calls[0].function.name, 'read_active_quote');
  assert.equal(result.request_id, 'request-1');
});

test('conversation agent client fails closed on invalid or failed backend responses', async () => {
  const failed = createConversationAgentClient({ conversationAgent: { url: 'http://127.0.0.1/api/agents/turn', ingestKey: 'a'.repeat(32) } }, {
    async fetchImpl() { return new Response(JSON.stringify({ status: 'failed', failure_code: 'model_unavailable' }), { status: 200 }); },
  });
  await assert.rejects(() => failed.plan({ event_id: 'e', tenant_id: 't', latest_message: 'x', state: {}, observations: [] }), /model_unavailable/u);

  const malformed = createConversationAgentClient({ conversationAgent: { url: 'http://127.0.0.1/api/agents/turn', ingestKey: 'a'.repeat(32) } }, {
    async fetchImpl() { return new Response('{}', { status: 200 }); },
  });
  await assert.rejects(() => malformed.plan({ event_id: 'e', tenant_id: 't', latest_message: 'x', state: {}, observations: [] }), /invalid response/u);
});
