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

test('action queue prioritizes paid exceptions and open manual tasks over passive conversations', () => {
  const queue = buildActionQueue({
    operations: [
      { event_id: 'event-quoted', buyer_label: '买家B', stage: 'quoted', next_action: '等待确认', updated_at: '2026-08-23T07:00:00Z' },
      { event_id: 'event-paid', buyer_label: '买家A', stage: 'exception_review', exception_reason: 'paid_amount_mismatch', order_id: 'order-1', updated_at: '2026-08-23T07:01:00Z' },
      { event_id: 'event-done', buyer_label: '买家D', stage: 'ticket_sent', updated_at: '2026-08-23T07:03:00Z' },
    ],
    orders: [{ order_id: 'order-1', platform_order_status_text: '已付款，等待发货', platform_payment_cents: 20000 }],
    manualTasks: [{ task_id: 'manual-1', buyer_label: '买家C', status: 'open', priority: 'high', summary: '人工核价', updated_at: '2026-08-23T07:02:00Z' }],
  });
  assert.equal(queue[0].kind, 'order-exception');
  assert.equal(queue[0].severity, 'urgent');
  assert.equal(queue[1].kind, 'manual-task');
  assert.equal(queue.at(-1).kind, 'conversation');
  assert.equal(queue.filter((item) => item.orderId === 'order-1').length, 1);
  assert.equal(queue.some((item) => item.buyerLabel === '买家D'), false);
});

test('blocker labels translate safety gate codes for operators', () => {
  assert.equal(blockerLabel('tool_selection_accuracy_below_95'), '工具选择正确率低于95%');
  assert.equal(blockerLabel('unknown_new_blocker'), 'unknown_new_blocker');
});
