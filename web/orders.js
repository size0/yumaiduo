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
/* orders.js —— 订单管理页
 *
 * ★ 三块：报价记录 / 订单 / 万达出票
 *
 * ★ 这一页补的是真缺口：
 *   /api/orders 之前没有，界面上的「订单管理」按钮是 disabled 的 ——
 *   运营看不到任何一笔报价或订单。
 */

let DATA = null;
let LOAD_VERSION = 0;
let PROFILE_TIMER;          // /api/orders 的全部数据
let FILTER = 'all';       // 订单桶筛选
// ★ 报价 tab 的筛选状态（分开存：两个 tab 的筛选互不影响）
let Q_FILTER = 'all';

const $ = id => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(window.withBase(path), {
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) {
    const err = new Error(data.message || data.error || ('HTTP ' + res.status));
    err.payload = data;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso).slice(0, 16);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ================================================================ 渲染 */
function renderStorage() {
  const st = DATA.storage || {};
  const bad = [];
  if (!st.quotes) bad.push('报价记录');
  if (!st.orders) bad.push('订单');
  const warn = $('store-warn');
  if (bad.length) {
    warn.hidden = false;
    warn.textContent = `★ ${bad.join('、')}存储打不开 —— 这一页看不到数据。`;
  } else {
    warn.hidden = true;
  }

}

function renderBuckets() {
  const B = DATA.buckets || {};
  const names = {
    all: '全部', unpaid: '待付款', awaiting_ticket: '待出票',
    ticketed: '已出票', delivered: '已交付', attention: '要人管', closed: '已结束',
  };
  const order = ['all', 'unpaid', 'awaiting_ticket', 'ticketed',
    'delivered', 'attention', 'closed'];
  $('buckets').innerHTML = order.map(k => {
    const n = k === 'all' ? (B.all ?? 0) : (B[k] ?? 0);
    const cls = 'bucket' + (FILTER === k ? ' active' : '')
      + (k === 'attention' && n > 0 ? ' warn' : '');
    return `<button class="${cls}" data-bucket="${k}">
              <span class="b-n">${n}</span>
              <span class="b-t">${names[k]}</span>
            </button>`;
  }).join('');
  // ★★★ 2026-10-14 改成【容器限定 + 事件委托】。
  //   原来是 document.querySelectorAll('.bucket') —— 没限定容器，
  //   而报价 tab 的桶用的也是 .bucket ⇒ 它把报价的桶一起绑了，
  //   并用 el.dataset.bucket（对报价桶是 undefined）覆盖了报价的 onclick。
  //   现象：点报价的桶变成"按 undefined 筛订单"，两个 tab 互相踩。
  //   ★ 委托到 #buckets 上，只认 [data-bucket]：对方怎么重画都不影响。
  const box = $('buckets');
  if (box && !box.dataset.delegated) {
    box.dataset.delegated = '1';
    box.addEventListener('click', ev => {
      const el = ev.target.closest('[data-bucket]');
      if (!el || !box.contains(el)) return;
      FILTER = el.dataset.bucket;
      window.recordOffset = 0;
      load().catch(e => { $('record-error').textContent = e.message; });
    });
  }
}

function renderOrders() {
  const all = DATA.orders || [];
  const rows = all;
  $('n-orders').textContent = DATA.counts?.orders ?? all.length;
  $('orders-note').textContent = FILTER === 'all'
    ? `${all.length} 笔` : `${rows.length} 笔（已筛）`;
  const body = $('orders-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="10" class="od-empty">' +
      (all.length ? '这个桶里没有订单。' : '还没有订单。') + '</td></tr>';
    return;
  }
  const routes={WANDA:'万达',LIANGPIAO:'良票'};
  const statuses={MANUAL_CONFIRMED:'人工成交价已核对',STALE:'旧报价改价记录',SUCCEEDED:'已核验改价成功',SENT:'请求已发出',PENDING:'待执行',CLAIMED:'处理中',SENDING:'处理中',UNKNOWN:'结果待核实',FAILED:'改价失败',SUPPRESSED:'已停止',GATE_REJECTED:'检查未通过'};
  body.innerHTML = rows.map(o => `<tr>
    <td>${fmtTime(o.created_at)}<br><span class="dim">${esc(o.platform_order_id || '尚未关联闲鱼订单')}</span></td>
    <td><b>${esc(o.buyer_name || '昵称暂不可用')}</b><br><span class="dim">${esc(o.record_kind === 'PLATFORM_ORDER' ? '出票路线待核对' : routes[o.provider] || '未知路线')}</span></td>
    <td>${o.quote_bound ? '已绑定' : o.record_kind === 'PLATFORM_ORDER' ? '购票信息待核对' : '报价记录缺失'}<br><span class="dim">${esc(o.quote_id)}</span></td>
    <td><b>${esc(o.cinema || '—')}</b><br>${esc(o.movie || '')}<br><span class="dim">${esc(o.show || '')}</span></td>
    <td>${esc((o.seats || []).join('、') || '待核对')}<br><span class="dim">${o.ticket_count ? o.ticket_count+' 张' : '张数待核对'}</span></td>
    <td>${Number(o.amount_yuan).toFixed(2)} 元${Number.isFinite(o.manual_settlement?.source_amount_fen)?'<br><span class="dim">原自动报价 '+Number(o.manual_settlement.source_amount_fen/100).toFixed(2)+' 元</span>':''}<br><span class="dim">预估毛利 ${o.margin_yuan==null?'—':Number(o.margin_yuan).toFixed(2)+' 元'}</span></td>
    <td>${esc(o.state_label)}<br><span class="dim">${esc(o.payment_label)}</span>${o.platform_paid_yuan==null?'':'<br><span class="dim">实付 '+Number(o.platform_paid_yuan).toFixed(2)+' 元</span>'}${o.can_confirm_manual_price?'<br><button type="button" data-manual-settlement="'+esc(o.order_id)+'">确认人工成交价</button>':''}</td>
    <td>${esc(statuses[o.reprice_status] || '暂无改价记录')}${o.reprice_failure_reason?'<br><span class="dim">'+esc(o.reprice_failure_reason)+'</span>':''}${o.reprice_target_yuan==null?'':'<br><span class="dim">目标 '+Number(o.reprice_target_yuan).toFixed(2)+' 元</span>'}</td>
    <td>${esc((o.wanda_ticket_image ? '已登记二维码' : (o.ticket_codes || []).join('、')) || '暂无票码')}<br><span class="dim">${o.shipped?'已发货':o.platform_shipped_observed?'平台已发货（交付待核对）':'未发货'}</span></td>
    <td>${esc(o.supplier_order_id || '—')}</td>
  </tr>`).join('');
}

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-manual-settlement]');
  if (!button) return;
  const row = (DATA?.orders || []).find(o => o.order_id === button.dataset.manualSettlement);
  if (!row) return;
  const value = window.prompt('请填写你已人工确认的整单成交金额（元）。仅登记结算，出票和发货保持暂停。', Number(row.platform_paid_yuan).toFixed(2));
  if (value === null) return;
  if (!/^\d+(\.\d{1,2})?$/.test(value.trim()) || Number(value) <= 0) return window.alert('请输入正确的金额');
  const amount = Math.round(Number(value) * 100);
  if (!Number.isSafeInteger(amount)) return window.alert('金额无效');
  if (!window.confirm(`订单 ${row.platform_order_id}\n人工成交 ${value} 元。将实时核对平台实付并保留原报价；不会自动出票、发送或发货。确认登记？`)) return;
  button.disabled = true;
  try {
    await api('/api/order/manual-settlement', {method:'POST', body:JSON.stringify({
      order_id:row.order_id, tenant_id:row.tenant_id, shop_id:row.shop_id,
      amount_fen:amount, expected_quote_id:row.quote_id,
      expected_amount_fen:Math.round(Number(row.amount_yuan)*100), expected_updated_at:row.updated_at,
      approval_id:crypto.randomUUID()
    })});
    await load();
  } catch (error) { window.alert(error.message); }
  finally { button.disabled = false; }
});

/* 出票表单：下拉只列【能出票】的订单 */
function renderDeliverForm() {
  const rows = (DATA.delivery_candidates || []).filter(o =>
    o.provider === 'WANDA' && o.payment_state === 'CONFIRMED' && !o.shipped
    && ['PAID', 'AWAITING_TICKET', 'TICKETED'].includes(o.state));
  const sel = $('df-order');
  const keep = sel.value;
  sel.innerHTML = rows.length
    ? rows.map(o => `<option value="${esc(o.order_id)}">
        ${esc(o.cinema)} · ${esc(o.movie)} · ${esc(o.show)}
        （${o.ticket_count} 张 · ${o.amount_yuan} 元）
      </option>`).join('')
    : '<option value="">（没有待出票的订单）</option>';
  if (keep && rows.some(o => o.order_id === keep)) sel.value = keep;
  if (sel.value !== keep) clearQrImage();
  $('btn-deliver').disabled = QR_SENDING || !rows.length;
}

function render() {
  // ★ 报价记录已拆回独立页（/quotes）⇒ 这里不再调它的渲染。
  //   ★ 我拆回去时漏删了这两行 —— 留着是死代码：
  //     独立页加载 orders.js 吗？不加载；订单页加载 quotes.js 吗？也不加载。
  //     ⇒ `typeof renderQuotes === 'function'` 永远 false。
  renderStorage();
  renderBuckets();
  renderOrders();
  renderDeliverForm();
  renderAuth();
  renderPlugin();
}

/* ================================================================ 插件密钥 */
function renderPlugin() {
  const p = DATA.plugin || {};
  const note = $('plug-note');
  note.textContent = p.configured ? '已配置 · 通常无需修改' : '未配置 · 请联系维护人员';
  note.style.color = p.configured ? '#52c41a' : '#ff4d4f';
  $('plug-secret').value = '';
  $('plug-secret').placeholder = p.secret_hint
    ? `已配置：${p.secret_hint}（要改就填新的）` : '还没配置，点右边生成';
  const q = p.queue || {};
  const parts = [];
  if (typeof q.PENDING === 'number') {
    parts.push(`待执行 ${q.PENDING}`);
    parts.push(`已发出 ${q.SENT}`);
    if (q.FAILED) parts.push(`失败 ${q.FAILED}`);
    if (q.UNKNOWN) parts.push(`★ 未确认 ${q.UNKNOWN}`);
    if (q.SUPPRESSED) parts.push(`放弃 ${q.SUPPRESSED}`);
    parts.push(`待处理事件 ${q.events_pending}`);
  }
  $('plug-hint').innerHTML = parts.length
    ? `队列：${esc(parts.join(' · '))}<br>` +
      `★ 插件要连的后端地址：<code>${esc(p.backend_url || '')}</code>` +
      `（由维护人员管理）`
    : '';
}

async function savePlugin() {
  const msg = $('plug-msg');
  msg.hidden = true;
  const body = {};
  const k = $('plug-secret').value.trim();
  if (k) body.secret = k;             // ★ 留空 = 不改
  try {
    await api('/api/plugin-auth', { method: 'POST', body: JSON.stringify(body) });
    DATA.plugin = await api('/api/plugin-auth');
    renderPlugin();
    msg.hidden = false;
    msg.className = 'alert-ok';
    msg.textContent = '✓ 已保存';
  } catch (e) {
    msg.hidden = false;
    msg.className = 'alert-warn';
    msg.textContent = '✗ ' + e.message;
  }
}

/* ================================================================ 加载 */
async function load() {
  const version = ++LOAD_VERSION;
  clearTimeout(PROFILE_TIMER);
  const result = await api('/api/orders?' + window.recordParams('orders', FILTER));
  if (version !== LOAD_VERSION) return;
  DATA = result;
  if (DATA.profiles_pending) PROFILE_TIMER = setTimeout(() => load().catch(() => {}), 3000);
  window.recordPage(DATA.page);
  // ★ 鉴权配置一起拉（出票页要显示"能不能用"）
  try {
    DATA.auth = await api('/api/delivery-auth');
  } catch (e) {
    DATA.auth = { configured: false, missing: ['读取失败'] };
  }
  // ★ 插件密钥状态
  try {
    DATA.plugin = await api('/api/plugin-auth');
  } catch (e) {
    DATA.plugin = { configured: false };
  }
  render();
}

let QR_DATA = '', QR_ORDER = '', QR_LOADING = false, QR_VERSION = 0, QR_SENDING = false;
function clearQrImage() {
  QR_VERSION++; QR_DATA = ''; QR_ORDER = ''; QR_LOADING = false;
  $('df-image').value = ''; $('df-preview').hidden = true;
  $('df-preview').removeAttribute('src'); $('df-clear-image').hidden = true;
  $('df-image-note').textContent = 'PNG、JPG、WEBP，最大 5MB';
}
async function selectQrImage(file) {
  if (QR_SENDING) return;
  clearQrImage(); const version = QR_VERSION, orderId = $('df-order').value;
  if (!orderId) return showMsg('请先选择订单', true);
  if (!file) return;
  if (!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size > 5 * 1024 * 1024)
    return showMsg('请选择 5MB 以内的 PNG、JPG 或 WEBP 图片', true);
  QR_LOADING = true;
  try {
    const data = await new Promise((resolve,reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('图片读取失败')); reader.readAsDataURL(file);
    });
    const img = new Image(); img.src = data; await img.decode();
    if (version !== QR_VERSION) return;
    if ($('df-order').value !== orderId) return;
    QR_ORDER = orderId; QR_DATA = data; $('df-preview').src = data; $('df-preview').hidden = false;
    $('df-clear-image').hidden = false; $('df-image-note').textContent = file.name || '已粘贴图片';
  } catch { if (version === QR_VERSION) showMsg('图片无法读取，请重新选择', true); }
  finally { if (version === QR_VERSION) QR_LOADING = false; }
}
/* ================================================================ 出票 */
async function submitDelivery() {
  const msg = $('deliver-msg');
  const out = $('deliver-out');
  msg.hidden = true; out.hidden = true;

  const orderId = $('df-order').value.trim();
  const imageData = QR_DATA;
  const operator = $('df-operator').value.trim();
  if (!orderId) return showMsg('请选一个订单', true);
  if (!imageData || QR_LOADING || QR_ORDER !== orderId) return showMsg('请先上传取票二维码图片', true);


  if (QR_SENDING) return;
  QR_SENDING = true;
  $('btn-deliver').disabled = true;
  try {
    // ★ 界面也走【同一道鉴权】（/api/order/deliver 需要 Bearer）。
    //   ★ 为什么不给界面开一条绕过鉴权的后门：
    //     那样"鉴权"就只是装饰，而且两条路的行为迟早不一致
    //     （一条有店铺白名单检查、一条没有）。
    //   ★ 密钥从 /api/delivery-token 取：这个后台是运营在本机/内网开的，
    //     能打开它的人本来就是运营。若要暴露到公网，
    //     必须给后台本身加登录 —— 那时这个接口就是泄露点。
    const tok = await api('/api/delivery-token');
    if (!tok.ok) {
      showMsg('★ ' + (tok.message || '出票接口未启用'), true);
      return;
    }
    const r = await api('/api/order/deliver', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json; charset=utf-8',
        'Authorization': 'Bearer ' + tok.token,
      },
      body: JSON.stringify({
        order_id: orderId,
        image_data: imageData,
        operator: operator,
        provider_order_id: $('df-provider').value.trim(),
        source: $('df-source').value,
      }),
    });
    showMsg('✓ 二维码已登记，等待插件发送。实际发送和发货结果请在订单记录中查看。', false);
    out.hidden = false;
    out.textContent = JSON.stringify(r, null, 2);
    clearQrImage();
    await load();
  } catch (e) {
    showMsg('✗ ' + e.message, true);
    if (e.payload) { out.hidden = false; out.textContent = JSON.stringify(e.payload, null, 2); }
  } finally {
    QR_SENDING = false;
    $('btn-deliver').disabled = !$('df-order').value;
  }
}

/* ================================================================ 鉴权配置 */
function renderAuth() {
  const a = DATA.auth || {};
  const note = $('auth-note');
  if (a.configured && a.write_enabled) {
    note.textContent = '已配置并启用';
    note.style.color = '#52c41a';
  } else if (a.configured) {
    note.textContent = '★ 配好了但开关关着（出票接口不可用）';
    note.style.color = '#fa8c16';
  } else {
    note.textContent = '未启用 · 暂不能登记票码';
    note.style.color = '#ff4d4f';
  }
  $('auth-tenant').value = a.tenant_id || '';
  $('auth-shops').value = (a.shop_ids || []).join(',');
  $('auth-enabled').checked = !!a.write_enabled;
  // ★ 密钥【不回显明文】—— 只显示掩码让运营确认"配了哪一把"
  $('auth-key').value = '';
  $('auth-key').placeholder = a.delivery_key_hint
    ? `已配置：${a.delivery_key_hint}（要改就填新的）`
    : '还没配置，粘贴密钥';
  $('auth-missing').textContent = (a.missing || []).length
    ? '还缺：' + a.missing.join('、') : '';
}

async function saveAuth() {
  const msg = $('auth-msg');
  msg.hidden = true;
  const body = {
    tenant_id: $('auth-tenant').value.trim(),
    shop_ids: $('auth-shops').value.trim(),
    write_enabled: $('auth-enabled').checked,
  };
  // ★ 密钥留空 = 不改（不是清空）——
  //   界面不回显明文，所以必须能区分"没动"和"要清掉"。
  const k = $('auth-key').value.trim();
  if (k) body.delivery_key = k;
  try {
    DATA.auth = await api('/api/delivery-auth', {
      method: 'POST', body: JSON.stringify(body),
    });
    renderAuth();
    msg.hidden = false;
    msg.className = 'alert-ok';
    msg.textContent = '✓ 已保存' + (DATA.auth.configured && DATA.auth.write_enabled
      ? '，出票接口可用' : '（还缺配置，出票接口仍不可用）');
  } catch (e) {
    msg.hidden = false;
    msg.className = 'alert-warn';
    msg.textContent = '✗ ' + e.message;
  }
}

function showMsg(text, isErr) {
  const el = $('deliver-msg');
  el.hidden = false;
  el.className = isErr ? 'alert-warn' : 'alert-ok';
  el.textContent = text;
}

/* ================================================================ 绑定 */
document.querySelectorAll('.tab').forEach(el => {
  el.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tabpane').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    document.getElementById('pane-' + el.dataset.tab).classList.add('active');
  };
});

window.bindRecordFilters(load, () => { FILTER = 'all'; });
$('btn-reload').onclick = () => load().catch(e => { $('record-error').textContent = e.message; });
$('df-image').onchange = e => selectQrImage(e.target.files[0]);
$('df-clear-image').onclick = clearQrImage;
$('df-order').addEventListener('change', clearQrImage);
$('df-drop').onclick = () => $('df-image').click();
$('df-drop').onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('df-image').click(); } };
$('df-drop').ondragover = e => { e.preventDefault(); $('df-drop').classList.add('drag-over'); };
$('df-drop').ondragleave = () => $('df-drop').classList.remove('drag-over');
$('df-drop').ondrop = e => { e.preventDefault(); $('df-drop').classList.remove('drag-over'); selectQrImage(e.dataTransfer.files[0]); };
$('pane-deliver').addEventListener('paste', e => {
  const item = [...(e.clipboardData?.items || [])].find(x => x.kind === 'file' && x.type.startsWith('image/'));
  if (item) { e.preventDefault(); selectQrImage(item.getAsFile()); }
});
$('btn-deliver').onclick = submitDelivery;
$('btn-save-auth').onclick = saveAuth;
$('btn-save-plug').onclick = savePlugin;
// ★★★ 2026-10-14 删掉了一行死代码：
//       $('btn-gen-secret').onclick = generatePluginSecret;
//
//   #btn-gen-secret 这个元素【从来不存在】，generatePluginSecret
//   这个函数【也从来没定义过】。它在 IIFE 里、且在 load() 之前，
//   所以执行到这里就抛
//       TypeError: Cannot set properties of null
//   后面的 load() 永远不执行 ⇒【整个订单页空白】：状态桶、订单列表全没有。
//   而页面看着是正常渲染的（只是空），控制台那句错很容易被忽略。
//
//   ★ 为什么冒烟测试没抓到：测试拿渲染后的 DOM 判断，
//     验的是"表格存在""有告警区"这类静态结构 —— 空表也能过。
//   ★ 秘钥是手填后点「保存」的（#plug-secret + #btn-save-plug），
//     不需要"生成"按钮，所以这里是【删】而不是补。
//   ★ 已补断言（tests/_check_orders_ui.py）：扫
//     "JS 引用的 id 在 HTML 里必须存在"，防止同类孤儿再出现。

load().catch(e => {
  $('store-warn').hidden = false;
  $('store-warn').textContent = '★ 加载失败：' + e.message;
});
