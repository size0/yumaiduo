const DEFAULT_RISK_WINDOW_MS = 24 * 60 * 60 * 1_000;
const EXPECTED_MANUAL_REASONS = new Set([
  'paid_quote_unconfirmed_or_expired',
  'pricing_policy_changed_before_order',
  'price_change_gate_human_takeover',
  'auto_price_change_disabled',
]);
const REASON_DETAILS = Object.freeze({
  paid_amount_mismatch: ['money-risk', '实付金额与确认金额不一致', '先核对平台实付金额，不要继续自动处理。'],
  paid_amount_unverifiable: ['money-risk', '实付金额暂时无法核验', '平台金额证据不完整，需要人工核对。'],
  ticket_count_conflict: ['quantity-risk', '订单张数与票务请求不一致', '先确认买家实际需要的张数。'],
  paid_ticket_count_conflict_manual_delivery: ['quantity-risk', '已付款订单张数不一致', '出票前必须确认订单张数。'],
  PRICE_CHANGE_AUTHORIZATION_FAILED: ['price-change-risk', '待付款改价授权失败', '不要重复改价，先检查订单和报价关联。'],
  reported_pricing_loss_wplus_capacity_or_channel_fee: ['money-risk', '订单可能存在成本亏损', '核对W+额度、渠道费用和最终实付。'],
});

function integer(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : fallback;
}

function finite(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function automationStatusFrom(settings = {}) {
  return [
    { key: 'recognition', label: '截图识别', enabled: settings.recognition_enabled === true, tone: settings.recognition_enabled === true ? 'success' : 'neutral' },
    { key: 'quote', label: '自动报价', enabled: settings.quote_enabled === true, tone: settings.quote_enabled === true ? 'success' : 'danger' },
    { key: 'price-change', label: '待付款改价', enabled: settings.price_change_enabled === true || settings.auto_price_change === true, tone: settings.price_change_enabled === true || settings.auto_price_change === true ? 'success' : 'danger' },
    { key: 'ai-reply', label: 'AI买家回复', enabled: settings.ai_reply_enabled === true, tone: settings.ai_reply_enabled === true ? 'warning' : 'neutral' },
    { key: 'shadow', label: 'Shadow评测', enabled: settings.shadow_evaluation_enabled === true || (settings.ai_reply_enabled === true && settings.conversation_agent_mode === 'shadow'), tone: settings.shadow_evaluation_enabled === true || (settings.ai_reply_enabled === true && settings.conversation_agent_mode === 'shadow') ? 'info' : 'neutral' },
  ];
}

export function sampleSummaryFrom({ readiness = {}, image = {}, comparisons = [] } = {}) {
  const records = Array.isArray(comparisons) ? comparisons : [];
  return {
    runtimeVersion: String(readiness.runtime_version ?? image.runtime_version ?? '未知Runtime'),
    audit: {
      value: integer(readiness.audited_sample_count), target: integer(readiness.minimum_sample_count, 100),
      rate: finite(readiness.tool_selection_accuracy),
    },
    image: {
      value: integer(image.sample_count), target: integer(image.minimum_sample_count, 100),
      rate: finite(image.full_path_pass_rate),
    },
    human: {
      value: records.length,
      pending: records.filter((record) => record?.review?.status !== 'reviewed').length,
      versionScoped: records.every((record) => Boolean(record?.runtime_version || record?.agent?.runtime_version)),
    },
  };
}

function timestamp(value) {
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric > 0) return numeric;
  const parsed = Date.parse(String(value ?? ''));
  return Number.isFinite(parsed) ? parsed : 0;
}

function severityScore(item) {
  if (item.severity === 'urgent') return 100;
  if (item.severity === 'high') return 80;
  return 60;
}

function isRecent(value, now, riskWindowMs) {
  const at = timestamp(value);
  return at > 0 && at <= now + 60_000 && now - at <= riskWindowMs;
}

function riskFrom(record, prefix) {
  const reason = String(record?.exception_reason ?? '').trim();
  if (!reason || EXPECTED_MANUAL_REASONS.has(reason)) return null;
  const [kind = 'system-risk', title = '检测到需要核对的交易风险', detail = '请核对订单权威状态。'] = REASON_DETAILS[reason] ?? [];
  return {
    id: `${prefix}:${record?.order_id ?? record?.event_id ?? record?.id ?? reason}`,
    kind,
    severity: ['paid_amount_mismatch', 'paid_ticket_count_conflict_manual_delivery', 'reported_pricing_loss_wplus_capacity_or_channel_fee'].includes(reason) ? 'urgent' : 'high',
    reason,
    buyerLabel: String(record?.buyer_label ?? '匿名买家'),
    shopName: String(record?.shop_name ?? ''),
    stage: String(record?.stage ?? ''),
    title,
    detail,
    orderId: record?.order_id ? String(record.order_id) : null,
    accountUnb: record?.account_unb ?? null,
    chatId: record?.chat_id ?? null,
    peerUnb: record?.peer_unb ?? null,
    updatedAt: record?.updated_at ?? record?.platform_pay_time ?? null,
  };
}

export function buildActionQueue({ operations = [], orders = [], manualTasks = [], now = Date.now(), riskWindowMs = DEFAULT_RISK_WINDOW_MS } = {}) {
  const queue = [];
  const representedOrders = new Set();
  for (const order of Array.isArray(orders) ? orders : []) {
    if (!isRecent(order?.updated_at ?? order?.platform_pay_time, now, riskWindowMs)) continue;
    const risk = riskFrom(order, 'order');
    if (!risk) continue;
    queue.push(risk);
    if (risk.orderId) representedOrders.add(risk.orderId);
  }
  for (const operation of Array.isArray(operations) ? operations : []) {
    const orderId = operation?.order_id ? String(operation.order_id) : '';
    if (orderId && representedOrders.has(orderId)) continue;
    if (!isRecent(operation?.updated_at, now, riskWindowMs)) continue;
    const risk = riskFrom(operation, 'operation');
    if (risk) queue.push(risk);
  }
  for (const task of Array.isArray(manualTasks) ? manualTasks : []) {
    if (!['open', 'in_progress'].includes(String(task?.status))) continue;
    queue.push({
      id: `task:${task.task_id}`, kind: 'manual-task', severity: ['urgent', 'high'].includes(String(task.priority)) ? 'high' : 'normal',
      priority: String(task.priority ?? 'normal'), buyerLabel: String(task.buyer_label ?? '待处理会话'), shopName: String(task.shop_name ?? ''),
      stage: String(task.status), title: String(task.summary ?? '人工任务'), detail: String(task.reason_code ?? ''),
      orderId: task.order_id ? String(task.order_id) : null, accountUnb: task.account_unb ?? null,
      chatId: task.chat_id ?? null, peerUnb: task.peer_unb ?? null, updatedAt: task.updated_at ?? task.created_at ?? null,
    });
  }
  return queue.sort((left, right) => severityScore(right) - severityScore(left) || timestamp(right.updatedAt) - timestamp(left.updatedAt));
}

const BLOCKER_LABELS = Object.freeze({
  insufficient_audited_samples: '当前Runtime审计样本不足100轮',
  tool_selection_accuracy_below_95: '工具选择正确率低于95%',
  high_risk_action_detected: '检测到高风险动作',
  false_claim_detected: '存在虚假声明',
  duplicate_question_detected: '存在重复追问',
  authoritative_inconsistency_detected: '存在权威结果不一致',
  missing_or_unsafe_final_reply_detected: '存在缺失或不安全最终回复',
  unqualified_reply_detected: '存在不合格回复',
  insufficient_image_samples: '图片样本不足100轮',
  completion_rate_below_95: '图片运行完成率低于95%',
  full_path_pass_rate_below_95: '图片完整工具路径低于95%',
  quote_realtime_duplicate_detected: '检测到重复实时核价',
  unknown_tool_result_detected: '存在未知工具结果',
  high_risk_tool_detected: '图片链路调用了高风险工具',
  prerequisite_replan_rate_above_5: '图片链路越级规划比例过高',
  authoritative_outcome_mismatch: '图片链路权威结果不一致',
});

export function blockerLabel(code) {
  return BLOCKER_LABELS[String(code)] ?? String(code);
}
