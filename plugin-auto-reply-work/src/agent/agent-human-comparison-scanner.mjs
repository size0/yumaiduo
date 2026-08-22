import { createHash } from 'node:crypto';

function text(value, limit = 500) { return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, limit); }
function intentFor(value) {
  const message = text(value).replace(/\s+/gu, '');
  if (/(?:退款|退票|售后|取消)/u.test(message)) return '售后';
  if (/(?:出票|票码|发货)/u.test(message)) return '出票';
  if (/(?:付款|支付|改价|订单|拍下)/u.test(message)) return '订单付款';
  if (/(?:价格|报价|多少钱|几钱|核价|优惠)/u.test(message)) return '核价';
  return '普通咨询';
}
function expectedActionFor(value) {
  const message = text(value).replace(/\s+/gu, '');
  if (/(?:完整.*(?:截图|选座图)|补.*(?:图|截图)|发.*(?:图片|截图))/u.test(message)) return 'ask_for_image';
  if (/(?:几张|多少张|补充.*张数|需要.*张)/u.test(message)) return 'ask_for_missing_information';
  if (/(?:人工|核实|核对|稍等|出票|票码|退款|售后|发货)/u.test(message)) return 'handoff';
  if (/(?:订单|付款|支付|状态|进度)/u.test(message)) return 'read_linked_order';
  if (/(?:实时|核价|报价|价格)/u.test(message)) return 'quote_realtime';
  return 'respond';
}
function actionAligned(actual, expected) {
  if (actual === expected) return true;
  if (expected === 'ask_for_missing_information' && ['ask_for_city', 'ask_for_missing_information'].includes(actual)) return true;
  if (expected === 'handoff' && ['handoff', 'create_manual_task'].includes(actual)) return true;
  return false;
}
function quoteStatus(facts) { return ['quoted', 'quote_confirmed', 'waiting_payment'].includes(String(facts?.stage ?? '')) ? 'present' : 'none'; }
function factAlignment(agent, facts) {
  const reply = text(agent.proposed_reply);
  if (!reply) return null;
  const stage = String(facts?.stage ?? '');
  if (/(?:已经|已)付款/u.test(reply) && !/paid|ticket_/u.test(stage)) return false;
  const claimedCount = reply.match(/(\d{1,2})\s*张/u)?.[1];
  const actualCount = Number(facts?.quote_ticket_count ?? facts?.ticket_count);
  if (claimedCount && Number.isInteger(actualCount) && actualCount > 0 && Number(claimedCount) !== actualCount) return false;
  const claimedPrices = [...reply.matchAll(/(\d+(?:\.\d{1,2})?)\s*元/gu)].map((match) => Math.round(Number(match[1]) * 100));
  const knownPrices = new Set([Number(facts?.quote_unit_cents), Number(facts?.quote_total_cents)].filter((value) => Number.isSafeInteger(value) && value > 0));
  if (claimedPrices.length && (!knownPrices.size || claimedPrices.some((value) => !knownPrices.has(value)))) return false;
  return true;
}
function automatic(agent, human, facts) {
  const intentAligned = String(human.intent ?? '') === String(agent.intent ?? '') || String(human.intent ?? '') === intentFor(agent.proposed_reply);
  const toolAligned = actionAligned(String(agent.action ?? ''), human.expected_action);
  const factsAligned = factAlignment(agent, facts);
  const riskAligned = factsAligned !== false && !(human.expected_action === 'handoff' && ['quote_realtime', 'request_price_change'].includes(String(agent.action ?? '')));
  const replyStrategyAligned = toolAligned && riskAligned;
  let suggested = 'aligned';
  if (!riskAligned) suggested = 'unsafe_claim';
  else if (!intentAligned) suggested = 'wrong_intent';
  else if (!toolAligned) suggested = agent.action === 'handoff' && human.expected_action !== 'handoff' ? 'unnecessary_handoff' : 'wrong_tool';
  return { intent_aligned: intentAligned, tool_aligned: toolAligned, facts_aligned: factsAligned, risk_aligned: riskAligned, reply_strategy_aligned: replyStrategyAligned, suggested_label: suggested };
}
function messageTime(message) { const parsed = Date.parse(String(message?.sentAt ?? message?.sent_at ?? '')); return Number.isFinite(parsed) ? parsed : 0; }
function messageId(message) { return text(message?.messageId ?? message?.message_id, 200); }
function messageBody(message) { return text(message?.content ?? message?.text ?? message?.body?.text, 500); }
function direction(message) { return String(message?.direction ?? '').toLowerCase(); }

export function createAgentHumanComparisonScanner({ conversationContextStore, eventStore, agentRunStore, coreFor, comparisonStore, logger = console } = {}) {
  let scanCursor = 0;
  async function tick() {
    if (!conversationContextStore?.listRecent || !eventStore?.list || !agentRunStore?.list || !comparisonStore?.capture) return { scanned: 0, captured: 0 };
    const contexts = await conversationContextStore.listRecent({ limit: 100 });
    const scanBatch = contexts.length <= 10 ? contexts : Array.from({ length: Math.min(10, contexts.length) }, (_, offset) => contexts[(scanCursor + offset) % contexts.length]);
    scanCursor = contexts.length ? (scanCursor + scanBatch.length) % contexts.length : 0;
    const events = await eventStore.list({ limit: 500 });
    const runsByTenant = new Map(); let captured = 0;
    for (const context of scanBatch) {
      try {
        const core = coreFor(context.tenant_id);
        const page = await core.im.listMessages({ accountUnb: context.account_unb, chatId: context.chat_id, pageSize: 50 });
        const messages = (Array.isArray(page?.items) ? page.items : []).slice().sort((left, right) => messageTime(left) - messageTime(right));
        for (let index = 1; index < messages.length; index += 1) {
          const seller = messages[index]; const sellerId = messageId(seller); const sellerReply = messageBody(seller);
          if (direction(seller) !== 'outbound' || !sellerId || !sellerReply) continue;
          if (await eventStore.wasSentMessage(context.tenant_id, context.chat_id, sellerId)) continue;
          let buyer = null;
          for (let cursor = index - 1; cursor >= 0; cursor -= 1) if (['inbound', 'buyer'].includes(direction(messages[cursor]))) { buyer = messages[cursor]; break; }
          const buyerId = messageId(buyer); if (!buyerId) continue;
          const source = events.find((event) => String(event.envelope?.tenantId) === context.tenant_id
            && String(event.envelope?.payload?.messageId ?? event.envelope?.payload?.message_id ?? '') === buyerId);
          if (!source) continue;
          if (!runsByTenant.has(context.tenant_id)) runsByTenant.set(context.tenant_id, await agentRunStore.list({ tenantId: context.tenant_id, limit: 500 }));
          const run = runsByTenant.get(context.tenant_id).find((item) => item.mode === 'shadow' && item.event_key === source.key);
          if (!run) continue;
          const trace = Array.isArray(run.result?.trace) ? run.result.trace : [];
          const plan = trace[0] ?? {}; const humanExpected = expectedActionFor(sellerReply);
          const agent = { intent: text(plan.intent, 32), action: text(plan.action, 64), goal: text(plan.goal, 160), proposed_reply: text(run.result?.proposed_reply, 500) };
          const human = { reply: sellerReply, intent: intentFor(messageBody(buyer)), expected_action: humanExpected };
          const facts = context.facts ?? {}; const stage = text(facts.stage, 64);
          const id = createHash('sha256').update(`${context.tenant_id}:${context.account_unb}:${context.chat_id}:${sellerId}`).digest('hex');
          await comparisonStore.capture({
            comparisonId: id, tenantId: context.tenant_id, sourceEventId: String(source.envelope?.id ?? ''), sellerMessageId: sellerId,
            accountUnb: context.account_unb, chatId: context.chat_id, peerUnb: context.peer_unb,
            buyerTurn: { summary: messageBody(buyer) || '[图片或非文本消息]', has_image: Array.isArray(source.envelope?.payload?.imageUrls) && source.envelope.payload.imageUrls.length > 0 },
            conversationFacts: { ...facts, ticket_count: facts.quote_ticket_count ?? facts.ticket_count, has_linked_order: Boolean(facts.order_id) },
            quoteOrderState: { quote_status: quoteStatus(facts), order_lifecycle: stage || 'none', paid: /paid|ticket_/u.test(stage), fulfilled: /ticket_issued|ticket_sent/u.test(stage) },
            agent, human, outcome: { stage, quote_status: quoteStatus(facts), order_lifecycle: stage || 'none', paid: /paid|ticket_/u.test(stage), fulfilled: /ticket_issued|ticket_sent/u.test(stage) },
            automaticComparison: automatic(agent, human, facts),
          });
          captured += 1;
        }
      } catch (error) { logger.warn?.('[human-comparison] scan skipped', { tenantId: context.tenant_id, error: String(error?.message ?? error) }); }
    }
    return { scanned: scanBatch.length, captured };
  }
  return Object.freeze({ tick });
}
