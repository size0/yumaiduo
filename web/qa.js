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
/* qa.js —— 关键词回复页
 *
 * ★ 这一页补的是一个真实缺口：
 *   /api/qa 之前只有 GET，界面上没有任何地方能看到、
 *   更别说改这 18 条关键词回复 —— 它们只活在 qa.py 里。
 *
 * ★ 保存策略：整表提交
 *   运营可能同时改多条、调顺序、删条目。整表提交语义最清楚，
 *   也不会出现"改了一半"的中间态。
 */

let DATA = null;          // /api/qa 的全部数据
let EDIT_INDEX = -1;      // 当前编辑第几条（-1 = 新增）
let RULES = [];           // 工作副本（改完点保存才提交）

const $ = id => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(window.withBase(path), {
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* 把内容里的 [id] 标出来，方便一眼看见引用了谁 */
function markRefs(text) {
  const pattern = new RegExp(DATA?.ref_pattern || '\\[([a-z0-9]{6,16})\\]', 'g');
  return esc(text).replace(pattern, (m, id) => {
    const exists = RULES.some(r => r.qa_id === id);
    const cls = exists ? 'qa-ref' : 'qa-ref qa-ref-bad';
    const title = exists ? '引用：' + id : '★ 找不到 ' + id + '，会原样发出去';
    return `<span class="${cls}" title="${title}">[${id}]</span>`;
  });
}

/* ================================================================ 渲染 */
function render() {
  const body = $('qa-body');
  if (!RULES.length) {
    body.innerHTML = '<tr><td colspan="8" class="qa-empty">还没有关键词。点右上角新增。</td></tr>';
    $('count-note').textContent = '0 条';
    return;
  }

  body.innerHTML = RULES.map((r, i) => `
    <tr>
      <td>${i + 1}</td>
      <td class="qa-keyword">${esc(r.keyword)}</td>
      <td><div class="qa-content">${markRefs(r.content)}</div></td>
      <td><span class="qa-badge ${r.trigger === '单人限制' ? 'once' : ''}">${esc(r.trigger)}</span></td>
      <td>${esc(r.total_limit)}</td>
      <td>${esc(r.daily_limit)}</td>
      <td class="qa-id">${esc(r.qa_id)}</td>
      <td>
        <!-- ★★ 2026-10-14 删掉了 ↑ ↓ 上移/下移（店主：「把上下移动删除」）。
             ★ 不只是听命 —— 顺序对关键词回复【没有语义】：
               匹配是"包含关键字就回"，谁在前谁在后不影响结果；
               保存时还会做重复关键字检查。
             ⇒ 那两个箭头给的是【假的控制感】：
               它暗示"顺序重要"，其实不重要。删掉更诚实。 -->
        <button class="btn-primary" data-edit="${i}">编辑</button>
        <button class="btn-ghost qa-delete" data-delete="${i}">删除</button>
      </td>
    </tr>`).join('');

  $('count-note').textContent = RULES.length + ' 条';

  body.querySelectorAll('[data-edit]').forEach(b => {
    b.onclick = () => openModal(Number(b.dataset.edit));
  });
  body.querySelectorAll('[data-delete]').forEach(b => {
    b.onclick = () => deleteRule(Number(b.dataset.delete));
  });

  // ---- ★ 回复内容：超过 5 行才给「展开」按钮 --------------------------
  //   ★ 为什么要判"真的超行"再插按钮：
  //     原来 .qa-content 是 max-height:96px + overflow:hidden，
  //     长回复被【静默截断】—— 用户看不到全文，也不知道被截了。
  //     现在默认收起 5 行；只有真的被截了才出现"展开"，
  //     短内容不会多出一个没用的按钮。
  body.querySelectorAll('.qa-content').forEach(box => {
    // scrollHeight > clientHeight 说明被 clamp 截住了
    if (box.scrollHeight <= box.clientHeight + 2) return;
    const btn = document.createElement('button');
    btn.className = 'qa-more';
    btn.type = 'button';
    btn.textContent = '展开看全';
    btn.onclick = (ev) => {
      ev.stopPropagation();
      const open = box.classList.toggle('open');
      btn.textContent = open ? '收起' : '展开看全';
    };
    box.after(btn);
  });

  checkLocally();
}


/* 客户端先自查一遍，别等提交才报错 */
function checkLocally() {
  const problems = [];
  const seen = {};
  const ids = new Set(RULES.map(r => r.qa_id));
  RULES.forEach((r, i) => {
    const k = (r.keyword || '').trim();
    if (!k) problems.push(`第 ${i + 1} 条关键字为空`);
    else if (seen[k] !== undefined) {
      problems.push(`关键字「${k}」重复（第 ${seen[k] + 1} 条和第 ${i + 1} 条）`);
    } else seen[k] = i;
    if (!(r.content || '').trim()) problems.push(`第 ${i + 1} 条内容为空`);
    const pattern = new RegExp(DATA?.ref_pattern || '\\[([a-z0-9]{6,16})\\]', 'g');
    let m;
    while ((m = pattern.exec(r.content || '')) !== null) {
      if (!ids.has(m[1])) problems.push(`第 ${i + 1} 条引用了不存在的 [${m[1]}]`);
    }
  });

  const box = $('problems');
  if (problems.length) {
    box.innerHTML = '<b>这几处有问题，保存会被拦下：</b><br>'
      + problems.slice(0, 6).map(esc).join('<br>');
    box.hidden = false;
  } else {
    box.hidden = true;
  }
  return problems;
}

/* ================================================================ 弹窗 */
function openModal(index) {
  EDIT_INDEX = index;
  const isNew = index < 0;
  const r = isNew
    ? { qa_id: '', keyword: '', content: '', trigger: '重复触发',
        total_limit: '不限制', daily_limit: '不限制', message_type: '文本消息' }
    : RULES[index];

  $('modal-title').textContent = isNew ? '新增关键词' : ('编辑：' + r.keyword);
  $('qf-keyword').value = r.keyword;
  $('qf-id').value = r.qa_id;
  $('qf-content').value = r.content;
  $('qf-trigger').value = r.trigger;
  $('qf-total').value = r.total_limit;
  $('qf-daily').value = r.daily_limit;
  $('qf-type').value = r.message_type || '文本消息';
  $('btn-del').hidden = isNew;

  $('modal').hidden = false;
  updatePreview();
  setTimeout(() => $('qf-keyword').focus(), 30);
}

function closeModal() { $('modal').hidden = true; }

async function deleteRule(index) {
  const rule = RULES[index];
  if (!rule) return;
  const referencedBy = RULES.filter((r, i) => i !== index &&
    [r.content, ...Object.values(r.channel_contents || {})].some(content =>
      String(content || '').includes('[' + rule.qa_id + ']')));
  if (referencedBy.length) {
    return alert('请先移除以下关键词中的引用，再删除：' + referencedBy.map(r => r.keyword).join('、'));
  }
  if (!confirm(`删除关键词「${rule.keyword}」？`)) return;
  const previous = RULES.slice();
  RULES.splice(index, 1);
  if (await saveAll()) closeModal();
  else { RULES = previous; render(); }
}

/* 本地展开 [id] —— 和 qa.py 的 resolve 一个道理，这里只为预览 */
function expand(text, seen = new Set(), depth = 0) {
  if (depth > 10) return '（引用层级过深，可能是环）';
  const pattern = new RegExp(DATA?.ref_pattern || '\\[([a-z0-9]{6,16})\\]', 'g');
  return String(text || '').replace(pattern, (m, id) => {
    const target = RULES.find(r => r.qa_id === id);
    if (!target) return m + '  ← 找不到这一条';
    if (seen.has(id)) return '（引用成环）';
    return expand(target.content, new Set([...seen, id]), depth + 1);
  });
}

function updatePreview() {
  const text = $('qf-content').value;
  $('qf-count').textContent = text.length + ' / 2000';
  $('qf-preview').textContent = text.trim() ? expand(text) : '（还没写内容）';
}

/* ================================================================ 保存 */
async function saveAll() {
  try {
    await api('/api/qa', { method: 'PUT', body: JSON.stringify({ rules: RULES }) });
  } catch (e) {
    alert('保存失败：' + e.message);
    return false;
  }
  try {
    await load();
    $('order-note').textContent = '已保存';
  } catch (e) {
    render();
    $('order-note').textContent = '已保存，刷新失败，请稍后重新加载';
  }
  return true;
}

async function load() {
  DATA = await api('/api/qa');
  RULES = JSON.parse(JSON.stringify(DATA.rules));
  render();
  $('order-note').textContent = '';
}

/* ================================================================ 事件 */
function initControls() {
  const fill = (el, list, cur) => {
    el.innerHTML = list.map(v =>
      `<option${v === cur ? ' selected' : ''}>${esc(v)}</option>`).join('');
  };
  // 先把选项填上，openModal 里再设 value
  fill($('qf-trigger'), DATA.triggers, DATA.triggers[0]);
  fill($('qf-total'), DATA.limits, '不限制');
  fill($('qf-daily'), DATA.limits, '不限制');
  fill($('qf-type'), DATA.message_types, '文本消息');
}

document.addEventListener('DOMContentLoaded', async () => {
  try {
    await load();
  } catch (e) {
    alert('读不到数据：' + e.message);
    return;
  }
  initControls();

  $('btn-new').onclick = () => openModal(-1);
  $('modal-x').onclick = closeModal;
  // 2026-10-14: 底部按钮条的「取消」已去掉（和「关闭」是同一件事）。
  //   这里原来有 $(btn-cancel).onclick = ... 元素删了之后
  //   它会抛 Cannot set properties of null，而它在【启动路径】上
  //   => 后面那些绑定（btn-save / btn-del）全都不执行，
  //      「保存」和「删除」静默失效。这类错误页面看着正常，最难发现。
  $('modal').onclick = e => { if (e.target === $('modal')) closeModal(); };
  $('qf-content').oninput = updatePreview;
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !$('modal').hidden) closeModal();
  });

  $('btn-save').onclick = async () => {
    const keyword = $('qf-keyword').value.trim();
    const qaId = $('qf-id').value.trim();
    const content = $('qf-content').value;

    if (!keyword) return alert('触发关键字不能为空');
    if (!qaId) {
      // 自动生成一个 6~16 位小写字母数字的 ID
      const gen = 'k' + Math.random().toString(36).slice(2, 10);
      $('qf-id').value = gen;
      return alert('全局调用标识ID 不能为空，已经帮你生成一个：' + gen + '\n再点一次保存。');
    }
    if (!/^[a-z0-9]{6,16}$/.test(qaId)) {
      return alert('标识ID 只能是小写字母和数字，6~16 位');
    }
    // 重复检查（自己不算）
    const dup = RULES.findIndex((r, i) => i !== EDIT_INDEX && r.keyword === keyword);
    if (dup >= 0) return alert(`关键字「${keyword}」已经用在第 ${dup + 1} 条了`);
    const dupId = RULES.findIndex((r, i) => i !== EDIT_INDEX && r.qa_id === qaId);
    if (dupId >= 0) return alert(`标识ID「${qaId}」已经用在第 ${dupId + 1} 条了`);
    if (!content.trim()) return alert('回复内容不能为空');

    const row = {
      ...(EDIT_INDEX >= 0 ? RULES[EDIT_INDEX] : {}),
      qa_id: qaId, keyword, content,
      trigger: $('qf-trigger').value,
      total_limit: $('qf-total').value,
      daily_limit: $('qf-daily').value,
      message_type: $('qf-type').value,
    };
    if (EDIT_INDEX < 0) RULES.push(row);
    else RULES[EDIT_INDEX] = row;

    if (await saveAll()) closeModal();
  };

  $('btn-del').onclick = () => deleteRule(EDIT_INDEX);
});
