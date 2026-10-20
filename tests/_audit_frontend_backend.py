"""
审计：前端与后端的接口是否对得上（双向）。

· 查两个方向：
  ① 前端调了、但后端没有这个路由  → 界面点了会 404
  ② 后端有、但前端从来没调        → "写好了没接上"（功能等于没有）

· 为什么要机器查：这两种错我都犯过 ——
  · PUT /api/credentials 后端有、前端一次没调（手机号界面上根本没法填）
  · 我自己以为 /api/service 没了，其实只是在文本里数错了次数
  靠眼睛数会错，靠机器数不会。
"""
import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ★★ 用【脚本自身位置】定位项目根，不用相对路径。
#
#   踩过：这里原来写 Path("web") / Path("server.py") ——
#   从项目根跑没问题，但 run_checks 是从【上级目录】跑的
#   （python ticket-agent\tests\...），于是报
#   FileNotFoundError: 'server.py'，体检里这套永远失败。
#   ⇒ 所有路径都从 __file__ 推。
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SERVER_PY = ROOT / "server.py"


def routes_of(text: str) -> set:
    """从 server.py 里取出所有 `path == "/api/xxx"` 的路由。"""
    out = set()
    for m in re.finditer(r'path\s*==\s*"([^"]+)"', text):
        out.add(m.group(1))
    # · 前缀路由：path.startswith("/api/preview/")
    for m in re.finditer(r'path\.startswith\(\s*"([^"]+)"', text):
        out.add(m.group(1))
    # · 正则路由：re.match(r"^/api/v1/commands/([^/]+)/result$")
    #   去掉锚点与捕获组，还原成可前缀匹配的形状
    for m in re.finditer(r're\.match\(\s*r?"([^"]+)"', text):
        raw = m.group(1)
        raw = raw.lstrip("^").rstrip("$")
        raw = re.sub(r"\([^)]*\)", "", raw)
        raw = re.sub(r"\\[a-z]+", "", raw)
        out.add(raw)
    return out


def frontend_calls() -> dict:
    """前端调用了哪些 /api/*。返回 {path: [文件:行]}。"""
    calls = {}
    for f in sorted(WEB.glob("*.js")) + sorted(WEB.glob("*.html")):
        text = f.read_text(encoding="utf-8")
        # ★★★ 2026-10-14 修：先剥掉注释再扫。
        #   我加了段说明注释，里面写 `fetch('/api/...')` 当例子 ⇒
        #   审计把它当成"前端调了不存在的端点 /api/..."。
        #   ★ 这类"断言扫到自己写的解释文字"我踩过好几次了 ——
        #     判据应该是【代码】，不是解释代码的文字。
        text = re.sub(r"/\*[\s\S]*?\*/", "", text)      # 块注释
        for i, ln in enumerate(text.splitlines(), 1):
            code = re.sub(r"//.*$", "", ln)             # 行注释
            for m in re.finditer(r"""['"`](/api/[^'"`?\s]*)""", code):
                p = m.group(1).rstrip("/")
                calls.setdefault(p, []).append(f"{f.name}:{i}")
    return calls


def main() -> int:
    srv = SERVER_PY.read_text(encoding="utf-8")
    routes = routes_of(srv)
    calls = frontend_calls()
    bad = []

    print("=" * 78)
    print("① 前端调了、后端没有（点了会 404）")
    print("=" * 78)
    missing = []
    for p, where in sorted(calls.items()):
        # · 双向前缀匹配：前端的 /api/preview 对应路由 /api/preview/，
        #   也对应 /api/preview/xxx。踩过：只单向匹配会误报。
        hit = any(
            p == r
            or p.startswith(r.rstrip("/") + "/")
            or r.rstrip("/").startswith(p.rstrip("/") + "/")
            or r.rstrip("/") == p.rstrip("/")
            for r in routes)
        if not hit:
            missing.append((p, where))
            print(f"  ★ {p}  ← {', '.join(where[:3])}")
    if not missing:
        print("  ✓ 没有")
    bad += missing

    print()
    print("=" * 78)
    print("② 后端有、前端从来没调（写好了没接上）")
    print("=" * 78)
    unused = []
    # · 这些是【给插件/外部】用的，不是给前端用的，不算"没接上"
    # · 这些是【给插件/外部/运维】的，本来就不该有前端调用 ——
    #   列出来是为了这是有意的，不是忘了接。
    EXTERNAL = (
        "/api/v1/",              # 插件端点（HMAC 签名）
        "/api/order/deliver",    # 出票后台
        "/api/delivery-token",   # 界面出票按钮取令牌
        "/api/callback",         # 良票回调
        "/api/message",          # 买家消息入口（插件/外部系统）
        "/api/refund",           # 退票（运维/内部）
        "/api/ping",             # 连通性自检（运维用）
        "/api/preview/",         # 回复文案页的预览（前端用，前缀路由）
        # ★★ 2026-10-14 新加：/quotes 现在是【重定向路由】（302 → /orders）。
        #   报价记录并进了订单管理页的第一个 tab（店主要求，照 V5），
        #   独立页没了，但保留重定向让旧链接不 404。
        #   ⇒ 它是"给浏览器跳转用"的，不是前端 fetch 的接口，
        #     所以"前端从来没调"对它不适用。
        "/quotes",
    )
    for r in sorted(routes):
        if any(r.startswith(e) for e in EXTERNAL):
            continue
        if r in calls:
            continue
        unused.append(r)
        print(f"  ★ {r}")
    if not unused:
        print("  ✓ 没有")
    bad += unused

    print()
    print("=" * 78)
    print("③ 用【正确的方法】实测每个后端路由")
    print("=" * 78)
    # · 关键：要用【路由自己声明的方法】去探。
    #   踩过两次：用 GET 去探 PUT/POST-only 的端点，得到 404，
    #   误判成路由不存在（其实它只是不接受 GET）。
    #
    # ·· 服务没在跑时【跳过】这一段，不算失败 ——
    #   体检不该依赖"服务已经起着"（run_checks 会在服务没起时跑）。
    #   但"没验"必须说出来，不能让跳过看起来像通过。
    import urllib.error
    import urllib.request
    BASE = "http://127.0.0.1:8801"
    alive = True
    try:
        urllib.request.urlopen(BASE + "/api/rules", timeout=3).read()
    except Exception:                                     # noqa: BLE001
        alive = False
    if not alive:
        print("  · 服务没在跑（127.0.0.1:8801）→ 跳过实测")
        print("    · 这段只在服务起着时才验；要完整验请先起服务")
        print()
        print("=" * 78)
        print(f"合计问题：{len(bad)} 处（静态检查）")
        print("=" * 78)
        return 0 if not bad else 1

    # · 按 do_XXX 分段，拿到"路由 → 它属于哪个方法"。
    #   不能靠"哪个字符串出现在哪一段"猜 —— 我踩过两次。
    segs, cur = {}, None
    for ln in srv.splitlines():
        s = ln.strip()
        if s.startswith("def do_"):
            cur = s.split("(")[0][3:]
            segs.setdefault(cur, [])
        elif cur is not None:
            segs[cur].append(ln)
    routes_with_method = []
    for method, lines in segs.items():
        body = "\n".join(lines)
        for m in re.finditer(r'path\s*==\s*"([^"]+)"', body):
            routes_with_method.append((method, m.group(1)))
    missing_route = []
    for method, r in sorted(set(routes_with_method)):
        if "{" in r or "(" in r or r.endswith("x"):  # 前缀/正则路由不逐字探
            continue                  # 正则路由要真实 id，不探
        url = BASE + r
        data = b"{}" if method in ("POST", "PUT") else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                st = resp.status
        except urllib.error.HTTPError as e:
            st = e.code
        except Exception:                                 # noqa: BLE001
            st = -1
        ok = st != 404
        if not ok:
            missing_route.append(f"{method} {r}")
        print(f"  {'✓' if ok else '★ 404'} [{method}] {r:<26} 实测={st}")
    bad += missing_route

    print()
    print("=" * 78)
    print(f"合计问题：{len(bad)} 处")
    print("=" * 78)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
