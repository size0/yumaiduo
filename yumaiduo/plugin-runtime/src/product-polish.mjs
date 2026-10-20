// Platform adapter only; Backend owns daily scheduling and authorization.
export async function executeProductPolish(command, cfg, backend, env, timeoutMs=15000) {
  if (cfg.mode!=='live') return {status:'WOULD_SEND'};
  const failed=reason=>({status:'FAILED',failure_reason:reason});
  if (!cfg.client || command.tenant_id!==env.YUMAIDUO_TENANT_ID || !command.shop_id ||
      !(env.LIVE_SEND_GLOBAL_ENABLED==='true' || (command.shop_id===env.LIVE_SEND_ALLOWLIST_ACCOUNT_UNB && command.tenant_id===env.LIVE_SEND_ALLOWLIST_TENANT_ID)))
    return failed('POLISH_LIVE_GATE_REJECTED');
  if (typeof cfg.client.products?.polishAll!=='function') return failed('POLISH_SDK_UNAVAILABLE');
  let timer,attempted=false;
  try {
    const grant=await backend(`/api/v1/commands/${command.command_id}/authorize-polish`,'POST',{});
    if (grant.status!==200 || ['command_id','type','tenant_id','shop_id','event_id'].some(k=>grant.data?.[k]!==command[k]))
      return failed('POLISH_AUTHORIZATION_REJECTED');
    attempted=true;
    const result=await Promise.race([cfg.client.products.polishAll({accountUnb:command.shop_id}),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('TIMEOUT')),timeoutMs);})]);
    if (result?.ok===true && result.accountUnb===command.shop_id)
      return {status:'SUCCEEDED',provider_result_status:result.alreadyPolished===true?'ALREADY_COMPLETED':'PLATFORM_CONFIRMED'};
  } catch(error) {
    if (!attempted) return failed('POLISH_PRECHECK_FAILED');
    if ([401,403].includes(error?.status) || ['E_PLUGIN_DEVELOPER_APP_GATEWAY_REQUIRED','E_PLUGIN_SCOPE_DENIED'].includes(error?.code))
      return failed('POLISH_PERMISSION_REQUIRED');
  } finally {clearTimeout(timer);}
  // No automatic replay of an ambiguous write, including after restart.
  return {status:'UNKNOWN',failure_reason:'POLISH_RESULT_UNCONFIRMED'};
}
