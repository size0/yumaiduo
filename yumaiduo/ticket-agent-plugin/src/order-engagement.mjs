async function bounded(call, timeoutMs) {
  let timer;
  try { return await Promise.race([Promise.resolve().then(call), new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new Error('ENGAGEMENT_TIMEOUT')),timeoutMs);
  })]); } finally {clearTimeout(timer);}
}
// Platform execution only. Backend owns activation, scheduling and durable commands.
export async function executeOrderEngagement(command, cfg, backend, env, timeoutMs=15000) {
  const failure = reason => ({status:'FAILED',failure_reason:reason});
  if (cfg.mode !== 'live') return {status:'WOULD_SEND'};
  if (!cfg.client || command.tenant_id !== env.YUMAIDUO_TENANT_ID ||
      !(env.LIVE_SEND_GLOBAL_ENABLED === 'true' ||
        (command.shop_id === env.LIVE_SEND_ALLOWLIST_ACCOUNT_UNB && command.tenant_id === env.LIVE_SEND_ALLOWLIST_TENANT_ID)))
    return failure('ENGAGEMENT_LIVE_GATE_REJECTED');
  let attempted=false;
  const review=command.type==='AUTO_REVIEW';
  const orders=cfg.client.orders;
  const done=rates=>review ? rates?.hasSellerMainRate === true : rates?.redFlowerRequested === true;
  try {
    const detail=await bounded(()=>orders.get(command.order_id),timeoutMs);
    if (detail?.orderId!==command.order_id || String(detail.accountUnb)!==command.shop_id)
      return failure('ORDER_IDENTITY_MISMATCH');
    if (!(review ? detail.orderStatus===4 : [3,4].includes(detail.orderStatus)))
      return failure('ORDER_STATUS_NOT_ELIGIBLE');
    if (review && (typeof command.text!=='string' || !command.text.trim() || [...command.text].length>500))
      return failure('REVIEW_TEXT_INVALID');
    const rates=await bounded(()=>orders.getRates(command.order_id),timeoutMs);
    if (!rates || rates.orderId!==command.order_id || typeof rates.hasSellerMainRate!=='boolean' || typeof rates.redFlowerRequested!=='boolean')
      return failure('ORDER_RATES_UNVERIFIED');
    const grant=await backend(`/api/v1/commands/${command.command_id}/authorize-engagement`,'POST',{});
    if (grant.status!==200 || ['command_id','type','tenant_id','shop_id','order_id','text'].some(k=>grant.data?.[k]!==command[k]))
      return failure('ENGAGEMENT_AUTHORIZATION_REJECTED');
    if (done(rates)) return {status:'SUCCEEDED',provider_result_status:'ALREADY_COMPLETED'};
    attempted=true;
    const result=review
      ? await bounded(()=>orders.createRate(command.order_id,{rate:1,rateType:0,feedback:command.text}),timeoutMs)
      : await bounded(()=>orders.requestRedFlower(command.order_id,{idempotencyKey:command.dedupe_key}),timeoutMs);
    if (review ? result?.ok===true && result.successCount===1 && Array.isArray(result.failures) && result.failures.length===0
               : result?.ok===true)
      return {status:'SUCCEEDED',provider_result_status:'PLATFORM_CONFIRMED'};
  } catch {
    if (!attempted) return failure('ENGAGEMENT_PRECHECK_FAILED');
  }
  // A failed/ambiguous write is never repeated. Only inspect its original result.
  try {
    const rates=await bounded(()=>orders.getRates(command.order_id),timeoutMs);
    if (rates?.orderId===command.order_id && done(rates))
      return {status:'SUCCEEDED',provider_result_status:'READBACK_CONFIRMED'};
  } catch {}
  return {status:'UNKNOWN',failure_reason:'ENGAGEMENT_RESULT_UNCONFIRMED'};
}
