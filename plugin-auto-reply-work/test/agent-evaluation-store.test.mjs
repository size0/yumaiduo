import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { AgentEvaluationStore, agentCanaryReadinessFrom, automatedAgentSafetyReviewFrom } from '../src/agent-evaluation-store.mjs';

const safeReview = {
  expected_action: 'start_quote', should_ask: false, should_handoff: false,
  reply_quality: 'not_applicable', authoritative_consistency: 'consistent',
  high_risk_action: false, false_claim: false, duplicate_question: false,
};

test('agent evaluations are tenant isolated and contain only bounded review fields', async () => {
  const store = new AgentEvaluationStore(join(await mkdtemp(join(tmpdir(), 'agent-eval-')), 'reviews.json'), { now: () => 123 });
  await store.review('tenant-1', 'event-1', safeReview);
  assert.deepEqual(await store.list('tenant-2'), []);
  assert.deepEqual(await store.list('tenant-1'), [{
    tenant_id: 'tenant-1', event_id: 'event-1', ...safeReview, safety_audited: true, reviewed_at: 123,
  }]);
  await assert.rejects(() => store.review('tenant-1', 'event-2', { ...safeReview, expected_action: 'change_price' }), /invalid agent evaluation/u);
  await assert.rejects(() => store.review('tenant-1', 'event-3', { ...safeReview, false_claim: undefined }), /invalid agent evaluation/u);
});

test('automated safety review replaces manual sample review without hiding failed runs', () => {
  const safe = automatedAgentSafetyReviewFrom({
    actual_action: 'recognize_image', run_status: 'completed', result_status: 'reply', reason: 'authoritative_tool_response',
    tool_names: ['recognize_image', 'resolve_showtime', 'quote_realtime'], authoritative_outcome: 'quote_succeeded',
    final_reply_safe: true, authoritative_consistent: true,
  });
  assert.equal(safe.safety_audited, true);
  assert.equal(safe.expected_action, 'recognize_image');
  assert.equal(safe.reply_quality, 'qualified');
  assert.equal(safe.audit_source, 'automated');

  const failed = automatedAgentSafetyReviewFrom({
    actual_action: 'recognize_image', run_status: 'timed_out', result_status: 'handoff', reason: 'agent_deadline_exceeded',
    tool_names: ['recognize_image'], authoritative_outcome: 'quote_succeeded', final_reply_safe: false, authoritative_consistent: true,
  });
  assert.equal(failed.safety_audited, true);
  assert.notEqual(failed.expected_action, 'recognize_image');
  assert.equal(failed.reply_quality, 'unqualified');
});

test('agent canary readiness requires audited samples and every safety gate', () => {
  const clean = Array.from({ length: 100 }, (_, index) => ({
    actual_action: index < 96 ? 'recognize_image' : 'ask_for_image',
    review: {
      expected_action: 'recognize_image', safety_audited: true, high_risk_action: false,
      false_claim: false, duplicate_question: false, authoritative_consistency: 'consistent', reply_quality: 'qualified',
    },
  }));
  const ready = agentCanaryReadinessFrom(clean);
  assert.equal(ready.ready, true);
  assert.equal(ready.tool_selection_accuracy, 96);
  assert.deepEqual(ready.blockers, []);

  const unsafe = agentCanaryReadinessFrom([
    ...clean.slice(0, 99),
    { actual_action: 'recognize_image', review: { ...clean[0].review, high_risk_action: true } },
  ]);
  assert.equal(unsafe.ready, false);
  assert.deepEqual(unsafe.blockers, ['high_risk_action_detected']);

  const missingReply = agentCanaryReadinessFrom([
    ...clean.slice(0, 99),
    { actual_action: 'recognize_image', authoritative_outcome: 'quote_succeeded', final_reply_safe: false, review: clean[0].review },
  ]);
  assert.equal(missingReply.ready, false);
  assert.equal(missingReply.missing_or_unsafe_final_reply_count, 1);
  assert.deepEqual(missingReply.blockers, ['missing_or_unsafe_final_reply_detected']);

  const unqualified = agentCanaryReadinessFrom([
    ...clean.slice(0, 99),
    { actual_action: 'recognize_image', review: { ...clean[0].review, reply_quality: 'unqualified' } },
  ]);
  assert.equal(unqualified.ready, false);
  assert.equal(unqualified.unqualified_reply_count, 1);
  assert.deepEqual(unqualified.blockers, ['unqualified_reply_detected']);
});
