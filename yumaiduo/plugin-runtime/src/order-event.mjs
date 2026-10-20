// Call only after webhook signature verification. Never trust attached facts.
export async function readOfficialOrder(body, client, tenantId, verified) {
  if (verified !== true || !tenantId || body?.tenant_id !== tenantId) throw new Error('ORDER_READ_UNAUTHORIZED');
  if (typeof body.order_id !== 'string' || !/^[0-9]+$/.test(body.order_id)) throw new Error('ORDER_READ_ID_INVALID');
  if (!client) throw new Error('OFFICIAL_ORDER_CLIENT_UNAVAILABLE');
  const detail = await client.orders.get(body.order_id);
  if (!detail || detail.orderId !== body.order_id) throw new Error('OFFICIAL_ORDER_ID_MISMATCH');
  return Object.fromEntries(['orderId','accountUnb','orderStatus','payment','postFee','payTime','grouponPending','quantity']
    .filter(key => detail[key] !== undefined).map(key => [key,detail[key]]));
}

export async function attachOrderDetail(event, client, tenantId) {
  delete event._order_detail;
  delete event._order_verified;
  const body = event.payload || {};
  // ★★ 2026-10-14：这里【故意不】加 order.price.changed。
  //
  //   我一度加上了它，方向是错的 —— 事后查 V5 线上插件才知道：
  //     · V5 的 subscribes 里【没有】order.price.changed（V5 把它删了）
  //     · V4 曾经订阅过它，但事件处理器里【根本没有这个分支】
  //       （src/runtime/event-processor.mjs 的 ORDER_EVENT_TYPES 不含它）
  //       ⇒ 那是个【死订阅】：订阅了却没人处理
  //     · V5 判"改价成功"的唯一判据是 orders.get 回查金额
  //       （reprice.mjs:145-147：回查 payment == 授权金额 → SUCCEEDED）
  //     · 成功信号的载体是【命令结果回传】POST /commands/{id}/result，
  //       不是事件。bubble 18 由 CHANGE_ORDER_PRICE.status==='SUCCEEDED' 触发
  //   ⇒ 走事件这条路会变成"两个信号源"，状态会漂移。
  //     照 V5 的做法走命令回传，这里保持原样。
  const needsDetail = event.event === 'order.paid' ||
    (event.event === 'im.message.received' && String(body.messageType).trim() === '26');
  if (!needsDetail) return;
  if (!tenantId || String(event.tenantId) !== String(tenantId)) throw new Error('ORDER_EVENT_TENANT_MISMATCH');
  if (!body.orderId || typeof body.orderId !== 'string') throw new Error('ORDER_EVENT_ID_REQUIRED');
  if (!client) throw new Error('OFFICIAL_ORDER_CLIENT_UNAVAILABLE');
  event._order_detail = await client.orders.get(body.orderId);
}
