import assert from 'node:assert/strict';
import test from 'node:test';
import { agentImageOfflineEvaluationFrom } from '../src/agent/agent-offline-evaluator.mjs';

function event(key, result = { preview_status: 'preview_ready' }) {
  return { key, envelope: { payload: { imageUrls: ['https://img.example/seat.jpg'] } }, result };
}
function run(key, { status = 'completed', tools = ['recognize_image', 'resolve_showtime', 'quote_realtime'], outcome = 'quote_succeeded', duration = 1_000 } = {}) {
  return {
    event_key: key, mode: 'shadow', status, created_at: '2026-01-01T00:00:00.000Z', updated_at: new Date(Date.parse('2026-01-01T00:00:00.000Z') + duration).toISOString(),
    tool_calls: tools.map((tool, index) => ({ call_id: `${key}:${index}`, step: index + 1, tool, status: 'completed', observation: { status: 'success', tool } })),
    result: { authoritative_outcome: outcome, status: 'reply', reply_generated: true, authoritative_reply_used: true, proposed_reply: '50.00元/张，1张合计50.00元。' },
  };
}

test('offline image evaluation verifies full tool path, latency and one quote attempt', () => {
  const events = [event('107:e1'), event('107:e2')];
  const runs = [run('107:e1', { duration: 1_000 }), run('107:e2', { duration: 3_000 })];
  const report = agentImageOfflineEvaluationFrom(runs, events, { minimumSamples: 2 });
  assert.equal(report.ready, true);
  assert.equal(report.sample_count, 2);
  assert.equal(report.full_path_pass_rate, 100);
  assert.equal(report.quote_duplicate_count, 0);
  assert.equal(report.latency_p50_ms, 1_000);
  assert.equal(report.latency_p95_ms, 3_000);
});

test('offline image evaluation keeps counting durable image samples after source events age out', () => {
  const durable = run('107:expired');
  durable.result.source_snapshot = { has_image: true, authoritative_outcome: 'quote_succeeded' };
  const report = agentImageOfflineEvaluationFrom([durable], [], { minimumSamples: 1 });
  assert.equal(report.ready, true);
  assert.equal(report.sample_count, 1);
  assert.equal(report.successful_quote_sample_count, 1);
  assert.equal(report.full_path_pass_rate, 100);
});

test('offline evaluation counts a historical replay but never double-counts the same source event', () => {
  const live = run('107:e1', { duration: 1_000 });
  const replay = run('107:e1', { duration: 2_000 });
  replay.mode = 'evaluation';
  const report = agentImageOfflineEvaluationFrom([live, replay], [event('107:e1')], { minimumSamples: 1 });
  assert.equal(report.sample_count, 1);
  assert.equal(report.completed_count, 1);
  assert.equal(report.latency_p50_ms, 2_000);
});

test('offline image evaluation exposes prerequisite replans instead of hiding corrected model choices', () => {
  const recovered = run('107:e1');
  recovered.result.trace = [{ action: 'recognize_image' }, { action: 'quote_realtime' }, { action: 'resolve_showtime' }, { action: 'quote_realtime' }];
  const report = agentImageOfflineEvaluationFrom([recovered], [event('107:e1')], { minimumSamples: 1 });
  assert.equal(report.prerequisite_replan_count, 1);
  assert.equal(report.prerequisite_replan_rate, 100);
  assert.equal(report.ready, false);
  assert.ok(report.blockers.includes('prerequisite_replan_rate_above_5'));
});

test('offline image evaluation rejects a successful quote path without a final safe reply', () => {
  const missing = run('107:e1');
  missing.result.status = 'handoff';
  missing.result.reply_generated = false;
  delete missing.result.proposed_reply;
  const placeholder = run('107:e2');
  placeholder.result.proposed_reply = '影片：{影片}，价格：{价格}。';
  const report = agentImageOfflineEvaluationFrom([missing, placeholder], [event('107:e1'), event('107:e2')], { minimumSamples: 2 });
  assert.equal(report.ready, false);
  assert.equal(report.safe_reply_count, 0);
  assert.equal(report.missing_or_unsafe_reply_count, 2);
  assert.equal(report.full_path_pass_count, 0);
  assert.equal(report.full_path_pass_rate, 0);
  assert.ok(report.blockers.includes('missing_or_unsafe_final_reply_detected'));
  assert.ok(report.blockers.includes('full_path_pass_rate_below_95'));
});

test('offline image evaluation fails closed for duplicate quote, pending tool and outcome mismatch', () => {
  const duplicate = run('107:e1', { tools: ['recognize_image', 'resolve_showtime', 'quote_realtime', 'quote_realtime'], outcome: 'quote_failed' });
  duplicate.tool_calls[2].status = 'pending';
  duplicate.tool_calls[2].observation = null;
  const report = agentImageOfflineEvaluationFrom([duplicate], [event('107:e1')], { minimumSamples: 1 });
  assert.equal(report.ready, false);
  assert.equal(report.quote_duplicate_count, 1);
  assert.equal(report.unknown_tool_result_count, 1);
  assert.equal(report.authoritative_mismatch_count, 1);
  assert.deepEqual(report.blockers, ['full_path_pass_rate_below_95', 'quote_realtime_duplicate_detected', 'unknown_tool_result_detected', 'authoritative_outcome_mismatch']);
});
