import { createHmac } from 'node:crypto';
import { validateDifyShadowResponse } from './dify-response-validator.mjs';

const CONTACT = /1\d{10}/gu;
const LONG_IDENTIFIER = /\b\d{12,}\b/gu;
const URL = /https?:\/\/[^\s,，。！？、;；]+/giu;
const MONEY = /(?:[¥￥]\s*\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s*(?:元|块钱?|人民币))/giu;
const BEARER = /\bBearer\s+[^\s,;]+/giu;
const TOKEN = /\b(?:pdk|yp|app)-?[A-Za-z0-9_-]{8,}\b/gu;
const CONTEXT_STRING_FIELDS = Object.freeze(['stage', 'city', 'cinema', 'movie', 'date', 'showtime', 'hall']);

/** Advisory-only Dify Workflow client. It has no transaction or reply port. */
export function createDifyClient(config, { fetchImpl = globalThis.fetch } = {}) {
  const dify = config?.difyShadow;
  if (!dify) return null;
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');

  async function evaluate(input = {}) {
    const body = workflowBody(input, dify.apiKey);
    const encoded = JSON.stringify(body);
    if (Buffer.byteLength(encoded, 'utf8') > 20_000) throw new TypeError('Dify Shadow request exceeds bounded size');
    let response;
    try {
      response = await fetchImpl(dify.url, {
        method: 'POST',
        headers: {
          accept: 'application/json',
          'content-type': 'application/json',
          authorization: `Bearer ${dify.apiKey}`,
        },
        body: encoded,
        redirect: 'error',
        signal: combinedSignal(input.signal, dify.timeoutMs),
      });
    } catch {
      throw new Error('Dify Shadow request failed');
    }
    if (!response.ok) throw new Error(`Dify Shadow request failed with HTTP ${response.status}`);
    let payload;
    try {
      const responseText = await response.text();
      if (Buffer.byteLength(responseText, 'utf8') > 20_000) throw new TypeError('response too large');
      payload = JSON.parse(responseText);
    } catch { throw new Error('Dify Shadow returned invalid JSON'); }
    return validateDifyShadowResponse(payload);
  }

  return Object.freeze({ evaluate });
}

function workflowBody(input, pseudonymKey) {
  return {
    inputs: {
      latest_message: safeText(input?.latest_message, 500),
      recent_history_json: JSON.stringify(recentHistory(input?.state)),
      context_json: JSON.stringify(safeContext(input)),
      observations_json: JSON.stringify(safeObservations(input?.observations)),
    },
    response_mode: 'blocking',
    user: pseudonymousUser(input?.tenant_id, pseudonymKey),
  };
}

function recentHistory(state) {
  const messages = Array.isArray(state?.messages) ? state.messages : [];
  return messages.slice(-6).map((item) => Object.freeze({
    role: item?.role === 'seller' ? 'seller' : 'buyer',
    source: item?.role === 'seller' && ['plugin', 'external_seller', 'unknown'].includes(String(item?.source))
      ? String(item.source)
      : 'buyer',
    content: safeText(item?.content ?? item?.text, 300),
  })).filter((item) => item.content);
}

function safeContext(input) {
  const facts = input?.state?.facts && typeof input.state.facts === 'object' && !Array.isArray(input.state.facts)
    ? input.state.facts
    : {};
  const context = {};
  for (const key of CONTEXT_STRING_FIELDS) {
    const value = safeText(facts[key], key === 'cinema' || key === 'movie' ? 160 : 80);
    if (value) context[key] = value;
  }
  if (input?.has_image === true) context.has_image = true;
  if (facts.has_active_quote === true) context.has_active_quote = true;
  if (facts.has_linked_order === true) context.has_linked_order = true;
  const ticketCount = Number(facts.ticket_count ?? facts.quote_ticket_count);
  if (Number.isSafeInteger(ticketCount) && ticketCount > 0 && ticketCount <= 20) context.ticket_count = ticketCount;
  return context;
}

function safeObservations(values) {
  if (!Array.isArray(values)) return [];
  return values.slice(-6).map((item) => ({
    status: ['success', 'warning', 'error'].includes(item?.status) ? item.status : 'error',
    tool: plainText(item?.tool, 64) || 'unknown',
    next_actions: Array.isArray(item?.next_actions)
      ? item.next_actions.slice(0, 6).map((action) => plainText(action, 64)).filter(Boolean)
      : [],
    ...(item?.stop_reason ? { stop_reason: plainText(item.stop_reason, 100) } : {}),
  }));
}

function pseudonymousUser(tenantId, key) {
  const digest = createHmac('sha256', key).update(String(tenantId ?? '')).digest('hex').slice(0, 24);
  return `wanda-shadow-${digest}`;
}

function safeText(value, maxLength) {
  return plainText(value, maxLength * 2)
    .replace(BEARER, 'Bearer [凭据]')
    .replace(TOKEN, '[凭据]')
    .replace(URL, '[链接]')
    .replace(CONTACT, '[联系方式]')
    .replace(MONEY, '[金额]')
    .replace(LONG_IDENTIFIER, '[编号]')
    .slice(0, maxLength);
}

function plainText(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

function combinedSignal(value, timeoutMs) {
  const timeout = AbortSignal.timeout(Number(timeoutMs));
  return typeof AbortSignal !== 'undefined' && value instanceof AbortSignal
    ? AbortSignal.any([value, timeout])
    : timeout;
}
