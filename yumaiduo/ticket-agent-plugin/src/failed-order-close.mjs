// The backend authorizes only terminal supplier failure. Never replay a cancel write.
async function bounded(fn, ms) {
  let timer;
  try { return await Promise.race([Promise.resolve().then(fn),new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new Error('CLOSE_TIMEOUT')),ms);
  })]); } finally { clearTimeout(timer); }
}
export async function executeFailedOrderClose(command,cfg,backend,env,timeoutMs=15000) {
  const fail=reason=>({status:'FAILED',failure_reason:reason});
  if(cfg.mode!=='live')return {status:'WOULD_SEND'};
  if(!cfg.client || command.tenant_id!==env.YUMAIDUO_TENANT_ID ||
    !(env.LIVE_SEND_GLOBAL_ENABLED==='true' || (command.shop_id===env.LIVE_SEND_ALLOWLIST_ACCOUNT_UNB && command.tenant_id===env.LIVE_SEND_ALLOWLIST_TENANT_ID)))
    return fail('CLOSE_LIVE_GATE_REJECTED');
  const read=()=>bounded(()=>cfg.client.orders.get(command.order_id),timeoutMs);
  const same=d=>d?.orderId===command.order_id && String(d.accountUnb)===command.shop_id;
  let attempted=false;
  try {
    const detail=await read();
    if(!same(detail))return fail('CLOSE_ORDER_IDENTITY_MISMATCH');
    // Paid but not shipped only; never close shipped/finished/unknown orders.
    if(![2,6].includes(detail.orderStatus))return fail('CLOSE_ORDER_STATUS_NOT_ELIGIBLE');
    const grant=await backend(`/api/v1/commands/${command.command_id}/authorize-close`,'POST',{});
    if(grant.status!==200 || ['command_id','type','tenant_id','shop_id','order_id','booking_order_id','text'].some(k=>grant.data?.[k]!==command[k]))
      return fail('CLOSE_AUTHORIZATION_REJECTED');
    if(detail.orderStatus===6)return {status:'SUCCEEDED',provider_result_status:'ALREADY_COMPLETED'};
    attempted=true;
    await bounded(()=>cfg.client.orders.cancel(command.order_id,{reason:command.text}),timeoutMs);
  } catch {
    if(!attempted)return fail('CLOSE_PRECHECK_FAILED');
  }
  try {
    const current=await read();
    if(same(current) && current.orderStatus===6)return {status:'SUCCEEDED',provider_result_status:'READBACK_CONFIRMED'};
  } catch {}
  return {status:'UNKNOWN',failure_reason:'CLOSE_RESULT_UNCONFIRMED'};
}
