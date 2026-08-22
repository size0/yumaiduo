import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { FileSettingsStore } from '../src/settings-store.mjs';

async function fixture(options = {}) {
  const directory = await mkdtemp(path.join(tmpdir(), 'wanda-settings-'));
  const file = path.join(directory, 'settings.json');
  const store = new FileSettingsStore(file, options);
  await store.initialize();
  return { directory, file, store };
}

test('runtime settings are isolated by tenant and do not own quote policy', async (t) => {
  const { directory, store } = await fixture();
  t.after(() => rm(directory, { recursive: true, force: true }));

  const defaults = await store.get('tenant-a');
  assert.equal(Object.hasOwn(defaults, 'wplus_adjustment_cents'), false);
  assert.equal(Object.hasOwn(defaults, 'regular_adjustment_cents'), false);
  assert.equal(defaults.ai_key_configured, false);

  await store.update('tenant-a', { automation_enabled: false });
  assert.equal((await store.get('tenant-a')).automation_enabled, false);
  assert.equal(Object.hasOwn(await store.get('tenant-a'), 'wplus_adjustment_cents'), false);
  assert.equal((await store.get('tenant-b')).automation_enabled, true);
});

test('merchant model API key is encrypted at rest and never returned by public settings', async (t) => {
  const apiKey = 'sk-merchant-secret-value';
  const { directory, file, store } = await fixture({ encryptionKey: randomBytes(32) });
  t.after(() => rm(directory, { recursive: true, force: true }));

  const updated = await store.update('tenant-a', {
    ai_base_url: 'https://example.com/v1',
    ai_model: 'vision-model-1',
    ai_api_key: apiKey,
  });
  assert.equal(updated.ai_key_configured, true);
  assert.equal(Object.hasOwn(updated, 'ai_api_key'), false);
  assert.deepEqual(await store.getAiCredentials('tenant-a'), {
    baseUrl: 'https://example.com/v1',
    model: 'vision-model-1',
    apiKey,
  });
  assert.equal((await readFile(file, 'utf8')).includes(apiKey), false);
});

test('saving a merchant API key fails closed without an encryption key', async (t) => {
  const { directory, store } = await fixture();
  t.after(() => rm(directory, { recursive: true, force: true }));
  await assert.rejects(
    store.update('tenant-a', { ai_api_key: 'secret' }),
    /CONFIG_ENCRYPTION_KEY/u,
  );
});

test('model endpoint rejects public plaintext HTTP URLs', async (t) => {
  const { directory, store } = await fixture({ encryptionKey: randomBytes(32) });
  t.after(() => rm(directory, { recursive: true, force: true }));
  await assert.rejects(
    store.update('tenant-a', { ai_base_url: 'http://models.example.com/v1' }),
    /HTTPS/u,
  );
});
