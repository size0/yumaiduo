import assert from 'node:assert/strict';
import test from 'node:test';
import { validateDifyShadowResponse } from '../src/ai/dify-response-validator.mjs';

function response(outputs = {}) {
  return {
    data: {
      status: 'succeeded',
      outputs: {
        intent: '其他',
        confidence: 0.92,
        reply_draft: '请发送当前完整选座页截图，并说明需要的张数。',
        handoff_recommended: false,
        reason_code: 'needs_image',
        missing_fields: ['image', 'ticket_count'],
        ...outputs,
      },
    },
  };
}

test('accepts only the bounded advisory output contract', () => {
  const result = validateDifyShadowResponse(response());
  assert.deepEqual(result, {
    intent: '其他',
    confidence: 0.92,
    reply_draft: '请发送当前完整选座页截图，并说明需要的张数。',
    handoff_recommended: false,
    reason_code: 'needs_image',
    missing_fields: ['image', 'ticket_count'],
  });
  assert.equal(Object.isFrozen(result), true);
  assert.equal(Object.isFrozen(result.missing_fields), true);
});

test('also validates an already-unwrapped provider advisory', () => {
  const result = validateDifyShadowResponse(response().data.outputs);
  assert.equal(result.reason_code, 'needs_image');
});

test('rejects failed workflows, missing fields, and undeclared outputs', () => {
  assert.throws(
    () => validateDifyShadowResponse({ data: { status: 'failed', outputs: response().data.outputs } }),
    /workflow did not succeed/u,
  );
  const missing = response();
  delete missing.data.outputs.confidence;
  assert.throws(() => validateDifyShadowResponse(missing), /invalid AI Shadow advisory/u);
  assert.throws(
    () => validateDifyShadowResponse(response({ action: 'quote_realtime' })),
    /undeclared AI Shadow advisory output/u,
  );
});

test('rejects transaction claims, amounts, contact data, links, and placeholders', () => {
  for (const replyDraft of [
    '价格是53元，可以付款了。',
    '价格是五十三元。',
    '已经锁座成功。',
    '订单已经改价完成。',
    '请联系13800138000。',
    '请打开https://example.com查看。',
    '当前价格是{价格}。',
  ]) {
    assert.throws(() => validateDifyShadowResponse(response({ reply_draft: replyDraft })), /unsafe AI Shadow reply draft/u);
  }
});

test('rejects invalid enum, confidence, reason, and missing-field values', () => {
  assert.throws(() => validateDifyShadowResponse(response({ intent: '执行改价' })), /invalid AI Shadow intent/u);
  assert.throws(() => validateDifyShadowResponse(response({ confidence: 2 })), /invalid AI Shadow confidence/u);
  assert.throws(() => validateDifyShadowResponse(response({ reason_code: '../unsafe' })), /invalid AI Shadow reason code/u);
  assert.throws(() => validateDifyShadowResponse(response({ missing_fields: ['order_id'] })), /invalid AI Shadow missing fields/u);
  assert.throws(() => validateDifyShadowResponse(response({ handoff_recommended: 'yes' })), /invalid AI Shadow handoff recommendation/u);
});
