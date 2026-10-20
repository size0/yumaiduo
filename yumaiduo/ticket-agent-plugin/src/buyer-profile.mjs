export async function readBuyerProfile(body, client, tenantId) {
  if (!tenantId || body.tenant_id !== tenantId || !body.shop_id || !body.buyer_id)
    throw new Error('BUYER_SCOPE_REQUIRED');
  const session = await client.im.getSessionByPeer(body.shop_id,body.buyer_id);
  if (!session || String(session.accountUnb)!==body.shop_id || String(session.peerUnb)!==body.buyer_id)
    throw new Error('BUYER_IDENTITY_MISMATCH');
  return {shop_id:body.shop_id,buyer_id:body.buyer_id,buyer_name:session.peerNick || ''};
}
