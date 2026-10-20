/* ============================================================ 主题色盘
   ★ 2026-10-14：店主说「背景色太丑了」，要能自己换。

   设计取舍：
     · 只让选【一个色】，不是"底色 + 光斑"两个 ——
       光斑颜色由 color-mix 从这一个色推出来（见 style.css 的主题区）。
       两个色随便配很容易配出更难看的组合，一个色反而是"闭眼选都不丑"。
     · 存在 localStorage，整站（6 个页面）共用 —— 换一页就丢等于没有。
     · 写 <html data-theme>，样式全在 CSS 里，JS 不碰颜色计算。
*/
(function () {
  'use strict';

  // 预设：色盘上那 8 个圆点。
  //   color 只用于画圆点本身（真实背景由 data-theme 对应的 CSS 决定，
  //   两边写同一个值 —— 圆点颜色和选中后的底色必须一致，否则用户会困惑）
  var PRESETS = [
    { key: 'mist',  name: '雾蓝', color: '#5b7cfa', base: '#f4f6fb', accent: '#5b7cfa' },
    { key: 'sky',   name: '天青', color: '#2f9bd4', base: '#eff6fc', accent: '#2f9bd4' },
    { key: 'mint',  name: '薄荷', color: '#2fae86', base: '#f0f7f4', accent: '#2fae86' },
    { key: 'rose',  name: '藕荷', color: '#e8749e', base: '#fbf3f6', accent: '#e8749e' },
    { key: 'paper', name: '暖纸', color: '#c99a5b', base: '#f7f4ef', accent: '#c99a5b' },
    { key: 'ink',   name: '墨灰', color: '#2b3a4a', base: '#eef1f6', accent: '#2b3a4a' },
    { key: 'plain', name: '无光斑', color: '#c9ccd4', base: '#ececef', accent: 'transparent' }
  ];
  var STORE_KEY = 'ticket-theme';
  var CUSTOM_KEY = 'ticket-theme-custom';

  var html = document.documentElement;

  function apply(key, customColor) {
    if (key === 'custom') {
      // 自定义：底色由 --bg-accent 调淡得出，写在行内变量上
      html.setAttribute('data-theme', 'custom');
      html.style.setProperty('--bg-accent', customColor);
      html.style.setProperty('--bg-base',
        'color-mix(in oklab, ' + customColor + ' 5%, #f6f7f9)');
      return;
    }
    html.setAttribute('data-theme', key);
    html.style.removeProperty('--bg-accent');
    html.style.removeProperty('--bg-base');
  }

  function save(key, customColor) {
    try {
      localStorage.setItem(STORE_KEY, key);
      if (customColor) localStorage.setItem(CUSTOM_KEY, customColor);
    } catch (e) { /* 隐私模式下 localStorage 会抛，忽略即可 */ }
  }

  function load() {
    try {
      var k = localStorage.getItem(STORE_KEY) || 'mist';
      return { key: k, custom: localStorage.getItem(CUSTOM_KEY) || '#5b7cfa' };
    } catch (e) { return { key: 'mist', custom: '#5b7cfa' }; }
  }

  // 颜色白名单校验：值会被写进 style 属性。
  //   ★ 哪怕只可能是自己存的，也要校验 —— 别人塞一条 localStorage
  //     或带 ?theme 参数就能注入；CSS 变量里放任意字符串是能被利用的。
  function safeColor(v, fallback) {
    return /^#[0-9a-fA-F]{6}$/.test(String(v || '')) ? v : fallback;
  }

  function mount() {
    var st = load();
    var custom = safeColor(st.custom, '#5b7cfa');

    // URL 参数可覆盖（?theme=mint / ?theme=%23ff0000）——
    // 方便截图比对，也方便店主把喜欢的色发链接给别人
    var q = new URLSearchParams(location.search).get('theme');
    if (q) {
      if (q.charAt(0) === '#') { custom = safeColor(q, custom); st.key = 'custom'; }
      else if (PRESETS.some(function (p) { return p.key === q; })) { st.key = q; }
    }
    apply(st.key, custom);

    // 挂控件
    var host = document.getElementById('theme-picker');
    if (!host) return;

    var swatches = PRESETS.map(function (p) {
      return '<button class="theme-swatch" data-key="' + p.key + '" ' +
        'title="' + p.name + '" aria-label="' + p.name + '" ' +
        'style="background:' + p.color + '"></button>';
    }).join('');

    host.innerHTML =
      '<button class="theme-btn" id="theme-btn" aria-label="换个背景色" ' +
      'title="换个背景色">' +
      '<svg xmlns="http://www.w3.org/2000/svg" width="17" height="17" ' +
      'viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      '<circle cx="13.5" cy="6.5" r=".5" fill="currentColor"></circle>' +
      '<circle cx="17.5" cy="10.5" r=".5" fill="currentColor"></circle>' +
      '<circle cx="8.5" cy="7.5" r=".5" fill="currentColor"></circle>' +
      '<circle cx="6.5" cy="12.5" r=".5" fill="currentColor"></circle>' +
      '<path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 ' +
      '1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 ' +
      '1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 ' +
      '5.555-5.554C21.965 6.012 17.461 2 12 2z"></path></svg></button>' +
      '<div class="theme-panel" id="theme-panel">' +
      '<p class="tp-title">背景色</p>' +
      '<div class="theme-grid">' + swatches + '</div>' +
      '<div class="theme-custom-row">' +
      '<input type="color" id="theme-custom" value="' + custom + '">' +
      '<span>自定义颜色</span></div></div>';

    var panel = document.getElementById('theme-panel');
    var btn = document.getElementById('theme-btn');
    var input = document.getElementById('theme-custom');

    function markActive() {
      var cur = html.getAttribute('data-theme');
      host.querySelectorAll('.theme-swatch').forEach(function (s) {
        s.classList.toggle('active', s.dataset.key === cur);
      });
      input.classList.toggle('active', cur === 'custom');
    }
    markActive();

    btn.onclick = function (ev) {
      ev.stopPropagation();
      panel.classList.toggle('open');
    };
    // 点面板外面关掉；点面板里面不关
    document.addEventListener('click', function (ev) {
      if (!host.contains(ev.target)) panel.classList.remove('open');
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') panel.classList.remove('open');
    });

    host.querySelectorAll('.theme-swatch').forEach(function (s) {
      s.onclick = function () {
        apply(s.dataset.key, custom);
        save(s.dataset.key, null);
        markActive();
      };
    });

    input.oninput = function () {
      custom = safeColor(input.value, custom);
      apply('custom', custom);
      save('custom', custom);
      markActive();
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
