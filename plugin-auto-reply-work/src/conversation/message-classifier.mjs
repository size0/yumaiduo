export function isBareAcknowledgement(value) {
  return /^(?:ok|okay|好(?:的)?|知道了|明白了|收到|嗯+|谢谢(?:了)?|行)\s*[!！。.]?$/iu.test(String(value ?? '').trim());
}

export function isCurrentQuoteQuestion(value) {
  const content = String(value ?? '').replace(/\s+/gu, '').trim();
  return /^(?:(?:你这|这个|这边|那这个)?(?:多少钱|多少|什么价)|这个呢|现在多少钱|价格(?:呢|多少)?|会员价(?:可以)?优惠吗|[WwＷｗ][+＋](?:价格|优惠)?(?:呢|吗)?)[？?]?$/u.test(content)
    || /^(?:不是|不是说|怎么不是)\d+(?:\.\d{1,2})?(?:元|块)?(?:吗|嘛)?[？?]?$/u.test(content)
    || /^(?:那我|我)?(?:就是)?直接.{0,20}(?:拍下|下单)(?:是吧|对吧|吗|么)?[？?]?$/u.test(content);
}

export function isQuotePurchaseIntent(value) {
  return /^(?:下单|改价|待付款|我?已?拍(?:了|下)?)(?:\s*[，,。！!]?\s*(?:待付款|改价))?\s*[!！。.]?$/u.test(String(value ?? '').trim());
}

export function isQuoteConfirmation(value) {
  const content = String(value ?? '').trim();
  if (/^(?:ok|okay|好(?:的)?|对(?:的)?|可以|确认|没问题|行|嗯+)\s*[!！。.]?$/iu.test(content)) return true;
  return isQuotePurchaseIntent(content);
}

export function isExplicitTypedSeatChoice(value) {
  const text = String(value ?? '').trim();
  if (!text || /[？?吗么呢]/u.test(text)) return false;
  return /^(?:好的?[，,、\s]*要?\s*)?\d{1,2}\s*排\s*\d{1,2}\s*(?:座|号)?[。.!！]?$/u.test(text)
    || /\d{1,2}\s*排\s*\d{1,2}\s*(?:座|号)/u.test(text)
    || /\d{1,2}\s*排[\d\s、,，.．-]{2,}(?:座|号)/u.test(text);
}

export function isSeatClarificationQuestion(value) {
  const text = String(value ?? '').trim();
  return /(?:座位|位置|第\s*\d{1,2}\s*(?:排|行)|中间)/u.test(text) && /(?:吗|么|？|\?)$/u.test(text);
}
