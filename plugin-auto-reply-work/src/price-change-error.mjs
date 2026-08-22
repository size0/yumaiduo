const AUTHORIZATION_CODES = new Set(['E_UNAUTHORIZED', 'E_FORBIDDEN']);
const UPSTREAM_WRAPPER_CODES = new Set(['E_OAUTH_FLOW_FAILED']);
const BUSINESS_REJECTION_CODES = new Set(['CANNOT_MODIFY_FEE']);

function safeCode(value) {
  const code = String(value ?? '').trim().toUpperCase();
  return /^[A-Z0-9_]{1,80}$/.test(code) ? code : null;
}

function safeCents(value) {
  const cents = Number(value);
  return Number.isSafeInteger(cents) && cents >= 0 && cents <= 10_000_000_000 ? cents : null;
}

function safeAmountReview(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const currentTotal = safeCents(value.current_total_cents);
  const targetTotal = safeCents(value.target_total_cents);
  const currentTransport = safeCents(value.current_transport_cents);
  const direction = ['increase', 'decrease', 'unchanged'].includes(value.direction) ? value.direction : null;
  return {
    ...(currentTotal != null ? { current_total_cents: currentTotal } : {}),
    ...(targetTotal != null ? { target_total_cents: targetTotal } : {}),
    ...(currentTransport != null ? { current_transport_cents: currentTransport } : {}),
    ...(direction ? { direction } : {}),
  };
}

export function classifyPriceChangeError(error) {
  const sdkCode = safeCode(error?.code ?? error?.errorCode);
  const providerCode = safeCode(error?.body?.errorCode ?? error?.body?.code);
  const providerReasonCode = safeCode(
    error?.body?.retCode
      ?? error?.body?.reasonCode
      ?? error?.body?.message
      ?? error?.body?.error?.code
      ?? error?.body?.error?.message,
  );
  const status = Number(error?.status);
  const httpStatus = Number.isInteger(status) && status >= 100 && status <= 599 ? status : null;
  const codes = new Set([sdkCode, providerCode, providerReasonCode].filter(Boolean));
  const businessRejected = [...codes].some((code) => BUSINESS_REJECTION_CODES.has(code));
  const authorizationFailure = !businessRejected && (
    [...codes].some((code) => AUTHORIZATION_CODES.has(code)) || httpStatus === 401 || httpStatus === 403
  );
  const upstreamRejected = !businessRejected && !authorizationFailure
    && [...codes].some((code) => UPSTREAM_WRAPPER_CODES.has(code));
  const kind = businessRejected
    ? 'business_rejected'
    : authorizationFailure
      ? 'authorization_failed'
      : upstreamRejected
        ? 'upstream_rejected'
        : 'unknown';
  return Object.freeze({
    kind,
    terminal: businessRejected || authorizationFailure || upstreamRejected,
    code: businessRejected
      ? 'CANNOT_MODIFY_FEE'
      : authorizationFailure
        ? 'PRICE_CHANGE_AUTHORIZATION_FAILED'
        : upstreamRejected
          ? 'PRICE_CHANGE_UPSTREAM_REJECTED'
          : 'PRICE_CHANGE_REJECTED',
    diagnostics: Object.freeze({
      failure_stage: 'change_price',
      http_status: httpStatus,
      sdk_error_code: sdkCode,
      provider_error_code: providerCode,
      provider_reason_code: providerReasonCode,
      ...safeAmountReview(error?.priceChangeReview),
    }),
  });
}
