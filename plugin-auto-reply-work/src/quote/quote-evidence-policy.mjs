const DIRECT_WANDA_PRICING = /(?:万达临时锁座|wanda\s+available-offers|available-offers\s*\+\s*后台报价规则)/iu;
const OPAQUE_ACCOUNT_REF = /^[a-f0-9]{32}$/u;

/** Require account attribution only for quotes produced by the direct temporary-probe path. */
export function hasRequiredPricingAccountEvidence(facts = {}) {
  const source = String(facts?.pricing_source ?? '').trim();
  if (!DIRECT_WANDA_PRICING.test(source)) return true;
  return OPAQUE_ACCOUNT_REF.test(String(facts?.pricing_account_ref ?? '').trim());
}
