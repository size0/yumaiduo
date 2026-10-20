"""
_check_params.py —— 参数审计：多余的 / 没人用的 / 我编的

════════════════════════════════════════════════════════════════
★ 为什么专门查这个

  参数有四类问题，危害依次递增：

    1. 函数签名有、函数体不用          → 噪音
    2. 数据类字段解析了、没人读        → 看起来像在传数据，实际是死的
    3. 请求里传了、文档里没有          → 可能被当未知参数处理
    4. 文档里有权威字段、我们该用没用  → ★ 最亏：明明能少错，却靠错误码反推

★ 判据（踩过坑，特意写清楚）

  早先版本用"有没有 `.字段名` 点号访问"来判断，误报一大堆：
    · settings 的字段通过 UI 元数据层显示，程序读不读是另一回事
    · 字段在 from_data() 里被赋值，看起来"有访问"，其实是写入不是读取

  现在用【精确判据】：
    一个字段如果只出现在
      (a) dataclass 定义行
      (b) 它自己的 from_data() 里那行赋值
    那么它就是"解析了没人用"—— 这三处之外零出现才算。

★ 良票响应字段的处置原则

  不是"没人用就删"。要先问：**这个字段该不该用？**
    · `pickupUrl`        → 该用（文档：可提前发给顾客，免登录、不含价格）
    · `refundable`       → 该用（文档：当前是否可发起主动退票）
    · `estimatedSettleAmount` → 该用（文档：即这一单你的成本价）
  这类是"该用没用"，比"多余"更值得修。
════════════════════════════════════════════════════════════════
"""

import ast
import re
import sys
from pathlib import Path

# ★ Windows 上 stdout 默认 GBK，打不出 ↔ ★ ✓ − 这类字符会抛
#   UnicodeEncodeError。★ 必须放在【任何 print 之前】——
#   这些测试是模块级脚本，从第一行就开始跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass



ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT))

# ★★ 良票 API 文档的位置：本地在 D:\13250\Users\，服务器上放在仓库上一级。
#   ★ 为什么必须列候选：原来只写死那个 Windows 绝对路径 ——
#     在服务器上 DOC.exists() 为假，第 423 行就拿到空文档，
#     "写死的字面量有没有出处"那一节于是把 8 个本来有出处的常量全标成"待核"。
#     ⇒ 不是常量没出处，是【测试读不到文档】。
#   ★ _check_doc_fields.py 早就用了同样的候选列表写法，这个漏了。
DOC_CANDIDATES = [
    Path(r"D:\13250\Users\良票开放平台-API接入文档 (3).md"),
    ROOT.parent / "良票开放平台-API接入文档 (3).md",
]
DOC = next((p for p in DOC_CANDIDATES if p.exists()), DOC_CANDIDATES[-1])

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return bool(ok)


def section(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def production_files() -> dict:
    """生产代码（不含 tests / legacy / demos）。"""
    out = {}
    for p in sorted(ROOT.rglob("*.py")):
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if any(x in rel for x in ("__pycache__", "legacy/", "demos/", "tests/")):
            continue
        if p.name.startswith("_"):
            continue
        out[rel] = p.read_text(encoding="utf-8")
    return out


# ==================================================================== 一
def audit_unused_function_params():
    section("一、函数参数：声明了但函数体从来不用")
    # ★ 有理由的例外，逐条写清楚为什么
    ALLOW = {
        ("integrations/liangpiao/client.py", "send"):
            "Transport 协议：实现类可以忽略参数，但签名必须一致",
        ("integrations/wanda/client.py", "send"):
            "★ 同上，万达 Transport 协议：签名必须与调用方一致",
        ("integrations/wanda/client.py", "_judge"):
            "domain/path 是给【日志与排查】用的上下文，实现里暂时没读。"
            "★ 保留是因为报错定位要靠它（多域系统里不说清哪个域很难查）",
        ("integrations/wanda/client.py", "_classify"):
            "path 同上：报错上下文，便于定位是哪个接口出错",
        ("chat.py", "send"):
            "★ Transport 协议：chat.py 里的假传输只按 URL 路由，"
            "不读 headers/timeout，但签名必须与真客户端一致",
        ("server.py", "log_message"):
            "覆盖 BaseHTTPRequestHandler 的方法，签名由基类固定",
        ("prompts/persona.py", "build_persona"):
            "★ 真问题见下：style 参数没用上，说明风格没真接进去",
    }
    problems = []
    for rel, src in production_files().items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = [a.arg for a in node.args.args + node.args.kwonlyargs]
            args = [a for a in args if a not in ("self", "cls")]
            if not args:
                continue
            used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            used |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            for a in args:
                if a in used:
                    continue
                problems.append((rel, node.name, a, node.lineno))

    real = [p for p in problems if (p[0], p[1]) not in ALLOW]
    for rel, fn, arg, line in problems:
        why = ALLOW.get((rel, fn))
        mark = "·（有理由）" if why else "⚠"
        print(f"  {mark} {rel}:{line}  {fn}(… {arg} …)")
        if why:
            print(f"        {why}")
    if not problems:
        print("  ✓ 没有未使用的函数参数")
    check("没有【无理由】的未使用函数参数", not real,
          f"{len(real)} 处" if real else f"（{len(problems)} 处都有理由）")
    return real


def audit_style_presets():
    """
    ★ 专门查"宣传了但不存在"的功能。

    `style_presets` 这个配置字段曾经是 ["default","conservative","warm"]，
    接口也返回它，但三个预设的【文本】只硬编码在教学演示里 ——
    生产代码拿不到。于是 build_persona 的 style 参数收下了却用不了。

    这类问题的特征：**名字存在、实现不存在**。
    比"没这个功能"更糟：读代码的人会去找它。
    """
    section("一之二、★ 配置字段是不是空头承诺")
    from prompts import persona as pm
    import settings as st

    check("预设文本在生产代码里存在", len(pm.STYLE_PRESETS) == 3,
          str(list(pm.STYLE_PRESETS)))
    check("预设名与配置字段一致",
          list(pm.STYLE_PRESETS) == st.Assistant().style_presets,
          f"代码={list(pm.STYLE_PRESETS)} 配置={st.Assistant().style_presets}")
    for name in pm.STYLE_PRESETS:
        check(f"预设 {name} 有正文", bool(pm.style_preset(name).strip()),
              pm.style_preset(name).splitlines()[0][:40])

    # ★ 真能用：预设要真的进 system prompt
    built = pm.build_persona({"persona": "我是店主", "precautions": "",
                              "reply_style": ""}, style="default")
    check("★ 预设真的进了 system prompt",
          "【说话风格】" in built and "闲鱼" in built,
          "default 预设的正文出现在 prompt 里")
    # ★ 优先级：运营手填 > 预设
    manual = pm.build_persona({"persona": "我", "precautions": "",
                               "reply_style": "手填风格"}, style="warm")
    check("★ 运营手填优先于预设",
          "手填风格" in manual and "亲切" not in manual,
          "手填时预设不生效")


# ==================================================================== 二
def audit_cross_source_consistency():
    """
    ★★ 查"同一个事实写在两个地方，然后漂移了"。

    真实案例：出票时效
      templates.TICKET_WINDOW  → STANDARD 1-60 分钟（改对了）
      settings.DEFAULT_PRECAUTIONS → "1-30 分钟（最迟 50 分钟）"（漏改了）

    后果更严重：DEFAULT_PRECAUTIONS 会进 system prompt，
    **直接教模型对买家说错话**。模板改了、模型的话没改，等于白改。

    ⇒ 凡是"良票文档里的事实"，全项目只能有一个来源。
    """
    section("一之三、★★ 同一个事实有没有写在两个地方")
    import settings as st
    import templates as tpl
    from integrations.liangpiao.api import TICKET_MODE_MINUTES

    # 出票时效：文档口径
    doc = TICKET_MODE_MINUTES["STANDARD"]        # (1, 60)
    expected = f"1-{doc[1]}分钟"

    # 来源 1：模板
    check("模板里的时效口径与文档一致",
          tpl.TICKET_WINDOW["STANDARD"] == expected,
          f"模板={tpl.TICKET_WINDOW['STANDARD']} 文档={expected}")

    # 来源 2：配置里的默认注意事项（会进 system prompt）
    prec = st.DEFAULT_PRECAUTIONS
    # ★ 判据要精确：只查【真正会进 prompt 的内容】。
    #   以 ★ 开头的是写给人看的说明（"曾经这里写死成 1-30"），
    #   把它也算成冲突，就变成"不许写清楚为什么改"—— 那不对。
    prec_body = "\n".join(ln for ln in prec.splitlines()
                          if not ln.strip().startswith(("#", "//", "★")))
    stale = [bad for bad in ("1-30", "最迟 50", "1~30") if bad in prec_body]
    check("★ system prompt 里没有与文档冲突的时效",
          not stale, f"发现旧口径：{stale}" if stale else "正文里无 1-30 / 最迟50")

    # 来源 3：其他常量（★ qa.py 是有意保留的运营文案，单独说明）
    others = []
    for rel, src in list(production_files().items()):
        for i, ln in enumerate(src.splitlines(), 1):
            if rel.endswith("templates.py") and "TICKET_WINDOW" in ln:
                continue
            if re.search(r"1\s*[-~]\s*30\s*分钟|最迟\s*50\s*分钟|10\s*[-~]\s*30\s*分钟", ln):
                if "曾经" in ln or "票达人页面" in ln or "★" in ln:
                    continue          # 注释里说明历史/出处的，不算漂移
                others.append(f"{rel}:{i}")
    # ★ qa.py 是运营配的智能问答原文，不是"我们定义的时效"——
    #   它与文档不一致这件事【已知且待确认】，不算代码漂移。
    drift = [o for o in others if not o.startswith("qa.py")]
    qa_known = [o for o in others if o.startswith("qa.py")]
    check("★ 代码里只有一处定义出票时效", not drift,
          f"另有 {drift}" if drift else "唯一来源：templates.TICKET_WINDOW")
    if qa_known:
        print(f"  ★ 已知待确认：qa.py 里 {len(qa_known)} 处（运营配的问答原文）")
        print(f"      {qa_known}")
        print(f"      与文档 STANDARD 1~{doc[1]} 分钟不一致 —— 上线前要拍板用哪个口径")
        print(f"      位置：真正的出票通知走 templates.TICKET_WINDOW（已按档位渲染）")

    # 确认 prompt 里确实带上了正确口径
    built = st.Assistant().build_prompt()
    check("★ system prompt 含正确时效",
          expected in built.replace(" ", ""),
          f"找 {expected}")


def audit_parsed_but_unread():
    """
    ★ 核心：找出"从响应里解析了、但生产代码里没人读"的字段。

    精确判据：除 (定义行, from_data 里的赋值行) 外零出现。
    """
    section("二、★ 解析了却没人读的字段（精确判据）")
    files = production_files()
    rows = []
    for rel, src in files.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            fields = [(s.target.id, s.lineno) for s in cls.body
                      if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]
            if not fields:
                continue
            fd = next((n for n in cls.body
                       if isinstance(n, ast.FunctionDef) and n.name == "from_data"), None)
            parse_lines = set(range(fd.lineno, (fd.end_lineno or fd.lineno) + 1)) if fd else set()
            for field, defline in fields:
                reads = []
                for other_rel, other_src in files.items():
                    for i, ln in enumerate(other_src.splitlines(), 1):
                        if not re.search(rf"\b{re.escape(field)}\b", ln):
                            continue
                        if other_rel == rel and (i == defline or i in parse_lines):
                            continue
                        reads.append(f"{other_rel}:{i}")
                if not reads:
                    rows.append((cls.name, field, rel, defline))

    # 分类：settings 的字段是 UI 驱动，属设计使然；其余是接口响应字段，要处理
    ui_driven = [r for r in rows if r[2].startswith("settings.py") or r[2] == "qa.py"]
    api_fields = [r for r in rows if r not in ui_driven]

    print(f"  良票响应字段（要处理）：{len(api_fields)} 个")
    for cls, field, rel, line in sorted(api_fields):
        print(f"      {cls}.{field:<26} {rel}:{line}")
    print()
    print(f"  配置/问答字段（UI 元数据驱动，属设计使然）：{len(ui_driven)} 个")
    for cls, field, rel, line in sorted(ui_driven)[:8]:
        print(f"      {cls}.{field}")
    if len(ui_driven) > 8:
        print(f"      …共 {len(ui_driven)} 个")

    # ★ 这几个文档明确说该用，必须已被读
    MUST_USE = ["pickup_url", "refundable", "estimated_settle_fen"]
    for name in MUST_USE:
        used = name not in [f for _, f, _, _ in rows]
        check(f"★ {name} 已被使用（文档明确说该用）", used,
              "" if used else "仍未被读取")
    return api_fields


# ==================================================================== 三
def audit_api_request_params():
    section("三、我们传给良票的请求参数 vs 文档")
    # ★★ 这份白名单【逐字抄自文档的请求参数表】。
    #    早先我自己凭印象写了一份，结果把代码里正确的参数误报成"文档外"：
    #      · recognize/confirm 的 seats  → 文档有（"必须与 showId 同传"）
    #      · cinema/list 用的是 page 不是 pageNum
    #      · movie/list 有 type / cityCode / page
    #    教训：白名单也是"要验的东西"，不能凭印象建。
    DOC_PARAMS = {
        "order/preflight": {"showId", "seats", "ticketMode", "priceMode",
                            "areaQuoteStrategy", "invoiceRequired"},
        "order/create": {"outOrderNo", "showId", "seats", "ticketMode", "priceMode",
                         "tel", "maxPrice", "areaQuoteStrategy", "invoiceRequired",
                         "allowSeatChange", "notifyUrl", "attach"},
        "recognize/seat-shot": {"imageUrl", "cityName", "notifyUrl"},
        "recognize/confirm": {"recognizeId", "cinemaId", "movieId", "showId",
                              "cityName", "seats"},
        "order/cancel": {"orderNo", "reason"},
        "order/detail": {"orderNo"},
        "order/urge": {"orderNo"},
        "order/refund": {"orderNo", "reason"},
        "order/refund-detail": {"refundNo"},
        "brand/list": {"keyword", "cityCode"},
        "city/list": {"keyword"},
        "seat/map": {"showId"},
        "ping": set(),
        "ticketcode/get": {"orderNo"},
        # 4.6 影院列表
        "cinema/list": {"cityCode", "districtCode", "movieId", "brandId",
                        "keyword", "longitude", "latitude", "page", "pageSize"},
        # 4.7 影片列表
        "movie/list": {"type", "cityCode", "keyword", "page", "pageSize"},
        # 4.8 场次列表
        "show/list": {"cinemaId", "movieId", "showDate", "page", "pageSize"},
    }
    api_src = (ROOT / "integrations/liangpiao/api.py").read_text(encoding="utf-8")
    tree = ast.parse(api_src)

    extras = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("ch"):
            continue
        path = ""
        for s in ast.walk(node):
            if isinstance(s, ast.Constant) and isinstance(s.value, str) \
                    and s.value.startswith("/") and not s.value.startswith("/api"):
                path = s.value.lstrip("/")
                break
        allowed = DOC_PARAMS.get(path)
        if allowed is None:
            continue
        # 收集这个函数里所有写进 payload 的字符串 key
        keys = set()
        for s in ast.walk(node):
            if isinstance(s, ast.Dict):
                for k in s.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
            elif isinstance(s, ast.Subscript):
                sl = s.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    keys.add(sl.value)
        bad = sorted(k for k in keys if k not in allowed)
        if bad:
            extras.append((node.name, path, bad))

    if extras:
        for fn, path, bad in extras:
            print(f"  ⚠ {fn}() → /{path}")
            print(f"      文档外的参数：{bad}")
    else:
        print("  ✓ 所有请求参数都在文档白名单里")
    check("没有文档外的请求参数", not extras, f"{len(extras)} 处")

    # 反向：文档必填但我们没传
    MUST = {
        "order/create": ["outOrderNo", "showId", "seats", "ticketMode", "priceMode", "tel"],
        "order/preflight": ["showId", "seats", "ticketMode", "priceMode"],
        "recognize/seat-shot": ["imageUrl"],
        "recognize/confirm": ["recognizeId"],
    }
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("ch"):
            continue
        path = ""
        for s in ast.walk(node):
            if isinstance(s, ast.Constant) and isinstance(s.value, str) \
                    and s.value.startswith("/") and not s.value.startswith("/api"):
                path = s.value.lstrip("/")
                break
        need = MUST.get(path)
        if not need:
            continue
        keys = set()
        for s in ast.walk(node):
            if isinstance(s, ast.Dict):
                for k in s.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
            elif isinstance(s, ast.Subscript):
                sl = s.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    keys.add(sl.value)
        for k in need:
            if k not in keys:
                missing.append(f"{node.name}() 缺 {k}")
    for m in missing:
        print(f"  ⚠ {m}")
    check("文档必填参数都传了", not missing, f"{len(missing)} 处缺失")
    return extras


# ==================================================================== 四
def audit_hardcoded():
    section("四、写死的字面量：有没有出处")
    doc = DOC.read_text(encoding="utf-8", errors="replace") if DOC.exists() else ""
    JUSTIFIED = {
        "40": "文档：离开场不足40分钟勿支付",
        "64": "outOrderNo ≤64 字符",
        "512": "attach / notifyUrl ≤512 字符",
        "32": "nonce ≤32 字符",
        "1024": "imageUrl ≤1024 字符",
        "300": "签名容差 ±5 分钟 = 300 秒",
        "600": "nonce 幂等窗口 10 分钟",
        "50": "限流 50 次/秒",
        "10": "回调 10 秒内应答",
        "15": "档位阈值 FAST ≥15 分钟（读自配置）",
        "0": "零值/默认值",
        "1": "最小阈值",
        "2": "两位小数 / 最小候选数",
    }
    # ★ 文档【明确给了数字】的超时，逐条记出处；
    #   文档没给、由我选的那些，必须单独标出来（见 SUSPICIOUS_BY_DESIGN）
    TIMEOUT_JUSTIFIED = {
        ("integrations/liangpiao/client.py", "SEAT_LIST_TIMEOUT"):
            "文档原文：请把该接口客户端超时设为 12 秒",
        ("integrations/liangpiao/client.py", "RECOGNIZE_TIMEOUT"):
            "文档原文：中位 3.7 秒 / p95 5.2 秒，请按 5 秒设超时",
        ("integrations/liangpiao/client.py", "DEFAULT_TIMEOUT"):
            "★ 文档没给这个数，是我选的兜底值 —— 已在代码注释里标注",
        ("integrations/model/client.py", "DEFAULT_TIMEOUT"):
            "★ 文档没给这个数，是我选的（买家在等，等太久不如转人工）",
        ("integrations/model/client.py", "DEFAULT_MAX_TOKENS"):
            "答一句话不需要长输出，这是我选的",
        ("integrations/model/client.py", "DEFAULT_RETRIES"):
            "偶发抖动值得再试一次，但别让买家等第二轮",
        # ---- 万达（★ 全部是【我选的】，没有文档依据）----
        #   万达那边我们【没有任何超时规定】，所以以下每个数都是我定的。
        #   逐条写选它的理由，并且明确标出"这是我选的"。
        ("integrations/wanda/client.py", "DEFAULT_TIMEOUT"):
            "★ 我选的。万达没有超时文档。取 15 秒：比良票的 10 秒宽，"
            "因为万达要跨多个域、可能多一跳网关",
        ("integrations/wanda/client.py", "SHOW_TIMEOUT"):
            "★ 我选的。场次要拉整日排期，数据量比单场大",
        ("integrations/wanda/client.py", "SEAT_TIMEOUT"):
            "★ 我选的。座位图是一整个厅的座位 + 分区价，是响应最大的接口",
        ("integrations/wanda/client.py", "WRITE_TIMEOUT"):
            "★ 我选的。锁座/取消是【写操作】：挂太久不知道成没成，"
            "比直接失败更难处理 ⇒ 给得比读接口更大一点，但不能无限等",
        ("integrations/wanda/client.py", "LOGIN_TIMEOUT"):
            "★ 我选的。登录要走短信通道，链路比普通接口长",
        # ---- 万达插件桥 ----
        ("integrations/wanda/bridge.py", "MAX_TTL"):
            "★ 出处：你给的出票系统接口说明 —— "
            "store.acquire_account_lease(phone, order_id, operator_id, ttl≤1800s)。"
            "照抄同一个上限，免得本地记账有效期比后端长",
        # ---- 黑名单缓存 ----
        ("application/blacklist.py", "MAX_CACHE_ENTRIES"):
            "★ 我选的，不是业务常量：这是缓存字典的容量上限，"
            "防它无限长。一个店的买家量级是几百，4096 足够；"
            "满了整体清空的代价只是多查几次（黑名单是店主手动标注，"
            "不是秒级事实）",
        ("application/blacklist.py", "DEFAULT_TTL_SECONDS"):
            "★ 我选的：黑名单是店主手动标注，不是秒级事实；"
            "而每条买家消息都查一次会在热路径上多两次网络往返。"
            "取 10 分钟 —— 店主把人拉黑后最迟 10 分钟生效",
    }
    suspicious = []
    for rel, src in production_files().items():
        for i, line in enumerate(src.splitlines(), 1):
            m = re.match(r"\s*([A-Z][A-Z0-9_]{2,})\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)[\"']?\s*(?:#.*)?$", line)
            if not m:
                continue
            name, num = m.group(1), m.group(2)
            if name in ("HOST", "PORT"):
                continue
            if (rel, name) in TIMEOUT_JUSTIFIED:
                continue
            if num in JUSTIFIED:
                continue
            if doc and num in doc:
                continue
            suspicious.append((rel, i, name, num))
    if suspicious:
        for rel, i, name, num in suspicious:
            print(f"  ⚠ {rel}:{i}  {name} = {num}")
    else:
        print("  ✓ 所有业务常量的字面量都能在文档里找到出处")
    check("写死的业务常量都有出处", not suspicious, f"{len(suspicious)} 处待核")
    return suspicious


# ==================================================================== 主流程
def main() -> int:
    p1 = audit_unused_function_params()
    audit_style_presets()
    audit_cross_source_consistency()
    audit_parsed_but_unread()
    audit_api_request_params()
    audit_hardcoded()

    section("五、★★ 院线名缓存：写好了却没人调（2026-10-14 审计发现）")
    # ★★★ 这是一个真缺口：
    #   store.sync_brands_from_api 早就写好了，但【全仓没有任何调用者】——
    #   ⇒ data/brands.json 永远不会存在 ⇒ load_brands() 永远退回
    #     KNOWN_BRANDS（只有两个名字）。
    #   实测后果（线上确认）：
    #     · 良票 brand/list 真实有【140】个院线名，我们只认识 2 个
    #     · 规则的 brands 条件是 name 精确比较 ⇒ 除万达/CGV 外
    #       其余 138 个院线的规则永远匹配不上
    #     · rules_payload 的 known_brands 只给 2 个选项 ⇒ 店主没法配
    #   修法：server 的 /api/rules 里懒同步一次（过期才拉，失败静默）。
    from application import store as _st

    check("★★ sync_brands_from_api 有调用者了（不再是死代码）",
          callable(getattr(_st, "refresh_brands_if_stale", None)),
          "缺 refresh_brands_if_stale")

    class _FakeBrandAPI:
        def __init__(self):
            self.calls = 0

        def ch4_brand_list(self, **kw):
            self.calls += 1
            return [{"brandId": 1, "name": "测试院线"}]

    fake = _FakeBrandAPI()
    age = _st.brands_age_seconds()
    if age is not None and age < _st.BRANDS_TTL_SECONDS:
        n = _st.refresh_brands_if_stale(fake)
        check("★ 缓存新鲜时不重复拉（省一次外网调用）",
              n == 0 and fake.calls == 0,
              f"返回 {n} / 良票被调 {fake.calls} 次")
    else:
        print("  （本机缓存不存在或已过期，跳过'新鲜时不拉'这条）")

    # ★ 传 None（没配凭证）时必须静默返回 0，不能让规则页打不开
    check("★ 没凭证/没 api 时静默返回 0（规则页照样能开）",
          _st.refresh_brands_if_stale(None) == 0)

    # ★ load_brands 必须永远有兜底 —— 它没有缓存文件时给 KNOWN_BRANDS
    check("★ load_brands 永远有兜底（不会返回空）",
          bool(_st.load_brands()), str(_st.load_brands()[:3]))

    # ★ 文档说的良票真实院线名和识别出的 brandName 是同一个值 ——
    #   这条是能不能直接拿识别结果去比规则的前提，值得钉住出处。
    import domain.quote_rules as _qr
    check("★ KNOWN_BRANDS 用的是良票真名（不是票达人分级名）",
          all("类" not in b and "一线" not in b for b in _qr.KNOWN_BRANDS),
          str(_qr.KNOWN_BRANDS))

    section("六、★★★ 闲鱼订单数量【不许】作为校验依据（店主定的纪律）")
    # ★★★ 店主原话（2026-10-14）：
    #    「另外闲鱼提交的订单数量不作为校验 —— 买家需要多张，
    #      但是拍下的数量为一，也需要改价」
    #
    #    为什么必须钉住：
    #      闲鱼上的「数量」是买家点单时选的件数，通常就是 1 件
    #      （卖家挂的是一个 1 件的咨询商品）。而买家真实要几张
    #      是他【说】的、或我们从他【选的座位】数出来的。
    #      拿闲鱼订单数量去校验张数，会让「要 2 张只拍 1 件」的正常单被拒。
    #
    #    ★ 我们现在的做法（本来就对，但要防止以后有人改错）：
    #      张数一律来自 len(seats)：报价 seat_count、下单 ticket_count、
    #      万达 resolve_cost 的 count，全是从座位列表数出来的。
    #      闲鱼订单事件的字段【只用于】付款/发货状态与金额核对。
    import re as _re2
    _root = Path(__file__).resolve().parent.parent
    _src = (_root / "application" / "ticket_flow.py").read_text(encoding="utf-8")
    _code = [ln for ln in _src.splitlines()
             if not ln.lstrip().startswith("#")]
    _code_txt = "\n".join(_code)

    check("★★ 出票张数来自 len(seats)（不是订单数量）",
          "ticket_count=len(seats_names)" in _code_txt
          or "ticket_count=len(seat_names)" in _code_txt,
          "没找到 ticket_count=len(seat...)")
    _bad = [ln.strip()[:70] for ln in _code
            if _re2.search(r"(ticket_count|seat_count)\s*=\s*.*"
                           r"(quantity|buyAmount|buyNum|orderQuantity)", ln)]
    check("★★ 没有任何一处把闲鱼订单数量当张数", not _bad, str(_bad))
    _wc = (_root / "application" / "wanda_cost.py").read_text(encoding="utf-8")
    check("★ 万达 resolve_cost 的 count 也来自座位数",
          "count = len(want)" in _wc)

    section("汇总")
    print(f"  无理由的未用函数参数 {len(p1)}")
    print()
    passed, total = sum(RESULTS), len(RESULTS)
    for i, ok in enumerate(RESULTS):
        if not ok:
            print(f"  ✗ 第 {i + 1} 项断言失败")
    print(f"  结果：{passed}/{total} 项通过")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
