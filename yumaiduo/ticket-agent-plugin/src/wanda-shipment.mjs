import {sendReplyMessage, validateReplyImage} from './reply-image.mjs';
import {trustedMessageId} from './execution-result.mjs';

// Each command performs at most one external write; ExecutionJournal owns crash recovery.
export async function executeWandaFollowup(command, client, backend, allowed) {
  if (!allowed || !client) return {status:'GATE_REJECTED'};
  let token, attempted=false;
  try {
    const before=await client.orders.get(command.order_id);
    const session=await client.im.getSessionByOrder(command.order_id);
    const gate=await backend(`/api/v1/commands/${command.command_id}/authorize-ticket-delivery`,'POST',{detail:before,session});
    if (gate.status!==200) return {status:'GATE_REJECTED'};
    const grant=gate.data;token=grant?.execution_token;
    if (!token || ['command_id','type','order_id','tenant_id','shop_id','buyer_id','chat_id'].some(k=>grant[k]!==command[k]))
      return {status:'GATE_REJECTED',...(token?{execution_token:token}:{})};
    if (command.type==='TICKET_DELIVERY_NOTICE') {
      if (grant.image) {
        if (grant.text?.trim()) return {status:'GATE_REJECTED',execution_token:token};
        validateReplyImage(grant.image);
      } else if (typeof grant.text!=='string' || !grant.text.trim()) return {status:'GATE_REJECTED',execution_token:token};
      attempted=true;
      const message=trustedMessageId(await sendReplyMessage(grant,client));
      return message?{status:'SENT',execution_token:token,sent_message_id:message}:{status:'UNKNOWN',execution_token:token};
    }
    if (command.type!=='SHIP_ORDER') return {status:'GATE_REJECTED',execution_token:token};
    let after=before;
    if (![3,4].includes(before.orderStatus)) {
      if (before.orderStatus!==2) return {status:'GATE_REJECTED',execution_token:token};
      attempted=true;
      await client.orders.fakeShip(grant.order_id);
      after=await client.orders.get(grant.order_id);
    }
    if (after.orderId!==grant.order_id || after.accountUnb!==grant.shop_id || ![3,4].includes(after.orderStatus))
      return {status:'UNKNOWN',execution_token:token};
    const keys=['orderId','accountUnb','orderStatus','payment','quantity','postFee','payTime','grouponPending'];
    return {status:'SUCCEEDED',execution_token:token,detail:Object.fromEntries(keys.filter(k=>after[k]!==undefined).map(k=>[k,after[k]]))};
  } catch {
    return {status:attempted?'UNKNOWN':'GATE_REJECTED',...(token?{execution_token:token}:{})};
  }
}
