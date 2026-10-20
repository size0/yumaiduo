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
/* quotes.js —— 报价记录【独立页】
 *
 * ★★ 这个文件的归属改过两次，记清楚免得再来回：
 *   ① 原来：独立页（店主最初的需求）
 *   ② 中段：我改成"订单页的一个 tab 渲染器"（看到 V5 截图里报价记录是 tab，
 *      以为要合并）—— 理解反了
 *   ③ 现在：店主明确「订单管理和报价记录要分为两个菜单栏」
 *      ⇒ 恢复独立页，所以它要自己 fetch、自己持有状态。
 *
 * ★ 数据走 /api/orders（和订单页同一个读模型 application/ledger.py）——
 *   不另开 API：那样"能不能下单 / 算不算有效"的判据会各写一套。
 *
 * ★ 两条从"tab 版"继承下来的正确做法（与一页/两页无关）：
 *   ① 桶用【容器限定 + 事件委托】：同名 class .bucket 在订单页也有一份，
 *      用 document.querySelectorAll('.bucket') 会互相踩（我踩过）。
 *   ② 列头对齐 V5 报价记录 tab 的 11 列。
 */

let DATA = null;
let LOAD_VERSION = 0;
let PROFILE_TIMER;
let FILTER = 'all';
const ROUTE_NAMES = { WANDA: '万达', LIANGPIAO: '良票' };

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
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ================================================================ 汇总 */
function renderSummary() {
  const rows = DATA.quote_summary || DATA.quotes || [];
  const n = st => rows.filter(r => r.status === st).length;
  const usable = rows.filter(r => r.status === 'READY');
  // ★ 毛利合计只算【可使用】的 —— 作废/过期的不会成交，算进去是虚的
  const margin = usable.reduce((a, r) => a + r.margin_total_yuan, 0);
  const loss = usable.filter(r => r.is_loss).length;
  const today = new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date());

  // ★ 桶名对齐 V5（今日报价 / 可使用 / 已确认 / 已失效）；
  //   我们比 V5 多「已作废 / 已过期 / 亏本」——
  //   "作废"和"过期"是两回事（买家改需求 vs 超时），分开更利于排查；
  //   「亏本」V5 没有但很有用（负毛利要立刻看到）。
  const cells = [
    ['all', '全部', rows.length, ''],
    ['TODAY', '今日报价',
     rows.filter(r => new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date(r.created_at)) === today).length, ''],
    ['READY', '可使用', n('READY'), ''],
    ['CONFIRMED', '已确认', n('CONFIRMED'), ''],
    ['SUPERSEDED', '已作废', n('SUPERSEDED'), ''],
    ['EXPIRED', '已过期', n('EXPIRED'), ''],
    ['LOSS', '亏本', loss, loss ? 'warn' : ''],
  ];
  const box = $('summary');
  if (!box) return;
  box.innerHTML = cells.map(([k, label, count, cls]) => `
    <button class="bucket ${FILTER === k ? 'active' : ''} ${cls}"
            data-bucket="${k}">
      <span class="b-n">${count}</span>
      <span class="b-t">${label}</span>
    </button>`).join('');

  // ★★ 容器限定 + 事件委托：只在 #summary 里认 .bucket。
  //   （订单页也有 .bucket；那边早先用 document.querySelectorAll('.bucket')
  //    把这里的桶也绑了，用 dataset.bucket（undefined）覆盖掉我们的 onclick，
  //    结果点报价的桶去筛了订单。委托 + 限定容器之后互不影响。）
  if (!box.dataset.delegated) {
    box.dataset.delegated = '1';
    box.addEventListener('click', ev => {
      const el = ev.target.closest('[data-bucket]');
      if (!el || !box.contains(el)) return;
      FILTER = el.dataset.bucket;
      window.recordOffset = 0;
      load().catch(e => { $('record-error').textContent = e.message; });
    });
  }

  const note = $('sum-note');
  if (note) {
    note.textContent = usable.length
      ? `可使用报价毛利合计 ${margin >= 0 ? '+' : ''}${margin.toFixed(2)} 元` +
        (loss ? `　★ 其中 ${loss} 笔亏本` : '')
      : '还没有可使用报价';
  }
}

/* ================================================================ 明细 */
function renderList() {
  const all = DATA.quotes || [];
  const rows = all;
  const note = $('list-note');
  if (note) {
    note.textContent = FILTER === 'all'
      ? `${all.length} 条` : `${rows.length} 条（已筛）`;
  }
  const body = $('quotes-body');
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="11" class="od-empty">' +
      (all.length ? '这个筛选下没有记录。'
        : '还没有报价记录。买家发一张选座截图、走过报价流程后，这里就会出现。') +
      '</td></tr>';
    return;
  }
  // ★ 列头对齐 V5 报价记录 tab（11 列）：
  //   买家/路线 · 影院 · 影片 · 场次 · 座位 · 成本 · 数量 · 单价/总价 ·
  //   报价时间 · 状态 · 操作
  body.innerHTML = rows.map(r => `
    <tr>
      <td class="quote-time">${fmtTime(r.created_at)}</td>
      <td><b>${esc(r.buyer_name || '昵称暂不可用')}</b><br>
          <span class="dim">${esc(ROUTE_NAMES[r.provider] || r.provider || '')}</span></td>
      <td>${esc(r.cinema || '—')}</td>
      <td>${esc(r.movie || '—')}</td>
      <td>${esc(r.show || '—')}</td>
      <td>${r.seats && r.seats.length
            ? esc(r.seats.join('、'))
            : '<span class="dim">没有具体座位</span>'}</td>
      <td>${r.total_cost_yuan}</td>
      <td>${r.ticket_count}</td>
      <td class="quote-price"><b>${r.unit_sell_yuan}</b> / ${r.total_sell_yuan}</td>

      <td><span class="tag tag-${String(r.status).toLowerCase()}">${esc(r.status_label)}</span></td>
      <!-- ★ 店主定的：英文参数要配中文注释。quote_id 是字段名（不能改）。 -->
      <td><button class="btn-ghost" data-copy-quote="${esc(r.quote_id)}"
                  title="复制报价单编号（quote_id）">复制</button></td>
    </tr>`).join('');

  // 操作列：只做能真用的（V5 那列是"查看/操作"，我们没有可撤销的动作，
  // 不假装有）。复制报价 ID 便于对账时贴给别人。
  body.querySelectorAll('[data-copy-quote]').forEach(b => {
    b.onclick = () => {
      const id = b.dataset.copyQuote;
      if (navigator.clipboard) {
        navigator.clipboard.writeText(id).then(
          () => { b.textContent = '已复制';
                  setTimeout(() => { b.textContent = '复制'; }, 1200); },
          () => { b.textContent = id.slice(0, 6); });
      } else {
        b.textContent = id.slice(0, 6);
      }
    };
  });
}

function render() {
  const st = DATA.storage || {};
  const warn = $('store-warn');
  if (warn) {
    if (!st.quotes) {
      warn.hidden = false;
      warn.textContent = '★ 报价记录存储打不开 —— 这一页看不到数据。';
    } else {
      warn.hidden = true;
    }
  }
  renderSummary();
  renderList();
}

async function load() {
  const version = ++LOAD_VERSION;
  clearTimeout(PROFILE_TIMER);
  const result = await api('/api/orders?' + window.recordParams('quotes', FILTER));
  if (version !== LOAD_VERSION) return;
  DATA = result;
  if (DATA.profiles_pending) PROFILE_TIMER = setTimeout(() => load().catch(() => {}), 3000);
  window.recordPage(DATA.page);
  render();
}

window.bindRecordFilters(load, () => { FILTER = 'all'; });
$('btn-reload').onclick = () => load().catch(e => { $('record-error').textContent = e.message; });
load().catch(e => {
  const warn = $('store-warn');
  if (warn) {
    warn.hidden = false;
    warn.textContent = '★ 加载失败：' + e.message;
  }
});
