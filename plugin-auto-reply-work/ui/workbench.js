import { createPluginSdk } from './sdk.js';
import { automationStatusFrom, blockerLabel, buildActionQueue, sampleSummaryFrom } from './workbench-model.js';

const API = Object.freeze({
  overview: 'api/overview', settings: 'api/settings', operations: 'api/operations', orders: 'api/orders',
  manualTasks: 'api/manual-tasks', readiness: 'api/agent-canary-readiness', image: 'api/agent-offline-evaluation',
  comparisons: 'api/agent-human-comparisons',
});

const state = {
  sdk: null, localPreview: false, loading: false, filter: 'all', settings: {}, overview: {}, operations: [], orders: [],
  manualTasks: [], readiness: {}, image: {}, comparisons: [], queue: [],
};
const elements = {};

document.addEventListener('DOMContentLoaded', init);

async function init() {
  for (const id of ['workbench-shell', 'connection-status', 'last-updated', 'refresh-button', 'retry-button', 'loading-banner', 'error-banner', 'error-message', 'runtime-mode', 'automation-rail', 'urgent-count', 'urgent-summary', 'action-queue', 'runtime-version', 'sample-cards', 'blocker-list', 'toast']) {
    elements[id] = document.getElementById(id);
  }
  elements.filters = [...document.querySelectorAll('[data-filter]')];
  document.querySelectorAll('[data-settings-hash]').forEach((link) => {
    link.href = settingsUrl(link.dataset.settingsHash);
  });
  elements['refresh-button'].addEventListener('click', loadData);
  elements['retry-button'].addEventListener('click', loadData);
  elements.filters.forEach((button) => button.addEventListener('click', () => {
    state.filter = button.dataset.filter;
    elements.filters.forEach((item) => item.classList.toggle('is-active', item === button));
    renderQueue();
  }));
  await loadData();
}

async function resolveSdk() {
  if (window.self === window.top) {
    return { localPreview: true, authedFetch: (path, options) => window.fetch(`/ui/${String(path).replace(/^\/+/, '')}`, options) };
  }
  const sdk = createPluginSdk({ pluginId: 'wanda-seat-autoquote' });
  if (typeof sdk?.ready === 'function') await sdk.ready();
  return sdk;
}

async function requestJson(path) {
  const response = await state.sdk.authedFetch(path);
  const payload = await response.json();
  if (!response.ok || payload?.ok === false) throw new Error(payload?.error || `HTTP ${response.status}`);
  return Object.prototype.hasOwnProperty.call(payload ?? {}, 'data') ? payload.data : payload;
}

async function loadData() {
  if (state.loading) return;
  state.loading = true;
  elements['workbench-shell'].setAttribute('aria-busy', 'true');
  elements['loading-banner'].hidden = false;
  elements['error-banner'].hidden = true;
  try {
    state.sdk ??= await resolveSdk();
    state.localPreview = state.sdk.localPreview === true;
    const requests = Object.entries(API).map(async ([key, path]) => [key, await requestJson(path)]);
    const settled = await Promise.allSettled(requests);
    const failures = [];
    for (const result of settled) {
      if (result.status === 'fulfilled') state[result.value[0]] = result.value[1];
      else failures.push(result.reason);
    }
    state.queue = buildActionQueue({ operations: state.operations, orders: state.orders, manualTasks: state.manualTasks });
    render();
    if (failures.length === settled.length) throw failures[0];
    if (failures.length) showError(`部分数据暂不可用（${failures.length}/${settled.length}），已保留可读取内容。`);
  } catch (error) {
    showError(readableError(error));
  } finally {
    state.loading = false;
    elements['workbench-shell'].setAttribute('aria-busy', 'false');
    elements['loading-banner'].hidden = true;
  }
}

function render() {
  elements['connection-status'].textContent = state.localPreview ? '本地预览' : '平台数据';
  elements['connection-status'].classList.add('is-ready');
  elements['last-updated'].textContent = `更新于 ${new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date())}`;
  renderAutomation();
  renderQueue();
  renderSamples();
}

function renderAutomation() {
  const settings = state.settings ?? {};
  elements['runtime-mode'].textContent = settings.conversation_agent_mode === 'shadow' ? 'Shadow · 确定性执行' : String(settings.conversation_agent_mode ?? '未配置');
  elements['automation-rail'].replaceChildren(...automationStatusFrom(settings).map((item) => {
    const card = document.createElement('div'); card.className = `automation-state tone-${item.tone}`;
    const label = document.createElement('span'); label.textContent = item.label;
    const value = document.createElement('strong'); value.textContent = item.enabled ? '已开启' : '已关闭';
    card.append(label, value); return card;
  }));
}

function filteredQueue() {
  if (state.filter === 'urgent') return state.queue.filter((item) => item.severity === 'urgent' || item.severity === 'high');
  if (state.filter === 'money') return state.queue.filter((item) => ['money-risk', 'price-change-risk'].includes(item.kind));
  if (state.filter === 'manual') return state.queue.filter((item) => item.kind === 'manual-task');
  return state.queue;
}

function renderQueue() {
  const urgent = state.queue.filter((item) => item.severity === 'urgent' || item.severity === 'high');
  elements['urgent-count'].textContent = String(urgent.length);
  elements['urgent-summary'].textContent = urgent.length ? '只统计有明确证据的金额、张数、改价风险和系统人工任务。' : '当前没有确定性交易风险。人工回复和人工出票属于正常工作，不算异常。';
  const records = filteredQueue();
  elements['action-queue'].replaceChildren();
  if (!records.length) {
    const empty = document.createElement('div'); empty.className = 'empty-state'; empty.textContent = '当前没有需要系统提醒的确定性风险。人工处理流程不计为异常。'; elements['action-queue'].append(empty); return;
  }
  records.slice(0, 80).forEach((record) => elements['action-queue'].append(queueItem(record)));
}

function queueItem(record) {
  const item = document.createElement('article'); item.className = `queue-item is-${record.severity}`;
  const marker = document.createElement('span'); marker.className = 'queue-marker'; marker.setAttribute('aria-hidden', 'true');
  const copy = document.createElement('div'); copy.className = 'queue-copy';
  const titleRow = document.createElement('div'); titleRow.className = 'queue-title-row';
  const title = document.createElement('strong'); title.textContent = record.title;
  const badge = document.createElement('span'); badge.className = 'queue-badge'; badge.textContent = kindLabel(record.kind);
  titleRow.append(title, badge);
  const detail = document.createElement('p'); detail.textContent = record.detail || '暂无补充说明';
  const meta = document.createElement('div'); meta.className = 'queue-meta';
  for (const text of [record.buyerLabel, record.shopName, record.orderId ? `订单 ${maskReference(record.orderId)}` : null, formatTime(record.updatedAt)]) {
    if (!text) continue; const span = document.createElement('span'); span.textContent = text; meta.append(span);
  }
  copy.append(titleRow, detail, meta);
  const action = document.createElement('button'); action.type = 'button'; action.className = 'button button-secondary queue-action'; action.textContent = record.chatId ? '打开会话' : '查看详情';
  action.addEventListener('click', () => openRecord(record));
  item.append(marker, copy, action); return item;
}

async function openRecord(record) {
  if (!record.chatId) { notify('该记录没有可打开的会话，请到订单管理或异常记录查看。'); return; }
  if (state.localPreview || typeof state.sdk?.navigateToImSession !== 'function') { notify('本地预览不能打开鱼麦多会话。'); return; }
  try {
    await state.sdk.navigateToImSession({ accountUnb: record.accountUnb, chatId: record.chatId, peerUnb: record.peerUnb });
  } catch (error) { notify(`打开会话失败：${readableError(error)}`); }
}

function renderSamples() {
  const summary = sampleSummaryFrom({ readiness: state.readiness, image: state.image, comparisons: state.comparisons });
  elements['runtime-version'].textContent = `当前版本：${summary.runtimeVersion}`;
  const definitions = [
    { label: '安全审计', data: summary.audit, note: summary.audit.rate == null ? '工具正确率待计算' : `工具正确率 ${summary.audit.rate}%` },
    { label: '图片工具链', data: summary.image, note: summary.image.rate == null ? '完整路径率待计算' : `完整路径率 ${summary.image.rate}%` },
    { label: '人工回复对照', data: { value: summary.human.value, target: Math.max(summary.human.value, 100) }, note: summary.human.versionScoped ? `待审核 ${summary.human.pending} 条` : `待审核 ${summary.human.pending} 条 · 尚未按Runtime区分` },
  ];
  elements['sample-cards'].replaceChildren(...definitions.map(sampleCard));
  const blockers = [...new Set([...(state.readiness?.blockers ?? []), ...(state.image?.blockers ?? [])])];
  elements['blocker-list'].replaceChildren();
  if (!blockers.length) {
    const clear = document.createElement('span'); clear.className = 'blocker-chip is-clear'; clear.textContent = '当前自动门槛无阻塞项'; elements['blocker-list'].append(clear);
  } else blockers.forEach((code) => { const chip = document.createElement('span'); chip.className = 'blocker-chip'; chip.textContent = blockerLabel(code); elements['blocker-list'].append(chip); });
}

function sampleCard(definition) {
  const card = document.createElement('article'); card.className = 'sample-card';
  const header = document.createElement('div'); header.className = 'sample-card-header';
  const label = document.createElement('span'); label.textContent = definition.label;
  const target = document.createElement('small'); target.textContent = `目标 ${definition.data.target}`; header.append(label, target);
  const value = document.createElement('div'); value.className = 'sample-value';
  const strong = document.createElement('strong'); strong.textContent = String(definition.data.value);
  const suffix = document.createElement('span'); suffix.textContent = `/ ${definition.data.target}`; value.append(strong, suffix);
  const progress = document.createElement('div'); progress.className = 'progress-track'; progress.setAttribute('role', 'progressbar');
  progress.setAttribute('aria-valuemin', '0'); progress.setAttribute('aria-valuemax', String(definition.data.target)); progress.setAttribute('aria-valuenow', String(Math.min(definition.data.value, definition.data.target)));
  const bar = document.createElement('i'); bar.style.width = `${Math.min(100, definition.data.target ? definition.data.value / definition.data.target * 100 : 0)}%`; progress.append(bar);
  const note = document.createElement('p'); note.textContent = definition.note; card.append(header, value, progress, note); return card;
}

function settingsUrl(hash) {
  let base = window.location.pathname.replace(/\/workbench(?:\.html)?\/?$/u, '/');
  if (/\/ui\/ui\/$/u.test(base)) base = base.replace(/\/ui\/$/u, '/');
  return `${base}#${encodeURIComponent(String(hash ?? ''))}`;
}

function kindLabel(kind) {
  return ({ 'money-risk': '金额风险', 'quantity-risk': '张数风险', 'price-change-risk': '改价风险', 'system-risk': '系统风险', 'manual-task': '系统任务' })[kind] ?? '风险核对';
}
function formatTime(value) {
  const date = new Date(value); return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date) : '';
}
function maskReference(value) { const text = String(value); return text.length > 8 ? `${text.slice(0, 3)}…${text.slice(-4)}` : text; }
function showError(message) { elements['error-message'].textContent = message; elements['error-banner'].hidden = false; }
function readableError(error) { return String(error?.message ?? error ?? '未知错误').slice(0, 240); }
function notify(message) { elements.toast.textContent = message; elements.toast.hidden = false; clearTimeout(notify.timer); notify.timer = setTimeout(() => { elements.toast.hidden = true; }, 3200); }
