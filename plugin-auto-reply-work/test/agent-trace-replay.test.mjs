import assert from 'node:assert/strict';
import test from 'node:test';
import { agentTraceReplayFrom } from '../src/agent/agent-trace-replay.mjs';

test('agent trace replay joins plans, journaled tools, observations, and authoritative outcome', () => {
  const replay = agentTraceReplayFrom({
    run_id: 'shadow:tenant-1:event-1', event_key: 'tenant-1:event-1', tenant_id: 'tenant-1', mode: 'shadow',
    status: 'completed', attempts: 1, created_at: '2026-08-22T00:00:00.000Z', updated_at: '2026-08-22T00:00:02.000Z',
    trace: [
      { step: 1, action: 'recognize_image', intent: '选座核价', confidence: 0.98, goal: '识图', missing_fields: [] },
      { step: 2, action: 'resolve_showtime', intent: '选座核价', confidence: 0.99, goal: '匹配场次', missing_fields: [] },
    ],
    observations: [
      { status: 'success', tool: 'recognize_image', summary: '识图完成', facts: { cinema: '测试万达' }, next_actions: ['resolve_showtime'] },
      { status: 'success', tool: 'resolve_showtime', summary: '场次唯一', facts: { showtime: '19:00' }, next_actions: ['quote_realtime'] },
    ],
    tool_calls: [
      { call_id: 'tool:1:recognize_image', step: 1, tool: 'recognize_image', status: 'completed', started_at: '2026-08-22T00:00:00.500Z', completed_at: '2026-08-22T00:00:01.000Z', observation: { status: 'success', tool: 'recognize_image', summary: '识图完成', facts: { cinema: '测试万达' }, next_actions: ['resolve_showtime'] } },
      { call_id: 'tool:2:resolve_showtime', step: 2, tool: 'resolve_showtime', status: 'completed', started_at: '2026-08-22T00:00:01.100Z', completed_at: '2026-08-22T00:00:01.600Z', observation: { status: 'success', tool: 'resolve_showtime', summary: '场次唯一', facts: { showtime: '19:00' }, next_actions: ['quote_realtime'] } },
    ],
    result: { runtime_version: 'runtime-v5', status: 'handoff', reason: 'agent_step_limit', authoritative_outcome: 'quote_failed', proposed_reply: '请人工确认。' },
  }, {
    envelope: { tenantId: 'tenant-1', id: 'event-1', payload: { content: '手机号13800138000，订单123456789012345', imageUrls: ['https://example/image.jpg'], peerNick: '测试买家' } },
    result: { quote_failure_code: 'showtime_not_unique' },
  });

  assert.equal(replay.runtime_version, 'runtime-v5');
  assert.equal(replay.duration_ms, 2000);
  assert.equal(replay.source.turn_summary, '买家说：手机号[手机号]，订单[编号]；并发送1张图片');
  assert.equal(replay.steps.length, 2);
  assert.equal(replay.steps[0].tool.name, 'recognize_image');
  assert.equal(replay.steps[0].tool.duration_ms, 500);
  assert.deepEqual(replay.steps[1].observation.facts, { showtime: '19:00' });
  assert.equal(replay.outcome.reason, 'agent_step_limit');
});

test('agent trace replay rejects tenant mismatch and does not expose raw event payload', () => {
  assert.throws(() => agentTraceReplayFrom({ run_id: 'run', event_key: 'event', tenant_id: 'tenant-a', trace: [], tool_calls: [] }, {
    envelope: { tenantId: 'tenant-b', payload: { content: 'secret' } },
  }), /tenant mismatch/u);
  const replay = agentTraceReplayFrom({ run_id: 'run', event_key: 'event', tenant_id: 'tenant-a', trace: [], tool_calls: [], result: {} }, {
    envelope: { tenantId: 'tenant-a', payload: { content: '普通咨询', token: 'must-not-leak' } }, result: {},
  });
  assert.doesNotMatch(JSON.stringify(replay), /must-not-leak/u);
});
