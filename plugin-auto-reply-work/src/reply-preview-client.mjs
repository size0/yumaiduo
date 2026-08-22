function text(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

function buyerLabel(payload) {
  const nick = text(payload?.buyerNick ?? payload?.buyer_nick ?? payload?.buyerName, 48);
  if (!nick) return '匿名买家';
  if (nick.length <= 2) return `${nick[0]}*`;
  return `${nick[0]}${'*'.repeat(Math.min(4, nick.length - 2))}${nick.at(-1)}`;
}

export function createReplyPreviewClient(config, { fetchImpl = globalThis.fetch } = {}) {
  const preview = config?.replyPreview;
  if (!preview) return null;
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');

  async function capture(envelope, history) {
    const payload = envelope?.payload ?? {};
    const latestMessage = text(payload?.content ?? payload?.text, 1_000) || '[图片或非文本消息]';
    const response = await fetchImpl(preview.ingestUrl, {
      method: 'POST',
      headers: {
        accept: 'application/json',
        'content-type': 'application/json',
        'x-wanda-preview-key': preview.ingestKey,
      },
      body: JSON.stringify({
        event_id: text(envelope?.id, 200),
        tenant_id: text(envelope?.tenantId, 128),
        buyer_label: buyerLabel(payload),
        latest_message: latestMessage,
        history: normalizeHistory(history, latestMessage),
      }),
      signal: AbortSignal.timeout(75_000),
    });
    if (!response.ok) throw new Error(`reply preview ingestion failed with HTTP ${response.status}`);
    const result = await response.json();
    if (!result || typeof result !== 'object' || Array.isArray(result)) {
      throw new Error('reply preview ingestion returned an invalid response');
    }
    return Object.freeze({
      ...result,
      draft: result.draft ? toSafeDraft(result.draft) : null,
      autoSend: preview.autoSend === true,
    });
  }

  return Object.freeze({ capture });
}

function normalizeHistory(history, latestMessage) {
  const entries = Array.isArray(history) ? history : [];
  const normalized = entries.map((item) => ({
    role: item?.role === 'seller' ? 'seller' : 'buyer',
    content: text(item?.content, 1_000),
    ...(item?.sent_at ? { sent_at: text(item.sent_at, 64) } : {}),
  })).filter((item) => item.content).slice(-20);
  if (!normalized.some((item) => item.role === 'buyer' && item.content === latestMessage)) {
    normalized.push({ role: 'buyer', content: latestMessage });
  }
  return normalized.slice(-20);
}

function toSafeDraft(draft) {
  const input = draft && typeof draft === 'object' && !Array.isArray(draft) ? draft : {};
  return Object.freeze({
    intent: text(input.intent, 64),
    confidence: Number.isFinite(input.confidence) ? Number(input.confidence) : 0,
    needs_human: input.needs_human === true,
    reply: text(input.reply, 500),
    reason: text(input.reason, 160),
  });
}
