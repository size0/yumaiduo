import { hasUnresolvedReplyPlaceholder } from '../agent/response-composer.mjs';

export function configuredReply(settings, key, fallback) {
  const templates = settings?.reply_templates;
  const candidate = templates && typeof templates === 'object' ? templates[key] : null;
  return typeof candidate === 'string' && candidate.trim() ? candidate.trim() : fallback;
}

export function configuredReplyImage(settings, key) {
  const images = settings?.reply_template_images;
  const candidate = key && images && typeof images === 'object' ? String(images[key] ?? '').trim() : '';
  return /^https:\/\/[^\s]{1,1992}$/iu.test(candidate) ? candidate : '';
}

export function withConfiguredReplyImage(action, settings, key) {
  if (!action) return null;
  const imageUrl = configuredReplyImage(settings, key);
  return imageUrl ? { ...action, kind: 'reply_with_image', image_url: imageUrl } : action;
}

export function createBuyerReplyAction(envelope, text, replyOrigin, actionSuffix = 'auto-reply') {
  if (hasUnresolvedReplyPlaceholder(text)) return null;
  const payload = envelope?.payload ?? {};
  const accountUnb = String(payload.accountUnb ?? payload.account_unb ?? '').trim();
  const chatId = String(payload.chatId ?? payload.chat_id ?? '').trim();
  const peerUnb = String(payload.peerUnb ?? payload.peer_unb ?? '').trim();
  if (!accountUnb || !chatId || !peerUnb) return null;
  return {
    action_id: `${envelope.id}:${actionSuffix}`,
    kind: 'reply',
    tenant_id: String(envelope.tenantId),
    account_unb: accountUnb,
    chat_id: chatId,
    peer_unb: peerUnb,
    text: String(text ?? '').trim(),
    reply_origin: replyOrigin,
  };
}

export function createQuoteFollowUpAction(envelope, message) {
  const action = createBuyerReplyAction(envelope, message, 'quote_follow_up', 'quote-conversation-follow-up');
  return action ? { ...action, allow_plugin_followup: true } : null;
}
