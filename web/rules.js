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
   rules.js —— 报价规则页

   ★ 规则表格 + 新增/编辑弹窗。
     弹窗里的字段与票达人「新增规则」页逐项对齐，
     并额外提供【实时预览】把数字翻成人话。

   ★ 最重要的一条：与后端同一套校验口径。
     后端 PUT /api/rules 有矛盾就不落盘，
     前端在弹窗里就先把矛盾指出来（限价 < 报价），不让用户白填。
   ============================================================ */

let DATA = null;         // /api/rules 的返回
let RULES = [];          // 当前编辑中的规则数组
let SCOPE = null;
let SHOP_OPTIONS = [];
let DIRTY = false;
let BUSY = false;
let EDIT_INDEX = null;   // 正在编辑第几条；null = 新增

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

/* 与后端同口径的默认值 */
const DEFAULT_RULE = {
  name: '', weight: 0, enabled: true,
  brands: [], excluded_cinemas: [],
  time_start: '00:00:00', time_end: '23:59:59',
  price_left_yuan: 0, price_right_yuan: 999,
  quote_mode: '渠道预估价', method: '加法',
  markup_yuan: 0, limit_yuan: 0, capped: true,
  adjust: '不调整', created_seq: 0,
  // 分档报价（票达人弹窗里的「动态报价加法」那张表）
  // ★ 空数组 = 不用分档，退回"成本 + 报价金额"的老算法
  tiers: [], basis: 'estimated',
};

const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const yuan = v => `${Number(v).toFixed(2)} 元`;
const routeLabel = channel => ({
  WANDA: '万达路线', LIANGPIAO: '良票路线（排除万达路线）',
}[channel] || (channel ? `未知路线：${channel}` : '通用规则（两条路线均可匹配）'));

function syncRouteHelp() {
  const channel = $('#f-channel').value;
  $('#route-help').textContent = channel === 'WANDA'
    ? '仅匹配已确定走万达的报价，不匹配良票路线；加减金额由下方规则配置。'
    : channel === 'LIANGPIAO'
      ? '仅匹配已确定走良票的报价，自动排除万达路线，无需逐个排除万达影院。'
      : '两条路线均可匹配此规则，并与其他规则按权重竞争；如需隔离，请明确选择万达或良票路线。';
}

/* ============================================================ 加载 */
async function load() {
  if (!SCOPE) return;
  const res = await fetch(window.withBase('/api/rules') + '?' + new URLSearchParams(SCOPE));
  const data = await res.json();
  if (!res.ok || !Array.isArray(data.rules)) throw new Error(data.error || '读取规则失败');
  DATA = data;
  DIRTY = false;
  $('.push-tip').textContent = DATA.scope === 'shop'
    ? '当前为本店独立规则，保存仅对本店生效。'
    : '本店尚未单独配置，沿用公共规则；保存后成为本店独立规则。';
  RULES = DATA.rules.map(r => ({ ...DEFAULT_RULE, ...r }));
  // created_seq 用于"同权重取最新"，新增的递增
  RULES.forEach((r, i) => { if (!r.created_seq) r.created_seq = i + 1; });
  renderTable();
  showProblems(DATA.problems);
  syncPushOptions();
}

function showProblems(problems) {
  const box = $('#problems');
  if (!problems || !problems.length) {
    box.hidden = true; box.innerHTML = ''; return;
  }
  box.hidden = false;
  box.innerHTML = '<b>⚠ 规则有问题（保存会被拦下）</b><ul>' +
    problems.map(p => `<li>${esc(p)}</li>`).join('') + '</ul>';
}

/* ============================================================ 表格 */
function renderTable() {
  const body = $('#rules-body');
  const sorted = RULES.map((r, i) => ({ r, i }))
    .sort((a, b) => (b.r.weight - a.r.weight) || (b.r.created_seq - a.r.created_seq));

  $('#rules-hint').textContent =
    `共 ${RULES.length} 条，启用 ${RULES.filter(r => r.enabled).length} 条 · ` +
    `按权重降序匹配，同权重取最新添加的`;

  body.innerHTML = sorted.map(({ r, i }) => {
    const bad = r.capped && Number(r.limit_yuan) < Number(r.markup_yuan);
    const mode = DATA.options.price_mode_of_quote[r.quote_mode] || '?';
    const brands = r.brands || [];
    const halls = r.hall_keywords || [];
    const excludedHalls = r.excluded_hall_keywords || [];
    const excludedCinemas = r.excluded_cinemas || [];
    const brandText = brands.length ? brands.slice(0, 2).join('、') : '全部院线';
    const brandSummary = brands.length > 2
      ? `<details class="rule-scope-more"><summary>${esc(brandText)} <span>+${brands.length - 2}</span></summary><div>${esc(brands.join('、'))}</div></details>`
      : `<div class="rule-muted">${esc(brandText)}</div>`;
    const hallText = halls.length ? halls.join(' / ') + ' 影厅' : '全部影厅';
    const excludedText = excludedHalls.length ? '排除 ' + excludedHalls.join('、') : '';
    const pricing = r.original_discount_bps != null
      ? `官方原价 ${(Number(r.original_discount_bps) / 1000).toFixed(3).replace(/\.?0+$/, '')} 折`
      : r.tiers?.length ? `分档加减 · ${r.tiers.length} 档`
      : `预估成本 ${Number(r.markup_yuan) < 0 ? '−' : '+'} ${Math.abs(Number(r.markup_yuan) || 0)} 元 / 张`;
    const pricingDetails = r.original_discount_bps != null
      ? `最低 ${(r.minimum_price_fen || 0) / 100} 元 / 张${r.fixed_cost_fen == null ? '' : ' · 预估成本 ' + r.fixed_cost_fen / 100 + ' 元 / 张'}`
      : r.adjust !== '不调整' ? r.adjust : (r.tiers?.length ? '按折扣区间匹配对应加减值' : '按每张成本计算');
    const allDay = !r.time_start && !r.time_end ||
      ['00:00:00-23:59:59', '00:00-23:59'].includes(`${r.time_start}-${r.time_end}`);
    return `<tr class="${r.enabled ? '' : 'rule-off'} ${bad ? 'rule-bad' : ''}">
      <td class="rule-identity">
        <strong class="rule-name">${esc(r.name) || '未命名规则'}</strong>
        <div class="rule-meta"><span class="rule-route ${r.channel === 'WANDA' ? 'route-wanda' : r.channel === 'LIANGPIAO' ? 'route-liangpiao' : ''}">${esc(r.channel === 'LIANGPIAO' ? '良票路线' : r.channel ? routeLabel(r.channel) : '通用路线')}</span><span class="rule-weight">优先级 ${esc(r.weight)}</span></div>
      </td>
      <td data-label="适用范围">
        <div class="rule-main">${esc(hallText)}${excludedText ? `<span class="rule-muted"> · ${esc(excludedText)}</span>` : ''}</div>
        ${brandSummary}
        ${excludedCinemas.length ? `<div class="rule-muted rule-exclusions" title="${esc(excludedCinemas.join('、'))}">排除影院：${esc(excludedCinemas.slice(0, 2).join('、'))}${excludedCinemas.length > 2 ? ` 等 ${excludedCinemas.length} 家` : ''}</div>` : ''}
      </td>
      <td data-label="报价计算">
        <div class="rule-price">${esc(pricing)}</div>
        <div class="rule-muted">${esc(pricingDetails)}</div>
        <span class="rule-mode">${mode === 'FIXED' ? '一口价' : '预估限价'}${r.original_discount_bps != null || !r.tiers?.length ? '' : ' · ' + esc(r.method)}</span>
      </td>
      <td data-label="状态与限制">
        <span class="rule-status ${r.enabled ? 'is-enabled' : ''}"><i aria-hidden="true"></i>${r.enabled ? '已启用' : '已停用'}</span>
        <div class="rule-muted ${bad ? 'rt-bad' : ''}">${r.capped ? `单张限价：成本 + ${esc(r.limit_yuan)} 元` : '不限价'}${bad ? ' · 低于报价' : ''}</div>
        ${allDay ? '' : `<div class="rule-muted">${esc(r.time_start)} – ${esc(r.time_end)}</div>`}
      </td>
      <td class="rule-actions"><button type="button" class="rt-link" data-edit="${i}" aria-label="编辑${esc(r.name)}">编辑</button><button type="button" class="rt-link danger" data-del="${i}" aria-label="删除${esc(r.name)}">删除</button></td>
    </tr>`;
  }).join('');

  if (!sorted.length) body.innerHTML = '<tr><td colspan="5" class="rules-empty">还没有报价规则，点击“新增规则”开始配置。</td></tr>';

  $$('[data-edit]', body).forEach(a => a.onclick = () => openModal(Number(a.dataset.edit)));
  $$('[data-del]', body).forEach(a => a.onclick = () => {
    if (BUSY) return;
    if (confirm(`删除规则「${RULES[Number(a.dataset.del)].name}」？`)) {
      RULES.splice(Number(a.dataset.del), 1);
      DIRTY = true;
      $("#save-status").textContent = "删除尚未保存，请点保存规则";
      renderTable();
    }
  });
}

/* ============================================================ 弹窗 */
function fillSelect(sel, options, current, labels = null) {
  // ★ labels 可选，形如 { value: 显示名 }。
  //   为什么需要：分档的计价基数 value 是英文（estimated/original/…），
  //   直接显示英文运营看不懂；但存回后端必须是英文（后端按它分支）。
  sel.innerHTML = options.map(o => {
    const text = labels && labels[o] ? labels[o] : o;
    return `<option value="${esc(o)}"${o === current ? ' selected' : ''}>${esc(text)}</option>`;
  }).join('');
}

function fillRadio(box, options, current, disabled = [], values = null) {
  box.innerHTML = options.map((o, i) => {
    const val = values ? values[i] : o;
    const off = disabled.includes(o);
    return `<label class="${off ? 'disabled' : ''}">
      <input type="radio" name="${box.id}" value="${esc(String(val))}"
             ${val === current ? 'checked' : ''} ${off ? 'disabled' : ''}>${esc(o)}</label>`;
  }).join('');
}

function readRadio(box) {
  const el = $(`input[name="${box.id}"]:checked`, box);
  if (!el) return null;
  const raw = el.value;
  if (raw === 'true') return true;
  if (raw === 'false') return false;
  return raw;
}

function openModal(index) {
  if (BUSY || !DATA) return;
  EDIT_INDEX = index;
  const rule = index === null
    ? { ...DEFAULT_RULE, created_seq: Math.max(0, ...RULES.map(r => r.created_seq || 0)) + 1 }
    : RULES[index];

  $('#m-title').textContent = index === null ? '新增规则' : `编辑规则 · ${rule.name}`;

  $('#f-name').value = rule.name || '';
  $('#f-weight').value = rule.weight ?? 0;
  $('#f-time-start').value = normTime(rule.time_start);
  $('#f-time-end').value = normTime(rule.time_end);
  $('#f-price-left').value = rule.price_left_yuan ?? 0;
  $('#f-price-right').value = rule.price_right_yuan ?? 999;
  $('#f-markup').value = rule.markup_yuan ?? 0;
  $('#f-limit').value = rule.limit_yuan ?? 0;

  fillSelect($('#f-method'), DATA.options.methods, rule.method);
  // ★ 回填生效渠道（老规则没这字段 → 空串 = 不限渠道）
  if ($('#f-channel')) $('#f-channel').value = rule.channel || '';
  $('#f-channel').onchange = syncRouteHelp;
  syncRouteHelp();
  $('#f-halls').value = (rule.hall_keywords || []).join('，');
  $('#f-excluded-halls').value = (rule.excluded_hall_keywords || []).join('，');
  $('#f-calculation').value = rule.original_discount_bps == null ? 'standard' : 'original_discount';
  $('#f-original-discount').value = rule.original_discount_bps == null ? '' : rule.original_discount_bps / 1000;
  $('#f-fixed-cost').value = rule.fixed_cost_fen == null ? '' : rule.fixed_cost_fen / 100;
  $('#f-minimum-price').value = (rule.minimum_price_fen || 0) / 100;
  $('#f-calculation').onchange = () => { syncMethodRows(); updatePreview(); };
  // ★ 报价方式一变就切换显隐（加法 ↔ 动态折扣）
  $('#f-method').onchange = () => { syncMethodRows(); updatePreview(); };
  fillSelect($('#f-adjust'), DATA.options.adjusts, rule.adjust);
  fillRadio($('#f-quote-mode'), DATA.options.quote_modes, rule.quote_mode,
            DATA.options.quote_mode_disabled);
  fillRadio($('#f-capped'), ['启用限价', '不启用限价'], rule.capped, [], [true, false]);

  // 分档报价表
  if ($('#f-basis')) {
    fillSelect($('#f-basis'), DATA.options.bases, rule.basis || 'estimated',
               DATA.options.basis_labels);
    $('#f-basis').onchange = () => { renderTiers(); updatePreview(); };
  }
  TIERS = (rule.tiers || []).map(r => ({ ...r }));
  renderTiers();
  syncMethodRows();
  if ($('#f-tier-add')) $('#f-tier-add').onclick = addTier;

  $('#f-enabled').classList.toggle('on', !!rule.enabled);
  $('#f-enabled').setAttribute('aria-checked', String(!!rule.enabled));

  renderBrands(rule.brands || []);
  renderExcluded(rule.excluded_cinemas || []);
  wireOnce();
  updatePreview();
  $('#m-status').textContent = '';
  $('#modal').hidden = false;
}

function normTime(t) {
  const parts = String(t || '00:00:00').split(':');
  while (parts.length < 3) parts.push('00');
  return parts.map((p, i) => i === 0 ? p.padStart(2, '0')
    : String(p).padStart(2, '0').slice(0, 2)).join(':');
}

/* 院线：全集里勾选 */
function renderBrands(selected) {
  const box = $('#f-brands');
  const all = [...new Set([...selected, ...(DATA.brands || [])])];
  $('#brands-help').textContent =
    `共 ${all.length} 条院线可选 · 已选择报价路线时可留空；勾选后会进一步限制该路线的院线范围`;
  box.innerHTML = all.map(b => {
    const on = selected.includes(b);
    return `<span class="chip ${on ? '' : 'off'}" data-brand="${esc(b)}">
      <button type="button" data-toggle="${esc(b)}">${on ? '✓' : '+'}</button>${esc(b)}</span>`;
  }).join('');
  // ★★★ 2026-10-14 店主：「点击影院名字也可以添加/删除　没必要必须点击加号」
  //   原来只有那个 <button>（+ / ✓）挂了 click —— 点院线名字没反应，
  //   而名字占的面积大得多，所以用起来像"点不动"。
  //   ★ 修法：把 click 挂到【整个 chip】上，名字和按钮都能点。
  //     按钮保留（它是"能点"的视觉提示），但不再是唯一热区。
  $$('#f-brands .chip', box).forEach(chip => {
    chip.onclick = () => toggleBrand(chip);
  });

  const updateCount = () => { $('#brands-count').textContent = `已选 ${currentBrands().length} / ${all.length} 条院线`; };
  const selectAll = selected => {
    $$('#f-brands .chip').forEach(chip => {
      chip.classList.toggle('off', !selected);
      chip.querySelector('[data-toggle]').textContent = selected ? '✓' : '+';
    });
    updateCount(); updatePreview();
  };
  $('#brands-all').onclick = () => selectAll(true);
  $('#brands-clear').onclick = () => selectAll(false);
  updateCount();
  function toggleBrand(chip) {
    const on = !chip.classList.contains('off');
    chip.classList.toggle('off', on);
    const btn = chip.querySelector('[data-toggle]');
    if (btn) btn.textContent = on ? '+' : '✓';
    updateCount(); updatePreview();
  }
}

function currentBrands() {
  return $$('#f-brands .chip').filter(c => !c.classList.contains('off'))
    .map(c => c.dataset.brand);
}

/* 排除影院：自由输入 */
function renderExcluded(list) {
  const box = $('#f-excluded');
  box.innerHTML = list.map(name =>
    `<span class="chip" data-cinema="${esc(name)}">
      <button type="button" data-rm="${esc(name)}">×</button>${esc(name)}</span>`).join('');
  // ★★ 2026-10-14：和上面的院线一样 —— 点整块都能删，
  //   不再只有那个小小的 × 能点（店主："点击影院名字也可以添加/删除"）。
  $$('#f-excluded .chip', box).forEach(chip => {
    chip.onclick = () => { chip.remove(); updatePreview(); };
  });
}

function currentExcluded() {
  return $$('#f-excluded .chip').map(c => c.dataset.cinema);
}

/* 事件只绑一次 */
let wired = false;
function wireOnce() {
  if (wired) return;
  wired = true;

  $('#m-close').onclick = $('#m-cancel').onclick = () => { if (!BUSY) $('#modal').hidden = true; };
  $('#f-enabled').onclick = () => {
    const on = !$('#f-enabled').classList.contains('on');
    $('#f-enabled').classList.toggle('on', on);
    $('#f-enabled').setAttribute('aria-checked', String(on));
    updatePreview();
  };

  // 排除影院：回车或点"添加"
  const addExclude = () => {
    const input = $('#f-exclude-input');
    const name = input.value.trim();
    if (!name) return;
    if (currentExcluded().includes(name)) { input.value = ''; return; }
    const box = $('#f-excluded');
    box.insertAdjacentHTML('beforeend',
      `<span class="chip" data-cinema="${esc(name)}">
        <button type="button" data-rm="${esc(name)}">×</button>${esc(name)}</span>`);
    $$('[data-rm]', box).forEach(btn => btn.onclick = () => {
      btn.closest('.chip').remove(); updatePreview();
    });
    input.value = '';
    updatePreview();
  };
  $('#f-exclude-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); addExclude(); }
  });
  const addLink = document.createElement('a');
  addLink.className = 'rt-link';
  addLink.textContent = '添加';
  addLink.style.marginLeft = '6px';
  addLink.onclick = addExclude;
  $('#f-exclude-input').after(addLink);

  // 任何输入都重算预览
  ['f-name', 'f-weight', 'f-time-start', 'f-time-end', 'f-price-left', 'f-price-right',
   'f-markup', 'f-limit', 'f-method', 'f-adjust', 'f-halls', 'f-excluded-halls',
   'f-original-discount', 'f-fixed-cost', 'f-minimum-price'].forEach(id => {
    $('#' + id).addEventListener('input', updatePreview);
    $('#' + id).addEventListener('change', updatePreview);
  });
  $$('#f-quote-mode input, #f-capped input').forEach(i =>
    i.addEventListener('change', updatePreview));

  $('#m-save').onclick = saveRule;
}

/* ============================================================ 预览
   ★ 把数字翻成人话。规则配错时下单会被 300005 拒，
     与其等下单失败，不如在这里先看出来。 */
function collectForm() {
  const discount = $('#f-calculation').value === 'original_discount';
  const keywords = value => value.split(/[,，、\n]/).map(s => s.trim()).filter(Boolean);
  return {
    name: $('#f-name').value.trim(),
    weight: Number($('#f-weight').value || 0),
    enabled: $('#f-enabled').classList.contains('on'),
    brands: currentBrands(),
    excluded_cinemas: currentExcluded(),
      // ★ 生效渠道：留空 = 按院线匹配（老行为）
      channel: $('#f-channel') ? $('#f-channel').value : '',
    hall_keywords: keywords($('#f-halls').value),
    excluded_hall_keywords: keywords($('#f-excluded-halls').value),
    fixed_cost_fen: $('#f-fixed-cost').value.trim() === '' ? null : Math.round(Number($('#f-fixed-cost').value) * 100),
    original_discount_bps: discount ? Math.round(Number($('#f-original-discount').value) * 1000) : null,
    minimum_price_fen: discount ? Math.round(Number($('#f-minimum-price').value || 0) * 100) : 0,
    time_start: $('#f-time-start').value || '00:00:00',
    time_end: $('#f-time-end').value || '23:59:59',
    price_left_yuan: Number($('#f-price-left').value || 0),
    price_right_yuan: Number($('#f-price-right').value || 0),
    quote_mode: discount ? '渠道一口价' : readRadio($('#f-quote-mode')) || '渠道预估价',
    method: $('#f-method').value,
    markup_yuan: discount ? 0 : Number($('#f-markup').value || 0),
    limit_yuan: discount ? 0 : Number($('#f-limit').value || 0),
    capped: discount ? false : readRadio($('#f-capped')) === true,
    adjust: discount ? '不调整' : $('#f-adjust').value,
    tiers: discount ? [] : readTiers(),
    basis: $('#f-basis') ? $('#f-basis').value : 'estimated',
    created_seq: EDIT_INDEX === null
      ? Math.max(0, ...RULES.map(r => r.created_seq || 0)) + 1
      : RULES[EDIT_INDEX].created_seq,
  };
}


/* ============================================================ 分档报价表 */
/* ★★ 这张表就是票达人弹窗里的「动态报价加法」。
     折扣率 = 两个金额的比值（见后端 discount_bps_of）：
       basis=estimated → 折扣率 = 预估成本 / 官方原价
       basis=original  → 折扣率 = 原价 / 原价 = 恒 100%
     比例带符号：+ 加价，− 减价。V5 里就是 adjustment_bps 的正负。 */

const BASIS_HELP = {
  original: '折扣率 = 原价/原价 = 恒 100%，比例乘在【原价】上',
  estimated: '折扣率 = 预估成本/官方原价，比例乘在【成本】上',
  fixed_per_ticket: '每张固定加价（分），不用比例',
  cost_plus_fixed: '整单固定加价（分），不用比例',
};

let TIERS = [];

// ★★ 报价方式决定显示哪一块 —— 对齐 V5 弹窗的互斥行为：
//     加法      → 显示「报价金额（预估成本 + N 元）」，隐藏分档表
//     动态折扣  → 隐藏「报价金额」，显示「动态报价加法（分档表）」
//
//   ★ 为什么必须互斥（不是"可读性问题"）：
//     两条路都会算出"报给买家的价"。同时显示时运营不知道哪个生效，
//     而后端是【分档优先】—— 填了报价金额却不生效，最容易被误以为生效。
//     我原来两个都显示，就是这个毛病。
function methodNeedsTiers() {
  if ($('#f-calculation').value === 'original_discount') return false;
  const m = $('#f-method') ? $('#f-method').value : '';
  // ★★★ 2026-10-14 修：判据从「报价方式 = 动态折扣」改成
  //   「按每张固定加价 OR 报价方式要分档 OR 表里已经有行」。
  //
  //   为什么必须改：后端是 `if rule.tiers and original_total:` ——
  //   只要【有分档表】就用分档算，跟 method 是什么无关。
  //   而 V5 那两条（万达报价/良票报价）method=加法、却带 6 档分档表。
  //   ⇒ 旧判据下界面认为"不用分档"，于是：
  //        ① 分档表被藏起来（用户看不到那 6 档）
  //        ② 报价金额/单张限价行反而显示出来
  //        ③ 预览说"报给买家 预估成本 + 0 元"—— 而实际是 +1 元/张
  //      等于界面在骗人（店主截图里那句"预估成本 + 0 元"就是它）。
  if (m === '动态折扣' || m === '动态折扣加法') return true;
  // basis=fixed_per_ticket / cost_plus_fixed 本来就必须配分档表
  const b = $('#f-basis') ? $('#f-basis').value : '';
  if (b === 'fixed_per_ticket' || b === 'cost_plus_fixed') return true;
  // 表里已经有行 → 这条规则就是带分档的，不能把表藏了
  return readTiers().length > 0;
}

function syncMethodRows() {
  const discount = $('#f-calculation').value === 'original_discount';
  $('#f-discount-row').hidden = !discount;
  $('#f-floor-row').hidden = !discount;
  ['f-method', 'f-quote-mode', 'f-capped', 'f-adjust', 'f-limit'].forEach(id => {
    $('#' + id).closest('.rf-item').hidden = discount;
  });
  const needsTiers = methodNeedsTiers();
  if ($('#f-markup-row')) $('#f-markup-row').hidden = needsTiers || discount;
  if ($('#f-tier-block')) $('#f-tier-block').hidden = !needsTiers;
}

function isFixedBasis() {
  const b = $('#f-basis') ? $('#f-basis').value : 'estimated';
  return b === 'fixed_per_ticket' || b === 'cost_plus_fixed';
}

function renderTiers() {
  const box = $('#f-tiers');
  if (!box) return;
  const fixed = isFixedBasis();
  if ($('#f-tier-col3')) $('#f-tier-col3').textContent = fixed ? '每张加减（分）' : '加减比例';
  box.innerHTML = TIERS.map((row, i) => {
    const lo = (row.lower_bps ?? 0) / 100;
    const hi = (row.upper_bps ?? 10000) / 100;
    const adj = row.adjustment_bps ?? 0;
    const add = row.fixed_add_fen ?? 0;
    const amount = fixed ? add : adj;
    const signKey = fixed ? '_fixedSign' : '_ratioSign';
    const sign = Math.sign(amount) || row[signKey] || 1;
    return `<tr>
      <td><input type="number" class="tier-num" data-i="${i}" data-k="lower_bps"
                 step="0.01" value="${lo}" aria-label="第${i + 1}档折扣率下限"></td>
      <td><input type="number" class="tier-num" data-i="${i}" data-k="upper_bps"
                 step="0.01" value="${hi}" aria-label="第${i + 1}档折扣率上限"></td>
      <td>${fixed
        ? `<input type="number" class="tier-num" data-i="${i}" data-k="fixed_add_fen"
                   step="1" min="0" value="${Math.abs(add)}" aria-label="第${i + 1}档金额（分）"><span class="tier-unit">分</span>`
        : `<input type="number" class="tier-num" data-i="${i}" data-k="adjustment_bps"
                   step="0.1" min="0" value="${Math.abs(adj) / 100}" aria-label="第${i + 1}档比例（%）"><span class="tier-unit">%</span>`}</td>
      <td><div class="tier-sign">
             <button type="button" class="tier-sign-btn ${sign > 0 ? 'on' : ''}"
                     data-i="${i}" data-sign="1" title="加价" aria-label="第${i + 1}档加价" aria-pressed="${sign > 0}">+</button>
             <button type="button" class="tier-sign-btn ${sign < 0 ? 'on' : ''}"
                     data-i="${i}" data-sign="-1" title="减价" aria-label="第${i + 1}档减价" aria-pressed="${sign < 0}">−</button>
           </div></td>
      <td><button type="button" class="tier-del" data-del="${i}"
                  title="删掉这一档">✕</button></td>
    </tr>`;
  }).join('');

  // 输入即写回 TIERS（不等到保存）
  // ★ 必须用 $$（querySelectorAll）—— `$` 是 querySelector，返回单个或 null，
  //   对它调 .forEach 直接 TypeError。我第一版三个都写成了 `$`，
  //   结果 openModal 在 renderTiers 抛异常 → 弹窗根本打不开（第 210 行没执行到）。
  $$('.tier-num', box).forEach(inp => {
    inp.oninput = () => {
      const i = Number(inp.dataset.i);
      const k = inp.dataset.k;
      let v = Number(inp.value || 0);
      if (k !== 'fixed_add_fen') v = Math.round(v * 100);   // % → bps
      if (k === 'adjustment_bps' || k === 'fixed_add_fen') {
        const signKey = k === 'fixed_add_fen' ? '_fixedSign' : '_ratioSign';
        const sign = v < 0 ? -1 : Math.sign(TIERS[i][k]) || TIERS[i][signKey] || 1;
        TIERS[i][signKey] = sign;
        if (Number(inp.value) < 0) inp.value = String(Math.abs(Number(inp.value)));
        $$('.tier-sign-btn', inp.closest('tr')).forEach(button => {
          const selected = Number(button.dataset.sign) === sign;
          button.classList.toggle('on', selected);
          button.setAttribute('aria-pressed', String(selected));
        });
        v = Math.round(Math.abs(v)) * sign;
      }
      TIERS[i][k] = v;
      updatePreview();
    };
  });
  $$('.tier-sign-btn', box).forEach(btn => {
    btn.onclick = () => {
      const i = Number(btn.dataset.i);
      const sign = Number(btn.dataset.sign);
      const key = isFixedBasis() ? 'fixed_add_fen' : 'adjustment_bps';
      TIERS[i][isFixedBasis() ? '_fixedSign' : '_ratioSign'] = sign;
      TIERS[i][key] = Math.abs(TIERS[i][key] || 0) * sign;
      renderTiers();
      updatePreview();
    };
  });
  $$('.tier-del', box).forEach(btn => {
    btn.onclick = () => {
      TIERS.splice(Number(btn.dataset.del), 1);
      renderTiers();
      updatePreview();
    };
  });

  if ($('#f-tier-help')) {
    // ★ 说明里必须写出折扣率怎么算 —— 票达人那张表只有四列，
    //   没有"折扣率按"下拉，算法是固定的，所以要把口径写出来给人看。
    const basis = $('#f-basis') ? $('#f-basis').value : 'estimated';
    const how = basis === 'original' ? '折扣率 = 原价 / 官方原价（恒 100%）'
      : basis === 'fixed_per_ticket' ? '每张固定加价（分）'
      : basis === 'cost_plus_fixed' ? '整单固定加价（分）'
      : '折扣率 = 预估成本 / 官方原价';
    $('#f-tier-help').textContent = TIERS.length
      ? `共 ${TIERS.length} 档 · ${how} · ${fixed ? '100分＝1元 · ' : ''}＋加价 / −减价；最终售价不低于成本`
      : '没有分档 → 按「报价金额」算（成本 + N 元）';
  }
}

function readTiers() {
  // 读回时把 % 形式转成 bps（渲染时已转，这里只做一次兜底修正）
  return TIERS.map(r => ({
    lower_bps: Number(r.lower_bps || 0),
    upper_bps: Number(r.upper_bps || 0),
    adjustment_bps: Number(r.adjustment_bps || 0),
    fixed_add_fen: Number(r.fixed_add_fen || 0),
    upper_inclusive: !!r.upper_inclusive,
  }));
}

function addTier() {
  const last = TIERS[TIERS.length - 1];
  // At a complete 0–100% table, split the final interval and retain its
  // existing adjustment. Adding a draft row must not silently change prices.
  if (last && Number(last.upper_bps) >= 10000 && Number(last.upper_bps) - Number(last.lower_bps) >= 2) {
    const midpoint = Math.floor((Number(last.lower_bps) + Number(last.upper_bps)) / 2);
    TIERS.push({ ...last, lower_bps: midpoint });
    last.upper_bps = midpoint;
    last.upper_inclusive = false;
    renderTiers(); updatePreview();
    return;
  }
  const lo = last ? Number(last.upper_bps || 0) : 0;
  if (lo >= 100000) { $('#m-status').textContent = '折扣率已到1000%上限，请先调整现有区间。'; return; }
  const hi = Math.min(100000, lo + 500);
  if (last) last.upper_inclusive = false;
  const fixed = isFixedBasis();
  TIERS.push(fixed
    ? { lower_bps: lo, upper_bps: hi, adjustment_bps: 0,
        fixed_add_fen: 100, upper_inclusive: hi >= 10000 }
    : { lower_bps: lo, upper_bps: hi, adjustment_bps: 100,
        fixed_add_fen: 0, upper_inclusive: hi >= 10000 });
  renderTiers();
  updatePreview();
}

function updatePreview() {
  const r = collectForm();
  const mode = DATA.options.price_mode_of_quote[r.quote_mode] || '?';
  const parts = [];
  const problems = [];

  // 校验（与后端 validate_rule 同口径）
  if (!r.name) problems.push('规则名称不能为空');
  // ★★ 2026-10-14：有 channel 的规则【按渠道】匹配，不靠院线。
  //   不排除的话，V5 那两条（万达报价/良票报价）会被这条判为
  //   "匹配不上任何影院" —— 保存被拦下，而它们其实是好的。
  //   根因：校验只认 brands 一个维度，不认识 channel。
  if (!r.brands.length && !r.channel) {
    problems.push('未选择包含院线 —— 这条规则匹配不上任何影院'
      + '（若是按渠道生效的规则，请设置渠道）');
  }
  if (r.price_left_yuan >= r.price_right_yuan)
    problems.push(`猫淘价区间 [${r.price_left_yuan}, ${r.price_right_yuan}) 为空`);

  // ★★ 预览必须说【真正生效的那条路】。
  //   踩过：选「动态折扣」时分档表已经决定价格了，
  //   预览却还在说"报给买家 预估成本 + 2 元"（读的是报价金额）——
  //   而后端是分档优先，报价金额根本不参与。等于预览在骗人。
  const usesTiers = methodNeedsTiers();
  if (r.original_discount_bps != null) {
    if (!(r.original_discount_bps > 0 && r.original_discount_bps <= 10000)) problems.push('原价折扣必须大于0且不超过10折');
    parts.push(`按官方原价 × ${r.original_discount_bps / 1000}折计算，一口价，不叠加分档；最低${r.minimum_price_fen / 100}元/张且不低于成本`);
  } else if (usesTiers) {
    if (r.tiers && r.tiers.length) {
      // ★★ 2026-10-14：按 basis 说【真话】。
      //   原来不管什么 basis 都写"落档后按比例加减" ——
      //   而 basis=fixed_per_ticket（万达那条）是【每张固定加钱】，
      //   跟比例无关。预览说错口径，运营就会按错的算法估算毛利。
      const b = r.basis || 'estimated';
      const how = b === 'fixed_per_ticket'
        ? '落档后按该档的<b>每张固定加价</b>（不看比例）'
        : b === 'cost_plus_fixed'
          ? '落档后按该档的<b>整单固定加价</b>（不看比例）'
          : b === 'original'
            ? '落档后<b>按比例加减（基数 = 官方原价）</b>'
            : '落档后<b>按比例加减（基数 = 预估成本）</b>';
      const rate = b === 'fixed_per_ticket' || b === 'cost_plus_fixed'
        ? '折扣率 = 预估成本 ÷ 官方原价，只用来选档'
        : '折扣率 = 预估成本 ÷ 官方原价';
      parts.push(`命中后：报给买家由 <span class="hl">分档表算</span>`
        + `（${rate}；${how}）`);
    } else {
      problems.push('选了「动态折扣」但分档表是空的 —— '
        + '后端会退回「成本 + 报价金额」，不是动态折扣');
      parts.push(`<span class="hl">★ 分档表为空</span>：实际按「成本 + 报价金额」算`);
    }
  } else {
    parts.push(`命中后：报给买家 <span class="hl">预估成本 + ${r.markup_yuan} 元</span>`);
  }

  // ★ 店主定的：priceMode / maxPrice 是良票接口的字段名（不能改），
  //   所以每次出现都跟中文注释。
  parts.push(`报价模式：<span class="hl">${mode === 'FIXED' ? '一口价：报价即成交价' : '限价：报价是上限'}</span>`);
  if (r.capped) {
    parts.push(`maxPrice（总限价）= (预估成本 + ${r.limit_yuan} 元) × 座位数`);
    if (usesTiers && r.tiers && r.tiers.length) {
      // ★★ 动态折扣下「限价 ≥ 报价」这个约束【前端算不出来】——
      //   分档价要官方原价才能算（折扣率 = 成本 ÷ 原价），
      //   而官方原价是询价时才拿到的。所以这里只能提醒，不能断言。
      //   ★ 后端算的时候会判，并留下 "单张限价低于报价" 的说明。
      parts.push(`<span class="dim">★ 请确认限价 ≥ 分档表算出的价`
        + `（后端询价时会判，不满足会被 300005 拒）</span>`);
    }
  } else {
    parts.push(`<span class="dim">不启用限价，不传 maxPrice</span>`);
  }
  if (r.adjust !== '不调整') {
    parts.push(`最后再做「${esc(r.adjust)}」`);
    if (r.adjust === '四舍五入到元-0.1') {
      parts.push('先四舍五入到整元，再减0.1元；若结果低于成本，仍按成本报价，因此不一定以“.9”结尾');
    }
  }
  parts.push(`<span class="dim">院线 ${r.brands.length ? r.brands.length + ' 条' : '不限'} · `
    + `权重 ${r.weight} · ${r.time_start}-${r.time_end}</span>`);

  const box = $('#rule-preview');
  box.innerHTML = parts.join('　·　') +
    (problems.length
      ? '<div style="margin-top:6px">' +
        problems.map(p => `<span class="bad">✗ ${esc(p)}</span>`).join('<br>') + '</div>'
      : '<div style="margin-top:6px" class="dim">✓ 没有发现问题</div>');
}

/* ============================================================ 保存 */
async function saveRule() {
  if (BUSY) return;
  const rule = collectForm();
  const status = $('#m-status');

  if (!rule.name) {
    status.className = 'm-status error';
    status.textContent = '规则名称不能为空';
    return;
  }
  if (rule.capped && rule.limit_yuan < rule.markup_yuan) {
    status.className = 'm-status error';
    status.textContent = '单张限价不能低于报价金额（下单会被 300005 拒）';
    return;
  }

  if (EDIT_INDEX === null) { EDIT_INDEX = RULES.length; RULES.push(rule); }
  else RULES[EDIT_INDEX] = rule;

  DIRTY = true;
  renderTable();
  status.textContent = '保存中…';
  const ok = await saveAll();
  if (ok) $('#modal').hidden = true;
  else status.textContent = $('#save-status').textContent;
}

async function saveAll() {
  if (BUSY || !SCOPE) return false;
  BUSY = true;
  $('.modal-panel').inert = true;
  ['account','m-save','btn-save','btn-new','btn-reload','push-shop','btn-push'].forEach(id => $('#'+id).disabled = true);
  const status = $('#save-status');
  status.textContent = '保存中…';
  status.className = 'save-status';
  try {
    const res = await fetch(window.withBase('/api/rules'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...SCOPE, rules: RULES }),
    });
    const out = await res.json();
    if (res.ok && out.ok) {
      status.textContent = '已保存 ✓';
      status.className = 'save-status ok';
      showProblems([]);
      DIRTY = false;
      $(".push-tip").textContent = "当前为本店独立规则，保存仅对本店生效。";
      return true;
    } else {
      status.textContent = '未保存：' + (out.error || (out.problems || []).join('；') || '服务器拒绝保存');
      status.className = 'save-status bad';
      showProblems(out.problems);
    }
  } catch (err) {
    status.textContent = '保存失败：' + err.message;
    status.className = 'save-status bad';
  } finally {
    BUSY = false;
    $('.modal-panel').inert = false;
    ['account','m-save','btn-save','btn-new','btn-reload'].forEach(id => $('#'+id).disabled = false);
    syncPushOptions();
  }
  return false;
}

$('#btn-new').onclick = () => openModal(null);
$('#btn-reload').onclick = () => { if (!DIRTY || confirm('放弃未保存的修改并重新加载？')) reloadRules(); };
$('#btn-save').onclick = saveAll;

// ★ 支持 ?open=new 直接打开弹窗 —— 便于自动化验证（无头浏览器截图），
//   也方便把链接直接发给同事看某条规则。?open=2 表示第 3 条（0 基）。
const OPEN_PARAM = new URLSearchParams(location.search).get('open');

bootRules()
  .then(() => {
    if (OPEN_PARAM === null) return;
    if (OPEN_PARAM === 'new' || OPEN_PARAM === '') { openModal(null); return; }
    const idx = Number(OPEN_PARAM);
    if (Number.isInteger(idx) && idx >= 0 && idx < RULES.length) openModal(idx);
  })
  .catch(err => {
    $('#rules-body').innerHTML =
      `<tr><td colspan="7">加载失败：${esc(err.message)}<br>
       确认 <code>python server.py</code> 正在运行。</td></tr>`;
  });

async function reloadRules() {
  ['account','btn-new','btn-save','btn-reload','push-shop','btn-push'].forEach(id => $('#'+id).disabled = true);
  $('#rules-body').innerHTML = '';
  DATA = null;
  RULES = [];
  try {
    await load();
    ['btn-new','btn-save'].forEach(id => $('#'+id).disabled = false);
  } catch (err) {
    $('#rules-hint').textContent = '读取失败：' + err.message;
  } finally {
    $('#account').disabled = false;
    $('#btn-reload').disabled = false;
  }
}
async function bootRules() {
  ['btn-new','btn-save','btn-reload'].forEach(id => $('#'+id).disabled = true);
  const res = await fetch(window.withBase('/api/shops'));
  const data = await res.json();
  if (!res.ok || !data.ok || !data.shops?.length) throw new Error(data.error || '没有可用店铺');
  const shops = data.shops.filter(s => s.tenant_id && s.shop_id);
  SHOP_OPTIONS = shops;
  if (!shops.length) throw new Error('店铺缺少租户信息');
  const account = $('#account');
  account.innerHTML = '';
  shops.forEach(shop => {
    const option = document.createElement('option');
    option.value = JSON.stringify({tenant_id:String(shop.tenant_id),shop_id:String(shop.shop_id)});
    option.textContent = shop.display_name || shop.shop_id;
    account.append(option);
  });
  let remembered = '';
  try { remembered = localStorage.getItem('ticket-shop') || ''; } catch (_) {}
  account.selectedIndex = Math.max(0, shops.findIndex(s => String(s.shop_id) === remembered));
  SCOPE = JSON.parse(account.value);
  account.onchange = async () => {
    if (BUSY || (DIRTY && !confirm('有未保存的修改，放弃并切换店铺？'))) {
      account.value = JSON.stringify(SCOPE); return;
    }
    SCOPE = JSON.parse(account.value);
    $('#modal').hidden = true;
    $('#save-status').textContent = '';
    $('#push-status').textContent = '';
    try { localStorage.setItem('ticket-shop', SCOPE.shop_id); } catch (_) {}
    await reloadRules();
  };
  await reloadRules();
}
window.addEventListener('beforeunload', event => {
  if (DIRTY || BUSY) { event.preventDefault(); event.returnValue = ''; }
});

function syncPushOptions() {
  const select = $('#push-shop');
  const previous = select.value;
  select.innerHTML = '<option value="">请选择目标店铺</option>';
  SHOP_OPTIONS.forEach(shop => {
    if (String(shop.tenant_id) === SCOPE?.tenant_id && String(shop.shop_id) === SCOPE?.shop_id) return;
    const option = document.createElement('option');
    option.value = JSON.stringify({tenant_id:String(shop.tenant_id), shop_id:String(shop.shop_id)});
    option.textContent = shop.display_name || shop.shop_id;
    select.append(option);
  });
  if ([...select.options].some(o => o.value === previous)) select.value = previous;
  select.disabled = BUSY || !DATA || select.options.length < 2;
  $('#btn-push').disabled = BUSY || !DATA || !select.value;
  $('#push-hint').textContent = DIRTY
    ? '有未保存的修改，请先保存当前店铺，再推送。'
    : `复制当前全部 ${RULES.length} 条规则，覆盖目标店铺原规则并立即生效。`;
}
$('#push-shop').onchange = () => {
  $('#push-status').textContent = '';
  $('#btn-push').disabled = BUSY || !DATA || !$('#push-shop').value;
};
$('#btn-push').onclick = async () => {
  if (BUSY || !DATA || !SCOPE || !$('#push-shop').value) return;
  const status = $('#push-status');
  if (DIRTY) { status.textContent = '请先保存当前店铺的修改，再推送。'; return; }
  const target = JSON.parse($('#push-shop').value);
  const name = $('#push-shop').selectedOptions[0].textContent;
  // The selected target and overwrite notice remain visible beside this explicit action.
  const snapshot = JSON.stringify({...target, rules:RULES});
  BUSY = true;
  ['account','btn-save','btn-new','btn-reload','push-shop','btn-push'].forEach(id => $('#'+id).disabled = true);
  status.className = 'save-status'; status.textContent = '正在推送…';
  try {
    const response = await fetch(window.withBase('/api/rules'), {
      method:'PUT', headers:{'Content-Type':'application/json'}, body:snapshot
    });
    const data = await response.json();
    if (!response.ok || data.ok !== true) throw new Error(data.error || data.problems?.join('；') || '服务器未确认成功');
    const readback = await fetch(window.withBase('/api/rules') + '?' + new URLSearchParams(target));
    const actual = await readback.json();
    if (!readback.ok || actual.scope !== 'shop' || !Array.isArray(actual.rules)) throw new Error('已提交，但回读失败；请切换目标店铺核对');
    // Compare fields sent by the editor; server may append dataclass defaults.
    const expected = JSON.parse(snapshot).rules;
    if (expected.length !== actual.rules.length || expected.some((rule,i) =>
      Object.keys(rule).some(k => JSON.stringify(rule[k]) !== JSON.stringify(actual.rules[i][k]))))
      throw new Error('已提交，但回读内容不一致；请切换目标店铺核对');
    status.className = 'save-status ok'; status.textContent = `已推送至「${name}」，并核对生效`;
  } catch (err) {
    status.className = 'save-status bad'; status.textContent = '推送未确认：' + err.message;
  } finally {
    BUSY = false;
    ['account','btn-save','btn-new','btn-reload'].forEach(id => $('#'+id).disabled = false);
    syncPushOptions();
  }
};
