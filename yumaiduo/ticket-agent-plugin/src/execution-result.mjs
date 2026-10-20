// Only explicit business refusals prove the write was rejected. Transport errors do not.
const refusals = new Set(['CANNOT_MODIFY_FEE', 'ORDER_CLOSED', 'ORDER_NOT_FOUND',
  'ORDER_ALREADY_PAID', 'INVALID_PARAM', 'INVALID_PARAMS', 'PERMISSION_DENIED', 'FORBIDDEN']);

// The developer guide's response envelope puts the platform's business code in
// `errorCode` (`{ code, message, data, errorCode }`, thrown as PluginApiError with
// status / code). A refusal is therefore read from that structured field only, plus
// the documented `code` alias and the case where the message *is* the code - never by
// scanning a human-readable message for a word, which would invent a refusal the
// platform never named.
function codeOf(error) {
  if (!error || typeof error !== 'object') return null;
  // A captured PluginApiError envelope can wrap the exact platform refusal
  // in message while using E_OAUTH_FLOW_FAILED as its gateway code. Accept only
  // this consistent HTTP400 envelope and an exact allowlisted value, never prose.
  const body = error.body;
  if (error.status === 400 && error.errorCode === 'E_OAUTH_FLOW_FAILED' &&
      body?.code === 400 && body?.errorCode === error.errorCode &&
      body?.message === error.message && refusals.has(body.message)) return body.message;
  // `errorCode` / `code` are the documented fields. When either is present it is the
  // platform's code for this call, so the message is not consulted: a word inside
  // prose must never be promoted to a refusal.
  for (const value of [error.errorCode, error.code]) {
    if (typeof value === 'string' && value) return refusals.has(value) ? value : null;
  }
  // No structured code at all (a plain Error whose message is the code).
  return typeof error.message === 'string' && refusals.has(error.message) ? error.message : null;
}

export function rejectionCode(error) {
  return codeOf(error);
}

// Platform transport failures the developer guide marks safe to retry. Only these are
// ever retried, and only by callers that can prove the write did not land; a business
// refusal is never retryable.
const transientCodes = new Set(['E_PLUGIN_RATE_LIMITED', 'E_IM_WS_UNAVAILABLE']);
export function isTransient(error) {
  if (!error || typeof error !== 'object') return false;
  // These also use HTTP 429, but require credit/risk intervention rather than
  // rate-limit retries (developer guide v1.62, section 6.1).
  const noRetry = ['E_DEVELOPER_API_CREDIT_INSUFFICIENT', 'E_XIANYU_APP_GATEWAY_RISK_CONTROL'];
  if ([error.errorCode, error.code].some(code => noRetry.includes(code))) return false;
  if (error.status === 429) return true;
  for (const value of [error.errorCode, error.code]) {
    if (typeof value === 'string' && transientCodes.has(value)) return true;
  }
  return typeof error.message === 'string' && transientCodes.has(error.message);
}

// One place decides refuse / retry / unknown, so the reprice and send paths cannot
// drift apart. A refusal is the only shape that proves the write was rejected; an
// upstream flow failure that names no refusal stays unknown.
export function classifyExternalWrite(error) {
  const refusal = rejectionCode(error);
  if (refusal) return {kind: 'refused', code: refusal};
  if (isTransient(error)) return {kind: 'transient', code: error?.errorCode || error?.code || null};
  return {kind: 'unknown', code: null};
}

export function trustedMessageId(result) {
  const id = result?.messageId || result?.message_id;
  return typeof id === 'string' && id.trim() === id && id.length > 0 &&
    !id.startsWith('local-') && !/[\s\x00-\x1f]/.test(id) ? id : null;
}
