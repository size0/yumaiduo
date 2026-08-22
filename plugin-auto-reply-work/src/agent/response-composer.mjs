const UNSAFE_TRANSACTION_CLAIMS = /(?:已(?:锁座|改价|出票|发货|退款)|(?:可以|请|现在).{0,8}付款|保证(?:有票|出票)|(?:有票|能买|可购买)|(?:订单)?(?:金额|价格).{0,8}(?:修改|改成)|(?:正在|马上|立即|为您).{0,16}(?:核对|核验|查询|查).{0,12}(?:票价|价格|库存|余票)|稍后.{0,12}(?:报价|报给您|告诉您价格))/u;
const MONEY_CLAIMS = /(?:\d+(?:\.\d{1,2})?\s*元|优惠价|会员价|合计|总价)/u;
const UNRESOLVED_TEMPLATE_PLACEHOLDER = /(?:\{[^{}\r\n]{1,40}\}|｛[^｛｝\r\n]{1,40}｝|\$\{[^{}\r\n]{1,40}\})/u;

function clean(value, maxLength = 500) {
  return String(value ?? '').trim().slice(0, maxLength);
}

export function hasUnresolvedReplyPlaceholder(value) {
  return UNRESOLVED_TEMPLATE_PLACEHOLDER.test(String(value ?? ''));
}

export function composeAgentReply({ plan, observation = null, hasVerifiedQuote = false } = {}) {
  const authoritative = clean(observation?.authoritative_reply);
  if (authoritative) return hasUnresolvedReplyPlaceholder(authoritative) ? null : authoritative;
  const reply = clean(plan?.reply);
  if (!reply || hasUnresolvedReplyPlaceholder(reply)) return null;
  if (UNSAFE_TRANSACTION_CLAIMS.test(reply)) return null;
  if (!hasVerifiedQuote && MONEY_CLAIMS.test(reply)) return null;
  return reply;
}
