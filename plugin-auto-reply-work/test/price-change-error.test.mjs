import test from 'node:test';
import assert from 'node:assert/strict';
import { classifyPriceChangeError } from '../src/price-change-error.mjs';

test('generic OAuth flow wrapper preserves a concrete business rejection and safe amount review', () => {
  const error = Object.assign(new Error('rejected'), {
    status: 400,
    code: 'E_OAUTH_FLOW_FAILED',
    body: { message: 'CANNOT_MODIFY_FEE' },
    priceChangeReview: {
      current_total_cents: 1500,
      target_total_cents: 9160,
      current_transport_cents: 0,
      direction: 'increase',
    },
  });
  const result = classifyPriceChangeError(error);
  assert.equal(result.kind, 'business_rejected');
  assert.equal(result.code, 'CANNOT_MODIFY_FEE');
  assert.equal(result.terminal, true);
  assert.equal(result.diagnostics.provider_reason_code, 'CANNOT_MODIFY_FEE');
  assert.equal(result.diagnostics.current_total_cents, 1500);
  assert.equal(result.diagnostics.target_total_cents, 9160);
  assert.equal(result.diagnostics.current_transport_cents, 0);
  assert.equal(result.diagnostics.direction, 'increase');
});

test('OAuth flow wrapper without a concrete reason is an upstream rejection, not proven authorization failure', () => {
  const result = classifyPriceChangeError(Object.assign(new Error('rejected'), {
    status: 400,
    code: 'E_OAUTH_FLOW_FAILED',
  }));
  assert.equal(result.kind, 'upstream_rejected');
  assert.equal(result.code, 'PRICE_CHANGE_UPSTREAM_REJECTED');
  assert.equal(result.terminal, true);
});

test('HTTP authorization statuses remain authorization failures', () => {
  const result = classifyPriceChangeError(Object.assign(new Error('forbidden'), { status: 403 }));
  assert.equal(result.kind, 'authorization_failed');
  assert.equal(result.code, 'PRICE_CHANGE_AUTHORIZATION_FAILED');
  assert.equal(result.terminal, true);
});
