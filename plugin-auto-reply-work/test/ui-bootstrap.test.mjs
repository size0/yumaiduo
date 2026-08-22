import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

test('ordinary dashboard startup does not request restricted review-admin resources', async () => {
  const appPath = fileURLToPath(new URL('../ui/app.js', import.meta.url));
  const source = await readFile(appPath, 'utf8');
  const start = source.indexOf('async function loadData()');
  const end = source.indexOf('async function resolveSdk()');
  assert.ok(start >= 0 && end > start);
  assert.doesNotMatch(source.slice(start, end), /evaluationReview\.loadData\(\)/u);
});
