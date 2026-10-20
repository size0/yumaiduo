// ★ 服务端注入的界面前缀（'/' 或 '/ui/'）。
//   为什么需要：界面可以挂在 /ui 下（nginx / 插件转发），
//   而 fetch('/api/...') 是【绝对】路径，不带前缀会打到根上去。
//   ★ 相对路径（config.css）由 <base href> 管，两者是两套机制。
//   ★★ 挂在 window 上而不是 const —— 一个页面会加载多个 JS，
//      各自 const BASE 会撞成 "Identifier 'BASE' has already been declared"，
//      而且第二个文件会【整段不执行】（运行时才炸，语法检查看不出来）。
(function () {
  if (typeof window === 'undefined' || window.__dshBaseReady) return;
  window.__dshBaseReady = true;
  window.__dshBase = window.__DSH_BASE__ || '/';
  window.withBase = function (p) {
    return (typeof p === 'string' && p.charAt(0) === '/')
      ? window.__dshBase.replace(/\/$/, '') + p : p;
  };
})();
/* ============================================================
   app.js —— 前端逻辑（原生 JS，无框架）

   为什么不用 React：你会看到整个交互逻辑，不用先学框架。
   等这两个页面做熟了，再换框架只是换个写法，思路一样。
   ============================================================ */

const $ = id => document.getElementById(id);
const api = async (path, options) => {
  const res = await fetch(window.withBase(path), options);
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error((data.problems || []).join('；') || data.error || '请求失败');
  return data;
};
const esc = s => (s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

let SETTINGS = null;
let CATALOG = null;
let CURRENT_STAGE = 'intake';
// ★★ 2026-10-14 店主：「回复文案只区分万达和良票，没有全局」
//   ⇒ 界面上去掉了「全局」页签，默认落在万达。
//   数据层保留基准与渠道覆盖，编辑只保存当前渠道。
let CURRENT_CHANNEL = 'wanda';
let EDITING = null;

/* ============================================================ 标签页
   ★ 2026-10-14：顶部菜单换成了左侧栏，选择器随之从 .top-tab
     改成 .ticket-nav-item。
   ★★ 为什么要判 data-tab：
     "AI助理" 和 "回复文案" 是【同一个页面 index.html 里的两块】，
     而"功能设置"等是独立页面（<a href> 直接跳转）。
     ⇒ 侧栏里前两项带 data-tab（点它只切 panel，不跳转），
       其余是普通链接（浏览器自己跳）。若一律 preventDefault，
       那两项之外就再也点不动了。 */
/* ★★ 2026-10-14 抽出 switchPanel()，并【按 URL 初始化】。
   为什么必须抽出来 + 初始化：
     `/replies` 和 `/` 服务端都是 index.html（同一页两块），
     原来只有【点击】时才切 active 和 panel ——
     直接在地址栏输 /replies、或从旧书签进来，看到的还是「AI助理」：
       实测：打开 /replies → 侧栏高亮 'AI助理'、显示面板 'panel-assistant'
     ⇒ 抽出函数，并让它在页面加载时按 location.pathname 跑一次。
   ★ 这是合并不合并都存在的 bug（不是这次引入的）。 */
function switchPanel(name) {
  document.querySelectorAll('.ticket-nav-item').forEach(t => { t.classList.remove('active'); t.removeAttribute('aria-current'); });
  const nav = document.querySelector('.ticket-nav-item[data-tab="' + name + '"]');
  if (nav) { nav.classList.add('active'); nav.setAttribute('aria-current','page'); }
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  const panel = $('panel-' + name);
  (panel || $('panel-placeholder')).classList.add('active');
}

document.querySelectorAll('.ticket-nav-item[data-tab]').forEach(tab => {
  tab.onclick = (ev) => {
    ev.preventDefault();
    switchPanel(tab.dataset.tab);
    const target = new URL(tab.getAttribute('href') || './', document.baseURI);
    target.search = location.search;
    if (location.pathname !== target.pathname) history.pushState({}, '', target.pathname + target.search);
  };
});

// ★ 按 URL 初始化面板：/replies → 回复文案，其余 → AI助理
function initPanelFromUrl() {
  const path = location.pathname.replace(/\/+$/, '') || '/';
  const base = (window.__DSH_BASE__ || '/').replace(/\/+$/, '');
  switchPanel(path === base + '/replies' ? 'replies' : 'assistant');
}
initPanelFromUrl();
window.addEventListener('popstate', initPanelFromUrl);

/* ============================================================ AI助理页
   ★ 数据来自 /api/assistant（不是 /api/settings）。
     两者分开的理由：
       /api/assistant  = 对话层怎么说话（人设、风格、记忆）
       /api/settings   = 机器怎么运转（报价有效期、票速、退款条件）
     混在一起，运营调语气时容易误动业务参数。 */
async function loadSettings() {
  SETTINGS = await api('/api/assistant');

  $('sw-enabled').classList.toggle('on', !!SETTINGS.agent_enabled);
  $('sw-enabled').setAttribute('aria-checked', String(!!SETTINGS.agent_enabled));
  $('takeover').value = SETTINGS.takeover_seconds;
  $('human_pause').value = SETTINGS.human_pause_seconds;
  $('memory_hours').value = SETTINGS.memory_hours;
  $('memory_messages').value = SETTINGS.memory_messages;

  // 模型下拉：从后端给的候选填充，不要在前端写死
  // ★★ 2026-10-14 店主定的：操作台上不许出现"光秃秃的英文"。
  //   模型名本身是英文标识符（改不了），所以显示成
  //   「极速版（deepseek-flash）」—— 中文名在前，英文名放括号里对照。
  //   ★ option 的 value 仍然是英文名（存进配置的就是它）。
  const MODEL_LABELS = SETTINGS.model_labels || {};
  $('model').innerHTML = (SETTINGS.model_options || [])
    .map(m => {
      const cn = MODEL_LABELS[m];
      const text = cn ? `${cn}（${m}）` : m;
      return `<option value="${m}"${m === SETTINGS.model_name ? ' selected' : ''}>${text}</option>`;
    }).join('');

  $('apikey').placeholder = SETTINGS.api_key_configured ? `已配置 · 尾号 ${SETTINGS.api_key_tail}，输入新密钥可更换` : '未配置，请填写模型 API Key';
  $('model-protocol').value = SETTINGS.model_protocol || 'deepseek';
  $('model-base').value = SETTINGS.model_base_url || '';
  $('custom-model').value = SETTINGS.custom_model_name || '';
  renderModelConnection();
  $('persona').value = SETTINGS.persona;
  $('precautions').value = SETTINGS.precautions;
  $('reply_style').value = SETTINGS.reply_style;
  // ★ 本店经验：模型答买家问题时优先照它答
  $('experience').value = SETTINGS.experience || '';
  $('effective-prompt').textContent = SETTINGS.effective_prompt;

  const scope = SETTINGS.business_scope || [];
  document.querySelectorAll('input[name=scope]').forEach(box => {
    box.checked = scope.includes('不限制') ? box.value === '不限制' : scope.includes(box.value);
  });

  $('sw-stage').classList.toggle('on', !!SETTINGS.stage_filter_enabled);
  $('sw-stage').setAttribute('aria-checked', String(!!SETTINGS.stage_filter_enabled));

  // 停止时机滑块的刻度由后端给（和状态机阶段对应）
  const labels = SETTINGS.stage_labels || ['咨询', '拍下', '付款', '发货/退款'];
  $('stage-slider').max = String(labels.length - 1);
  $('stage-slider').value = String(Math.max(0, (SETTINGS.allowed_stages || []).length - 1));
  const marks = document.querySelector('.slider-marks');
  if (marks) marks.innerHTML = labels.map(l => `<span>${l}</span>`).join('');

  ['persona', 'precautions', 'reply_style', 'experience'].forEach(field => {
    updateCount(field);
    $(field).oninput = () => updateCount(field);
  });

  $('sw-enabled').onclick = () => {
    const on = !$('sw-enabled').classList.contains('on');
    $('sw-enabled').classList.toggle('on', on);
    $('sw-enabled').setAttribute('aria-checked', String(on));
    saveSettings({ agent_enabled: on }).then(ok => { if (!ok) { $('sw-enabled').classList.toggle('on', !on); $('sw-enabled').setAttribute('aria-checked', String(!on)); } });
  };
  $('sw-stage').onclick = () => {
    const on = !$('sw-stage').classList.contains('on');
    $('sw-stage').classList.toggle('on', on);
    $('sw-stage').setAttribute('aria-checked', String(on));
    renderSlider();
    saveSettings({ stage_filter_enabled: on }).then(ok => { if (!ok) { $('sw-stage').classList.toggle('on', !on); $('sw-stage').setAttribute('aria-checked', String(!on)); renderSlider(); } });
  };
  $('stage-slider').oninput = renderSlider;
  $('stage-slider').onchange = () => saveSettings({ allowed_stages: stageKeys().slice(0, Number($('stage-slider').value)+1) });
  document.querySelectorAll('input[name=scope]').forEach(box => { box.onchange = () => saveSettings({ business_scope: Array.from(document.querySelectorAll('input[name=scope]:checked'), b => b.value) }); });
  renderSlider();
  window.markShopReady?.('assistant');
}

function updateCount(field) {
  const el = $(field);
  $('c-' + field).textContent = `${el.value.length} / 2000`;
}

/* 停止时机：滑块位置对应允许 AI 回复的阶段 */
function stageKeys() { return SETTINGS?.stage_keys || ['intake','quote','order','after_sale']; }
function renderSlider() {
  const idx = Number($('stage-slider').value);
  const labels = SETTINGS?.stage_labels || ['咨询','拍下','付款','发货/退款'];
  const enabled = $('sw-stage').classList.contains('on');
  $('stage-slider').disabled = !enabled;
  document.querySelectorAll('.slider-marks span').forEach((s,i) => s.classList.toggle('active', enabled && i<=idx));
  $('stage-hint').textContent = !enabled ? '当前不限制阶段：AI 可在咨询、拍下、付款及售后阶段回复。关闭上方「启用AI助理」才会停止 AI 回复。'
    : idx===labels.length-1 ? '所有阶段均允许 AI 回复，包括售后。' : `允许回复到「${labels[idx]}」阶段（含该阶段）；之后停止 AI 文字回复。`;
}
let SAVE_QUEUE = Promise.resolve();
function saveSettings(patch) {
  if (window.shopPageReady && !window.shopPageReady('assistant')) return Promise.resolve(false);
  const task = async () => {
    const status = $('assistant-save-status');
    status.textContent = '保存中…';
    try {
      await api('/api/assistant', {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)});
      SETTINGS = await api('/api/assistant');
      $('effective-prompt').textContent = SETTINGS.effective_prompt;
      status.textContent = '已保存并生效';
      return true;
    } catch(e) { status.textContent = '未保存：' + e.message; return false; }
  };
  SAVE_QUEUE = SAVE_QUEUE.then(task, task);
  return SAVE_QUEUE;
}
$('btn-save-assistant').onclick = async () => {
  if (!await saveModelConnection()) return false;
  const patch = {agent_enabled:$('sw-enabled').classList.contains('on'),stage_filter_enabled:$('sw-stage').classList.contains('on'),allowed_stages:stageKeys().slice(0,Number($('stage-slider').value)+1),model_name:$('model').value,business_scope:Array.from(document.querySelectorAll('input[name=scope]:checked'),b=>b.value)};
  for(const field of ['persona','precautions','reply_style','experience'])patch[field]=$(field).value;
  for(const field of ['memory_hours','memory_messages'])patch[field]=Number($(field).value);
  $('btn-save-assistant').disabled = true;
  try { return await saveSettings(patch); } finally { $('btn-save-assistant').disabled = false; }
};

function renderModelConnection() {
  const custom = $('model-protocol').value === 'openai_chat';
  $('model-base-row').style.display = custom ? '' : 'none';
  $('custom-model-row').style.display = custom ? '' : 'none';
  $('model').closest('.row').style.display = custom ? 'none' : '';
}

async function saveModelConnection(force = false) {
  if (!SETTINGS) return false;
  const protocol = $('model-protocol').value;
  const patch = {model_protocol: protocol,
    model_base_url: protocol === 'deepseek' ? 'https://api.deepseek.com' : $('model-base').value.trim(),
    custom_model_name: protocol === 'deepseek' ? '' : $('custom-model').value.trim()};
  const key = $('apikey').value.trim();
  if (key) patch.api_key = key;
  const changed = key || patch.model_protocol !== (SETTINGS.model_protocol || 'deepseek') ||
    patch.model_base_url !== (SETTINGS.model_base_url || 'https://api.deepseek.com') ||
    patch.custom_model_name !== (SETTINGS.custom_model_name || '');
  if (!changed && !force) return true;
  $('btn-save-key').disabled = true;
  try {
    const r = await api('/api/model-key', {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)});
    if (!r.ok) throw new Error(r.error || '保存失败');
    Object.assign(SETTINGS, r);
    $('model-base').value = r.model_base_url;
    $('apikey').value = '';
    $('apikey').placeholder = `已配置，末尾 ${r.api_key_tail}；填写新密钥可更换`;
    $('test-result').textContent = '接口配置已保存；尚未调用模型';
    return true;
  } catch (e) { $('test-result').textContent = '未保存：' + e.message; return false; }
  finally { $('btn-save-key').disabled = false; }
}

/* 文本框失焦时保存（比边打字边存省请求） */
['persona', 'precautions', 'reply_style', 'experience'].forEach(field => {
  document.addEventListener('DOMContentLoaded', () => {
    $(field).addEventListener('blur', () => saveSettings({ [field]: $(field).value }));
  });
});
['memory_hours', 'memory_messages'].forEach(id => {
  document.addEventListener('DOMContentLoaded', () => {
    $(id).addEventListener('change', () => saveSettings({ [id]: Number($(id).value) }));
  });
});
document.addEventListener('DOMContentLoaded', () => {
  const model = $('model');
  if (model) model.addEventListener('change', () => saveSettings({ model_name: model.value }));
  const saveKey = $('btn-save-key');
  if (saveKey) saveKey.onclick = () => saveModelConnection(true);
  const listButton = $('btn-list-models');
  const modelList = $('proxy-model-list');
  let listRequest = 0;
  const invalidateList = () => { listRequest++; modelList.replaceChildren(); };
  $('model-base').addEventListener('input', invalidateList);
  $('apikey').addEventListener('input', invalidateList);
  $('model-protocol').addEventListener('change', invalidateList);
  listButton.onclick = async () => {
    if (listButton.disabled) return;
    const request = ++listRequest;
    listButton.disabled = true;
    $('test-result').textContent = '正在获取模型列表…';
    try {
      const data = {model_base_url: $('model-base').value.trim()};
      if ($('apikey').value.trim()) data.api_key = $('apikey').value.trim();
      const result = await api('/api/model-list', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
      if (request !== listRequest) return;
      modelList.replaceChildren(...result.models.map(id => new Option(id, id)));
      $('custom-model').focus();
      $('test-result').textContent = `已获取 ${result.models.length} 个模型；在模型框中选择或输入后保存。列表不代表模型已通过工具调用验证。`;
    } catch(e) {
      if (request === listRequest) { modelList.replaceChildren(); $('test-result').textContent = e.message; }
    } finally { listButton.disabled = false; }
  };
  $('model-protocol').onchange = () => {
    if ($('model-protocol').value === 'openai_chat' && SETTINGS.model_protocol !== 'openai_chat') $('model-base').value = '';
    renderModelConnection();
  };
  const btnTest = $('btn-test');
  if (btnTest) btnTest.onclick = async () => {
    if (btnTest.disabled) return;
    btnTest.disabled = true;
    const out = $('test-result');
    out.className = 'test-result';
    out.textContent = '测试中…';
    try {
      if (!await saveModelConnection()) return;
      const r = await api('/api/model-test', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: '{}'});
      if (!r.ok) throw new Error(r.error || '模型测试失败');
      out.textContent = `模型实际调用成功 · ${r.model_name}`;
    } catch (e) { out.className = 'test-result error'; out.textContent = '失败：' + e.message; }
    finally { btnTest.disabled = false; }
  };
});

/* ============================================================ 回复文案页 */
async function loadTemplates() {
  CATALOG = await api('/api/templates');
  const channelName = { global: '全局', wanda: '万达', liangpiao: '良票' }[CURRENT_CHANNEL];
  renderStages();
  renderCards();

  document.querySelectorAll('[data-channel]').forEach(tab => {
    tab.onclick = () => {
      document.querySelectorAll('[data-channel]').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      CURRENT_CHANNEL = tab.dataset.channel;
      renderCards();
    };
  });
  document.querySelectorAll('.plat-tabs')[1] // 回复文案页的渠道切换
    ?.querySelectorAll('.plat-tab').forEach(() => {});
  window.markShopReady?.('templates');
}

function renderStages() {
  $('stage-nav').innerHTML = CATALOG.stages.map((s, i) => {
    const count = (CATALOG.catalog.find(g => g.key === s.key)?.items || []).length;
    return `<button class="stage-btn ${s.key === CURRENT_STAGE ? 'active' : ''}" data-stage="${s.key}">
      <small>0${i + 1}</small><span>${s.name}</span><b>${count} 条</b></button>`;
  }).join('');
  $('stage-nav').querySelectorAll('.stage-btn').forEach(btn => {
    btn.onclick = () => { CURRENT_STAGE = btn.dataset.stage; renderStages(); renderCards(); };
  });
}

function currentText(item) {
  if (CURRENT_CHANNEL === 'global') return item.text;
  return Object.hasOwn(item.channel_text || {}, CURRENT_CHANNEL)
    ? item.channel_text[CURRENT_CHANNEL] : item.text;
}

/* ★★ 2026-10-14 店主：「点击文案对应的卡片 右侧即显示预览消息」
   ⇒ 点卡片（不是点"修改"）就刷新右侧预览。
   ★ 预览要显示【当前渠道正在用的那句】的最终效果：
     把 {变量} 用示例值替换、把 [引用的ID] 展开 —— 和买家看到的一致。
     直接用后端的 /api/preview/<key>，不自己算（两边算会不一致）。 */
let PREVIEW_SEQ = 0;          // ★ 防竞态：快速连点卡片时，只让最后一次生效
function showPreview(key) {
  if (!key) return;
  const box = document.getElementById('preview-bubbles');
  if (!box) return;
  const seq = ++PREVIEW_SEQ;
  box.innerHTML = '<div class="bubble dim">加载中…</div>';
  api('/api/preview/' + encodeURIComponent(key) + '?channel=' + encodeURIComponent(CURRENT_CHANNEL))
    .then(r => {
      if (seq !== PREVIEW_SEQ) return;        // 有更新的请求了，丢弃这次
      const parts = Array.isArray(r.parts) ? r.parts : [r.text || ''];
      box.innerHTML = parts.length
        ? parts.map(text => `<div class="bubble">${esc(text)}</div>`).join('')
        : '<div class="bubble dim">此文案已留空，不会发送消息</div>';
      document.querySelectorAll('.rc-card').forEach(c => {
        c.classList.toggle('is-previewing', c.dataset.key === key);
      });
    })
    .catch(e => {
      if (seq !== PREVIEW_SEQ) return;
      box.innerHTML = `<div class="bubble error">预览失败：${esc(e.message)}</div>`;
    });
}

function renderCards() {
  const group = CATALOG.catalog.find(g => g.key === CURRENT_STAGE);
  if (!group) { $('rc-cards').innerHTML = ''; return; }
  $('rc-cards').innerHTML = group.items.map(item => {
    const overridden = Object.hasOwn(item.channel_text || {}, CURRENT_CHANNEL);
    // ★ 标签从"全局/渠道自定义"改成"万达/良票 自定义/沿用基准" ——
    //   界面上已经没有"全局"这个说法了（店主：只区分万达和良票）
    const chanName = CURRENT_CHANNEL === 'wanda' ? '万达' : '良票';
    const chip = !currentText(item).trim() ? '不发送' : overridden ? `${chanName}自定义` : '沿用基准';
    return `<article class="rc-card" data-key="${item.key}" tabindex="0">
      <div class="rc-card-head">
        <h4>${esc(item.name)}</h4>
        <span class="rc-chip ${overridden ? 'shared' : ''}">${chip}</span>
        <button class="btn" data-edit="${item.key}">修改</button>
      </div>
      <div class="rc-bubble-ref">发送条件：${esc(item.trigger)}</div>
      <div class="rc-text">${esc(currentText(item) || '已留空，此步骤不发送消息')}</div>
      ${item.bubble ? `<div class="rc-bubble-ref">对应气泡码 ${esc(item.bubble)}</div>` : ''}
    </article>`;
  }).join('');

  $('rc-cards').querySelectorAll('.rc-card').forEach(card => {
    // ★ 点卡片 → 右侧预览
    card.onclick = (ev) => {
      // 点"修改"按钮不触发预览（它有自己的动作）
      if (ev.target.closest('[data-edit]')) return;
      showPreview(card.dataset.key);
    };
    // ★ 键盘也能用（tabindex 让卡片可聚焦）
    card.onkeydown = (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        showPreview(card.dataset.key);
      }
    };
  });
  $('rc-cards').querySelectorAll('[data-edit]').forEach(btn => {
    btn.onclick = () => openModal(btn.dataset.edit);
  });

  // ★ 换了阶段/渠道后，右侧预览跟着刷新成【第一张卡】的，
  //   而不是留着上一条（那样预览和卡片对不上）。
  const first = group.items[0];
  if (first) showPreview(first.key);
}

/* ============================================================ 编辑弹窗 */
function openModal(key) {
  const item = CATALOG.catalog.flatMap(g => g.items).find(i => i.key === key);
  if (!item) return;
  EDITING = item;
  $('m-title').textContent = item.name;
  $('m-trigger').textContent = '触发时机：' + item.trigger;
  $('m-notes').innerHTML = item.notes.map(n => `<p>※ ${esc(n)}</p>`).join('');
  const chanName = CURRENT_CHANNEL === 'wanda' ? '万达' : '良票';
  const hasOverride = Object.hasOwn(item.channel_text || {}, CURRENT_CHANNEL);
  // 没有渠道覆盖时显示当前沿用的基准，保存仅写当前渠道。
  $('m-text').value = hasOverride
    ? (item.channel_text?.[CURRENT_CHANNEL] || '')
    : item.text;
  const variants = item.variants || {};
  $('m-variants').hidden = !Object.keys(variants).length;
  $('m-variant-fields').innerHTML = Object.entries(variants).map(([name, value]) =>
    `<div class="m-variant-row"><label>${esc(name === 'marked' ? '已标记座位' : name === 'reselect' ? '标记座位不可售' : name)}</label><textarea rows="3" data-variant="${esc(name)}">${esc(value)}</textarea></div>`
  ).join('');
  $('m-label').textContent = `文案（${chanName}）`;
  $('m-tokens').innerHTML = item.variables
    .map(v => `<button data-token="${v.key}" title="${esc(v.description)}；示例：${esc(v.example)}">${esc(v.name)} <code>{${v.key}}</code></button>`)
    .join('') || '<span style="color:#bfbfbf;font-size:12px">这条模板没有变量</span>';
  $('m-tokens').querySelectorAll('[data-token]').forEach(btn => {
    btn.onclick = () => {
      const ta = $('m-text'), token = `{${btn.dataset.token}}`;
      const s = ta.selectionStart, e = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + token + ta.value.slice(e);
      ta.focus(); ta.setSelectionRange(s + token.length, s + token.length);
    };
  });
  $('m-status').textContent = '';
  $('modal').hidden = false;
}

function closeModal() { $('modal').hidden = true; }
$('m-close').onclick = closeModal;
// ★ 2026-10-14：底部按钮条的「取消」已去掉 ——
//   它和右上角「关闭」是同一件事，两个文字按钮并排
//   容易让人不知道该点哪个。
//   ★ 这里原来有一行 `if ($('m-close2')) ...` —— 元素删了之后
//     它就是死代码，一并去掉（不留着等它某天又出现）。
// ★ Esc 关弹窗（原来只能点按钮）
document.addEventListener('keydown', ev => {
  if (ev.key === 'Escape' && !$('modal').hidden) closeModal();
});
// ★ 原来这里绑的是 'm-preview'（预览按钮）—— 店主让删掉那个按钮了。
//   预览改由"点卡片"驱动（见 showPreview）。

$('m-inherit').onclick = async () => {
  if (!EDITING) return;
  try {
    const key = EDITING.key;
    await api('/api/templates', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, channel: CURRENT_CHANNEL, text: '', inherit: true }),
    });
    await loadTemplates();
    showPreview(key);
    closeModal();
  } catch (e) { $('m-status').textContent = '恢复失败：' + e.message; }
};

$('m-save').onclick = async () => {
  if (!EDITING) return;
  const status = $('m-status');
  const text = $('m-text').value;
  const chan = CURRENT_CHANNEL;
  try {
    const key = EDITING.key;
    await api('/api/templates', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: EDITING.key, channel: chan, text }),
    });
    for (const field of document.querySelectorAll('[data-variant]')) {
      await api('/api/templates', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: EDITING.key, channel: chan,
          variant: field.dataset.variant, text: field.value }),
      });
    }
    status.className = 'm-status';
    status.textContent = '已保存';
    await loadTemplates();
    showPreview(key);
    $('modal').hidden = true;
  } catch (e) {
    status.className = 'm-status error';
    status.textContent = '保存失败：' + e.message;
  }
};

/* ============================================================ 启动 */
(async () => {
  // Independent panels can load together; the hidden panel must not delay
  // rendering the current one behind a serial API request.
  await Promise.allSettled([
    loadSettings().catch(e => console.error('配置读取失败', e)),
    loadTemplates().catch(e => console.error('模板读取失败', e)),
  ]);
})();

window.saveShopPage = async kind => {
  if (!window.shopPageReady(kind)) return false;
  if (kind === 'assistant') return await $('btn-save-assistant').onclick();
  if (!$('modal').hidden) return false;
  await loadTemplates();
  return true;
};
