"""
domain/quote_rules.py —— 报价规则引擎

════════════════════════════════════════════════════════════════
★ 这一层解决什么问题

  良票 API 告诉我们【成本】：选座询价返回 totalAmount / estimateAmount。
  但我们要报给买家的是【售价】——我们自己加的价。

  加多少、怎么加、要不要限价、走一口价还是预估价
  —— 这就是「报价规则」干的事。

★ 和良票 API 的对应关系（先前我搞错的地方）

  渠道一口价  →  API 的 priceMode = FIXED   报价即成交价，确定
  渠道预估价  →  API 的 priceMode = LIMIT   报价是上限，实际由上游竞价后确定

  「是否限价」→ 决定要不要给 API 传 maxPrice
                maxPrice 由「单张限价 = 【预估成本】+ N」算出来

  ⇒ 所以 priceMode 不是操作员在下单时挑的，是【规则匹配出来的】。
    这也是为什么文档强调「areaQuoteStrategy / invoiceRequired 必须询价与下单传同一个值」
    —— 中间隔着规则引擎，容易传岔。

★ 匹配口径（从票达人「报价规则」页读出）

  按【规则权重降序】，数字越大越优先；同权重时优先使用最新添加的规则。
  第一条命中的规则决定这一单怎么报。

  规则命中条件：
    · 生效时间段（当前时间在区间内）
    · 启用状态
    · 包含院线（影院所属院线在列表里）
    · 排除影院（影院不在排除名单里）
    · 猫淘价区间（原价落在 [左, 右) 内 —— 文档口径是 0 ≤ 原价 < 999）

  全部规则都不命中 → 用兜底规则。
════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass, field
from datetime import time
from typing import Any

# ==================================================================== 枚举
# 报价出票模式 —— 直接决定调良票 API 时传什么 priceMode
QUOTE_FIXED = "渠道一口价"      # → priceMode = FIXED
QUOTE_ESTIMATE = "渠道预估价"   # → priceMode = LIMIT
QUOTE_SCENE = "场景预估价"      # → 页面显示"维护中"，暂不支持
# ★★ 万达专用报价（2026-10-13 加）
#
#   为什么要有它：万达和良票的【成本来源】根本不同 ——
#     良票：preflight 选座询价，而且 seats 必填 1~6
#     万达：座位图 area[].wPlusActivity.price（会员价），
#           ★ 没选座位也能取（取 W+专享区）
#
#   在界面上单独列一项，是为了让运营一眼看出"这条规则是给万达的"，
#   而不是靠品牌名去猜。加价 / 动态折扣 / 取整方式和别的规则完全一样 ——
#   它只是把【报价模式】标清楚，不改算价逻辑。
#
#   ★ priceMode 映射成 FIXED：万达的成本是座位图给的确定值，
#     不存在"报价只是上限、由上游竞价后确定"这回事。
#     （虽然万达下单走出票系统、不传 priceMode，但语义要标对，
#       免得以后看日志时误判成"这是个预估价"。）
QUOTE_WANDA = "万达专用报价"

QUOTE_MODES = (QUOTE_FIXED, QUOTE_ESTIMATE, QUOTE_SCENE, QUOTE_WANDA)

# 报价出票模式 → 良票 API 的 priceMode
PRICE_MODE_OF_QUOTE = {
    QUOTE_FIXED: "FIXED",
    QUOTE_ESTIMATE: "LIMIT",
    # ★ 万达的成本来自座位图（确定值），按 FIXED 语义
    QUOTE_WANDA: "FIXED",
}

# 报价方式（怎么在成本上加价）
ADD = "加法"                    # 成本 + N（填「报价金额」）
# ★★ 名字对齐 V5 弹窗：V5 的下拉里就叫「动态折扣」。
#    我原来叫「动态折扣加法」，多两个字，对不上。
DYNAMIC_DISCOUNT = "动态折扣"    # 走分档表（tiers），按折扣率落档
# ★ 兼容旧数据：这个旧名字读进来要认（否则老规则会被判成"非法报价方式"）
DYNAMIC_DISCOUNT_ADD = "动态折扣加法"
# ★★ 「乘法」已删除。理由：
#   ① 它是我自己加的，V5 和票达人后台都没有这个选项；
#   ② 算法是 `cost * (markup_yuan - 1)`，而界面标签写的是
#      「报价金额（预估成本 + __ 元）」—— 标签说"加"，算法按"系数"算，
#      语义不符。实测：填 1.0 → 零利润；填 0.5 → 负加价（亏本）。
#   ③ 全项目没有任何规则用过它。
#   分档表（tiers）才是"动态折扣"的正确实现，见 PricingTier。
QUOTE_METHODS = (ADD, DYNAMIC_DISCOUNT)
# ★ 历史别名：校验和读盘时要认这两个（老的 saved 规则里可能写着它们）
METHOD_ALIASES = {DYNAMIC_DISCOUNT_ADD: DYNAMIC_DISCOUNT}

# ★★ 分档计价基数 —— 逐字对齐 V5 `RoutePricing.basis`
#   （V5: Literal['original','estimated','fixed_per_ticket','cost_plus_fixed']）
#
#   它同时决定两件事：
#     ① 折扣率 = base × 10000 / original 里的 base 是谁
#     ② 比例（adjustment_bps）乘在谁身上
BASIS_ORIGINAL = "original"              # base=原价         调整乘在原价上
BASIS_ESTIMATED = "estimated"            # base=预估成本     调整乘在成本上
BASIS_PER_TICKET = "fixed_per_ticket"    # base=会员成本     每张固定加价
BASIS_COST_PLUS = "cost_plus_fixed"      # base=良票成本     整单固定加价
BASIS_TYPES = (BASIS_ORIGINAL, BASIS_ESTIMATED, BASIS_PER_TICKET, BASIS_COST_PLUS)

# 报价调整类型（最后怎么取整）
ADJUST_NONE = "不调整"
ADJUST_ROUND_YUAN_MINUS_01 = "四舍五入到元-0.1"
ADJUST_ROUND_YUAN = "四舍五入到元"
ADJUST_ROUND_JIAO = "四舍五入到角"
ADJUST_TYPES = (ADJUST_NONE, ADJUST_ROUND_YUAN_MINUS_01, ADJUST_ROUND_YUAN, ADJUST_ROUND_JIAO)

# 单位：分。整个项目金额一律用分，只有展示时才转元。
FEN_PER_YUAN = 100

# ★★ 没命中规则时的兜底限价：每张最多比成本多给这么多（元）。
#
#   为什么要有它：没规则命中时 maxPrice 原来是 None = 【钱上不封顶】。
#   文档 1189：「LIMIT 模式报价本身是预估上限，实扣以放映后结算为准，
#   建议同时设置 maxPrice（防"价格已变动"导致实付超预期）」。
#
#   ★ 为什么是 3 元：这是"覆盖从报价到出票之间正常价格波动"的量级。
#     实测 freezeAmount == maxPrice —— LIMIT 模式下冻结额就等于限价，
#     多冻的部分结算后会退，所以这个数不需要抠。
#     算出来的值还会【封顶到市场原价】（见 _fallback_max_price）——
#     绝不为一张票出得比买家自己买还贵。
#
#   ★ 为什么不能更小（我先写过 1 元/张，实测太紧）：
#     店主那张"座位价格不一致"的图（场次 37958957）：
#       成本 4635、市场原价 6000
#       1 元/张 → 兜底 4637，只要涨 0.04% 就被 300005 拒 ⇒ 几乎必然失败
#       3 元/张 → 兜底 5235，留出约 13% 空间 ⇒ 能扛住正常波动
FALLBACK_LIMIT_YUAN = 3


# ==================================================================== 规则
@dataclass
class PricingTier:
    """
    分档报价表的一行 —— 对应 V5 `domain/quote/models.py` 的 `PricingTier`。

    ★★ 逐字对齐 V5（字段名、含义、边界规则都一样），因为这张表是
       报价算法的核心，名字对不上以后就没法跟 V5 对数。

    一行长这样（就是票达人弹窗里「动态报价加法」那张表的每一行）：

        | 折扣率大于等于 | 折扣率小于 | 比例  | ±  |
        |     70%       |    75%     | +10% | +  |
        → lower_bps=7000, upper_bps=7500, adjustment_bps=1000（正号=加价）

    ★★ 折扣率到底是什么（我一开始理解错了，这里写清楚）：

        折扣率 = base × 10000 / original        ← 两个【金额】的比值

        base     和  original  按规则声明的 basis 取（见 _tier / _price_by_basis）：
          basis="original"          base=原价         original=原价   → 折扣率恒为 10000
          basis="estimated"         base=预估成本     original=官方原价
          basis="fixed_per_ticket"  base=万达会员成本 original=官方原价
          basis="cost_plus_fixed"   base=良票成本     original=官方原价

      ⇒ 所以「折扣率」不是上游给的折扣，也不是人工填的，
        它是【我们的成本相对官方原价打了多少折】。整张表就是
        "成本越接近原价（折扣率越高），加价越少甚至减价"。

    ★ 来源：V5 `backend/app/domain/quote/pricing.py:11-17`
          def _tier(configured, base, original):
              for row in configured.tiers:
                  value = base * 10000
                  if row.lower_bps * original <= value < row.upper_bps * original:
                      return row
    """
    # 折扣率区间（单位 bps，10000 = 100%）。左闭右开。
    lower_bps: int = 0
    upper_bps: int = 10000
    # ★ 比例（bps）。★ 带符号 —— 正=加价，负=减价。
    #   票达人界面里那个 «+ / -» 就是这个符号，不是另一个字段。
    adjustment_bps: int = 0
    # 每张固定加价（分）。★ 和 adjustment_bps 互斥（见 validate）——
    #   V5 的模型校验：固定加价的 basis 不能用百分比，百分比 basis 不能用固定加价。
    fixed_add_fen: int = 0
    # 上边界是否包含。V5 默认 False（左闭右开）
    upper_inclusive: bool = False


@dataclass
class QuoteRule:
    """
    一条报价规则。

    ★ 字段名对齐票达人「报价规则」页，便于逐项核对。

    ★★ 两个最容易搞混的字段（我第一版就搞混了）

      markup_yuan（页面上的「报价金额」）
          预估成本 + N 元  →  【报给买家的价】

      limit_yuan（页面上的「单张限价」；★ 实际算出来是【总限价】）
          预估成本 + M 元  →  【给上游的出价上限】，即 API 的 maxPrice

      顺序是：成本 --+报价金额--> 卖给买家 --+单张限价--> 向上游出价
      中间的差额（N 与 M 之间、以及实际结算价与二者之间的差）就是毛利空间。
    """
    name: str
    weight: int = 0                         # 数字越大越优先
    enabled: bool = True

    # ---- 命中条件 ----
    brands: list = field(default_factory=list)          # 包含院线（空 = 不限）
    # ★★★ 2026-10-14 新增：限定渠道（空 = 不限渠道，向后兼容）。
    #
    #   为什么需要它：V5 的两条主力规则（wanda_pricing / liangpiao_pricing）
    #   是【按渠道】的 —— 同一个影院走万达加 1 元/张、走良票按折扣率加价，
    #   跟"哪个院线"无关。光靠 brands 表达不了这件事。
    #
    #   ★ 取值用 CHANNEL_WANDA / CHANNEL_LIANGPIAO / ""。
    #     "" 表示不限 —— 所有现存规则都是 ""，行为一点不变。
    channel: str = ""
    hall_keywords: list = field(default_factory=list)
    excluded_hall_keywords: list = field(default_factory=list)
    fixed_cost_fen: int | None = None
    original_discount_bps: int | None = None
    minimum_price_fen: int = 0
    excluded_cinemas: list = field(default_factory=list)  # 排除影院（影院名或 ID）
    time_start: str = "00:00:00"
    time_end: str = "23:59:59"
    # 猫淘价区间：左闭右开。单位元，与页面一致（页面填的是元）
    price_left_yuan: float = 0
    price_right_yuan: float = 999

    # ---- 报价口径 ----
    quote_mode: str = QUOTE_ESTIMATE        # 渠道一口价 / 渠道预估价
    method: str = ADD                       # 报价方式
    markup_yuan: float = 0                  # ★ 报价金额 = 预估成本 + N（报给买家）
    limit_yuan: float = 0                   # ★ 单张限价 = 预估成本 + M；★ 乘上座位数才是传给上游的 maxPrice（总限价）
    capped: bool = True                     # 是否限价
    adjust: str = ADJUST_NONE               # 报价调整类型

    # ★★ 分档报价（对应票达人弹窗里的「动态报价加法」那张表）。
    #
    #   V5 里这张表挂在【供应路线】上（RoutePricing），用 basis 决定
    #   折扣率怎么算、调整乘在谁身上。我这里把它挂在规则上，
    #   语义一致：这条规则命中时，用这张表算价。
    #
    #   ★ 空列表 = 不用分档（退回 method + markup_yuan 的老算法）。
    #     这样老规则和数据不会被破坏。
    tiers: list = field(default_factory=list)
    # 分档的计价基数。★ 只在 tiers 非空时有意义。
    #   original / estimated / fixed_per_ticket / cost_plus_fixed
    basis: str = BASIS_ESTIMATED

    # ★★ 报价取整单位（分）。★ 同步自 V5 的 `rounding_fen`。
    #   V5 的业务设置里是 10（取整到 1 角），合法值只有 1 和 10。
    #   为什么要有：不取整会报出 4313 这种零头，V5 报 4310。
    #   算法见 _round_half_up —— 是四舍五入，不是向上取整。
    rounding_fen: int = 1

    created_seq: int = 0                    # 用于"同权重取最新"

    # ---------------------------------------------------------------- 命中
    def matches(self, *, now: time, brand: str = "", cinema: str = "",
                original_price_yuan: float | None = None, hall_name: str = "") -> bool:
        """这条规则是否命中。"""
        if not self.enabled:
            return False

        import unicodedata
        normalize = lambda value: unicodedata.normalize('NFKC', str(value)).casefold()
        hall = normalize(hall_name).strip()
        if (self.hall_keywords or self.excluded_hall_keywords) and not hall:
            return False
        if self.hall_keywords and not any(normalize(k) in hall for k in self.hall_keywords):
            return False
        if any(normalize(k) in hall for k in self.excluded_hall_keywords):
            return False

        # 生效时间段（支持跨零点，如 23:00 - 08:00）
        if not _in_time_range(now, self.time_start, self.time_end):
            return False

        if self.brands and brand not in self.brands:
            return False

        if self.excluded_cinemas and (cinema in self.excluded_cinemas):
            return False

        # 猫淘价区间：左闭右开，与页面提示一致（0 ≤ 原价 < 999）
        if original_price_yuan is not None:
            if not (self.price_left_yuan <= original_price_yuan < self.price_right_yuan):
                return False

        return True


def _parse_hms(text: str) -> time:
    parts = (text or "00:00:00").split(":")
    while len(parts) < 3:
        parts.append("0")
    return time(int(parts[0]) % 24, int(parts[1]) % 60, int(float(parts[2])) % 60)


def _in_time_range(now: time, start: str, end: str) -> bool:
    """
    时间区间判断。★ 必须支持跨零点 —— 夜间规则（23:00-08:00）很常见。
    """
    s, e = _parse_hms(start), _parse_hms(end)
    if s <= e:
        return s <= now <= e
    # 跨零点：23:00-08:00 表示 >=23:00 或 <=08:00
    return now >= s or now <= e


# ==================================================================== 校验
def validate_rule(rule: "QuoteRule") -> list:
    """
    校验一条规则是否自相矛盾。返回问题列表，空列表 = 合法。

    ★ 为什么要在【配置时】就校验：
      最典型的错误是「单张限价 < 报价金额」（算成总价后比较）。
      限价是给上游的出价上限，低于报价就撮不到货，下单会被 300005 拒。
      等下单失败才发现就晚了 —— 钱已经冻结、买家已经在等。
    """
    problems = []

    if not str(rule.name).strip():
        problems.append("规则名称不能为空")

    if rule.quote_mode not in QUOTE_MODES:
        problems.append(f"报价出票模式 {rule.quote_mode!r} 不是合法值")
    elif rule.quote_mode == QUOTE_SCENE:
        problems.append("「场景预估价」标注为维护中，暂不可用")
    if rule.method not in QUOTE_METHODS and rule.method not in METHOD_ALIASES:
        problems.append(f"报价方式 {rule.method!r} 不是合法值")
    if rule.adjust not in ADJUST_TYPES:
        problems.append(f"报价调整类型 {rule.adjust!r} 不是合法值")

    # ---- ★★ 分档表校验（逐条对齐 V5 `RoutePricing` 的模型校验）----
    #
    #   V5 的校验（models.py:67-89）：
    #     · lower_bps < upper_bps                    → 区间上限必须大于下限
    #     · 区间不能重叠                              → 折扣区间不能重叠
    #     · 固定加价的 basis 不能同时设百分比          → 固定加价不能同时设置百分比
    #     · 百分比 basis 不能同时设固定加价            → 百分比规则不能同时设置固定加价
    #
    #   ★ 为什么必须在【保存前】拦住重叠：
    #     落档是【按声明顺序】扫、命中第一个就返回（V5 就是这么写的）。
    #     区间重叠时，同一折扣率会命中哪一档取决于顺序 ——
    #     价格变得不可预测，而且看起来"能算出来"，最难查。
    tiers = getattr(rule, "tiers", None) or []
    if tiers:
        if getattr(rule, "basis", BASIS_ESTIMATED) not in BASIS_TYPES:
            problems.append(
                f"分档计价基数 {getattr(rule, 'basis', None)!r} 不是合法值")
        for i, t in enumerate(tiers):
            lo = int(getattr(t, "lower_bps", 0))
            hi = int(getattr(t, "upper_bps", 0))
            if lo >= hi:
                problems.append(f"分档第 {i + 1} 行：上限必须大于下限"
                                f"（{lo / 100:.0f}% - {hi / 100:.0f}%）")
            if not 0 <= lo <= 100000 or not 0 <= hi <= 100000:
                problems.append(f"分档第 {i + 1} 行：折扣率超出 0-1000%")
            if not -10000 <= int(getattr(t, "adjustment_bps", 0)) <= 10000:
                problems.append(f"分档第 {i + 1} 行：比例必须在 -100% ~ 100%")
        ordered = sorted(tiers, key=lambda x: int(getattr(x, "lower_bps", 0)))
        for left, right in zip(ordered, ordered[1:]):
            l_hi = int(getattr(left, "upper_bps", 0))
            r_lo = int(getattr(right, "lower_bps", 0))
            l_incl = bool(getattr(left, "upper_inclusive", False))
            if l_hi > r_lo or (l_incl and l_hi == r_lo):
                problems.append(
                    f"分档区间重叠：{int(getattr(left, 'lower_bps', 0)) / 100:.0f}%"
                    f"-{l_hi / 100:.0f}% 与 "
                    f"{r_lo / 100:.0f}%-"
                    f"{int(getattr(right, 'upper_bps', 0)) / 100:.0f}%")
        basis = getattr(rule, "basis", BASIS_ESTIMATED)
        has_bps = any(int(getattr(t, "adjustment_bps", 0)) for t in tiers)
        has_fen = any(int(getattr(t, "fixed_add_fen", 0)) for t in tiers)
        if basis in (BASIS_PER_TICKET, BASIS_COST_PLUS) and has_bps:
            problems.append("固定加价的基数不能同时用百分比（比例）")
        if basis not in (BASIS_PER_TICKET, BASIS_COST_PLUS) and has_fen:
            problems.append("百分比基数不能同时用固定加价（每张加价）")

    # ★ 核心约束：单张限价算出的【总限价】必须 ≥ 报价总额
    if rule.capped and rule.limit_yuan < rule.markup_yuan:
        problems.append(
            f"★ 单张限价（预估成本 +{rule.limit_yuan}）算出的总限价低于报价总额"
            f"（预估成本 +{rule.markup_yuan}）—— 上限盖不住报价，下单会被拒（300005）")

    # 猫淘价区间
    if rule.price_left_yuan >= rule.price_right_yuan:
        problems.append(
            f"猫淘价区间 [{rule.price_left_yuan}, {rule.price_right_yuan}) 为空 —— "
            f"左边界必须小于右边界")

    # ★★ 2026-10-14：有 channel 的规则【按渠道】匹配，不靠院线。
    #   不豁免的话，V5 那两条（万达报价/良票报价）会被判
    #   "匹配不上任何影院" —— 保存被拦下，而它们其实是好的。
    #   根因：校验原来只认 brands 一个维度，不认识 channel。
    if not rule.brands and not getattr(rule, "channel", ""):
        problems.append("未选择包含院线 —— 这条规则匹配不上任何影院"
                        "（若是按渠道生效的规则，请设置渠道）")

    for field_name in ('hall_keywords', 'excluded_hall_keywords'):
        values = getattr(rule, field_name)
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            problems.append('影厅关键词必须是非空文字列表')
    for field_name in ('fixed_cost_fen', 'minimum_price_fen'):
        value = getattr(rule, field_name)
        if (field_name == 'minimum_price_fen' or value is not None) and (type(value) is not int or value < 0):
            problems.append('预估成本和最低售价必须是非负整数分')
    if rule.fixed_cost_fen is not None and (rule.channel != 'WANDA'
            or type(rule.fixed_cost_fen) is not int or rule.fixed_cost_fen <= 0):
        problems.append('固定预估成本仅支持万达路线，且必须大于0')
    if rule.original_discount_bps is not None:
        if type(rule.original_discount_bps) is not int or not 1 <= rule.original_discount_bps <= 10000:
            problems.append('原价折扣必须大于0且不超过10折')
        if rule.tiers or rule.markup_yuan or rule.capped or rule.quote_mode != QUOTE_FIXED or rule.adjust != ADJUST_NONE:
            problems.append('原价折扣报价须使用渠道一口价、关闭限价，且不能同时使用分档或加价')
    elif rule.minimum_price_fen:
        problems.append('最低售价仅用于原价折扣报价')
    return problems


def validate_rules(rules: list) -> list:
    """校验一组规则，并检查名称重复 / 是否有规则启用。"""
    problems = []
    for rule in rules:
        for p in validate_rule(rule):
            problems.append(f"[{rule.name}] {p}")

    names = [r.name for r in rules]
    for n in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"[{n}] 规则名称重复")

    if not any(r.enabled for r in rules):
        problems.append("没有任何启用的规则 —— 报价会降级为按成本报，不赚钱")
    return problems


# ==================================================================== 结果
@dataclass
class QuoteResult:
    """一次报价的结论。上层照这个去调良票 API 和回买家。"""
    unit_price_fen: int              # ★ 每张报价（分）—— 报给买家
    total_price_fen: int             # ★ 整单报价（分）—— 报给买家
    price_mode: str                  # FIXED / LIMIT —— 传给良票 API
    max_price_fen: int | None        # 传给良票 API 的 maxPrice（分）；不限价时 None
    rule_name: str                   # 命中的规则名（便于排查）
    quote_mode: str
    adjust: str
    cost_basis: str                  # ★ 成本口径用了哪个字段（estimateAmount / totalAmount）
    cost_unit_fen: int               # 成本单价（分），用于核对
    cost_total_fen: int
    seat_count: int
    # ★★ 原价合计（分）—— 报价卡要显示「原价 X / 优惠后 Y」（2026-10-14 加）。
    #   为什么要放到这里：报价卡原来只从【识别结果】取原价
    #   （seat[].originalPriceFen / priceAllFen），而万达识别的 priceAllFen
    #   实测【从不返回】⇒ 万达单报价卡的原价恒为 0.00 元，
    #   买家看到「原价 0.00 / 优惠后 35.00」。
    #   成本侧本来就有原价（万达 area[].salesPrice / 良票 marketAmount），
    #   让它随引擎结果一起上来，报价卡才有得用。
    #   ★ 0 = 不知道原价 —— 宁可不显示，也不要编一个。
    original_total_fen: int = 0
    # ★ 毛利预估 = 报价 − 成本。正数才赚钱。
    margin_unit_fen: int = 0
    margin_total_fen: int = 0
    # ★★ 规则版本 —— 补这个是因为一个真实缺口：
    #   线上 V5 的报价记录里存着 pricing_rule_revision（真实值 "settings-52"），
    #   而我们只有 rule_name。
    #   ⇒ 规则改了之后，查不出"这笔报价是按哪一版规则算的"。
    #     买家问"为什么上次报 46 这次报 52"，没有版本就说不清。
    #   ★ 必须在带默认值的字段区（dataclass 不允许默认值字段
    #     夹在无默认值字段之前 —— 踩过这个 TypeError）。
    #   ★ 版本号由上层（设置层）给 —— 引擎是纯函数，不自己造版本。
    pricing_rule_id: str = ""
    pricing_rule_revision: str = ""
    # ★★ 2026-10-14：这一份是不是【二次报价】。
    #   为什么要标记：二次报价和首报的金额不同，事后对账要能分开
    #   （买家问"为什么涨价/降价"、以及"优惠到底生效了没"）。
    is_second_quote: bool = False
    notes: list = field(default_factory=list)

    def as_api_params(self) -> dict:
        """
        ★ 只产出【要传给良票 API 的字段】，不产出别的。
        这样调用方不可能漏传或多传 —— priceMode 和 maxPrice 必须成对出现。
        """
        params = {"priceMode": self.price_mode}
        if self.max_price_fen is not None:
            params["maxPrice"] = self.max_price_fen   # 数字，不是字符串（文档明确）
        return params

    def summary(self) -> str:
        return (f"成本 {self.cost_total_fen / 100:.2f} → "
                f"报价 {self.total_price_fen / 100:.2f} "
                f"(毛利 {self.margin_total_fen / 100:+.2f}) "
                f"[{self.rule_name} / {self.price_mode}]")


# ==================================================================== 报价模式
# ★★★ 2026-10-14：「优先报价模式」接进规则匹配。
#
#   界面上的「优先报价模式」是一个可排序清单（靠上优先、可停用）：
#       简易报价规则 / 专业报价规则 / API报价规则 / 比价模式
#
#   ★ 店主确认的语义（这句话是这次设计的唯一依据）：
#       "现在我们设计的报价规则就是简易报价规则
#        专业报价规则更加复杂，我没有设计"
#     ⇒ 只有「简易」有实现；其余三个是曾经设想过的方向。
#
#   ⇒ 所以这个控件的作用是【决定用哪一套规则】：
#       取清单里第一个“已启用且已实现”的模式，用它的规则集报价。
#       因为现在只有一种模式，所以行为与以前完全一致（所有规则都参与）。
#       等真要做「专业报价」时，只需在这里给它一份规则集，
#       控件立刻就是真的 —— 不用再改界面和配置。
#
#   ★ 为什么不直接把没实现的删掉：
#     删了以后没人知道规划过这几条路；标着「未实现」才诚实。
#     （界面会据此显示「未实现」并且不允许拖动/启用。）

MODE_SIMPLE = "简易报价规则"
MODE_PRO = "专业报价规则"
MODE_API = "API报价规则"
MODE_COMPARE = "比价模式"

# 已实现的模式 → 它用哪些规则。
# ★ None = 全部规则（「简易」就是我们现在这套：6 条种子规则按 weight 降序）。
#   ★★ 这一条很关键：只有「简易」返回全部，
#      所以加了这套筛选之后，报价结果与改动前【逐分一致】。
MODE_RULES: dict[str, Any] = {
    MODE_SIMPLE: None,      # 全部规则 —— 现在唯一实现的模式
    # MODE_PRO / MODE_API / MODE_COMPARE 未实现：不进这张表。
    #   代码里查不到 ⇒ 当成"未实现"处理（不会静默按别的模式算）。
}


def implemented_modes() -> list:
    """已实现的报价模式。界面据此把其余的标成「未实现」。"""
    return list(MODE_RULES)


def select_rules_by_priority(rules: list, priority: list) -> tuple:
    """
    按「优先报价模式」挑出该用哪套规则。

    返回 (过滤后的规则, 用到的模式名, 说明文字)。

    ★ 规则（都有测试盯着）：
      ① 按清单顺序找【第一个 enabled 且 implemented】的模式
      ② 一个都没有 ⇒ 返回全部规则 + 说明「未指定模式」，
         ★ 不返回空规则集：那会让所有报价掉进兜底（按成本卖），
           是"静默少赚"最典型的形态。
      ③ 命中的模式若在 MODE_RULES 里是 None ⇒ 全部规则
      ④ 未实现的模式即使排在前面且 enabled 也被跳过（不能假装支持）
    """
    for name in (priority or []):
        if name not in MODE_RULES:            # 未实现 —— 跳过，不静默当别的用
            continue
        picked = MODE_RULES[name]
        if picked is None:
            return list(rules), name, ""
        allow = set(picked)
        return ([r for r in rules if getattr(r, "name", "") in allow],
                name, "")
    return list(rules), "", "没有可用的报价模式（未指定或全都未实现），按全部规则报价"


# ==================================================================== 引擎
class QuoteRuleEngine:
    """
    规则引擎。

    ★ 设计成纯函数式：给成本 + 上下文，返回报价。
      不碰网络、不碰数据库。这样才测得动。
    """

    def __init__(self, rules: list[QuoteRule], *,
                 rule_id: str = "runtime-config",
                 revision: str = ""):
        """
        rules：规则列表。
        rule_id / revision：★ 这套规则的标识与版本，会写进每份 QuoteResult。
            ★ 为什么由外部给而不是引擎自己造：
              引擎是纯函数，不该有时间/全局状态的概念。
              版本号是【设置层】的事 —— 运营改一次规则就该换个版本。
              （线上 V5 的真实值形如 "settings-52"，就是这个意思。）
        """
        # 排序一次即可：权重降序，同权重按 created_seq 降序（最新优先）
        self.rules = sorted(rules, key=lambda r: (r.weight, r.created_seq), reverse=True)
        self.rule_id = rule_id
        self.revision = revision

    @staticmethod
    def _fallback_max_price(cost_total_fen: int, seat_count: int,
                            original_total_fen: int | None,
                            limit_yuan: float | None = None) -> int:
        """
        没命中规则时的兜底限价（分）。

        ★ 全部来自已有数据，不拍脑袋：
            base = 成本 + limit_yuan × 张数
            封顶 = 市场原价合计（买家自购价）—— 若拿得到且更低

        ★★ 2026-10-14：limit_yuan 变成参数（原来写死 FALLBACK_LIMIT_YUAN）。
            店主原话："后台不要硬编码 由这里控制" ——
            那个"这里"就是设置页的「出票溢价 → 调整限价 / 溢价上限」。
            ★ 默认 None ⇒ 用 FALLBACK_LIMIT_YUAN（= 原来那个 3），
              所以不传参数的调用方行为完全不变。

        ★ 为什么必须封顶：
          原价是买家自己买得到的价格。我们出价高过它就意味着
          「我们替买家买，比他直接买还贵」—— 这种单不如不做。
          所以兜底限价永远不超过原价。

        ★ 为什么不能只要原价、不要 base：
          原价可能拿不到（识别没给 originalPriceFen），那时 base 就是唯一依据。
          两个都在时取较小值 = 既覆盖波动，也不越过自购价。
        """
        per_ticket = (FALLBACK_LIMIT_YUAN if limit_yuan is None
                      else max(0.0, float(limit_yuan)))
        base = int(cost_total_fen) + int(round(per_ticket * FEN_PER_YUAN)) \
            * int(seat_count)
        if original_total_fen:
            return min(base, int(original_total_fen))
        return base

    # ---------------------------------------------------------------- 匹配
    def second_quote(self, base: "QuoteResult", *, discount_percent: int,
                     seat_count: int | None = None) -> "QuoteResult":
        """
        二次报价：在已报价的基础上让一点利，**且保证不亏**。

        ★ 优惠 = min(报价 × 比例, 毛利) —— 最多把毛利让完。
          最坏"不赚钱"，绝不"倒贴"。
        ★ 毛利 ≤ 0 ⇒ 原样返回（不给优惠，再减就是亏）。
        ★ 返回【新的】QuoteResult，不改原来那个 ——
          原来的报价记录要留着（"第一次报了多少"是可追溯的事实）。
        ★ 纯函数：不读配置、不看时间。比例由调用方按配置传进来。
        """
        from dataclasses import replace as _replace
        try:
            total = int(getattr(base, "total_price_fen", 0) or 0)
            margin = int(getattr(base, "margin_total_fen", 0) or 0)
        except (TypeError, ValueError):
            return base
        if total <= 0:
            return base
        pct = max(0, min(int(discount_percent or 0), 100))
        if pct <= 0:
            return base
        if margin <= 0:
            # ★ 没毛利就不减 —— 这一条是"不亏钱"的关键，别删。
            # ★★ 而且必须【原样返回同一个对象】（return base），
            #   不能用 _replace 造一个只是 notes 不同的新对象 ——
            #   调用方是靠「new_quote is quote」判断有没有真的优惠的，
            #   造新对象会让它以为让了利，于是发出
            #   "已再优惠 3%" 而价格根本没变 ⇒ **那是骗买家**。
            #   （实测踩到：毛利为 0 时消息里写着优惠 3%，金额一分没少。）
            return base
        want = total * pct // 100              # 想减多少
        cut = min(want, margin)                # 但最多减掉毛利
        if cut <= 0:
            return base
        new_total = total - cut
        n = int(seat_count or getattr(base, "seat_count", 0) or 0)
        new_unit = (new_total // n) if n > 0 else int(
            getattr(base, "unit_price_fen", 0) or 0)
        notes = list(base.notes) + [
            f"二次报价：优惠 {discount_percent}%（想减 {want} 分，"
            f"实际减 {cut} 分"
            + ("—— 减到毛利为止，没有再往下" if cut < want else "")
            + f"）；{total} → {new_total} 分",
            "★ 优惠最多让完毛利，不亏钱。"]
        # ★ 限价也要跟着降 —— 否则改价时会按旧限价去改，
        #   而旧限价比新报价高，等于优惠没生效。
        new_max = getattr(base, "max_price_fen", None)
        if new_max is not None:
            try:
                new_max = max(0, int(new_max) - cut)
            except (TypeError, ValueError):
                new_max = None
        return _replace(base, total_price_fen=new_total,
                        unit_price_fen=new_unit,
                        max_price_fen=new_max,
                        margin_total_fen=margin - cut,
                        margin_unit_fen=(margin - cut) // n if n > 0
                        else int(getattr(base, "margin_unit_fen", 0) or 0),
                        is_second_quote=True,
                        notes=notes)


    def find_rule(self, *, now: time, brand: str = "", cinema: str = "",
                  original_price_yuan: float | None = None,
                  channel: str = "", hall_name: str = "") -> QuoteRule | None:
        """
        按优先级找第一条命中的规则。

        ★ channel（2026-10-14 加）：本次报价走的是哪条渠道。
          · channel="" → 只看【不限渠道】的规则（保持老行为：
            不带 channel 的调用方不会突然命中渠道规则）
          · channel="WANDA" → 不限渠道的 + WANDA 专属的，一起按权重排
          ★ 为什么渠道规则要能压过品牌规则：
            V5 的 wanda_pricing 权重（我们要给它高权重）本来就是
            "万达渠道一律 +1 元/张"，比按院线分档更具体，
            更具体的规则必须先命中。
        """
        for rule in self.rules:
            if rule.channel and rule.channel != channel:
                continue
            if rule.matches(now=now, brand=brand, cinema=cinema,
                            original_price_yuan=original_price_yuan, hall_name=hall_name):
                return rule
        return None

    # ---------------------------------------------------------------- 报价
    def quote(self, *, seat_count: int, now: time,
              estimate_total_fen: int | None = None,
              total_amount_fen: int | None = None,
              cost_ceiling_fen: int | None = None,
              brand: str = "", cinema: str = "",
              original_total_fen: int | None = None,
              discount_bps: int = 0,
              channel: str = "",
              hall_name: str = "",
              channel_fee_fen: int = 0,
              fallback_mode: str = QUOTE_ESTIMATE,
              fallback_capped: bool = True,
              fallback_limit_yuan: float | None = None) -> QuoteResult:
        # ★★ 2026-10-14 新增两个参数：兜底报价的模式与是否限价。
        #   ★ 为什么用参数而不是在这里读配置：
        #     本模块是【纯领域层】，读配置会让它依赖外层（架构审计会抓）。
        #     调用方（应用层）从配置取好再传进来。
        #   ★ 默认值 = 现有写死的那套（预估 + 限价）
        #     ⇒ 不传的调用方【行为完全不变】。
        # ★★ fallback_limit_yuan（2026-10-14 再加一个）：
        #     兜底时"预估成本上增加多少元/张"。
        #     来自「设置 → 出票溢价 → 溢价上限」，替掉写死的 FALLBACK_LIMIT_YUAN=3。
        #     ★ 规则命中时用不到它（那边用规则自己的「单张限价」）——
        #       两者是不同层次：规则管"这条规则加多少"，它管"没规则时兜底加多少"。
        #     ★ 默认 None ⇒ 用 FALLBACK_LIMIT_YUAN，不传的调用方行为不变。
        """
        算报价。

        ★ 成本口径（两个都给的话优先 estimate）：
            estimate_total_fen  良票 preflight 的 estimateAmount —— 预估应付。
                                 文档：LIMIT 模式这是"参考"，实际按放映后结算。
                                 **这个才是我们的成本**，因为结算按实际成交价。
            total_amount_fen    良票 preflight 的 totalAmount —— FIXED 时即成交价。

        ★★ cost_ceiling_fen（2026-10-14 新增）：这张单【平台可能要付到的上限】。
            专门用来算 maxPrice，不参与"报价"本身。
            为什么不能只按成本算 maxPrice：跨区座位时
            estimateAmount（均价口径）会明显小于 totalAmount（限价口径），
            按前者反推的 maxPrice 可能低于平台认定的实际应付，
            下单直接 300005 失败（店主实测：两个座位价格不一样就建单失败）。
            ⇒ 传 preflight.price_ceiling_fen，让限价覆盖实际应付。

        其余参数：
            seat_count          座位数
            original_total_fen  市场原价合计（分）—— 用于猫淘价区间匹配
            discount_bps        折扣档位（万分比），"动态折扣加法" 用它
        """
        if seat_count <= 0:
            raise ValueError("座位数必须大于 0")

        # ---- 成本口径 ----
        if estimate_total_fen is not None:
            cost_total, cost_basis = estimate_total_fen, "estimateAmount"
        elif total_amount_fen is not None:
            cost_total, cost_basis = total_amount_fen, "totalAmount"
        else:
            raise ValueError("必须给 estimate_total_fen 或 total_amount_fen 之一")
        if cost_total < 0:
            raise ValueError("成本不能为负")

        cost_unit = _div_round(cost_total, seat_count)
        original_yuan = (original_total_fen / FEN_PER_YUAN
                         if original_total_fen is not None else None)

        rule = self.find_rule(now=now, brand=brand, cinema=cinema,
                              original_price_yuan=original_yuan,
                              channel=channel, hall_name=hall_name)
        if rule is not None and rule.fixed_cost_fen is not None:
            cost_unit = rule.fixed_cost_fen
            cost_total = cost_unit * seat_count
            cost_basis = 'WANDA_RULE_FIXED_COST'
        if rule is not None and rule.original_discount_bps is not None:
            problems = validate_rule(rule)
            if problems:
                raise ValueError('; '.join(problems))
            if not original_total_fen or original_total_fen <= 0:
                raise ValueError('原价折扣报价缺少官方原价')
            unit = max(cost_unit, rule.minimum_price_fen,
                       _div_round(original_total_fen * rule.original_discount_bps, seat_count * 10000))
            return QuoteResult(unit_price_fen=unit, total_price_fen=unit * seat_count,
                price_mode='FIXED', max_price_fen=None, rule_name=rule.name,
                quote_mode=rule.quote_mode, adjust=rule.adjust, cost_basis=cost_basis,
                cost_unit_fen=cost_unit, cost_total_fen=cost_total, seat_count=seat_count,
                original_total_fen=original_total_fen, margin_unit_fen=unit-cost_unit,
                margin_total_fen=(unit-cost_unit)*seat_count, pricing_rule_id=rule.name,
                pricing_rule_revision=self.revision,
                notes=[f'规则原价折扣：{rule.original_discount_bps}/10000；最低售价{rule.minimum_price_fen}分；成本{cost_unit}分/张'])
        if rule is None:
            # 没规则命中 —— 按成本报，不加价。
            # ★ 不抛异常是刻意的：宁可少赚，也不能因为没配规则就报不出价。
            #
            # ★★ 2026-10-14 接线：「设置 → 兜底报价 → 模式 / 是否限价」
            #   这一段就是"兜底报价"。原来写死 price_mode="LIMIT" + 有兜底限价。
            #   现在由调用方按配置传进来：
            #     fallback_mode   = 预估限价 → LIMIT；一口价 → FIXED
            #     fallback_capped = 启用限价 → 给 maxPrice；不启用 → None
            #   ★ 默认值就是原来写死的那套 ⇒ 不传的调用方行为不变。
            _fb_max = (self._fallback_max_price(
                cost_total, seat_count, original_total_fen,
                limit_yuan=fallback_limit_yuan)
                if fallback_capped else None)
            # ★ 报价模式：FIXED = 报价即成交价（一口价）；LIMIT = 报价是上限。
            #   ★ 这不是"随便选一个"：FIXED 时我们承诺的就是这个价，
            #     LIMIT 时实扣以放映后结算为准。选错会影响买家预期。
            _fb_price_mode = ("FIXED" if fallback_mode == QUOTE_FIXED
                              else "LIMIT")
            _fb_notes = [f"没有规则命中，按成本报（兜底报价：{fallback_mode}）"]
            if _fb_max is not None:
                _per = (FALLBACK_LIMIT_YUAN if fallback_limit_yuan is None
                        else max(0.0, float(fallback_limit_yuan)))
                _fb_notes.append(
                    f"★ 兜底限价 {_fb_max} 分（成本 {cost_total} + "
                    f"{_per}/张，且不超过市场原价 "
                    f"{original_total_fen or '未知'}）")
            else:
                # ★ 不启用限价要【说清楚】，因为这是"钱上不封顶"——
                #   出问题时排查要知道当时是故意不封顶的。
                _fb_notes.append(
                    "★ 兜底限价【未启用】—— maxPrice=None（钱上不封顶），"
                    "实扣可能超过报价。这是「兜底是否限价」配成"
                    "「不启用限价」的结果。")
            return QuoteResult(
                unit_price_fen=cost_unit, total_price_fen=cost_total,
                price_mode=_fb_price_mode,
                max_price_fen=_fb_max,
                rule_name="（无命中规则，按成本报）",
                quote_mode=fallback_mode,
                adjust=ADJUST_NONE, cost_basis=cost_basis,
                # ★ 没命中规则时：规则标识留空（没有哪条规则被用），
                #   但【版本照记】—— 它是"当时用的是哪一版规则集"，
                #   排查"为什么当时没规则命中"要靠它。
                pricing_rule_id="",
                pricing_rule_revision=self.revision,
                cost_unit_fen=cost_unit, cost_total_fen=cost_total,
                seat_count=seat_count,
                # 原价合计随结果上浮 —— 报价卡要用（见 QuoteResult 注释）
                original_total_fen=int(original_total_fen or 0),
                notes=_fb_notes)

        notes = []
        # ---- 1. 报价金额：预估成本 + N → 报给买家 ----
        #
        # ★★ 两条路，分档优先：
        #   ① 规则带分档表（tiers 非空）→ 走 V5 那套分档算法
        #      折扣率 = base × 10000 / 官方原价，落档后按 basis 算
        #   ② 没有分档表 → 走老的"成本 + markup_yuan"
        #   两条都保留，老规则/老数据不受影响。
        tier_note = ""
        tier_hit = None
        if getattr(rule, "tiers", None) and original_total_fen:
            pr = price_by_tier(rule, cost_total=cost_total, cost_unit=cost_unit,
                               seat_count=seat_count,
                               original_total=original_total_fen,
                               step=rule.rounding_fen,
                               # ★ V5 的万达分档（fixed_per_ticket）拿它当每张下限
                               channel_fee_fen=channel_fee_fen)
            tier_hit = pr.get("tier")
            if pr["status"] == "PRICED":
                unit = pr["total_fen"] // max(1, seat_count)
                tier_note = pr["note"]
                notes.append(tier_note)
            else:
                # ★ 分档算不出来【不能悄悄退回老算法】——
                #   那会让"配置错了"表现成"价格正常"，最难查。
                #   ⇒ 明确记一笔，并退回成本价（不亏本），由上层决定要不要报。
                notes.append(f"★ 分档报价未生效（{pr['status']}）：{pr['note']}"
                             " —— 已按成本价处理，请检查分档表")
                markup_fen = 0
                unit = cost_unit
        else:
            markup_fen = int(round(rule.markup_yuan * FEN_PER_YUAN))
            if rule.method in (DYNAMIC_DISCOUNT, DYNAMIC_DISCOUNT_ADD):
                # 动态折扣（老算法，无分档表）：在加价基础上，按折扣档位再让一点
                # ★ 注意：这是【线性让利】，不是 V5 的分档表。
                #   只有没配 tiers 的规则才会走到这里。
                delta = _dynamic_delta_bps(markup_fen, discount_bps)
                markup_fen += delta
                notes.append(f"动态折扣（线性，无分档表）："
                             f"报价金额 {rule.markup_yuan} 元，"
                             f"折扣 {discount_bps} bps 后加价 "
                             f"{markup_fen / FEN_PER_YUAN:+.4g} 元")
            unit = cost_unit + markup_fen

        if unit < cost_unit:
            notes.append("★ 报价金额为负加价，低于成本 —— 已回退到成本价")
            unit = cost_unit

        # ---- 2. 报价调整类型 ----
        unit = _adjust(unit, rule.adjust)
        if unit < cost_unit:
            # ★ 取整可能把价格压到成本以下（如"到元-0.1"）。宁可回退，不做亏本买卖。
            unit = cost_unit
            notes.append("取整后低于成本，已回退到成本价")

        # ★★ V5 同步：报价取整 + 上界夹逼
        #
        #   来源：V5 `domain/quote/pricing.py` 的 _round / _bounded / fixed_per_ticket。
        #   用线上真实记录验证过（成本4500×4、加价100、step10 → 4600/张、18400 总，
        #   与 quotes.sqlite3 里的记录完全一致）。
        #
        #   为什么要这两步：
        #     ① 不取整会报出 4313 这种零头，而 V5 报 4310。
        #        零头会让买家觉得是随手算的，也不好核对。
        #     ② 不夹逼可能【卖超官方原价】——买家一比对猫眼/淘票票就会炸。
        #        V5 的 _bounded 把售价压在 [成本, 官方原价] 之间，
        #        两者冲突时【报错停报】而不是出错价。
        step = int(getattr(rule, "rounding_fen", None) or 1)
        if step not in (1, 10):
            step = 1
        if step > 1:
            before = unit
            unit = _round_half_up(unit, 1, step)
            # ★ 取整只许往上或持平？不 —— V5 是四舍五入（4313→4310）。
            #   但取整后不能低于成本：那是亏本。
            if unit < cost_unit:
                unit = _round_half_up(cost_unit, 1, step)
                if unit < cost_unit:
                    unit = unit + step
            if unit != before:
                notes.append(f"报价取整到 {step} 分：{before} → {unit}")

        # ★ 上界：不许超过官方原价（original_total_fen 是整单原价）
        if original_total_fen:
            try:
                original_unit = int(original_total_fen) // max(1, seat_count)
            except (TypeError, ValueError):
                original_unit = 0
            if original_unit > 0 and unit > original_unit:
                notes.append(
                    f"★ 售价 {unit} 分超过官方原价 {original_unit} 分 —— 已压回原价"
                    f"（卖超原价买家会比对官方渠道）")
                unit = _floor_to(original_unit, step)

        # ★ 成本高于原价 = 规则冲突，宁可不报也不出错价
        if original_total_fen and cost_unit > 0:
            try:
                original_unit = int(original_total_fen) // max(1, seat_count)
            except (TypeError, ValueError):
                original_unit = 0
            if original_unit > 0 and cost_unit > original_unit:
                # ★ 照本文件既有的做法（限价低于报价那里）—— 记 note 不抛异常。
                #   抛异常会让整个回复挂掉，买家收到的是"模型暂时不可用"，
                #   比"回退到成本价"更糟。
                notes.append(
                    f"★★ 规则冲突：成本 {cost_unit / FEN_PER_YUAN:.2f} 元高于官方原价 "
                    f"{original_unit / FEN_PER_YUAN:.2f} 元 —— 无法既不加亏又不超原价。"
                    f"已按成本价报，请店家核对这单。")
                unit = cost_unit
                total = unit * seat_count
                margin_unit = unit - cost_unit
                return QuoteResult(
                    unit_price_fen=unit, total_price_fen=total,
                    price_mode=PRICE_MODE_OF_QUOTE.get(rule.quote_mode, "LIMIT"),
                    max_price_fen=None, rule_name=rule.name,
                    quote_mode=rule.quote_mode, adjust=rule.adjust,
                    pricing_rule_id=rule.name,
                    pricing_rule_revision=self.revision,
                    cost_basis=cost_basis, cost_unit_fen=cost_unit,
                    cost_total_fen=cost_total, seat_count=seat_count,
                    # 原价合计随结果上浮 —— 报价卡要用（见 QuoteResult 注释）
                    original_total_fen=int(original_total_fen or 0),
                    margin_unit_fen=margin_unit,
                    margin_total_fen=margin_unit * seat_count, notes=notes)

        total = unit * seat_count

        # ---- 3. 单张限价 → 给上游的 maxPrice ----
        price_mode = PRICE_MODE_OF_QUOTE.get(rule.quote_mode, "LIMIT")
        if rule.quote_mode == QUOTE_SCENE:
            notes.append("场景预估价标注为维护中，按渠道预估价处理")
        max_price = None
        if rule.capped:
            _limit_fen = int(round(rule.limit_yuan * FEN_PER_YUAN))
            limit_unit = cost_unit + _limit_fen
            # ★★★ 2026-10-14 跨区座位：查了一天，结论是【老算法是对的】。
            #
            #   一度以为要改成"按 totalAmount（限价上限）算 maxPrice"，
            #   理由是文档 1182「maxPrice 总限价，实际应付超过时下单失败 300005」
            #   加上跨区时 totalAmount(8697) 远大于 estimateAmount(7509)。
            #
            #   ★ 但【真接口实测推翻了它】（场次 38208129，4990 区 + 4290 区各一座）：
            #       老算法 maxPrice = (7509//2 + 50) × 2 = 7608
            #       /order/create → code=0 成功，freezeAmount=7608
            #     ⇒ maxPrice 判的是【实收金额】（按分区逐座算出来多少），
            #       不是 totalAmount 那个"限价上限"。
            #       老算法按 estimateAmount（预估应付）+ 限价 得出的数，
            #       本来就 ≥ 实收，不会触发 300005。
            #
            #   ★ 为什么不干脆改成按 totalAmount 算（更"宽"）：
            #       实测 freezeAmount == maxPrice —— LIMIT 模式【冻结额就是限价】。
            #       限价抬高 = 白冻更多余额（虽然结算后会退），没有任何收益。
            #       ⇒ 保持老算法，只要它 ≥ 实收就够了。
            #
            #   ★ 那 cost_ceiling_fen 留着干嘛：当【预警】。
            #       上限显著高于成本时说明这单跨区、实收可能接近上限，
            #       若上限已经高过市场原价，那这单可能不赚甚至倒挂。
            #       只记 note 让人看得见，不改钱。
            max_price = limit_unit * seat_count
            if cost_ceiling_fen and int(cost_ceiling_fen) > cost_total:
                _ceiling = int(cost_ceiling_fen)
                notes.append(
                    f"★ 跨区座位：平台限价上限 {_ceiling} 分 > 均价口径成本 "
                    f"{cost_total} 分（差 {_ceiling - cost_total} 分）。"
                    f"实收按分区逐座算，我们按预估 + 限价定 maxPrice"
                    f"（{max_price} 分）")
                _orig = int(original_total_fen or 0)
                if _orig and _orig < _ceiling:
                    # ★ 这条最要紧：应付上限已经高过市场原价 ⇒ 可能倒挂
                    notes.append(
                        f"★★ 可能倒挂：市场原价 {_orig} 分【低于】平台应付上限 "
                        f"{_ceiling} 分 —— 这单卖出去可能不赚甚至亏")
            if max_price < total:
                # ★ 限价低于报价 = 规则自相矛盾，下单必被 300005 拒。
                #   这里明确报出来，而不是等下单失败。
                notes.append(f"★ 单张限价（{limit_unit / FEN_PER_YUAN:.2f} 元）"
                             f"低于报价（{unit / FEN_PER_YUAN:.2f} 元），"
                             f"下单会被拒（300005）")

        margin_unit = unit - cost_unit
        return QuoteResult(
            unit_price_fen=unit, total_price_fen=total,
            price_mode=price_mode, max_price_fen=max_price,
            rule_name=rule.name, quote_mode=rule.quote_mode, adjust=rule.adjust,
            # ★ 规则版本：写进报价记录后，"这价怎么算的"才追得回来
            pricing_rule_id=rule.name,
            pricing_rule_revision=self.revision,
            cost_basis=cost_basis,
            cost_unit_fen=cost_unit, cost_total_fen=cost_total,
            seat_count=seat_count,
            # 原价合计随结果上浮 —— 报价卡要用（见 QuoteResult 注释）
            original_total_fen=int(original_total_fen or 0),
            margin_unit_fen=margin_unit, margin_total_fen=margin_unit * seat_count,
            notes=notes)


# ==================================================================== 工具
def _div_round(numerator: int, denominator: int) -> int:
    """整数除法四舍五入。"""
    return (numerator * 2 + denominator) // (denominator * 2)


def _round_half_up(numerator: int, denominator: int, step: int) -> int:
    """
    ★★ 照 V5 `domain/quote/pricing.py:_round` 原样搬。

        一次 half-up，整数运算，不做二次舍入。

    ★ 为什么不能写成 ((v + step//2) // step) * step：
      那在 v 是 step 的整数倍时会多进一格（4600 → 4610）。
      V5 这个式子对 4313 给 4310、对 4667 给 4670 —— 是四舍五入，不是向上取整。
      我用线上真实记录验过（成本4500、加价100、step10 → 4600）。
    """
    return (numerator * 2 + denominator * step) // (2 * denominator * step) * step


def _floor_to(value: int, step: int) -> int:
    """向下取整到 step 的整数倍。"""
    if step <= 1:
        return value
    return value // step * step


# ============================================================ 分档报价（V5 对齐）
def discount_bps_of(base_fen: int, original_fen: int) -> int:
    """
    折扣率（bps）—— 就是分档表的「折扣率」那一列。

        ★★ 定义（照 V5 `pricing.py:_tier` 反推，见 PricingTier 的注释）：

            折扣率 = base × 10000 / original      ← 两个【金额】的比值

        V5 的判据是 `row.lower_bps * original <= base * 10000`，
        两边同除 original 就是上面这个式子。所以折扣率不是上游给的、
        也不是人工填的，而是【成本相对官方原价的比值】。

    ★ 为什么单独抽成一个函数：这是整张表的分档依据，
      它算错了后面全错。抽出来能被测试直接盯住。

    ★ original_fen <= 0 时返回 0（没有原价就没法算折扣率）。
      调用方应当在此之前拦住"缺原价"的情况 —— 但这里不抛异常，
      免得一个缺字段把整条报价链打断。
    """
    if original_fen <= 0:
        return 0
    return base_fen * 10_000 // original_fen


def pick_tier(tiers: list, base_fen: int, original_fen: int):
    """
    按折扣率落档 —— 逐字对齐 V5 `domain/quote/pricing.py:11-17`。

        def _tier(configured, base, original):
            for row in configured.tiers:
                value = base * 10000
                if row.lower_bps * original <= value and (
                        value < row.upper_bps * original or
                        (row.upper_inclusive and value == row.upper_bps * original)):
                    return row
            raise PricingError('PRICING_TIER_NOT_FOUND', ...)

    ★ 注意 V5 是【按声明顺序】扫一遍就返回，不是先排序再二分 ——
      照抄，免得顺序语义跟 V5 不一致。规则保存时会按 lower_bps 排好。
    ★ 落不到档返回 None（调用方决定是报错还是退化），
      这里不抛 PricingError —— 我们的错误处理走 Result，不走异常。
    """
    if not tiers or original_fen <= 0:
        return None
    value = base_fen * 10_000
    for row in tiers:
        low = getattr(row, "lower_bps", 0) * original_fen
        high = getattr(row, "upper_bps", 10_000) * original_fen
        if low <= value and (value < high or
                             (getattr(row, "upper_inclusive", False)
                              and value == high)):
            return row
    return None


def _tier_params(rule, *, cost_total: int, cost_unit: int,
                 original_total: int) -> dict:
    """
    按 basis 决定「折扣率的 base」和「调整乘在谁身上」。

    ★★ 逐字对齐 V5 `pricing.py:62-100` 的分支：

        basis="original"          base=原价        调整乘在【原价】上
        basis="estimated"         base=预估成本    调整乘在【成本】上   ← V5 注释：base=成本
        basis="fixed_per_ticket"  base=单张成本    每张加 fixed_add_fen
        basis="cost_plus_fixed"   base=整单成本    整单加 fixed_add_fen×张数

    ★ 这里最容易写错的是 estimated 那个 —— V5 第 99-100 行：

          base = original if configured.basis == 'original' else cost.total_cost_fen
          numerator = cost.total_cost_fen * 10000 + base * row.adjustment_bps

      ⇒ original 用原价当调整基数，**其他都用成本**。
        我第一版想当然写成"都用原价"，是错的。
    """
    basis = getattr(rule, "basis", BASIS_ESTIMATED)
    if basis == BASIS_ORIGINAL:
        return {"base_fen": original_total, "adjust_on": original_total,
                "per_ticket": False}
    if basis == BASIS_ESTIMATED:
        return {"base_fen": cost_total, "adjust_on": cost_total,
                "per_ticket": False}
    if basis == BASIS_PER_TICKET:
        return {"base_fen": cost_unit, "adjust_on": cost_unit,
                "per_ticket": True}
    # cost_plus_fixed
    return {"base_fen": cost_total, "adjust_on": cost_total,
            "per_ticket": False}


def price_by_tier(rule, *, cost_total: int, cost_unit: int, seat_count: int,
                  original_total: int, step: int = 1,
                  channel_fee_fen: int = 0) -> dict:
    """
    用规则的分档表算总售价（分）。

    ★★ 逐字对齐 V5 的四个分支（`pricing.py:71-106`）：

      fixed_per_ticket:
          每张 = _round(base + fixed_add_fen, 1, step)
          再 _bounded(base + channel_fee, 原价, step)
          总价 = 各张之和
        ★ V5 要求「每张票对应原价」齐全才肯算；我们这里用单张均价近似 ——
          这点【与 V5 不一致】，只在 V5 那条分支上，且我们目前不走万达
          会员价那条路（source != WANDA_VIP_FIXED_COST）。

      cost_plus_fixed:
          分子 = 成本 + fixed_add_fen × 张数
          step==10 时：总价 = _round(分子, 张数, step) × 张数（先算单张再乘）
          否则：      总价 = _round(分子, 1, step)

      original:
          总价 = _round(成本×10000 + 原价×adjustment_bps, 10000, step)

      estimated:
          总价 = _round(成本×10000 + 成本×adjustment_bps, 10000, step)

    ★ 与 V5 的差异（诚实写出来，不含糊）：
      · V5 走异常（PricingError），我们返回 status/notes —— 我们不抛异常出这个层。
      · V5 会拿原价当上界夹逼（_bounded）并在冲突时报错；
        我们只保证【不亏本】（≥成本），上界交给调用方已有的夹逼逻辑。
        理由：我们的调用链已经有"取整后低于成本就回退"的处理，不重复。

    返回 {"status": "PRICED"|"TIER_NOT_FOUND"|"ORIGINAL_REQUIRED"|"INVALID",
          "total_fen": int, "tier": row|None, "discount_bps": int, "note": str}
    """
    tiers = getattr(rule, "tiers", None) or []
    if not tiers:
        return {"status": "INVALID", "total_fen": cost_total, "tier": None,
                "discount_bps": 0, "note": "规则没有分档表"}
    if original_total <= 0:
        return {"status": "ORIGINAL_REQUIRED", "total_fen": cost_total,
                "tier": None, "discount_bps": 0,
                "note": "缺官方原价，无法算折扣率"}

    p = _tier_params(rule, cost_total=cost_total, cost_unit=cost_unit,
                     original_total=original_total)
    # ★★★ 2026-10-14 修：折扣率必须【整单成本 ÷ 整单原价】。
    #
    #   原来写的是 discount_bps_of(p["base_fen"], original_total)：
    #   对 fixed_per_ticket 来说 p["base_fen"] 是【单张】成本（cost_unit），
    #   而 original_total 是【整单】原价 —— 两个口径不一致，比值就是错的。
    #
    #   实测（万达渠道，成本 6280 / 原价 7180，2 张）：
    #       错：单张 3140 ÷ 整单 7180 = 43.73%   ← 日志里显示的就是这个
    #       对：整单 6280 ÷ 整单 7180 = 87.47%
    #   ⇒ 两个后果：
    #     ① 日志/界面上的折扣率是错的，运营据此判断会误判；
    #     ② 档位选择也用错基数（pick_tier 同一个 bug）——
    #        万达那 6 档 fixed_add_fen 恰好全等，所以金额没暴露；
    #        一旦各档不同（就像良票那条），就会选错档 → 报错价。
    #
    #   ★ 为什么不直接把 base_fen 改成整单：
    #     fixed_per_ticket 的 adjustment 本来就是【按单张】算的
    #     （每张加 fixed_add_fen 分），动了它会改金额。
    #     ⇒ 折扣率单独用一个整单基数，两条路各用各的。
    #   ★ original 是例外：它的“调整基数”本来就是原价，
    #     折扣率 = 原价÷原价 = 恒 100%。这不是巧合，是定义 ——
    #     所以这里显式写出来，不靠 base_fen 恰好等于原价。
    if getattr(rule, "basis", BASIS_ESTIMATED) == BASIS_ORIGINAL:
        disc_base_fen = original_total
    else:
        disc_base_fen = cost_total
    disc = discount_bps_of(disc_base_fen, original_total)
    row = pick_tier(tiers, disc_base_fen, original_total)
    if row is None:
        return {"status": "TIER_NOT_FOUND", "total_fen": cost_total,
                "tier": None, "discount_bps": disc,
                "note": f"折扣率 {disc / 100:.2f}% 没落到任何档位"}

    basis = getattr(rule, "basis", BASIS_ESTIMATED)
    step = step if step and step > 0 else 1
    add = int(getattr(row, "fixed_add_fen", 0) or 0)
    adj = int(getattr(row, "adjustment_bps", 0) or 0)
    n = max(1, int(seat_count or 1))

    if basis in (BASIS_PER_TICKET, BASIS_COST_PLUS):
        if basis == BASIS_PER_TICKET:
            unit = _round_half_up(p["adjust_on"] + add, 1, step)
            # 不低于"成本 + 渠道费"
            floor_fen = cost_unit + channel_fee_fen
            if unit < floor_fen:
                unit = _round_half_up(floor_fen, 1, step)
            total = unit * n
        else:
            numerator = cost_total + add * n
            if step == 10:
                total = _round_half_up(numerator, n, step) * n
            else:
                total = _round_half_up(numerator, 1, step)
    else:
        # ★ original / estimated：调整基数分别是原价 / 成本
        check_fen = (10000 * cost_total + p["adjust_on"] * adj)
        total = _round_half_up(check_fen, 10_000, step)

    return {"status": "PRICED", "total_fen": int(total), "tier": row,
            "discount_bps": disc,
            "note": (f"分档 {basis}：折扣率 {disc / 100:.2f}% → "
                     f"比例 {adj / 100:+.2f}%"
                     + (f" 每张加价 {add / 100:.2f} 元" if add else ""))}


def _dynamic_delta_bps(base_fen: int, discount_bps: int) -> int:
    """
    动态折扣加法：折扣档位越高（越接近 10000），让利越少。

    discount_bps: 10000 = 不打折（不让利），8000 = 八折档位
    """
    if discount_bps <= 0:
        return 0
    if discount_bps >= 10_000:
        return 0
    give = int(base_fen * (10_000 - discount_bps) / 10_000)
    return -give


def _adjust(fen: int, kind: str) -> int:
    """
    报价调整类型。

    ★ "四舍五入到元-0.1" 的口径：
      先四舍五入到整元，再减 0.1 元。
      目的是让价格看起来像 34.90 / 23.90 这种"带尾巴"的价，
      而不是 35.00 的整数 —— 电商习惯，也显得更像真实优惠价。
    """
    if kind == ADJUST_NONE:
        return fen
    if kind == ADJUST_ROUND_JIAO:                 # 到角
        return (fen + 5) // 10 * 10
    if kind == ADJUST_ROUND_YUAN:                 # 到元
        return (fen + 50) // 100 * 100
    if kind == ADJUST_ROUND_YUAN_MINUS_01:        # 到元 - 0.1 元
        yuan = (fen + 50) // 100 * 100
        return max(0, yuan - 10)
    return fen


# ==================================================================== 院线名
# ★★ 一个真实的对接缺口（必须知道）
#
#   票达人的报价规则里，院线条件写的是【它自己的分级】：
#       「万达电影（一线）A类」「万达影院（非一线）B类」「万达电影（非一线）C类」
#       「UME影城」「耀莱一线」「耀莱二线」「原官方-其他」…
#
#   但良票 `brand/list` 只返回【简单品牌名】：
#       {"brandId": 3, "name": "万达影城"}
#       {"brandId": 8, "name": "CGV影城"}
#
#   ⇒ 「一线/二线/A类/B类」这层分级【在良票接口里找不到出处】。
#     它要么是票达人自己的内部字典，要么来自另一个数据源。
#
#   ★ 所以我们不能照抄票达人的院线名 —— 那会导致规则永远匹配不上：
#     场次返回的 brandName 是「万达影城」，规则里写「万达电影（一线）A类」，
#     两边对不上，规则形同虚设。
#
#   ★ 所以院线名应当【直接用良票返回的 brandName】，不做任何加工 ——
#     良票文档 7.1 识别响应里 brandName 就是影院所属品牌名，
#     那就是唯一权威来源。自己编分级名只会让规则匹配不上。
#
#     出处：良票文档 4.5 院线品牌列表（list[].name）
#           良票文档 7.1 识别响应（finalResults.brandName）
#
#   下面用的就是良票文档示例里的品牌名。真实名字要从 brand/list 拉。
BRAND_WANDA = "万达影城"
BRAND_CGV = "CGV影城"

# ★★★ 渠道常量（2026-10-14）
#   用途：QuoteRule.channel —— 限定这条规则只对某个渠道生效。
#   ★ 为什么单独定义：字符串散在各处迟早拼错（"WANDA" / "wanda"），
#     而渠道值在会话上是 quote_channel（WANDA / LIANGPIAO）。
#   ★ 与 BRAND_WANDA 的区别（很容易混）：
#       BRAND_WANDA  = 院线品牌名（"万达影城"）—— 用来匹配【哪个院线】
#       CHANNEL_WANDA = 出票渠道（"WANDA"）  —— 用来匹配【走哪条供货链】
#     同一个万达影院走万达渠道还是良票渠道，是两个不同的维度。
CHANNEL_WANDA = "WANDA"
CHANNEL_LIANGPIAO = "LIANGPIAO"
CHANNELS = (CHANNEL_WANDA, CHANNEL_LIANGPIAO)

# 良票文档示例里出现过的品牌名（用于对照与测试）
# ★ 生产环境请用 brands_from_api(api.ch4_brand_list()) 动态填这个列表
KNOWN_BRANDS = [
    "万达影城", "CGV影城",
]

# 票达人那 6 条规则里出现过的【分级名】。保留下来只为了能对照它的页面 ——
# 这些名字不该出现在生产规则里（上面说了原因）。
TIER_NAMES_FROM_PIAODAREN = [
    "万达电影（一线）A类", "万达影院（非一线）B类", "万达电影（非一线）C类",
    "UME影城", "耀莱一线", "耀莱二线", "横店电影", "金逸影城", "金逸光美",
    "大地影院", "星轶STARX影城", "万象影城", "嘉凯影城", "德信影城",
    "华谊兄弟", "博纳国际影城", "卢米埃影城", "幸福蓝海", "SFC上影影城",
    "中影国际影城", "沃美影城", "万达特许（非万达广场的万达影城）", "其他",
]


def tianjin_default_rules() -> list[QuoteRule]:
    """
    票达人「报价规则」页里那 6 条 —— 【用于对照结构，不是可直接用的配置】。

    ★★ 不能直接照抄的两个原因

    【1】院线条件用的是票达人的分级名（"万达电影（一线）A类"），
         而良票 brand/list 只给简单品牌名（"万达影城"）。
         两边对不上 ⇒ 规则会永远匹配不上，形同虚设。
         这里保留原分级名，是为了让界面能如实显示它、让校验器能报出问题；
         **生产规则应当从 brand/list 拉真实品牌名**。

    【2】其中两条自相矛盾：
           杂牌院线预估   报价 +1 / 限价 +0
           连锁B预估限价  报价 +2 / 限价 +0
         按文档「实际应付超过 maxPrice 时下单失败(300005)」，
         这两条只要启用就会下单失败。它们恰好都是停用状态 ——
         保留原样，让界面能显示这个问题，而不是悄悄"修好"。

    ★ 另外：启用的那条兜底规则 markup_yuan=0，也就是【按成本卖，没有毛利】。
      真实生产需要运营自己配加价。

    ★★ 2026-10 同步 V5 的真实报价口径
      来源：V5 生产库 business-settings.sqlite3 的 wanda_pricing 配置，
            以及同库 quotes.sqlite3 里的真实报价记录（已验证吻合）。
        · 万达 = 【每张固定加 1.00 元】
              V5: basis="fixed_per_ticket", fixed_add_fen=100（=1.00 元）
          验证：真实记录 成本 4500/张 → 售价 4600/张，4 张共 18400
                4500 + 100 = 4600 ✓ 完全吻合
        · rounding_fen = 10 —— 售价取整到 1 角（V5 的业务设置里就是 10）
        · V5 的万达不加价限（全部 tier 的 adjustment_bps 都是 0）——
          所以我们把 capped 关掉：限价是给"按比例加价"的渠道用的
        ★ 启用/停用状态【没改】—— 那是运营的决定，不是算法的事。
    """
    return [
        QuoteRule(name="万达A一口价", weight=9, enabled=False,
                  brands=["万达电影（一线）A类"],
                  quote_mode=QUOTE_FIXED, method=DYNAMIC_DISCOUNT,
                  markup_yuan=1.0, limit_yuan=0.5, capped=False,
                  rounding_fen=10,
                  adjust=ADJUST_ROUND_YUAN_MINUS_01, created_seq=9),
        QuoteRule(name="万达BC一口价", weight=8, enabled=False,
                  brands=["万达影院（非一线）B类", "万达电影（非一线）C类"],
                  quote_mode=QUOTE_FIXED, method=DYNAMIC_DISCOUNT,
                  markup_yuan=1.0, limit_yuan=0.5, capped=False,
                  rounding_fen=10,
                  adjust=ADJUST_ROUND_YUAN_MINUS_01, created_seq=8),
        QuoteRule(name="连锁A一口价", weight=7, enabled=False,
                  brands=["UME影城", "幸福蓝海", "SFC上影影城", "沃美影城",
                          "卢米埃影城", "博纳国际影城", "耀莱一线", "耀莱二线",
                          "德信影城", "华谊兄弟", "万象影城", "中影国际影城",
                          "万达特许（非万达广场的万达影城）"],
                  quote_mode=QUOTE_FIXED, method=DYNAMIC_DISCOUNT,
                  markup_yuan=0, limit_yuan=0.5, capped=True,
                  adjust=ADJUST_ROUND_YUAN_MINUS_01, created_seq=7),
        QuoteRule(name="杂牌院线预估", weight=6, enabled=False,
                  brands=["万达影院（非一线）B类", "万达电影（非一线）C类"],
                  quote_mode=QUOTE_ESTIMATE, method=ADD,
                  markup_yuan=1, limit_yuan=0, capped=True,
                  adjust=ADJUST_ROUND_YUAN_MINUS_01, created_seq=6),
        QuoteRule(name="连锁B预估限价", weight=5, enabled=False,
                  brands=["万达电影（一线）A类"],
                  quote_mode=QUOTE_ESTIMATE, method=ADD,
                  markup_yuan=2, limit_yuan=0, capped=True,
                  adjust=ADJUST_ROUND_YUAN_MINUS_01, created_seq=5),
        QuoteRule(name="兜底全院线预估", weight=4, enabled=True,
                  # ★ 用良票的真实品牌名（这里给的是已知的两个 + 不限院线的兜底）
                  brands=list(KNOWN_BRANDS),
                  quote_mode=QUOTE_ESTIMATE, method=DYNAMIC_DISCOUNT,
                  markup_yuan=0, limit_yuan=0.5, capped=True,
                  adjust=ADJUST_ROUND_YUAN_MINUS_01, created_seq=4),
    ]


def default_rules_for_brands(brands: list[str], *, markup_yuan: float = 2.0,
                             limit_yuan: float = 2.5) -> list[QuoteRule]:
    """
    最小可用的一份规则（只有一条兜底）。

    ★ 只在"连院线名都拿不到"时用。正常情况下请用 seed_rules()。
    """
    if not brands:
        brands = list(KNOWN_BRANDS)
    return [
        QuoteRule(
            name="兜底全院线",
            weight=1, enabled=True,
            brands=list(brands),
            quote_mode=QUOTE_ESTIMATE, method=ADD,
            markup_yuan=markup_yuan,
            limit_yuan=limit_yuan,   # ★ 必须 ≥ markup_yuan，否则下单会被 300005 拒
            capped=True,
            adjust=ADJUST_ROUND_YUAN_MINUS_01,
            created_seq=1),
    ]


def seed_rules(brands: list[str]) -> list[QuoteRule]:
    """
    ★ 首次打开规则页时的初始规则 —— 界面能看，也真能用。

    结构上对应票达人那 6 条的分档思路，但【院线名换成良票的真实品牌名】，
    并且【每档都带加价、限价 ≥ 报价】—— 不会再出现
    "启用的那条不加价"或"限价低于报价"这种一用就出问题的情况。

    ★★ 加价金额是【占位值，需要你按实际毛利改】。
      这里给 2 元/张是常见的量级，不是从任何数据推出来的。
      我不知道你的实际成本结构和目标毛利，所以不敢编得更具体。

    ★★ 院线分档也是占位：良票 brand/list 只给品牌名，
      没有"一线/二线/连锁/杂牌"这种分级。所以这里按
      "万达系 / 连锁系 / 其他" 三档分 —— 分法你可以改，改完界面会存下来。
    """
    known = [b for b in (brands or []) if b]
    if not known:
        known = list(KNOWN_BRANDS)

    # 按名字粗分三档（★ 这是占位分法，不是良票的分类）
    wanda = [b for b in known if "万达" in b]
    chain = [b for b in known if any(
        k in b for k in ("CGV", "UME", "金逸", "横店", "大地", "博纳",
                         "华谊", "卢米埃", "幸福蓝海", "中影", "万象",
                         "沃美", "耀莱", "德信", "嘉凯", "星轶", "SFC"))]
    other = [b for b in known if b not in wanda and b not in chain]

    def _rule(name, seq, weight, rule_brands, quote_mode, markup, limit):
        return QuoteRule(
            name=name, weight=weight, enabled=False,
            brands=list(rule_brands),
            quote_mode=quote_mode, method=ADD,
            markup_yuan=markup,
            # ★ 限价必须 ≥ 报价，否则下单会被 300005 拒
            limit_yuan=limit, capped=True,
            adjust=ADJUST_ROUND_YUAN_MINUS_01, created_seq=seq)

    rules = []
    if wanda:
        # ★★ 万达这条用【万达专用报价】模式，并且【默认启用】。
        #   为什么默认启用：
        #     以前它和别的规则一样是 enabled=False，只有兜底那条启用 ——
        #     结果是万达的单全部落到「兜底全院线预估」，走的是预估价，
        #     运营在界面上找不到万达的一口价规则（被问了 4 次）。
        #   ⇒ 万达是最主要的渠道，它的规则默认就该是生效的。
        w = _rule("万达系一口价", 6, 6, wanda, QUOTE_WANDA, 2, 2.5)
        w.enabled = True
        # ★ basis 用【每张固定加价】而不是「预估成本」：
        #   万达的成本是座位图里的会员价（确定值），不是预估值 ——
        #   用「预估成本」的折扣率分档没有意义。
        w.basis = BASIS_PER_TICKET
        rules.append(w)
    if chain:
        rules.append(_rule("连锁系一口价", 5, 5, chain, QUOTE_FIXED, 2, 2.5))
    if other:
        rules.append(_rule("其他院线预估", 4, 4, other, QUOTE_ESTIMATE, 2, 2.5))
    # 兜底：启用，覆盖全部院线。★ 必须启用 —— 否则新院线出现时无价可报。
    rules.append(_rule("兜底全院线预估", 3, 3, known, QUOTE_ESTIMATE, 2, 2.5))
    rules[-1].enabled = True
    # ================================================================
    # ★★★ V5 生产库里的两条主力规则（2026-10-14 店主：抄过来并启用）
    #
    #   来源：business-settings.sqlite3 的
    #         key="wanda_pricing" / key="liangpiao_pricing"
    #   （线上库 ubuntu@124.220.29.179:/var/lib/ticket-system-v5/，
    #     本地只读副本 .shadow-data/business-settings.sqlite3 revision=52）
    #
    #   ★ 为什么要有这两条：现有那三条是【按院线】分的，而 V5
    #     的主力口径是【按渠道】—— 同一家影院走万达是"每张 +1 元"，
    #     走良票是"按折扣率加 10%/8%/6%/4%/3%/1%"。院线维度表达不了。
    #
    #   ★★ 权重必须高于下面那三条（6/5/4）：
    #      渠道口径比院线口径更具体，更具体的先命中。
    #      否则万达的单会被"万达系一口价"先截走，这两条形同虚设。
    #
    #   ★ channel 字段：只在对应渠道被命中。不带 channel 的调用方
    #     （比如离线试算）只会看到下面那三条 —— 行为不变。
    #
    #   ★ adjust=ADJUST_NONE 是关键，别改：
    #     取整已经由 price_by_tier 按 rounding_fen=10 做完了，
    #     外面再套一次「到元-0.1」会二次取整（实测 6470 → 6480，
    #     把良票 +3% 算出来的价又抬了一角）。
    # ================================================================
    rules.insert(0, QuoteRule(
        name="万达报价（V5）", weight=20, enabled=True,
        channel=CHANNEL_WANDA, brands=[], excluded_cinemas=[],
        # V5: basis="fixed_per_ticket" → 报价 = 每张成本 + fixed_add_fen
        quote_mode=QUOTE_FIXED, method=ADD, basis=BASIS_PER_TICKET,
        markup_yuan=0, limit_yuan=0, capped=False,
        adjust=ADJUST_NONE, rounding_fen=10, created_seq=20,
        # V5 原样：6 档，每档 fixed_add_fen=100（1.00 元/张），末档右闭
        tiers=[
            PricingTier(0, 5000, 0, 100, False),
            PricingTier(5000, 6000, 0, 100, False),
            PricingTier(6000, 7000, 0, 100, False),
            PricingTier(7000, 8000, 0, 100, False),
            PricingTier(8000, 9000, 0, 100, False),
            PricingTier(9000, 10000, 0, 100, True),
        ]))
    rules.insert(1, QuoteRule(
        name="良票报价（V5）", weight=19, enabled=True,
        channel=CHANNEL_LIANGPIAO, brands=[], excluded_cinemas=[],
        # V5: basis="estimated" → 报价 = 成本 ×(1 + adjustment_bps/10000)
        quote_mode=QUOTE_FIXED, method=ADD, basis=BASIS_ESTIMATED,
        markup_yuan=0, limit_yuan=0, capped=False,
        adjust=ADJUST_NONE, rounding_fen=10, created_seq=19,
        # V5 原样：折扣率越低加得越多
        #   <50%→+10% / 50-60%→+8% / 60-70%→+6%
        #   70-80%→+4% / 80-90%→+3% / 90-100%→+1%
        tiers=[
            PricingTier(0, 5000, 1000, 0, False),
            PricingTier(5000, 6000, 800, 0, False),
            PricingTier(6000, 7000, 600, 0, False),
            PricingTier(7000, 8000, 400, 0, False),
            PricingTier(8000, 9000, 300, 0, False),
            PricingTier(9000, 10000, 100, 0, True),
        ]))
    # ================================================================

    return rules


def brands_from_api(brand_list: list) -> list[str]:
    """
    ★ 从良票 `brand/list` 的响应里取出院线名。

    票达人用的就是良票接口，两边 brandName 是同一个值 ——
    所以院线全集必须动态拉，不能硬编码。
    硬编码的后果：良票新增/改名一个院线，我们的规则就再也匹配不上它。

    brand_list 形如 [{"brandId": 3, "name": "万达影城"}, ...]
    """
    names = []
    for item in brand_list or []:
        name = (item or {}).get("name")
        if name and name not in names:
            names.append(str(name))
    return names


if __name__ == "__main__":
    engine = QuoteRuleEngine(tianjin_default_rules())
    print("=" * 76)
    print("报价规则引擎 —— 票达人那 6 条规则")
    print("=" * 76)
    for r in engine.rules:
        state = "启用" if r.enabled else "停用"
        mode = PRICE_MODE_OF_QUOTE.get(r.quote_mode, "?")
        print(f"  权重{r.weight}  {r.name:14} {state}  {r.quote_mode}({mode})  "
              f"{r.method}  +{r.markup_yuan}元  限价+{r.limit_yuan}元  {r.adjust}")

    print()
    print("=" * 76)
    print("同一单成本，走不同院线的报价差异")
    print("=" * 76)
    from datetime import time as t
    cost, seats = 3510 * 2, 2      # 成本 70.20 元，2 张
    for brand in [BRAND_WANDA, BRAND_CGV, "某不认识的院线"]:
        r = engine.quote(estimate_total_fen=cost, seat_count=seats, now=t(19, 0),
                         brand=brand, original_total_fen=3890 * 2, discount_bps=9500)
        print(f"\n  {brand}")
        print(f"    命中规则  {r.rule_name}  ({r.quote_mode} → priceMode={r.price_mode})")
        print(f"    成本      {r.cost_total_fen / 100:.2f} 元（{r.cost_unit_fen / 100:.2f}/张）")
        print(f"    报价      {r.total_price_fen / 100:.2f} 元（{r.unit_price_fen / 100:.2f}/张）")
        print(f"    maxPrice  {r.max_price_fen / 100:.2f} 元" if r.max_price_fen else "    不限价")
        print(f"    传 API    {r.as_api_params()}")
        for n in r.notes:
            print(f"      ※ {n}")

    print()
    print("=" * 76)
    print("★ 边界：限价低于报价会被 300005 拒 —— 规则自相矛盾要提前发现")
    print("=" * 76)
    bad = QuoteRule(name="矛盾规则", weight=99, quote_mode=QUOTE_ESTIMATE,
                    method=ADD, markup_yuan=10, limit_yuan=0, capped=True)
    e2 = QuoteRuleEngine([bad])
    r = e2.quote(estimate_total_fen=1000, seat_count=1, now=t(12, 0), brand="X")
    print(f"  报价 {r.unit_price_fen / 100:.2f} 元，限价 {(r.max_price_fen or 0) / 100:.2f} 元")
    for n in r.notes:
        print(f"    ※ {n}")
