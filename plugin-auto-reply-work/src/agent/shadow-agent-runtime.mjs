import { createHash } from 'node:crypto';
import { createConversationAgent } from './conversation-agent.mjs';
import { inspectTicketRequest } from './ticket-request-inspector.mjs';

export const AGENT_RUNTIME_VERSION = 'wanda-agent-runtime-v35-source-turn-supersession';

function eventKey(envelope) { return `${String(envelope?.tenantId ?? '')}:${String(envelope?.id ?? '')}`; }
function runIdFor(envelope, mode) {
  if (mode !== 'evaluation') return `${mode}:${eventKey(envelope)}`;
  const sourceHash = createHash('sha256').update(eventKey(envelope)).digest('hex');
  return `evaluation:${AGENT_RUNTIME_VERSION}:${sourceHash}`;
}
function text(value, length = 1_000) { return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, length); }
function comparisonReply(value) {
  return text(value, 500).replace(/https?:\/\/\S+/giu, '[链接]').replace(/1\d{10}/gu, '[手机号]').replace(/\b\d{12,}\b/gu, '[编号]');
}
function hasImage(payload = {}) {
  return (Array.isArray(payload.imageUrls) && payload.imageUrls.some((value) => typeof value === 'string' && value.trim()))
    || /https?:\/\/\S+\.(?:jpe?g|png|webp|gif)(?:\?\S*)?$/iu.test(String(payload.content ?? payload.text ?? ''));
}

function latestSourceImageUrl(payload = {}) {
  const values = Array.isArray(payload.imageUrls) ? payload.imageUrls.filter((value) => typeof value === 'string' && value.trim()) : [];
  return text(values.at(-1), 2_000);
}

function messageTime(value) {
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric >= 0) return numeric;
  const parsed = Date.parse(String(value ?? ''));
  return Number.isFinite(parsed) ? parsed : null;
}

async function sourceTimeState(envelope, conversationContextStore) {
  const payload = envelope?.payload ?? {};
  const state = await conversationContextStore.get(envelope.tenantId, payload);
  const sourceAt = messageTime(envelope.ts) ?? Date.now();
  const fallbackMessages = (Array.isArray(state?.messages) ? state.messages : [])
    .filter((item) => (messageTime(item?.at) ?? sourceAt) <= sourceAt)
    .map((item) => ({
      at: messageTime(item?.at) ?? sourceAt,
      role: item?.role === 'seller' ? 'seller' : 'buyer',
      source: item?.role === 'seller' ? text(item?.source, 32) || 'unknown' : 'buyer',
      content: text(item?.content ?? item?.text, 1_000) || (item?.image === true ? '[图片]' : ''),
    })).filter((item) => item.content);
  return { facts: state?.facts ?? {}, messages: fallbackMessages };
}

function sourceSnapshot(source) {
  const result = source?.result ?? {};
  const authoritativeOutcome = result.preview_status === 'preview_ready'
    ? 'quote_succeeded'
    : result.quote_failure_code ? 'quote_failed' : 'not_available';
  return Object.freeze({ has_image: hasImage(source?.envelope?.payload), authoritative_outcome: authoritativeOutcome });
}

function withSourceSnapshot(result, source) {
  return Object.freeze({ ...result, source_snapshot: sourceSnapshot(source) });
}

function sourceStateSnapshot(source) {
  const snapshot = source?.result?.agent_state_snapshot;
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) return { facts: {}, messages: [] };
  const facts = {};
  for (const key of ['quote_total_cents', 'quote_ticket_count', 'quote_expires_at']) {
    const value = Number(snapshot[key]);
    if (Number.isSafeInteger(value) && value > 0) facts[key] = value;
  }
  for (const key of ['stage', 'pricing_rule_version']) {
    const value = text(snapshot[key], 80);
    if (value) facts[key] = value;
  }
  if (snapshot.quote_reply_delivered === true) facts.quote_reply_delivered = true;
  return { facts, messages: [] };
}

function sourceObservation(source, tool) {
  const result = source?.result ?? {};
  const failureCode = text(result.quote_failure_code ?? result.agent_turn_reason, 100);
  const facts = {
    status: text(result.preview_status, 64) || null,
    failure_code: failureCode || null,
    quote_succeeded: result.preview_status === 'preview_ready',
  };
  const replySnapshot = result.agent_reply_snapshot;
  const authoritativeReply = ['quote', 'conversation_follow_up'].includes(String(replySnapshot?.kind)) ? text(replySnapshot.text, 1_000) : '';
  if (failureCode) {
    return authoritativeReply
      ? { status: 'warning', tool, summary: '确定性业务链路已安全停止并形成权威回复', facts, authoritative_reply: authoritativeReply, next_actions: ['respond'] }
      : { status: 'error', tool, summary: '确定性业务链路已安全停止', facts, next_actions: ['handoff'], stop_reason: failureCode };
  }
  if (result.preview_status === 'preview_ready' && !authoritativeReply) {
    return { status: 'error', tool, summary: '确定性核价成功但缺少事件时点的权威回复快照', facts, next_actions: ['handoff'], stop_reason: 'missing_authoritative_reply_snapshot' };
  }
  if (result.preview_status === 'quote_deduplicated' && !authoritativeReply) {
    return { status: 'warning', tool, summary: '本轮信息与上一轮相同，确定性链路未重复核价', facts, next_actions: ['handoff'], stop_reason: 'duplicate_quote_draft' };
  }
  return {
    status: 'success', tool,
    summary: result.preview_status === 'preview_ready' ? '确定性业务链路已形成权威报价' : '确定性业务链路已完成本轮处理',
    facts, ...(authoritativeReply ? { authoritative_reply: authoritativeReply } : {}), next_actions: ['respond'],
  };
}

function runtimeTools(source, state, mode, conversationContextStore, manualTaskStore, coreFor, quotePreviewClient, initialObservations = []) {
  let quoteInput = [...initialObservations].reverse().find((item) => item?.facts?._quote_input)?.facts?._quote_input ?? null;
  if (!quoteInput) {
    try { quoteInput = compactQuoteInput(state?.facts?.quote_draft?.recognition_artifact); }
    catch { quoteInput = null; }
  }
  let showtimeResolved = initialObservations.some((item) => item?.tool === 'resolve_showtime' && item?.status === 'success' && (mode !== 'active' || item?.facts?._quote_input));
  const identityFacts = Object.fromEntries(['city', 'cinema', 'movie', 'date', 'showtime', 'hall']
    .map((key) => [key, state?.facts?.[key]])
    .filter(([, value]) => value != null && String(value).trim()));
  return {
    async inspect_ticket_request() {
      const payload = source?.envelope?.payload ?? {};
      return {
        status: 'success', tool: 'inspect_ticket_request', summary: '已完成有界票务请求检查',
        facts: inspectTicketRequest({ message: payload.content ?? payload.text, hasImage: hasImage(payload), facts: state?.facts }),
        next_actions: hasImage(payload) ? ['recognize_image'] : ['respond', 'ask_for_image'],
      };
    },
    async resolve_ticket_identity() {
      if (mode !== 'active') {
        const complete = ['cinema', 'movie', 'date', 'showtime'].every((key) => identityFacts[key]);
        return complete
          ? { status: 'success', tool: 'resolve_ticket_identity', summary: '已读取事件时点的文字场次事实', facts: identityFacts, next_actions: ['show_available_wplus_seats'] }
          : { status: 'warning', tool: 'resolve_ticket_identity', summary: '事件时点缺少可唯一匹配的文字场次事实', facts: identityFacts, next_actions: ['ask_for_missing_information'] };
      }
      if (typeof quotePreviewClient?.recognize !== 'function' || typeof quotePreviewClient?.resolveShowtime !== 'function') {
        return { status: 'error', tool: 'resolve_ticket_identity', summary: '文字场次解析工具不可用', facts: {}, next_actions: ['handoff'], stop_reason: 'ticket_identity_tool_unavailable' };
      }
      const envelope = source.envelope;
      const candidateSet = state?.facts?.candidate_set;
      const candidateIndex = candidateIndexFromMessage(envelope?.payload?.content ?? envelope?.payload?.text);
      const selectedCandidate = candidateIndex && Array.isArray(candidateSet?.candidates)
        ? candidateSet.candidates.find((candidate) => Number(candidate?.index) === candidateIndex)
        : null;
      let recognized;
      if (selectedCandidate && candidateSet?.base_recognition) {
        recognized = {
          status: 'recognized', tenant_id: String(envelope.tenantId), text_quote: true,
          recognition: { ...boundedIdentity(candidateSet.base_recognition), ...boundedIdentity(selectedCandidate) },
        };
      } else {
        recognized = await quotePreviewClient.recognize(envelope);
      }
      if (recognized?.status !== 'recognized') {
        return { status: 'warning', tool: 'resolve_ticket_identity', summary: '买家文字尚不足以唯一匹配影院场次', facts: {}, next_actions: ['ask_for_missing_information'] };
      }
      let resolved;
      try { resolved = await quotePreviewClient.resolveShowtime(recognized); }
      catch (error) {
        const candidates = typeof quotePreviewClient.resolveCandidates === 'function'
          ? await quotePreviewClient.resolveCandidates(recognized)
          : [];
        if (candidates.length && typeof conversationContextStore?.recordCandidateSet === 'function') {
          await conversationContextStore.recordCandidateSet(envelope.tenantId, envelope.payload ?? {}, {
            baseRecognition: recognized.recognition, candidates,
          });
          const labels = candidates.map((candidate, index) => `${index + 1}.${text(candidate.cinema, 160) || '候选影院'}`);
          return {
            status: 'warning', tool: 'resolve_ticket_identity', summary: '官方场次匹配返回多个安全候选',
            facts: { ...boundedIdentity(recognized.recognition), candidate_count: candidates.length, candidate_labels: labels },
            authoritative_reply: `匹配到多个影院候选：${labels.join('；')}。请回复序号或完整分店名。`, next_actions: ['respond'],
          };
        }
        return { status: 'warning', tool: 'resolve_ticket_identity', summary: '影院或场次尚未唯一匹配', facts: boundedIdentity(recognized.recognition), next_actions: ['ask_for_missing_information'] };
      }
      if (resolved?.status !== 'resolved') {
        return { status: 'warning', tool: 'resolve_ticket_identity', summary: '影院或场次尚未唯一匹配', facts: boundedIdentity(recognized.recognition), next_actions: ['ask_for_missing_information'] };
      }
      if (selectedCandidate) await conversationContextStore?.clearCandidateSet?.(envelope.tenantId, envelope.payload ?? {});
      quoteInput = compactQuoteInput(resolved);
      showtimeResolved = true;
      return {
        status: 'success', tool: 'resolve_ticket_identity', summary: '已从买家文字唯一匹配影院、影片、日期和场次',
        facts: { ...boundedIdentity(resolved.recognition), _quote_input: quoteInput }, next_actions: ['show_available_wplus_seats'],
      };
    },
    async recognize_image() {
      if (!hasImage(source?.envelope?.payload)) return { status: 'error', tool: 'recognize_image', summary: '本轮没有图片证据', facts: {}, next_actions: ['handoff'], stop_reason: 'image_evidence_missing' };
      if (mode !== 'active') return { status: 'success', tool: 'recognize_image', summary: '已读取确定性识图结果', facts: identityFacts, next_actions: ['resolve_showtime'] };
      if (typeof quotePreviewClient?.recognize !== 'function') return { status: 'error', tool: 'recognize_image', summary: '权威识图工具不可用', facts: {}, next_actions: ['create_manual_task'], stop_reason: 'recognition_tool_unavailable' };
      const recognized = await quotePreviewClient.recognize(source.envelope);
      if (recognized?.status !== 'recognized') return { status: 'error', tool: 'recognize_image', summary: '图片未形成可核验的票务事实', facts: {}, authoritative_reply: text(recognized?.reply_text, 500), next_actions: ['create_manual_task'], stop_reason: text(recognized?.failure_code, 100) || 'recognition_not_usable' };
      quoteInput = compactQuoteInput(recognized);
      showtimeResolved = false;
      const facts = { ...boundedIdentity(recognized.recognition), requested_ticket_count: Number.isInteger(recognized.ticket_count) ? recognized.ticket_count : null, _quote_input: quoteInput };
      return { status: 'success', tool: 'recognize_image', summary: '权威识图已完成', facts, next_actions: ['resolve_showtime'] };
    },
    async resolve_showtime() {
      if (mode !== 'active') {
        const result = source?.result ?? {}; const failure = text(result.quote_failure_code, 100);
        if (failure) return { status: 'error', tool: 'resolve_showtime', summary: '确定性场次匹配未通过', facts: identityFacts, next_actions: ['handoff'], stop_reason: failure };
        showtimeResolved = true;
        return { status: 'success', tool: 'resolve_showtime', summary: '已读取确定性场次匹配结果', facts: identityFacts, next_actions: ['quote_realtime'] };
      }
      if (!quoteInput) return { status: 'error', tool: 'resolve_showtime', summary: '缺少已持久化的识图事实', facts: {}, next_actions: ['create_manual_task'], stop_reason: 'recognition_artifact_missing' };
      if (typeof quotePreviewClient?.resolveShowtime !== 'function') return { status: 'error', tool: 'resolve_showtime', summary: '权威场次匹配工具不可用', facts: {}, next_actions: ['create_manual_task'], stop_reason: 'showtime_tool_unavailable' };
      const resolved = await quotePreviewClient.resolveShowtime(quoteInput);
      if (resolved?.status !== 'resolved') return { status: 'error', tool: 'resolve_showtime', summary: '影院场次未能唯一匹配', facts: {}, next_actions: ['create_manual_task'], stop_reason: text(resolved?.failure_code, 100) || 'showtime_not_unique' };
      quoteInput = compactQuoteInput(resolved);
      showtimeResolved = true;
      return { status: 'success', tool: 'resolve_showtime', summary: '影院、影片、日期和场次已唯一匹配', facts: { ...boundedIdentity(resolved.recognition), _quote_input: quoteInput }, next_actions: ['quote_realtime'] };
    },
    async quote_realtime() {
      if (!showtimeResolved) return { status: 'error', tool: 'quote_realtime', summary: '必须先完成只读场次匹配', facts: {}, next_actions: ['resolve_showtime'], stop_reason: 'showtime_resolution_required' };
      if (mode !== 'active') return sourceObservation(source, 'quote_realtime');
      if (!quoteInput) return { status: 'error', tool: 'quote_realtime', summary: '缺少已匹配的场次事实', facts: {}, next_actions: ['create_manual_task'], stop_reason: 'showtime_artifact_missing' };
      if (typeof quotePreviewClient?.quote !== 'function') return { status: 'error', tool: 'quote_realtime', summary: '实时核价工具不可用', facts: {}, next_actions: ['create_manual_task'], stop_reason: 'quote_tool_unavailable' };
      const quoted = await quotePreviewClient.quote(quoteInput);
      let authoritativeReply = text(quoted?.reply_text, 500);
      if (quoted?.status !== 'preview_ready') {
        return authoritativeReply
          ? { status: 'warning', tool: 'quote_realtime', summary: '实时核价已安全停止', facts: { failure_code: text(quoted?.failure_code, 100) || quoted?.status }, authoritative_reply: authoritativeReply, next_actions: ['respond'] }
          : { status: 'error', tool: 'quote_realtime', summary: '实时核价未形成可回复结果', facts: {}, next_actions: ['create_manual_task'], stop_reason: text(quoted?.failure_code, 100) || 'quote_not_available' };
      }
      if (!/(?:回复|回答).{0,3}确认/u.test(authoritativeReply)) authoritativeReply = `${authoritativeReply}\n接受本次报价请回复“确认”。`.trim();
      const delivery = quoteDeliverySnapshot(quoted);
      if (quoted?.recognition?.hand_drawn_circle?.exists === true) delivery.circled_delivery_image_url = latestSourceImageUrl(source?.envelope?.payload);
      return {
        status: 'success', tool: 'quote_realtime', summary: '实时核价已完成', authoritative_reply: authoritativeReply,
        facts: { quote_succeeded: true, unit_quote_cents: delivery.unit_quote_cents, total_quote_cents: delivery.total_quote_cents, ticket_count: delivery.ticket_count, pricing_rule_version: delivery.pricing_rule_version, _quote_delivery: delivery },
        next_actions: ['respond'],
      };
    },
    async read_active_quote() {
      const total = safePositiveInteger(state?.facts?.quote_total_cents ?? state?.facts?.total_quote_cents);
      const count = safePositiveInteger(state?.facts?.quote_ticket_count ?? state?.facts?.ticket_count);
      const explicitUnit = safePositiveInteger(state?.facts?.quote_unit_cents ?? state?.facts?.unit_quote_cents);
      const unit = explicitUnit ?? (total && count && total % count === 0 ? total / count : null);
      if (!total || !count || !unit) {
        return { status: 'error', tool: 'read_active_quote', summary: '当前报价已失效或证据不完整', facts: {}, next_actions: ['respond'], stop_reason: 'active_quote_missing_or_expired' };
      }
      const snapshot = source?.result?.agent_reply_snapshot;
      const snapshotReply = ['quote', 'conversation_follow_up'].includes(String(snapshot?.kind)) ? text(snapshot.text, 1_000) : '';
      const authoritativeReply = snapshotReply || `当前有效报价是${(unit / 100).toFixed(2)}元/张，${count}张合计${(total / 100).toFixed(2)}元。接受本次报价请回复“确认”，提交订单后请先不要付款，等待系统确认改价成功后再付款。`;
      return {
        status: 'success', tool: 'read_active_quote', summary: '已读取当前有效权威报价', authoritative_reply: authoritativeReply,
        facts: { unit_quote_cents: unit, total_quote_cents: total, ticket_count: count }, next_actions: ['respond'],
      };
    },
    async request_price_change() {
      if (mode === 'active') {
        return { status: 'error', tool: 'request_price_change', summary: 'Agent改价申请执行尚未开放', facts: {}, next_actions: ['handoff'], stop_reason: 'agent_price_change_not_enabled' };
      }
      return {
        status: 'success', tool: 'request_price_change', summary: '影子模式仅评估无参数改价申请，不执行平台改价',
        facts: { price_change_requested: false }, next_actions: ['respond'],
      };
    },
    async recognize_and_quote() { return sourceObservation(source, 'recognize_and_quote'); },
    async list_available_wplus_seats() {
      if (mode !== 'active') return sourceObservation(source, 'list_available_wplus_seats');
      const message = text(source?.envelope?.payload?.content ?? source?.envelope?.payload?.text, 1_000);
      const rowMatch = message.match(/(?:^|\D)([1-9]\d?)\s*排/u);
      const row = rowMatch ? Number(rowMatch[1]) : null;
      const recognition = quoteInput?.recognition;
      if (!recognition || typeof recognition !== 'object' || Array.isArray(recognition)) {
        return { status: 'error', tool: 'list_available_wplus_seats', summary: '缺少已识别的影院场次事实', facts: {}, next_actions: ['handoff'], stop_reason: 'seat_lookup_artifact_missing' };
      }
      if (typeof quotePreviewClient?.availableSeats !== 'function') {
        return { status: 'error', tool: 'list_available_wplus_seats', summary: '只读实时座位工具不可用', facts: {}, next_actions: ['handoff'], stop_reason: 'seat_reader_unavailable' };
      }
      const result = await quotePreviewClient.availableSeats({ recognition, row });
      const seatPattern = row === null ? /^\d{1,2}排\d{1,3}座$/u : new RegExp(`^${row}排\\d{1,3}座$`, 'u');
      const seats = Array.isArray(result?.seats)
        ? result.seats.map((seat) => text(seat, 80)).filter((seat) => seatPattern.test(seat)).slice(0, 30)
        : [];
      const availableCount = Number.isSafeInteger(result?.available_count) && result.available_count >= seats.length
        ? result.available_count
        : seats.length;
      const offerAvailable = result?.wplus_offer_available === true;
      const cinema = text(result?.matched_cinema_name ?? recognition.cinema, 160);
      const scopeLabel = row === null ? 'W+区域' : `${row}排`;
      const authoritativeReply = offerAvailable && seats.length
        ? `当前万达实时座位图中，${scopeLabel}可选座位：${seats.join('、')}。座位状态可能变化，请以提交订单时页面为准。`
        : `当前万达实时座位图中，${scopeLabel}暂未确认到可选W+优惠座位。`;
      return {
        status: 'success', tool: 'list_available_wplus_seats', summary: '已完成只读实时W+座位查询',
        facts: { requested_row: row, available_count: availableCount, seat_numbers: seats, wplus_offer_available: offerAvailable, ...(cinema ? { cinema } : {}) },
        authoritative_reply: authoritativeReply, next_actions: ['respond'],
      };
    },
    async record_seat_preference() {
      const snapshot = source?.result?.agent_reply_snapshot;
      const shadowReply = mode !== 'active' && snapshot?.kind === 'conversation_follow_up' ? text(snapshot.text, 1_000) : '';
      if (mode !== 'active') {
        return {
          status: 'success', tool: 'record_seat_preference', summary: '影子模式仅评估记录指令，不写入业务状态',
          facts: { preference_recorded: false }, ...(shadowReply ? { authoritative_reply: shadowReply } : {}), next_actions: ['respond'],
        };
      }
      const envelope = source?.envelope ?? {};
      const message = text(envelope.payload?.content ?? envelope.payload?.text, 120);
      const circled = /(?:红点|绿点|圈出|圈的|画的|标出|圈好|圈了|标好|标了)/u.test(message);
      if (circled && typeof conversationContextStore?.recordCircledDeliveryInstruction === 'function') {
        await conversationContextStore.recordCircledDeliveryInstruction(envelope.tenantId, envelope.payload ?? {}, latestSourceImageUrl(envelope.payload));
        return {
          status: 'success', tool: 'record_seat_preference', summary: '已记录按买家原图圈选位置出票的履约指令',
          facts: { circled_delivery_instruction_recorded: true },
          authoritative_reply: '已记录：出票时按您原图圈选的位置操作。若该位置届时不可选，会先联系您确认，不会擅自换座。', next_actions: ['respond'],
        };
      }
      if (typeof conversationContextStore?.recordSeatPreference !== 'function'
        || !await conversationContextStore.recordSeatPreference(envelope.tenantId, envelope.payload ?? {}, message)) {
        return { status: 'error', tool: 'record_seat_preference', summary: '位置偏好未能安全保存', facts: {}, next_actions: ['handoff'], stop_reason: 'seat_preference_store_unavailable' };
      }
      return {
        status: 'success', tool: 'record_seat_preference', summary: '已保存买家文字位置偏好，但未形成官方选座',
        facts: { preference_recorded: true },
        authoritative_reply: '已记录您的位置偏好，具体座位仍以出票时官方实时可选情况为准；当前未替您选座或锁座。', next_actions: ['respond'],
      };
    },
    async confirm_active_quote() {
      const snapshot = source?.result?.agent_reply_snapshot;
      const shadowReply = mode !== 'active' && snapshot?.kind === 'conversation_follow_up' ? text(snapshot.text, 1_000) : '';
      if (mode !== 'active') {
        return {
          status: 'success', tool: 'confirm_active_quote', summary: '影子模式仅评估报价确认请求',
          facts: { quote_confirmed: false }, ...(shadowReply ? { authoritative_reply: shadowReply } : {}), next_actions: ['respond'],
        };
      }
      if (typeof conversationContextStore?.markQuoteConfirmed !== 'function') {
        return { status: 'error', tool: 'confirm_active_quote', summary: '确定性报价确认门禁不可用', facts: {}, next_actions: ['handoff'], stop_reason: 'quote_confirmation_gate_unavailable' };
      }
      const envelope = source?.envelope ?? {};
      const confirmed = await conversationContextStore.markQuoteConfirmed(envelope.tenantId, envelope.payload ?? {});
      if (confirmed !== true) {
        return {
          status: 'warning', tool: 'confirm_active_quote', summary: '确定性报价确认门禁拒绝本次确认',
          facts: { quote_confirmed: false },
          authoritative_reply: '当前报价缺少有效规则版本或送达确认，不能进入下单流程。请发送最新完整选座页，我重新实时核价。',
          next_actions: ['respond'],
        };
      }
      return {
        status: 'success', tool: 'confirm_active_quote', summary: '确定性报价确认门禁已通过',
        facts: { quote_confirmed: true },
        authoritative_reply: '点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）\n提交订单后请先不要付款，等待系统确认改价成功后再付款。',
        next_actions: ['respond'],
      };
    },
    async create_manual_task() {
      if (mode !== 'active') return { status: 'error', tool: 'create_manual_task', summary: '影子模式不创建真实人工任务', facts: {}, next_actions: ['handoff'], stop_reason: 'shadow_write_tool_disabled' };
      if (!manualTaskStore) return { status: 'error', tool: 'create_manual_task', summary: '人工任务队列不可用', facts: {}, next_actions: ['handoff'], stop_reason: 'manual_task_tool_unavailable' };
      const envelope = source?.envelope ?? {}; const payload = envelope.payload ?? {};
      const created = await manualTaskStore.create({
        taskId: `agent:${String(envelope.tenantId)}:${String(envelope.id)}:manual`, tenantId: String(envelope.tenantId), eventId: String(envelope.id),
        accountUnb: String(payload.accountUnb ?? payload.account_unb ?? ''), chatId: String(payload.chatId ?? payload.chat_id ?? ''), peerUnb: String(payload.peerUnb ?? payload.peer_unb ?? ''),
        orderId: String(state?.facts?.order_id ?? ''), reasonCode: 'agent_requested_manual_review', summary: 'Agent请求人工处理', source: 'agent',
      });
      return { status: 'success', tool: 'create_manual_task', summary: created.created ? '已创建人工处理任务' : '人工处理任务已存在', facts: { manual_task_created: created.created }, authoritative_reply: '这个问题需要人工进一步确认，已记录处理，请稍候。', next_actions: ['respond'] };
    },
    async get_manual_task_status() {
      if (mode === 'evaluation') {
        return { status: 'error', tool: 'get_manual_task_status', summary: '历史事件缺少事件时点的人工任务快照', facts: {}, next_actions: ['handoff'], stop_reason: 'historical_manual_task_snapshot_unavailable' };
      }
      if (typeof manualTaskStore?.findLatestForConversation !== 'function') {
        return { status: 'error', tool: 'get_manual_task_status', summary: '人工任务状态读取工具不可用', facts: {}, next_actions: ['handoff'], stop_reason: 'manual_task_reader_unavailable' };
      }
      const envelope = source?.envelope ?? {};
      const payload = envelope.payload ?? {};
      const task = await manualTaskStore.findLatestForConversation(envelope.tenantId, {
        accountUnb: payload.accountUnb ?? payload.account_unb,
        chatId: payload.chatId ?? payload.chat_id,
        peerUnb: payload.peerUnb ?? payload.peer_unb,
      });
      if (!task) {
        return {
          status: 'success', tool: 'get_manual_task_status', summary: '当前会话没有人工处理任务',
          facts: { manual_task_found: false, manual_task_status: 'none', manual_task_resolved: false },
          authoritative_reply: '当前会话暂未查到人工处理任务；如果仍需人工核对，请告诉我具体问题。', next_actions: ['respond'],
        };
      }
      const taskStatus = ['open', 'in_progress', 'resolved'].includes(String(task.status)) ? String(task.status) : 'open';
      const replies = {
        open: '人工处理任务已记录，目前仍在排队处理中，请稍候。',
        in_progress: '人工客服正在处理这项任务，有结果后会继续通知您。',
        resolved: '人工处理记录已更新；是否已经出票仍以闲鱼订单状态和人工明确通知为准。',
      };
      return {
        status: 'success', tool: 'get_manual_task_status', summary: `已读取人工任务状态：${taskStatus}`,
        facts: { manual_task_found: true, manual_task_status: taskStatus, manual_task_resolved: taskStatus === 'resolved' },
        authoritative_reply: replies[taskStatus], next_actions: ['respond'],
      };
    },
    async read_linked_order() {
      let orderId = text(state?.facts?.order_id, 128);
      if (!orderId && mode !== 'evaluation' && typeof conversationContextStore?.get === 'function') {
        const envelope = source?.envelope ?? {};
        const liveState = await conversationContextStore.get(envelope.tenantId, envelope.payload ?? {});
        orderId = text(liveState?.facts?.order_id, 128);
        if (orderId && state?.facts && typeof state.facts === 'object') state.facts.order_id = orderId;
      }
      if (!orderId) return { status: 'error', tool: 'read_linked_order', summary: '未找到系统关联订单', facts: {}, next_actions: ['create_manual_task'], stop_reason: 'linked_order_missing' };
      if (typeof coreFor !== 'function') return { status: 'error', tool: 'read_linked_order', summary: '权威订单读取工具不可用', facts: {}, next_actions: ['create_manual_task'], stop_reason: 'order_reader_unavailable' };
      const core = coreFor(String(source?.envelope?.tenantId ?? ''));
      if (typeof core?.orders?.get !== 'function') return { status: 'error', tool: 'read_linked_order', summary: '权威订单读取工具不可用', facts: {}, next_actions: ['create_manual_task'], stop_reason: 'order_reader_unavailable' };
      const order = await core.orders.get(orderId);
      const lifecycle = linkedOrderLifecycle(order);
      if (lifecycle === 'unknown') return { status: 'error', tool: 'read_linked_order', summary: '暂时无法确认闲鱼订单状态', facts: { has_linked_order: true, lifecycle }, next_actions: ['create_manual_task'], stop_reason: 'order_status_unknown' };
      const facts = { has_linked_order: true, lifecycle, paid: ['paid', 'shipped', 'completed'].includes(lifecycle), fulfilled: ['shipped', 'completed'].includes(lifecycle) };
      return { status: 'success', tool: 'read_linked_order', summary: `已读取闲鱼权威订单状态：${lifecycle}`, facts, authoritative_reply: linkedOrderReply(lifecycle), next_actions: ['respond'] };
    },
  };
}

function boundedIdentity(recognition = {}) {
  return Object.fromEntries(['city', 'cinema', 'movie', 'date', 'showtime', 'hall']
    .map((key) => [key, text(recognition?.[key], key === 'cinema' || key === 'movie' ? 160 : 80)])
    .filter(([, value]) => value));
}

function compactQuoteInput(value = {}) {
  const candidate = {
    status: value.status === 'resolved' ? 'resolved' : 'recognized',
    tenant_id: text(value.tenant_id, 128),
    ticket_count: safePositiveInteger(value.ticket_count),
    recognition: value.recognition && typeof value.recognition === 'object' && !Array.isArray(value.recognition) ? structuredClone(value.recognition) : null,
    ...(value.text_quote === true ? { text_quote: true } : {}),
  };
  if (!candidate.tenant_id || !candidate.recognition) throw new TypeError('invalid recognition artifact');
  if (JSON.stringify(candidate).length > 5_000) throw new TypeError('recognition artifact exceeds durable bound');
  return candidate;
}

function candidateIndexFromMessage(value) {
  const message = text(value, 80).replace(/\s+/gu, '');
  const chinese = { '第一个': 1, '第一个店': 1, '第二个': 2, '第二个店': 2, '第三个': 3, '第三个店': 3, '第四个': 4, '第四个店': 4, '第五个': 5, '第五个店': 5 };
  if (chinese[message]) return chinese[message];
  const matched = message.match(/^第?([1-5])个(?:店)?$/u);
  return matched ? Number(matched[1]) : null;
}

function safePositiveInteger(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

function safeNonnegativeInteger(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

function quoteDeliverySnapshot(quoted = {}) {
  const total = safePositiveInteger(quoted.total_quote_cents); const count = safePositiveInteger(quoted.ticket_count);
  const pricingRuleVersion = text(quoted.pricing_rule_version, 80);
  if (!total || !count || !pricingRuleVersion) throw new TypeError('authoritative quote is missing delivery evidence');
  const recognition = quoted.recognition ?? {};
  const seatQuotes = Array.isArray(quoted.seat_quotes) ? quoted.seat_quotes.slice(0, 20) : [];
  const originalPrices = seatQuotes.map((item) => safePositiveInteger(item?.original_price_cents));
  const memberPrices = seatQuotes.map((item) => safePositiveInteger(item?.member_price_cents));
  const channelFees = seatQuotes.map((item) => safeNonnegativeInteger(item?.channel_fee_cents));
  const originalPriceTotal = seatQuotes.length && originalPrices.every(Boolean) ? originalPrices.reduce((sum, value) => sum + value, 0) : null;
  const memberUnitPrice = safePositiveInteger(quoted.member_unit_price_cents);
  const memberCostTotal = seatQuotes.length && memberPrices.every(Boolean) ? memberPrices.reduce((sum, value) => sum + value, 0) : memberUnitPrice ? memberUnitPrice * count : null;
  const channelFeeTotal = safeNonnegativeInteger(quoted.channel_fee_total_cents)
    ?? (seatQuotes.length && channelFees.every((value) => value != null) ? channelFees.reduce((sum, value) => sum + value, 0) : null);
  const pricingAccountRef = /^[a-f0-9]{32}$/u.test(String(quoted.pricing_account_ref ?? '')) ? String(quoted.pricing_account_ref) : '';
  return {
    type: 'quote', unit_quote_cents: safePositiveInteger(quoted.unit_quote_cents), total_quote_cents: total, ticket_count: count,
    pricing_rule_version: pricingRuleVersion, cinema: text(recognition.cinema, 160), movie: text(recognition.movie, 160), date: text(recognition.date, 32),
    showtime: text(recognition.showtime, 32), hall: text(recognition.hall, 80), quote_scope: ['area_probe', 'exact_seats'].includes(String(quoted.quote_scope)) ? String(quoted.quote_scope) : '',
    member_cost_total_cents: safePositiveInteger(quoted.member_cost_total_cents) ?? memberCostTotal,
    original_price_total_cents: safePositiveInteger(quoted.original_price_total_cents) ?? originalPriceTotal,
    channel_fee_total_cents: channelFeeTotal,
    pricing_source: text(quoted.pricing_source, 100),
    ...(pricingAccountRef ? { pricing_account_ref: pricingAccountRef } : {}),
  };
}

function sourcePlatformMessageId(payload = {}) {
  return text(payload.remoteMessageId ?? payload.remote_message_id ?? payload.messageId ?? payload.message_id, 240);
}

function activeQuoteClarificationReply(state, message, nowValue) {
  const facts = state?.facts ?? {};
  const unit = safePositiveInteger(facts.quote_unit_cents ?? facts.unit_quote_cents);
  const total = safePositiveInteger(facts.quote_total_cents ?? facts.total_quote_cents);
  const count = safePositiveInteger(facts.quote_ticket_count ?? facts.ticket_count);
  const expiresAt = safePositiveInteger(facts.quote_expires_at);
  const active = ['quoted', 'quote_confirmed', 'waiting_payment'].includes(String(facts.stage ?? ''))
    && unit && total && count && expiresAt && expiresAt > nowValue;
  if (!active || !/(?:灰色|W\+|W座|能买吗|代买|买到|这些?位置|这几个位置)/iu.test(String(message ?? ''))) return '';
  return `系统刚才已通过万达实时核验，当前有效报价是${(unit / 100).toFixed(2)}元/张，${count}张合计${(total / 100).toFixed(2)}元。座位状态可能变化；需要按这份报价购买请回复“确认”。`;
}

async function createFallbackManualTask(manualTaskStore, source, orderId = '') {
  if (!manualTaskStore) return false;
  const envelope = source?.envelope ?? {}; const payload = envelope.payload ?? {};
  await manualTaskStore.create({
    taskId: `agent:${String(envelope.tenantId)}:${String(envelope.id)}:fallback`, tenantId: String(envelope.tenantId), eventId: String(envelope.id),
    accountUnb: String(payload.accountUnb ?? payload.account_unb ?? ''), chatId: String(payload.chatId ?? payload.chat_id ?? ''), peerUnb: String(payload.peerUnb ?? payload.peer_unb ?? ''),
    orderId: String(orderId ?? ''), reasonCode: 'agent_runtime_handoff', summary: 'Agent执行未安全完成，需要人工处理', source: 'agent_runtime',
  });
  return true;
}

function linkedOrderLifecycle(order = {}) {
  const status = Number(order?.orderStatus ?? order?.order_status ?? order?.status);
  const label = text(order?.orderStatusText ?? order?.order_status_text ?? order?.statusText, 120);
  if (/(?:交易关闭|已关闭|已取消|退款成功|已退款)/u.test(label)) return 'closed';
  if (status === 4 || /(?:交易成功|交易完成)/u.test(label)) return 'completed';
  if (status === 3 || /(?:已发货|等待买家收货|待收货)/u.test(label)) return 'shipped';
  if (status === 2 || /(?:买家已付款|等待卖家发货|待发货)/u.test(label)) return 'paid';
  if (status === 1 || /(?:待付款|等待买家付款|未付款)/u.test(label)) return 'unpaid';
  return 'unknown';
}

function linkedOrderReply(lifecycle) {
  if (lifecycle === 'unpaid') return '闲鱼订单显示尚未付款。请先不要付款，等待系统确认改价成功后再付款。';
  if (lifecycle === 'paid') return '闲鱼订单显示已付款，正在等待人工出票处理，请勿重复付款。';
  if (lifecycle === 'shipped') return '闲鱼订单显示已发货，请在订单内查看票务信息。';
  if (lifecycle === 'completed') return '闲鱼订单显示交易已完成，请在订单内查看票务信息。';
  return '闲鱼订单已关闭或取消，无法继续原订单流程。如仍需购票，请重新发送当前选座页截图。';
}

export function createShadowAgentRuntime({ runStore, eventStore, conversationContextStore, planner, getSettings, replyOutboxStore = null, manualTaskStore = null, coreFor = null, quotePreviewClient = null, logger = console, maxSteps = 8, leaseMs = 60_000, heartbeatMs = 15_000, deadlineMs = 180_000, now = Date.now } = {}) {
  if (!runStore || !eventStore || !conversationContextStore || !planner || typeof getSettings !== 'function') throw new TypeError('shadow agent runtime dependencies are required');
  const activeControllers = new Set();

  async function schedule(envelope, { mode = 'shadow', contextSnapshot = null } = {}) {
    if (envelope?.event !== 'im.message.received') return { created: false, run: null };
    if (!['shadow', 'active', 'evaluation'].includes(mode)) throw new TypeError('invalid durable agent mode');
    if (mode === 'active' && !replyOutboxStore) throw new TypeError('active agent reply outbox is required');
    const sourceContext = mode === 'evaluation'
      ? null
      : contextSnapshot ?? await sourceTimeState(envelope, conversationContextStore);
    return runStore.enqueue({ runId: runIdFor(envelope, mode), eventKey: eventKey(envelope), tenantId: envelope.tenantId, mode, deadlineMs, contextSnapshot: sourceContext });
  }

  async function tick() {
    const run = await runStore.claimDue({ leaseMs });
    if (!run) return null;
    const heartbeat = startLeaseHeartbeat(runStore, run, { leaseMs, heartbeatMs });
    const controller = new AbortController();
    activeControllers.add(controller);
    try {
      const source = await eventStore.get(run.event_key);
      if (!source || ['queued', 'retry', 'processing'].includes(source.status)) {
        await heartbeat.stop();
        await runStore.defer(run.run_id, run.lease_id, { delayMs: 2_000, reason: 'source_event_pending' });
        return { status: 'deferred', run_id: run.run_id };
      }
      if (source.status !== 'completed') {
        await heartbeat.stop();
        await runStore.complete(run.run_id, run.lease_id, withSourceSnapshot({ runtime_version: AGENT_RUNTIME_VERSION, status: 'skipped', reason: `source_event_${source.status}`, trace: run.trace }, source));
        return { status: 'completed', run_id: run.run_id };
      }
      if (run.mode === 'active' && source.result?.execution_owner !== 'agent') {
        const result = { runtime_version: AGENT_RUNTIME_VERSION, status: 'skipped', reason: 'execution_owner_mismatch', trace: run.trace, reply_generated: false, reply_queued: false, authoritative_outcome: 'not_available' };
        await heartbeat.stop();
        const persistedResult = withSourceSnapshot(result, source);
        await runStore.complete(run.run_id, run.lease_id, persistedResult);
        return { status: 'completed', run_id: run.run_id, result: persistedResult };
      }
      const unknownTool = Array.isArray(run.tool_calls) ? run.tool_calls.find((call) => call?.status === 'pending') : null;
      if (unknownTool) {
        let replyQueued = false;
        if (run.mode === 'active' && await createFallbackManualTask(manualTaskStore, source)) {
          const payload = source.envelope?.payload ?? {};
          await replyOutboxStore.enqueue({
            actionId: `${run.run_id}:fallback`, runId: run.run_id, tenantId: run.tenant_id, mode: 'active',
            accountUnb: String(payload.accountUnb ?? payload.account_unb ?? ''), chatId: String(payload.chatId ?? payload.chat_id ?? ''), peerUnb: String(payload.peerUnb ?? payload.peer_unb ?? ''),
            ...(sourcePlatformMessageId(payload) ? { sourceMessageId: sourcePlatformMessageId(payload) } : {}), text: '这个问题需要人工进一步确认，已记录处理，请稍候。',
          });
          replyQueued = true;
        }
        const result = {
          runtime_version: AGENT_RUNTIME_VERSION,
          status: 'handoff', reason: 'agent_tool_result_unknown', trace: run.trace,
          reply_generated: replyQueued, ...(run.mode === 'active' ? { reply_queued: replyQueued } : {}), authoritative_outcome: 'not_available',
        };
        await heartbeat.stop();
        const persistedResult = withSourceSnapshot(result, source);
        await runStore.complete(run.run_id, run.lease_id, persistedResult);
        return { status: 'completed', run_id: run.run_id, result: persistedResult };
      }
      const payload = source.envelope?.payload ?? {};
      const settings = await getSettings(source.envelope.tenantId);
      const state = run.mode === 'evaluation'
        ? sourceStateSnapshot(source)
        : run.context_snapshot ?? await sourceTimeState(source.envelope, conversationContextStore);
      const executionState = state;
      const context = {
        event_id: String(source.envelope.id), tenant_id: String(source.envelope.tenantId),
        latest_message: text(payload.content ?? payload.text) || '[图片或非文本消息]', has_image: hasImage(payload),
        settings, state: state ?? { facts: {}, messages: [] }, observations: run.observations, trace: run.trace, mode: run.mode,
        now: run.mode === 'evaluation' && Number.isSafeInteger(Number(source?.result?.agent_state_snapshot?.observed_at))
          ? Number(source.result.agent_state_snapshot.observed_at) : now(),
        human_takeover: false, signal: controller.signal,
      };
      const agent = createConversationAgent({
        planner, tools: runtimeTools(source, executionState, run.mode, conversationContextStore, manualTaskStore, coreFor, quotePreviewClient, run.observations),
        maxSteps, mode: run.mode, allowWriteSimulation: run.mode !== 'active',
      });
      const outcome = await agent.runTurn(context, {
        onToolStart: (call) => runStore.beginTool(run.run_id, run.lease_id, {
          callId: `tool:${call.step}:${call.tool}`, step: call.step, tool: call.tool,
          trace: call.trace, observations: call.observations,
        }, { leaseMs }),
        onToolFinish: (call, observation) => runStore.completeTool(
          run.run_id, run.lease_id, `tool:${call.step}:${call.tool}`, observation, { leaseMs },
        ),
        onCheckpoint: (checkpoint) => runStore.checkpoint(run.run_id, run.lease_id, checkpoint, { leaseMs }),
        shouldContinue: () => now() < Number(run.deadline_at),
      });
      await heartbeat.stop();
      let replyQueued = false;
      let queuedReply = outcome.status === 'reply' && typeof outcome.reply === 'string' ? outcome.reply.trim() : '';
      let actionSuffix = 'reply';
      const persistedRun = run.mode === 'active' && typeof runStore.get === 'function' ? await runStore.get(run.run_id) : run;
      if (run.mode === 'active' && outcome.status === 'handoff' && await createFallbackManualTask(manualTaskStore, source, context.state?.facts?.order_id)) {
        const message = payload.content ?? payload.text ?? '';
        const quoteClarification = activeQuoteClarificationReply(context.state, message, now());
        const attemptedAuthorityTool = Array.isArray(persistedRun?.tool_calls) && persistedRun.tool_calls.length > 0;
        queuedReply = quoteClarification || (attemptedAuthorityTool ? '这个问题需要人工进一步确认，已记录处理，请稍候。' : '');
        actionSuffix = quoteClarification ? 'quote-clarification-fallback' : 'fallback';
      }
      if (run.mode === 'active' && queuedReply) {
        const payload = source.envelope?.payload ?? {};
        const delivery = actionSuffix === 'reply'
          ? [...(persistedRun?.observations ?? [])].reverse().find((item) => item?.facts?._quote_delivery)?.facts?._quote_delivery ?? null
          : null;
        await replyOutboxStore.enqueue({
          actionId: `${run.run_id}:${actionSuffix}`, runId: run.run_id, tenantId: run.tenant_id, mode: 'active',
          accountUnb: String(payload.accountUnb ?? payload.account_unb ?? ''), chatId: String(payload.chatId ?? payload.chat_id ?? ''), peerUnb: String(payload.peerUnb ?? payload.peer_unb ?? ''),
          ...(sourcePlatformMessageId(payload) ? { sourceMessageId: sourcePlatformMessageId(payload) } : {}), text: queuedReply, ...(delivery ? { delivery } : {}),
        });
        replyQueued = true;
      }
      const result = {
        runtime_version: AGENT_RUNTIME_VERSION,
        status: outcome.status, reason: outcome.reason, trace: outcome.trace,
        reply_generated: Boolean(queuedReply) || (typeof outcome.reply === 'string' && outcome.reply.length > 0),
        authoritative_reply_used: outcome.reason === 'authoritative_tool_response',
        ...(comparisonReply(outcome.reply) ? { proposed_reply: comparisonReply(outcome.reply) } : {}),
        ...(run.mode === 'active' ? { reply_queued: replyQueued } : {}),
        authoritative_outcome: source.result?.preview_status === 'preview_ready' ? 'quote_succeeded' : source.result?.quote_failure_code ? 'quote_failed' : 'not_available',
      };
      if (outcome.reason === 'agent_deadline_exceeded') {
        const persistedResult = withSourceSnapshot(result, source);
        await runStore.timeout(run.run_id, run.lease_id, persistedResult);
        return { status: 'timed_out', run_id: run.run_id, result: persistedResult };
      }
      const persistedResult = withSourceSnapshot(result, source);
      await runStore.complete(run.run_id, run.lease_id, persistedResult);
      return { status: 'completed', run_id: run.run_id, result: persistedResult };
    } catch (error) {
      let failure = error;
      try { await heartbeat.stop(); } catch (heartbeatError) { failure = heartbeatError; }
      if (controller.signal.aborted) {
        try {
          await runStore.defer(run.run_id, run.lease_id, { delayMs: 1_000, reason: 'agent_runtime_stopping' });
          return { status: 'deferred', run_id: run.run_id };
        } catch (leaseError) {
          if (/agent run lease mismatch/u.test(String(leaseError?.message ?? ''))) return { status: 'lease_lost', run_id: run.run_id };
          throw leaseError;
        }
      }
      logger.warn?.('[shadow-agent] durable run failed', { runId: run.run_id, error: String(failure?.message ?? failure) });
      try {
        await runStore.retry(run.run_id, run.lease_id, failure, { delayMs: 5_000, maxAttempts: 4 });
        return { status: 'retry', run_id: run.run_id };
      } catch (leaseError) {
        if (/agent run lease mismatch/u.test(String(leaseError?.message ?? ''))) return { status: 'lease_lost', run_id: run.run_id };
        throw leaseError;
      }
    } finally {
      activeControllers.delete(controller);
    }
  }

  function stop() {
    for (const controller of activeControllers) controller.abort(new Error('agent_runtime_stopping'));
  }

  return Object.freeze({ schedule, tick, stop });
}

function startLeaseHeartbeat(runStore, run, { leaseMs, heartbeatMs }) {
  let stopped = false;
  let heartbeatError = null;
  let pending = Promise.resolve();
  const interval = setInterval(() => {
    if (stopped) return;
    pending = pending.then(() => runStore.renewLease(run.run_id, run.lease_id, { leaseMs })).catch((error) => {
      heartbeatError = error;
    });
  }, Math.max(10, Number(heartbeatMs)));
  interval.unref?.();
  return Object.freeze({
    async stop() {
      if (!stopped) { stopped = true; clearInterval(interval); }
      await pending;
      if (heartbeatError) throw heartbeatError;
    },
  });
}
