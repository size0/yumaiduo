import { createPluginSdk } from './sdk.js';

(() => {
  'use strict';

  const PLUGIN_ID = 'wanda-seat-autoquote';
  const API = Object.freeze({
    overview: 'api/overview',
    settings: 'api/settings',
    logs: 'api/logs',
    conversationLearningSummary: 'api/conversation-learning-summary',
    agentEvaluations: 'api/agent-evaluations',
    agentCanaryReadiness: 'api/agent-canary-readiness',
    agentOfflineEvaluation: 'api/agent-offline-evaluation',
    agentHumanComparisons: 'api/agent-human-comparisons',
    corrections: 'api/corrections',
    manualTasks: 'api/manual-tasks',
    operations: 'api/operations',
    quoteAnalytics: 'api/quote-analytics',
    replyTemplateImages: 'api/reply-template-images',
    replyTemplateImageChunks: 'api/reply-template-images',
    ticketOrders: 'api/orders',
    shops: 'api/shops',
    knowledgeBase: 'api/knowledge-base',
  });

  const STATUS_LABELS = Object.freeze({
    running: '运行中', paused: '已暂停', completed: '已完成', healthy: '正常',
    configured: '已配置', subscribed: '已订阅', needs_configuration: '待配置',
    queued: '排队中', processing: '处理中', retry: '重试中', failed: '执行失败',
    unknown: '结果未知', skipped: '已跳过', waiting_input: '待买家补充', business_blocked: '安全停止', submitted: '已提交', pending: '待审核',
    reviewed: '已确认', rejected: '已拒绝', predicted: '已有建议', idle: '未启动',
    completed_with_errors: '部分失败', stopped: '已停止',
    quoted: '待确认报价', quote_replaced: '已被新报价替换', quote_confirmed: '等待下单', waiting_payment: '待付款',
    paid_manual_delivery: '待人工出票', paid_unmanaged: '非插件报价订单', ticket_issued: '已出票', ticket_sent: '票码已发送', fulfillment_exception: '出票异常', aftersale: '售后处理', exception_review: '异常复核', quote_expired: '报价已过期',
    UNSENT_PREVIEW: '未发送 · 仅预览',
  });

  const EVENT_LABELS = Object.freeze({
    'im.message.received': '收到买家消息', 'order.created': '订单创建',
    'order.price.changed': '订单改价', 'order.paid': '订单付款', 'order.closed': '订单关闭',
  });

  const TAB_ALIASES = Object.freeze({
    shops: 'automation', pricing: 'automation',
    model: 'ai-settings', 'knowledge-base': 'ai-settings',
    operations: 'quote-records', quotes: 'quote-records',
    orders: 'order-management', 'ticket-orders': 'order-management', review: 'diagnostics', logs: 'diagnostics',
  });

  const defaultSettings = Object.freeze({
    automation_enabled: true,
    recognition_enabled: true,
    quote_enabled: true,
    price_change_enabled: true,
    ai_reply_enabled: true,
    conversation_agent_mode: 'shadow',
    ai_reply_system_prompt: '仅基于已确认的会话事实和已启用知识库回复；不编造价格、库存、订单或承诺。',
    ai_reply_shop_background: '',
    ai_reply_precautions: '',
    ai_reply_style: '',
    ai_reply_memory_hours: 24,
    ai_reply_memory_depth: 20,
    ai_reply_delay_seconds: 3,
    ai_reply_manual_takeover_seconds: 20,
    wplus_adjustment_cents: -290,
    wplus_member_price_threshold_cents: 6000,
    regular_adjustment_cents: 100,
    max_auto_order_amount_cents: 200000,
    minimum_confidence: 0.9,
    ai_base_url: 'https://api.openai.com',
    ai_model: 'gpt-4.1-mini',
    ai_key_configured: false,
    reply_templates: {
      first_contact_notice: '您好，请发送已标记购买位置的完整选座页截图\n并说明需要几张\n\n收到报价后请回复“确认”\n再提交订单并保持待付款\n收到“价格已修改”后再付款',
      quote_confirmation_instruction: '接受本次报价请回复“确认”。',
      quote_processing_notice: '收到，正在按当前信息核对万达实时场次和优惠，请稍等。',
      quote_replaced_by_official_selection: '您这次发送的是官方已选座截图，已按具体座位重新核价；上一版未选座试价已失效。',
      quote_exact: '※{城市标记}| {影院}\n电影：{影片}\n影厅：{影厅}\n场次：{日期} {场次}\n座位：{座位}\n\n{单价}元/张，{张数}张合计{合计}元。',
      quote_area: '※{城市标记}| {影院}\n电影：{影片}\n影厅：{影厅}\n场次：{日期} {场次}\n\n{单价}元/张，{张数}张合计{合计}元。\n出票时按您原图圈选的位置操作，无需提供具体座位号；若该位置届时不可选，会先联系您确认，不会擅自换座。',
      quote_need_count: '※{城市标记}| {影院}\n电影：{影片}\n影厅：{影厅}\n场次：{日期} {场次}\n\n实时单价{单价}元/张，请告诉我需要几张。\n出票时按您原图圈选的位置操作，无需提供具体座位号；若该位置届时不可选，会先联系您确认，不会擅自换座。',
      quote_count_completed: '已收到{张数}张需求\n实时单价{单价}元/张，{张数}张合计{合计}元。\n请提交订单后先不要付款\n等待系统改价\n仅在收到“价格已修改”后付款。',
      quote_buyer_app_better_price: '您现在用的APP有合适的优惠价，可以自行购买。',
      available_wplus_seats: '可以选。当前万达实时座位图中，{排数}排可选W+座位：{可选座位}。',
      manual_delivery_preference: '已记录：出票时按您原图圈选的位置操作，无需提供具体座位号。若该位置届时不可选，会先联系您确认，不会擅自换座。',
      wplus_area_unavailable: '当前无法核验 W+ 区域，请人工确认后处理。',
      wplus_seats_unavailable: '当前场次没有W+位置可选择，您看到的位置可能是维修状态无法购买。',
      wplus_price_unavailable: '当前场次未查到可用的W+会员专属优惠，暂不能自动报价，请人工确认。',
      quote_price_conflict: '当前场次会员优惠不足，按当前规则暂无法形成安全报价，请人工确认。',
      insufficient_available_seats: '当前没有足够同类可用座位，请人工确认。',
      cinema_catalog_not_unique: '请问这是哪个城市的万达影城？已识别的影片、日期、场次和座位信息会保留，无需重发截图。',
      showtime_not_unique: '截图信息无法唯一匹配场次，请补充影院和开场时间。',
      showtime_not_found: '当前万达官方场次中未找到该日期和开场时间，请刷新万达选座页后发送最新截图。',
      official_selection_unverifiable: '截图中的官方已选座当前并非全部实时可选，请在购票平台重新选择当前可选座位后发送最新完整截图；请勿付款。',
      image_not_seat_map: '截图价格仅供参考，实际价格以万达实时核价结果为准。',
      order_paid: '订单已付款，后续由人工出票或售后处理，不会重新核价。',
      need_image: '请发送万达电影票座位图截图，并补充需要的张数。',
      text_quote_missing_fields: '为了实时核价，请发送已标记需要购买位置的完整选座页截图，并说明需要几张。截图需要补全：{缺失信息}。图片标记仅供人工出票，不代表官方选座。',
      non_wanda_cinema: '暂时只代订万达影院的电影票。',
      ticket_count_conflict: '文字张数与官方已选座张数不一致，请人工确认。',
      wplus_account_unavailable: 'W+ 核价账号暂不可用，请稍后人工确认。',
      temporary_lock_failed: '实时优惠核验暂未完成，请稍后人工确认。',
      temporary_lock_release_unverified: '本次临时试价已尝试取消，但试价座位尚未在万达实时座位图中确认恢复；这不代表该场会员座都不可售。为避免重复占座，本次已停止自动报价，请勿付款，稍后重新发送最新完整选座页。',
      wanda_gateway_unavailable: '万达实时核价暂不可用，请稍后重试。',
      quote_verification_failed: '暂未核到该场实时价格，请补充完整影院名、影片和开场时间。',
      recognition_failed: '选座截图暂未识别成功，请重新发送清晰完整的选座图，并补充影院、影片、场次和需要张数。',
      order_submit_before_payment: '点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）\n提交订单后请先不要付款，等待系统确认改价成功后再付款。',
      order_price_change_failed: '当前订单金额无法自动修改，请先不要付款，已转人工处理。',
      price_change_authorization_failed: '平台订单改价授权失败，请先不要付款，已转人工核查。',
      paid_amount_mismatch: '订单金额与本次核验报价不一致；如已支付，请勿重复下单，联系人工处理。',
      paid_quote_unconfirmed: '订单已付款，但未找到有效确认报价；请勿重复下单，联系人工处理。',
      paid_manual_delivery: '已收到付款，请稍等人工出票。订单已付款，不会重新核价。',
    },
  });

  const REPLY_TEMPLATE_GROUPS = Object.freeze([
    { id: 'guide', title: '接待与信息补充', description: '首次接待、补图、补城市和识别失败时使用', keys: ['first_contact_notice', 'cinema_catalog_not_unique', 'showtime_not_unique', 'showtime_not_found', 'image_not_seat_map', 'need_image', 'text_quote_missing_fields', 'non_wanda_cinema', 'recognition_failed'] },
    { id: 'quote', title: '报价与座位沟通', description: '权威核价完成后，根据座位范围和买家反馈选择', keys: ['quote_confirmation_instruction', 'quote_processing_notice', 'quote_replaced_by_official_selection', 'quote_exact', 'quote_area', 'quote_need_count', 'quote_count_completed', 'quote_buyer_app_better_price', 'available_wplus_seats', 'manual_delivery_preference', 'wplus_area_unavailable', 'wplus_seats_unavailable', 'wplus_price_unavailable', 'quote_price_conflict', 'insufficient_available_seats'] },
    { id: 'safety', title: '核验失败与安全停止', description: '账号、实时试价、释放复核或网关异常时停止自动链路', keys: ['ticket_count_conflict', 'wplus_account_unavailable', 'temporary_lock_failed', 'temporary_lock_release_unverified', 'wanda_gateway_unavailable', 'quote_verification_failed', 'official_selection_unverifiable'] },
    { id: 'order', title: '下单、付款与人工处理', description: '报价确认后到付款核验及人工出票阶段使用', keys: ['order_paid', 'order_submit_before_payment', 'order_price_change_failed', 'price_change_authorization_failed', 'paid_amount_mismatch', 'paid_quote_unconfirmed', 'paid_manual_delivery'] },
  ]);

  const state = {
    sdk: null,
    localPreview: false,
    overview: null,
    settings: { ...defaultSettings },
    logs: [],
    operations: [],
    quoteAnalytics: { records: [], summary: {} },
    ticketOrders: [],
    shops: [],
    knowledgeBase: [],
    conversationLearningSummary: null,
    agentEvaluations: [],
    agentCanaryReadiness: null,
    agentOfflineEvaluation: null,
    agentHumanComparisons: [],
    corrections: [],
    loading: false,
  };

  const elements = {};

  document.addEventListener('DOMContentLoaded', init);

  async function init() {
    cacheElements();
    bindTabs();
    bindActions();
    selectInitialTab();
    await loadData();
  }

  function cacheElements() {
    const ids = [
      'app-shell', 'loading-notice', 'error-notice', 'error-message', 'demo-notice',
      'plugin-status', 'data-mode', 'last-updated', 'refresh-button', 'retry-button',
      'pricing-form', 'wplus-adjustment', 'wplus-member-price-threshold', 'regular-adjustment', 'max-auto-order-amount', 'automation-enabled',
      'quote-enabled', 'price-change-enabled', 'pricing-save-status', 'reply-templates-form', 'reply-template-fields', 'reply-templates-save-status', 'model-form',
      'ai-base-url', 'ai-model', 'ai-api-key', 'clear-ai-api-key', 'minimum-confidence',
      'ai-reply-enabled', 'conversation-agent-mode', 'ai-reply-system-prompt', 'ai-reply-shop-background', 'ai-reply-precautions', 'ai-reply-style', 'ai-reply-memory-hours', 'ai-reply-memory-depth', 'ai-reply-delay-seconds', 'ai-reply-manual-takeover-seconds', 'model-key-status', 'model-save-status',
      'agent-takeover-status', 'learning-agent-mode', 'learning-agent-failures', 'learning-observed-turns', 'learning-experience-drafts', 'learning-experience-enabled', 'learning-summary-note',
      'agent-evaluation-count', 'agent-readiness-status', 'agent-offline-evaluation-status', 'agent-evaluation-body',
      'human-comparison-count', 'human-comparison-body', 'correction-review-count', 'correction-review-body',
      'knowledge-base-form', 'knowledge-category', 'knowledge-title', 'knowledge-sort-order', 'knowledge-content', 'knowledge-save-status', 'knowledge-count', 'knowledge-list', 'review-filter',
      'review-search', 'review-count', 'review-records-body',
      'log-filter', 'log-search', 'clear-log-filter', 'log-count', 'logs-body', 'toast-region',
      'quote-records-count', 'quote-records-body', 'quote-learning-summary', 'quote-formula-summary', 'ticket-orders-count', 'ticket-orders-body', 'sync-shops', 'shops-count', 'shops-body', 'reply-template-search',
    ];
    ids.forEach((id) => { elements[id] = document.getElementById(id); });
    elements.tabs = [...document.querySelectorAll('[role="tab"]')];
    elements.panels = [...document.querySelectorAll('[role="tabpanel"]')];
  }

  function bindTabs() {
    elements.tabs.forEach((tab) => {
      tab.addEventListener('click', () => activateTab(tab.dataset.tab));
      tab.addEventListener('keydown', handleTabKeydown);
    });
    document.querySelectorAll('[data-open-tab]').forEach((button) => {
      button.addEventListener('click', () => activateTab(button.dataset.openTab, true));
    });
  }

  function bindActions() {
    elements['refresh-button'].addEventListener('click', loadData);
    elements['sync-shops'].addEventListener('click', refreshShops);
    elements['retry-button'].addEventListener('click', loadData);
    elements['pricing-form'].addEventListener('submit', savePricingSettings);
    elements['reply-templates-form'].addEventListener('submit', saveReplyTemplates);
    elements['reply-template-search'].addEventListener('input', filterReplyTemplates);
    elements['model-form'].addEventListener('submit', saveModelSettings);
    elements['knowledge-base-form'].addEventListener('submit', createKnowledgeEntry);
    elements['review-filter'].addEventListener('change', renderReviewRecords);
    elements['review-search'].addEventListener('input', renderReviewRecords);
    elements['log-filter'].addEventListener('change', renderLogs);
    elements['log-search'].addEventListener('input', renderLogs);
    elements['clear-log-filter'].addEventListener('click', () => {
      elements['log-filter'].value = 'all';
      elements['log-search'].value = '';
      renderLogs();
      elements['log-filter'].focus();
    });
    window.addEventListener('hashchange', selectInitialTab);
  }

  function selectInitialTab() {
    const requested = TAB_ALIASES[window.location.hash.replace('#', '')] ?? window.location.hash.replace('#', '');
    const valid = elements.tabs.some((tab) => tab.dataset.tab === requested);
    activateTab(valid ? requested : 'automation');
  }

  function activateTab(name, updateHash = false) {
    name = TAB_ALIASES[name] ?? name;
    elements.tabs.forEach((tab) => {
      const selected = tab.dataset.tab === name;
      tab.classList.toggle('is-active', selected);
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    elements.panels.forEach((panel) => { panel.hidden = panel.dataset.panel !== name; });
    if (updateHash) {
      history.replaceState(null, '', `#${name}`);
      document.getElementById(`tab-${name}`)?.focus();
    }
  }

  function handleTabKeydown(event) {
    const current = elements.tabs.indexOf(event.currentTarget);
    let next = current;
    if (event.key === 'ArrowRight') next = (current + 1) % elements.tabs.length;
    else if (event.key === 'ArrowLeft') next = (current - 1 + elements.tabs.length) % elements.tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = elements.tabs.length - 1;
    else return;
    event.preventDefault();
    activateTab(elements.tabs[next].dataset.tab);
    elements.tabs[next].focus();
  }

  async function loadData() {
    setLoading(true);
    clearError();
    try {
      state.sdk = await resolveSdk();
      state.localPreview = state.sdk.localPreview === true;
      updateModeNotice();
      const results = await Promise.allSettled([
        requestJson(API.overview),
        requestJson(API.settings),
        requestJson(API.logs),
        requestJson(API.operations),
        requestJson(API.quoteAnalytics),
        requestJson(API.ticketOrders),
        requestJson(API.shops),
        requestJson(API.knowledgeBase),
        requestJson(API.conversationLearningSummary),
        requestJson(API.agentEvaluations),
        requestJson(API.agentCanaryReadiness),
        requestJson(API.agentOfflineEvaluation),
        requestJson(API.agentHumanComparisons),
        requestJson(API.corrections),
      ]);
      const valueAt = (index, fallback) => results[index].status === 'fulfilled' ? results[index].value : fallback;
      const coreFailure = results.slice(0, 2).find((result) => result.status === 'rejected');
      if (coreFailure) throw coreFailure.reason;
      state.overview = valueAt(0, null);
      state.settings = { ...defaultSettings, ...valueAt(1, {}) };
      state.logs = Array.isArray(valueAt(2, [])) ? valueAt(2, []) : [];
      state.operations = Array.isArray(valueAt(3, [])) ? valueAt(3, []) : [];
      const analytics = valueAt(4, { records: [], summary: {} });
      state.quoteAnalytics = analytics && typeof analytics === 'object' ? analytics : { records: [], summary: {} };
      state.ticketOrders = Array.isArray(valueAt(5, [])) ? valueAt(5, []) : [];
      state.shops = Array.isArray(valueAt(6, [])) ? valueAt(6, []) : [];
      state.knowledgeBase = Array.isArray(valueAt(7, [])) ? valueAt(7, []) : [];
      const learningSummary = valueAt(8, null);
      state.conversationLearningSummary = learningSummary && typeof learningSummary === 'object' ? learningSummary : null;
      state.agentEvaluations = Array.isArray(valueAt(9, [])) ? valueAt(9, []) : [];
      state.agentCanaryReadiness = valueAt(10, null);
      state.agentOfflineEvaluation = valueAt(11, null);
      state.agentHumanComparisons = Array.isArray(valueAt(12, [])) ? valueAt(12, []) : [];
      state.corrections = Array.isArray(valueAt(13, [])) ? valueAt(13, []) : [];
      renderAll();
      if (results.some((result, index) => index > 1 && result.status === 'rejected')) {
        notify('部分辅助数据暂时不可用，不影响图片识别和实时核价。', 'warning');
      }
    } catch (error) {
      state.overview = null;
      state.logs = [];
      state.operations = [];
      state.quoteAnalytics = { records: [], summary: {} };
      state.ticketOrders = [];
      state.shops = [];
      state.conversationLearningSummary = null;
      state.agentEvaluations = [];
      state.agentCanaryReadiness = null;
      state.agentOfflineEvaluation = null;
      state.agentHumanComparisons = [];
      state.corrections = [];
      renderAll();
      showError(readableError(error));
    } finally {
      setLoading(false);
    }
  }

  async function resolveSdk() {
    if (window.self === window.top) {
      return {
        localPreview: true,
        authedFetch: (requestPath, options) => window.fetch(
          `/ui/${String(requestPath).replace(/^\/+/, '')}`,
          options,
        ),
      };
    }
    const sdk = createPluginSdk({ pluginId: PLUGIN_ID });
    if (!sdk || typeof sdk.authedFetch !== 'function') {
      throw new Error('鱼麦多前端 SDK 未加载，正式插件已停止运行');
    }
    if (typeof sdk.ready === 'function') await sdk.ready();
    else if (sdk.ready && typeof sdk.ready.then === 'function') await sdk.ready;
    return sdk;
  }

  async function requestJson(path, options = {}) {
    const response = await state.sdk.authedFetch(path, options);
    const payload = response && typeof response.json === 'function' ? await response.json() : response;
    if (response && typeof response.ok === 'boolean' && !response.ok) {
      throw new Error(payload?.error || `HTTP ${response.status}`);
    }
    if (payload?.ok === false) throw new Error(payload.error || 'request_failed');
    return Object.prototype.hasOwnProperty.call(payload ?? {}, 'data') ? payload.data : payload;
  }

  function renderAll() {
    renderHeader();
    renderQuoteRecords();
    renderOrderManagement();
    renderShops();
    renderKnowledgeBase();
    renderConversationLearningSummary();
    renderAgentEvaluations();
    renderCorrections();
    renderHumanComparisons();
    populateForms();
    renderReviewRecords();
    renderLogs();
  }

  function renderHeader() {
    const plugin = state.overview?.plugin;
    const status = plugin?.status ?? 'unknown';
    setStatusPill(elements['plugin-status'], status, plugin ? statusLabel(status) : '无数据');
    elements['last-updated'].textContent = plugin?.updated_at ? `更新于 ${formatDateTime(plugin.updated_at)}` : '尚未刷新';
  }

  function renderConversationLearningSummary() {
    const summary = state.conversationLearningSummary;
    const mode = state.settings?.conversation_agent_mode ?? 'off';
    const modeLabel = ({ off: '关闭', shadow: '影子', active: '启用' })[mode] ?? '未知';
    elements['agent-takeover-status'].textContent = mode === 'active' ? '已接管' : '未接管';
    elements['learning-agent-mode'].textContent = modeLabel;
    elements['learning-observed-turns'].textContent = String(summary?.observed_turn_count ?? 0);
    elements['learning-experience-drafts'].textContent = String(summary?.experience_draft_count ?? 0);
    elements['learning-agent-failures'].textContent = `智能体失败 ${summary?.agent_failure_count ?? 0} 轮`;
    elements['learning-experience-enabled'].textContent = `已审核启用 ${summary?.experience_enabled_count ?? 0} 条 · 累计证据 ${summary?.experience_evidence_count ?? 0} 次`;
    if (!summary) {
      elements['learning-summary-note'].textContent = '学习状态暂时读取失败；不影响实时识图和核价。';
      return;
    }
    const lastObserved = summary.last_observed_at ? `最近观察 ${formatDateTime(summary.last_observed_at)}。` : '尚无会话观察。';
    elements['learning-summary-note'].textContent = `影子观察不会发送AI生成的回复；会话经验只生成停用草稿，人工审核启用后才生效。${lastObserved}`;
  }

  function renderAgentEvaluations() {
    const records = state.agentEvaluations;
    const reviewed = records.filter((item) => item.review).length;
    const correct = records.filter((item) => item.review?.expected_action === item.actual_action).length;
    elements['agent-evaluation-count'].textContent = `最近 ${records.length} 轮 · 已抽查 ${reviewed} 轮 · 下一步判断一致 ${reviewed ? `${correct}/${reviewed}` : '暂无'} · 当前影子模式无需立即审核`;
    const readiness = state.agentCanaryReadiness;
    const blockerLabels = { insufficient_audited_samples: '审计样本不足', tool_selection_accuracy_below_95: '工具正确率不足95%', high_risk_action_detected: '发现高风险动作', false_claim_detected: '发现虚假声明', duplicate_question_detected: '发现重复追问', authoritative_inconsistency_detected: '权威结果不一致' };
    elements['agent-readiness-status'].textContent = readiness?.ready
      ? `低风险Canary门禁已通过：${readiness.audited_sample_count}个审计样本，工具正确率${readiness.tool_selection_accuracy}%。生产Active仍需单独人工批准。`
      : `低风险Canary尚未开放：${(readiness?.blockers ?? ['insufficient_audited_samples']).map((item) => blockerLabels[item] || item).join('、')}。`;
    const offline = state.agentOfflineEvaluation;
    const offlineBlockers = { insufficient_image_samples: '图片样本不足', completion_rate_below_95: '完成率不足95%', full_path_pass_rate_below_95: '完整工具路径不足95%', quote_realtime_duplicate_detected: '实时核价发生重复调用', unknown_tool_result_detected: '存在结果未知的工具调用', high_risk_tool_detected: '图片链路调用了高风险工具', prerequisite_replan_rate_above_5: '越级工具申请超过5%', authoritative_outcome_mismatch: 'Agent与确定性结果不一致' };
    elements['agent-offline-evaluation-status'].textContent = offline?.ready
      ? `图片链路离线门禁已通过：${offline.sample_count}轮，完整路径${offline.full_path_pass_rate ?? '不适用'}%，P50 ${offline.latency_p50_ms}ms，P95 ${offline.latency_p95_ms}ms。`
      : `当前Agent运行版本图片门禁未通过：${(offline?.blockers ?? ['insufficient_image_samples']).map((item) => offlineBlockers[item] || item).join('、')}；新版本样本${offline?.sample_count ?? 0}轮（历史${offline?.historical_sample_count ?? 0}轮不计入放行），P95 ${offline?.latency_p95_ms ?? '-'}ms。`;
    const body = elements['agent-evaluation-body']; body.replaceChildren();
    if (!records.length) { body.append(createEmptyRow(13, '暂无Agent影子观察记录。当前无需人工操作。')); return; }
    records.slice(0, 50).forEach((record) => {
      const review = record.review ?? {};
      const expected = evaluationSelect([...AGENT_ACTION_LABELS.keys()], review.expected_action || record.actual_action);
      const shouldAsk = evaluationSelect(['false', 'true'], String(review.should_ask ?? record.suggested_ask));
      const shouldHandoff = evaluationSelect(['false', 'true'], String(review.should_handoff ?? record.suggested_handoff));
      const quality = evaluationSelect(['unreviewed', 'qualified', 'unqualified', 'not_applicable'], review.reply_quality || 'unreviewed');
      const consistency = evaluationSelect(['unreviewed', 'consistent', 'inconsistent', 'not_applicable'], review.authoritative_consistency || 'unreviewed');
      const highRisk = evaluationSelect(['false', 'true'], String(review.high_risk_action ?? false));
      const falseClaim = evaluationSelect(['false', 'true'], String(review.false_claim ?? false));
      const duplicateQuestion = evaluationSelect(['false', 'true'], String(review.duplicate_question ?? false));
      const save = document.createElement('button'); save.type = 'button'; save.className = 'button button-secondary button-compact'; save.textContent = review.event_id ? '更新' : '保存';
      const correct = document.createElement('button'); correct.type = 'button'; correct.className = 'button button-secondary button-compact'; correct.textContent = '纠正本轮';
      correct.addEventListener('click', async () => {
        const correctedAnswer = window.prompt('请输入这轮应该给买家的正确回答：', '');
        if (!correctedAnswer?.trim()) return;
        const handlingGuidance = window.prompt('可选：请输入可复用于相似问题的处理方式（审核通过后进入知识版本）：', '') ?? '';
        correct.disabled = true;
        try {
          await requestJson(`${API.agentEvaluations}/${encodeURIComponent(record.event_id)}/correction`, {
            method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ corrected_answer: correctedAnswer.trim(), handling_guidance: handlingGuidance.trim() }),
          });
          state.corrections = await requestJson(API.corrections);
          renderCorrections();
          notify('本轮纠正已保存为待审核记录');
        } catch (error) { notify(readableError(error), 'error'); }
        finally { correct.disabled = false; }
      });
      save.addEventListener('click', async () => {
        save.disabled = true;
        try {
          await requestJson(`${API.agentEvaluations}/${encodeURIComponent(record.event_id)}`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ expected_action: expected.value, should_ask: shouldAsk.value === 'true', should_handoff: shouldHandoff.value === 'true', reply_quality: quality.value, authoritative_consistency: consistency.value, high_risk_action: highRisk.value === 'true', false_claim: falseClaim.value === 'true', duplicate_question: duplicateQuestion.value === 'true' }) });
          [state.agentEvaluations, state.agentCanaryReadiness] = await Promise.all([requestJson(API.agentEvaluations), requestJson(API.agentCanaryReadiness)]); renderAgentEvaluations(); notify('影子工具选择评测已保存');
        } catch (error) { save.disabled = false; notify(readableError(error), 'error'); }
      });
      const identity = document.createElement('td');
      identity.append(
        textLine(record.buyer_label || `买家 …${String(record.peer_unb || '').slice(-4)}`),
        textLine(formatDateTime(record.time), 'table-muted'),
        textLine(record.turn_summary || '本轮消息摘要暂不可用'),
        textLine(`AI识别：${record.intent || '未分类'} · ${Math.round(Number(record.confidence || 0) * 100)}%`, 'table-muted'),
      );
      const session = document.createElement('td'); session.append(sessionActionControl(record));
      const cells = [
        identity,
        textCell(AGENT_ACTION_LABELS.get(record.actual_action) || record.actual_action),
        textCell(record.authoritative_summary || authoritativeOutcomeText(record.authoritative_outcome), 'table-muted'),
        selectCell(expected), selectCell(shouldAsk), selectCell(shouldHandoff), selectCell(quality), selectCell(consistency),
        selectCell(highRisk), selectCell(falseClaim), selectCell(duplicateQuestion), session, document.createElement('td'),
      ];
      cells.at(-1).append(save, correct); const row = document.createElement('tr'); row.append(...cells); body.append(row);
    });
  }

  function renderCorrections() {
    const records = Array.isArray(state.corrections) ? state.corrections : [];
    const drafts = records.filter((record) => record.status === 'draft').length;
    elements['correction-review-count'].textContent = `${records.length}条纠正 · ${drafts}条待审核`;
    const body = elements['correction-review-body']; body.replaceChildren();
    if (!records.length) { body.append(createEmptyRow(6, '暂无运营纠正记录。')); return; }
    const scenes = { general: '通用', intake: '信息收集', quote_followup: '报价跟进', order: '订单', fulfillment: '履约', aftersale: '售后' };
    records.slice(0, 100).forEach((record) => {
      const scene = evaluationSelect(Object.keys(scenes), record.scene || 'general');
      [...scene.options].forEach((option) => { option.textContent = scenes[option.value]; });
      const actions = document.createElement('td');
      if (record.status === 'draft') {
        const approve = document.createElement('button'); approve.type = 'button'; approve.className = 'button button-primary button-compact'; approve.textContent = '审核通过';
        const reject = document.createElement('button'); reject.type = 'button'; reject.className = 'button button-secondary button-compact'; reject.textContent = '拒绝';
        const review = async (status) => {
          approve.disabled = true; reject.disabled = true;
          try {
            await requestJson(`${API.corrections}/${encodeURIComponent(record.id)}`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ status, scene: scene.value, revision: record.revision }) });
            state.corrections = await requestJson(API.corrections); renderCorrections();
            if (status === 'approved') {
              state.knowledgeBase = await requestJson(API.knowledgeBase);
              renderKnowledgeBase();
            }
            notify(status === 'approved' ? '纠正已进入当前租户知识版本' : '纠正已拒绝');
          } catch (error) { approve.disabled = false; reject.disabled = false; notify(readableError(error), 'error'); }
        };
        approve.addEventListener('click', () => void review('approved'));
        reject.addEventListener('click', () => void review('rejected'));
        actions.append(approve, reject);
      } else actions.append(textLine(record.status === 'approved' ? '已审核通过' : '已拒绝', 'table-muted'));
      const row = document.createElement('tr');
      row.append(
        textCell(record.buyer_message || '[图片或非文本消息]'),
        textCell(record.assistant_reply || '未形成回复', 'table-muted'),
        textCell(`${record.corrected_answer || ''}${record.handling_guidance ? `\n处理方式：${record.handling_guidance}` : ''}`),
        selectCell(scene), textCell(record.status || 'draft'), actions,
      );
      body.append(row);
    });
  }

  function renderHumanComparisons() {
    const records = state.agentHumanComparisons;
    const unreviewed = records.filter((item) => item.review?.status !== 'reviewed').length;
    elements['human-comparison-count'].textContent = `${records.length}条对照记录 · ${unreviewed}条待审核；人工回复不会直接训练模型。`;
    const body = elements['human-comparison-body']; body.replaceChildren();
    if (!records.length) { body.append(createEmptyRow(9, '尚未发现可与影子Agent计划关联的人工回复。')); return; }
    const labelNames = { aligned: '一致', safer_than_human: 'Agent更保守但可接受', missed_context: '漏掉会话事实', wrong_intent: '意图错误', wrong_tool: '工具错误', unsafe_claim: '不安全承诺', unnecessary_handoff: '不必要转人工', needs_policy: '需要沉淀规则' };
    const targetNames = { none: '暂不沉淀', knowledge_base: '知识库', reply_template: '回复模板', business_rule: '业务规则', tool_gate: '工具门禁' };
    records.slice(0, 100).forEach((record) => {
      const review = record.review ?? {}; const automatic = record.automatic_comparison ?? {};
      const label = evaluationSelect(Object.keys(labelNames), review.label || automatic.suggested_label || 'needs_policy');
      [...label.options].forEach((option) => { option.textContent = labelNames[option.value]; });
      const target = evaluationSelect(Object.keys(targetNames), review.target || 'none');
      [...target.options].forEach((option) => { option.textContent = targetNames[option.value]; });
      const note = document.createElement('input'); note.type = 'text'; note.maxLength = 500; note.value = review.note || ''; note.placeholder = '审核说明（可选）';
      const save = document.createElement('button'); save.type = 'button'; save.className = 'button button-secondary button-compact'; save.textContent = review.status === 'reviewed' ? '更新' : '审核';
      save.addEventListener('click', async () => { save.disabled = true; try { await requestJson(`${API.agentHumanComparisons}/${encodeURIComponent(record.id)}`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ label: label.value, target: target.value, note: note.value }) }); state.agentHumanComparisons = await requestJson(API.agentHumanComparisons); renderHumanComparisons(); notify('人工对照审核已保存，仅生成受控沉淀意向'); } catch (error) { save.disabled = false; notify(readableError(error), 'error'); } });
      const buyer = document.createElement('td'); buyer.append(textLine(record.buyer_turn?.summary || '[图片或非文本消息]'), textLine(`${record.conversation_facts?.cinema || ''} ${record.conversation_facts?.movie || ''} ${record.conversation_facts?.stage || ''}`.trim() || '暂无完整会话事实', 'table-muted'));
      const agent = document.createElement('td'); agent.append(textLine(`${record.agent?.intent || '未分类'} / ${AGENT_ACTION_LABELS.get(record.agent?.action) || record.agent?.action || '无计划'}`), textLine(record.agent?.proposed_reply || 'Agent未形成拟回复', 'table-muted'));
      const human = document.createElement('td'); human.append(textLine(record.human?.reply || ''), textLine(`对应策略：${AGENT_ACTION_LABELS.get(record.human?.expected_action) || record.human?.expected_action}`, 'table-muted'));
      const auto = textCell(`意图${automatic.intent_aligned === true ? '✓' : '×'} · 工具${automatic.tool_aligned === true ? '✓' : '×'} · 事实${automatic.facts_aligned == null ? '待审' : automatic.facts_aligned ? '✓' : '×'} · 风险${automatic.risk_aligned === true ? '✓' : '×'} · 策略${automatic.reply_strategy_aligned === true ? '✓' : '×'}`, 'table-muted');
      const session = document.createElement('td'); session.append(sessionActionControl(record));
      const action = document.createElement('td'); action.append(save);
      const row = document.createElement('tr'); row.append(buyer, agent, human, auto, selectCell(label), selectCell(target), (() => { const cell = document.createElement('td'); cell.append(note); return cell; })(), session, action); body.append(row);
    });
  }

  const AGENT_ACTION_LABELS = new Map([
    ['respond', '直接回复'], ['ask_for_image', '补图片'], ['ask_for_city', '补城市'], ['ask_for_missing_information', '补缺失信息'], ['start_quote', '旧版识图核价'], ['recognize_image', '识别图片'], ['resolve_showtime', '匹配影院场次'], ['quote_realtime', '调用实时核价'], ['request_price_change', '申请安全改价'], ['create_manual_task', '创建人工任务'], ['show_available_wplus_seats', '查询W+座位'], ['record_seat_preference', '记录圈选出票指令'], ['confirm_quote', '请求确认报价'], ['get_order_status', '读取订单状态'], ['handoff', '转人工'], ['wait', '等待'],
  ]);
  function authoritativeOutcomeText(value) {
    return ({ quote_succeeded: '确定性系统：已形成实时报价', quote_failed: '确定性系统：核价安全停止', not_available: '确定性系统：本轮没有可对照的交易结果' })[value] || '确定性系统：结果未知';
  }
  function evaluationSelect(values, selected) { const select = document.createElement('select'); values.forEach((value) => { const option = document.createElement('option'); option.value = value; option.textContent = AGENT_ACTION_LABELS.get(value) || ({ true: '是', false: '否', unreviewed: '未选择', qualified: '合适', unqualified: '不合适', not_applicable: '不适用', consistent: '符合', inconsistent: '不符合' })[value] || value; option.selected = value === selected; select.append(option); }); return select; }
  function selectCell(select) { const cell = document.createElement('td'); cell.append(select); return cell; }

  function operationKey(item) {
    if (!item) return '';
    return `${String(item.account_unb ?? '')}:${String(item.chat_id ?? '')}`;
  }

  function renderQuoteRecords() {
    const analytics = state.quoteAnalytics && typeof state.quoteAnalytics === 'object' ? state.quoteAnalytics : { records: [], summary: {} };
    const records = Array.isArray(analytics.records) && analytics.records.length
      ? analytics.records
      : state.operations.filter((record) => Number.isSafeInteger(record.quote_unit_cents) || Number.isSafeInteger(record.quote_total_cents));
    const summary = analytics.summary ?? {};
    const body = elements['quote-records-body'];
    setStatusPill(elements['quote-records-count'], records.length ? 'quoted' : 'idle', records.length ? `${records.length} 条` : '暂无报价');
    elements['quote-learning-summary'].textContent = records.length
      ? `已记录${summary.sample_size ?? records.length}条权威报价，付款成功${summary.paid_success_count ?? 0}条，整体付款转化率${formatRate(summary.paid_success_rate)}。当前只做匿名统计，不训练模型，也不会自动调整报价公式。${summary.pricing_evaluation_status === 'eligible_for_manual_shadow_evaluation' ? '样本量已达到可人工启动独立影子价格建议评估的门槛；当前仍未启用。' : `至少记录${summary.minimum_pricing_evaluation_samples ?? 100}条后，才评估是否启动独立的影子价格建议。`}`
      : '暂无足够权威报价样本；当前不运行价格学习，也不会根据买家个人特征推断价格。';
    elements['quote-formula-summary'].textContent = summary.formula
      ? `${summary.formula} 这是后台当前设置的确定性规则，不是AI学习结果。`
      : '当前报价使用后台确定性实时成本规则；不是AI学习结果。';
    body.replaceChildren();
    if (!records.length) {
      body.append(createEmptyRow(7, '暂无已核验报价。收到新的实时核价结果后会自动记录。'));
      return;
    }
    records.slice(0, 200).forEach((record) => {
      const row = document.createElement('tr');
      const identity = document.createElement('td');
      const buyer = document.createElement('strong'); buyer.textContent = record.buyer_label || '匿名买家';
      const shop = document.createElement('small'); shop.className = 'table-muted'; shop.textContent = record.shop_name || record.account_unb || '未知店铺';
      identity.append(buyer, document.createElement('br'), shop);
      const screening = document.createElement('td');
      screening.append(document.createTextNode(record.cinema || '未记录影院'), document.createElement('br'));
      const screeningLine = document.createElement('small'); screeningLine.className = 'table-muted';
      screeningLine.textContent = [record.movie, record.date, record.showtime, record.hall].filter(Boolean).join(' · ') || '场次身份未完整记录';
      screening.append(screeningLine);
      const price = document.createElement('td');
      const unit = Number.isSafeInteger(record.quote_unit_cents) ? `${centsToYuan(record.quote_unit_cents)}元/张` : '单价未记录';
      const total = Number.isSafeInteger(record.quote_total_cents) ? `${record.quote_ticket_count || '?'}张 ${centsToYuan(record.quote_total_cents)}元` : '合计未记录';
      const cost = Number.isSafeInteger(record.member_cost_total_cents) ? `实时会员成本 ${centsToYuan(record.member_cost_total_cents)}元` : '实时成本未完整保存';
      price.append(document.createTextNode(unit), document.createElement('br'));
      const totalLine = document.createElement('small'); totalLine.className = 'table-muted'; totalLine.textContent = `${total}；${cost}`; price.append(totalLine);
      const conversion = document.createElement('td');
      conversion.append(document.createTextNode(`${formatRate(record.screening_order_success_rate)} (${record.screening_order_created_count ?? 0}/${record.screening_sample_size ?? 0})`), document.createElement('br'));
      const paidLine = document.createElement('small'); paidLine.className = 'table-muted'; paidLine.textContent = `付款成功 ${formatRate(record.screening_paid_success_rate)}`; conversion.append(paidLine);
      row.append(
        textCell(formatDateTime(record.quoted_at || record.updated_at), 'table-muted'), identity,
        screening, price, conversion, statusCell(record.stage), sessionActionCell(record),
      );
      body.append(row);
    });
  }

  function renderOrderManagement() {
    const fulfillmentByOrder = new Map(state.ticketOrders.filter((record) => record.order_id).map((record) => [record.order_id, record]));
    const records = state.operations.filter((record) => record.order_id).map((record) => ({ ...record, ...(fulfillmentByOrder.get(record.order_id) ?? {}) }));
    state.ticketOrders.forEach((record) => { if (record.order_id && !records.some((item) => item.order_id === record.order_id)) records.push(record); });
    const body = elements['ticket-orders-body'];
    setStatusPill(elements['ticket-orders-count'], records.length ? 'pending' : 'idle', records.length ? `${records.length} 笔` : '暂无订单');
    body.replaceChildren();
    if (!records.length) {
      body.append(createEmptyRow(7, '暂无已关联订单。买家下单并成功关联后会自动显示。'));
      return;
    }
    records.slice(0, 200).forEach((record) => {
      const row = document.createElement('tr');
      const identity = document.createElement('td');
      const buyer = document.createElement('strong'); buyer.textContent = record.buyer_label || '匿名买家';
      const shop = document.createElement('small'); shop.className = 'table-muted'; shop.textContent = record.shop_name || record.account_unb || '未知店铺';
      identity.append(buyer, document.createElement('br'), shop);
      const pluginStatus = statusCell(record.stage);
      const exceptionHint = exceptionReasonText(record.exception_reason);
      if (exceptionHint) {
        const hint = document.createElement('small'); hint.className = 'table-muted'; hint.textContent = exceptionHint;
        pluginStatus.append(document.createElement('br'), hint);
      }
      const confirmedTotal = Number.isSafeInteger(record.quote_total_cents)
        ? `${record.quote_ticket_count || '?'}张 ${centsToYuan(record.quote_total_cents)}元`
        : '未记录确认报价';
      row.append(
        textCell(record.order_id || '未关联', record.order_id ? '' : 'table-muted'), identity,
        textCell(confirmedTotal, Number.isSafeInteger(record.quote_total_cents) ? '' : 'table-muted'),
        platformOrderStatusCell(record), ticketIssuanceCell(record, pluginStatus),
        textCell(formatDateTime(record.platform_pay_time || record.fulfillment_updated_at || record.updated_at), 'table-muted'), sessionActionCell(record),
      );
      body.append(row);
    });
  }

  function exceptionReasonText(reason) {
    return ({
      PRICE_CHANGE_AUTHORIZATION_FAILED: '改价授权失败：请在鱼麦多“已订阅服务”中重新授权本插件，并勾选订单改价权限。',
      PRICE_CHANGE_UPSTREAM_REJECTED: '平台上游拒绝改价：请在数据交换日志查看“失败原因”，并凭订单号和调用时间联系官方排查。',
      CANNOT_MODIFY_FEE: '平台拒绝修改该订单金额，请保持不付款并人工处理。',
      price_change_gate_human_takeover: '检测到人工客服接管，本次未执行自动改价。',
      paid_quote_unconfirmed_or_expired: '存在插件报价，但付款时未确认或已过期，请人工核对。',
      paid_amount_unverifiable: '平台未返回可核验的实付金额，请人工核对。',
      paid_amount_mismatch: '实付金额与确认报价不一致，请人工核对。',
      ticket_count_conflict: '买家张数与官方已选座张数冲突，请人工核对。',
    })[String(reason ?? '')] ?? (reason ? `异常代码：${String(reason).slice(0, 100)}` : '');
  }

  function platformOrderStatusCell(record) {
    const cell = document.createElement('td');
    const text = String(record.platform_order_status_text ?? '').trim();
    if (record.platform_read_status === 'available') {
      const title = document.createElement('strong');
      title.textContent = text || (record.platform_order_status != null ? `状态码 ${record.platform_order_status}` : '闲鱼未提供状态文案');
      cell.append(title);
      if (Number.isSafeInteger(record.platform_payment_cents)) {
        const payment = document.createElement('small'); payment.className = 'table-muted'; payment.textContent = `闲鱼实付 ${centsToYuan(record.platform_payment_cents)}元`;
        cell.append(document.createElement('br'), payment);
      }
    } else {
      cell.className = 'table-muted'; cell.textContent = '闲鱼订单暂时读取失败';
    }
    return cell;
  }

  function ticketIssuanceCell(record, pluginStatus) {
    const cell = document.createElement('td');
    const ticketStatus = String(record.platform_ticket_status ?? 'unknown');
    const title = document.createElement(ticketStatus === 'issued' ? 'strong' : 'span');
    if (ticketStatus === 'issued') title.textContent = `已出票（依据：${record.platform_ticket_evidence || '闲鱼已发货'}）`;
    else if (ticketStatus === 'not_confirmed') {
      title.className = 'table-muted'; title.textContent = '尚不能确认已出票（闲鱼尚未发货）';
    } else {
      title.className = 'table-muted'; title.textContent = '暂时无法判断（闲鱼订单读取失败）';
    }
    cell.append(title, document.createElement('br'), pluginStatus);
    if (record.seat_delivery_instruction) {
      const instruction = document.createElement('small');
      instruction.className = 'table-muted';
      instruction.textContent = `${record.seat_delivery_instruction}${record.seat_delivery_image_recorded ? '（原图已关联）' : '（请查看原聊天图）'}`;
      cell.append(document.createElement('br'), instruction);
    }
    return cell;
  }

  function renderShops() {
    const shops = Array.isArray(state.shops) ? state.shops : [];
    const body = elements['shops-body'];
    const countLabel = shops.length ? `${shops.length} 家店铺` : '暂无数据';
    setStatusPill(elements['shops-count'], shops.length ? 'running' : 'idle', countLabel);
    body.replaceChildren();
    if (shops.length === 0) {
      body.append(createEmptyRow(6, '暂无已授权店铺。请确认当前鱼麦多账号已授权店铺后，再点击“同步店铺”。'));
      return;
    }
    shops.forEach((shop) => {
      const row = document.createElement('tr');
      const name = document.createElement('td');
      const title = document.createElement('strong');
      const account = document.createElement('small');
      title.textContent = shop.shop_name || shop.account_unb;
      account.className = 'table-muted';
      account.textContent = shop.account_unb;
      name.append(title, document.createElement('br'), account);
      row.append(
        name,
        featureStatusCell(Boolean(state.settings.recognition_enabled)),
        featureStatusCell(Boolean(state.settings.quote_enabled)),
        featureStatusCell(Boolean(state.settings.ai_reply_enabled)),
        featureStatusCell(Boolean(state.settings.price_change_enabled)),
        shopToggleCell(shop),
      );
      body.append(row);
    });
  }

  function featureStatusCell(enabled) {
    const cell = document.createElement('td');
    const pill = document.createElement('span');
    setStatusPill(pill, enabled ? 'running' : 'paused', enabled ? '已启用' : '已关闭');
    cell.append(pill);
    return cell;
  }

  function shopToggleCell(shop) {
    const cell = document.createElement('td');
    const label = document.createElement('label');
    const input = document.createElement('input');
    const control = document.createElement('span');
    const text = document.createElement('span');
    label.className = 'shop-toggle';
    input.type = 'checkbox';
    input.checked = shop.automation_enabled === true;
    input.setAttribute('aria-label', `${shop.shop_name || shop.account_unb} 自动化开关`);
    control.className = 'toggle-control';
    text.textContent = input.checked ? '已开启' : '已关闭';
    input.addEventListener('change', () => void updateShopEnabled(shop.account_unb, input.checked));
    label.append(input, control, text);
    cell.append(label);
    return cell;
  }

  async function refreshShops() {
    setButtonBusy(elements['sync-shops'], true, '正在同步…');
    try {
      const shops = await requestJson(API.shops);
      state.shops = Array.isArray(shops) ? shops : [];
      renderShops();
      notify('店铺列表已同步');
    } catch (error) {
      notify(readableError(error), 'error');
    } finally {
      setButtonBusy(elements['sync-shops'], false);
    }
  }

  async function updateShopEnabled(accountUnb, automationEnabled) {
    try {
      const updated = await requestJson(`${API.shops}/${encodeURIComponent(accountUnb)}`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ automation_enabled: automationEnabled }),
      });
      state.shops = state.shops.map((shop) => shop.account_unb === updated.account_unb
        ? { ...shop, automation_enabled: updated.automation_enabled }
        : shop);
      renderShops();
      notify(`店铺自动化已${updated.automation_enabled ? '开启' : '关闭'}`);
    } catch (error) {
      renderShops();
      notify(readableError(error), 'error');
    }
  }

  function populateForms() {
    const settings = state.settings ?? defaultSettings;
    elements['wplus-adjustment'].value = centsToYuan(settings.wplus_adjustment_cents);
    elements['wplus-member-price-threshold'].value = centsToYuan(settings.wplus_member_price_threshold_cents);
    elements['regular-adjustment'].value = centsToYuan(settings.regular_adjustment_cents);
    elements['max-auto-order-amount'].value = centsToYuan(settings.max_auto_order_amount_cents);
    elements['automation-enabled'].checked = Boolean(settings.automation_enabled);
    elements['quote-enabled'].checked = Boolean(settings.quote_enabled);
    elements['price-change-enabled'].checked = Boolean(settings.price_change_enabled);
    renderReplyTemplateFields(settings.reply_templates ?? defaultSettings.reply_templates);
    elements['ai-base-url'].value = settings.ai_base_url ?? '';
    elements['ai-model'].value = settings.ai_model ?? '';
    elements['ai-api-key'].value = '';
    elements['clear-ai-api-key'].checked = false;
    elements['minimum-confidence'].value = Math.round(Number(settings.minimum_confidence ?? 0.9) * 100);
    elements['ai-reply-enabled'].checked = Boolean(settings.ai_reply_enabled);
    elements['conversation-agent-mode'].value = settings.conversation_agent_mode ?? defaultSettings.conversation_agent_mode;
    elements['ai-reply-system-prompt'].value = settings.ai_reply_system_prompt ?? defaultSettings.ai_reply_system_prompt;
    elements['ai-reply-shop-background'].value = settings.ai_reply_shop_background ?? '';
    elements['ai-reply-precautions'].value = settings.ai_reply_precautions ?? '';
    elements['ai-reply-style'].value = settings.ai_reply_style ?? '';
    elements['ai-reply-memory-hours'].value = settings.ai_reply_memory_hours ?? defaultSettings.ai_reply_memory_hours;
    elements['ai-reply-memory-depth'].value = settings.ai_reply_memory_depth ?? defaultSettings.ai_reply_memory_depth;
    elements['ai-reply-delay-seconds'].value = settings.ai_reply_delay_seconds ?? defaultSettings.ai_reply_delay_seconds;
    elements['ai-reply-manual-takeover-seconds'].value = settings.ai_reply_manual_takeover_seconds ?? defaultSettings.ai_reply_manual_takeover_seconds;
    setStatusPill(
      elements['model-key-status'],
      settings.ai_key_configured ? 'configured' : 'needs_configuration',
      settings.ai_key_configured ? '密钥已配置' : '密钥待配置',
    );
  }

  async function savePricingSettings(event) {
    event.preventDefault();
    if (!elements['pricing-form'].reportValidity()) return;
    const patch = {
      wplus_adjustment_cents: yuanToCents(elements['wplus-adjustment'].value),
      wplus_member_price_threshold_cents: yuanToCents(elements['wplus-member-price-threshold'].value),
      regular_adjustment_cents: yuanToCents(elements['regular-adjustment'].value),
      max_auto_order_amount_cents: yuanToCents(elements['max-auto-order-amount'].value),
      automation_enabled: elements['automation-enabled'].checked,
      quote_enabled: elements['quote-enabled'].checked,
      price_change_enabled: elements['price-change-enabled'].checked,
    };
    await saveSettings(patch, elements['pricing-form'], elements['pricing-save-status']);
  }

  function renderReplyTemplateFields(templates) {
    const labels = {
      first_contact_notice: '首次进线流程说明', quote_confirmation_instruction: '完整报价后的确认指令',
      quote_processing_notice: '实时核价进行中提示', quote_replaced_by_official_selection: '官方选座替换未选座试价',
      quote_exact: '官方已选座报价', quote_area: '标记位置/未选座完整报价', quote_need_count: '标记位置/未选座待补张数',
      quote_count_completed: '买家补充张数后的下单提示', quote_buyer_app_better_price: '买家APP价格更合适', available_wplus_seats: '指定排可选W+座位',
      manual_delivery_preference: '按原图圈选位置出票', wplus_area_unavailable: 'W+ 区域无法核验',
      wplus_seats_unavailable: 'W+ 可用座位为 0', wplus_price_unavailable: 'W+ 会员优惠不可用',
      quote_price_conflict: '会员优惠与原价冲突', insufficient_available_seats: '可用座位不足', cinema_catalog_not_unique: '官方影院库无法唯一匹配',
      showtime_not_unique: '场次无法唯一匹配', showtime_not_found: '官方场次未找到', official_selection_unverifiable: '官方已选座当前不可用', image_not_seat_map: '非座位图', order_paid: '订单已付款',
      need_image: '缺少座位图', text_quote_missing_fields: '文字询价缺少购票信息', non_wanda_cinema: '非万达影院',
      ticket_count_conflict: '张数冲突', wplus_account_unavailable: 'W+ 账号不可用',
      temporary_lock_failed: '临时试价失败', temporary_lock_release_unverified: '临时试价释放未确认',
      wanda_gateway_unavailable: '万达网关不可用', quote_verification_failed: '通用核价失败', recognition_failed: '识别失败',
      order_submit_before_payment: '提交订单提示', order_price_change_failed: '订单改价失败',
      price_change_authorization_failed: '平台改价授权失败', paid_amount_mismatch: '付款金额不一致',
      paid_quote_unconfirmed: '已付款但报价未确认', paid_manual_delivery: '已付款人工出票',
    };
    const target = elements['reply-template-fields']; target.replaceChildren();
    const configuredImages = state.settings.reply_template_images ?? {};
    REPLY_TEMPLATE_GROUPS.forEach((group, groupIndex) => {
      const section = document.createElement('details'); section.className = 'reply-template-group'; section.dataset.group = group.id; section.open = groupIndex < 2;
      const summary = document.createElement('summary');
      const summaryCopy = document.createElement('span');
      const summaryTitle = document.createElement('strong'); summaryTitle.textContent = group.title;
      const summaryDescription = document.createElement('small'); summaryDescription.textContent = group.description;
      const count = document.createElement('span'); count.className = 'reply-template-group-count'; count.textContent = `${group.keys.length} 条`;
      summaryCopy.append(summaryTitle, summaryDescription); summary.append(summaryCopy, count);
      const grid = document.createElement('div'); grid.className = 'reply-template-card-grid';
      group.keys.forEach((key) => {
        const fallback = defaultSettings.reply_templates[key];
        const field = document.createElement('article'); field.className = 'reply-template-card'; field.dataset.search = `${labels[key] || key} ${templates[key] ?? fallback}`.toLocaleLowerCase('zh-CN');
        const cardHeader = document.createElement('div'); cardHeader.className = 'reply-template-card-header';
        const title = document.createElement('label'); title.htmlFor = `reply-template-${key}`; title.textContent = labels[key] || key;
        const imageUrl = String(configuredImages[key] ?? '').trim();
        const imageBadge = document.createElement('span'); imageBadge.className = `reply-template-image-badge${imageUrl ? ' is-configured' : ''}`; imageBadge.textContent = imageUrl ? '已配图' : '无配图';
        cardHeader.append(title, imageBadge);
        const input = document.createElement('textarea'); input.id = `reply-template-${key}`; input.maxLength = 500; input.rows = 4; input.required = true; input.value = templates[key] ?? fallback;
        input.addEventListener('input', () => {
          field.dataset.search = `${labels[key] || key} ${input.value}`.toLocaleLowerCase('zh-CN');
          elements['reply-templates-save-status'].textContent = '有未保存的文字修改';
        });
        const attachment = document.createElement('details'); attachment.className = 'reply-template-attachment'; attachment.open = Boolean(imageUrl);
        const attachmentSummary = document.createElement('summary'); attachmentSummary.textContent = imageUrl ? '管理配图' : '添加配图';
        const imageTools = document.createElement('div'); imageTools.className = 'reply-template-image-tools';
        const help = document.createElement('small'); help.className = 'table-muted'; help.textContent = 'PNG、JPEG、WEBP，最大5MB。图片上传后立即保存，不需要再点底部按钮。';
        const file = document.createElement('input'); file.type = 'file'; file.accept = '.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp'; file.setAttribute('aria-label', `${labels[key] || key} 选择回复图片`);
        const upload = document.createElement('button'); upload.type = 'button'; upload.className = 'button button-secondary'; upload.textContent = '上传并保存图片'; upload.disabled = true;
        const uploadStatus = document.createElement('small'); uploadStatus.className = 'table-muted'; uploadStatus.setAttribute('aria-live', 'polite'); uploadStatus.textContent = '尚未选择图片';
        file.addEventListener('change', () => {
          const selected = file.files?.[0];
          upload.disabled = !selected;
          uploadStatus.textContent = selected ? `已选择 ${selected.name} · ${(selected.size / 1024 / 1024).toFixed(2)}MB` : '尚未选择图片';
        });
        upload.addEventListener('click', () => uploadReplyTemplateImage(key, file, upload, uploadStatus));
        const link = document.createElement('a'); link.className = 'reply-template-image-link'; link.textContent = imageUrl || '当前没有配图';
        if (imageUrl) { link.href = imageUrl; link.target = '_blank'; link.rel = 'noreferrer'; }
        imageTools.append(help, file, upload, uploadStatus, link);
        if (imageUrl) {
          const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'button button-danger button-compact'; remove.textContent = '移除图片';
          remove.addEventListener('click', () => removeReplyTemplateImage(key, remove));
          imageTools.append(remove);
        }
        attachment.append(attachmentSummary, imageTools);
        field.append(cardHeader, input, attachment); grid.append(field);
      });
      section.append(summary, grid); target.append(section);
    });
    filterReplyTemplates();
  }

  function filterReplyTemplates() {
    const query = normalizeSearch(elements['reply-template-search']?.value);
    document.querySelectorAll('.reply-template-group').forEach((group) => {
      let visible = 0;
      group.querySelectorAll('.reply-template-card').forEach((card) => {
        const matches = !query || String(card.dataset.search ?? '').includes(query);
        card.hidden = !matches;
        if (matches) visible += 1;
      });
      group.hidden = visible === 0;
      if (query && visible > 0) group.open = true;
    });
  }

  function replyTemplateKeys() {
    const backendTemplates = state.settings?.reply_templates;
    return backendTemplates && typeof backendTemplates === 'object' && !Array.isArray(backendTemplates)
      ? Object.keys(backendTemplates)
      : Object.keys(defaultSettings.reply_templates);
  }

  function currentReplyTemplateDrafts() {
    return Object.fromEntries(replyTemplateKeys().map((key) => {
      const input = document.getElementById(`reply-template-${key}`);
      return [key, input ? input.value : (state.settings.reply_templates?.[key] ?? defaultSettings.reply_templates[key] ?? '')];
    }));
  }

  async function uploadReplyImageInChunks(input, status) {
    // Fish-Mai-Duo's authenticated plugin gateway rejects larger request
    // bodies before forwarding them to the plugin. Keep every signed JSON
    // envelope below 64 KiB while retaining the original image bytes.
    const chunkSize = 48 * 1024;
    const total = Math.ceil(input.data_base64.length / chunkSize);
    const uploadId = globalThis.crypto?.randomUUID?.().replaceAll('-', '')
      ?? `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.padEnd(16, '0');
    let result = null;
    for (let index = 0; index < total; index += 1) {
      status.textContent = `正在上传图片 ${index + 1}/${total}…`;
      result = await requestJson(API.replyTemplateImageChunks, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          upload_id: uploadId,
          key: input.key,
          filename: input.filename,
          content_type: input.content_type,
          index,
          total,
          data_base64_chunk: input.data_base64.slice(index * chunkSize, (index + 1) * chunkSize),
        }),
      });
    }
    return result;
  }

  async function uploadReplyTemplateImage(key, input, button, status) {
    const file = input.files?.[0];
    if (!file) { status.textContent = '请先选择图片。'; return; }
    const normalizedType = file.type === 'image/jpg' ? 'image/jpeg' : file.type;
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(normalizedType) || file.size < 1 || file.size > 5 * 1024 * 1024) {
      status.textContent = '上传失败：只支持5MB以内的PNG、JPEG或WEBP图片。';
      notify(status.textContent, 'error');
      return;
    }
    input.disabled = true;
    button.disabled = true;
    status.textContent = '正在上传并保存图片...';
    try {
      const data_base64 = await fileToBase64(file);
      const drafts = currentReplyTemplateDrafts();
      const result = await uploadReplyImageInChunks({ key, filename: file.name, content_type: normalizedType, data_base64 }, status);
      if (!result?.image_url) throw new Error('图片上传成功但没有返回链接');
      state.settings.reply_template_images = { ...(state.settings.reply_template_images ?? {}), [key]: result.image_url };
      renderReplyTemplateFields(drafts);
      notify('回复图片已上传并保存，图片链接已显示。', 'success');
    } catch (error) {
      input.disabled = false;
      button.disabled = false;
      status.textContent = `上传失败：${readableError(error)}`;
      notify(status.textContent, 'error');
    }
  }

  async function removeReplyTemplateImage(key, button) {
    button.disabled = true;
    const images = Object.fromEntries(replyTemplateKeys().map((templateKey) => [
      templateKey,
      templateKey === key ? '' : String(state.settings.reply_template_images?.[templateKey] ?? ''),
    ]));
    try {
      await saveSettings({ reply_template_images: images }, elements['reply-templates-form'], elements['reply-templates-save-status']);
    } catch {
      button.disabled = false;
    }
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error('图片读取失败'));
      reader.onload = () => resolve(String(reader.result ?? '').split(',', 2)[1] ?? '');
      reader.readAsDataURL(file);
    });
  }

  async function saveReplyTemplates(event) {
    event.preventDefault();
    if (!elements['reply-templates-form'].reportValidity()) {
      elements['reply-templates-save-status'].textContent = '保存失败：每条文字文案都不能为空。';
      notify('请补全为空的回复文案后再保存。', 'error');
      return;
    }
    const reply_templates = Object.fromEntries(Object.entries(currentReplyTemplateDrafts()).map(([key, value]) => [key, String(value).trim()]));
    await saveSettings({ reply_templates }, elements['reply-templates-form'], elements['reply-templates-save-status']);
  }

  async function saveModelSettings(event) {
    event.preventDefault();
    if (!elements['model-form'].reportValidity()) return;
    const patch = {
      ai_base_url: elements['ai-base-url'].value.trim(),
      ai_model: elements['ai-model'].value.trim(),
      minimum_confidence: Number(elements['minimum-confidence'].value) / 100,
      ai_reply_enabled: elements['ai-reply-enabled'].checked,
      conversation_agent_mode: elements['conversation-agent-mode'].value,
      ai_reply_system_prompt: elements['ai-reply-system-prompt'].value.trim(),
      ai_reply_shop_background: elements['ai-reply-shop-background'].value.trim(),
      ai_reply_precautions: elements['ai-reply-precautions'].value.trim(),
      ai_reply_style: elements['ai-reply-style'].value.trim(),
      ai_reply_memory_hours: Number(elements['ai-reply-memory-hours'].value),
      ai_reply_memory_depth: Number(elements['ai-reply-memory-depth'].value),
      ai_reply_delay_seconds: Number(elements['ai-reply-delay-seconds'].value),
      ai_reply_manual_takeover_seconds: Number(elements['ai-reply-manual-takeover-seconds'].value),
    };
    const apiKey = elements['ai-api-key'].value.trim();
    if (apiKey) patch.ai_api_key = apiKey;
    if (elements['clear-ai-api-key'].checked) patch.clear_ai_api_key = true;
    await saveSettings(patch, elements['model-form'], elements['model-save-status']);
    elements['ai-api-key'].value = '';
    elements['clear-ai-api-key'].checked = false;
  }

  function settingsPatchMatches(patch, settings) {
    if (!settings || typeof settings !== 'object') return false;
    return Object.entries(patch).every(([key, value]) => {
      if (key === 'ai_api_key') return settings.ai_key_configured === true;
      if (key === 'clear_ai_api_key') return value !== true || settings.ai_key_configured === false;
      return JSON.stringify(settings[key]) === JSON.stringify(value);
    });
  }

  async function saveSettings(patch, form, statusElement) {
    setFormBusy(form, true);
    statusElement.textContent = '正在保存并复核...';
    let committed = false;
    try {
      let writeError = null;
      try {
        await requestJson(API.settings, {
          method: 'PUT',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(patch),
        });
      } catch (error) {
        writeError = error;
      }
      // Always read the authoritative state back, even when PUT returned 200.
      // This catches partial writes and also recovers from a gateway 500 that
      // happened after the backend had already committed the update.
      const confirmed = await requestJson(API.settings);
      if (!settingsPatchMatches(patch, confirmed)) {
        throw writeError ?? new Error('设置写入后端复核不一致，请刷新后重试。');
      }
      committed = true;
      state.settings = { ...defaultSettings, ...confirmed };
      if (state.overview) {
        state.overview.settings = state.settings;
        state.overview.plugin = {
          ...state.overview.plugin,
          status: state.settings.automation_enabled ? 'running' : 'paused',
          updated_at: new Date().toISOString(),
        };
      }
      try {
        renderHeader();
        renderRuleSummary();
        renderConversationLearningSummary();
        populateForms();
      } catch (renderError) {
        statusElement.textContent = '已保存，但页面状态刷新失败';
        notify('设置已由后端复核保存，请刷新页面查看最新状态。', 'warning');
        return true;
      }
      statusElement.textContent = writeError ? '已保存，并已从后端复核' : '已保存并完成后端复核';
      notify(writeError ? '设置已保存；平台返回异常，但后端回读复核一致。' : '设置已保存并完成后端复核。');
      return true;
    } catch (error) {
      statusElement.textContent = committed ? '已保存，但页面状态刷新失败' : '保存失败';
      notify(committed ? '设置已保存，请刷新页面查看最新状态。' : readableError(error), committed ? 'warning' : 'error');
      return committed;
    } finally {
      setFormBusy(form, false);
    }
  }

  async function createKnowledgeEntry(event) {
    event.preventDefault();
    const form = elements['knowledge-base-form'];
    if (!form.reportValidity()) return;
    await saveKnowledgeRequest(API.knowledgeBase, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ category: elements['knowledge-category'].value, title: elements['knowledge-title'].value.trim(), content: elements['knowledge-content'].value.trim(), sort_order: Number(elements['knowledge-sort-order'].value) }),
    }, '知识条目已保存为草稿');
    form.reset();
    elements['knowledge-sort-order'].value = '0';
  }

  async function saveKnowledgeRequest(path, options, success) {
    const status = elements['knowledge-save-status'];
    try {
      status.textContent = '正在保存...';
      await requestJson(path, options);
      [state.knowledgeBase, state.conversationLearningSummary] = await Promise.all([
        requestJson(API.knowledgeBase),
        requestJson(API.conversationLearningSummary),
      ]);
      renderKnowledgeBase();
      renderConversationLearningSummary();
      status.textContent = success;
      notify(success);
    } catch (error) {
      status.textContent = '保存失败';
      notify(readableError(error), 'error');
    }
  }

  function renderKnowledgeBase() {
    const items = Array.isArray(state.knowledgeBase) ? state.knowledgeBase : [];
    const activeCount = items.filter((item) => item.status === 'approved' && item.enabled).length;
    const draftCount = items.filter((item) => item.status !== 'approved').length;
    elements['knowledge-count'].textContent = `共 ${items.length} 条 · 当前生效 ${activeCount} 条 · 草稿 ${draftCount} 条`;
    elements['knowledge-list'].replaceChildren(...items.map((item) => {
      const article = document.createElement('article');
      article.className = 'knowledge-item';
      const title = document.createElement('strong');
      title.textContent = item.title || '未命名条目';
      const detail = document.createElement('p');
      detail.textContent = item.content || '';
      const controls = document.createElement('div');
      controls.className = 'knowledge-controls';
      const stateText = document.createElement('span');
      const sourceLabel = item.source === 'conversation_experience'
        ? `会话提炼 · ${Math.max(1, Number(item.evidence_count) || 1)}次一致证据 · `
        : '';
      stateText.textContent = `${sourceLabel}${item.category} · ${item.status === 'approved' ? '已审核' : '草稿'} · ${item.enabled ? '已启用' : '已停用'}`;
      const approve = document.createElement('button');
      approve.type = 'button'; approve.className = 'button button-secondary';
      approve.textContent = item.status === 'approved' ? '改为草稿' : '审核通过';
      approve.addEventListener('click', () => saveKnowledgeRequest(`${API.knowledgeBase}/${encodeURIComponent(item.id)}`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ...item, status: item.status === 'approved' ? 'draft' : 'approved' }) }, '知识条目状态已更新'));
      const enabled = document.createElement('button');
      enabled.type = 'button'; enabled.className = 'button button-secondary';
      enabled.textContent = item.enabled ? '停用' : '启用';
      enabled.addEventListener('click', () => saveKnowledgeRequest(`${API.knowledgeBase}/${encodeURIComponent(item.id)}`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ...item, enabled: !item.enabled }) }, '知识条目状态已更新'));
      controls.append(stateText, approve, enabled);
      article.append(title, detail, controls);
      return article;
    }));
  }

  function renderReviewRecords() {
    const records = Array.isArray(state.overview?.review_records) ? state.overview.review_records : [];
    const filter = elements['review-filter'].value;
    const query = normalizeSearch(elements['review-search'].value);
    const filtered = records.filter((record) => {
      const matchesStatus = filter === 'all' || record.status === filter;
      const haystack = normalizeSearch([record.summary, record.error, record.order_id, record.event].filter(Boolean).join(' '));
      return matchesStatus && (!query || haystack.includes(query));
    });
    elements['review-count'].textContent = `${filtered.length} 条记录`;
    const body = elements['review-records-body'];
    body.replaceChildren();
    if (filtered.length === 0) {
      body.append(createEmptyRow(5, records.length ? '没有符合当前筛选条件的异常。' : '暂无待复核异常。'));
      return;
    }
    filtered.forEach((record) => {
      const row = document.createElement('tr');
      const issue = document.createElement('td');
      const summary = document.createElement('strong');
      const error = document.createElement('small');
      summary.textContent = record.summary || eventLabel(record.event);
      error.className = 'table-muted';
      error.textContent = record.error || '未提供错误摘要';
      issue.append(summary, document.createElement('br'), error);
      const reference = [record.order_id ? `订单 ${record.order_id}` : null, record.chat_id ? `会话 ${record.chat_id}` : null].filter(Boolean).join(' / ') || '暂无关联信息';
      const action = sessionActionCell(record);
      row.append(textCell(formatDateTime(record.updated_at), 'table-muted'), issue, textCell(reference), statusCell(record.status), action);
      body.append(row);
    });
  }

  function sessionActionControl(record) {
    if (!record?.chat_id) {
      const note = document.createElement('span');
      note.className = 'table-muted';
      note.textContent = record?.order_id ? '订单后台事件' : '无关联会话';
      return note;
    }
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'button button-secondary';
    button.textContent = '打开会话';
    button.addEventListener('click', () => openOriginalSession(record));
    return button;
  }

  function sessionActionCell(record) {
    const cell = document.createElement('td');
    cell.className = 'action-column';
    cell.append(sessionActionControl(record));
    if (record?.manual_task_id) {
      const resolve = document.createElement('button');
      resolve.type = 'button'; resolve.className = 'button button-secondary'; resolve.textContent = '标记已处理';
      resolve.addEventListener('click', async () => {
        resolve.disabled = true;
        try {
          await requestJson(`${API.manualTasks}/${encodeURIComponent(record.manual_task_id)}`, {
            method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ status: 'resolved' }),
          });
          notify('人工任务已标记为处理完成。', 'success');
          await loadData();
        } catch (error) {
          resolve.disabled = false;
          notify(`更新人工任务失败：${readableError(error)}`, 'error');
        }
      });
      cell.append(resolve);
    }
    return cell;
  }

  async function openOriginalSession(record) {
    if (!record.chat_id) {
      notify('该异常没有关联会话。', 'error');
      return;
    }
    if (!state.sdk || typeof state.sdk.navigateToImSession !== 'function') {
      notify(state.localPreview ? '本地调试入口不能打开鱼麦多会话。' : '当前平台 SDK 不支持会话跳转。', 'error');
      return;
    }
    try {
      await state.sdk.navigateToImSession({
        accountUnb: record.account_unb,
        chatId: record.chat_id,
        peerUnb: record.peer_unb,
      });
    } catch (error) {
      notify(`打开会话失败：${readableError(error)}`, 'error');
    }
  }

  function renderLogs() {
    const filter = elements['log-filter'].value;
    const query = normalizeSearch(elements['log-search'].value);
    const filtered = state.logs.filter((record) => {
      const matchesStatus = filter === 'all' || record.status === filter;
      const haystack = normalizeSearch([record.event, record.status, record.error, record.diagnostic].filter(Boolean).join(' '));
      return matchesStatus && (!query || haystack.includes(query));
    });
    elements['log-count'].textContent = `${filtered.length} 条记录`;
    const body = elements['logs-body'];
    body.replaceChildren();
    if (filtered.length === 0) {
      body.append(createEmptyRow(5, state.logs.length ? '没有符合当前筛选条件的日志。' : '暂无运行日志。'));
      return;
    }
    filtered.forEach((record) => {
      const row = document.createElement('tr');
      row.append(
        textCell(formatDateTime(record.time), 'table-muted'),
        textCell(eventLabel(record.event)),
        textCell(String(record.attempts ?? 0)),
        statusCell(record.status),
        textCell(record.error || record.diagnostic || '无', record.error || record.diagnostic ? '' : 'table-muted'),
      );
      body.append(row);
    });
  }

  function updateModeNotice() {
    elements['demo-notice'].hidden = !state.localPreview;
    elements['data-mode'].textContent = state.localPreview ? '安全管理面板' : '平台实时数据';
    elements['data-mode'].classList.toggle('is-demo', state.localPreview);
  }

  function setLoading(loading) {
    state.loading = loading;
    elements['app-shell'].setAttribute('aria-busy', String(loading));
    elements['loading-notice'].hidden = !loading;
    elements['refresh-button'].disabled = loading;
    if (loading) setButtonBusy(elements['refresh-button'], true, '刷新中...');
    else setButtonBusy(elements['refresh-button'], false);
  }

  function setButtonBusy(button, busy, busyLabel) {
    if (!button.dataset.label) button.dataset.label = button.textContent.trim();
    button.disabled = busy;
    button.textContent = busy ? busyLabel : button.dataset.label;
  }

  function setFormBusy(form, busy) {
    form.querySelectorAll('button, input, select').forEach((control) => {
      if (busy) {
        control.dataset.busyWasDisabled = control.disabled ? '1' : '0';
        control.disabled = true;
      } else if (Object.hasOwn(control.dataset, 'busyWasDisabled')) {
        control.disabled = control.dataset.busyWasDisabled === '1';
        delete control.dataset.busyWasDisabled;
      }
    });
  }

  function showError(message) {
    elements['error-message'].textContent = message;
    elements['error-notice'].hidden = false;
  }

  function clearError() { elements['error-notice'].hidden = true; }

  function notify(message, type = 'success') {
    if (state.sdk && typeof state.sdk.toast === 'function') {
      try {
        state.sdk.toast({ message, type });
        return;
      } catch {
        try {
          state.sdk.toast(message, type);
          return;
        } catch {
          // Fall through to the accessible local toast.
        }
      }
    }
    const toast = document.createElement('div');
    toast.className = `toast${type === 'error' ? ' is-error' : ''}`;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    toast.textContent = message;
    elements['toast-region'].append(toast);
    window.setTimeout(() => toast.remove(), 3600);
  }

  function setStatusPill(element, status, label) {
    element.className = `status-pill status-${status || 'unknown'}`;
    element.textContent = label;
  }

  function statusCell(status) {
    const cell = document.createElement('td');
    cell.className = 'status-cell';
    const pill = document.createElement('span');
    setStatusPill(pill, status, statusLabel(status));
    cell.append(pill);
    return cell;
  }

  function textCell(value, className = '') {
    const cell = document.createElement('td');
    if (className) cell.className = className;
    cell.textContent = value;
    return cell;
  }

  function textLine(value, className = '') {
    const line = document.createElement('div');
    if (className) line.className = className;
    line.textContent = value;
    return line;
  }

  function createEmptyRow(columns, message) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = columns;
    cell.className = 'table-empty';
    cell.textContent = message;
    row.append(cell);
    return row;
  }

  function createEmptyListItem(message) {
    const item = document.createElement('li');
    item.className = 'table-muted';
    item.textContent = message;
    return item;
  }

  function statusLabel(status) { return STATUS_LABELS[status] ?? String(status ?? '状态未知'); }
  function eventLabel(event) { return EVENT_LABELS[event] ?? String(event ?? '未知事件'); }
  function formatMetric(value) { return Number.isFinite(Number(value)) ? new Intl.NumberFormat('zh-CN').format(Number(value)) : '--'; }

  function formatDateTime(value) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '时间未知';
    return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
  }

  function formatAdjustment(cents) {
    const amount = Number(cents ?? 0) / 100;
    const sign = amount > 0 ? '+' : amount < 0 ? '-' : '';
    return `${sign}¥${Math.abs(amount).toFixed(2)}`;
  }

  function centsToYuan(cents) { return (Number(cents ?? 0) / 100).toFixed(2); }
  function formatRate(value) { return Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)}%` : '暂无'; }
  function formatYuan(cents) { return Number.isInteger(cents) ? `¥${(cents / 100).toFixed(2)}` : '--'; }
  function formatSignedYuan(cents) {
    if (!Number.isFinite(cents)) return '--';
    const prefix = cents > 0 ? '+' : cents < 0 ? '-' : '';
    return `${prefix}${formatYuan(Math.abs(cents))}`;
  }
  function yuanToCents(value) { return Math.round(Number(value) * 100); }
  function normalizeSearch(value) { return String(value ?? '').trim().toLocaleLowerCase('zh-CN'); }

  function readableError(error) {
    const code = String(error?.message ?? error ?? 'unknown_error');
    const messages = {
      invalid_gateway_context: '平台授权上下文无效，请从鱼麦多插件入口重新打开。',
      invalid_gateway_signature: '平台签名校验失败，请重新打开插件。',
      not_found: '请求的插件接口不存在。',
      internal_error: '插件服务暂时不可用，请稍后重试。',
      request_failed: '请求失败，请稍后重试。',
      body_too_large: '图片上传请求过大，请换5MB以内的图片。',
      invalid_reply_template_image: '图片格式或内容校验失败，请重新选择PNG、JPEG或WEBP原图。',
      invalid_reply_template_key: '回复文案已更新，请刷新页面后重新上传。',
      invalid_reply_template_image_chunk: '图片分片校验失败，请重新选择图片上传。',
      reply_template_image_chunk_conflict: '图片上传状态冲突，请重新选择图片上传。',
      too_many_image_uploads: '当前上传任务较多，请稍后重试。',
      image_fetch_failed: '图片链接无法从服务器访问：请确认链接可公开打开、未过期且没有防盗链限制。',
      image_fetch_timeout: '服务器下载图片链接超时，请换一个可公开访问的图片链接。',
      image_url_invalid: '仅支持可公开访问的 HTTPS 图片链接。',
      image_media_type_unsupported: '链接不是 PNG、JPEG 或 WEBP 图片。',
      image_content_mismatch: '链接返回的内容不是有效图片，请换原图链接。',
      image_too_large: '图片超过 5MB，请换较小的原图链接。',
      ai_key_required: '请先在“AI 模型”中保存可用的 AI Key。',
      prelabel_job_running: '已有预标注任务正在运行。',
      review_image_missing: '服务器上缺少这张样本图片。',
      review_image_hash_mismatch: '样本图片校验失败，已阻止审核。',
      invalid_review_label: '标签字段不完整或格式不正确，请检查后重试。',
      invalid_review_filter: '样本筛选条件无效。',
      review_sample_not_found: '该样本不存在或已被移除。',
      review_revision_required: '样本版本缺失，请刷新后重试。',
      review_revision_conflict: '样本已被其他审核人更新，请刷新后重新确认。',
    };
    return messages[code] ?? code.replace(/(?:sk|yp|pdk)_[A-Za-z0-9._-]+/gu, '[REDACTED]').slice(0, 180);
  }

})();
