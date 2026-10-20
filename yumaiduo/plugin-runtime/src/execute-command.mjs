import {executeFailedOrderClose} from './failed-order-close.mjs';
import {executeProductPolish} from './product-polish.mjs';
import {executeOrderEngagement} from './order-engagement.mjs';
import {sendWithHistoryCheck} from './message-history.mjs';
import {readOfficialShops} from './shops.mjs';
import {executeReprice, isRepriceAllowed} from './reprice.mjs';
import {executeTicketDelivery, isTicketDeliveryAllowed} from './ticket-delivery.mjs';
import {executeWandaFollowup} from './wanda-shipment.mjs';

// Existing transaction/send gates unchanged; persistence is outside this function.
export async function executeCommand(command,cfg,backend,env=process.env) {
  if (command.type==='CLOSE_FAILED_ORDER') return executeFailedOrderClose(command,cfg,backend,env);
  if (command.type==='POLISH_SHOP') return executeProductPolish(command,cfg,backend,env);
  if (['AUTO_REVIEW','REQUEST_RED_FLOWER'].includes(command.type))
    return executeOrderEngagement(command,cfg,backend,env);
  if (['SHIP_ORDER','TICKET_DELIVERY_NOTICE'].includes(command.type))
    return executeWandaFollowup(command,cfg.client,backend,
      command.delivery_provider==='WANDA' && isTicketDeliveryAllowed({...command,type:'DELIVER_TICKET'},cfg.mode,env));
  if (command.type==='DELIVER_TICKET')
    return executeTicketDelivery(command,cfg.client,backend,isTicketDeliveryAllowed(command,cfg.mode,env));
  if (command.type==='CHANGE_ORDER_PRICE') {
    let shops=[];
    try { shops=await readOfficialShops(cfg.client,env.YUMAIDUO_TENANT_ID,true); } catch {}
    const allowed=isRepriceAllowed(command,cfg.mode,env,shops);
    return executeReprice(command,cfg.client,backend,allowed);
  }
  let result={status:'WOULD_SEND',provider_result_status:'shadow'};
  const allowLive=command.type==='SEND_MESSAGE' && (env.LIVE_SEND_GLOBAL_ENABLED==='true' ||
    (command.shop_id===env.LIVE_SEND_ALLOWLIST_ACCOUNT_UNB && command.tenant_id===env.LIVE_SEND_ALLOWLIST_TENANT_ID));
  if (cfg.mode==='live' && allowLive) {
    if (!cfg.client) result={status:'FAILED',provider_result_status:'failed',failure_reason:'OFFICIAL_CLIENT_UNAVAILABLE'};
    else try { result=await sendWithHistoryCheck(command,cfg.client,backend); }
    catch(error) { result={status:'UNKNOWN',provider_result_status:'failed',failure_reason:String(error?.message||error)}; }
  } else if (cfg.mode==='live' && !allowLive) {
    result={status:'FAILED',provider_result_status:'LIVE_CAPABILITY_GATE_REJECTED',failure_reason:'LIVE_SEND_ALLOWLIST_OR_TYPE_REJECTED'};
  }
  return result;
}
