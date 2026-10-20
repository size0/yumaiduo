import test from 'node:test';
import assert from 'node:assert/strict';
import {classifyExternalWrite, rejectionCode} from '../src/execution-result.mjs';
import {executeReprice, isRepriceAllowed, describeError, orderSnapshot, errorDetail} from '../src/reprice.mjs';

// A PluginApiError-shaped failure whose refusal code sits in `message`, exactly as
// the upstream wrapper reports it.
const wrapped = (errorCode, message) => Object.assign(new Error(message), {name: 'PluginApiError', status: 400, errorCode});

const ORDER = '5127744384643018944';
const SHOP = '2313315754';
const TARGET = 6460;

// A fake official client: `payment` is the order amount, and only a landed write
// moves it, so a read-back is meaningful. `throwOnRead` makes the read that follows
// the write fail, which is the real "response lost after the write" shape.
function harness({before = 2000, failOnce = null, failAlways = null, throwOnRead = null} = {}) {
  const calls = {change: 0, get: 0};
  let payment = before;
  const client = {
    orders: {
      async get() {
        calls.get += 1;
        if (throwOnRead && calls.get > 1) throw throwOnRead;
        return {orderId: ORDER, accountUnb: SHOP, payment, orderStatus: 1};
      },
      async changePrice(_id, request) {
        calls.change += 1;
        if (calls.change === 1 && failOnce) throw failOnce;
        if (failAlways) throw failAlways;
        payment = request.priceFee;
        return {ok: true};
      },
    },
  };
  const backend = async () => ({status: 200, data: {order_id: ORDER, shop_id: SHOP, target_amount_fen: TARGET}});
  return {client, backend, calls, seen: () => payment};
}

test('a refusal is taken from the documented errorCode, not from message text', () => {
  // The guide's envelope carries the platform's business code in `errorCode`
  // (PluginApiError exposes it as code / errorCode). A word inside a human-readable
  // message is not a refusal the platform named.
  assert.deepEqual(classifyExternalWrite(wrapped('CANNOT_MODIFY_FEE', 'CANNOT_MODIFY_FEE')),
    {kind: 'refused', code: 'CANNOT_MODIFY_FEE'});
  assert.deepEqual(classifyExternalWrite(wrapped('ORDER_CLOSED', 'order is closed')),
    {kind: 'refused', code: 'ORDER_CLOSED'});
  // Production shape: errorCode is an upstream flow failure, the word only appears
  // in the message, so nothing proves the write was rejected.
  assert.deepEqual(classifyExternalWrite(wrapped('E_OAUTH_FLOW_FAILED', 'CANNOT_MODIFY_FEE')),
    {kind: 'unknown', code: null});
});

test('an upstream flow failure that names no refusal stays unknown', () => {
  // E_OAUTH_FLOW_FAILED only proves the attempt did not succeed; it never proves the
  // platform rejected the write, so it must not be reported as a final failure.
  const error = wrapped('E_OAUTH_FLOW_FAILED', 'oauth flow could not complete');
  assert.equal(rejectionCode(error), null);
  assert.deepEqual(classifyExternalWrite(error), {kind: 'unknown', code: null});
  assert.equal(classifyExternalWrite(Object.assign(new Error('boom'), {status: 400, code: 'E_OAUTH_FLOW_FAILED'})).kind, 'unknown');
});

test('classify keeps refusal, transient and unknown apart', () => {
  assert.equal(classifyExternalWrite(wrapped('ORDER_CLOSED', 'CLOSED')).kind, 'refused');
  assert.equal(classifyExternalWrite(wrapped('CANNOT_MODIFY_FEE', 'x')).kind, 'refused');
  assert.equal(classifyExternalWrite(wrapped('E_PLUGIN_RATE_LIMITED', 'too many')).kind, 'transient');
  assert.equal(classifyExternalWrite(Object.assign(new Error('429'), {status: 429})).kind, 'transient');
  assert.equal(classifyExternalWrite(wrapped('E_OAUTH_FLOW_FAILED', 'gateway said no')).kind, 'unknown');
  assert.equal(classifyExternalWrite(null).kind, 'unknown');
});

test('a retryable transport code is read from its own field, not from prose', () => {
  assert.equal(classifyExternalWrite(wrapped('E_PLUGIN_RATE_LIMITED', 'too many')).kind, 'transient');
  // A code merely mentioned inside a message is not the platform's code for this call.
  const prose = wrapped('E_OAUTH_FLOW_FAILED', 'upstream said E_PLUGIN_RATE_LIMITED, slow down');
  assert.equal(classifyExternalWrite(prose).kind, 'unknown');
});

test('a rate-limited reprice is transient and therefore retryable', () => {
  const error = wrapped('E_PLUGIN_RATE_LIMITED', 'slow down');
  assert.deepEqual(classifyExternalWrite(error), {kind: 'transient', code: 'E_PLUGIN_RATE_LIMITED'});
});

test('credit exhaustion and platform risk control are never rate-limit retries', () => {
  for (const code of ['E_DEVELOPER_API_CREDIT_INSUFFICIENT', 'E_XIANYU_APP_GATEWAY_RISK_CONTROL']) {
    const error = Object.assign(new Error('requires operator action'), {status: 429, code});
    assert.equal(classifyExternalWrite(error).kind, 'unknown');
  }
});

test('a refused reprice is reported as FAILED with the platform code and never retried', async () => {
  const error = wrapped('CANNOT_MODIFY_FEE', 'cannot modify fee');
  const h = harness({failAlways: error});
  const result = await executeReprice({order_id: ORDER}, h.client, h.backend, true, {sleep: async () => {}});
  assert.equal(result.status, 'FAILED');
  assert.equal(result.failure_reason, 'CANNOT_MODIFY_FEE');
  assert.equal(h.calls.change, 1);                     // exactly one write, no repeat
  assert.match(result.readback_error, /errorCode=CANNOT_MODIFY_FEE/);
});

test('a rate-limited reprice retries once and succeeds when the amount was unchanged', async () => {
  const error = wrapped('E_PLUGIN_RATE_LIMITED', 'slow down');
  const h = harness({failOnce: error});
  const result = await executeReprice({order_id: ORDER}, h.client, h.backend, true, {sleep: async () => {}});
  assert.equal(result.status, 'SUCCEEDED');
  assert.equal(h.calls.change, 2);
  assert.equal(h.seen(), TARGET);
});

test('an unknown failure stays unconfirmed, is not retried, and keeps the original error', async () => {
  const error = wrapped('E_OAUTH_FLOW_FAILED', 'socket hang up');
  const h = harness({throwOnRead: error});
  const result = await executeReprice({order_id: ORDER}, h.client, h.backend, true, {sleep: async () => {}});
  assert.equal(result.status, 'UNKNOWN_RESULT');
  assert.equal(result.failure_reason, 'REPRICE_RESULT_UNCONFIRMED');
  assert.equal(h.calls.change, 1);                      // exactly one write
  assert.equal(h.calls.get, 2);                         // the precheck and the failed read-back, no more
  assert.match(result.readback_error, /E_OAUTH_FLOW_FAILED/);
});

test('an unknown failure with the amount unchanged stays unconfirmed and is not retried', async () => {
  const error = wrapped('E_OAUTH_FLOW_FAILED', 'socket hang up');
  const h = harness({failAlways: error});
  const result = await executeReprice({order_id: ORDER}, h.client, h.backend, true, {sleep: async () => {}});
  assert.equal(result.status, 'UNKNOWN_RESULT');
  assert.equal(result.failure_reason, 'REPRICE_RESULT_UNCONFIRMED');
  assert.equal(h.calls.change, 1);
});

test('a transient failure that keeps failing ends unconfirmed with the transport reason', async () => {
  const error = wrapped('E_PLUGIN_RATE_LIMITED', 'slow down');
  const h = harness({failAlways: error, throwOnRead: error});
  const result = await executeReprice({order_id: ORDER}, h.client, h.backend, true, {sleep: async () => {}});
  assert.equal(result.status, 'UNKNOWN_RESULT');
  assert.equal(result.failure_reason, 'REPRICE_TRANSPORT_ERROR');
  assert.equal(h.calls.change, 3);                      // the first write plus two bounded retries
});

test('a successful reprice reports the authorised amount as verified', async () => {
  const h = harness();
  const result = await executeReprice({order_id: ORDER}, h.client, h.backend, true, {sleep: async () => {}});
  assert.equal(result.status, 'SUCCEEDED');
  assert.equal(result.price_after_fen, TARGET);
  assert.equal(result.platform_action_ok, true);
});

test('a mismatch after the write is still reported as unverified', async () => {
  const h = harness();
  const client = {...h.client, orders: {...h.client.orders, changePrice: async () => ({ok: true})}};
  const result = await executeReprice({order_id: ORDER}, client, h.backend, true, {sleep: async () => {}});
  assert.equal(result.status, 'VERIFY_FAILED');
  assert.equal(result.price_after_fen, 2000);
});

test('a rejected authorization never writes', async () => {
  const h = harness();
  const backend = async () => ({status: 409, data: {detail: 'REPRICE_AUTHORIZATION_REJECTED'}});
  const result = await executeReprice({order_id: ORDER}, h.client, backend, true, {sleep: async () => {}});
  assert.equal(result.status, 'GATE_REJECTED');
  assert.equal(h.calls.change, 0);
});

test('a business denial preserves the backend reason and never writes', async () => {
  const h = harness();
  const backend = async () => ({status: 200, data: {allowed: false, reason: 'ORDER_NOT_REPRICEABLE:2'}});
  const result = await executeReprice({order_id: ORDER}, h.client, backend, true);
  assert.equal(result.status, 'GATE_REJECTED');
  assert.equal(result.failure_reason, 'ORDER_NOT_REPRICEABLE:2');
  assert.equal(h.calls.change, 0);
});

test('the gate still refuses a command that is not allowlisted', async () => {
  const h = harness();
  const result = await executeReprice({order_id: ORDER}, h.client, h.backend, false, {sleep: async () => {}});
  assert.equal(result.status, 'GATE_REJECTED');
  assert.equal(h.calls.change, 0);
});

test('the allowlist gate requires the shop directory entry', () => {
  const command = {type: 'CHANGE_ORDER_PRICE', tenant_id: '107', shop_id: SHOP};
  const env = {LIVE_REPRICE_ENABLED: 'true', YUMAIDUO_TENANT_ID: '107'};
  assert.equal(isRepriceAllowed(command, 'live', env, [{tenant_id: '107', shop_id: SHOP, platform_enabled: true}]), true);
  assert.equal(isRepriceAllowed(command, 'live', env, [{tenant_id: '107', shop_id: SHOP, platform_enabled: false}]), false);
  assert.equal(isRepriceAllowed(command, 'live', env, []), false);
});

test('describeError stays bounded and carries no credentials', () => {
  const text = describeError(wrapped('E_OAUTH_FLOW_FAILED', 'x'.repeat(500)));
  assert.match(text, /name=PluginApiError/);
  assert.match(text, /status=400/);
  assert.ok(text.length < 500);
  assert.equal(describeError(null), 'none');
});

test('the order snapshot keeps the state facts and drops identity and titles', () => {
  const snapshot = orderSnapshot({
    orderId: 'O1', accountUnb: '2313315754', buyerNick: 'someone', productTitle: 'W+会员什么价我什么价',
    orderStatus: 1, orderStatusText: '待付款', payment: '2000', postFee: '0', quantity: 2,
    payTime: null, createTime: '2026-10-07T01:53:00.000Z', grouponPending: false, itemId: '999785374109'});
  assert.equal(snapshot.orderStatus, 1);
  assert.equal(snapshot.payment, '2000');
  assert.equal(snapshot.createTime, '2026-10-07T01:53:00.000Z');
  assert.equal(snapshot.orderId, undefined, 'the order id is already on the command');
  assert.equal(snapshot.accountUnb, undefined, 'the account stays out of the diagnostic');
  assert.equal(snapshot.buyerNick, undefined);
  assert.equal(snapshot.productTitle, undefined);
  assert.equal(orderSnapshot(null), null);
});

test('the error detail is bounded and never carries credentials', () => {
  const error = Object.assign(new Error('CANNOT_MODIFY_FEE'), {
    name: 'PluginApiError', status: 400, errorCode: 'E_OAUTH_FLOW_FAILED',
    body: {traceId: 'abc', token: 'sk-secret', headers: {authorization: 'Bearer xyz'},
           nested: {deep: {deeper: {deepest: 'x'}}}, list: Array.from({length: 20}, (_, i) => i)}});
  const detail = errorDetail(error);
  assert.equal(detail.status, 400);
  assert.equal(detail.errorCode, 'E_OAUTH_FLOW_FAILED');
  assert.equal(detail.body.traceId, 'abc');
  assert.ok(detail.body.list.length <= 5, 'a list must be bounded');
  // Asserted on the serialised diagnostic: no credential value may survive anywhere.
  const text = JSON.stringify(detail);
  for (const secret of ['sk-secret', 'Bearer', 'authorization', 'token']) {
    assert.equal(text.includes(secret), false, `${secret} leaked into the diagnostic`);
  }
  assert.equal(errorDetail(null), null);
});

test('a refused reprice reports the order it read and the error as sent', async () => {
  const error = Object.assign(new Error('cannot modify fee'), {
    name: 'PluginApiError', status: 400, errorCode: 'CANNOT_MODIFY_FEE', body: {traceId: 't1'}});
  const h = harness({failAlways: error});
  const result = await executeReprice({order_id: ORDER}, h.client, h.backend, true, {sleep: async () => {}});
  assert.equal(result.status, 'FAILED');
  assert.equal(result.platform_order.payment, 2000, 'the order as the platform showed it');
  assert.equal(result.platform_order.orderStatus, 1);
  assert.equal(result.error_detail.body.traceId, 't1');
});


test('captured HTTP400 gateway envelope preserves exact platform refusal without retry', async () => {
  const error=Object.assign(new Error('CANNOT_MODIFY_FEE'), {
    name:'PluginApiError', status:400, errorCode:'E_OAUTH_FLOW_FAILED',
    body:{code:400,message:'CANNOT_MODIFY_FEE',errorCode:'E_OAUTH_FLOW_FAILED',traceId:'audit-fixture'}});
  assert.deepEqual(classifyExternalWrite(error), {kind:'refused',code:'CANNOT_MODIFY_FEE'});
  const h=harness({failAlways:error});
  const result=await executeReprice({order_id:ORDER},h.client,h.backend,true,{sleep:async()=>{}});
  assert.equal(result.status,'FAILED');
  assert.equal(result.failure_reason,'CANNOT_MODIFY_FEE');
  assert.equal(h.calls.change,1);
  for(const overrides of [
    {status:503}, {body:{...error.body,message:'upstream said CANNOT_MODIFY_FEE'}},
    {body:{...error.body,errorCode:'OTHER_GATEWAY'}},
    {body:{...error.body,code:500}},
  ]) assert.equal(classifyExternalWrite(Object.assign(new Error(error.message),error,overrides)).kind,'unknown');
});
