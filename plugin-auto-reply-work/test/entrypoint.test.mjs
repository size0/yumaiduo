import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';
import { isEntrypointPath } from '../index.mjs';

test('accepts a systemd entrypoint reached through the current release symlink', () => {
  const modulePath = path.resolve('virtual', 'releases', 'release-1', 'plugins', 'wanda-seat-autoquote', 'index.mjs');
  const moduleUrl = pathToFileURL(modulePath).href;
  const argvPath = path.resolve('virtual', 'current', 'plugins', 'wanda-seat-autoquote', 'index.mjs');
  const resolveRealPath = (value) => (
    value.includes('release-1') || value.includes('current') ? 'canonical-entrypoint' : value
  );

  assert.equal(isEntrypointPath(moduleUrl, argvPath, resolveRealPath), true);
});

test('rejects a different executable module', () => {
  assert.equal(
    isEntrypointPath('file:///srv/plugin/index.mjs', '/srv/other.mjs', (value) => value),
    false,
  );
});
