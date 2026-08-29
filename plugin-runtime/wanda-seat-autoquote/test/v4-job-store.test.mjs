import assert from 'node:assert/strict';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { V4UiJobStore } from '../src/runtime/v4-ui-job-store.mjs';

test('UI jobs are encrypted, durable, tenant scoped, and recover after restart', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-v4-ui-jobs-'));
  const key = Buffer.alloc(32, 31);
  const first = new V4UiJobStore(dataDir, key);
  await first.initialize();
  const created = await first.enqueue({
    tenantId: 'tenant-a', userId: 'user-a', contentType: 'application/json',
    rawBody: JSON.stringify({ secret: 'sensitive-image-payload' }),
  });
  const claimed = await first.claim();
  assert.equal(claimed.id, created.id);
  assert.equal(claimed.tenantId, 'tenant-a');
  assert.equal(claimed.rawBody.includes('sensitive-image-payload'), true);

  const serialized = await readFile(join(dataDir, `${created.id}.json`), 'utf8');
  assert.equal(serialized.includes('sensitive-image-payload'), false);

  const restarted = new V4UiJobStore(dataDir, key);
  await restarted.initialize();
  const recovered = await restarted.claim();
  assert.equal(recovered.id, created.id);
  assert.equal(recovered.attempts, 2);

  await restarted.complete(created.id, recovered.lease, {
    status: 200, contentType: 'application/json; charset=utf-8', body: Buffer.from('{"ok":true}'),
  });
  const denied = await restarted.get(created.id, { tenantId: 'tenant-b', userId: 'user-a' });
  assert.equal(denied, null);
  const result = await restarted.get(created.id, { tenantId: 'tenant-a', userId: 'user-a' });
  assert.equal(result.status, 'completed');
  assert.equal(result.result.body.toString(), '{"ok":true}');
});

test('UI job claims are atomic and enforce the pending-job limit', async () => {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-v4-ui-job-limit-'));
  const store = new V4UiJobStore(dataDir, Buffer.alloc(32, 32), { maxJobs: 2 });
  await store.initialize();
  await store.enqueue({ tenantId: 'tenant-a', userId: 'user-a', rawBody: '{}', contentType: 'application/json' });
  await store.enqueue({ tenantId: 'tenant-a', userId: 'user-a', rawBody: '{}', contentType: 'application/json' });
  await assert.rejects(
    () => store.enqueue({ tenantId: 'tenant-a', userId: 'user-a', rawBody: '{}', contentType: 'application/json' }),
    (error) => error.status === 503 && error.message === 'too_many_v4_jobs',
  );
  const claims = await Promise.all([store.claim(), store.claim()]);
  const claimedIds = claims.filter(Boolean).map((claim) => claim.id);
  assert.equal(new Set(claimedIds).size, 2);
});
