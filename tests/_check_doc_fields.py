"""
_check_doc_fields.py —— ★ 全量核对：良票文档声明的每个响应字段，我们有没有真在用

════════════════════════════════════════════════════════════════
★ 为什么会有这个文件

  起因：「识别有两层，另外还有失败原因，我们还没把」

  我去查，先用 grep 数行数 —— 把注释和 docstring 也算成了实现，
  于是下了个错结论（"noMatchReason 没处理"），其实处理了。

  再用 AST 认真查，发现确实有字段没人读（cached / cinemaCode …）。

  ★ 问题不在某一次判断错，在于【没有系统性核对】。
    原来的 _check_params.py 只人工挑了 3 个字段查（pickup_url /
    refundable / estimated_settle_fen），其余全靠人记得。

  所以这个文件把口径反过来：**从文档出发**，把每个接口的
  每个响应字段都列出来，逐个查代码里有没有人读。
  漏一个就 FAIL，不靠记性。

★ 判据（★ 这里最容易出错，写清楚）

  "用到" = 业务层（application/ domain/ prompts/ server.py settings.py
           templates.py qa.py）里，通过以下任一方式读了它：
             · d["field"]          （字符串下标）
             · d.get("field")      （字符串 get）
             · d.field             （属性访问）
             · .field              （属性访问）
             · update(field=...)   （关键字参数）

  ★ 只解析不算用 —— integrations/liangpiao/api.py 负责把响应搬进
    dataclass，那里出现不代表业务在用。所以它被排除在"用"之外。
    （这正是我上一轮判断翻车的地方：解析层写了一行，看着像实现了。）

★ 已知例外（列出来，不偷偷放过）

  · cached        —— 只影响扣费，业务不用。★ 但应该显示给运营看
  · finalResults  —— 整层原样留着做留痕/排查，不是"读字段"
  这些列在 KNOWN_UNUSED 里，附理由。理由过期的就删掉，
  ★ 不许因为它们麻烦就长期挂在例外里。
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

DOC_CANDIDATES = [
    Path(r"D:\13250\Users\良票开放平台-API接入文档 (3).md"),
    ROOT.parent / "良票开放平台-API接入文档 (3).md",
]

# ★ 业务层 = 会"读字段做决定"的地方
BIZ_DIRS = ("application", "domain", "prompts", "integrations/liangpiao")
BIZ_FILES = ("server.py", "settings.py", "templates.py", "qa.py", "run_checks.py")
# ★ 只负责搬运的层 —— 它出现不算"用"
PARSE_ONLY = {"integrations/liangpiao/api.py"}

# ★ 明确的例外。每条都要有理由，理由过期的要删。
KNOWN_UNUSED: dict[str, str] = {
    "cached": "只影响是否扣费，不影响业务流程。★ 待办：应显示给运营（成本可见性）",
    "finalResults": "整个第二层的原始对象，只做留痕/排查用，不是逐字段读。"
                    "★ 识别争议（买家说识别错了）时应该把它留痕，便于对账申诉",
    # ---- 取票凭证：文档 2984 行明说「只靠 ticketCode 也能取票」----
    "ticketPassword": "取票密码。文档 2984：『ticketPassword 少量订单有值(一般 6 位)』"
                      "且『只靠 ticketCode 也能取票』。"
                      "★ 待办（便宜改进）：有值就跟着取票码一起发，"
                      "没有就不提 —— 那部分订单取票机可能要两个都输",
    "ticketImage": "票根图/二维码图。★ 待办：扫码入场(entryType=1)时该发这张图",
    "entryType": "入场方式 0 取票入场 / 1 扫码入场。文档 2984：『以 0 为主，1 少量，"
                 "两者都可能为 null』。★ 待办：=1 时应发 ticketImage 而不是码",
    "getTicketType": "取票方式。文档 2984：『常为 null』，不能依赖。不用是对的",
}

# ==================================================================== 分类
#
# ★ 关键认识：不是所有"没读的字段"都是漏做。
#   文档 195 个响应字段里，一大半是【展示用】的 ——
#   拼音、导演、演员、海报、豆瓣评分、经纬度、座机电话…
#   我们做的是影票代订客服，不需要展示这些。
#
#   把 104 个未使用字段全列出来只会埋掉真正要紧的那 3~5 个。
#   ⇒ 分类，而且把"要紧"的单独拎出来报。
#
# ★ 分三类，判据写死在这里，不靠人工每次重新判断：
CATEGORY = {
    # ---- A 类：展示/元数据。我们不用是【设计选择】，不是漏做。
    "display": {
        "pinyin", "initial", "level", "poster", "director", "actors",
        "genres", "rating", "boxOffice", "intro", "releaseDate",
        "address", "phone", "longitude", "latitude", "distance",
        "districtCode", "districtName", "cityCode", "brandId",
        "channelId", "env", "serverTime", "city", "movieId", "cinemaId",
        "total", "list", "cinemaName", "cinemaAddress", "cityName",
        "brandName", "movieName", "hallName", "recognizeId", "startTime",
    },
    # ---- B 类：★ 可能要用的业务字段。这些没读就要【逐个说清为什么】。
    "business": {
        "maxSeatsPerOrder", "stopSaleTime", "halls", "endTime", "showCount",
        "cinemaCode", "duration", "priceMismatch", "seatMatched",
        "seatConfirmRequired", "seatConfirmReasons", "expectedSeatCount",
        "prices", "priceAllFen", "rawResults", "matchLevel", "noMatchReason",
        "candidates", "showId", "seat", "ticketMode", "urgeCooldown",
        "settleAmount", "refundAmount", "lossAmount", "freezeAmount",
        "maxPrice", "estimateAmount", "totalAmount", "pickupUrl",
        "refundable", "orderStatus", "failCode", "failReason",
    },
}

# ★ B 类里"已知且有意不读"的 —— 每条都要有理由。
#   没写理由的 B 类字段会被报成 ★★（要处理）。
BUSINESS_EXPLAINED: dict[str, str] = {
    "cinemaCode": "影院专资编码。我们靠 cinemaId 定位，编码只在排查时用，"
                  "★ 待办：影院校验（对账）时应该用得上",
    "halls": "未来有排片的影厅名列表。良票路径用不到（它直接给 hallName）；"
             "★ 万达路径要用 —— 它拿不到 hallName，只能靠这份厅名列表反查",
    "stopSaleTime": "停售时间。★ 待办：报价有效期应该跟它取小，"
                    "否则可能出现「我们还在报价，上游已停售」",
    "endTime": "散场时间。已解析（api.py:235）但业务层没读。"
               "★ 待办：场次已开场判断可以用它，比只看 startTime 准",
    "showCount": "该日可售场次数。用于给买家提示「这天有几场」，非必需",
    "duration": "影片时长。非必需（可用 endTime - startTime 推）",
    "maxSeatsPerOrder": "本场次单笔限购。★ 待办：下单前应校验张数不超它，"
                        "现在只在报价时用了平台默认 6",
    "priceMismatch": "座位价合计与截图总价对不上。已通过 warnings() 出口，"
                     "★ 但不该算『没人读』—— 判据没捕到 self.x 形态",
    "seatMatched": "座位有没有对齐到平台图。已通过 warnings() 出口",
    "seatConfirmRequired": "座位卡片可能被遮挡。已通过 warnings() 出口",
    "seatConfirmReasons": "同上，理由明细。warnings() 只用了布尔，明细没用",
    "expectedSeatCount": "期望座位数。已通过 warnings() 出口（间接）",
    "prices": "★ 这是【我们该看到的价】。业务层通过 _seats_original_fen() 间接用",
    "priceAllFen": "座位价合计。已通过 warnings() 间接用",
    "rawResults": "模型读到的原文。★ 待办：识别争议时应该留痕，便于申诉",
    # ---- 以下 3 个是审计查出来、逐个确认过"不影响业务"的 ----
    "estimated": "★ 已修：文档说 true 表示 totalAmount 是预估上限而非成交价。"
                 "原来解析了没人读 —— 退回到用 totalAmount 当成本时，"
                 "等于把上限当确定价。现在 ticket_flow 读 cost_basis_is_estimate，"
                 "是预估价时给报价打标记（price_is_estimate）",
    "allowSeatChange": "这是【请求参数】（我们发 true），响应里只是回显供换座对照。"
                       "不回读不影响。★ 待办：如果以后要问买家「接受调换附近座位吗」，"
                       "就该把它显式发出去并且记住买家的选择",
    "graphReliable": "整图级标记：画图坐标是否全部来自供货方矩阵。"
                     "为 false 时坐标仍可用来摆放座位图，只是分不清真实物理排布"
                     "（座位号可能从右往左编、单双号分侧）。"
                     "★ 我们目前不用座位图渲染（买家发的是闲鱼截图），所以不影响。"
                     "要做手动选座时必须读它",
    # ---- ★ 判据盲区：只在【字符串下标】里出现的字段 ----
    #   这些代码里确实用了，但形态是 s["rowNo"] / d["key"]，
    #   而"解析层映射"只认 xxx=data.get("docName") 这种赋值形态，
    #   于是被判成没读。★ 记下来，免得每次重跑都要人工解释一遍。
    #   如果哪天要改判据，这几条应该能被自动覆盖到。
    "rowNo": "下单座位结构：api.py:627 `int(s[\"rowNo\"])`。"
             "★ 字符串下标形态，判据有盲区，实际在用",
    "colNo": "同上，api.py:627 `int(s[\"colNo\"])`",
}

RESULTS: list[bool] = []


def categorize(name: str) -> str:
    """把一个文档字段归到 A 展示 / B 业务 / C 其它。"""
    lf = leaf(name).split(".")[-1].split("[")[0]
    if lf in CATEGORY["business"]:
        return "business"
    if lf in CATEGORY["display"]:
        return "display"
    return "other"


def check(label: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return bool(ok)


# ==================================================================== 一、解析文档
def parse_doc() -> list[dict]:
    """
    从文档抽出每个接口的响应字段。

    返回 [{section, path, fields:[(name, type, desc), ...]}, ...]

    格式（从 4.2 抄的）：
        ### 4.2 城市列表
        `POST /api/v1/city/list` — 平台已开通的城市。
        **响应参数**
        | 字段 | 类型 | 说明 |
        | `list[].code` | string | 国标行政区划编码 |
    """
    doc = next((p for p in DOC_CANDIDATES if p.exists()), None)
    if doc is None:
        return []
    lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()

    out: list[dict] = []
    cur: dict | None = None
    in_resp = False
    in_req = False

    for ln in lines:
        # 章节标题
        m = re.match(r"^#{3,4}\s+(\S+)\s*(.*)$", ln)
        if m:
            cur = {"section": m.group(1), "title": m.group(2).strip(),
                   "path": "", "fields": [], "refs": []}
            out.append(cur)
            in_resp = in_req = False
            continue
        if cur is None:
            continue
        # 接口路径
        for pm in re.finditer(r"`(?:POST|GET)\s+(/[\w/{}\-.]+)`", ln):
            if not cur["path"]:
                cur["path"] = pm.group(1)
        # 表格区段标记
        #
        # ★ 这里踩过坑。文档的标题有【四种变体】，第一版只认了一种，
        #   结果 5.x / 6.x / 7.x 全没抽到（包括 7.1 那个"两层"的识别）：
        #       **响应参数**
        #       **响应参数**(两层)                          ← 7.1，最要紧的一节
        #       **响应参数**(金额均为分,字符串)              ← 6.1
        #       **响应参数**:单个场次对象,结构与…完全相同     ← 引用别的章节，本节点没有表
        #   ⇒ 用 startswith 判，不吃死整行。
        stripped = ln.strip()
        if stripped.startswith("**请求参数**"):
            in_req, in_resp = True, False
            continue
        if stripped.startswith("**响应参数**"):
            in_resp, in_req = True, False
            # ★ 冒号后面是"结构同 XX"这种引用写法 —— 本节没有字段表，
            #   不能当成"字段缺失"，要单独记账（见 below: 引用型章节）。
            after = stripped[len("**响应参数**"):].lstrip()
            if after.startswith(":") or after.startswith("："):
                cur.setdefault("refs", []).append(after.lstrip(":：").strip())
                in_resp = False
            continue
        if re.match(r"^\*\*(请求示例|响应示例|返回示例|示例)", stripped):
            in_resp = in_req = False
            continue
        if re.match(r"^\*\*[^*]+\*\*\s*$", stripped) and "参数" not in stripped:
            # 别的粗体小节标题 —— 表格结束
            in_resp = in_req = False
            continue
        # 表格行
        if in_resp and ln.startswith("|"):
            # ★ 类型列里含转义的竖线（`string \| null`）——
            #   直接 split("|") 会把类型切成两半、说明整体错位，
            #   于是说明显示成 null。要先保护 \| 再切。
            safe = ln.replace(r"\|", "\x00")
            cells = [c.replace("\x00", "|").strip()
                     for c in safe.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            name = cells[0].strip("`").strip()
            if not name or name in ("字段", "参数") or set(name) <= set("-: "):
                continue
            if not re.match(r"^[A-Za-z_][\w.\[\]]*$", name):
                continue
            cur["fields"].append((name, cells[1], cells[2]))
    return out


# ==================================================================== 二、扫代码
def collect_used() -> dict[str, set[str]]:
    """
    扫业务层，收集"被读过"的名字。

    两类都收，因为它们形态不同：
      · 属性名        self.cinema_id      → "cinema_id"
      · 字符串键      f.get("cinemaId")   → "cinemaId"
    """
    used: dict[str, set[str]] = {}

    def add(name: str, where: str) -> None:
        if name:
            used.setdefault(name, set()).add(where)

    files = []
    for d in BIZ_DIRS:
        base = ROOT / d
        if base.exists():
            files.extend(p for p in base.rglob("*.py")
                         if "__pycache__" not in p.parts)
    for f in BIZ_FILES:
        p = ROOT / f
        if p.exists():
            files.append(p)

    for p in files:
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if rel in PARSE_ONLY:
            continue                      # ★ 只负责搬运，不算用
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                add(node.attr, rel)
            elif isinstance(node, ast.Subscript) and isinstance(node.slice,
                                                                ast.Constant):
                if isinstance(node.slice.value, str):
                    add(node.slice.value, rel)
            elif isinstance(node, ast.Call):
                f = node.func
                # f.get("x") / obj.get("x")
                if isinstance(f, ast.Attribute) and f.attr in ("get", "pop",
                                                               "setdefault"):
                    if node.args and isinstance(node.args[0], ast.Constant):
                        if isinstance(node.args[0].value, str):
                            add(node.args[0].value, rel)
                # f(x="y") / SomeClass(x="y")
                for kw in node.keywords or []:
                    if kw.arg:
                        add(kw.arg, rel)
    return used


# ==================================================================== 三、对照
# ★ 文档字段名 → 我们代码里的名字。两种可能：原样保留，或转 snake_case。
def camel_to_snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def leaf(name: str) -> str:
    """list[].cinemaId → cinemaId；a.b.c → c"""
    n = name.split(".")[-1]
    return re.sub(r"\[\]$", "", n)


def collect_parser_attrs() -> dict[str, set[str]]:
    """
    ★ 第一跳：api.py 把文档字段搬成了哪个属性名。

    从 `xxx=data.get("docName")` / `xxx=f.get("docName")` 这类赋值里抽。
    返回 {文档字段名: {我们的属性名}}

    ★ 为什么要这一跳：
      判据原来是「文档名 → 业务层」，中间隔着解析层，
      于是 api.py 里叫 `price_mismatch`、文档里叫 `priceMismatch` 就对不上，
      工具报了一堆假阳性（estimateAmount / totalAmount 都报了，其实在用）。
      两跳之后：文档名 → 属性名 → 业务层有没有读【那个属性】。
    """
    out: dict[str, set[str]] = {}
    src = ROOT / "integrations" / "liangpiao" / "api.py"
    if not src.exists():
        return out
    try:
        tree = ast.parse(src.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        # xxx=data.get("docName")  在 dataclass 字段赋值里
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if not isinstance(tgt, ast.Name):
                    continue
                v = node.value
                # int(x.get("k") or 6) / bool(x.get("k")) / str(x["k"])
                inner = v
                if isinstance(inner, ast.Call) and inner.args:
                    inner = inner.args[0]
                if isinstance(inner, ast.Call) and isinstance(inner.func,
                                                              ast.Attribute):
                    if inner.func.attr in ("get",) and inner.args:
                        if isinstance(inner.args[0], ast.Constant) and \
                                isinstance(inner.args[0].value, str):
                            out.setdefault(inner.args[0].value, set()).add(tgt.id)
                elif isinstance(inner, ast.Subscript):
                    if isinstance(inner.slice, ast.Constant) and \
                            isinstance(inner.slice.value, str):
                        out.setdefault(inner.slice.value, set()).add(tgt.id)
        # cls(xxx=data.get("docName")) —— from_data 里的关键字参数
        if isinstance(node, ast.Call):
            for kw in node.keywords or []:
                if not kw.arg:
                    continue
                inner = kw.value
                if isinstance(inner, ast.Call) and inner.args:
                    inner = inner.args[0]
                if isinstance(inner, ast.Call) and isinstance(inner.func,
                                                              ast.Attribute):
                    if inner.func.attr == "get" and inner.args:
                        if isinstance(inner.args[0], ast.Constant) and \
                                isinstance(inner.args[0].value, str):
                            out.setdefault(inner.args[0].value, set()).add(kw.arg)
                elif isinstance(inner, ast.Subscript):
                    if isinstance(inner.slice, ast.Constant) and \
                            isinstance(inner.slice.value, str):
                        out.setdefault(inner.slice.value, set()).add(kw.arg)
    return out


def collect_attr_graph() -> dict[str, set[str]]:
    """
    ★ 第三跳：api.py 里【属性引用了属性】的图。

    例如：
        @property
        def cost_basis_fen(self):
            return self.estimate_amount_fen or self.total_amount_fen

    ⇒ estimate_amount_fen / total_amount_fen 是通过 cost_basis_fen 被间接用的。
      业务层读的是 pre.cost_basis_fen，看不到那两个原始字段。
      不做这一跳就会把 estimateAmount / totalAmount 误报成"没人读"。

    返回 {被引用的属性: {引用它的属性}}
    """
    out: dict[str, set[str]] = {}
    src = ROOT / "integrations" / "liangpiao" / "api.py"
    if not src.exists():
        return out
    try:
        tree = ast.parse(src.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        owner = node.name
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute):
                out.setdefault(sub.attr, set()).add(owner)
    return out


def transitive_attrs(attrs: set[str], graph: dict[str, set[str]],
                    used: dict[str, set[str]]) -> bool:
    """
    attrs 里的属性，是否【直接或经由属性链】被业务层读过。

    ★ 走 BFS：attrs → 引用了它们的属性 → 再往上，直到碰到业务层读过的名字。
      最多 6 层（足够，再多说明设计有问题）。
    """
    seen: set[str] = set()
    frontier = set(attrs)
    for _ in range(6):
        if not frontier:
            return False
        if frontier & used.keys():
            return True
        seen |= frontier
        nxt: set[str] = set()
        for a in frontier:
            nxt |= graph.get(a, set())
        frontier = nxt - seen
    return False


def main() -> int:                                      # noqa: C901
    sections = parse_doc()
    if not sections:
        print("★ 找不到良票文档 —— 这个检查跳过不了。")
        print(f"  找过：{[str(p) for p in DOC_CANDIDATES]}")
        return 1

    print("=" * 78)
    print(f"一、从文档抽出 {len(sections)} 个带响应字段的接口")
    print("=" * 78)
    total_fields = sum(len(s["fields"]) for s in sections)
    print(f"  接口 {len(sections)} 个，响应字段合计 {total_fields} 个")
    check("文档解析到接口（不是 0 个）", len(sections) >= 20,
          f"{len(sections)} 个")
    check("文档解析到字段（不是 0 个）", total_fields >= 100,
          f"{total_fields} 个")

    used = collect_used()
    parser_attrs = collect_parser_attrs()
    attr_graph = collect_attr_graph()
    print(f"  业务层收集到 {len(used)} 个被读过的名字")
    print(f"  解析层映射表 {len(parser_attrs)} 条（文档字段 → 我们的属性名）")
    print(f"  属性引用图   {len(attr_graph)} 个属性（用于追间接使用）")

    # ---- 逐个字段查（★ 两跳）----
    print()
    print("=" * 78)
    print("二、★ 逐字段核对：文档说有的，我们读了没有")
    print("=" * 78)
    unused: list[tuple[str, str, str, str]] = []      # sec, field, type, desc
    parsed_never_read: list[tuple[str, str, str]] = []   # ★ 更精确的一类
    used_count = 0
    for sec in sections:
        for (name, ftype, desc) in sec["fields"]:
            lf = leaf(name)
            # 第一跳：文档名 → 我们的属性名
            attrs = set(parser_attrs.get(lf) or ())
            attrs |= {camel_to_snake(lf)}
            if not attrs:
                attrs = {lf}
            # 第二跳：业务层有没有读【那些属性】or 直接用文档名
            # ★ 第三跳：那些属性有没有通过别的属性（@property 计算）被读到
            if lf in used or transitive_attrs(attrs, attr_graph, used):
                used_count += 1
            else:
                unused.append((sec["section"], name, ftype, desc))
                if parser_attrs.get(lf):
                    # 解析层搬了，业务层一次没读 —— 这类最可疑
                    parsed_never_read.append((sec["section"], name,
                                              "/".join(sorted(attrs))))

    print(f"  已使用 {used_count} / {total_fields}")
    print(f"  未被业务层读取 {len(unused)} 个")
    if parsed_never_read:
        print()
        print(f"  ★★ 其中【解析层搬了、业务层一次没读】{len(parsed_never_read)} 个"
              f"（这类最可疑）：")
        for sec, name, attr in parsed_never_read:
            print(f"      【{sec}】{name:<24}→ {attr}")

    # ---- ★ 分类，别把要紧的埋在展示字段里 ----
    biz_missing, display_missing, other_missing = [], [], []
    for sec, name, ftype, desc in unused:
        lf = leaf(name)
        if lf in KNOWN_UNUSED or camel_to_snake(lf) in KNOWN_UNUSED:
            continue
        cat = categorize(name)
        row = (sec, name, desc)
        if cat == "business":
            biz_missing.append(row)
        elif cat == "display":
            display_missing.append(row)
        else:
            other_missing.append(row)

    print()
    print("  ── 分类 ──")
    print(f"    B 业务字段未读   {len(biz_missing):>3} 个   ← ★ 这些逐个要有交代")
    print(f"    A 展示字段未读   {len(display_missing):>3} 个   ← 设计上不用，正常")
    print(f"    C 其它未读       {len(other_missing):>3} 个")

    # ★★ B 类：没写理由的，才是真漏做
    print()
    print("=" * 78)
    print("三、★★ B 类业务字段：没读、且没写理由的（这些才是漏做）")
    print("=" * 78)
    unexplained = []
    explained = []
    for sec, name, desc in biz_missing:
        lf = leaf(name)
        why = BUSINESS_EXPLAINED.get(lf) or BUSINESS_EXPLAINED.get(
            camel_to_snake(lf))
        if why:
            explained.append((sec, name, desc, why))
        else:
            unexplained.append((sec, name, desc))

    if unexplained:
        for sec, name, desc in unexplained:
            print(f"  ★★ 【{sec}】{name}")
            print(f"        {desc[:80]}")
    else:
        print("  ★ 没有 —— B 类字段全部有交代 ✓")

    if explained:
        print()
        print(f"  ── 有交代的（{len(explained)} 个）──")
        for sec, name, desc, why in explained:
            print(f"    {name:<24}{why[:66]}")

    # ★ 专门盯 estimated：它是审计查出来的真问题（预估上限当确定价）
    import inspect
    from application.ticket_flow import TicketFlow
    flow_src = inspect.getsource(TicketFlow)
    check("★ estimated 真的被读了（预估上限不能当成交价）",
          "cost_basis_is_estimate" in flow_src
          and "price_is_estimate" in flow_src,
          "ticket_flow 里有 price_is_estimate 标记")

    check("★★ B 类业务字段都有交代（读了 或 写了不读的理由）",
          not unexplained, f"{len(unexplained)} 个待处理" if unexplained else "全部有交代")

    if other_missing:
        print()
        print(f"  ── C 类其它（{len(other_missing)} 个，需要人工归类一次）──")
        for sec, name, desc in other_missing[:20]:
            print(f"    【{sec}】{name:<26}{desc[:44]}")

    # ---- 反向：接口级覆盖 ----
    print()
    print("=" * 78)
    print("三、★ 接口级：文档里的章节，我们的 api.py 实现了没有")
    print("=" * 78)
    api_src = (ROOT / "integrations" / "liangpiao" / "api.py").read_text(
        encoding="utf-8", errors="replace")
    # ★ 判据要剥掉 /api/v1 前缀后【整条】匹配，不能按尾段。
    #   我们的写法：api.py 里 client.call("/order/refund/detail", ...)
    #   client.py:37  API_PREFIX = "/api/v1"   ← 前缀在这里拼
    #
    #   第一版拿整条 "/api/v1/ping" 去 api.py 找 —— 一个都找不到，
    #   把 28 个【已经实现】的接口全报成"缺失"。
    #   第二版只取尾段（"detail"）—— 又不够精确（show/detail 和 refund/detail 混）。
    #   ⇒ 剥前缀，整条比。
    # ★ 两边都 strip("/") 再比 —— 一边有前导斜杠一边没有，是这类匹配最常见的坑。
    called = {m.strip("/") for m in
              re.findall(r'client\.call\(\s*[fr]?["\']([^"\']+)["\']', api_src)}
    # ★ 在 client.py 里实现的（不走 client.call 的写法）
    called |= {"ping"}          # ch4_ping → client.ping() → self.call("/ping")
    # ★★ 有意不实现的 —— 每条必须有理由，且理由要能追溯到决策来源
    #   ★ 键要不要前导斜杠：统一在这里 strip 掉，免得两边写法不一致对不上
    #     （踩过：字典写 "order/preflight-guaranteed"，实际是
    #      "/order/preflight-guaranteed"，匹配不上。）
    NOT_IMPLEMENTED_BY_DESIGN = {
        "order/preflight-guaranteed":
            "兜底下单。★ 运营明确说过不要：「兜底下单不需要，一口价和预估设置中"
            "不是可以选择吗，另外还有报价规则」—— 价格由报价规则定，不靠兜底接口",
        "order/create-guaranteed":
            "兜底下单的下单接口，同上",
    }
    missing_impl = []
    for sec in sections:
        path = sec["path"]
        if not path:
            continue
        rel = re.sub(r"^/api/v1", "", path).strip("/")
        if rel in called or rel in NOT_IMPLEMENTED_BY_DESIGN:
            continue
        missing_impl.append((sec["section"], sec["title"], path))
    if missing_impl:
        print(f"  ★ {len(missing_impl)} 个章节的路径在 api.py 里找不到：")
        for sec, title, path in missing_impl:
            print(f"    {sec:<8}{title:<20}{path}")
    else:
        print("  ★ 文档里每个接口路径都在 api.py 里出现了 ✓")
    check("★ 文档接口都有对应实现", not missing_impl,
          f"{len(missing_impl)} 个缺失" if missing_impl else "全部覆盖")

    print()
    print("=" * 78)
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"{passed}/{total} 项通过")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
