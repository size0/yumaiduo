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
/* ============================================================ 店铺选择器
   ★★ 2026-10-14：店主「当前账号 读取当前店铺配置」。

   数据从哪来（重要，决定了为什么要有这个文件）：
     店铺列表**只有插件进程能拿到** —— 它持有鱼麦多的 client（带 pluginToken），
     调 core.shops.list()。后端没有那个 client。
     ⇒ 链路是：浏览器 → 后端 /api/shops → 插件 /internal/shops/read → 鱼麦多
     后端那条出站调用是这次新加的（integrations/yumaiduo/shop_client.py）。

   ★ 为什么不让各页各写一遍：
     账号/店铺下拉出现在两个地方（页头「当前店铺」+ 底部推送栏「请选择店铺」），
     各写一遍必然出现"一处修了一处没修"。统一放这里。

   ★ 失败不静默：/api/shops 会带 error_code 回来，这里把原因显示在下拉旁边。
     几种原因处置完全不同，不能都显示成"没有店铺"：
       PLUGIN_SECRET_MISSING     插件还没自注册交接密钥
       PLUGIN_UNREACHABLE        插件进程没起来 / 地址不对
       SHOP_SCOPE_REQUIRED       租户没同意 account.read
       PLUGIN_SIGNATURE_REJECTED 两边密钥不一致
*/
(function () {
  'use strict';

  var STORE_KEY = 'ticket-shop';        // 记住选过哪个店铺（纯界面偏好）

  // 后端 error_code → 给人看的话
  var WHY = {
    PLUGIN_SECRET_MISSING: '插件还没自注册（拿不到密钥）',
    PLUGIN_UNREACHABLE: '连不上插件进程',
    PLUGIN_SIGNATURE_REJECTED: '插件拒绝了签名（两边密钥不一致）',
    SHOP_SCOPE_REQUIRED: '租户没有授权读店铺（account.read）',
    TENANT_ID_MISSING: '没配租户 id',
    PLUGIN_SHOP_READ_FAILED: '插件读店铺失败',
    PLUGIN_SHOP_BAD_JSON: '插件返回的不是 JSON',
    PLUGIN_SHOP_BAD_SHAPE: '插件返回的形状不对'
  };


  // ★ error_code → 具体怎么排查（不是笼统一句"加载失败"）
  //   为什么分这么细：四种原因处置完全不同，
  //   混成一句"读不到店铺"等于没说。
  //   ★★ 2026-10-14 店主定的：操作台上不许出现"光秃秃的英文"。
  //      下面 these 是【错误码】(后端返回的，不能改) 和【环境变量名】，
  //      所以每条后面都跟一句中文说明它是什么意思、该怎么处理。
  var FIX = {
    PLUGIN_SECRET_MISSING: {
      why: '插件进程还没把密钥交给后端。',
      steps: [
        '确认插件进程在跑（默认 127.0.0.1:4015）。',
        '插件要设 YUMAIDUO_AUTO_REGISTER=true（鱼麦多自动注册开关=真）' +
          '才会去注册，并把 webhookSecret（网页回调密钥）写到' +
          ' V5_BACKEND_SECRET_FILE（后台密钥交接文件）。',
        '检查那个文件在不在、后端能不能读到它。'
      ]
    },
    PLUGIN_UNREACHABLE: {
      why: '连不上插件进程。',
      steps: [
        '确认插件在跑，端口对得上。',
        '后端读的是环境变量 YUMAIDUO_PLUGIN_URL（插件后端地址，' +
          '默认 http://127.0.0.1:4015）。'
      ]
    },
    PLUGIN_SIGNATURE_REJECTED: {
      why: '插件拒绝了后端的签名 —— 两边密钥不一致。',
      steps: [
        '多半是密钥文件被覆盖过（插件重新注册会换一把）。',
        '重启后端让它重新读密钥文件。'
      ]
    },
    SHOP_SCOPE_REQUIRED: {
      // ★ account.read 是鱼麦多的权限名（授权页上写的就是它），不能翻译
      why: '租户没有授权读店铺列表（缺 account.read 权限，即"读取账号"授权）。',
      steps: [
        '去鱼麦多的授权页，把「读取账号/店铺」这一项勾上。',
        '授权是租户级的，改完回这里点刷新。'
      ]
    },
    TENANT_ID_MISSING: {
      why: '没配租户 id，注册时没法指名道姓。',
      // ★ 环境变量名不能翻译
      steps: ['给插件设 YUMAIDUO_TENANT_ID（鱼麦多租户编号，'
              + 'V5 生产环境是 107）。']
    }
  };

  // ★★ 2026-10-14 店主定的：操作台上不许出现"光秃秃的英文"。
  //   平台状态码是鱼麦多返回的英文（不能改），这里给一张中文对照表，
  //   显示成「正常（online）」—— 中文在前、原文放括号里。
  //   ★ 认不出来的状态码【原样显示】：宁可露出英文，
  //     也不要把一个没见过的新状态悄悄显示成"正常"。
  var STATUS_CN = {
    online: '正常',
    offline: '已下线',
    risk_pending: '风控审核中',
    pending: '待审核',
    rejected: '未通过',
    disabled: '已停用',
    suspended: '已暂停'
  };

  function statusText(code) {
    var s = String(code == null ? '' : code);
    var cn = STATUS_CN[s];
    return cn ? cn + '（' + s + '）' : s;
  }

  function load() {
    try { return localStorage.getItem(STORE_KEY) || ''; } catch (e) { return ''; }
  }
  function save(v) {
    try { localStorage.setItem(STORE_KEY, v); } catch (e) { /* 隐私模式 */ }
  }

  // 把一批 <select> 都填上同一个店铺列表
  function fill(selects, shops, remembered) {
    selects.forEach(function (sel) {
      var isPush = sel.id === 'push-shop';
      sel.innerHTML = '';
      if (!isPush) {
        // 页头那个是"当前店铺"，不需要空选项
      } else {
        var ph = document.createElement('option');
        ph.value = '';
        ph.textContent = '请选择店铺';
        sel.appendChild(ph);
      }
      shops.forEach(function (s) {
        var o = document.createElement('option');
        o.value = s.shop_id;
        o.textContent = s.display_name || s.shop_id;
        if (s.platform_enabled === false) o.textContent += '（未启用）';
        sel.appendChild(o);
      });
      // 回填记住的那个；没有就用第一个
      var want = remembered && shops.some(function (s) {
        return s.shop_id === remembered;
      }) ? remembered : (isPush ? '' : shops[0].shop_id);
      sel.value = want;
    });
  }

  // 失败时：下拉里放一条说明，并在它后面插一句可见的原因
  function showWhy(selects, text) {
    selects.forEach(function (sel) {
      var isPush = sel.id === 'push-shop';
      sel.innerHTML = '';
      var o = document.createElement('option');
      o.value = '';
      o.textContent = isPush ? '请选择店铺' : '读不到店铺';
      sel.appendChild(o);
      sel.disabled = true;
      // 原因写在提示文字里（每个下拉后面那截 .push-tip / 旁边）
      var tip = sel.parentElement
        && sel.parentElement.querySelector('.push-tip');
      if (!tip) {
        // 页头那个没有 .push-tip —— 用 title，鼠标停上去能看到
        sel.title = text;
      }
    });
  }

  function mount() {
    var selects = [];
    var head = document.getElementById('account');
    var push = document.getElementById('push-shop');
    if (head) selects.push(head);
    if (push) selects.push(push);
    if (!selects.length) return;

    fetch(window.withBase('/api/shops'), { headers: { accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok && Array.isArray(d.shops) && d.shops.length) {
          fill(selects, d.shops, load());
          selects.forEach(function (sel) {
            if (sel.id === 'push-shop') return;
            sel.addEventListener('change', function () { save(sel.value); });
          });
          // 把渠道/店铺数写进提示（有人会问"这些是从哪来的"）
          var tip = document.querySelector('.push-bar .push-tip');
          if (tip) {
            tip.textContent = tip.textContent.replace(
              /（来源于[^）]*）/g, '') + '（来源于鱼麦多店铺列表）';
          }
        } else {
          var code = (d && d.error_code) || '';
          var why = WHY[code] || (d && d.error) || '未知原因';
          showWhy(selects, why);
          var t = document.querySelector('.push-bar .push-tip');
          if (t) t.textContent = '读不到店铺（' + why + '）';
          if (head) head.title = why;
          // ★ 控制台留一条，便于开发排查（界面只给一句人话）
          console.warn('[shops] /api/shops 失败:', code, d && d.error);
        }
      })
      .catch(function (e) {
        showWhy(selects, '接口不通');
        console.warn('[shops] 请求 /api/shops 失败:', e && e.message);
      });
  }


/* ============================================================ 店铺管理页
   ★ 这个页面回答一个问题：
     「我这个店现在能不能接单、能不能收发消息？」
     ⇒ 所以重点列 平台启用 / 收发消息(im_enabled) ——
       平台启用了不等于能收发消息，没开 IM 权限两边的话都传不过来。
   ★ 只读：官方只给了「读店铺列表」，没有新建/改名/停用。
     要改得去鱼麦多后台，改完回来点刷新。 */
function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
  });
}

function renderShopsPage() {
  var body = document.getElementById('shops-body');
  if (!body) return;                       // 不是店铺页

  fetch(window.withBase('/api/shops'), { headers: { accept: 'application/json' } })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var warn = document.getElementById('load-warn');
      var trouble = document.getElementById('trouble');
      var note = document.getElementById('list-note');
      var shops = (d && Array.isArray(d.shops)) ? d.shops : [];

      if (!d || !d.ok || !shops.length) {
        var code = (d && d.error_code) || '';
        var fix = FIX[code];
        var why = fix ? fix.why : ((d && d.error) || WHY[code] || '未知原因');
        if (warn) {
          warn.hidden = false;
          warn.textContent = '★ ' + why;
        }
        if (trouble) {
          trouble.hidden = false;
          // ★★ 2026-10-14 店主定的：操作台上不许出现"光秃秃的英文"。
          //   这个 code 是后端返回的【错误码】（如 PLUGIN_SECRET_MISSING），
          //   它本身必须保持英文（是我们和后端约定的标识），
          //   所以前面加一个中文小标签说明它是什么。
          document.getElementById('trouble-code').textContent =
            code ? '错误码 · ' + code : '';
          document.getElementById('trouble-why').textContent = why;
          var ol = document.getElementById('trouble-steps');
          ol.innerHTML = (fix ? fix.steps : [
            '看点这里之后浏览器控制台的 [shops] 那行，里面有后端给的原话。'
          ]).map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('');
        }
        if (note) note.textContent = '0 个店铺';
        body.innerHTML = '<tr><td colspan="5" class="od-empty">' +
          '读不到店铺列表。' + esc(why) + '（上面有排查步骤）</td></tr>';
        window.__shopsLoaded = true;
        return;
      }

      if (warn) warn.hidden = true;
      if (trouble) trouble.hidden = true;
      if (note) {
        var imOff = shops.filter(function (s) { return !s.im_enabled; }).length;
        var dis = shops.filter(function (s) { return !s.platform_enabled; }).length;
        note.textContent = shops.length + ' 个店铺'
          + (dis ? '　★ ' + dis + ' 个平台未启用' : '')
          + (imOff ? '　★ ' + imOff + ' 个未开收发消息' : '');
      }

      body.innerHTML = shops.map(function (s) {
        var name = s.display_name || s.shop_id;
        // ★★★ 2026-10-14 店主加的：店铺级「自动发送」开关。
        //   ★ 它和左边那列「平台启用」【不是一回事】，所以分成两列：
        //     平台启用   ：鱼麦多那边的状态（只读，官方只给了"读店铺列表"）
        //     自动发送   ：我们自己的开关（关掉 ⇒ 不往这个店发自动消息）
        //   ★ 两列视觉上也要区分：只读那列是标签，开关这列是真控件。
        //     混在一起运营会以为"我把平台上的店停用了"。
        var on = s.send_enabled !== false;
        // ★ 用全站同一个开关（style.css 的 .switch，button + .on）——
        //   不另造一套：视觉一致，也不会出现"这页的开关长得不一样"。
        var sw = '<button type="button" role="switch" aria-checked="'
          + (on ? 'true' : 'false') + '" class="switch'
          + (on ? ' on' : '') + '" data-shop="' + esc(s.shop_id) + '" title="'
          + (on ? '关掉后不再往这个店自动发消息'
                : '现在不会往这个店自动发消息（订单止损仍然执行）')
          + '"></button>';
        return '<tr>'
          + '<td><b>' + esc(name) + '</b><br>'
          + '<span class="dim">' + esc(s.shop_id) + '</span><br>'
          + '<span class="dim">租户 ' + esc(s.tenant_id) + '</span></td>'
          // ★★ 2026-10-14 店主定的：操作台上不许出现"光秃秃的英文"。
          //   平台状态是【鱼麦多返回的英文状态码】（online / risk_pending…），
          //   它本身不能改，所以查一张对照表显示成「正常（online）」——
          //   中文在前，原始状态码放括号里，排查时还能对上平台的原文。
          + '<td>' + (s.platform_status
              ? '<span class="tag">' + esc(statusText(s.platform_status))
                + '</span>'
              : '<span class="dim">—</span>') + '</td>'
          + '<td>' + (s.platform_enabled
              ? '<span class="tag tag-ready">启用</span>'
              : '<span class="tag tag-superseded">未启用</span>') + '</td>'
          + '<td>' + sw + '</td>'
          + '<td>' + (s.im_enabled
              ? '<span class="tag tag-ready">已开通</span>'
              : '<span class="tag tag-superseded">未开通</span>') + '</td>'
          + '<td>' + (s.area ? esc(s.area) : '<span class="dim">—</span>')
          + '</td></tr>';
      }).join('');

      // ★ 绑开关：切一下就打 PUT，成功用后端返回的值为准（不靠本地状态猜）
      mountSendSwitches(body, shops);

      // ★ 给测试/排障一个明确的"加载完成"信号：
      //   靠"等 800ms"判断渲染完很脆（网络慢就假失败）。
      window.__shopsLoaded = true;
    })
    .catch(function (e) {
      var warn = document.getElementById('load-warn');
      if (warn) {
        warn.hidden = false;
        warn.textContent = '★ 请求 /api/shops 失败：' + (e && e.message);
      }
      window.__shopsLoaded = true;
    });
}

  // ============================================================ 自动发送开关
  // ★★★ 2026-10-14 店主：「启用怎么没有关闭　这里设置为开关
  //     关闭则不会自动发送信息」
  //
  //   ★ 这个开关管什么、不管什么（必须让运营看懂，否则会误用）：
  //       管  ：我们【是否往这个店自动发消息】（报价卡、AI 回复、
  //             取票码、发货通知、付款回复…）
  //       不管：鱼麦多那边这个店是不是启用（那是平台状态，只读）；
  //             也不管订单止损（错价单该关还是要关 ——
  //             关掉的是"说话"，不是"止损"）
  //   ★ 为什么失败要还原勾选状态：不还原的话界面显示"已关闭"、
  //     实际后端没存上，是最容易骗人的一种不一致。
  function mountSendSwitches(body, shops) {
    var btns = body.querySelectorAll('.switch[data-shop]');
    Array.prototype.forEach.call(btns, function (btn) {
      btn.addEventListener('click', function () {
        var sid = btn.getAttribute('data-shop');
        var want = !btn.classList.contains('on');
        // ★ 先乐观切换（点一下就有反馈），失败再还原
        btn.classList.toggle('on', want);
        btn.setAttribute('aria-checked', String(want));
        btn.disabled = true;
        fetch(window.withBase('/api/shops'), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ shop_id: sid, send_enabled: want }),
        }).then(function (r) {
          return r.json().then(function (d) { return { ok: r.ok, d: d }; });
        }).then(function (res) {
          if (!res.ok || !res.d || res.d.ok !== true) {
            throw new Error((res.d && res.d.error) || ('HTTP ' + res.status));
          }
          // ★ 以后端返回的为准（不靠本地状态猜）
          var real = res.d.send_enabled !== false;
          btn.classList.toggle('on', real);
          btn.setAttribute('aria-checked', String(real));
          say(real
            ? '店铺「' + sid + '」的自动发送已开启'
            : '店铺「' + sid + '」的自动发送已关闭，不再自动发消息'
              + '（订单止损仍会执行）');
        }).catch(function (e) {
          // ★ 失败还原：不还原的话界面显示"已关"、实际没存上，
          //   是最容易骗人的一种不一致。
          btn.classList.toggle('on', !want);
          btn.setAttribute('aria-checked', String(!want));
          say('★ 保存失败：' + (e && e.message) + '（已还原）');
        }).then(function () { btn.disabled = false; });
      });
    });
  }

  // 一行人类可读的结果提示（店铺页顶部那条 warn 复用不了，所以自己找位置）
  function say(msg) {
    var el = document.getElementById('sw-note');
    if (!el) {
      el = document.createElement('p');
      el.id = 'sw-note';
      el.className = 'hint';
      var body = document.getElementById('shops-body');
      var card = body && body.closest ? body.closest('.card') : null;
      if (card && card.parentElement) card.parentElement.insertBefore(el, card.nextSibling);
      else return;
    }
    el.textContent = msg;
    el.hidden = false;
  }

  function boot() {
    mount();                 // 页头/推送栏的下拉（很多页都有）
    renderShopsPage();       // 店铺管理页（只有那一页有 #shops-body）
  }
  // 刷新按钮：改完去鱼麦多后台回来点它重读
  function wireReload() {
    var b = document.getElementById('btn-reload');
    if (!b || !document.getElementById('shops-body')) return;
    b.addEventListener('click', function () {
      window.__shopsLoaded = false;
      renderShopsPage();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      wireReload();
      boot();
    });
  } else {
    wireReload();
    boot();
  }
})();
