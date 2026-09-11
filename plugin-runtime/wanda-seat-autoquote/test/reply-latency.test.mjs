import test from 'node:test';
import assert from 'node:assert/strict';
import { latencyRun, latencyStage } from '../src/runtime/latency.mjs';

test('latency summary records bounded stage timing without sensitive values', async () => {
  const logs = []; const logger = { info: (_, data) => logs.push(data) };
  await latencyRun('sensitive-event', 'event', logger, async () => {
    await latencyStage('recent_messages', async () => []);
    await latencyStage('backend_http', async () => ({ status: 202 }));
  });
  const output = JSON.stringify(logs);
  assert.ok(!output.includes('sensitive-event'));
  assert.equal(logs.at(-1).stages.recent_messages.count, 1);
  assert.equal(logs.at(-1).stages.backend_http.count, 1);
  assert.ok(Number.isFinite(logs.at(-1).total_ms));
});
