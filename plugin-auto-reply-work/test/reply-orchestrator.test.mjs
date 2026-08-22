import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { createReplyOrchestrator, isBuyerReplyAction } from '../src/reply/reply-orchestrator.mjs';

function replyAction(overrides = {}) {
  return {
    action_id: 'event-1:reply',
    kind: 'reply',
    tenant_id: '107',
    account_unb: 'shop-1',
    chat_id: 'chat-1',
    peer_unb: 'buyer-1',
    text: '请问需要几张？',
    ...overrides,
  };
}

test('identifies every buyer-facing delivery kind and excludes transaction actions', () => {
  for (const kind of ['reply', 'send_message', 'reply_with_image', 'send_image']) {
    assert.equal(isBuyerReplyAction({ kind }), true, kind);
  }
  for (const kind of ['change_price', 'noop', 'create_order', '']) {
    assert.equal(isBuyerReplyAction({ kind }), false, kind);
  }
});

test('delegates a buyer reply unchanged to the existing action executor', async () => {
  const calls = [];
  const executor = { async execute(action) { calls.push(action); return { status: 'succeeded', message_id: 'message-1' }; } };
  const orchestrator = createReplyOrchestrator({ actionExecutor: executor });
  const action = replyAction({ allow_plugin_followup: true, human_takeover_window_ms: 20_000 });

  const result = await orchestrator.deliver(action);

  assert.deepEqual(result, { status: 'succeeded', message_id: 'message-1' });
  assert.equal(calls.length, 1);
  assert.strictEqual(calls[0], action);
});

test('routes text-with-image and image-only deliveries through the same boundary', async () => {
  const kinds = [];
  const orchestrator = createReplyOrchestrator({
    actionExecutor: { async execute(action) { kinds.push(action.kind); return { status: 'succeeded' }; } },
  });

  await orchestrator.deliver(replyAction({ kind: 'reply_with_image', image_url: 'https://example.invalid/reply.png' }));
  await orchestrator.deliver(replyAction({ kind: 'send_image', text: undefined, image_base64: 'AA==' }));

  assert.deepEqual(kinds, ['reply_with_image', 'send_image']);
});

test('fails closed before execution when reply text contains an unresolved template placeholder', async () => {
  let calls = 0;
  const orchestrator = createReplyOrchestrator({
    actionExecutor: { async execute() { calls += 1; return { status: 'succeeded' }; } },
  });

  const result = await orchestrator.deliver(replyAction({ text: '当前{价格}元，请付款。' }));

  assert.deepEqual(result, { status: 'skipped', reason: 'unresolved_reply_placeholder' });
  assert.equal(calls, 0);
});

test('refuses transaction actions so Dify or another caller cannot use the reply boundary to execute them', async () => {
  const orchestrator = createReplyOrchestrator({ actionExecutor: { async execute() { throw new Error('must not run'); } } });

  await assert.rejects(
    orchestrator.deliver({ action_id: 'price-1', kind: 'change_price' }),
    /buyer-facing reply action/u,
  );
});

test('requires an existing action executor', () => {
  assert.throws(() => createReplyOrchestrator({ actionExecutor: null }), /actionExecutor\.execute/u);
});

test('workflow and durable Agent outbox both delegate buyer delivery to reply orchestrator', async () => {
  const [workflow, application] = await Promise.all([
    readFile(new URL('../src/workflow.mjs', import.meta.url), 'utf8'),
    readFile(new URL('../src/application.mjs', import.meta.url), 'utf8'),
  ]);

  assert.match(workflow, /replyOrchestrator\.deliver\(action\)/u);
  assert.match(application, /agentOutboxReplyOrchestrator\.deliver\(action\)/u);
  assert.doesNotMatch(application, /executeReply:\s*\(action\)\s*=>\s*agentOutboxExecutor\.execute/u);
});
