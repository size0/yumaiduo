import assert from 'node:assert/strict';
import test from 'node:test';
import { hasRequiredPricingAccountEvidence } from '../src/quote/quote-evidence-policy.mjs';

test('direct Wanda temporary pricing requires an opaque account evidence reference', () => {
  assert.equal(hasRequiredPricingAccountEvidence({
    pricing_source: '万达临时锁座 available-offers + 后台报价规则',
    pricing_account_ref: 'a'.repeat(32),
  }), true);
  assert.equal(hasRequiredPricingAccountEvidence({
    pricing_source: '万达临时锁座 available-offers + 后台报价规则',
  }), false);
  assert.equal(hasRequiredPricingAccountEvidence({
    pricing_source: 'Wanda available-offers', pricing_account_ref: 'raw-account-id',
  }), false);
});

test('legacy non-direct quotes remain compatible without invented account evidence', () => {
  assert.equal(hasRequiredPricingAccountEvidence({ pricing_source: 'legacy reviewed quote' }), true);
  assert.equal(hasRequiredPricingAccountEvidence({}), true);
});
