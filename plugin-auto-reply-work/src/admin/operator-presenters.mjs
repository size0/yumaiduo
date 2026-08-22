export function runtimeToUiSettings(runtime) {
  return Object.freeze({
    ...runtime,
    automation_enabled: runtime.automation_enabled === true,
    recognition_enabled: runtime.recognition_enabled !== false,
    quote_enabled: runtime.quote_enabled !== false,
    price_change_enabled: runtime.auto_price_change === true,
    ai_reply_enabled: runtime.ai_reply_enabled === true,
    conversation_agent_mode: ['off', 'shadow', 'active'].includes(runtime.conversation_agent_mode) ? runtime.conversation_agent_mode : 'shadow',
    minimum_confidence: Number(runtime.low_confidence_threshold ?? 0.9),
    ai_base_url: String(runtime.ai_reply_base_url ?? ''),
    ai_model: String(runtime.ai_reply_model ?? ''),
    ai_key_configured: runtime.ai_reply_key_configured === true,
    ai_key_masked: String(runtime.ai_reply_api_key_masked ?? ''),
    updated_at: runtime.updated_at ?? null,
  });
}

export function runtimePatchFromUi(patch) {
  const result = {};
  if (Object.hasOwn(patch, 'automation_enabled')) result.automation_enabled = patch.automation_enabled === true;
  if (Object.hasOwn(patch, 'recognition_enabled')) result.recognition_enabled = patch.recognition_enabled === true;
  if (Object.hasOwn(patch, 'quote_enabled')) result.quote_enabled = patch.quote_enabled === true;
  if (Object.hasOwn(patch, 'price_change_enabled')) result.auto_price_change = patch.price_change_enabled === true;
  if (Object.hasOwn(patch, 'minimum_confidence')) result.low_confidence_threshold = Number(patch.minimum_confidence);
  if (Object.hasOwn(patch, 'ai_reply_enabled')) {
    result.ai_reply_enabled = patch.ai_reply_enabled === true;
  }
  if (Object.hasOwn(patch, 'ai_base_url')) result.ai_reply_base_url = patch.ai_base_url;
  if (Object.hasOwn(patch, 'ai_model')) result.ai_reply_model = patch.ai_model;
  if (Object.hasOwn(patch, 'ai_api_key')) result.ai_reply_api_key = patch.ai_api_key;
  if (patch.clear_ai_api_key === true) result.ai_reply_clear_api_key = true;
  for (const field of [
    'ai_only_mode_enabled',
    'conversation_agent_mode',
    'ai_reply_system_prompt',
    'ai_reply_shop_background',
    'ai_reply_precautions',
    'ai_reply_style',
    'ai_reply_temperature',
    'ai_reply_timeout_seconds',
    'ai_reply_fallback',
    'ai_reply_daily_limit',
    'ai_reply_cooldown_seconds',
    'ai_reply_memory_hours',
    'ai_reply_memory_depth',
    'ai_reply_delay_seconds',
    'ai_reply_manual_takeover_seconds',
    'shop_execution_modes',
    'shop_automation_overrides',
    'shop_feature_overrides',
    'reply_templates',
    'reply_template_images',
  ]) {
    if (Object.hasOwn(patch, field)) result[field] = patch[field];
  }
  return result;
}

export function imageSignatureMatches(bytes, contentType) {
  if (contentType === 'image/png') return bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex'));
  if (contentType === 'image/jpeg') return bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  if (contentType === 'image/webp') return bytes.subarray(0, 4).toString('ascii') === 'RIFF' && bytes.subarray(8, 12).toString('ascii') === 'WEBP';
  return false;
}

export function pricingAccountEvidenceSummary(records = []) {
  const values = Array.isArray(records) ? records : [];
  const refs = values
    .map((record) => String(record?.pricing_account_ref ?? '').trim())
    .filter((value) => /^[a-f0-9]{32}$/u.test(value));
  return Object.freeze({
    pricing_account_evidence_count: refs.length,
    pricing_account_unknown_count: Math.max(0, values.length - refs.length),
    pricing_account_count: new Set(refs).size,
  });
}

export function pricingFormulaSummary(settings = {}) {
  const wplusAdjustment = Number(settings.wplus_adjustment_cents ?? -290);
  const threshold = Number(settings.wplus_member_price_threshold_cents ?? 6000);
  const regularAdjustment = Number(settings.regular_adjustment_cents ?? 100);
  return `W+区：会员价不高于${(threshold / 100).toFixed(2)}元时，取会员价与实时原价${wplusAdjustment < 0 ? '下调' : '上调'}${(Math.abs(wplusAdjustment) / 100).toFixed(2)}元的较高值；普通区：会员价加${(regularAdjustment / 100).toFixed(2)}元。最终按0.1元轮整，并受会员成本下限和实时原价上限约束。`;
}

export function uiApiError(status, code) {
  const error = new Error(code);
  error.status = status;
  error.code = code;
  return error;
}

export function ticketIssuanceFromXianyuOrder(input = {}) {
  if (input.read_status !== 'available') return Object.freeze({ status: 'unknown', evidence: '闲鱼订单读取失败' });
  const status = Number(input.order_status);
  const text = compactText(input.order_status_text, 120);
  if (status === 4 || /(?:交易成功|交易完成)/u.test(text)) {
    return Object.freeze({ status: 'issued', evidence: '闲鱼交易完成' });
  }
  if (status === 3 || /(?:已发货|待买家收货|等待买家收货|待收货)/u.test(text)) {
    return Object.freeze({ status: 'issued', evidence: '闲鱼已发货' });
  }
  return Object.freeze({ status: 'not_confirmed', evidence: '闲鱼尚未发货' });
}

export function nonNegativeCentsOrNull(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

export function compactText(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

export function agentEvaluationTurnSummary(payload = {}) {
  const imageCount = Array.isArray(payload.imageUrls)
    ? payload.imageUrls.filter((value) => typeof value === 'string' && value.trim()).length
    : 0;
  const text = compactText(payload.content ?? payload.text, 120)
    .replace(/https?:\/\/[^\s，。；！？,;]+/giu, '[链接]')
    .replace(/(?<!\d)1\d{10}(?!\d)/gu, '[手机号]')
    .replace(/(?<!\d)\d{8,}(?!\d)/gu, '[编号]');
  if (text && imageCount) return `买家说：${text}；并发送${imageCount}张图片`;
  if (text) return `买家说：${text}`;
  if (imageCount) return `买家发送${imageCount}张图片`;
  return '本轮没有可展示的买家文字或图片';
}

export function agentEvaluationBuyerLabel(payload = {}) {
  const name = safeBuyerName(payload);
  if (name) return name;
  const peer = compactText(payload.peerUnb, 128);
  return peer ? `买家 …${peer.slice(-4)}` : '买家（身份未知）';
}

export function agentEvaluationAuthoritativeSummary(result = {}) {
  if (result.preview_status === 'preview_ready') return '确定性系统：已形成实时报价';
  if (result.quote_failure_code) return eventDiagnosticSummary(result);
  const statuses = (Array.isArray(result.actions) ? result.actions : []).map((item) => String(item?.status ?? ''));
  if (statuses.includes('succeeded')) return '确定性系统：已按既有规则完成本轮回复';
  if (statuses.includes('skipped')) return '确定性系统：本轮已安全跳过';
  return '确定性系统：本轮没有形成可对照的交易结果';
}

export function toRecentRecord(record) {
  const payload = record.envelope?.payload ?? {};
  return {
    id: record.key,
    event: record.envelope?.event,
    status: record.status,
    updated_at: record.updatedAt,
    account_unb: payload.accountUnb ?? null,
    chat_id: payload.chatId ?? null,
    peer_unb: payload.peerUnb ?? null,
    order_id: payload.orderId ?? null,
    summary: safeSummary(payload.content ?? payload.text ?? record.envelope?.event),
    result: record.result ?? null,
    error: record.lastError ?? null,
  };
}

export function manualTaskReviewRecord(task) {
  return {
    id: `manual:${task.task_id}`,
    manual_task_id: task.task_id,
    event: 'agent.manual_task',
    status: 'failed',
    updated_at: task.updated_at,
    account_unb: task.account_unb,
    chat_id: task.chat_id,
    peer_unb: task.peer_unb,
    order_id: task.order_id,
    summary: task.summary,
    error: task.reason_code,
  };
}

export function matchesOperationEvent(operation, record) {
  const payload = record.envelope?.payload ?? {};
  const sameChat = String(payload.accountUnb ?? '') === String(operation.account_unb ?? '')
    && String(payload.chatId ?? '') === String(operation.chat_id ?? '');
  const sameOrder = operation.order_id && String(payload.orderId ?? '') === String(operation.order_id);
  return sameChat || sameOrder;
}

export function safeBuyerName(payload) {
  return [
    payload?.peerNick, payload?.peerNickname, payload?.peer_nick,
    payload?.buyerNick, payload?.buyerNickname, payload?.buyer_nick, payload?.buyerName,
  ].map((value) => String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, 80)).find(Boolean) ?? '';
}

export function toOperationActivity(record) {
  const result = record.result ?? {};
  const actions = Array.isArray(result.actions) ? result.actions : [];
  const actionSummary = actions.map((action) => String(action.status ?? '')).filter(Boolean).join('、');
  return Object.freeze({
    event: String(record.envelope?.event ?? ''),
    status: String(record.status ?? ''),
    updated_at: record.updatedAt ?? null,
    summary: safeSummary(
      actionSummary
      || result.reason
      || result.quote_skipped
      || quoteFailureSummary(result.quote_failure_code)
      || result.preview_status
      || record.lastError
      || record.envelope?.event,
    ),
  });
}

const WAITING_INPUT_QUOTE_CODES = new Set([
  'text_quote_missing_fields', 'cinema_catalog_not_unique', 'showtime_not_unique', 'showtime_not_found', 'need_image',
  'official_selection_unverifiable',
]);
const BUSINESS_BLOCKED_QUOTE_CODES = new Set([
  'wplus_area_unavailable', 'wplus_seats_unavailable', 'wplus_price_unavailable', 'insufficient_available_seats',
  'quote_price_conflict', 'ticket_count_conflict', 'non_wanda_cinema',
]);

function quoteFailureSummary(code) {
  return ({
    text_quote_missing_fields: '文字询价信息不完整',
    need_image: '需要完整选座页截图',
    wplus_area_unavailable: 'W+ 区域无法可靠核验',
    wplus_seats_unavailable: '当前场次没有可用 W+ 座位',
    wplus_account_unavailable: 'W+ 核价账号暂不可用',
    wplus_price_unavailable: '未找到可用 W+ 优惠价',
    insufficient_available_seats: '没有足够的同类可用座位',
    showtime_not_unique: '截图信息无法唯一匹配场次',
    showtime_not_found: '万达官方场次中未找到该日期和时间',
    cinema_catalog_not_unique: '影院无法在官方库唯一匹配',
    wanda_gateway_unavailable: '万达实时接口暂不可用',
    temporary_lock_failed: '临时试价订单创建失败',
    temporary_lock_release_unverified: '临时试价座位释放未确认',
    official_selection_unverifiable: '官方已选座无法逐座核验',
    quote_price_conflict: '会员成本与实时原价冲突',
    quote_verification_failed: '实时核价未通过',
  })[String(code ?? '')] ?? '';
}

export function conversationLearningSummaryFrom(events = [], entries = []) {
  const agentEvents = (Array.isArray(events) ? events : []).filter((item) => {
    const mode = String(item?.result?.conversation_agent_mode ?? '');
    return ['shadow', 'active'].includes(mode) && item?.result?.agent_turn_status;
  });
  const experiences = (Array.isArray(entries) ? entries : [])
    .filter((item) => item?.source === 'conversation_experience');
  const latestTimestamp = agentEvents.reduce((latest, item) => {
    const timestamp = new Date(item?.updatedAt ?? item?.envelope?.ts ?? 0).getTime();
    return Number.isFinite(timestamp) ? Math.max(latest, timestamp) : latest;
  }, 0);
  const evidenceCount = experiences.reduce((total, item) => {
    const count = Number(item?.evidence_count ?? 0);
    return total + (Number.isSafeInteger(count) && count > 0 ? count : 0);
  }, 0);
  return Object.freeze({
    model_training_enabled: false,
    automatic_activation_enabled: false,
    observed_turn_count: agentEvents.length,
    shadow_turn_count: agentEvents.filter((item) => item.result.conversation_agent_mode === 'shadow').length,
    active_turn_count: agentEvents.filter((item) => item.result.conversation_agent_mode === 'active').length,
    agent_failure_count: agentEvents.filter((item) => item.result.agent_turn_status === 'failed').length,
    experience_draft_count: experiences.filter((item) => item.status === 'draft').length,
    experience_approved_count: experiences.filter((item) => item.status === 'approved').length,
    experience_enabled_count: experiences.filter((item) => item.status === 'approved' && item.enabled === true).length,
    experience_evidence_count: evidenceCount,
    last_observed_at: latestTimestamp > 0 ? new Date(latestTimestamp).toISOString() : null,
  });
}

export function toLogRecord(record) {
  const result = record.result && typeof record.result === 'object' ? record.result : {};
  return {
    id: record.key,
    time: record.updatedAt,
    event: record.envelope?.event,
    status: operationalLogStatus(record.status, result),
    attempts: record.attempts,
    error: record.lastError ?? null,
    diagnostic: eventDiagnosticSummary(result),
  };
}

export function operationalLogStatus(storedStatus, result) {
  const priceStatus = String(result?.order_price_change?.status ?? '');
  if (['rejected', 'blocked'].includes(priceStatus)) return 'failed';
  if (priceStatus === 'unknown') return 'unknown';
  const quoteCode = String(result?.quote_failure_code ?? result?.quote_diagnostics?.safe_error_code ?? '');
  if (WAITING_INPUT_QUOTE_CODES.has(quoteCode)) return 'waiting_input';
  if (BUSINESS_BLOCKED_QUOTE_CODES.has(quoteCode)) return 'business_blocked';
  if (quoteCode) return 'failed';
  const skippedAction = Array.isArray(result?.actions)
    ? result.actions.find((item) => item?.status === 'skipped')
    : null;
  if (skippedAction) {
    return skippedAction.reason === 'price_change_gate_failed' ? 'business_blocked' : 'skipped';
  }
  return String(storedStatus ?? '');
}

export function eventDiagnosticSummary(result) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return '';
  const price = result.order_price_change;
  if (price && typeof price === 'object' && !Array.isArray(price)) {
    const diagnostics = price.diagnostics && typeof price.diagnostics === 'object' ? price.diagnostics : {};
    const status = String(price.status ?? '');
    const statusLabel = ({ submitted: '改价已提交', succeeded: '改价成功', rejected: '改价拒绝', blocked: '改价阻止', unknown: '改价结果未知' })[status] ?? '改价处理';
    const current = moneyLabel(diagnostics.current_total_cents);
    const target = moneyLabel(diagnostics.target_total_cents ?? price.amount_cents);
    const direction = ({ increase: '涨价', decrease: '降价', unchanged: '不变' })[diagnostics.direction];
    return safeSummary([
      `${statusLabel}${price.code ? ` ${safeSummary(price.code)}` : ''}`,
      current && target ? `金额 ${current}→${target}元` : target ? `目标 ${target}元` : '',
      direction ? `方向 ${direction}` : '',
      Number.isInteger(diagnostics.http_status) ? `HTTP ${diagnostics.http_status}` : '',
      diagnostics.provider_reason_code ? `上游 ${safeSummary(diagnostics.provider_reason_code)}` : '',
    ].filter(Boolean).join('；'));
  }
  if (result.quote_diagnostics) {
    const code = String(result.quote_diagnostics?.safe_error_code ?? result.quote_failure_code ?? '');
    const prefix = WAITING_INPUT_QUOTE_CODES.has(code)
      ? '待买家补充'
      : BUSINESS_BLOCKED_QUOTE_CODES.has(code) ? '安全停止' : '核价异常';
    return `${prefix}；${quoteDiagnosticSummary(result.quote_diagnostics)}`;
  }
  if (result.quote_failure_code) {
    const code = String(result.quote_failure_code);
    const detail = quoteFailureSummary(code) || safeSummary(code);
    if (WAITING_INPUT_QUOTE_CODES.has(code)) return `待买家补充：${detail}`;
    if (BUSINESS_BLOCKED_QUOTE_CODES.has(code)) return `安全停止：${detail}`;
    return `核价异常：${detail}`;
  }
  const action = Array.isArray(result.actions)
    ? result.actions.find((item) => item && (item.status === 'skipped' || item.status === 'rejected' || item.status === 'blocked'))
    : null;
  if (action?.status === 'skipped' && action.reason) {
    const reason = ({ human_takeover: '人工接管', price_change_gate_failed: '改价门禁未通过' })[action.reason] ?? safeSummary(action.reason);
    return `跳过：${reason}`;
  }
  if (result.price_changed_notified === true && Number.isInteger(result.verified_amount_cents)) {
    return `改价结果已核验：${moneyLabel(result.verified_amount_cents)}元`;
  }
  if (result.conversation_agent_mode) {
    const shadow = result.conversation_agent_mode === 'shadow';
    const confidence = Number(result.agent_confidence);
    const confidenceLabel = Number.isFinite(confidence) && confidence >= 0 && confidence <= 1 ? `（置信度${Math.round(confidence * 100)}%）` : '';
    const intent = agentIntentLabel(result.agent_intent);
    const actionLabels = Array.isArray(result.agent_actions)
      ? result.agent_actions.map(agentActionLabel).filter(Boolean)
      : [];
    const action = actionLabels.length ? actionLabels.join('，然后') : '不执行动作';
    const reason = agentReasonLabel(result.agent_turn_reason, result.agent_turn_status);
    const experience = ['draft_created', 'draft_updated'].includes(result.conversation_experience_status)
      ? '已提炼会话经验草稿，尚未生效。'
      : '';
    return safeSummary(`${shadow ? 'AI观察（未执行）' : 'AI客服'}：识别为“${intent}”，${shadow ? '建议' : '请求'}“${action}”${confidenceLabel}。${reason}${experience}`);
  }
  if (result.ignored_event && result.reason) return `已忽略：${safeSummary(result.reason)}`;
  return '';
}

function agentIntentLabel(value) {
  const intent = safeSummary(value) || '未分类';
  return ({
    '票价咨询': '票价咨询', '选座核价': '选座核价', '订单进度': '订单进度',
    '补充信息': '需要补充信息', '其他': '其他问题',
  })[intent] ?? intent;
}

function agentActionLabel(value) {
  const action = String(value ?? '');
  return ({
    respond: '回复流程说明', ask_for_image: '请买家补发选座截图', ask_for_city: '询问城市',
    ask_for_missing_information: '询问缺失信息', start_quote: '请求旧版识图核价',
    recognize_image: '识别买家图片', resolve_showtime: '匹配影院场次', quote_realtime: '调用实时核价',
    request_price_change: '申请安全改价', create_manual_task: '创建人工处理任务', get_manual_task_status: '查询人工处理进度',
    show_available_wplus_seats: '查询可用W+座位', record_seat_preference: '记录圈选出票指令',
    confirm_quote: '确认本次报价', get_order_status: '查询订单状态', read_linked_order: '读取关联订单',
    inspect_ticket_request: '检查票务请求', handoff: '转人工处理', wait: '暂停自动处理',
  })[action] ?? (action ? `未知动作 ${safeSummary(action)}` : '');
}

function agentReasonLabel(value, status) {
  const reason = String(value ?? '');
  const label = ({
    quote_requested: '判断依据：买家正在询价。',
    conversation_only: '判断依据：本轮只需继续沟通，不执行交易。',
    agent_wait: '判断依据：无需继续回复。',
    agent_requested_handoff: '安全判断：应转人工处理。',
    paid_order: '安全限制：订单已付款，不再核价或改价。',
    human_takeover: '安全限制：人工客服已经接管。',
    low_confidence: '安全限制：AI判断置信度不足。',
    tool_failed: '执行结果：权威工具调用失败，已停止。',
  })[reason];
  if (label) return label;
  if (String(status ?? '') === 'failed') return '执行结果：AI规划失败，未执行任何动作。';
  return reason ? `内部依据：${safeSummary(reason)}。` : '仅用于评估AI判断，不改变实际回复。';
}

function moneyLabel(value) {
  const cents = Number(value);
  return Number.isSafeInteger(cents) && cents >= 0 ? (cents / 100).toFixed(2) : '';
}

export function quoteDiagnosticSummary(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  const requested = value.requested_match && typeof value.requested_match === 'object' ? value.requested_match : {};
  const match = value.match && typeof value.match === 'object' ? value.match : {};
  const areas = Array.isArray(value.realtime_areas) ? value.realtime_areas : [];
  const requestedSummary = [requested.city, requested.cinema, requested.movie, requested.date, requested.showtime, requested.hall]
    .map(safeSummary).filter(Boolean).join(' / ');
  const parts = [
    value.safe_error_code && `代码 ${safeSummary(value.safe_error_code)}`,
    value.failure_step && `步骤 ${safeSummary(value.failure_step)}`,
    requestedSummary && `请求 ${requestedSummary}`,
    Number.isInteger(match.result_count) && `匹配 ${match.result_count} 个`,
    match.cinema && `影院 ${safeSummary(match.cinema)}`,
    match.showtime && `场次 ${safeSummary(match.showtime)}`,
    areas.length && `实时区域 ${areas.map((area) => `${safeSummary(area.label || area.area_code)}:可用${Number.isInteger(area.available_seat_count) ? area.available_seat_count : '-'}`).join('；')}`,
  ].filter(Boolean);
  return safeSummary(parts.join('；'));
}

function safeSummary(value) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, 120);
}
