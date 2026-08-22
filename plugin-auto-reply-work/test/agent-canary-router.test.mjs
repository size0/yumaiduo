import assert from 'node:assert/strict';
import test from 'node:test';
import { agentCanaryBucket, agentCanaryDecision } from '../src/agent/agent-canary-router.mjs';

const version = 'runtime-v1';
const approved = {
  agent_canary_enabled: true,
  agent_canary_kill_switch: false,
  agent_canary_percentage: 5,
  agent_canary_approved: true,
  agent_canary_runtime_version: version,
};

function selectedEventId() {
  for (let index = 0; index < 10_000; index += 1) {
    const eventId = `event-${index}`;
    if (agentCanaryBucket('tenant-1', eventId) < 500) return eventId;
  }
  throw new Error('no deterministic canary sample found');
}

test('canary defaults to zero traffic and fails closed on every missing gate', () => {
  const base = { tenantId: 'tenant-1', eventId: selectedEventId(), runtimeVersion: version, eligible: true };
  assert.equal(agentCanaryDecision({ ...base, settings: {} }).reason, 'kill_switch_enabled');
  assert.equal(agentCanaryDecision({ ...base, settings: { ...approved, agent_canary_enabled: false } }).selected, false);
  assert.equal(agentCanaryDecision({ ...base, settings: { ...approved, agent_canary_percentage: 6 } }).reason, 'invalid_or_zero_percentage');
  assert.equal(agentCanaryDecision({ ...base, settings: { ...approved, agent_canary_approved: false } }).reason, 'approval_missing');
  assert.equal(agentCanaryDecision({ ...base, settings: { ...approved, agent_canary_runtime_version: 'old' } }).reason, 'runtime_version_mismatch');
  assert.equal(agentCanaryDecision({ ...base, settings: approved, eligible: false }).reason, 'turn_not_low_risk');
});

test('approved five-percent canary uses a stable tenant and event bucket', () => {
  const eventId = selectedEventId();
  const first = agentCanaryDecision({ settings: approved, tenantId: 'tenant-1', eventId, runtimeVersion: version, eligible: true });
  const second = agentCanaryDecision({ settings: approved, tenantId: 'tenant-1', eventId, runtimeVersion: version, eligible: true });
  assert.equal(first.selected, true);
  assert.deepEqual(second, first);
  assert.ok(first.bucket >= 0 && first.bucket < 500);
});
