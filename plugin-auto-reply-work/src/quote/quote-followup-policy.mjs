import { isCurrentQuoteQuestion } from '../conversation/message-classifier.mjs';
import {
  configuredReply,
  createBuyerReplyAction,
  createQuoteFollowUpAction,
  withConfiguredReplyImage,
} from '../reply/reply-policy.mjs';

const PAYMENT_HOLD_INSTRUCTION = '提交订单后请先不要付款，等待系统确认改价成功后再付款。';

export function paymentSafeOrderInstruction(value) {
  const text = String(value ?? '').trim();
  if (!text) return PAYMENT_HOLD_INSTRUCTION;
  return text.includes(PAYMENT_HOLD_INSTRUCTION) ? text : `${text}\n${PAYMENT_HOLD_INSTRUCTION}`;
}

export function hasActiveQuote(facts, now = Date.now()) {
  return Number(facts?.quote_expires_at ?? 0) > now
    && positiveCents(facts?.quote_total_cents) !== null
    && positiveCents(facts?.quote_ticket_count) !== null;
}

export function quoteSupersessionPreview(preview, quoteContext, envelope, settings = {}, now = Date.now()) {
  const facts = quoteContext?.facts ?? {};
  const previousTotal = positiveCents(facts.quote_total_cents);
  const nextTotal = positiveCents(preview?.total_quote_cents ?? preview?.quote_total_cents);
  const replacesAreaQuote = firstImageUrl(envelope?.payload)
    && preview?.status === 'preview_ready'
    && preview?.quote_scope === 'exact_seats'
    && facts.quote_scope === 'area_probe'
    && Number(facts.quote_expires_at ?? 0) > now
    && previousTotal && nextTotal && previousTotal !== nextTotal;
  if (!replacesAreaQuote) return preview;
  const prefix = configuredReply(
    settings,
    'quote_replaced_by_official_selection',
    '您这次发送的是官方已选座截图，已按具体座位重新核价；上一版未选座试价已失效。',
  );
  return Object.freeze({ ...preview, reply_text: `${prefix}\n${String(preview.reply_text ?? '').trim()}`.trim() });
}

export function createQuoteReplyAction(envelope, preview, enabled, allowRecognitionFollowUp = false, settings = {}) {
  let text = String(preview?.reply_text ?? '').trim();
  // Deterministic quote failures are authoritative and cannot be weakened by
  // stale editable templates or false claims that a human task was created.
  const configuredFailure = preview?.status === 'quote_failed'
    ? ''
    : configuredReply(settings, String(preview?.failure_code ?? ''), '');
  if (configuredFailure) {
    const missing = Array.isArray(preview?.missing_fields)
      ? preview.missing_fields.filter((item) => typeof item === 'string' && item.trim()).join('、')
      : '';
    text = configuredFailure.replaceAll('{缺失信息}', missing || '购票信息');
  }
  if (!enabled || !['preview_ready', 'needs_confirmation', 'ignored', 'quote_failed', 'quote_not_competitive'].includes(preview?.status) || !text) return null;
  const hasCompleteQuote = preview?.status === 'preview_ready'
    && positiveCents(preview?.total_quote_cents ?? preview?.quote_total_cents)
    && positiveCents(preview?.ticket_count ?? preview?.quote_ticket_count);
  if (hasCompleteQuote && preview?.count_completion !== true) {
    const instruction = configuredReply(settings, 'quote_confirmation_instruction', '接受本次报价请回复“确认”。');
    text = `${text}\n${instruction}`;
  }
  const origin = preview?.status === 'preview_ready' ? 'verified_quote' : 'quote_follow_up';
  const action = createBuyerReplyAction(envelope, text, origin, 'quote-reply');
  const templateKey = preview?.status === 'quote_failed' ? '' : String(preview?.failure_code ?? '').trim()
    || (preview?.status === 'quote_not_competitive' ? 'quote_buyer_app_better_price'
      : preview?.count_completion === true ? 'quote_count_completed'
        : preview?.needs_ticket_count === true ? 'quote_need_count'
          : preview?.quote_scope === 'exact_seats' ? 'quote_exact'
            : preview?.quote_scope === 'area_probe' ? 'quote_area' : '');
  const configuredAction = withConfiguredReplyImage(action, settings, templateKey);
  return configuredAction && allowRecognitionFollowUp
    ? { ...configuredAction, allow_plugin_followup: true }
    : configuredAction;
}

export function createImageFailureReplyAction(envelope, mayFollowRecognition, enabled, settings = {}) {
  if (!enabled) return null;
  const text = configuredReply(settings, 'recognition_failed', '选座截图暂未识别成功，请重新发送清晰完整的选座图，并补充影院、影片、场次和需要张数。');
  const action = mayFollowRecognition
    ? createQuoteFollowUpAction(envelope, text)
    : createBuyerReplyAction(envelope, text, 'recognition_failure', 'recognition-failure-reply');
  return withConfiguredReplyImage(action, settings, 'recognition_failed');
}

export function duplicateQuoteClosureAction(envelope, quoteContext, now = Date.now()) {
  const facts = quoteContext?.facts ?? {};
  const unit = positiveCents(facts.quote_unit_cents);
  const total = positiveCents(facts.quote_total_cents);
  const count = positiveCents(facts.quote_ticket_count);
  const delivered = facts.quote_reply_delivered === true && Number(facts.quote_expires_at ?? 0) > now;
  let reply = '当前信息与上一轮一致，本轮未重复发起核价。若需要刷新，请发送最新完整选座图。';
  if (delivered && unit && total && count) {
    reply = `本轮信息与上一轮一致，未重复核价。当前有效报价为${(unit / 100).toFixed(2)}元/张，${count}张合计${(total / 100).toFixed(2)}元。接受本次报价请回复“确认”。`;
  } else if (delivered && unit) {
    reply = `本轮信息与上一轮一致，未重复核价。当前有效报价为${(unit / 100).toFixed(2)}元/张。请告诉我需要几张，我再核对合计。`;
  }
  return createBuyerReplyAction(envelope, reply, 'quote_follow_up', 'duplicate-quote-closure');
}

export function deliveredUnitQuoteQuestionPreview(envelope, quoteContext, now = Date.now()) {
  const facts = quoteContext?.facts ?? {};
  const unit = positiveCents(facts.quote_unit_cents);
  if (!unit || Number(facts.quote_expires_at ?? 0) <= now || facts.quote_reply_delivered !== true
    || positiveCents(facts.quote_total_cents) || positiveCents(facts.quote_ticket_count)
    || !isCurrentQuoteQuestion(envelope?.payload?.content ?? envelope?.payload?.text)) return null;
  return Object.freeze({
    status: 'preview_ready', unit_quote_cents: unit, unit_replay: true,
    reply_text: `当前有效报价为${(unit / 100).toFixed(2)}元/张。请告诉我需要几张，我再核对合计。`,
  });
}

export function completedUnitQuotePreview(envelope, quoteContext, settings = {}, now = Date.now()) {
  const facts = quoteContext?.facts ?? {};
  const unit = positiveCents(facts.quote_unit_cents);
  const expiresAt = Number(facts.quote_expires_at ?? 0);
  if (!unit || expiresAt <= now || positiveCents(facts.quote_total_cents) || positiveCents(facts.quote_ticket_count)
    || !String(facts.pricing_rule_version ?? '').trim() || facts.quote_reply_delivered !== true) return null;
  const buyerText = String(envelope?.payload?.content ?? envelope?.payload?.text ?? '').trim();
  const numeric = buyerText.match(/^([1-9]|1\d|20)\s*(?:张)?[。.!！]?$/u);
  const chinese = buyerText.match(/^([一二三四五六七八九十两])\s*张?[。.!！]?$/u);
  const count = numeric
    ? Number(numeric[1])
    : ({ 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 })[chinese?.[1]] ?? null;
  if (!Number.isInteger(count) || count < 1 || count > 20) return null;
  const total = unit * count;
  const maximum = boundedInteger(settings.max_auto_order_amount_cents, 1, 10_000_000, 200_000);
  if (!Number.isSafeInteger(total) || total <= 0 || total > maximum) return null;
  const cinema = String(facts.cinema ?? '').replace(/\s+/gu, ' ').trim().slice(0, 160);
  const fallback = '已收到{张数}张需求\n实时单价{单价}元/张，{张数}张合计{合计}元。\n请提交订单后先不要付款\n等待系统改价\n仅在收到“价格已修改”后付款。';
  const replyText = configuredReply(settings, 'quote_count_completed', fallback)
    .replaceAll('{张数}', String(count))
    .replaceAll('{单价}', (unit / 100).toFixed(2))
    .replaceAll('{合计}', (total / 100).toFixed(2));
  return Object.freeze({
    status: 'preview_ready', unit_quote_cents: unit, total_quote_cents: total, ticket_count: count, cinema,
    pricing_rule_version: String(facts.pricing_rule_version ?? '').trim(), count_completion: true, reply_text: replyText,
  });
}

function firstImageUrl(payload = {}) {
  const urls = Array.isArray(payload.imageUrls) ? payload.imageUrls : [];
  return urls.find((value) => typeof value === 'string' && value.trim()) ?? null;
}

function boundedInteger(value, minimum, maximum, fallback) {
  const number = Number(value);
  return Number.isInteger(number) && number >= minimum && number <= maximum ? number : fallback;
}

function positiveCents(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}
