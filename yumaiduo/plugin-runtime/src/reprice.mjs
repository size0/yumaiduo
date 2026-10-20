import {classifyExternalWrite} from './execution-result.mjs';
export function isRepriceAllowed(command, mode, env, authorizedShops) {
  if (authorizedShops !== undefined) {
    return command.type === 'CHANGE_ORDER_PRICE' && mode === 'live' &&
      env.LIVE_REPRICE_ENABLED === 'true' && Boolean(env.YUMAIDUO_TENANT_ID) &&
      command.tenant_id === env.YUMAIDUO_TENANT_ID && Array.isArray(authorizedShops) &&
      authorizedShops.some(shop => shop.tenant_id === command.tenant_id &&
        shop.shop_id === command.shop_id && shop.platform_enabled === true);
  }
  // Compatibility for callers without an official directory. Production passes it explicitly.
  return command.type === 'CHANGE_ORDER_PRICE' && mode === 'live' &&
    env.LIVE_REPRICE_ENABLED === 'true' &&
    Boolean(env.LIVE_SEND_ALLOWLIST_TENANT_ID && env.LIVE_SEND_ALLOWLIST_ACCOUNT_UNB) &&
    command.tenant_id === env.LIVE_SEND_ALLOWLIST_TENANT_ID &&
    command.shop_id === env.LIVE_SEND_ALLOWLIST_ACCOUNT_UNB;
}

// Read-back failures were previously indistinguishable: a thrown read error, a
// read error before any write, and a successful read with a mismatched amount all
// became the same UNKNOWN_RESULT status, and the error itself was discarded. This
// produces a bounded, credential-free summary the backend can persist.
const ERROR_FIELD_CAP = 160;
const ERROR_MESSAGE_CAP = 300;
export function describeError(error) {
  if (error === null || error === undefined) return `none`;
  if (typeof error !== 'object') return `non-object ${typeof error}`;
  const parts = [];
  if (typeof error.name === 'string' && error.name) parts.push(`name=${error.name}`);
  if (typeof error.status === 'number') parts.push(`status=${error.status}`);
  for (const key of ['errorCode', 'code']) {
    const value = error[key];
    if (typeof value === 'string' && value) { parts.push(`${key}=${value.slice(0, ERROR_FIELD_CAP)}`); break; }
  }
  if (typeof error.message === 'string' && error.message) parts.push(`message=${error.message.slice(0, ERROR_MESSAGE_CAP)}`);
  return parts.length ? parts.join(' ') : `unnamed ${error.constructor?.name || 'object'}`;
}

// Why the platform refused is not answerable from the summary above: it says what we
// were told, not what the platform showed for the order at that moment. These two
// snapshots are what make a future refusal diagnosable at all, and they exist only
// because the caller happened to read the order before writing. No credentials, no
// buyer identity and no titles: order state, amount and timestamps only.
const ORDER_FACT_CAP = 120;
export function orderSnapshot(order) {
  if (!order || typeof order !== 'object') return null;
  const kept = {};
  for (const key of ['orderStatus', 'orderStatusText', 'payment', 'postFee', 'quantity',
                     'payTime', 'createTime', 'grouponPending', 'rechargeOrder', 'itemId']) {
    const value = order[key];
    if (value === undefined || value === null) continue;
    kept[key] = typeof value === 'string' ? value.slice(0, ORDER_FACT_CAP) : value;
  }
  return Object.keys(kept).length ? kept : null;
}

const BODY_DEPTH = 3;
function boundedBody(value, depth = 0) {
  if (value === null || typeof value === 'number' || typeof value === 'boolean') return value;
  if (typeof value === 'string') return value.slice(0, ERROR_MESSAGE_CAP);
  if (depth >= BODY_DEPTH) return undefined;
  if (Array.isArray(value)) return value.slice(0, 5).map((item) => boundedBody(item, depth + 1));
  if (typeof value === 'object') {
    const out = {};
    for (const key of Object.keys(value)) {
      // Credentials never travel with a diagnostic.
      if (/token|secret|cookie|authorization|session|password/i.test(key)) continue;
      const bounded = boundedBody(value[key], depth + 1);
      if (bounded !== undefined) out[key] = bounded;
    }
    return out;
  }
  return undefined;
}

/** The error as the platform sent it, bounded and credential-free, or null. */
export function errorDetail(error) {
  if (!error || typeof error !== 'object') return null;
  const detail = {
    name: typeof error.name === 'string' ? error.name.slice(0, ERROR_FIELD_CAP) : null,
    status: typeof error.status === 'number' ? error.status : null,
    errorCode: typeof error.errorCode === 'string' ? error.errorCode.slice(0, ERROR_FIELD_CAP) : null,
    code: typeof error.code === 'string' ? error.code.slice(0, ERROR_FIELD_CAP) : null,
    message: typeof error.message === 'string' ? error.message.slice(0, ERROR_MESSAGE_CAP) : null,
    body: boundedBody(error.body) ?? null,
  };
  return Object.values(detail).some((value) => value !== null) ? detail : null;
}

const RETRY_BASE_MS = 200;
const RETRY_ATTEMPTS = 2;

// CHANGE_ORDER_PRICE carries no idempotency key (the developer guide's endpoint
// table requires one only for red-flower and remind-receipt), so an unconfirmed
// write must never be retried blindly. What is safe is to LOOK: if the order
// already holds the authorised amount, the write landed and only its response was
// lost, which is a success rather than an unknown. A failed read returns null and
// the caller then stays unconfirmed.
async function priceAfterFailure(client, orderId) {
  try {
    const after = await client.orders.get(orderId);
    return {amount: Number(after?.payment), order: after};
  } catch {
    return null;
  }
}

// The only write this path may repeat: the platform's own transport failures, and
// only once a read has shown the amount is still below the authorisation, so the
// repeat cannot charge a second time.
async function retryQuietTransport(client, authorized, error, sleep) {
  let current = error;
  for (let attempt = 1; attempt <= RETRY_ATTEMPTS; attempt += 1) {
    if (classifyExternalWrite(current).kind !== 'transient') break;
    const observed = await priceAfterFailure(client, authorized.order_id);
    if (observed && observed.amount === authorized.target_amount_fen) return {recovered: observed};
    await sleep(RETRY_BASE_MS * attempt);
    try {
      await client.orders.changePrice(authorized.order_id, {priceFee: authorized.target_amount_fen, transportFee: 0});
      return {};
    } catch (next) { current = next; }
  }
  return {error: current};
}

export async function executeReprice(command, client, backend, allowed, options = {}) {
  if (!allowed || !client) return {status:'GATE_REJECTED', failure_reason:'REPRICE_ALLOWLIST_OR_CLIENT_REJECTED'};
  const sleep = options.sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  let authorized = null, attempted = false, before = null;
  // The order as the platform showed it before any write. Attached to every failure so a
  // refusal can be explained later: whether the order existed, was paid, or was still
  // being created is exactly what the summary alone cannot tell us.
  const context = (extra = {}) => {
    const snapshot = orderSnapshot(before);
    return snapshot ? {platform_order: snapshot, ...extra} : {...extra};
  };
  try {
    before = await client.orders.get(command.order_id);
    const gate = await backend(`/api/v1/commands/${command.command_id}/authorize-execution`, 'POST', before);
    if (gate.status !== 200) return {status:'GATE_REJECTED', failure_reason:gate.data?.detail || 'REPRICE_AUTHORIZATION_REJECTED'};
    authorized = gate.data;
    if (authorized.order_id !== command.order_id || !Number.isSafeInteger(authorized.target_amount_fen) || authorized.target_amount_fen <= 0) {
      return {status:'GATE_REJECTED',failure_reason:'INVALID_AUTHORIZED_PRICE'};
    }
    attempted = true;
    const action = await client.orders.changePrice(authorized.order_id, {priceFee:authorized.target_amount_fen, transportFee:0});
    const after = await client.orders.get(authorized.order_id);
    const verified = String(after.orderId) === authorized.order_id && String(after.accountUnb) === authorized.shop_id && Number(after.payment) === authorized.target_amount_fen;
    return {status:verified?'SUCCEEDED':'VERIFY_FAILED', provider_result_status:verified?'succeeded':'unverified',
      price_after_fen:Number(after.payment), order_status:after.orderStatus, platform_action_ok:action?.ok ?? null};
  } catch(error) {
    // A refusal is final: the platform rejected this write, so it is reported as a
    // failure with the platform's own code, not as a result the shop must chase.
    const classification = attempted ? classifyExternalWrite(error) : {kind:'unknown', code:null};
    if (classification.kind === 'refused') {
      return {status:'FAILED', failure_reason:classification.code, readback_error:describeError(error),
        error_detail: errorDetail(error), ...context()};
    }
    if (!attempted) {
      // Nothing was written, so this is not a read-back failure and must not claim one.
      return {status:'FAILED', failure_reason:'REPRICE_PRECHECK_FAILED',
        error_detail: errorDetail(error), ...context()};
    }
    if (classification.kind === 'transient') {
      const retried = await retryQuietTransport(client, authorized, error, sleep);
      if (retried.recovered) {
        return {status:'SUCCEEDED', provider_result_status:'succeeded', recovered:'readback',
          price_after_fen:retried.recovered.amount, order_status:retried.recovered.order?.orderStatus};
      }
      if (retried.error) {
        return {status:'UNKNOWN_RESULT', failure_reason:'REPRICE_TRANSPORT_ERROR',
          readback_error:describeError(retried.error), error_detail: errorDetail(retried.error), ...context()};
      }
      // The repeat was accepted, so confirm it with the single read this path makes.
      const settled = await priceAfterFailure(client, authorized.order_id);
      return settled && settled.amount === authorized.target_amount_fen
        ? {status:'SUCCEEDED', provider_result_status:'succeeded', recovered:'retry',
           price_after_fen:settled.amount, order_status:settled.order?.orderStatus}
        : {status:'UNKNOWN_RESULT', failure_reason:'REPRICE_RESULT_UNCONFIRMED',
           readback_error:describeError(error), error_detail: errorDetail(error), ...context()};
    }
    // The write was attempted and its result is unconfirmed: the request failed and
    // the read-back could not confirm it either. Reported as unconfirmed and never
    // retried, because the write may already have landed; a second read here would
    // only replace this diagnostic with an unrelated error.
    return {status:'UNKNOWN_RESULT', failure_reason:'REPRICE_RESULT_UNCONFIRMED',
      readback_error:describeError(error), error_detail: errorDetail(error), ...context()};
  }
}
