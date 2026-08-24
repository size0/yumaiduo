import { createHash } from 'node:crypto';

function boundedPercent(value) {
  const number = Number(value);
  return Number.isInteger(number) && number >= 1 && number <= 100 ? number : 0;
}

export function agentCanaryBucket(tenantId, eventId) {
  const key = `${String(tenantId ?? '').trim()}:${String(eventId ?? '').trim()}`;
  if (key === ':') throw new TypeError('tenantId and eventId are required');
  return createHash('sha256').update(key).digest().readUInt32BE(0) % 10_000;
}

export function agentCanaryDecision({ settings = {}, tenantId, eventId, runtimeVersion, eligible = false } = {}) {
  const percentage = boundedPercent(settings.agent_canary_percentage);
  let reason = 'selected';
  if (settings.agent_canary_kill_switch !== false) reason = 'kill_switch_enabled';
  else if (settings.agent_canary_enabled !== true) reason = 'canary_disabled';
  else if (!percentage) reason = 'invalid_or_zero_percentage';
  else if (settings.agent_canary_approved !== true) reason = 'approval_missing';
  else if (!String(runtimeVersion ?? '').trim() || String(settings.agent_canary_runtime_version ?? '') !== String(runtimeVersion)) reason = 'runtime_version_mismatch';
  else if (eligible !== true) reason = 'turn_not_low_risk';
  const bucket = agentCanaryBucket(tenantId, eventId);
  if (reason === 'selected' && bucket >= percentage * 100) reason = 'outside_percentage';
  return Object.freeze({ selected: reason === 'selected', reason, percentage, bucket });
}
