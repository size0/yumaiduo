import assert from 'node:assert/strict';
import test from 'node:test';
import { buildV37Report, validateFrozenDataset } from '../scripts/eval-v37.mjs';

function datasets() {
  const failure = {
    schema_version: 1, kind: 'v37-failure-100', dataset_id: 'failure-final', frozen: true,
    cases: Array.from({ length: 100 }, (_, index) => ({ case_id: `failure-${index}`, expected_source: 'human', expected_action: 'handoff', expected_disposition: 'safe', recoverable: index % 2 === 0 })),
  };
  const image = {
    schema_version: 1, kind: 'v37-image-100', dataset_id: 'image-final', frozen: true,
    cases: Array.from({ length: 100 }, (_, index) => ({ case_id: `image-${index}`, expected_source: 'authoritative', consent: true, image_url: `https://example.test/${index}.png`, identity_gold: { cinema: '影院', movie: '影片', date: '2026-08-24', showtime: '20:00' }, identity_eligible: true })),
  };
  return { failure, image };
}

test('frozen eval rejects labels derived from current model output', () => {
  const { failure } = datasets();
  failure.cases[0].expected_source = 'model_output';
  assert.throws(() => validateFrozenDataset(failure, 'v37-failure-100'), /independent/u);
});

test('v37 report scores independent gold and requires current image recognition path', () => {
  const { failure, image } = datasets();
  const failureResults = failure.cases.map((item) => ({ case_id: item.case_id, action: 'handoff', disposition: 'safe', status: item.recoverable ? 'completed' : 'handoff', side_effect_count: 0, latency_ms: 10, incidents: {} }));
  const imageResults = image.cases.map((item) => ({ case_id: item.case_id, status: 'completed', recognition_reexecuted: true, recognition_response_sha256: 'a'.repeat(64), schema_valid: true, precondition_replans: 0, tool_path: ['recognize_image', 'resolve_showtime', 'quote_realtime'], identity: item.identity_gold, latency_ms: 20, incidents: {} }));
  failureResults[0].incidents = { cross_tenant_access: 999 };
  const report = buildV37Report({ failureDataset: failure, imageDataset: image, failureResults, imageResults, versions: { model: 'm', prompt: 'p', tool: 't', knowledge: 'k', grader: 'g' }, runIndex: 1 });
  assert.equal(report.metrics.tool_selection_accuracy, 100);
  assert.equal(report.metrics.unrecoverable_safe_handoff_rate, 100);
  assert.equal(report.metrics.image_full_path_rate, 100);
  assert.equal(report.metrics.identity_accuracy, 100);
  assert.equal(report.metrics.cross_tenant_access, 0, 'runner self-labels are ignored by the grader');
  assert.equal(report.report_sha256.length, 64);
});
