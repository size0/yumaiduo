import test from 'node:test';
import assert from 'node:assert/strict';
import { createModelDrivenAgentLoop, transactionFactConflicts } from '../src/agent/model-driven-agent-loop.mjs';

function context() {
  return {
    tenant_id: 'tenant-1', conversation_id: 'conversation-1', run_id: 'run-1', latest_message: '两张多少钱',
    state: { facts: { cinema: '测试万达', movie: '测试电影' }, messages: [{ role: 'buyer', content: '两张多少钱', at: 1 }] },
    observations: [], trace: [], reasoning: { enabled: true, effort: 'medium', max_output_tokens: 1600 },
  };
}

function toolCall(id, name, args = {}) {
  return { id, type: 'function', function: { name, arguments: JSON.stringify(args) } };
}

test('model-led loop learns from tool errors and chooses its own recovery path', async () => {
  const requests = [];
  const responses = [
    { assistant: { role: 'assistant', content: '', tool_calls: [toolCall('c1', 'quote_realtime')] } },
    { assistant: { role: 'assistant', content: '', tool_calls: [toolCall('c2', 'resolve_showtime')] } },
    { assistant: { role: 'assistant', content: '', tool_calls: [toolCall('c3', 'quote_realtime')] } },
    { assistant: { role: 'assistant', content: '已实时核验，两张合计100元。' } },
  ];
  const model = {
    async complete(input) {
      requests.push(structuredClone(input.messages));
      return { ...responses.shift(), finish_reason: responses.length ? 'tool_calls' : 'stop', model: 'test', versions: {}, usage: {}, latency_ms: 1, request_id: 'r' };
    },
  };
  let resolved = false;
  const tools = {
    async quote_realtime() {
      return resolved
        ? { status: 'success', code: 'quote_ready', summary: '报价完成', facts: { ticket_count: 2, total_quote_cents: 10_000 }, retryable: false }
        : { status: 'error', code: 'showtime_missing', summary: '尚未获得唯一场次', missing: ['showtime_identity'], retryable: true };
    },
    async resolve_showtime() {
      resolved = true;
      return { status: 'success', code: 'showtime_resolved', summary: '已获得唯一场次', facts: { showtime: '19:10' }, retryable: false };
    },
  };
  const outcome = await createModelDrivenAgentLoop({ model, tools }).runTurn(context());
  assert.equal(outcome.status, 'reply');
  assert.equal(outcome.reply, '已实时核验，两张合计100元。');
  assert.deepEqual(outcome.trace.map((item) => item.action), ['quote_realtime', 'resolve_showtime', 'quote_realtime']);
  assert.match(requests[1].at(-1).content, /showtime_missing/u);
  assert.match(requests[3].at(-1).content, /10000/u);
});

test('explicit transaction conflicts are returned to the model once for self-correction', async () => {
  const seen = [];
  const model = {
    calls: 0,
    async complete(input) {
      this.calls += 1;
      seen.push(structuredClone(input.messages));
      return this.calls === 1
        ? { assistant: { role: 'assistant', content: '两张合计88元。' }, model: 'test' }
        : { assistant: { role: 'assistant', content: '两张合计100元。' }, model: 'test' };
    },
  };
  const input = context();
  input.now = Date.now();
  input.state.facts = { stage: 'quoted', quote_ticket_count: 2, quote_total_cents: 10_000, quote_expires_at: input.now + 60_000 };
  const outcome = await createModelDrivenAgentLoop({ model, tools: {} }).runTurn(input);
  assert.equal(outcome.status, 'reply');
  assert.equal(outcome.reason, 'model_response_fact_repaired');
  assert.equal(outcome.reply, '两张合计100元。');
  assert.match(seen[1].at(-1).content, /fact_check_failed/u);
});

test('transaction guard catches Chinese amounts and unsupported state claims', () => {
  const input = context();
  input.now = Date.now();
  input.state.facts = {
    stage: 'quoted', quote_ticket_count: 2, quote_total_cents: 10_000,
    quote_expires_at: input.now + 60_000, quote_confirmed: false,
  };
  const conflicts = transactionFactConflicts('已经锁座，可以付款了，两张一百零八元。', input);
  assert.deepEqual(new Set(conflicts.map((item) => item.field)), new Set(['seat_lock', 'payment_authorization', 'amount_cents']));
});

test('an unknown write result is non-retryable and blocks a repeated model call', async () => {
  let executions = 0;
  const observed = [];
  const model = {
    calls: 0,
    async complete(input) {
      this.calls += 1;
      if (this.calls <= 2) return { assistant: { role: 'assistant', content: '', tool_calls: [toolCall(`w${this.calls}`, 'change_order_price')] }, model: 'test' };
      observed.push(input.messages.filter((item) => item.role === 'tool').map((item) => JSON.parse(item.content)));
      return { assistant: { role: 'assistant', content: '改价结果暂时无法确认，已停止重复操作。' }, model: 'test' };
    },
  };
  const outcome = await createModelDrivenAgentLoop({
    model,
    tools: { async change_order_price() { executions += 1; throw new Error('network result unknown'); } },
  }).runTurn(context());
  assert.equal(executions, 1);
  assert.equal(outcome.status, 'reply');
  assert.equal(observed[0].at(-2).code, 'tool_write_result_unknown');
  assert.equal(observed[0].at(-2).retryable, false);
  assert.equal(observed[0].at(-1).code, 'write_retry_blocked');
});

test('deadline is checked again after a model call returns', async () => {
  let checks = 0;
  let toolCalls = 0;
  const model = { async complete() { return { assistant: { role: 'assistant', content: '', tool_calls: [toolCall('late', 'read_active_quote')] }, model: 'test' }; } };
  const outcome = await createModelDrivenAgentLoop({ model, tools: { async read_active_quote() { toolCalls += 1; } } }).runTurn(context(), {
    shouldContinue: () => { checks += 1; return checks === 1; },
  });
  assert.equal(outcome.reason, 'agent_deadline_exceeded');
  assert.equal(toolCalls, 0);
});

test('native loop runs an all-read batch in parallel but serializes any write batch', async () => {
  let active = 0;
  let maxActiveReads = 0;
  const readModel = {
    calls: 0,
    async complete() {
      this.calls += 1;
      return this.calls === 1
        ? { assistant: { role: 'assistant', content: '', tool_calls: [toolCall('r1', 'read_active_quote'), toolCall('r2', 'read_linked_order')] }, model: 'test' }
        : { assistant: { role: 'assistant', content: '读取完成' }, model: 'test' };
    },
  };
  const read = async () => {
    active += 1; maxActiveReads = Math.max(maxActiveReads, active);
    await new Promise((resolve) => setTimeout(resolve, 10));
    active -= 1;
    return { status: 'success', code: 'ok', summary: 'ok', facts: {} };
  };
  await createModelDrivenAgentLoop({ model: readModel, tools: { read_active_quote: read, read_linked_order: read } }).runTurn(context());
  assert.equal(maxActiveReads, 2);

  active = 0;
  let maxActiveWrites = 0;
  const writeModel = {
    calls: 0,
    async complete() {
      this.calls += 1;
      return this.calls === 1
        ? { assistant: { role: 'assistant', content: '', tool_calls: [toolCall('w1', 'confirm_active_quote'), toolCall('w2', 'record_seat_preference')] }, model: 'test' }
        : { assistant: { role: 'assistant', content: '写入完成' }, model: 'test' };
    },
  };
  const write = async () => {
    active += 1; maxActiveWrites = Math.max(maxActiveWrites, active);
    await new Promise((resolve) => setTimeout(resolve, 5));
    active -= 1;
    return { status: 'success', code: 'ok', summary: 'ok', facts: {} };
  };
  await createModelDrivenAgentLoop({ model: writeModel, tools: { confirm_active_quote: write, record_seat_preference: write } }).runTurn(context());
  assert.equal(maxActiveWrites, 1);
});
