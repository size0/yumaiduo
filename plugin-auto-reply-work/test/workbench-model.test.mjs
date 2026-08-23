import assert from 'node:assert/strict';
import test from 'node:test';
import {
  automationStatusFrom,
  blockerLabel,
  buildActionQueue,
  sampleSummaryFrom,
} from '../ui/workbench-model.js';

test('automation status exposes mixed safe production mode without calling it fully running', () => {
  assert.deepEqual(automationStatusFrom({
    automation_enabled: true,
    recognition_enabled: true,
    quote_enabled: false,
    price_change_enabled: true,
    ai_reply_enabled: false,
    shadow_evaluation_enabled: true,
  }), [
    { key: 'recognition', label: '截图识别', enabled: true, tone: 'success' },
    { key: 'quote', label: '自动报价', enabled: false, tone: 'danger' },
    { key: 'price-change', label: '待付款改价', enabled: true, tone: 'success' },
    { key: 'ai-reply', label: 'AI买家回复', enabled: false, tone: 'neutral' },
    { key: 'shadow', label: 'Shadow评测', enabled: true, tone: 'info' },
  ]);
});

test('sample summary keeps text, image and human evidence separate', () => {
  assert.deepEqual(sampleSummaryFrom({
    readiness: { runtime_version: 'runtime-v33', audited_sample_count: 91, minimum_sample_count: 100, tool_selection_accuracy: 92.3 },
    image: { sample_count: 32, minimum_sample_count: 100, full_path_pass_rate: 85.7 },
    comparisons: [{ review: { status: 'unreviewed' } }, { review: { status: 'reviewed' } }],
  }), {
    runtimeVersion: 'runtime-v33',
    audit: { value: 91, target: 100, rate: 92.3 },
    image: { value: 32, target: 100, rate: 85.7 },
    human: { value: 2, pending: 1, versionScoped: false },
  });
});

test('risk queue excludes normal manual reply and fulfillment states', () => {
  const queue = buildActionQueue({
    now: Date.parse('2026-08-23T08:00:00Z'),
    operations: [
      { event_id: 'event-quoted', buyer_label: '买家B', stage: 'quoted', updated_at: '2026-08-23T07:00:00Z' },
      { event_id: 'event-paid', buyer_label: '买家D', stage: 'paid_manual_delivery', updated_at: '2026-08-23T07:03:00Z' },
    ],
    orders: [
      { order_id: 'order-normal', buyer_label: '买家E', stage: 'exception_review', exception_reason: 'paid_quote_unconfirmed_or_expired', updated_at: '2026-08-23T07:05:00Z' },
      { order_id: 'order-old', buyer_label: '买家F', stage: 'exception_review', exception_reason: 'paid_amount_mismatch', updated_at: '2026-08-20T07:05:00Z' },
    ],
  });
  assert.deepEqual(queue, []);
});

test('risk queue shows only recent concrete transaction risks in plain language', () => {
  const queue = buildActionQueue({
    now: Date.parse('2026-08-23T08:00:00Z'),
    operations: [{ event_id: 'duplicate', order_id: 'order-1', exception_reason: 'paid_amount_mismatch', updated_at: '2026-08-23T07:01:00Z' }],
    orders: [{ order_id: 'order-1', buyer_label: '买家A', exception_reason: 'paid_amount_mismatch', updated_at: '2026-08-23T07:02:00Z' }],
    manualTasks: [{ task_id: 'manual-1', buyer_label: '买家C', status: 'open', priority: 'high', summary: '人工核价', updated_at: '2026-08-23T07:03:00Z' }],
  });
  assert.equal(queue.length, 2);
  assert.equal(queue[0].kind, 'money-risk');
  assert.equal(queue[0].title, '实付金额与确认金额不一致');
  assert.equal(queue[0].detail, '先核对平台实付金额，不要继续自动处理。');
  assert.equal(queue[1].kind, 'manual-task');
  assert.equal(queue.filter((item) => item.orderId === 'order-1').length, 1);
});

test('blocker labels translate safety gate codes for operators', () => {
  assert.equal(blockerLabel('tool_selection_accuracy_below_95'), '工具选择正确率低于95%');
  assert.equal(blockerLabel('unknown_new_blocker'), 'unknown_new_blocker');
});
