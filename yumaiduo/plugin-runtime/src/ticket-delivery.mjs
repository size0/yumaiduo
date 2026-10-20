import {sendReplyMessage, validateReplyImage} from './reply-image.mjs';

export function isTicketDeliveryAllowed(command, mode, env) {
  return command.type === 'DELIVER_TICKET' && mode === 'live' &&
    Boolean(env.YUMAIDUO_TENANT_ID) && command.tenant_id === env.YUMAIDUO_TENANT_ID &&
    (env.LIVE_SEND_GLOBAL_ENABLED === 'true' ||
      (command.tenant_id === env.LIVE_SEND_ALLOWLIST_TENANT_ID &&
       command.shop_id === env.LIVE_SEND_ALLOWLIST_ACCOUNT_UNB));
}

export async function executeTicketDelivery(command, client, backend, allowed) {
  if (!allowed || !client) return {status:'GATE_REJECTED'};
  let token, attempted = false;
  try {
    const detail = await client.orders.get(command.order_id);
    const session = await client.im.getSessionByOrder(command.order_id);
    const gate = await backend(`/api/v1/commands/${command.command_id}/authorize-ticket-delivery`,
      'POST', {detail,session});
    if (gate.status !== 200) return {status:'GATE_REJECTED'};
    const grant = gate.data;
    token = grant?.execution_token;
    if (!token || (grant.image ? Boolean(grant.text?.trim()) : typeof grant.text !== 'string' || !grant.text.trim()) ||
        ['command_id','order_id','tenant_id','shop_id','buyer_id','chat_id','ticket_version']
          .some(key => grant[key] !== command[key])) {
      return {status:'GATE_REJECTED', ...(token ? {execution_token:token} : {})};
    }
    if (grant.image) validateReplyImage(grant.image);
    attempted = true;
    const result = await sendReplyMessage(grant,client);
    const messageId = result?.messageId || result?.message_id;
    return typeof messageId === 'string' && messageId.trim() && !messageId.startsWith('local-')
      ? {status:'SENT',sent_message_id:messageId,execution_token:token}
      : {status:'UNKNOWN',execution_token:token};
  } catch {
    // No external send retry, and no private URL/text in error payloads.
    return {status:attempted?'UNKNOWN':'GATE_REJECTED', ...(token ? {execution_token:token} : {})};
  }
}
