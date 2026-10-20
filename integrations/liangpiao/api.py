"""
integrations/liangpiao/api.py —— 良票 26 个接口的类型化封装

════════════════════════════════════════════════════════════════
★ 这一层只做四件事，不掺业务判断

  1. 把路径与参数【按文档】拼对
  2. 按接口设对的超时（文档有明确要求）
  3. 把响应里的金额字符串转成分（int），别让"3510"这种字符串流进业务层
  4. 把询价与下单的公共参数【绑在一起】，防止传岔

★ 为什么第 4 条单独强调

  文档反复说：「areaQuoteStrategy / invoiceRequired 必须与下单传同一个值，
  否则询价结果与实际应付对不上」。
  这两次调用中间隔着规则引擎和用户交互，很容易只改一处。
  所以这里做成一个 QuoteParams 对象，询价和下单都传它，不可能岔。

★ 还没做的（无凭证，无法验证）

  真实的 HTTP 调用。所有方法都能在 FakeTransport 下跑通，
  拿到 AppKey 后先跑 ping() 验证签名即可。
════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .client import (RECOGNIZE_TIMEOUT, SEAT_LIST_TIMEOUT,
                     ApiResponse, LiangPiaoClient)

# ==================================================================== 枚举
TICKET_STANDARD, TICKET_FAST, TICKET_FLASH = "STANDARD", "FAST", "FLASH"
TICKET_MODES = (TICKET_STANDARD, TICKET_FAST, TICKET_FLASH)

PRICE_FIXED, PRICE_LIMIT = "FIXED", "LIMIT"
PRICE_MODES = (PRICE_FIXED, PRICE_LIMIT)

# 跨分区报价策略（询价与下单必须传同一个值）
AREA_AVERAGE, AREA_HIGHEST, AREA_LOWEST = "AVERAGE", "HIGHEST", "LOWEST"
AREA_STRATEGIES = (AREA_AVERAGE, AREA_HIGHEST, AREA_LOWEST)

# 识别结果
MATCH_EXACT, MATCH_CANDIDATE, MATCH_NONE = "EXACT", "CANDIDATE", "NONE"
# 没定到场次的原因
NOMATCH_SHOW_EXPIRED = "SHOW_EXPIRED"
NOMATCH_NOT_FOUND = "NOT_FOUND"

# 订单状态（文档第八节）
ORDER_STATUSES = ("CREATED", "SUBMITTING", "TICKETING", "TICKETED", "SETTLED",
                  "REJECTED", "FAILED", "CANCELLED", "REFUNDING", "REFUNDED")
ORDER_TERMINAL = ("SETTLED", "REJECTED", "FAILED", "CANCELLED", "REFUNDED")

# 座位
SEAT_AVAILABLE, SEAT_SOLD = "AVAILABLE", "SOLD"

# 票速档位 → 出票时效（文档）
TICKET_MODE_MINUTES = {
    TICKET_STANDARD: (1, 60),      # 特惠：1~60 分钟
    TICKET_FAST: (0, 15),          # 快速：15 分钟内
    TICKET_FLASH: (1, 15),         # 极速：1~15 分钟，院线直出
}
# 催促冷却（文档 order/detail 的 urgeAvailableAt 说明）
URGE_COOLDOWN_MINUTES = {TICKET_STANDARD: 20, TICKET_FAST: 15, TICKET_FLASH: 10}


def fen(value) -> int:
    """
    ★ 文档：金额单位是分，**响应中一律为字符串**（如 "3510"）。
    这个函数是唯一允许做这个转换的地方 —— 别的代码不该见到字符串金额。

    ★ 转不动就返回 0，绝不抛异常：
      解析金额失败不该让整条链路崩掉。真出问题会在对账时暴露
      （分账为 0 很容易看出来），而抛异常会让回调处理失败、触发平台重推。
    """
    if value in (None, ""):
        return 0
    if isinstance(value, bool):          # bool 是 int 的子类，单独挡掉
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def fen_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    return fen(value)


# ==================================================================== 数据对象
@dataclass
class QuoteParams:
    """
    ★ 询价与下单【必须一致】的参数，绑成一个对象。

    文档：「areaQuoteStrategy / invoiceRequired 必须与下单传同一个值，
    否则询价结果与实际应付对不上」。
    做成对象后，两条调用路径共用同一份，不可能岔。
    """
    area_quote_strategy: str = AREA_AVERAGE
    invoice_required: bool = False

    def __post_init__(self):
        if self.area_quote_strategy not in AREA_STRATEGIES:
            raise ValueError(f"跨分区策略不合法：{self.area_quote_strategy}")

    def to_fields(self) -> dict:
        return {"areaQuoteStrategy": self.area_quote_strategy,
                "invoiceRequired": self.invoice_required}


@dataclass
class PreflightResult:
    """选座询价结果。"""
    available: bool
    reason: str = ""
    total_amount_fen: int = 0        # FIXED 即成交价；LIMIT 是限价上限
    estimate_amount_fen: int = 0     # ★ LIMIT 模式的成本基准
    market_amount_fen: int | None = None
    estimated: bool = False          # totalAmount 是预估上限而非成交价
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: dict) -> "PreflightResult":
        data = data or {}
        return cls(
            available=bool(data.get("available")),
            reason=str(data.get("reason") or ""),
            total_amount_fen=fen(data.get("totalAmount")),
            estimate_amount_fen=fen(data.get("estimateAmount")),
            market_amount_fen=fen_or_none(data.get("marketAmount")),
            estimated=bool(data.get("estimated")),
            raw=data)

    @property
    def cost_basis_fen(self) -> int:
        """
        ★ 我们的成本基准。

        文档 5.1：
            estimateAmount   预估应付(分)，LIMIT 模式参考
            totalAmount      最优应付总额(分)。FIXED 即成交价，LIMIT 为限价上限
            estimated        true 表示 totalAmount 是预估上限而非确定成交价

        ⇒ 优先用 estimateAmount（LIMIT 的成本基准）；
          它没给才退回 totalAmount —— 而这一退回【有风险】：
          此时若 estimated=true，我们拿到的是【上限】不是确定价。

        ★ 这个属性只回答"用哪个数"。
          "这个数可不可靠"看 cost_basis_is_estimate —— 上层报价时要看它。
          （原来这两个问题混在一起，于是 estimated 字段解析了却没人读。）
        """
        return self.estimate_amount_fen or self.total_amount_fen

    @property
    def cost_basis_is_estimate(self) -> bool:
        """
        ★ 成本基准是不是【预估/上限】而不是确定价。

        文档 5.1 原文：「`estimated` boolean，true 表示 `totalAmount`
        是预估上限而非确定成交价」。

        ★ 什么时候为真：
          用了 estimateAmount → 文档说它本身是"预估应付"，故为真。
          退回 totalAmount 且 estimated=true → 拿到的是上限，为真。
          退回 totalAmount 且 estimated=false → 确定价，为假。

        ★ 为什么必须有人读它：
          为真时我们报出去的价是【可能兑现不了的】。
          文档 1101 行：「报价有时效，询价后请尽快下单，下单时平台会再实时询一次，
          以下单结果为准」。
          ⇒ 报价必须标成"预估"，且下单时用 maxPrice 兜底。
        """
        if self.estimate_amount_fen:
            return True
        return bool(self.estimated)

    @property
    def price_ceiling_fen(self) -> int:
        """
        这张单【平台认为可能要付的上限】（分）—— 用来算下单的 maxPrice。

        ★★★ 为什么不能只用 estimateAmount 当"实际应付"（2026-10-14 实测发现）：
          文档 5.1：
            estimateAmount  预估应付(分)，LIMIT 模式参考
            totalAmount     最优应付总额(分)。FIXED 即成交价，LIMIT 为【限价上限】
          文档 1182：
            maxPrice 总限价(分，数字)。【实际应付超过时下单失败，报 300005】

          ★ 两者在【同区座位】时相等，在【跨区座位】时会分开。
            实测（场次 38208129，最贵区 4990 + 最便宜区 4290 各一座，
                  原价合计 9280，priceMode=LIMIT）：
                AVERAGE → total=8697  estimate=7509
                HIGHEST → total=9353  estimate=7509
                LOWEST  → total=8041  estimate=7509
                （FIXED 三种都 = 7673，策略无影响 —— 文档 1088 行确认）

          ⇒ 我们原来按 estimateAmount（7509）反推单张成本 3755，
            再按规则加 limit_yuan 得出 maxPrice。跨区时这个数
            【可能小于平台认定的应付】，下单直接 300005 失败。
            店主原话：「买家发送的座位图两个座位价格不一样，会建单失败」。

        ★ 所以取两者较大者当"这张单可能要付到多少"。
          为什么取 max 而不是只取 totalAmount：
            实测那次 AVERAGE 的 total(8697) 比 HIGHEST 的 total(9353) 小，
            而策略在【下单时】可以变（不传就走平台默认，文档 1083 说默认 AVERAGE，
            但我们实测不传时拿到的却是 9353 —— 默认值不可依赖）。
            取 max 是"按最坏情况留够"，宁可限价宽一点也不让单子失败。
          ★ 代价：限价宽了，理论上可能按更高的价成交。
            但文档 1189 说「LIMIT 模式报价本身是预估上限，实扣以放映后结算为准」——
            maxPrice 是【超过就拒单】的闸门，不是出价。
            真正的成交价由平台的比价链路决定，不会因为闸门高就多收。
        """
        return max(int(self.total_amount_fen or 0),
                   int(self.estimate_amount_fen or 0))


@dataclass
class CreateOrderResult:
    """下单结果。★ 冻结成功即返回，不代表已出票。"""
    order_no: str
    status: str
    freeze_amount_fen: int
    seat_count: int
    duplicated: bool = False          # true = 命中幂等，返回的是首次下单结果
    pickup_url: str | None = None     # ★ 下单即返回、固定不变、不含价格，可提前发给买家
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: dict) -> "CreateOrderResult":
        data = data or {}
        return cls(
            order_no=str(data.get("orderNo") or ""),
            status=str(data.get("status") or ""),
            freeze_amount_fen=fen(data.get("freezeAmount")),
            seat_count=int(data.get("seatCount") or 0),
            duplicated=bool(data.get("duplicated")),
            pickup_url=data.get("pickupUrl"),
            raw=data)


@dataclass
class OrderDetail:
    """订单详情。字段很多，只挑业务要用的。"""
    order_no: str = ""
    out_order_no: str = ""
    status: str = ""
    show_id: str = ""
    cinema_name: str = ""
    movie_name: str = ""
    hall_name: str = ""
    cinema_address: str | None = None
    city_name: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    seat_count: int = 0
    seats: list = field(default_factory=list)          # 下单快照，换座后不变
    actual_seats: list | None = None                   # ★ 入场座位，以这个为准
    seat_changed: bool | None = None                   # ★ 三态
    allow_seat_change: bool = True
    ticket_mode: str = ""
    price_mode: str = ""
    quote_amount_fen: int = 0
    freeze_amount_fen: int = 0
    settle_amount_fen: int | None = None
    estimated_settle_fen: int | None = None
    market_amount_fen: int | None = None
    refundable: bool = False
    fail_code: str | None = None
    fail_reason: str | None = None
    pickup_url: str | None = None
    ticket_link: str | None = None
    urge_available_at: str | None = None
    ticketed_at: str | None = None
    created_at: str | None = None
    tickets: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: dict) -> "OrderDetail":
        data = data or {}

        def s(key):
            v = data.get(key)
            return str(v) if v not in (None, "") else None

        return cls(
            order_no=str(data.get("orderNo") or ""),
            out_order_no=str(data.get("outOrderNo") or ""),
            status=str(data.get("status") or ""),
            show_id=str(data.get("showId") or ""),
            cinema_name=str(data.get("cinemaName") or ""),
            movie_name=str(data.get("movieName") or ""),
            hall_name=str(data.get("hallName") or ""),
            cinema_address=data.get("cinemaAddress"),
            city_name=data.get("cityName"),
            start_time=data.get("startTime"),
            end_time=data.get("endTime"),
            seat_count=int(data.get("seatCount") or 0),
            seats=list(data.get("seats") or []),
            actual_seats=list(data["actualSeats"]) if data.get("actualSeats") else None,
            seat_changed=data.get("seatChanged"),
            allow_seat_change=bool(data.get("allowSeatChange", True)),
            ticket_mode=str(data.get("ticketMode") or ""),
            price_mode=str(data.get("priceMode") or ""),
            quote_amount_fen=fen(data.get("quoteAmount")),
            freeze_amount_fen=fen(data.get("freezeAmount")),
            settle_amount_fen=fen_or_none(data.get("settleAmount")),
            estimated_settle_fen=fen_or_none(data.get("estimatedSettleAmount")),
            market_amount_fen=fen_or_none(data.get("marketAmount")),
            refundable=bool(data.get("refundable")),
            fail_code=s("failCode"), fail_reason=s("failReason"),
            pickup_url=data.get("pickupUrl"),
            ticket_link=data.get("ticketLink"),
            urge_available_at=data.get("urgeAvailableAt"),
            ticketed_at=data.get("ticketedAt"),
            created_at=data.get("createdAt"),
            tickets=list(data.get("tickets") or []),
            raw=data)

    @property
    def is_terminal(self) -> bool:
        return self.status in ORDER_TERMINAL

    @property
    def tickets_valid(self) -> bool:
        """
        ★ 凭证是否可用。

        文档：未出票、以及订单已关闭（FAILED / CANCELLED / REJECTED）时
        tickets 恒为空数组；已关闭订单即使上游事后出了票也不会在此返回，
        避免顾客误用。
        ⇒ 所以我们也不该拿已关闭订单的凭证发给买家。
        """
        return self.status in ("TICKETED", "SETTLED", "REFUNDING") and bool(self.tickets)

    @property
    def should_urge_at(self) -> str:
        """可催促时刻。文档：档位冷却与出票限时取较早值。"""
        return self.urge_available_at or ""


@dataclass
class SeatMap:
    """座位图。"""
    hall_name: str = ""
    row_count: int | None = None
    col_count: int | None = None
    max_seats_per_order: int = 6       # ★ 单笔限购，可能低于平台上限 6
    graph_reliable: bool = False       # ★ 整图级标记
    areas: list = field(default_factory=list)
    seats: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: dict) -> "SeatMap":
        data = data or {}
        hall = data.get("hall") or {}
        return cls(
            hall_name=str(hall.get("name") or ""),
            row_count=hall.get("rowCount"),
            col_count=hall.get("colCount"),
            max_seats_per_order=int(data.get("maxSeatsPerOrder") or 6),
            graph_reliable=bool(data.get("graphReliable")),
            areas=list(data.get("areas") or []),
            seats=list(data.get("seats") or []),
            raw=data)

    def available_seats(self) -> list:
        return [s for s in self.seats if s.get("status") == SEAT_AVAILABLE]

    @property
    def has_areas(self) -> bool:
        """是否分区定价厅。★ 分区时逐座必须带 areaId，否则可能买错价位。"""
        return bool(self.areas)


@dataclass
class RecognitionResult:
    """
    截图识别结果。

    ★ 最高频的误用：把 seatPriceFen 当成我们的应付价。
      那是【截图上顾客看到的售价】。我们的应付价看 prices。
    """
    recognize_id: str | None = None
    cached: bool = False
    match_level: str = MATCH_NONE
    no_match_reason: str | None = None

    # 命中的身份（与 cinemaId / movieId 同生共死）
    cinema_id: int | None = None
    cinema_name: str | None = None
    cinema_address: str | None = None
    cinema_hit_nums: int = 0
    # ★★ 影院名是否被模型截断（rawResults.cinemaTruncated，2026-10-14 加）。
    #   为什么必须解析它：良票文档 §7.1 明确写
    #     「cinemaTruncated=true（真实样本约四分之一）时 cinemaHitNums 往往大于 1，
    #       ★ 必须走二次确认。」
    #   实测（两张真图）：
    #     截断   rawResults.cinema = '万达影城（娄底五江广...'   13 字，以 ... 结尾，true
    #     没截断 rawResults.cinema = '万达影城（九原万达广场IMAX店）' 17 字，无 ...，false
    #   ⇒ 截断意味着【模型只看到了一部分影院名】：
    #     它可能把两家不同影院的截断名都匹配到同一家，而 cinemaHitNums 仍是 1。
    #     这种情况我们不能当唯一命中直接报价。
    cinema_truncated: bool = False
    city_name: str | None = None
    brand_name: str | None = None
    movie_id: int | None = None
    movie_name: str | None = None

    # 场次（★ 仅 EXACT 有值）
    show_id: str | None = None
    hall_name: str | None = None          # ★ 目录厅名，不是 OCR 原文
    start_time: str | None = None         # ★ ISO 带时区，不是 OCR 的 showtime
    max_seats_per_order: int | None = None  # ★ 仅 EXACT 且已拉座位图时有值

    # 座位与价格
    seats: list = field(default_factory=list)
    seat_matched: bool = False
    seat_confirm_required: bool = False
    seat_confirm_reasons: list = field(default_factory=list)
    expected_seat_count: int | None = None
    price_all_fen: int = 0
    price_mismatch: bool = False
    prices: list = field(default_factory=list)      # ★ 我们的应付价

    candidates: dict = field(default_factory=dict)
    raw_results: dict = field(default_factory=dict)
    final_results: dict = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: dict) -> "RecognitionResult":
        data = data or {}
        f = data.get("finalResults") or {}
        raw = data.get("rawResults") or {}
        final_seats = list(f.get("seat") or [])
        # Some recognition responses keep marked seats only in rawResults when
        # the selected seat is no longer available. Preserve those literal
        # seats so Wanda can price the same area and explain the reselection;
        # do not infer seats from the geometry of the image.
        if not final_seats and isinstance(raw.get("seat"), list):
            candidates = [s for s in raw["seat"] if isinstance(s, dict)
                          and (s.get("seatNo") or s.get("name"))]
            if candidates:
                final_seats = candidates
        # The recognition service may return only the first visible selected
        # seats in finalResults while rawResults still contains the complete
        # marked-seat list.  When the response declares a larger quantity,
        # retain the literal raw seats so the normal preflight can validate
        # all seats instead of treating a six-seat request as a price error.
        expected_count = f.get("expectedSeatCount")
        if isinstance(raw.get("seat"), list) and isinstance(expected_count, int):
            raw_seats = [s for s in raw["seat"] if isinstance(s, dict)
                         and (s.get("seatNo") or s.get("name"))]
            if len(raw_seats) > len(final_seats) and len(raw_seats) <= expected_count:
                final_seats = raw_seats
        return cls(
            recognize_id=(str(data["recognizeId"]) if data.get("recognizeId") else None),
            cached=bool(data.get("cached")),
            match_level=str(f.get("matchLevel") or MATCH_NONE),
            no_match_reason=f.get("noMatchReason"),
            cinema_id=f.get("cinemaId"),
            cinema_name=f.get("cinemaName"),
            cinema_address=f.get("cinemaAddress"),
            cinema_hit_nums=int(f.get("cinemaHitNums") or 0),
            # ★ 影院名截断标记（文档 §7.1：true 时必须走二次确认）
            cinema_truncated=bool(f.get("cinemaTruncated")),
            city_name=f.get("cityName"),
            brand_name=f.get("brandName"),
            movie_id=f.get("movieId"),
            movie_name=f.get("movieName"),
            show_id=(str(f["showId"]) if f.get("showId") else None),
            hall_name=f.get("hallName"),
            start_time=f.get("startTime"),
            max_seats_per_order=f.get("maxSeatsPerOrder"),
            seats=final_seats,
            seat_matched=bool(f.get("seatMatched")),
            seat_confirm_required=bool(f.get("seatConfirmRequired")),
            seat_confirm_reasons=list(f.get("seatConfirmReasons") or []),
            expected_seat_count=expected_count,
            price_all_fen=fen(f.get("priceAllFen")),
            price_mismatch=bool(f.get("priceMismatch")),
            prices=list(f.get("prices") or []),
            candidates=f.get("candidates") or {"cinemas": [], "movies": [], "shows": []},
            raw_results=raw,
            final_results=f)

    # ---------------------------------------------------------------- 判断
    @property
    def can_order_directly(self) -> bool:
        """★ 能不能直接拿 showId 去询价/下单。只有 EXACT 可以。"""
        return self.match_level == MATCH_EXACT and bool(self.show_id)

    @property
    def is_show_expired(self) -> bool:
        """
        ★ 截图里的场次已经开场。
        文档：重试、补 cityName 都不会有可售场次 —— 不要浪费调用。
        """
        return self.no_match_reason == NOMATCH_SHOW_EXPIRED

    @property
    def needs_candidate_pick(self) -> bool:
        return self.match_level == MATCH_CANDIDATE

    def pending_dimensions(self) -> list:
        """
        ★ 还差哪个维度没收敛。

        文档：只对尚未唯一的维度发问
        —— cinemaId 已有值就不要再拿 candidates.cinemas 去问。
        """
        pending = []
        if not self.cinema_id:
            pending.append("cinema")
        if not self.movie_id:
            pending.append("movie")
        if not self.show_id:
            pending.append("show")
        return pending

    def candidate_options(self) -> dict:
        """给出"该问哪些候选"，只含尚未唯一的维度。"""
        out = {}
        for dim in self.pending_dimensions():
            key = {"cinema": "cinemas", "movie": "movies", "show": "shows"}[dim]
            options = self.candidates.get(key) or []
            if options:
                out[dim] = options
        return out

    @property
    def effective_max_seats(self) -> int:
        """单笔限购。EXACT 且拉到座位图时以接口给的为准，否则按平台上限 6。"""
        return self.max_seats_per_order or 6

    def warnings(self) -> list:
        """
        需要人工/额外核查的提示。★ 这些都不是错误，是"要小心"。
        """
        out = []
        if self.price_mismatch:
            out.append("座位价合计与截图总价对不上 —— 可能漏识别了座位，建议人工复核")
        if not self.seat_matched:
            out.append("有座位没对齐到平台座位图 —— 场次结论仍有效，"
                       "但下单座位要以座位图接口为准")
        if self.seat_confirm_required:
            out.append("座位卡片可能被遮挡漏读 —— 请买家核对后回传最终座位")
        if self.cinema_hit_nums > 1:
            out.append(f"影院命中 {self.cinema_hit_nums} 家 —— 必须走二次确认，不要默认取第一个")
        return out


# ==================================================================== API
class LiangPiaoAPI:
    """
    26 个接口的封装。

    ★ 命名按文档章节走，便于对照：
      ch4_* 基础数据  ch5_* 交易  ch6_* 账户  ch7_* 截图识别
    """

    def __init__(self, client: LiangPiaoClient, quote_params: QuoteParams | None = None):
        self.client = client
        # ★ 询价/下单共用的参数。默认值只在这里出现一次。
        self.quote_params = quote_params or QuoteParams()

    # ---------------------------------------------------------------- 基础
    def ch4_ping(self) -> dict:
        """连通性自检。联调第一步。"""
        return self.client.ping().data or {}

    def ch4_city_list(self, keyword: str | None = None) -> list:
        payload = {"keyword": keyword} if keyword else {}
        return (self.client.call("/city/list", payload).data or {}).get("list", [])

    def ch4_city_locate(self, longitude: float, latitude: float) -> dict | None:
        """坐标反查城市。★ GCJ-02；city 为 null 不是错误，要回退让用户手选。"""
        data = self.client.call("/city/locate", {
            "longitude": longitude, "latitude": latitude}).data or {}
        return data.get("city")

    def ch4_region_list(self, parent_code: str | None = None) -> list:
        payload = {"parentCode": parent_code} if parent_code else {}
        return (self.client.call("/region/list", payload).data or {}).get("list", [])

    def ch4_brand_list(self, keyword: str | None = None,
                       city_code: str | None = None) -> list:
        payload = {}
        if keyword:
            payload["keyword"] = keyword
        if city_code:
            payload["cityCode"] = city_code
        return (self.client.call("/brand/list", payload).data or {}).get("list", [])

    def ch4_cinema_list(self, *, city_code: str | None = None,
                        district_code: str | None = None,
                        movie_id: int | None = None, brand_id: int | None = None,
                        keyword: str | None = None,
                        longitude: float | None = None, latitude: float | None = None,
                        page: int = 1, page_size: int = 20) -> dict:
        """
        ★ 坐标必须与 cityCode 成对出现（文档明确）。
        这里直接校验，避免发出去被 100001 拒。
        """
        if (longitude is None) != (latitude is None):
            raise ValueError("经纬度必须成对传")
        if longitude is not None and not city_code:
            raise ValueError("传经纬度时必须同时传 cityCode")
        if page_size > 200:
            raise ValueError("pageSize 上限 200")
        payload: dict[str, Any] = {"page": page, "pageSize": page_size}
        for key, value in (("cityCode", city_code), ("districtCode", district_code),
                           ("movieId", movie_id), ("brandId", brand_id),
                           ("keyword", keyword), ("longitude", longitude),
                           ("latitude", latitude)):
            if value is not None:
                payload[key] = value
        return self.client.call("/cinema/list", payload).data or {}

    def ch4_cinema_detail(self, cinema_id: int) -> dict:
        return self.client.call("/cinema/detail", {"cinemaId": cinema_id}).data or {}

    def ch4_movie_list(self, *, type_: str | None = None, city_code: str | None = None,
                       keyword: str | None = None, page: int = 1,
                       page_size: int = 20) -> dict:
        payload: dict[str, Any] = {"page": page, "pageSize": page_size}
        if type_:
            payload["type"] = type_
        if city_code:
            payload["cityCode"] = city_code
        if keyword:
            payload["keyword"] = keyword
        return self.client.call("/movie/list", payload).data or {}

    def ch4_movie_detail(self, movie_id: int) -> dict:
        return self.client.call("/movie/detail", {"movieId": movie_id}).data or {}

    def ch4_show_dates(self, cinema_id: int, movie_id: int | None = None) -> list:
        """
        可售日期。

        ★ 文档：**不要本地生成未来固定天数**。
          固定天数会同时犯两个错：没排片的日期照样显示；
          热门片预售能提前一个月开出，固定几天又看不到。
        """
        payload: dict[str, Any] = {"cinemaId": cinema_id}
        if movie_id is not None:
            payload["movieId"] = movie_id
        return (self.client.call("/show/dates", payload).data or {}).get("dates", [])

    def ch4_show_list(self, cinema_id: int, show_date: str,
                      movie_id: int | None = None) -> list:
        """
        场次列表。

        ★ showDate 是【营业日】不是自然日：从当天 08:00 到次日 08:00。
          凌晨 00:15 的那场属于前一天。以响应里的 startTime 为准。
        """
        payload: dict[str, Any] = {"cinemaId": cinema_id, "showDate": show_date}
        if movie_id is not None:
            payload["movieId"] = movie_id
        return (self.client.call("/show/list", payload).data or {}).get("list", [])

    def ch4_show_detail(self, show_id: str) -> dict:
        """场次详情。会强制回源复核价格与可售状态。"""
        return self.client.call("/show/detail", {"showId": str(show_id)}).data or {}

    def ch4_seat_list(self, show_id: str) -> SeatMap:
        """
        座位图。

        ★ 超时设 12 秒 —— 平台整条链路有 10 秒预算，
          设短了会在平台已切到可用供货方并返回时你已经放弃等待。
        ★ 收到 600002 间隔 1~2 秒重试一次即可（默认参数已按此配）。
        """
        resp = self.client.call("/seat/list", {"showId": str(show_id)},
                               timeout=SEAT_LIST_TIMEOUT, retries=1, backoff=1.5)
        return SeatMap.from_data(resp.data or {})

    # ---------------------------------------------------------------- 交易
    def _seat_payload(self, seats: list, *, with_area: bool = True) -> list:
        """
        组装座位数组。

        ★ 分区厅逐座必带 areaId —— 平台不替你猜是哪一区（猜错就是买了另一个价位的票）。
          这里默认带上；调用方从座位图拿到的 areaId 应原样传回。
        """
        out = []
        for s in seats:
            item: dict[str, Any] = {"rowNo": int(s["rowNo"]), "colNo": int(s["colNo"])}
            if s.get("seatNo"):
                item["seatNo"] = str(s["seatNo"])
            if with_area and s.get("areaId") is not None:
                item["areaId"] = str(s["areaId"])
            out.append(item)
        if not 1 <= len(out) <= 6:
            raise ValueError(f"座位数必须在 1~6 之间，收到 {len(out)}")
        return out

    def ch5_preflight(self, show_id: str, seats: list, ticket_mode: str,
                      price_mode: str, *,
                      params: QuoteParams | None = None) -> PreflightResult:
        """
        选座询价。★ 与真实下单跑相同的比价链路，但不建单、不冻结、免费。

        ⚠️ 有限频，**不要当轮询接口刷**。
        """
        if ticket_mode not in TICKET_MODES:
            raise ValueError(f"ticketMode 不合法：{ticket_mode}")
        if price_mode not in PRICE_MODES:
            raise ValueError(f"priceMode 不合法：{price_mode}")
        payload = {
            "showId": str(show_id),
            "seats": self._seat_payload(seats),
            "ticketMode": ticket_mode,
            "priceMode": price_mode,
            **(params or self.quote_params).to_fields(),
        }
        resp = self.client.call("/order/preflight", payload)
        return PreflightResult.from_data(resp.data or {})

    def ch5_create(self, out_order_no: str, show_id: str, seats: list,
                   ticket_mode: str, price_mode: str, tel: str, *,
                   max_price_fen: int | None = None,
                   allow_seat_change: bool = True,
                   notify_url: str | None = None,
                   attach: str | None = None,
                   params: QuoteParams | None = None) -> CreateOrderResult:
        """
        下单。★ 幂等键是 out_order_no。

        ★ 三条必须守住的规则（来自文档）：
          1. 重试【用同一个 outOrderNo】，才能命中幂等拿到首次结果。
          2. 已被拒的单（REJECTED）要【换新的 outOrderNo】，
             否则会一直按首次失败码报错。
          3. 重试必须重新生成 nonce —— 这个在 client 层已保证（每次调用重新签名）。
        """
        if not out_order_no or len(out_order_no) > 64:
            raise ValueError("outOrderNo 必填且 ≤64 字符")
        if not _valid_tel(tel):
            raise ValueError(f"tel 必须是 1[3-9] 开头 11 位：{tel!r}")

        payload: dict[str, Any] = {
            "outOrderNo": out_order_no,
            "showId": str(show_id),
            "seats": self._seat_payload(seats),
            "ticketMode": ticket_mode,
            "priceMode": price_mode,
            "tel": tel,
            "allowSeatChange": allow_seat_change,
            **(params or self.quote_params).to_fields(),
        }
        # ★ maxPrice 在请求里是【数字】，不是字符串（文档明确）
        if max_price_fen is not None:
            payload["maxPrice"] = int(max_price_fen)
        if notify_url:
            payload["notifyUrl"] = notify_url
        if attach:
            payload["attach"] = attach

        # ★ 下单默认【不重试】。
        #   重试是否安全取决于 outOrderNo 的复用策略，那是上层的决定，
        #   不该由客户端偷偷重试 —— 偷偷重试会掩盖"该换单号"这个判断。
        resp = self.client.call("/order/create", payload)
        return CreateOrderResult.from_data(resp.data or {})

    def ch5_order_detail(self, order_no: str) -> OrderDetail:
        return OrderDetail.from_data(
            self.client.call("/order/detail", {"orderNo": order_no}).data or {})

    def ch5_order_list(self, **filters) -> dict:
        return self.client.call("/order/list", {k: v for k, v in filters.items()
                                                if v is not None}).data or {}

    def ch5_cancel(self, order_no: str) -> dict:
        """
        订单拦截（取消）。

        ★ 只有出票中（SUBMITTING / TICKETING）状态可调用，
          否则报 300008；已出票请改走退票。
        """
        return self.client.call("/order/cancel", {"orderNo": order_no}).data or {}

    def ch5_urge(self, order_no: str) -> dict:
        """
        订单催促。

        ★ 未到可催促时间报 300009。
          可催时刻看 order/detail 的 urgeAvailableAt。
          冷却：特惠 20 / 快速 15 / 极速 10 分钟。
        """
        return self.client.call("/order/urge", {"orderNo": order_no}).data or {}

    # ---------------------------------------------------------------- 退票
    def ch5_refund(self, order_no: str, **extra) -> dict:
        return self.client.call("/order/refund",
                                {"orderNo": order_no, **extra}).data or {}

    def ch5_refund_detail(self, order_no: str) -> dict:
        return self.client.call("/order/refund/detail", {"orderNo": order_no}).data or {}

    # ---------------------------------------------------------------- 账户
    def ch6_balance(self) -> dict:
        """余额查询。可用来在下单前预判 400002。"""
        return self.client.call("/account/balance", {}).data or {}

    def ch6_transactions(self, **filters) -> dict:
        return self.client.call("/account/transaction/list",
                                {k: v for k, v in filters.items() if v is not None}).data or {}

    # ---------------------------------------------------------------- 识别
    def ch7_recognize(self, image_url: str, city_name: str | None = None
                      ) -> RecognitionResult:
        """
        同步识别。中位 3.7s / p95 5.2s，超时设 5 秒。

        ★ image_url 必须【公网可读、未过期、无防盗链】—— 模型侧直接读取，平台不下载。
          报 700002 是【我们侧问题】，要查防盗链与有效期。
        ★ city_name 强烈建议传：截图上通常没有城市，
          连锁品牌全国几百家店，不带城市很难收敛。
        """
        if not image_url or len(image_url) > 1024:
            raise ValueError("imageUrl 必填且 ≤1024 字符")
        payload: dict[str, Any] = {"imageUrl": image_url}
        if city_name:
            payload["cityName"] = city_name
        resp = self.client.call("/recognize/seat-shot", payload,
                               timeout=RECOGNIZE_TIMEOUT, retries=1)
        return RecognitionResult.from_data(resp.data or {})

    def ch7_confirm(self, recognize_id: str, *, cinema_id: int | None = None,
                    movie_id: int | None = None, show_id: str | None = None,
                    city_name: str | None = None,
                    seats: list | None = None) -> RecognitionResult:
        """
        二次确认。★ 不重新调模型，也不计费。

        ★ 两条约束：
          1. **只接受本次识别返回过的候选**，拿平台没给过的 ID 得 700009。
          2. 正确答案不在候选里时（候选被截断到前 5），
             要【补 cityName 重新识别】，不要绕过这个接口。
        """
        if not any([cinema_id, movie_id, show_id]):
            raise ValueError("cinemaId / movieId / showId 至少给一个")
        payload: dict[str, Any] = {"recognizeId": str(recognize_id)}
        if cinema_id:
            payload["cinemaId"] = cinema_id
        if movie_id:
            payload["movieId"] = movie_id
        if show_id:
            payload["showId"] = str(show_id)
        if city_name:
            # ★ 文档：识别时传过的城市不会存下来，要接着用得再传
            payload["cityName"] = city_name
        if seats:
            if not show_id:
                raise ValueError("传 seats 时必须同时传 showId")
            payload["seats"] = self._seat_payload(seats)
        resp = self.client.call("/recognize/confirm", payload,
                               timeout=RECOGNIZE_TIMEOUT)
        return RecognitionResult.from_data(resp.data or {})

    def ch7_recognize_async(self, image_url: str, *,
                            out_trade_no: str | None = None,
                            city_name: str | None = None,
                            notify_url: str | None = None) -> dict:
        """异步识别。★ outTradeNo 是幂等号，重复提交返回原任务、不重复计费。"""
        payload: dict[str, Any] = {"imageUrl": image_url}
        if out_trade_no:
            payload["outTradeNo"] = out_trade_no
        if city_name:
            payload["cityName"] = city_name
        if notify_url:
            payload["notifyUrl"] = notify_url
        return self.client.call("/recognize/seat-shot/async", payload).data or {}

    def ch7_task_detail(self, task_id: str) -> dict:
        return self.client.call("/recognize/task/detail",
                                {"taskId": str(task_id)}).data or {}

    # ---------------------------------------------------------------- 便利
    def pick_ticket_mode(self, start_time: str | None,
                         now: datetime | None = None,
                         enabled_tiers: list | None = None) -> str:
        """
        按距开场时间挑票速档位。

        ★ 文档：档位有停售安全边界（极速 2 分钟、快速 5 分钟、特惠 15 分钟）。
          这里只做粗筛，最终以 prices[].available 为准。

        ★★ 2026-10-14 接线：「设置 → 出票及放单 → 票速设置」
          enabled_tiers = settings 的 speed_tiers()，形如
              [("FLASH", 1), ("FAST", 15), ("STANDARD", 40)]
          按时间算出的档位若【被停用】，退回下一个仍启用的档位。
          ★ 不传 ⇒ 行为与接线前完全一致（旧调用一行都不用改）。
          ★ 这个函数保持【纯函数】：不 import settings、不读盘 ——
            读配置是调用方的事，否则它就没法单测了。
          ★ 一个都不启用（配置被全关）⇒ 仍返回算出来的档位：
            宁可照旧出票，也不要因为配置错了就卡死买家。
        """
        if not start_time:
            return TICKET_STANDARD
        try:
            start = datetime.fromisoformat(start_time)
        except ValueError:
            return TICKET_STANDARD
        current = now or datetime.now(start.tzinfo)
        minutes = (start - current).total_seconds() / 60
        if minutes <= 15:
            want = TICKET_FLASH
        elif minutes <= 40:
            want = TICKET_FAST
        else:
            want = TICKET_STANDARD
        if not enabled_tiers:
            return want
        allowed = {str(name) for name, _ in enabled_tiers}
        if not allowed or want in allowed:
            return want
        # 被停用 ⇒ 退回【仍启用的】里最接近的（按剩余分钟从紧到松）
        for name, _need in enabled_tiers:
            if str(name) in allowed:
                return str(name)
        return want


# ==================================================================== 工具
def _valid_tel(tel: str) -> bool:
    """购票预留手机号：1[3-9] 开头 11 位。"""
    if not tel or len(tel) != 11 or not tel.isdigit():
        return False
    return tel[0] == "1" and tel[1] in "3456789"


def make_out_order_no(prefix: str, seq: int, *, when: datetime | None = None) -> str:
    """
    生成我们的单号（幂等键）。

    ★ 要求：渠道内唯一、≤64 字符。
      带时间戳便于排查，带序号避免同秒冲突。

    ★ 注意：**已被拒的单要换新单号**。所以上层要能区分
      "重试同一单"（复用）和"重新下单"（换号）。
    """
    stamp = (when or datetime.now()).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{seq:04d}"[:64]
