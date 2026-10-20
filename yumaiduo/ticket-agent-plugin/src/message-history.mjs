import {rejectionCode, trustedMessageId} from './execution-result.mjs';
import {sendReplyMessage} from './reply-image.mjs';

function sendExceptionCode(error, refused) {
  const code = error?.errorCode || error?.code;
  // Do not persist arbitrary SDK messages, URLs, credentials or buyer content.
  return refused || (typeof code === 'string' &&
    (/^E_IM_[A-Z0-9_]{1,64}$/.test(code) ||
     ['ETIMEDOUT','ECONNRESET','ECONNREFUSED','ABORT_ERR'].includes(code))
    ? code : 'UNCLASSIFIED');
}
// Read only: the platform does not emit outbound-message webhooks.
export async function readOfficialMessages(body, client, tenantId, verified) {
  if (verified !== true || !tenantId || body?.tenant_id !== tenantId || !client)
    throw new Error('IM_HISTORY_UNAUTHORIZED');
  for (const key of ['shop_id','chat_id','buyer_id']) {
    if (typeof body[key] !== 'string' || !body[key]) throw new Error('IM_HISTORY_IDENTITY_REQUIRED');
  }
  const page = await client.im.listMessages({accountUnb:body.shop_id,chatId:body.chat_id,pageSize:100});
  if (!Array.isArray(page?.items)) throw new Error('IM_HISTORY_INVALID');
  return page.items;
}

export async function sendWithHistoryCheck(command, client, backend) {
  if (!Array.isArray(command.history_message_ids))
    return {status:'SUPPRESSED',failure_reason:'MESSAGE_HISTORY_SNAPSHOT_MISSING'};
  let check;
  try {
    check = await backend(`/api/v1/commands/${command.command_id}/authorize-message-send`,'POST',{});
  } catch (error) {
    // Authorization may already have moved CLAIMED to SENDING. We know no send
    // was attempted; FAILED is legal in both states, unlike SUPPRESSED.
    if (error?.code==='BACKEND_REQUEST_TIMEOUT')
      return {status:'FAILED',provider_result_status:'not_sent',failure_reason:'MESSAGE_AUTHORIZATION_TIMEOUT'};
    return {status:'SUPPRESSED',provider_result_status:'not_sent',failure_reason:'IM_HISTORY_CHECK_FAILED'};
  }
  if (check.status !== 200 || check.data?.allowed !== true)
    return {status:'SUPPRESSED',provider_result_status:'not_sent',
      failure_reason:check.data?.reason || 'IM_HISTORY_CHECK_FAILED'};
  try {
    const sent = await sendReplyMessage(command, client);
    const id = trustedMessageId(sent);
    const rawId = sent?.messageId || sent?.message_id;
    return id ? {status:'SENT',sent_message_id:id,provider_result_status:'succeeded'}
      : {status:'UNKNOWN',failure_reason:'SEND_RESULT_UNCONFIRMED',
          provider_result_status: !rawId ? 'SEND_RESPONSE_MISSING_ID'
            : typeof rawId === 'string' && rawId.startsWith('local-')
              ? 'SEND_RESPONSE_LOCAL_ID' : 'SEND_RESPONSE_INVALID_ID'};
  } catch (error) {
    const refused = rejectionCode(error);
    return {status:refused ? 'FAILED' : 'UNKNOWN',
      failure_reason:refused || 'SEND_RESULT_UNCONFIRMED',
      provider_result_status:`SEND_EXCEPTION:${sendExceptionCode(error, refused)}`};
  }
}
