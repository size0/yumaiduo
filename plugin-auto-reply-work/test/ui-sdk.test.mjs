import assert from 'node:assert/strict';
import test from 'node:test';
import { buildGatewayUrl, normalizeGatewayRequestPath } from '../ui/sdk.js';

test('gateway API paths use the UI mount only when the session base is the gateway root', () => {
  const rootBase = 'https://core.example.com/api/v1/plugin/wanda-seat-autoquote/gateway';
  const uiBase = `${rootBase}/ui`;

  assert.equal(normalizeGatewayRequestPath(rootBase, 'api/overview'), 'ui/api/overview');
  assert.equal(buildGatewayUrl(rootBase, normalizeGatewayRequestPath(rootBase, 'api/overview')), `${rootBase}/ui/api/overview`);
  assert.equal(normalizeGatewayRequestPath(uiBase, 'api/overview'), 'api/overview');
  assert.equal(buildGatewayUrl(uiBase, normalizeGatewayRequestPath(uiBase, 'api/overview')), `${uiBase}/api/overview`);
  assert.equal(normalizeGatewayRequestPath(rootBase, 'styles.css'), 'styles.css');
});
