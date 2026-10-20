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
   config.js —— 功能设置页渲染器

   ★ 核心思路：界面完全由后端元数据生成。

     后端 /api/settings 返回每个配置项的
       { key, label, control, value, options, min, max, ... }
     这个文件照着 control 字段决定渲染成什么控件。

     好处：加一个配置项只需在 settings.py 的 dataclass 上加一行
           field(metadata=opt(label=..., control=...))，
           界面自动多一行 —— 不会出现"代码加了、界面忘了"。

   ★ 取值也不手写：collect() 遍历 DOM 上带 data-key 的元素，
     所以新增控件不需要改收集逻辑。
   ============================================================ */

let LAYOUT = null;
let sectionKeys = [];      // 用于导航点击定位

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ---------------------------------------------- 小工具 */
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    // ★ 只跳过 null / undefined。
    //   千万不要把 false 也跳过 —— checked:false、disabled:false 是【有效值】，
    //   跳过它们会导致"未勾选的复选框根本不创建"，整组显示为空白。
    //   （这是截图验证抓出来的真实 bug，不是推测。）
    if (v === null || v === undefined) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('data-') || k === 'title' || k === 'type'
             || k === 'placeholder' || k === 'maxlength' || k === 'rows') {
      node.setAttribute(k, v);
    } else if (k === 'draggable') {
      // ★★ 2026-10-14：draggable 必须当【布尔属性】处理。
      //   踩过的坑：`draggable: 'false'` 走下面的 `node[k] = v`，
      //   而 node.draggable 只认真假值 —— 字符串 'false' 是真值
      //   ⇒ 元素变成可拖动。这个 bug 让"未实现的模式"还能被拖，
      //   而且源码里那行字看着是对的，测试只看源码就假通过了。
      if (v === true || v === 'true') node.setAttribute('draggable', 'true');
      else node.removeAttribute('draggable');
    } else node[k] = v;
  }
  // 子节点里的 false 仍然要跳过（那是用来做条件渲染的惯用法）
  (Array.isArray(children) ? children : [children]).forEach(c => {
    if (c === null || c === undefined || c === false) return;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return node;
}

/* ============================================================
   渲染：一个配置项 → 一个控件
   每种 control 一个 render 分支，公共信息（label/tip）统一处理
   ============================================================ */
const RENDERERS = {

  /* 开关：用一个隐藏 input 存值，按钮只是外观 */
  switch(c, sectionKey) {
    const input = el('input', { type: 'hidden', 'data-key': c.key, value: c.value ? '1' : '' });
    const btn = el('button', {
      class: 'switch' + (c.value ? ' on' : ''),
      type: 'button', role: 'switch', 'aria-checked': String(!!c.value),
    });
    btn.addEventListener('click', () => {
      const on = !btn.classList.contains('on');
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-checked', String(on));
      input.value = on ? '1' : '';
    });
    return el('div', { class: 'field' }, [btn, input]);
  },

  /* 复选框组：每个选项一个独立 input —— 想勾几个勾几个 */
  checks(c) {
    const box = el('div', { class: 'check-group' });
    // ★★★ 2026-10-14 修一个真 bug：「票速设置」把值存丢了。
    //
    //   原来的实现假设【整个组 = 一个数组字段】：
    //       每个 input 都写 data-key = c.key，collect() 聚合成数组。
    //   但票速设置在后端是【三个独立标量】：
    //       speed_flash / speed_fast / speed_slow
    //   而组控件只有一个 key = "speed_flash" ⇒
    //       三个复选框全都写进 speed_flash，fast/slow 两个字段【根本没控件】
    //     ⇒ 一保存 speed_flash 变成数组、fast/slow 保持原值，
    //       而 float/slow 的值再也没人能改（我在浏览器测试里就把它弄成了 []）。
    //
    //   ★ 修法：c.keys 给每个选项指定各自的字段名；
    //     没有 keys 的组（别的页面用的）行为完全不变 —— 仍然是聚合成一个数组。
    const keys = Array.isArray(c.keys) ? c.keys : null;
    (c.options || []).forEach((optName, i) => {
      const oneKey = keys ? keys[i] : c.key;
      const checked = keys
        ? !!(c.value_by_key || {})[oneKey]
        : (Array.isArray(c.value) && c.value.includes(optName));
      const attrs = { type: 'checkbox', 'data-key': oneKey,
                      'data-opt': optName, checked };
      // ★ data-own="1" = "这一项写的是它自己的独立字段（布尔）"，
      //   而不是"整个组合成一个数组"。collect() 靠它分流。
      if (keys) attrs['data-own'] = '1';
      box.appendChild(el('label', {}, [el('input', attrs), optName]));
    });
    return el('div', { class: 'field stretch' }, [el('div', { class: 'check-box' }, [box])]);
  },

  /* 单选组 */
  radio(c) {
    const group = el('div', { class: 'radio-group' });
    const values = c.values || null;
    (c.options || []).forEach((optName, i) => {
      const disabled = (c.disabled_options || []).includes(optName);
      const val = values ? values[i] : optName;
      const input = el('input', { type: 'radio', name: 'r-' + c.key,
                                  'data-key': c.key, value: JSON.stringify(val),
                                  checked: c.value === val, disabled });
      group.appendChild(el('label', { class: disabled ? 'disabled' : '' }, [input, optName]));
    });
    return el('div', { class: 'field' }, [group]);
  },

  /* ★★ 图片上传（取票图）
     ★ 为什么需要它：取票图是【店主的图】，得有个地方传上来。
     ★ 值【不进 settings.json】：那是一段 base64，混进配置文件会让它难读、
       每次读盘都变慢。⇒ 后端在保存时把图截出来存到
       data/ticket-image.json。所以这里的隐藏 input 只负责把 base64 送出去。
     ★ 上传到平台（拿 alicdn URL）不在这里做 ——
       那是【出票时】才做（那时插件才一定在跑）；保存时插件可能没起来，
       传失败店主还以为存上了。 */
  image(c) {
    const input = el('input', { type: 'hidden', 'data-key': c.key, value: '' });
    const box = el('div', { class: 'img-pick' });
    const file = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp',
                               class: 'img-file' });
    const pick = el('button', { class: 'btn', type: 'button', text: '选择图片…' });
    const note = el('span', { class: 'img-note' });
    pick.addEventListener('click', () => file.click());
    file.addEventListener('change', () => {
      const f = file.files && file.files[0];
      if (!f) return;
      // ★ 前端也先挡一道（后端还会再校验一遍，那才是防线）
      if (f.size > 5 * 1024 * 1024) {
        note.textContent = '图片不能超过 5MB';
        note.className = 'img-note bad';
        return;
      }
      const fr = new FileReader();
      fr.onload = () => {
        // ★ FileReader 给的是 data:image/png;base64,xxx ——
        //   整串送出去，后端会剥前缀（前端剥了后端也得能收，
        //   两端都做才是稳的）。
        input.value = String(fr.result || '');
        note.textContent = '已选择：' + f.name + '（' + Math.round(f.size / 1024) + ' KB，保存后生效）';
        note.className = 'img-note ok';
      };
      fr.onerror = () => {
        note.textContent = '读不出这个文件';
        note.className = 'img-note bad';
      };
      fr.readAsDataURL(f);
    });
    // 服务端当前状态（/api/settings 带出来的 ticket_image）
    const now = (window.__DSH_TICKET_IMAGE__) || {};
    const cur = el('span', { class: 'img-cur' });
    if (now.has_image) {
      cur.textContent = '已存：' + (now.name || '取票图')
        + '（' + Math.round((now.bytes || 0) / 1024) + ' KB'
        + (now.uploaded ? '，已上传到平台' : '，还没上传（出票时自动传）') + '）';
    } else {
      cur.textContent = '还没传图';
      cur.className = 'img-cur dim';
    }
    box.appendChild(pick);
    box.appendChild(cur);
    box.appendChild(file);
    return el('div', { class: 'field' }, [box, note, input]);
  },

  /* 数字 */
  number(c) {
    const input = el('input', {
      type: 'number', class: 'num-input', 'data-key': c.key, value: c.value,
      min: c.min, max: c.max, step: c.step || 1,
    });
    return el('div', { class: 'field' }, [input, c.unit ? el('span', { class: 'unit', text: c.unit }) : null]);
  },

  /* 小数 */
  float(c) {
    const input = el('input', {
      type: 'number', class: 'num-input', 'data-key': c.key, value: c.value,
      min: c.min, max: c.max, step: c.step || 0.01,
    });
    return el('div', { class: 'field' }, [input, c.unit ? el('span', { class: 'unit', text: c.unit }) : null]);
  },

  /* 数字范围：100 - 500 毫秒（两个 input 合成一个配置对） */
  number_range(c) {
    const a = el('input', { type: 'number', class: 'num-input narrow', 'data-key': c.key,
                            value: c.value, min: c.min, max: c.max, placeholder: '最短' });
    const b = el('input', { type: 'number', class: 'num-input narrow', 'data-key': c.pair,
                            value: c.pair_value, min: c.min, max: c.max, placeholder: '最长' });
    return el('div', { class: 'field' }, [
      el('span', { class: 'range-line' }, [a, el('span', { class: 'range-sep', text: '-' }), b]),
      c.unit ? el('span', { class: 'unit', text: c.unit }) : null,
    ]);
  },

  /* 时间范围 */
  time_range(c) {
    const a = el('input', { type: 'time', class: 'text-input', 'data-key': c.key,
                            value: c.value, step: 1 });
    const b = el('input', { type: 'time', class: 'text-input', 'data-key': c.pair,
                            value: c.pair_value, step: 1 });
    return el('div', { class: 'field' }, [
      el('span', { class: 'range-line' }, [a, el('span', { class: 'range-sep', text: '-' }), b]),
    ]);
  },

  /* 单行文本 */
  text(c) {
    return el('div', { class: 'field' }, [
      el('input', { type: 'text', class: 'text-input', 'data-key': c.key,
                    value: c.value, placeholder: c.placeholder || '',
                    maxlength: c.maxlength || null }),
    ]);
  },

  /* 多行文本 —— 风控话术都在这类里，用实底保证可读 */
  textarea(c) {
    const ta = el('textarea', { rows: 4, 'data-key': c.key, value: c.value });
    const wrap = el('div', { class: 'ta-wrap' }, [ta]);
    const field = el('div', { class: 'field stretch',
                              style: 'flex-direction:column;align-items:stretch' }, [wrap]);
    if (c.tokens && c.tokens.length) {
      const tokens = el('div', { class: 'tokens' });
      c.tokens.forEach(name => {
        const b = el('button', { class: 'token', type: 'button', text: name });
        b.addEventListener('click', () => insertToken(ta, name));
        tokens.appendChild(b);
      });
      wrap.appendChild(tokens);
    }
    return field;
  },

  /* 可排序列表：优先报价模式 */
  sortable(c) {
    const list = el('div', { class: 'sortable', 'data-key': c.key });
    const order = Array.isArray(c.value) ? c.value.slice() : [];
    const all = c.all_options || order;
    // ★★★ 2026-10-14：哪些模式【真的实现了】。
    //   店主："现在我们设计的报价规则就是简易报价规则，
    //          专业报价规则更加复杂，我没有设计。"
    //   ⇒ 没实现的：显示「未实现」、不可拖动、不可启用。
    //   ★ 为什么不删掉：删了没人知道规划过这几条路，
    //     以后做「专业报价」还得把界面加回来。
    //   ★ 为什么不能让它可启用：一旦能拖到最前并启用，
    //     运营会以为切换过去了，而代码里查不到这个模式 ⇒ 静默按别的算。
    const impl = Array.isArray(c.implemented) && c.implemented.length
      ? c.implemented : null;
    const isImpl = name => !impl || impl.includes(name);
    // 全集按"已启用的排前面、停用的排后面"展示
    const shown = order.concat(all.filter(x => !order.includes(x)));
    shown.forEach((name, i) => {
      const ok = isImpl(name);
      const enabled = order.includes(name) && ok;
      const item = el('div', {
        class: 'sortable-item' + (enabled ? '' : ' off')
               + (ok ? '' : ' not-impl'),
        'data-name': name,
        'data-on': enabled ? '1' : '',
        'data-impl': ok ? '1' : '',
        title: ok ? '' : '这个模式还没实现（当前只有「简易报价规则」可用）',
      }, [
        el('span', { class: 'sort-badge', text: String(i + 1) }),
        el('span', { text: name }),
        ok ? null : el('span', { class: 'not-impl-tag', text: '未实现' }),
        el('span', { class: 'sort-grip', text: ok ? '⠿' : '' }),
      ].filter(Boolean));
      // ★★ draggable 必须用 setAttribute / 不设，不能用 el() 传。
      //   踩过的坑：我写了 `draggable: ok ? 'true' : 'false'`，
      //   而 el() 对非白名单键做的是【属性赋值】`node.draggable = v` ——
      //   传进去的是【字符串 'false'】，它是真值 ⇒ 元素变成可拖动的。
      //   ★ 而且我的测试只断言了源码里有这行字，所以它"通过"了，
      //     实际浏览器里四个都能拖。⇒ 断言必须打在【渲染结果】上。
      if (ok) item.setAttribute('draggable', 'true');
      if (ok) {
        item.addEventListener('click', () => {
          item.dataset.on = item.dataset.on === '1' ? '' : '1';
          item.classList.toggle('off', item.dataset.on !== '1');
        });
        item.addEventListener('dragstart', () => item.classList.add('dragging'));
        item.addEventListener('dragend', () => {
          item.classList.remove('dragging');
          renumber(list);
        });
        item.addEventListener('dragover', ev => {
          ev.preventDefault();
          const dragging = $('.dragging', list);
          if (!dragging || dragging === item) return;
          const rect = item.getBoundingClientRect();
          const after = (ev.clientY - rect.top) > rect.height / 2;
          list.insertBefore(dragging, after ? item.nextSibling : item);
        });
      }
      list.appendChild(item);
    });
    return el('div', { class: 'field' }, [list]);
  },

  /* 按钮弹窗
     ★★★ 2026-10-14 修一个真 bug：
       原来用 prompt() 取值，然后写进 `btn.dataset.value` ——
       而 `collect()` 只收集带 `data-key` 的 <input>，
       【按钮上的 dataset 根本没人读】⇒ 这个控件改了什么都存不下去。
       它看起来能用（会弹窗、会回显你输的字），实际是摆设。
       ★ 影响两个设置：首次打招呼设置、退货信息。
     ★ 现在的做法：给一个真的 hidden input（带 data-key），
       弹窗结果写进它的 value，`collect()` 就能收到了。
     ★ 值是 bool 的（greeting_configured）⇒ 用 '1' / '' 表示，
       和后端 apply_dict 对 bool 的读法一致。 */
  picker(c) {
    // ★ 原值是什么类型，就按什么类型回传（见 collect() 里 hidden 的处理）。
    //   一律当布尔会让 return_info（字典）被写成 false。
    const vtype = Array.isArray(c.value) ? 'list'
      : (c.value && typeof c.value === 'object') ? 'dict'
      : typeof c.value === 'boolean' ? 'bool' : 'str';
    const isBool = vtype === 'bool';
    const initial = isBool ? (c.value ? '1' : '')
      : vtype === 'str' ? String(c.value ?? '')
      : JSON.stringify(c.value ?? (vtype === 'dict' ? {} : []));
    const input = el('input', {
      type: 'hidden', 'data-key': c.key, 'data-vtype': vtype, value: initial,
    });
    const shown = el('span', { class: 'picker-shown' });
    const refresh = () => {
      const v = input.value;
      if (isBool) {
        shown.textContent = v === '1' ? '已设置' : '未设置';
      } else if (vtype === 'str') {
        shown.textContent = v || '未填写';
      } else {
        try {
          const o = JSON.parse(v || '{}');
          const n = Object.keys(o || {}).length;
          shown.textContent = n ? `已填 ${n} 项` : '未填写';
        } catch (e) { shown.textContent = '格式不对'; }
      }
      shown.className = 'picker-shown' + (v ? '' : ' dim');
    };
    const btn = el('button', { class: 'link-btn', type: 'button',
                               text: c.action || '设 置' });
    btn.addEventListener('click', () => {
      if (isBool) {
        // ★ 布尔型：点一下切换（不需要输文字）
        input.value = input.value === '1' ? '' : '1';
        refresh();
        return;
      }
      const box = prompt(c.key === 'return_info'
        ? '退货信息（省市区/街道/姓名/手机号）'
        : '打招呼设置（文字/图片、间隔时长）');
      if (box !== null) {
        input.value = box;
        refresh();
      }
    });
    refresh();
    return el('div', { class: 'field' }, [
      el('span', { class: 'picker-btn' }, [btn, shown,
        el('span', { class: 'hint', text: c.hint || '' })]),
      input,
    ]);
  },
};

function renumber(list) {
  $$('.sortable-item', list).forEach((it, i) => {
    $('.sort-badge', it).textContent = String(i + 1);
  });
}

function insertToken(textarea, name) {
  const pos = textarea.selectionStart ?? textarea.value.length;
  const text = textarea.value;
  textarea.value = text.slice(0, pos) + `[${name}]` + text.slice(pos);
  textarea.focus();
  textarea.selectionStart = textarea.selectionEnd = pos + name.length + 2;
}

/* ============================================================
   渲染：一节配置
   ============================================================ */
/* ★★ 哪些控件类型点了会"变"（必须真的挡住）
   理由：文本/数字类只看一眼值就够（保留可选中，方便复制）；
   但开关/单选/多选一点就变，不挡住用户会以为改成功了。
   ★ 而"改了没生效"正是这一整件事要消灭的东西。 */
const MUTATING_CONTROLS = ['switch', 'radio', 'checks', 'sortable', 'picker'];

function renderSection(section) {
  const renderRow = c => {
    const renderer = RENDERERS[c.control] || RENDERERS.text;
    const isTall = c.control === 'textarea' || c.control === 'sortable' || c.control === 'checks';
    const labelKids = [el('span', { class: 'label-text', text: c.label })];
    if (c.hint) {
      labelKids.push(el('i', { class: 'info', text: 'i', title: c.hint }));
    }
    // ★★ 未接线标记（后端 UNWIRED 注册表带出来的，见 settings.py）
    //   为什么要标：这些控件"能动、能存、但没有任何代码读它"——
    //   运营以为关掉了某个功能，其实没有。标出来才不骗人。
    const unwired = c.unwired || '';
    if (unwired) {
      labelKids.push(el('span', {
        class: 'unwired-badge',
        text: '未接线',
        title: '这个设置目前不影响任何行为。\n' + unwired
              + '\n\n（改它会被保存，但系统干活时不读它。）',
      }));
    }
    const kids = [
      el('div', { class: 'label-row' }, labelKids),
      renderer(c, section.key),
    ];
    if (unwired) {
      kids.push(el('div', { class: 'unwired-note', text: unwired }));
    }
    const row = el('div', {
      class: 'rowBox' + (isTall ? ' tall' : '') + (unwired ? ' unwired' : ''),
      'data-row': c.key,
      'data-unwired': unwired ? '1' : null,
    }, kids);
    // 会变值的控件：真的挡住（renderer 之后再禁用，因为此刻 input 已生成）
    if (unwired && MUTATING_CONTROLS.includes(c.control)) {
      queueMicrotask(() => {
        row.querySelectorAll('input, select, textarea, button').forEach(x => {
          x.disabled = true;
          x.setAttribute('aria-disabled', 'true');
        });
      });
    }
    return row;
  };

  let rows;
  if (section.key === 'recognize_quote') {
    const keywordKeys = new Set(['keyword_cinema', 'keyword_movie', 'keyword_show']);
    const normal = section.controls.filter(c => !keywordKeys.has(c.key)).map(renderRow);
    const keywords = section.controls.filter(c => keywordKeys.has(c.key));
    const keywordFields = keywords.map(c => {
      const renderer = RENDERERS[c.control] || RENDERERS.text;
      const label = el('div', { class: 'keyword-label' }, [
        el('span', { class: 'label-text', text: c.label }),
        c.hint ? el('i', { class: 'info', text: 'i', title: c.hint }) : null,
      ]);
      return el('div', { class: 'keyword-field', 'data-row': c.key }, [
        label, renderer(c, section.key),
      ]);
    });
    const grouped = el('div', { class: 'rowBox keyword-group' }, [
      el('div', { class: 'label-row' }, [
        el('span', { class: 'label-text', text: '候选确认关键字' }),
        el('i', { class: 'info', text: 'i',
                 title: '当前主流程直接回复 1/2/3；只有使用“影院1”这类带前缀回复时才会用到。' }),
      ]),
      el('div', { class: 'keyword-fields' }, keywordFields),
    ]);
    rows = normal.concat(grouped);
  } else {
    rows = section.controls.map(renderRow);
  }

  // ★ section 头部也标一下：整块都没接线的话，一眼就能看出来
  const nUnwired = section.controls.filter(c => c.unwired).length;
  const metaKids = [el('span', {
    class: 'sec-meta', text: section.count + ' 项配置' })];
  if (nUnwired) {
    metaKids.push(el('span', {
      class: 'unwired-badge',
      text: nUnwired + ' 项未接线',
      title: '这一块里有 ' + nUnwired + ' 项设置目前不影响行为',
    }));
  }

  return el('section', { class: 'sec-block', id: 'sec-' + section.key,
                         'data-section': section.key }, [
    el('div', { class: 'sec-head' }, [
      el('span', { class: 'sec-title', text: section.name }),
      el('span', { class: 'sec-desc', text: section.desc }),
    ].concat(metaKids)),
    el('div', { class: 'sec-card' }, rows),
  ]);
}

function renderNav(groups) {
  const nav = $('#cfg-nav');
  nav.innerHTML = '';
  sectionKeys = [];
  groups.forEach(group => {
    const cat = el('div', { class: 'nav-cat' }, [
      el('div', { class: 'nav-cat-label', text: group.name }),
    ]);
    group.sections.forEach(sec => {
      sectionKeys.push(sec.key);
      const item = el('div', { class: 'nav-sec', 'data-target': sec.key }, [
        el('span', { class: 'nav-sec-name', text: sec.name }),
        el('span', { class: 'nav-sec-cnt', text: String(sec.count) }),
      ]);
      item.addEventListener('click', () => {
        const target = document.getElementById('sec-' + sec.key);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      cat.appendChild(item);
    });
    nav.appendChild(cat);
  });
}

/* 滚动时高亮当前所在的配置节 */
function wireScrollSpy() {
  const navItems = $$('.nav-sec');
  const pick = () => {
    let current = sectionKeys[0];
    for (const key of sectionKeys) {
      const node = document.getElementById('sec-' + key);
      if (node && node.getBoundingClientRect().top <= 140) current = key;
    }
    navItems.forEach(it => it.classList.toggle('active', it.dataset.target === current));
  };
  window.addEventListener('scroll', pick, { passive: true });
  pick();
}

function renderDerived(derived) {
  if (!derived) return;
  const cmds = Object.entries(derived.buyer_commands || {})
    .map(([k, v]) => `${v.join(' / ')}`).join(' · ');
  console.log('[派生] 买家指令：', cmds);
  console.log('[派生] 票速档位：', derived.speed_tiers);
  console.log('[派生] 报价有效期：', derived.quote_ttl_minutes, '分钟');
  console.log('[派生] 确认指令：', derived.confirm_keyword);
}

/* ============================================================
   取值：遍历 DOM 上带 data-key 的元素
   ★ 不手写字段列表 —— 新增控件自动被收集
   ============================================================ */
function collect() {
  const data = {};
  $$('[data-section]').forEach(sec => {
    const key = sec.dataset.section;
    const bucket = {};
    $$('[data-key]', sec).forEach(input => {
      const k = input.dataset.key;

      if (input.type === 'checkbox') {
        // ★★★ 2026-10-14 修：checkbox 有两种用途，必须分开处理 ——
        //   ① 组里的一项（有 data-opt）+ data-key 就是它自己的字段名
        //      ⇒ 写【布尔】到那个字段（票速设置：speed_flash/fast/slow）
        //   ② 组里的一项（有 data-opt）+ 组只有一个 key
        //      ⇒ 聚合成【数组】（其它页面用的是这种）
        //   ③ 单个复选框（无 data-opt）⇒ 写布尔
        //   ★ 怎么区分 ① 和 ②：看这个 key 在这个 section 里
        //     是不是【被多个 input 共用】。共用 ⇒ 是数组组。
        //     更稳的做法是看后端有没有给 keys —— 所以①用 data-own="1" 标记。
        if (input.dataset.opt !== undefined) {
          if (input.dataset.own === '1') {
            bucket[k] = !!input.checked;          // ① 独立字段
            return;
          }
          if (!Array.isArray(bucket[k])) bucket[k] = [];   // ② 数组组
          if (input.checked) bucket[k].push(input.dataset.opt);
        } else {
          bucket[k] = input.checked;              // ③ 单个
        }
        return;
      }

      if (input.type === 'radio') {
        if (input.checked) bucket[k] = JSON.parse(input.value);
        return;
      }

      if (input.type === 'hidden') {
        // ★★ 2026-10-14 修：原来一律转成布尔（`input.value === '1'`）。
        //   而 hidden 里不只放布尔 —— picker 控件存的是
        //   bool（greeting_configured）或【字典】（return_info）。
        //   一律转布尔的话，return_info 会被写成 false，
        //   把原来的 {} 覆盖掉 ⇒ 配置坏掉。
        //   ⇒ 按【原值类型】转：
        //        布尔 → "1"/"" 是真假
        //        字典 → JSON 文本
        //        其它 → 原样字符串
        const orig = input.dataset.vtype || 'str';
        if (orig === 'bool') { bucket[k] = input.value === '1'; return; }
        if (orig === 'dict' || orig === 'list') {
          try { bucket[k] = JSON.parse(input.value || (orig === 'dict' ? '{}' : '[]')); }
          catch (e) { bucket[k] = orig === 'dict' ? {} : []; }
          return;
        }
        bucket[k] = input.value;
        return;
      }

      if (input.type === 'number') {
        const n = input.value === '' ? 0 : Number(input.value);
        bucket[k] = Number.isInteger(n) ? n : n;
        return;
      }

      if (input.classList.contains('sortable')) {
        // 可排序：按 DOM 顺序取，且只保留已启用的。
        // ★ 2026-10-14：同时排掉「未实现」的模式 ——
        //   它们本来就不该出现在启用清单里；
        //   万一 DOM 被改过（或者旧配置里带着），这里再兜一道，
        //   保证存下去的永远是"真的能用的那几种"。
        bucket[k] = $$('.sortable-item', input)
          .filter(it => it.dataset.on === '1' && it.dataset.impl !== '0')
          .map(it => it.dataset.name);
        return;
      }

      bucket[k] = input.value;
    });

    // 数字范围 / 时间范围：配对的两个字段已在同一 sec 内各自收集，无需特殊处理
    data[key] = bucket;
  });
  return data;
}

/* ============================================================
   加载 / 保存
   ============================================================ */
async function load() {
  const res = await fetch(window.withBase('/api/settings'));
  LAYOUT = await res.json();
  // ★★ 2026-10-14：把取票图的当前状态挂到 window 上给 image 渲染器读。
  //   为什么要绕一下：图存在 data/ticket-image.json（不在 settings.json 里），
  //   而渲染器是按控件逐行生成的、拿不到整份响应。
  //   ⇒ 在这里存一次，渲染时读。
  window.__DSH_TICKET_IMAGE__ = LAYOUT.ticket_image || {};

  renderNav(LAYOUT.groups);
  const body = $('#cfg-body');
  body.innerHTML = '';
  LAYOUT.groups.forEach(group => {
    group.sections.forEach(sec => body.appendChild(renderSection(sec)));
  });
  wireScrollSpy();
  renderDerived(LAYOUT.derived);
  showProblems(LAYOUT.problems);
  window.markShopReady?.('features');
}

function showProblems(problems) {
  const box = $('#problems');
  if (!problems || !problems.length) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  box.hidden = false;
  box.innerHTML = '<b>⚠ 配置有问题</b><ul>' +
    problems.map(p => `<li>${p}</li>`).join('') + '</ul>';
}

async function save() {
  if (window.shopPageReady && !window.shopPageReady('features')) return false;
  const status = $('#save-status');
  status.textContent = '保存中…';
  status.className = 'save-status';
  try {
    const res = await fetch(window.withBase('/api/settings'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collect()),
    });
    const out = await res.json();
    if (out.ok) {
      status.textContent = '已保存 ✓';
      status.className = 'save-status ok';
      showProblems([]);
      renderDerived(out.derived);
      return true;
    } else {
      status.textContent = '未保存：配置有问题';
      status.className = 'save-status bad';
      showProblems(out.problems);
      return false;
    }
  } catch (err) {
    status.textContent = '保存失败：' + err.message;
    status.className = 'save-status bad';
    return false;
  }
}

$('#btn-save').addEventListener('click', save);
load().catch(err => {
  $('#cfg-body').innerHTML =
    `<div class="placeholder-card"><h2>加载失败</h2><p>${err.message}</p>` +
    `<p>确认 <code>python server.py</code> 正在运行。</p></div>`;
});

/* ================================================================ 良票凭证 */
/* ★ 为什么这一段是本次补的：
     后端一直有 PUT /api/credentials，但前端【一次都没调】——
     于是界面上没有填手机号的入口，而 tel 是下单必填。
     写好了但没接上就是把功能做没了。 */
async function loadCred() {
  const r = await fetch(window.withBase('/api/credentials'));
  const d = await r.json();
  document.getElementById('cred-key').value = d.app_key || '';
  document.getElementById('cred-tel').value = d.tel || '';
  document.getElementById('cred-base').value = d.base_url || '';
  const sec = document.getElementById('cred-secret');
  sec.value = '';
  // ★ 用字符串拼接而不是模板字符串 ——
  //   第一版用反引号写，被写入工具吃掉了，变成裸文本
  //   "已配置：（要改就填新的）" → JS 语法错。
  sec.placeholder = d.app_secret_hint
    ? ('已配置：' + d.app_secret_hint + '（要改就填新的）')
    : '还没配置';
  const note = document.getElementById('cred-note');
  if (d.can_order) {
    note.textContent = '可以下单';
    note.style.color = '#52c41a';
  } else {
    note.textContent = '★ 缺 ' + (d.missing || []).join('、') + '（不能下单）';
    note.style.color = '#ff4d4f';
  }
}

async function saveCred() {
  const msg = document.getElementById('cred-msg');
  msg.hidden = true;
  const body = {
    app_key: document.getElementById('cred-key').value.trim(),
    tel: document.getElementById('cred-tel').value.trim(),
    base_url: document.getElementById('cred-base').value.trim(),
  };
  // ★ 密钥留空 = 不改（不是清空）—— 界面不回显明文，必须能区分这两件事
  const s = document.getElementById('cred-secret').value.trim();
  if (s) body.app_secret = s;
  try {
    const r = await fetch(window.withBase('/api/credentials'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const out = await r.json();
    await loadCred();
    msg.hidden = false;
    msg.className = 'alert-ok';
    msg.textContent = out.ok ? '✓ 已保存' : ('✗ ' + JSON.stringify(out));
  } catch (e) {
    msg.hidden = false;
    msg.className = 'alert-warn';
    msg.textContent = '✗ ' + e.message;
  }
}

document.getElementById('btn-save-cred').onclick = saveCred;
loadCred().catch(() => {});

window.saveShopPage = () => save();
