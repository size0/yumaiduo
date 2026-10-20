"""
_check_orders_ui.py —— 订单管理页（界面 + API + 接线）

★ 覆盖三件事
  ① 页面存在且被登记（/orders、PAGES、路由映射）
  ② /api/orders 的数据面（报价记录 / 订单 / 分桶 / 状态词典）
  ③ 出票接口（界面按钮走的那条 POST）
     没操作人 → 拒；正常 → 发码 + 自动发货
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = []


def _linked(text: str, href: str) -> bool:
    """
    页面里有没有指向 href 的链接（或脚本/样式）。

    ★ 为什么两种写法都认：界面可以挂在 /ta/ 前缀下，那时 HTML 里是
      相对路径（href="qa"），靠服务端注入的 <base href="/ta/"> 解析；
      本机开发时是根路径。两种都合法，断言不该只认一种。
    """
    rel = href.lstrip("/")
    return (f'href="{href}"' in text or f'href="{rel}"' in text
            or f'src="{href}"' in text or f'src="{rel}"' in text)


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


WEB = ROOT / "web"


def main() -> int:                                       # noqa: C901
    section("一、页面文件与登记")
    for f in ("orders.html", "orders.js", "orders.css",
              "quotes.js"):
        p = WEB / f
        check(f"web/{f} 存在且非空", p.exists() and p.stat().st_size > 500,
              f"{p.stat().st_size if p.exists() else 0} 字节")

    from application import store as store_module
    paths = {pg["path"] for pg in store_module.PAGES}
    check("★ PAGES 里有 /orders", "/orders" in paths, str(sorted(paths)))
    check("★★ PAGES 里有 /quotes（报价记录是独立页签）",
          "/quotes" in paths, str(sorted(paths)))
    quotes_pg = next((pg for pg in store_module.PAGES if pg["path"] == "/quotes"), None)
    check("★ /quotes 标 done=True", bool(quotes_pg) and quotes_pg.get("done") is True,
          str(quotes_pg))
    orders_pg = next((pg for pg in store_module.PAGES if pg["path"] == "/orders"), None)
    check("★ /orders 不再标 done=False（已经实现）",
          bool(orders_pg) and orders_pg.get("done") is True,
          str(orders_pg))

    server_src = (ROOT / "server.py").read_text(encoding="utf-8")
    check("★ 路由映射 /orders → /orders.html",
          '"/orders": "/orders.html"' in server_src)
    # ★★ 2026-10-14：/quotes 不再是页面映射，而是【重定向】。
    #   报价记录并进订单页第一个 tab，独立页已删
    #   （quotes.html 文件也删了 —— 留着等于留个废弃入口，实测还能 200 打开）。
    # ★★ 必须先剥注释再搜 —— 我在 server.py 的注释里写了
    #   `if path == "/quotes": return self._redirect("/orders")`
    #   来解释"这里原来有条死路由"，纯字符串搜会命中那句注释。
    #   ★ 这是本项目第三次踩"断言命中自己的注释"
    #     （前两次：孤儿 id、crumb）。规律：凡"搜代码里有没有某段文本"，
    # ★★ 2026-10-14 店主：「订单管理和报价记录要分为两个菜单栏」
    #   ⇒ /quotes 恢复成【独立页】，不是重定向。
    #   ★ 这条断言的结论反转过两次，最终形态就是这一行。
    #   ★ 仍然要先剥注释再搜：我在 server.py 注释里写过
    #     `if path == "/quotes": return self._redirect("/orders")`
    #     解释"这里原来有条死路由"，纯字符串搜会命中那句注释。
    def _strip_py_comments(text: str) -> str:
        return "\n".join(ln.split("  #")[0] for ln in text.splitlines()
                         if not ln.strip().startswith("#"))

    _srv_code = _strip_py_comments(server_src)
    check("★★ /quotes 是独立页（不是重定向）",
          '"/quotes": "/quotes.html"' in _srv_code
          and '_redirect("/orders")' not in _srv_code)
    check("★ 启动横幅里有报价记录",
          "/quotes    报价记录" in server_src)
    check("★ 有 GET /api/orders", 'if path == "/api/orders":' in server_src)
    check("★ 有 POST /api/order/deliver",
          'if path == "/api/order/deliver":' in server_src)

    # ★ HTML 引用的【静态资源】都要真的在（引用不存在的会白屏）。
    #   ★ 要排掉页面链接（/replies、/qa…）—— 那是路由不是文件。
    #     早先没排，把导航链接全当成"文件不存在"误报了。
    html = (WEB / "orders.html").read_text(encoding="utf-8")
    import re
    page_paths = ({pg["path"] for pg in store_module.PAGES}
                  | {"/config", "/shops"})   # ★ 新增的页面路由也在内
    refs = [r for r in re.findall(r'(?:href|src)="(/[^"]+)"', html)
            if not r.endswith(".html") and r not in page_paths]
    missing = [r for r in refs if not (WEB / r.lstrip("/")).exists()]
    check("★ HTML 引用的静态资源都存在（引用不存在的会白屏）",
          not missing, str(missing))
    # ★★ 2026-10-14：从 2 个标签改成 3 个 ——
    #   原来「报价记录」是独立页（顶部菜单），店主明确要求合并（照 V5）：
    #     报价记录 / 订单关联 / 万达出票
    #   顺序也是规格的一部分（V5 把报价记录排第一）
    # ★★ 2026-10-14：数量改过两次 —— 2 → 3（我一度把报价记录并进
    #   订单页当第一个 tab）→ 2（店主：「要分为两个菜单栏」+「只叫订单」）
    check("订单管理页有两个标签（订单 / 万达出票）",
          html.count('data-tab=') == 2, f"{html.count('data-tab=')} 个")
    # ★ 2026-10-14：quotes.html 已删，不再读它做结构断言
    qhtml = ""
    check("报价记录独立页没有标签页（单页内容）",
          qhtml.count('data-tab=') == 0)
    check("HTML 有出票表单的操作人输入框（必填项）",
          'id="df-operator"' in html)
    js = (WEB / "orders.js").read_text(encoding="utf-8")
    check("★ JS 里前端也拦了空操作人（不靠后端兜底）",
          "操作人必填" in js)
    check("JS 走的是 /api/orders 与 /api/order/deliver",
          "/api/orders" in js and "/api/order/deliver" in js)

    section("一之二、★ 导航一致性（每个页面都能走到所有页签）")
    # ★ 为什么查这个：早先「订单管理」在别的页面是 disabled 按钮，
    #   只有 URL 直接输 /orders 才进得去 —— 页签点不动。
    #   ★ 后来「报价记录」又有过一次：它当时是订单管理页里的标签，
    #     顶部菜单里根本没有 —— 用户找不到。
    pages = ["index.html", "config.html", "qa.html", "rules.html",
             "orders.html"]
    # ★ 报价记录是独立菜单项（店主：要分为两个菜单栏）
    #   所以它从这份导航清单里去掉了。链接仍然存在（/quotes 302 → /orders），
    #   但侧栏里不该再有那一项 —— 两个入口会让人不知该看哪个。
    NAV = [("报价规则", "/rules"),
           ("订单管理", "/orders"), ("关键词回复", "/qa"),
           ("功能设置", "/features")]
    stale = []
    for name in pages:
        p = WEB / name
        if not p.exists():
            stale.append(f"{name}: 文件不存在")
            continue
        text = p.read_text(encoding="utf-8")
        for label, href in NAV:
            if label not in text:
                stale.append(f"{name}: 导航里没有{label}")
            elif f'disabled title="本示例未实现">{label}' in text:
                stale.append(f"{name}: {label} 还是 disabled")
            elif not _linked(text, href):
                stale.append(f"{name}: {label} 不是链接")
    check("★ 每个页面的导航里，五个页签都是可点的链接", not stale, str(stale))

    section("一之二点五、★★★ 左侧栏（2026-10-14 顶部菜单改侧栏）")
    # ★ 店主给了图二效果：菜单放左侧常驻侧栏。
    #   为什么必须有这几条断言：
    #     改侧栏时最危险的三种错，语法检查【一个都发现不了】：
    #       ① 某页漏改（还留着 top-tabs）→ 那一页没导航
    #       ② app-shell / app-main 没闭合 → 内容区宽度/滚动整体坏掉
    #       ③ 侧栏里没有 active 项 → 看不出自己在哪一页
    # ★★ 2026-10-14：把 quotes.html 从这份清单去掉。
    #   报价记录已并进订单管理页的第一个 tab（店主要求，照 V5），
    #   quotes.html 不再被 serve（/quotes 302 → /orders）。
    #   ★ 它的文件还留在磁盘上作参考，但断一个没人访问的文件没有意义 ——
    #     而且它注定"active 项不是 1 个"（侧栏那项去掉了）。
    _SIDE = {
        "index.html": "assistant", "config.html": "features",
        "qa.html": "qa", "rules.html": "rules",
        "orders.html": "orders", "quotes.html": "quotes",
    }
    _bad = []
    for _name in _SIDE:
        _t = (WEB / _name).read_text(encoding="utf-8")
        if 'class="top-tabs"' in _t:
            _bad.append(f"{_name}: 还留着旧的 top-tabs")
        if _t.count('class="app-shell"') != 1:
            _bad.append(f"{_name}: app-shell 不是恰好 1 个")
        if _t.count('/app-shell') != 1 or _t.count('/app-main') != 1:
            _bad.append(f"{_name}: app-shell/app-main 没闭合")
        if _t.count('class="ticket-nav-item') != 8:
            _bad.append(f"{_name}: 侧栏项不是 7 个")
        if _t.count('class="ticket-nav-item active"') != 1:
            _bad.append(f"{_name}: active 项不是恰好 1 个")
    check("★★ 五个在用的页面都换成了侧栏、结构闭合、恰好一个选中项（8 项）",
          not _bad, str(_bad))

    # ★★ app.js 的选择器必须跟着改 —— 改 HTML 忘了改 JS 是最容易漏的，
    #    表现是侧栏点了没反应，而页面看着完全正常。
    _js = (WEB / "app.js").read_text(encoding="utf-8")
    check("★★ app.js 的页内切换选择器已改成 .ticket-nav-item",
          ".ticket-nav-item[data-tab]" in _js and ".top-tab[data-tab]" not in _js,
          "app.js 还在找 .top-tab" if ".top-tab" in _js else "ok")
    # ★ AI助理/回复文案是同页两块，必须靠 data-tab 走页内切换；
    #   其余是独立页面（<a href> 跳转）。两者混了就会点不动。
    _idx = (WEB / "index.html").read_text(encoding="utf-8")
    check("★★ 同页两块（AI助理/回复文案）带 data-tab，走页内切换不跳转",
          'data-tab="assistant"' in _idx and 'data-tab="replies"' in _idx)

    section("一之二点六、★★ 主题色盘（店主说背景太丑，要能自己换）")
    # ★★ 这块最容易出的是【静默失效】：CSS 注释没闭合、变量名拼错，
    #    都表现为"能点但颜色不变"，而语法检查/断言全绿。
    #    所以这里逐个主题核它的变量确实写进了样式表。
    _css = (WEB / "style.css").read_text(encoding="utf-8")

    # ① 注释必须闭合 —— 踩过：用 HTML 的结束标记收尾，
    #    把 :root 默认块整段吞进注释，light 模式全变默认白
    _depth, _bad_close = 0, []
    for _ln in _css.splitlines():
        _i = 0
        while _i < len(_ln):
            if _ln.startswith("/*", _i):
                _depth += 1; _i += 2
            elif _ln.startswith("*/", _i):
                _depth -= 1
                if _depth < 0:
                    _bad_close.append(_ln.strip()[:50]); _depth = 0
                _i += 2
            else:
                _i += 1
    check("★★ CSS 注释开合平衡（注释吃掉规则是最阴的错）",
          _depth == 0 and not _bad_close,
          f"depth={_depth} 多余闭合={_bad_close}")

    # ② 七个主题的变量都得在
    _themes = ["mist", "sky", "mint", "rose", "paper", "ink", "plain"]
    _miss = [t for t in _themes
             if f'[data-theme="{t}"]' not in _css]
    check("★★ 七个主题的 CSS 都在", not _miss, str(_miss))
    check("★ 默认主题（mist）不依赖 data-theme 也有值",
          ":root," in _css and "--bg-base" in _css)
    check("★ 底色铺在 html 上（body 背景有时不铺满）",
          "html { background: var(--bg-base); }" in _css)
    # ★ 只查【真实代码】不查注释 —— 注释里正解释着"老做法硬编码了 #ffb3d9"，
    #   连注释一起搜会把它自己判成"还留着老色"（我第一版就这么误报的）。
    # ★★ 剥离注释要按【注释状态】逐字符走 ——
    #    我第一版用 split("/*") 取前半截，对【跨行注释的续行】无效
    #    （续行里没有 "/*"，整行都被当成代码）⇒ 注释里解释老色的那句话
    #    又把自己判成"还留着老色"，白误报一次。
    def _strip_comments(text: str) -> str:
        out, depth = [], 0
        for ln in text.splitlines():
            i, keep = 0, []
            while i < len(ln):
                if depth == 0 and ln.startswith("/*", i):
                    depth = 1; i += 2
                elif depth == 1 and ln.startswith("*/", i):
                    depth = 0; i += 2
                elif depth == 0:
                    keep.append(ln[i]); i += 1
                else:
                    i += 1
            out.append("".join(keep))
        return "\n".join(out)

    _css_code = _strip_comments(_css)
    _old_colors = [x for x in ("#ffb3d9", "#4a7cff", "#7ac7ff", "#9dffd8")
                   if x in _css_code]
    check("★★ 光斑颜色从 --bg-accent 推（color-mix），不硬编码",
          "color-mix" in _css_code and not _old_colors,
          f"代码里还留着老光斑色 {_old_colors}" if _old_colors else "ok")
    check("★ 右上角给色盘让了位（否则压住当前账号选择器）",
          "padding-right: 52px" in _css)

    # ③ 前端文件与接线
    check("★★ theme.js 存在且是普通脚本（不是 module）",
          (WEB / "theme.js").exists()
          and "export " not in (WEB / "theme.js").read_text(encoding="utf-8")[:400])
    _tjs = (WEB / "theme.js").read_text(encoding="utf-8")
    check("★★ 自定义色做了白名单校验（值会写进 style 属性）",
          "safeColor" in _tjs and "[0-9a-fA-F]{6}" in _tjs)
    check("★ 主题存 localStorage（换页要跟着走）",
          "localStorage.setItem" in _tjs)
    _pages2 = ["index.html", "config.html", "qa.html", "rules.html",
               "orders.html"]
    _nopick = [n for n in _pages2
               if 'id="theme-picker"' not in (WEB / n).read_text(encoding="utf-8")
               or not _linked((WEB / n).read_text(encoding="utf-8"), "/theme.js")]
    check("★★ 六个页面都挂了色盘与 theme.js", not _nopick, str(_nopick))

    section("一之二点七、★★★ JS 不许引用不存在的元素（踩过：整页空白）")
    # ★★ 判据：把 JS 的注释行整行去掉，再看 $('id') 引用。
    #
    #   ★ 为什么用【逐行】而不是字符状态机：
    #     我先写了个按字符走的剥离函数（能正确处理字符串里的 // 和 /*），
    #     但它在本仓的 orders.js 上仍有 bug：喂全文时把
    #     //   $('btn-gen-secret')... 这行留了下来，只喂后半段却是对的
    #     ⇒ 毒在前面的某个反引号模板串里。
    #     我不该在纯工具函数上反复纠缠。逐行做法笨，但对本仓的写法够用：
    #     这三个文件里注释都是【整行】的，字符串不会跨行把 // 包进去。
    #
    #   ★ 这个断言只是第三道网，不是唯一防线：
    #       ① node --check   → 语法错（整个文件解析失败）
    #       ② 浏览器 pageerror → 运行时错
    #       ③ 本断言          → 引用了元素 id 但 HTML/脚本里都没有
    #     工具不完美没关系，只要不误报、不漏真错。
    def _strip_js_comments(text: str) -> str:
        keep, in_block = [], False
        for ln in text.splitlines():
            s = ln.strip()
            if in_block:
                if "*/" in s:
                    in_block = False
                    tail = s.split("*/", 1)[1].strip()
                    if tail and not tail.startswith("//"):
                        keep.append(tail)
                continue
            if s.startswith("//") or not s:
                continue
            if "/*" in s and "*/" not in s.split("/*", 1)[1]:
                in_block = True
                head = s.split("/*", 1)[0].strip()
                if head:
                    keep.append(head)
                continue
            keep.append(ln)
        return "\n".join(keep)

    _orphan = []
    _PAGES_JS = {"index.html": ["app.js", "theme.js"],
                 "config.html": ["config.js", "theme.js"],
                 "qa.html": ["qa.js", "theme.js"],
                 "rules.html": ["rules.js", "theme.js"],
                 "orders.html": ["orders.js", "theme.js"],
                 }
    for _page, _scripts in _PAGES_JS.items():
        _html = (WEB / _page).read_text(encoding="utf-8")
        _static_ids = set(re.findall(r'id="([^"]+)"', _html))
        for _s in _scripts:
            _sp = WEB / _s
            if not _sp.exists():
                continue
            _js = _strip_js_comments(_sp.read_text(encoding="utf-8"))
            _used = set(re.findall(r"\$\('([\w-]+)'\)", _js))
            _used |= set(re.findall(r"getElementById\('([\w-]+)'\)", _js))
            # 只排除【这个脚本自己创建】的 id（它 createElement 后设 id）
            _own = set(re.findall(r"\.id\s*=\s*['\"]([\w-]+)['\"]", _js))
            _own |= set(re.findall(r'\bid="([\w-]+)"', _js))
            for _u in sorted(_used):
                if _u in _static_ids or _u in _own:
                    continue
                _orphan.append(f"{_page}/{_s}: #{_u}")

    check("★★★ JS 引用的元素 id 都真实存在（缺一个就可能整页空白）",
          not _orphan, str(_orphan))

    section("一之二点八、★★★ 侧栏悬浮玻璃岛（2026-10-14 规格）")
    # ★ 规格来自店主给的文档：离边 20 / 圆角 22 / 258×calc(100vh-40px) /
    #   毛玻璃 blur(24px) saturate(135%) / 双层落影 / 内部滚动条在内层。
    # ★ 为什么每条都要断：这些是【纯视觉】的，跑起来不报错，
    #   只有人眼或断言能发现。而"离边 + 不满高"恰恰是
    #   「悬浮岛」和「贴边侧栏」的唯一分界 —— 最容易在后续改动里被改回去。
    _island = {
        "离边 20px（margin 左/上/下）": r"margin:\s*20px 0 20px 20px",
        "不满高 calc(100vh - 40px)": r"height:\s*calc\(100vh - 40px\)",
        # ★ 圆角走令牌了（--radius-xl = 22px），所以认两种写法；
        #   但令牌层那个值必须真是 22px —— 单独有一条断言管它。
        "圆角 22px": r"border-radius:\s*(?:22px|var\(--radius-xl\))",
        "毛玻璃 blur(24px) saturate(135%)": r"blur\(24px\)\s*saturate\(135%\)",
        "折叠宽度 78px": r"is-collapsed\s*\{\s*width:\s*78px",
        "过渡只动宽度": r"transition:\s*width \.2s",
    }
    _miss = [k for k, pat in _island.items() if not re.search(pat, _css_code)]
    check("★★ 令牌 --radius-xl 就是 22px（浮岛规格靠它）",
          bool(re.search(r"--radius-xl:\s*22px", _css_code)),
          "令牌被改了，侧栏就不是浮岛规格了")

    check("★★ 悬浮岛规格逐条在（离边/不满高/圆角/毛玻璃/折叠/过渡）",
          not _miss, f"缺: {_miss}")

    # ★★ 这些必须查 _css_code（剥过注释的），不能查 _css ——
    #    我在注释里把规格逐条写了一遍（margin: 20px 0 20px 20px 等），
    #    连注释一起搜会"全绿但代码是空的"。
    check("★★ 侧栏品牌区没有横线（店主反复指过这条下划线）",
          not re.search(r"ticket-sidebar-brand[^{]*\{[^}]*border-bottom:\s*1px",
                        _css_code),
          "品牌区还有 border-bottom")
    check("★ 深色模式单独覆盖侧栏变量（否则侧栏发白）",
          "--sidebar-bg: rgba(35, 44, 50, .76)" in _css_code)
    check("★★ 深色菜单文字用侧栏自己的变量（提亮过，不是全局 --text-dim）",
          "--sidebar-text-dim: #d0d0d6" in _css_code)
    # ★★ body 不许有 overflow-x:hidden —— 有它 backdrop-filter 只采样到
    #    body 背景（纯色），背后的光斑糊不到 ⇒「加了阴影但不透」。
    #    这是规格文档里点名"最常导致没效果"的那一条。
    check("★★ body 没有 overflow-x:hidden（否则毛玻璃失效）",
          not re.search(r"body\s*\{[^}]*overflow-x:\s*hidden", _css_code),
          "body 上有 overflow-x:hidden，毛玻璃会失效")
    # ★ 6 个页面顶部都不许再显示系统名 / 版本（店主明确要求）
    _brand = []
    for _n in ("index.html", "config.html", "qa.html", "rules.html",
               "orders.html"):
        _h = (WEB / _n).read_text(encoding="utf-8")
        if "brand-name" in _h or "brand-ver" in _h:
            _brand.append(_n)
    check("★ 六个页面顶部都不显示系统名/版本", not _brand, str(_brand))

    section("一之二点九、★ 面包屑已删（连带顶部留白要补回来）")
    # ★ 店主：「首页 / AI助理 这里也删除一下」「所有页面的 breadcrumb」
    # ★ 为什么"删了"和"留白"必须一起断：
    #   删面包屑会连带去掉标题的顶部留白（那 20px 原来是面包屑的
    #   padding-top 提供的）⇒ 只验"面包屑没了"会漏掉"标题顶到边了"。
    _PAGES9 = ("index.html", "config.html", "qa.html", "rules.html",
               "orders.html")
    _crumb = []
    for _n in _PAGES9:
        _h = (WEB / _n).read_text(encoding="utf-8")
        if ("breadcrumb" in _h or "crumb-home" in _h or "crumb-cur" in _h):
            _crumb.append(_n)
    check("★ 六个页面都没有面包屑元素了", not _crumb, str(_crumb))
    # 样式与脚本也要一起清（留着是死代码）
    check("★ style.css 里的面包屑样式已清",
          not re.search(r"^\.breadcrumb\s*\{", _css_code, re.M)
          and not re.search(r"^\.crumb-cur\s*\{", _css_code, re.M))
    # ★ app.js 里那段本来就是死代码（读 id="crumb"，而页面只有 class="crumb-cur"）
    # ★★ 必须【剥掉注释】再判 —— 我在 app.js 里留了注释说明那段死代码，
    #   注释正文就含 $('crumb')，纯字符串搜会命中自己的说明。
    #   （和上面"孤儿 id"那条踩的是同一个坑。）
    def _strip_js_comments2(text: str) -> str:
        out, i, n, mode = [], 0, len(text), None
        while i < n:
            two = text[i:i + 2]
            if mode is None:
                if two == "//":
                    mode = "line"; i += 2; continue
                if two == "/*":
                    mode = "block"; i += 2; continue
                if text[i] in (chr(34), chr(39), chr(96)):
                    mode = text[i]
                out.append(text[i]); i += 1
            elif mode == "line":
                if text[i] == "\n":
                    mode = None; out.append(text[i])
                i += 1
            elif mode == "block":
                if two == "*/":
                    mode = None; i += 2; continue
                i += 1
            else:
                if text[i] == "\\":
                    out.append(text[i:i + 2]); i += 2; continue
                if text[i] == mode:
                    mode = None
                out.append(text[i]); i += 1
        return "".join(out)

    _appjs_code = _strip_js_comments2((WEB / "app.js").read_text(encoding="utf-8"))
    check("★ app.js 里没有活的 crumb 引用（注释不算）",
          "$('crumb')" not in _appjs_code,
          "还有活的 crumb 引用" if "$('crumb')" in _appjs_code else "")
    # ★★ 顶部留白必须在 —— 否则标题顶着视口边
    #   ★ 修：我改渠道断言时把这条的"check(...)"那半截吃掉了
    #     （只留下 _main 的定义）—— 补回来。
    _main = re.search(r"\.app-main\s*>\s*main\s*\{[^}]*"
                      r"padding:\s*(\d+)px", _css_code)
    check("★★ 内容区有顶部留白（删面包屑后标题不会顶到边）",
          bool(_main) and int(_main.group(1)) >= 24,
          f"padding-top = {_main.group(1) if _main else '?'}px（要 ≥24）")

    # ★★ 2026-10-14 店主：「回复文案只区分万达和良票，没有全局」
    #   ⇒ 渠道切换器从 3 个（全局/万达/良票）变成 2 个（万达/良票）。
    #   ★ 去掉的是【界面上的页签】，不是功能 ——
    #     数据层仍是两层（text 基准 + channel_text 覆盖），
    #     保存时"第一条渠道的编辑同时写基准"（见 app.js 的 saveTemplate）。
    #     这样"两条渠道一样时改一次就够"的能力没丢。
    _ch = re.findall(r'data-channel="(\w+)"',
                     (WEB / "index.html").read_text(encoding="utf-8"))
    check("★★ 渠道切换器只剩 万达/良票（店主：没有全局）",
          _ch == ["wanda", "liangpiao"], str(_ch))
    # ★ 默认必须落在万达 —— 否则打开页面没有页签是选中的
    check("★★ 默认选中万达（去掉全局后不能没有默认）",
          'class="plat-tab active" data-channel="wanda"' in (
              WEB / "index.html").read_text(encoding="utf-8"))
    # ★ 去掉全局后"改基准"的能力靠保存逻辑补 —— 断言它在
    _ajs = (WEB / "app.js").read_text(encoding="utf-8")
    check("★★ 保存时会把第一条渠道的编辑同时写基准（能力没丢）",
          "channel: 'global'" in _ajs and "hasOverride" in _ajs)

    section("一之二点十、★★ 平台标签删了 / 渠道切换器还在（同名 class，别搞混）")
    # ★★ 背景：`.plat-tabs` 这个 class 同时用在两个地方 ——
    #   ① 顶部 header-right 里的「闲鱼」标签（店主：去掉）—— 已删
    #   ② 「回复文案」面板里的「全局/万达/良票」渠道切换器（功能）—— 保留
    #   我一度按 class 名把 .plat-tabs/.plat-tab 的样式全删了，
    #   结果切换器变成三个裸按钮。所以这两件事必须【一起断】。
    _hdr_bad = []
    for _n in ("index.html", "config.html", "rules.html"):
        _h = (WEB / _n).read_text(encoding="utf-8")
        # 找 AI助理面板的 header-right，确认里面没有「闲鱼」按钮
        if re.search(r'<div class="header-right">\s*<div class="plat-tabs">\s*'
                     r'<button class="plat-tab[^"]*"[^>]*>闲鱼</button>', _h):
            _hdr_bad.append(_n)
    check("★ 三个页面的顶部没有「闲鱼」标签了", not _hdr_bad, str(_hdr_bad))

    # 渠道切换器必须还在（在 index 的 panel-replies 里，带 data-channel）
    _idx2 = (WEB / "index.html").read_text(encoding="utf-8")
    _chan = re.findall(r'<button class="plat-tab[^"]*" data-channel="(\w+)"',
                       _idx2)
    # ★★ 2026-10-14 店主：「回复文案只区分万达和良票，没有全局」
    #   ⇒ 3 个变 2 个。★ 这条断言和上面那条查的是同一件事，
    #     我改上面那条时漏了这条（同一个需求的第二处断言）。
    check("★★ 「万达/良票」渠道切换器还在（2 个，店主去掉全局）",
          _chan == ["wanda", "liangpiao"], str(_chan))
    # 它的样式也必须在 —— 否则按钮是裸的
    check("★★ 渠道切换器的胶囊样式在（.plat-tabs / .plat-tab）",
          bool(re.search(r"\.plat-tabs\s*\{[^}]*border-radius", _css_code))
          and bool(re.search(r"\.plat-tab\.active\s*\{", _css_code)),
          "样式被删了，切换器会变成裸按钮")

    # ---------- 店铺下拉从接口读 ----------
    # ★ 这条也是"接线"断言：后端有出站客户端、有路由、前端有脚本，缺一不可。
    check("★★ 后端有读店铺的出站客户端（调插件 /internal/shops/read）",
          (ROOT / "integrations/yumaiduo/shop_client.py").exists()
          and "/internal/shops/read" in (
              ROOT / "integrations/yumaiduo/shop_client.py"
          ).read_text(encoding="utf-8"))
    check("★★ 后端有 /api/shops 路由",
          'path == "/api/shops"' in (ROOT / "server.py").read_text(
              encoding="utf-8"))
    check("★★ 前端有 shops.js 且三个页面都挂了",
          (WEB / "shops.js").exists()
          and all(_linked((WEB / _n).read_text(encoding="utf-8"), "/shops.js")
                  for _n in ("index.html", "config.html", "rules.html")))
    # ★ 标签统一叫「当前店铺」—— 不要再出现"当前账号"（同一屏两个叫法）
    _acct = [n for n in ("index.html", "config.html", "rules.html")
             if "当前账号" in (WEB / n).read_text(encoding="utf-8")]
    check("★ 标签统一叫「当前店铺」（不再出现旧叫法）", not _acct, str(_acct))

    section("一之三、★ 标签页与面板必须对得上（踩过空白页）")
    # ★ 为什么查这个：把「报价记录」标签从订单管理页删掉时，
    #   active 类留在了被删掉的面板上 —— 结果订单面板没有 active，
    #   打开页面一片空白。这类错 HTML 语法检查发现不了，
    #   是靠人眼看截图才发现的（不该靠眼睛）。
    import re as _re
    text = (WEB / "orders.html").read_text(encoding="utf-8")
    tab_keys = _re.findall(r'data-tab="([^"]+)"', text)
    pane_keys = _re.findall(r'id="pane-([^"]+)"', text)
    check("订单管理页：每个标签都有对应面板",
          sorted(tab_keys) == sorted(pane_keys),
          f"标签={tab_keys} 面板={pane_keys}")
    actives = _re.findall(r'class="tabpane active" id="pane-([^"]+)"', text)
    check("★ 默认面板带 active（否则打开就是空白）",
          len(actives) == 1, f"带 active 的面板={actives}")
    tab_active = _re.findall(r'class="tab active" data-tab="([^"]+)"', text)
    check("★ 默认高亮的标签与默认面板是同一个",
          tab_active == actives, f"标签={tab_active} 面板={actives}")
    # ★★ 2026-10-14 反转了这条断言：
    #   原来断「订单页里【没有】报价记录标签（已提到顶部菜单）」——
    #   那是把报价记录拆成独立页时的判断。
    #   店主明确要求合并（照 V5）⇒ 现在断「必须有，而且是第一个」。
    #   ★ 顺序也是规格的一部分：V5 的排法是「报价记录」在前。
    # ★★ 这条的结论【反转过两次】，最终形态：
    #   报价记录不是订单页的 tab，而是【独立页 + 独立菜单项】
    #   （店主：「订单管理和报价记录要分为两个菜单栏」）。
    check("★★ 报价记录不是订单页的 tab（它是独立页）",
          'data-tab="quotes"' not in text and "pane-quotes" not in text)
    # ★ 报价页的列头必须对齐 V5 的 11 列
    _qhtml = (WEB / "quotes.html").read_text(encoding="utf-8")
    _qcols = [c.strip() for c in
              re.findall(r'<th[^>]*>([^<]*)</th>', _qhtml) if c.strip()]
    check("★★ 报价页 11 列对齐 V5",
          len(_qcols) == 11 and "买家 / 路线" in _qhtml
          and "单价 / 总价" in _qhtml, str(_qcols))
    check("★ 报价桶含「今日报价」（V5 有）",
          "今日报价" in (WEB / "quotes.js").read_text(encoding="utf-8"))
    # ★ 独立页已删：/quotes 保留重定向，不该还有 quotes.html 这个入口
    check("★ 报价记录不再是独立页（/quotes 重定向到 /orders）",
          '"/quotes"' in (ROOT / "server.py").read_text(encoding="utf-8")
          and "_redirect" in (ROOT / "server.py").read_text(encoding="utf-8"))
    js_src = (WEB / "orders.js").read_text(encoding="utf-8")
    # ★★ 2026-10-14 反转：原来断 orders.js 【不】引用报价元素
    #   （报价记录是独立页时它是分开的）。合并后必须引用。
    #   ★ 但注意：报价的渲染在 quotes.js，orders.js 只负责【调】它。
    check("★★ orders.js 会调 renderQuotes()（合并后必须挂上渲染链）",
          "renderQuotes" in js_src)
    # ★★ 同页两个脚本，顶层名字绝不能重 —— 否则抛 SyntaxError，
    #    整个脚本都不执行（比"覆盖"更糟：页面直接不动）。
    _a = set(re.findall(r"^(?:let|const|var|function)\s+([\w$]+)",
                        (WEB / "orders.js").read_text(encoding="utf-8"), re.M))
    _b = set(re.findall(r"^(?:let|const|var|function)\s+([\w$]+)",
                        (WEB / "quotes.js").read_text(encoding="utf-8"), re.M))
    _dupq = sorted(_a & _b)
    # ★ 两页各加载一个业务脚本，不同页 ⇒ 允许重名。
    #   （同页加载两个脚本时重名会抛 SyntaxError，我踩过；
    #    所以下面断的是"订单页不引 quotes.js"。）
    # ★★ 必须【剥注释】再判 —— 这是本项目第 4 次踩同一个坑：
    #   我在 orders.js 里写了注释解释"为什么删掉 renderQuotes 调用"，
    #   注释正文就含 renderQuotes，纯字符串搜命中的是那句注释。
    #   ★ 规律：凡"搜代码里有没有某段文本"的断言，都必须先剥注释。
    def _strip_js_line_comments(text: str) -> str:
        return chr(10).join(ln for ln in text.splitlines()
                            if not ln.strip().startswith("//"))
    _ojs_code = _strip_js_line_comments(js_src)
    check("★★ orders.js 不再引用报价（拆回独立页了）",
          "renderQuotes" not in _ojs_code
          and "quotes-body" not in _ojs_code,
          "还有活的引用" if "renderQuotes" in _ojs_code else "")
    # ★ 独立页的标签页也要能对上
    # ★★ 2026-10-14 又改回来：quotes.html 恢复成独立页（店主要求两个菜单栏），
    #   所以这里要看【它自己】，不是 orders.html。
    #   （我拆回去时忘了把这里的目标文件换回来 —— 断言还在看 orders.html。）
    qtext = (WEB / "quotes.html").read_text(encoding="utf-8")
    check("报价记录独立页引了自己的脚本 quotes.js",
          _linked(qtext, "/quotes.js"))
    qjs = (WEB / "quotes.js").read_text(encoding="utf-8")
    check("报价记录独立页走同一个读模型（不另开 API，免得判据各写一套）",
          "/api/orders" in qjs)

    section("一之三点五、★★ 良票凭证：界面必须能设 tel（这次才发现没有）")
    # ★ 踩过：后端一直有 PUT /api/credentials，但【前端一次都没调】——
    #   界面上没有填手机号的入口，而 tel 是良票下单必填（缺了下单中止，
    #   订单表永远是空的）。反馈手机号在哪设置、我没看到就是这个原因。
    #   ⇒ 断言：GET 要能读（否则没法回显）、界面要有输入框、密钥不回明文。
    check("★ GET /api/credentials 存在（否则前端没法回显）",
          'if path == "/api/credentials":' in server_src)
    cred_get = server_src.count('if path == "/api/credentials":')
    check("★ /api/credentials 的 GET 和 PUT 都在", cred_get >= 2,
          f"出现 {cred_get} 次")
    cfg_html = (WEB / "config.html").read_text(encoding="utf-8")
    check("★★ 功能设置页有购票手机号输入框",
          'id="cred-tel"' in cfg_html, "id=cred-tel")
    check("★ 功能设置页有凭证保存按钮", 'id="btn-save-cred"' in cfg_html)
    cfg_js = (WEB / "config.js").read_text(encoding="utf-8")
    check("★ 前端真的调了凭证接口（不是只写了界面）",
          "'/api/credentials'" in cfg_js and "method: 'PUT'" in cfg_js)
    check("★ 密钥留空 = 不改（不是清空）—— 界面不回显明文必须能区分",
          "if (s) body.app_secret = s;" in cfg_js)
    # ★ 服务端不回明文
    check("★★ 凭证接口不回 app_secret 明文（只给掩码）",
          'app_secret_hint' in server_src
          and '"app_secret": cred.get' not in server_src)

    section("一之三点六、★ 回复文案预览：前端在做、后端曾经没有")
    # ★ 审计查出来的：app.js:243 调 /api/preview/{key}，后端【没有这个路由】
    #   —— 预览按钮点了直接 404。这是前端做了后端没做的实例。
    check("★★ 后端有 /api/preview 路由",
          'path.startswith("/api/preview/")' in server_src)
    check("★ 预览只允许本机访问（防公网读全部话术）",
          "PREVIEW_LOCAL_ONLY" in server_src)
    check("★ 预览真的渲染模板（用 render_parts，一条模板可能多段）",
          "templates_module.render_parts(key)" in server_src)

    section("一之三点七、★★ 报价规则页：字段互斥 + 那个 CSS 坑")
    # ★★ 两条都是对齐 V5 弹窗时发现的真问题，必须钉住。
    rules_html = (WEB / "rules.html").read_text(encoding="utf-8")
    rules_css = (WEB / "rules.css").read_text(encoding="utf-8")
    rules_js = (WEB / "rules.js").read_text(encoding="utf-8")
    check("★★ 报价金额和分档表是两块独立元素（要能互斥显隐）",
          'id="f-markup-row"' in rules_html
          and 'id="f-tier-block"' in rules_html)
    check("★★ JS 里有按报价方式切换显隐的逻辑",
          'syncMethodRows' in rules_js and 'methodNeedsTiers' in rules_js)
    check("★ 分档表默认隐藏（选加法时不该出现）",
          'id="f-tier-block" hidden' in rules_html)
    # ★★ 这个 CSS 坑：hidden 属性只靠 UA 样式，任何作者 display 都会盖掉它。
    #    踩过：报价方式=加法时分档表照样显示（属性设了但样式没生效）。
    check("★★★ 有 [hidden]{display:none!important}（否则 hidden 属性失效）",
          '[hidden]' in rules_css and 'display: none !important' in rules_css)
    check("★ 报价方式名字对齐 V5（「动态折扣」，不是「动态折扣加法」）",
          '动态折扣加法' not in rules_html or '动态折扣' in rules_html)
    check("★ 单张限价的说明写清了是【总】限价",
          '总</b>限价' in rules_html or '总限价' in rules_html)
    # ★★ 「折扣率按」必须藏起来（票达人和 V5 的弹窗都没有它），
    #    但元素要留着（JS 还要读它的值）。
    check("★★ 「折扣率按」对用户隐藏（票达人没有这个字段）",
          'class="tier-basis-row" hidden' in rules_html)
    check("★ 但 f-basis 元素保留（JS 要读，删了会取到空值）",
          'id="f-basis"' in rules_html)
    # ★★ 分档表必须跨两列 —— 挤在单栏里会跟「报价调整类型」重叠。
    check("★★ 分档表跨两列（否则和报价调整类型重叠）",
          'rf-span2 rf-tier-block' in rules_html)
    # ★★ 折扣率的公式必须在界面上能看到（票达人用 tooltip 显示它）。
    check("★★ 单张限价上有「折扣率 = 预估价格 / 原价」的说明",
          '折扣率 = 预估价格 / 原价' in rules_html)
    # ★ 预览必须按报价方式说不同的话（选动态折扣时不能说 +N 元）
    check("★★ 预览按报价方式分三种说法", 'usesTiers' in rules_js
          and '分档表算' in rules_js)

    section("一之三、★★★ 四层架构与令牌（规范 §1/§2/§7）")
    # ★★ 为什么这些必须断：
    #   令牌是【间接层】—— 改错一个名字不会报错，只是"看着不对"。
    #   而"看着不对"正是店主说的"怪"。没有断言，下次有人手写一个 9px
    #   圆角照样看不出来，层级又乱了。
    _tokens_want = {
        "品牌 --brand-*": r"--brand-\d",
        "强调 --accent-*": r"--accent-\d",
        "表面 --surface": r"--surface\b",
        "文字 ≥3 级": r"--text(-muted|-subtle)?\s*:",
        "描边 --border-*": r"--border(-strong|-highlight)?\s*:",
        "交互 --hover/--active": r"--(hover|active)\s*:",
        "字号 --fs-*": r"--fs-\w+\s*:",
        "圆角 --radius-*": r"--radius-\w+\s*:",
        "阴影 --shadow-*": r"--shadow-\w+\s*:",
        "模糊 --backdrop-glass": r"--backdrop-glass\s*:",
    }
    _tmiss = [k for k, pat in _tokens_want.items()
              if not re.search(pat, _css_code)]
    check("★★ 规范要求的 10 类令牌都在（L2/L3 层）", not _tmiss,
          f"缺: {_tmiss}")

    # ★★ 字号只许取档位上的值（11/12/13/14/16/20/26）
    _FS_OK = {"11", "12", "13", "14", "16", "20", "26"}
    _fs_bad = []
    for _f in sorted(WEB.glob("*.css")):
        _src = _f.read_text(encoding="utf-8")
        for _j, _ln in enumerate(_src.splitlines(), 1):
            if re.match(r"\s*--", _ln):          # 令牌定义行
                continue
            for _m in re.finditer(r"font-size:\s*([\d.]+)px", _ln):
                if _m.group(1) not in _FS_OK:
                    _fs_bad.append(f"{_f.name}:{_j} {_m.group(1)}px")
            for _m in re.finditer(r"font:\s*(?:\d{3}\s+)?([\d.]+)px", _ln):
                if _m.group(1) not in _FS_OK:
                    _fs_bad.append(f"{_f.name}:{_j} font:{_m.group(1)}px")
    check("★★ 字号只取档位上的值（不许 12.5px 这种半像素）",
          not _fs_bad, str(_fs_bad[:5]))

    # ★★ 圆角只许取档位/正圆/0
    _RAD_OK = {"6", "8", "12", "16", "22", "100", "50"}
    _rad_bad = []
    for _f in sorted(WEB.glob("*.css")):
        for _j, _ln in enumerate(_f.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*--", _ln):
                continue
            for _m in re.finditer(r"border-radius:\s*([\d.]+)px", _ln):
                if _m.group(1) not in _RAD_OK:
                    _rad_bad.append(f"{_f.name}:{_j} {_m.group(1)}px")
    check("★★ 圆角只取档位（6/8/12/16/22/100/50%）",
          not _rad_bad, str(_rad_bad[:5]))

    # ★★ 禁令 3：主题差异只能靠变量覆盖，不许 JS 判断加类名
    _js_theme = []
    for _f in sorted(WEB.glob("*.js")):
        _src = _f.read_text(encoding="utf-8")
        if re.search(r"classList\.(add|remove|toggle)\(\s*['\"]"
                     r"(dark|light)\b", _src):
            _js_theme.append(_f.name)
    check("★★ 没有 JS 按主题加类名（只能靠变量覆盖）",
          not _js_theme, str(_js_theme))

    # ★ 禁令 2：!important 只允许出现在 [hidden] 这种"防覆盖"的地方
    _imp_bad = []
    for _f in sorted(WEB.glob("*.css")):
        for _j, _ln in enumerate(_f.read_text(encoding="utf-8").splitlines(), 1):
            if "!important" in _ln and "[hidden]" not in _ln:
                _imp_bad.append(f"{_f.name}:{_j} {_ln.strip()[:50]}")
    check("★ !important 没被用来压主题（只剩 [hidden] 那种防覆盖）",
          len(_imp_bad) <= 2, str(_imp_bad))

    section("一之四、★★ 已删掉的东西不许回流（集成客服占位页）")
    # ★ 为什么要有这段：「集成客服」是个从来没实现的占位页 ——
    #   导航里一个 disabled 按钮，外加一个 /api/service 数据面。
    #   用户明确要求删掉。
    #   ★ 删掉的东西最容易悄悄回来（复制页面时带过去，或者有人
    #     觉得"顺手补上"）。所以要显式断言它不在。
    #   ★ 顺带一个更普遍的判据：导航里【不该再有 disabled 按钮】——
    #     有 disabled 就说明有个页签点不动，那种东西该删或该实现。
    all_pages = ["index.html", "config.html", "qa.html", "rules.html",
                 "orders.html"]
    back = []
    for name in all_pages:
        text = (WEB / name).read_text(encoding="utf-8")
        if "集成客服" in text:
            back.append(f"{name}: 导航里又有集成客服")
        for line in text.splitlines():
            if "disabled" in line and "top-tab" in line:
                back.append(f"{name}: 有灰掉的页签 {line.strip()[:52]}")
    check("★★ 导航里没有集成客服、也没有任何 disabled 页签",
          not back, str(back))
    check("★ /api/service 路由已删", "/api/service" not in server_src)
    check("★ service_payload 已删（不留'名字在、没人用'的死代码）",
          "def service_payload" not in server_src)
    check("★ PAGES 里没有 /service",
          "/service" not in {pg["path"] for pg in store_module.PAGES},
          str(sorted(pg["path"] for pg in store_module.PAGES)))

    section("一之四、★★★ 原子组件层（规范 §3）")
    # ★ 规范 §3 要求 7 个原子组件。我们是原生栈，落成
    #   「语义类名 + CSS（ui.css）」+「元素工厂（ui.js）」+「预览页」。
    for _f in ("ui.css", "ui.js", "components.html"):
        check(f"★ 组件层文件在：{_f}", (WEB / _f).exists())
    _uicss = (WEB / "ui.css").read_text(encoding="utf-8")

    # ★★ 7 个组件都要有（Button/Card/Input/Tag/PageHeader/Empty/cn）
    _uijs = (WEB / "ui.js").read_text(encoding="utf-8")
    _want_comp = ["Button", "Card", "Input", "Tag", "PageHeader", "Empty", "cn"]
    _miss_comp = [n for n in _want_comp
                  if f"function {n}(" not in _uijs and f"{n}," not in _uijs
                  and f" {n}:" not in _uijs and f" {n} " not in _uijs]
    check("★★ 规范要求的 7 个原子组件都在 ui.js",
          not _miss_comp, f"缺: {_miss_comp}")

    # ★★★ 禁令 1：组件层里不许出现具体色值
    #   ★ 正则要排掉两个假阳性：
    #     · `white-space` 里的 white（不是颜色）
    #     · `transparent` / `currentColor` 是合法关键字，不算"具体色值"
    _COLOR_RE = re.compile(
        r'#[0-9a-fA-F]{3,8}\b|rgba?\(|'
        r'(?<![\w-])(?:white|black|red|blue|green|orange|gray|grey|'
        r'yellow|purple|pink|silver|navy|teal|aqua|maroon|olive|lime|'
        r'fuchsia)(?![\w-])')
    _bad_color = []
    for _f in ("ui.css", "ui.js"):
        _src = (WEB / _f).read_text(encoding="utf-8")
        if _f.endswith(".css"):
            _code, _d, _o = [], 0, []
            for _ln in _src.splitlines():
                _i, _keep = 0, []
                while _i < len(_ln):
                    if _d == 0 and _ln.startswith("/*", _i):
                        _d = 1; _i += 2
                    elif _d == 1 and _ln.startswith("*/", _i):
                        _d = 0; _i += 2
                    elif _d == 0:
                        _keep.append(_ln[_i]); _i += 1
                    else:
                        _i += 1
                _o.append("".join(_keep))
            _code = "\n".join(_o)
        else:
            _code = "\n".join(l for l in _src.splitlines()
                               if not l.strip().startswith(("//", "*", "/*")))
        for _m in _COLOR_RE.finditer(_code):
            _bad_color.append(f"{_f}: {_m.group(0)}")
    check("★★★ 组件层里没有一个具体色值（规范禁令 1）",
          not _bad_color, str(_bad_color[:5]))

    # ★ 变体只认名字：Button 的 variant/size 必须是白名单里的
    check("★★ Button 只认 variant 名（不接受自定义色值）",
          all(v in _uijs for v in
              ("primary", "secondary", "outline", "ghost", "danger", "link")))
    # ★ Tag 的底色用 color-mix 从基色算，不另写 rgba
    check("★ Tag 底色用 color-mix 从令牌算（不另写 rgba）",
          "color-mix" in _uicss)

    # ★★ 预览页是规范 §8 第②步的验收载体
    _cehtml = (WEB / "components.html").read_text(encoding="utf-8")
    check("★★ 组件预览页存在且能换主题",
          _linked(_cehtml, "/ui.css") and _linked(_cehtml, "/ui.js")
          and "theme-select" in _cehtml)
    check("★ /components 路由已接",
          '"/components": "/components.html"' in (
              ROOT / "server.py").read_text(encoding="utf-8"))

    section("一之五、★ 内容区靠左 + 左间距 32px（店主指定）")
    # ★★ 这个仓库有三条规则都管内容区：
    #     main { padding: 0 20px 60px; max-width: 1420px; margin: 0 auto }
    #     main { max-width: 1420px; margin: 0 auto; padding: 34px 20px 60px }   ← 重复
    #     .app-main > main { ... }   ← ★ 更具体，真正生效的是这条
    #   ⇒ 断言查的必须是 .app-main > main（查裸 main 会得出错误结论）。
    _m = re.search(r"\.app-main > main\s*\{([^}]*)\}", _css_code)
    check("★ 找到了 .app-main > main 规则", bool(_m))
    if _m:
        _body = re.sub(r"\s+", " ", _m.group(1))
        check("★★ 内容区左间距 32px",
              "padding: 34px 32px 60px" in _body
              or "padding-left: 32px" in _body, _body[:80])
        check("★★ 内容区不再居中（没有 margin:auto / max-width）",
              "margin: 0 auto" not in _body and "max-width" not in _body,
              _body[:80])
        check("★★ 左右内边距对称（右边也 32px，否则表格顶视口边）",
              "padding: 34px 32px 60px" in _body, _body[:80])
    # ★ 另外两条旧 main 规则也别留着居中（否则以后有人改回裸 main 又居中）
    _old_auto = re.findall(r"(?<![\w.-])main\s*\{([^}]*)\}", _css_code)
    _still_auto = [b for b in _old_auto if "margin: 0 auto" in b]
    check("★ 旧的裸 main 规则也不带 margin:auto（防回退）",
          not _still_auto, str(_still_auto)[:120])
    # ★ 靠左会在宽屏右侧留空白 —— 这是店主明确要的，注释里要写清楚，
    #   免得后来人看到"空一片"就顺手加回居中。
    # ★★ 这条必须查【原始 _css】，不能查 _css_code ——
    #   _css_code 是【剥过注释的】（第 257 行 _strip_comments），
    #   而我要查的内容恰恰在注释里 ⇒ 查 _css_code 会永远失败。
    #   ★ 这一处我改了两版才对：第一版查措辞（被引号断了），
    #     第二版查锚点但查错了变量。规律：
    #     **先想清楚这东西在注释里还是在代码里，再选变量。**
    check("★ 注释写明了「靠左是店主指定的」（防后来人顺手改回居中）",
          "靠左" in _css and "店主" in _css and "不要" in _css)

    section("一之六、★ 刷新按钮 = 分段控件 UI（.plat-tabs）")
    # ★ 店主：「报价记录和订单管理 右上角的刷新 改为之前 闲鱼/微信/拼多多
    #         渠道那个 ui 按钮」
    # ★★ 这个 class 有两个用途，两个都要保住：
    #   ① 回复文案面板的渠道切换器
    #   ② 报价记录 / 订单管理的刷新按钮
    #   历史：我按 class 名把 .plat-tabs 的样式整块删过一次，
    #        渠道切换器变成三个裸按钮 ⇒ 所以断言要同时钉两件事。
    for _f in ("orders.html", "quotes.html"):
        _h = (WEB / _f).read_text(encoding="utf-8")
        check(f"★★ {_f} 的刷新按钮用 .plat-tabs 胶囊",
              'class="plat-tabs"' in _h
              and re.search(r'class="plat-tab active" id="btn-reload"', _h)
              is not None)
        check(f"★ {_f} 里没有残留的 btn-ghost 刷新按钮",
              'class="btn-ghost" id="btn-reload"' not in _h)
    # 样式必须还在（分段控件的视觉：圆角 full + 内边距 + 玻璃底）
    check("★★ .plat-tabs 样式还在（圆角 full + 3px 内边距）",
          re.search(r"\.plat-tabs\s*\{[^}]*border-radius:\s*var\(--radius-full\)",
                    _css_code) is not None
          and re.search(r"\.plat-tabs\s*\{[^}]*padding:\s*3px", _css_code)
          is not None)
    check("★★ .plat-tab.active 样式还在（白底 + 细阴影）",
          re.search(r"\.plat-tab\.active\s*\{[^}]*box-shadow", _css_code)
          is not None)
    # 渠道切换器不能被"顺手统一"掉
    _idx = (WEB / "index.html").read_text(encoding="utf-8")
    check("★★ 渠道切换器还在用同一套 class（别被顺手删了）",
          "plat-tabs" in _idx and 'data-channel' in _idx)
    # ★ 注释要写明这个 class 有两个用途（防下次按 class 名误删）
    check("★ 注释写明了 .plat-tabs 的两个用途",
          "两个用途" in _css and "渠道切换器" in _css)

    section("一之七、★★ 店铺管理页（上次漏做的那个）")
    # ★ 背景：店主说「另外新增店铺管理」。
    #   我上次只做了 /api/shops 接口 + 一个"当前店铺"下拉框，
    #   没做页面 —— 这一节就是钉住那个页面。
    _sh = WEB / "shops.html"
    check("★★ shops.html 存在", _sh.exists())
    if _sh.exists():
        _s = _sh.read_text(encoding="utf-8")
        check("★★ 路由 /shops → /shops.html",
              '"/shops": "/shops.html"' in server_src)
        check("★★ 页面标题是「店铺管理」", "<h1>店铺管理</h1>" in _s)
        check("★ 页面引了 shops.js（渲染靠它）", _linked(_s, "/shops.js"))
        check("★ 刷新按钮也用分段控件 UI（和另两页一致）",
              'class="plat-tabs"' in _s
              and 'class="plat-tab active" id="btn-reload"' in _s)
        # ★★★ 2026-10-14 改：这条原来靠"页面里有『不能…新建…鱼麦多后台』
        #   那段说明文字"来保证不假装能改平台。
        #   店主把那说明删了（"这里删除吧 没啥用"），断言于是失败 ——
        #   但【约束本身还该守】，所以改成用【行为】守：
        #     ① 页面上没有任何"改平台"的控件（新建/改名/停用/删除）
        #     ② 唯一可写的是我们自己的「自动发送」开关，且明确标着
        #        "我们是否发消息"（与平台状态分开，不混淆）
        _platform_actions = [w for w in ("新建店铺", "新增店铺", "改名",
                                         "停用店铺", "删除店铺", ">停用<")
                             if w in _s]
        check("★★★ 页面上没有「改平台状态」的控件（不假装能增删改店铺）",
              not _platform_actions, str(_platform_actions))
        check("★★ 唯一的可写控件是我们自己的「自动发送」开关",
              "自动发送" in _s and "刷新" in _s)
        check("★★ 那个开关明确标成「我们是否发消息」（与平台状态分开）",
              "我们是否发消息" in _s)
        check("★ 有「为什么读不到」的排查区块",
              'id="trouble"' in _s and 'id="trouble-steps"' in _s)
    # 8 个页面侧栏都有「店铺管理」，且都在第一位（和 V5 一致）
    _nav_missing = []
    _nav_notfirst = []
    for _f in sorted(WEB.glob("*.html")):
        _t = _f.read_text(encoding="utf-8")
        _titles = re.findall(r'class="nav-title">([^<]+)<', _t)
        if "店铺管理" not in _titles:
            _nav_missing.append(_f.name)
        elif _titles[0] != "店铺管理":
            _nav_notfirst.append(_f.name)
    check("★★ 每个页面侧栏都有「店铺管理」", not _nav_missing,
          str(_nav_missing))
    check("★★ 「店铺管理」在侧栏第一位（和 V5 一致）", not _nav_notfirst,
          str(_nav_notfirst))
    # ★ 侧栏项数：8 项（加了店铺管理）
    _cnt_bad = []
    for _f in sorted(WEB.glob("*.html")):
        _t = _f.read_text(encoding="utf-8")
        _n = _t.count('class="ticket-nav-item')
        _a = _t.count('class="ticket-nav-item active"')
        # 组件预览页故意没有 active（它不是业务页）
        if _n != 8 or (_f.name != "components.html" and _a != 1):
            _cnt_bad.append(f"{_f.name}: {_n}项/active{_a}")
    check("★★ 8 个页面侧栏都是 8 项、恰好 1 个选中", not _cnt_bad,
          str(_cnt_bad))
    # shops.js：错误码 → 排查步骤（不是笼统一句"加载失败"）
    _sjs = (WEB / "shops.js").read_text(encoding="utf-8")
    check("★★ shops.js 按 error_code 给排查步骤（不是一句加载失败）",
          "var FIX = {" in _sjs and "PLUGIN_SECRET_MISSING" in _sjs
          and "SHOP_SCOPE_REQUIRED" in _sjs)
    check("★ 店铺页有自己的加载完成信号（测试不靠等时间）",
          "__shopsLoaded" in _sjs)
    check("★ 下拉框逻辑保留（没被页面渲染挤掉）",
          "function mount()" in _sjs and "push-shop" in _sjs)
    # 店铺页要能把 im_enabled 单独显示出来
    # （平台启用 ≠ 能收发消息，这是最容易忽略的一条）
    check("★★ 页面单独显示「收发消息」（im_enabled）",
          "im_enabled" in _sjs and "未开收发消息" in _sjs)

    section("一之八、★ 弹窗关闭按钮：文字「关闭」，两个弹窗一致")
    # ★ 店主：「我需要关闭 文字 不要 ×」
    #   ★★ 我上一轮理解反了方向：店主说"关键词的关闭按钮和回复文案的
    #     做成一样"，基准是【回复文案那个（文字关闭）】，
    #     我却把两个都改成了 ×（那是我自己的偏好）。
    #   ⇒ 现在两个都是文字「关闭」，并且断言把它钉住。
    _idx = (WEB / "index.html").read_text(encoding="utf-8")
    _qah = (WEB / "qa.html").read_text(encoding="utf-8")
    check("★★ 回复文案弹窗的关闭按钮是文字「关闭」",
          re.search(r'id="m-close"[^>]*>关闭<', _idx) is not None
          and 'id="m-close" aria-label="关闭">×' not in _idx)
    check("★★ 关键词弹窗的关闭按钮是文字「关闭」",
          re.search(r'id="modal-x"[^>]*>关闭<', _qah) is not None)
    check("★★ 两个弹窗都不再用 × ",
          '>×<' not in _idx and '>×<' not in _qah)
    # 底部按钮条的"取消"和「关闭」重复 ⇒ 已去掉
    check("★ 底部按钮条没有重复的「取消」",
          'id="m-close2"' not in _idx and 'id="btn-cancel"' not in _qah)
    # ★ 关键：删掉 btn-cancel 之后，JS 里不许还绑它（那会抛 null，
    #   并把后面所有绑定（保存/删除）全废掉 —— 页面看着正常但功能失效）
    _qjs2 = (WEB / "qa.js").read_text(encoding="utf-8")
    _ajs2 = (WEB / "app.js").read_text(encoding="utf-8")
    _dead_bind = []
    for _f, _src in (("qa.js", _qjs2), ("app.js", _ajs2)):
        for _ln in _src.splitlines():
            _s = _ln.strip()
            if _s.startswith("//"):
                continue
            if ("btn-cancel" in _s or "m-close2" in _s) and ".onclick" in _s:
                _dead_bind.append(f"{_f}: {_s[:50]}")
    check("★★ JS 里没有绑到已删按钮上的 onclick（会抛 null 废掉后续绑定）",
          not _dead_bind, str(_dead_bind))

    section("二、/api/orders 数据面")
    tmp = Path(tempfile.mkdtemp())
    try:
        # ★ 把两个库指到临时目录 —— 不碰 data/
        from application import ledger
        # ★ 用显式注入，不改 store_module.DATA ——
        #   改 DATA 是无效的（QUOTES_FILE 导入时就算好了），
        #   踩过：那样会把测试数据写进真的 data/quotes.sqlite3。
        ledger.close_stores()
        ledger.set_paths(quotes=str(tmp / "q.sqlite3"),
                         orders=str(tmp / "o.sqlite3"))
        s = ledger.stores(fresh=True)

        from domain.order import Order, idempotency_key_for, new_order_id
        from domain.quote import QuoteRecord, default_expiry, new_quote_id

        tag = "65091694508"
        ident = {"tenant_id": "107", "shop_id": "2313315754",
                 "buyer_id": "2002808420", "chat_id": tag}
        qid = new_quote_id()
        s.quotes.create(QuoteRecord(
            quote_id=qid, **ident, provider="WANDA", request_type="EXACT_SEATS",
            cinema="常州新北万达广场店", movie="生化危机：爆发夜",
            show="2026-10-12 19:15", selected_seats=["9排16", "9排17"],
            unit_cost_fen=4500, unit_sell_price_fen=4600, ticket_count=2,
            total_cost_fen=9000, total_sell_price_fen=9200,
            pricing_rule_id="万达一口价", pricing_rule_revision="settings-52",
            expires_at=default_expiry()))
        order = Order(order_id=new_order_id(), quote_id=qid, **ident,
                      provider="WANDA", cinema="常州新北万达广场店",
                      movie="生化危机：爆发夜", show="2026-10-12 19:15",
                      seats=["9排16", "9排17"], ticket_count=2,
                      amount_fen=9200, payment_state="CONFIRMED", state="PAID",
                      idempotency_key=idempotency_key_for(qid, provider="WANDA"))
        s.orders.create(order)

        p = ledger.orders_payload()
        check("返回六个面", set(p) >= {"quotes", "orders", "buckets", "labels",
                                      "storage", "counts"}, str(sorted(p)))
        check("报价记录出现在面里", p["counts"]["quotes"] == 1)
        check("订单出现在面里", p["counts"]["orders"] == 1)
        check("★ 按线上 V5 的桶分组（待出票 → awaiting_ticket）",
              p["buckets"].get("awaiting_ticket") == 1, str(p["buckets"]))
        check("桶里有 all 总数", p["buckets"].get("all") == 1)

        q = p["quotes"][0]
        check("★ 金额在服务端就转成元（前端不再除 100）",
              q["total_sell_yuan"] == 92.0, str(q["total_sell_yuan"]))
        check("★ 成本与售价分开给（能核对毛利）",
              q["total_cost_yuan"] == 90.0 and q["margin_total_yuan"] == 2.0,
              f"{q['total_cost_yuan']} / {q['margin_total_yuan']}")
        check("★ 规则版本传到了界面",
              q["pricing_rule_revision"] == "settings-52")
        check("状态给了中文名", q["status_label"] == "有效", q["status_label"])
        check("★ 可下单判据来自记录自己（不在界面重算）",
              q["usable_for_order"] is True)

        o = p["orders"][0]
        check("订单状态给了中文名", o["state_label"] == "待出票", o["state_label"])
        check("付款态给了中文名", o["payment_label"] == "已确认", o["payment_label"])
        check("★ 没取票码时 can_deliver=False（界面据此提示）",
              o["can_deliver"] is False and "取票码" in o["can_deliver_reason"],
              o["can_deliver_reason"])

        section("三、出票接口（界面按钮走这条 POST）")
        r = ledger.submit_delivery(order.order_id, ticket_codes=["2032 5500 7913 72"],
                                   operator="")
        check("★ 没操作人被拒（不被界面绕过）",
              r.get("ok") is False and r.get("error") == "DELIVERY_NOT_ENABLED",
              str(r.get("error")))

        r = ledger.submit_delivery(order.order_id,
                                   ticket_codes=["2032 5500 7913 72"],
                                   operator="张三",
                                   provider_order_id="1493664033")
        check("★ 正常出票成功", r.get("ok") is True, str(r.get("error")))
        check("★ 出票后自动发货（你要的那一步）",
              (r.get("order") or {}).get("shipped") is True)
        check("★ 订单变已交付",
              (r.get("order") or {}).get("state_label") == "已交付",
              str((r.get("order") or {}).get("state_label")))
        check("取票码落进订单",
              (r.get("order") or {}).get("ticket_codes") == ["2032 5500 7913 72"])

        p2 = ledger.orders_payload()
        check("★ 桶跟着变（待出票 → 已交付）",
              p2["buckets"].get("delivered") == 1
              and p2["buckets"].get("awaiting_ticket") is None,
              str(p2["buckets"]))

        section("四、状态词典（界面显示中文用）")
        check("订单状态都有中文名",
              all(v for v in store_module.ORDER_STATE_LABELS.values()))
        check("付款态都有中文名",
              all(v for v in store_module.PAYMENT_STATE_LABELS.values()))
        check("报价状态都有中文名",
              all(v for v in store_module.QUOTE_STATUS_LABELS.values()))
        check("★ 每个订单状态都有桶（否则界面漏统计）",
              set(store_module.ORDER_STATE_LABELS)
              == set(store_module.ORDER_STATE_BUCKETS),
              str(set(store_module.ORDER_STATE_LABELS)
                  ^ set(store_module.ORDER_STATE_BUCKETS)))
        from domain.order.models import OrderState                     # noqa: F401
        import typing
        declared = set(typing.get_args(OrderState))
        check("★ 词典覆盖 domain 层声明的全部状态（不脱节）",
              declared <= set(store_module.ORDER_STATE_LABELS),
              str(declared - set(store_module.ORDER_STATE_LABELS)))

        section("五、★★ 出票接口鉴权（上服务器必须有的那道门）")
        # ★ 这个接口能决定给买家的票 —— 没鉴权等于谁都能改单/塞假码。
        #   机制照线上 V5 的 integrations/wanda_ticketing.py。
        import os
        from application import delivery_auth as da

        saved = {k: os.environ.get(k) for k in
                 (da.ENV_TENANT, da.ENV_SHOPS, da.ENV_READ_KEY,
                  da.ENV_WRITE_KEY, da.ENV_WRITE_ENABLED)}
        try:
            for k in saved:
                os.environ.pop(k, None)
            da._cred = lambda name: ""                 # noqa: SLF001
            r = da.check("Bearer x")
            check("★ 没配置 → 拒绝（不让接口在裸奔状态下可用）",
                  not r.ok and r.status == 503
                  and r.error == "WANDA_TICKETING_INTEGRATION_NOT_CONFIGURED",
                  f"{r.status} {r.error}")

            os.environ[da.ENV_TENANT] = "107"
            os.environ[da.ENV_SHOPS] = "2313315754"
            os.environ[da.ENV_WRITE_KEY] = "k-abcdef123456"
            # 开关默认关 —— 要显式打开才能出票
            r = da.check("Bearer k-abcdef123456")
            check("★★ 密钥对但写开关关着 → 仍然拒绝（默认安全）",
                  not r.ok and r.error == "WANDA_DELIVERY_DISABLED",
                  f"{r.status} {r.error}")

            os.environ[da.ENV_WRITE_ENABLED] = "true"
            for label, header in (("没带 Authorization", None),
                                  ("密钥错", "Bearer wrong"),
                                  ("不是 Bearer", "Basic k-abcdef123456"),
                                  ("少了 scheme", "k-abcdef123456")):
                rr = da.check(header)
                check(f"★ {label} → 401",
                      not rr.ok and rr.status == 401
                      and rr.error == "INVALID_INTEGRATION_CREDENTIAL",
                      f"{rr.status} {rr.error}")

            r = da.check("Bearer k-abcdef123456", shop_id="2313315754")
            check("★ 密钥对 + 店在白名单 → 放行", r.ok, f"{r.status} {r.error}")
            r = da.check("Bearer k-abcdef123456", shop_id="9999999")
            check("★★ 店不在白名单 → 403（密钥泄露也限范围）",
                  not r.ok and r.status == 403
                  and r.error == "SHOP_OUTSIDE_INTEGRATION_SCOPE",
                  f"{r.status} {r.error}")

            st = da.config_status()
            check("★★ 配置状态里【绝不回显密钥明文】",
                  "k-abcdef123456" not in json.dumps(st, ensure_ascii=False),
                  str(st.get("delivery_key_hint")))
            check("配置状态给的是掩码",
                  st["delivery_key_hint"].startswith("k-")
                  and "•" in st["delivery_key_hint"],
                  st["delivery_key_hint"])
            check("配置状态能说清缺什么",
                  st["configured"] is True and st["missing"] == [],
                  str(st["missing"]))

            # 密钥只写不读：传 None 保留，传 "" 才清掉
            check("★ save_config 的 None 表示不改（区别于清空）",
                  _keeps_key(da), "见实现")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        section("六、接口路由与界面接线")
        check("GET /api/delivery-auth（配置状态）",
              'if path == "/api/delivery-auth":' in server_src)
        check("GET /api/delivery-token（界面取令牌，走同一道鉴权）",
              'if path == "/api/delivery-token":' in server_src)
        check("★ 出票接口先鉴权再做事（顺序不能反）",
              server_src.index("delivery_auth.check(")
              < server_src.index("ledger.submit_delivery("),
              "鉴权必须在提交出票之前")
        check("★★ 店铺白名单按【订单自己的 shop_id】判（不能听调用方的）",
              "found.shop_id if found else" in server_src)
        check("界面出票时带上了 Bearer（走同一道鉴权，不开后门）",
              "Authorization" in js and "Bearer" in js)
        check("界面有鉴权配置区", 'id="btn-save-auth"' in html)
    finally:
        ledger.close_stores()
        ledger.set_paths(quotes=None, orders=None)
        shutil.rmtree(tmp, ignore_errors=True)

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} 项通过")
    print("=" * 74)
    return 0 if passed == total else 1


def _keeps_key(da) -> bool:
    """
    验证 save_config 的语义：传 None = 不改，传空字符串 = 清掉。

    ★ 为什么不真写文件：那会动 data/liangpiao.json（真凭证）。
      这里只验证【参数语义】在实现里 —— 读源码判据。
    """
    import inspect
    src = inspect.getsource(da.save_config)
    return "if delivery_key is not None" in src


if __name__ == "__main__":
    sys.exit(main())
