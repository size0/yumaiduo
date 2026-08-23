const ORDER_EXCEPTION_STAGES = new Set(['exception_review', 'fulfillment_exception', 'paid_unmanaged']);
const PAID_STAGES = new Set(['paid_manual_delivery', 'paid_unmanaged']);
const ACTIONABLE_STAGES = new Set(['quoted', 'quote_confirmed', 'waiting_payment', ...PAID_STAGES, ...ORDER_EXCEPTION_STAGES]);

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
  const parsed = Date.parse(String(value ?? ''));
  return Number.isFinite(parsed) ? parsed : 0;
}

function severityScore(item) {
  if (item.kind === 'order-exception') return 100;
  if (item.kind === 'manual-task' && item.priority === 'urgent') return 90;
  if (item.kind === 'manual-task' && item.priority === 'high') return 80;
  if (item.kind === 'paid-order') return 70;
  if (item.kind === 'manual-task') return 60;
  if (item.stage === 'waiting_payment' || item.stage === 'quote_confirmed') return 40;
  return 20;
}

export function buildActionQueue({ operations = [], orders = [], manualTasks = [] } = {}) {
  const orderById = new Map((Array.isArray(orders) ? orders : []).filter((item) => item?.order_id).map((item) => [String(item.order_id), item]));
  const queue = [];
  const representedOrders = new Set();
  for (const operation of Array.isArray(operations) ? operations : []) {
    const orderId = String(operation?.order_id ?? '');
    const order = orderId ? orderById.get(orderId) ?? {} : {};
    const stage = String(operation?.stage ?? order?.stage ?? '');
    const exception = Boolean(operation?.exception_reason) || ORDER_EXCEPTION_STAGES.has(stage);
    if (!exception && !ACTIONABLE_STAGES.has(stage)) continue;
    if (orderId && (exception || PAID_STAGES.has(stage))) representedOrders.add(orderId);
    queue.push({
      id: `operation:${operation?.event_id ?? operation?.id ?? orderId}`,
      kind: exception ? 'order-exception' : PAID_STAGES.has(stage) ? 'paid-order' : 'conversation',
      severity: exception ? 'urgent' : PAID_STAGES.has(stage) ? 'high' : 'normal',
      buyerLabel: String(operation?.buyer_label ?? order?.buyer_label ?? '匿名买家'),
      shopName: String(operation?.shop_name ?? order?.shop_name ?? ''),
      stage,
      title: exception ? '订单需要立即人工核对' : String(operation?.next_action ?? '查看会话进度'),
      detail: String(operation?.exception_reason ?? order?.platform_order_status_text ?? operation?.next_action ?? ''),
      orderId: orderId || null,
      accountUnb: operation?.account_unb ?? order?.account_unb ?? null,
      chatId: operation?.chat_id ?? order?.chat_id ?? null,
      peerUnb: operation?.peer_unb ?? order?.peer_unb ?? null,
      updatedAt: operation?.updated_at ?? order?.updated_at ?? null,
    });
  }
  for (const order of orderById.values()) {
    const orderId = String(order.order_id);
    if (representedOrders.has(orderId)) continue;
    const stage = String(order.stage ?? '');
    if (!ORDER_EXCEPTION_STAGES.has(stage) && !PAID_STAGES.has(stage)) continue;
    const exception = ORDER_EXCEPTION_STAGES.has(stage) || Boolean(order.exception_reason);
    queue.push({
      id: `order:${orderId}`, kind: exception ? 'order-exception' : 'paid-order', severity: exception ? 'urgent' : 'high',
      buyerLabel: String(order.buyer_label ?? '匿名买家'), shopName: String(order.shop_name ?? ''), stage,
      title: exception ? '订单需要立即人工核对' : '已付款，等待人工履约',
      detail: String(order.exception_reason ?? order.platform_order_status_text ?? ''), orderId,
      accountUnb: order.account_unb ?? null, chatId: order.chat_id ?? null, peerUnb: order.peer_unb ?? null,
      updatedAt: order.updated_at ?? order.platform_pay_time ?? null,
    });
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
