/* ==================================================================== 原子组件层（JS）
   ★★ 2026-10-14 按《UI 架构规范》§3 建。

   规范要求的 7 个组件里，`cn()` 是 JS 的（类名合并），
   其余 6 个（Button/Card/Input/Tag/PageHeader/Empty）在 ui.css 里是类名，
   这里给【元素工厂】——这样"用组件"就不用记一堆类名拼接。

   ★★★ 一条纪律（规范 §1 禁令 1）：
     本文件里不许出现具体色值。颜色全部由 ui.css 的语义令牌决定，
     工厂只负责【结构 + 变体类名】。
     ⇒ 有一条断言专门扫这个文件。

   ★ 为什么不引 React/Vue：
     规范 §6 说得很清楚 —— "先不要引入 redux / 状态机 / react-query，
     先用 hooks + 1~2 个 Context，等出现真实痛点再上"。
     我们连框架都没有（原生 + 服务端直出），
     最合适的"组件层"就是【类名契约 + 元素工厂 + 预览页】。
     引入框架是另一件事，收益不明确、改动面极大。
*/

/* ============================================================ cn()
   类名合并：去重、后者覆盖、过滤假值。
   ★ 为什么需要它：手写字符串拼接在"变体 + 条件类 + 透传 className"
     三者叠加时特别容易出 "ui-btn ui-btn--sm  ui-btn--sm" 这种脏串，
     而脏串会让 CSS 覆盖顺序变得不可预测。
   ★ 用法：cn('ui-btn', size && 'ui-btn--' + size, props.className)
     支持对象写法：cn('ui-btn', { 'ui-btn--loading': loading }) */
function cn(...parts) {
  const out = [];
  const push = (v) => {
    if (!v) return;
    if (typeof v === 'string') { out.push(v); return; }
    if (Array.isArray(v)) { v.forEach(push); return; }
    if (typeof v === 'object') {
      Object.keys(v).forEach(k => { if (v[k]) out.push(k); });
    }
  };
  parts.forEach(push);
  // 去重（保留最后一次出现的位置：后者覆盖 = 放后面）
  const seen = new Set();
  const dedup = [];
  for (let i = out.length - 1; i >= 0; i--) {
    const cls = out[i];
    if (cls && !seen.has(cls)) { seen.add(cls); dedup.unshift(cls); }
  }
  return dedup.join(' ');
}

/* ============================================================ 元素工厂 */
function _el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  Object.keys(attrs).forEach(k => {
    const v = attrs[k];
    if (v === undefined || v === null || v === false) return;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k === 'style') Object.assign(e.style, v);
    else if (k.startsWith('on') && typeof v === 'function') {
      e.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (v === true) e.setAttribute(k, '');
    else e.setAttribute(k, v);
  });
  (Array.isArray(children) ? children : [children]).forEach(c => {
    if (c === null || c === undefined || c === false) return;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return e;
}

/* ★★ 变体白名单（规范 §3 原话：「只认变体名，不接受自定义色值」）。
   为什么要有这个常量：
     · 组件要能【自描述】有哪些变体 —— 看预览页/这个常量就知道能传什么；
     · Button/Tag 会对传进来的 variant 做校验，拼错的变体名会被挡下并报错，
       而不是静默生成一个没有样式的按钮（那种最难查）。
   ★ 我原来只在注释里写了"只认变体名"，没真的做白名单 ——
     断言抓到了这种"说了没做"。 */
var BUTTON_VARIANTS = ['primary', 'secondary', 'outline', 'ghost',
                       'danger', 'link'];
var BUTTON_SIZES = ['xs', 'sm', 'md', 'lg'];
var TAG_VARIANTS = ['default', 'brand', 'success', 'warning', 'danger', 'info'];

/* Button
   ★ 只认 variant / size 名 —— 传色值不会被接受（也没地方传）。
     这是规范 §3 的原话："只认变体名，不接受自定义色值"。 */
function Button(props = {}) {
  const { variant = 'secondary', size = 'md', loading = false,
          leftIcon = null, rightIcon = null, disabled = false,
          className = '', ...rest } = props;
  // ★ 变体名校验：拼错就报出来，别静默生成一个没样式的按钮
  if (!BUTTON_VARIANTS.includes(variant)) {
    throw new Error('Button: 未知 variant ' + JSON.stringify(variant) +
                    '，可选：' + BUTTON_VARIANTS.join(' / '));
  }
  if (!BUTTON_SIZES.includes(size)) {
    throw new Error('Button: 未知 size ' + JSON.stringify(size) +
                    '，可选：' + BUTTON_SIZES.join(' / '));
  }
  const kids = [];
  if (loading) kids.push(_el('span', { class: 'ui-btn__spinner' }));
  else if (leftIcon) kids.push(leftIcon);
  if (props.text) kids.push(props.text);
  if (rightIcon) kids.push(rightIcon);
  return _el('button', {
    class: cn('ui-btn',
              'ui-btn--' + size,
              'ui-btn--' + variant,
              loading && 'ui-btn--loading',
              className),
    type: rest.type || 'button',
    disabled: disabled || loading || undefined,
    ...rest,
  }, kids);
}

/* Card */
function Card(props = {}) {
  const { padding = 'md', className = '', title = '', body = null,
          ...rest } = props;
  const kids = [];
  if (title) {
    kids.push(_el('div', { class: 'ui-card__head', text: title }));
  }
  if (body !== null) {
    kids.push(_el('div', { class: 'ui-card__body' },
                  Array.isArray(body) ? body : [body]));
  }
  return _el('div', {
    class: cn('ui-card', 'ui-card--pad-' + padding, className), ...rest,
  }, kids);
}

/* Input */
function Input(props = {}) {
  const { error = '', className = '', ...rest } = props;
  const input = _el('input', {
    class: cn('ui-input', error && 'ui-input--error', className),
    ...rest,
  });
  if (!error) return input;
  // 有 error 时返回一个带说明的包裹元素
  return _el('div', { class: 'ui-field' }, [
    input,
    _el('div', { class: 'ui-field__msg ui-field__msg--error', text: error }),
  ]);
}

/* Tag */
function Tag(props = {}) {
  const { variant = 'default', size = 'md', dot = false,
          text = '', className = '' } = props;
  if (!TAG_VARIANTS.includes(variant)) {
    throw new Error('Tag: 未知 variant ' + JSON.stringify(variant) +
                    '，可选：' + TAG_VARIANTS.join(' / '));
  }
  const kids = [];
  if (dot) kids.push(_el('span', { class: 'ui-tag__dot' }));
  if (text) kids.push(text);
  return _el('span', {
    class: cn('ui-tag', 'ui-tag--' + size,
              variant !== 'default' && 'ui-tag--' + variant, className),
  }, kids);
}

/* PageHeader */
function PageHeader(props = {}) {
  const { title = '', subtitle = '', actions = null } = props;
  return _el('div', { class: 'ui-page-header' }, [
    _el('div', {}, [
      _el('h1', { class: 'ui-page-header__title', text: title }),
      subtitle ? _el('p', { class: 'ui-page-header__sub', text: subtitle }) : null,
    ]),
    actions ? _el('div', { class: 'ui-page-header__actions' },
                  Array.isArray(actions) ? actions : [actions]) : null,
  ]);
}

/* Empty */
function Empty(props = {}) {
  const { icon = null, title = '', description = '', action = null } = props;
  return _el('div', { class: 'ui-empty' }, [
    icon ? _el('div', { class: 'ui-empty__icon' }, [icon]) : null,
    title ? _el('div', { class: 'ui-empty__title', text: title }) : null,
    description ? _el('div', { class: 'ui-empty__desc', text: description }) : null,
    action ? _el('div', { class: 'ui-empty__action' }, [action]) : null,
  ]);
}

/* ★ 挂在 window 上：这几个页面是原生脚本，没有模块系统。
   （用 var 而不是 let：万一被重复加载，var 重复声明不报错，
     而 let 会抛 SyntaxError —— 我在这上面踩过一次。） */
var UI = { cn, el: _el, Button, Card, Input, Tag, PageHeader, Empty,
           BUTTON_VARIANTS, BUTTON_SIZES, TAG_VARIANTS };
