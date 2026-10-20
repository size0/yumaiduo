"""
domain/quote/models.py —— 报价记录的数据模型

════════════════════════════════════════════════════════════════
★ 这个文件解决什么问题

  在这之前，报价算完就是内存里的一个 QuoteResult，**算完就丢**：
    · 买家问「上次报价多少」→ 只能靠会话里的对象，重启就没了
    · 买家改了需求（换场次/换座位）→ 旧报价和新报价分不清，可能拿旧价下单
    · 出了问题要追「这笔 184 元是怎么算出来的」→ 查不到用了哪版规则

  线上 V5 的 quotes.sqlite3 里是【一笔一行】存着的，字段见下面 QuoteRecord。
  我照它的语义建，字段名与它对齐，这样能直接和线上历史对照。

★ 字段来源说明（不自己发明）
    · 身份四元组 tenant_id/shop_id/buyer_id/chat_id
        → 线上真实记录里有；也是【报价隔离】的保证 ——
          买家只能看到自己的报价
    · provider / request_type
        → 线上真实记录里有（provider="WANDA"，
          request_type="EXACT_SEATS"）
    · cinema/movie/show/seats
        → 线上真实记录里有（show 是 "2026-10-12 19:15" 这种字符串）
    · unit_cost_fen / unit_sell_price_fen / ticket_count
      total_cost_fen / total_sell_price_fen
        → 线上真实记录里有，★ 成本与售价分开存 ——
          这是"能核对毛利"的唯一办法
    · pricing_rule_id / pricing_rule_revision
        → 线上真实记录里有（"settings-52"）。
          ★ 这是本次新补的：没有它，规则改了就无法复现旧报价
    · status / created_at / expires_at
        → 线上真实记录里有；状态见下面的 QuoteStatus
    · metadata
        → 线上真实记录里有，放着 request_snapshot / pricing_rule 等原始依据
════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

# ==================================================================== 状态
# ★ 状态取值来自线上真实记录（quotes.sqlite3 的 status 字段）+ V5 的
#   QuoteStore.update_status / supersede_for_identity。
QuoteStatus = Literal[
    "READY",        # 已报价，等买家确认
    "CONFIRMED",    # 买家确认了（线上记录里就是这个词）
    "SUPERSEDED",   # 买家改了需求 → 旧报价作废（V5 supersede_for_identity）
    "EXPIRED",      # 过了有效期
    "CANCELLED",    # 买家放弃 / 主动取消
]

# ★ 报价默认有效期。★ 依据：线上真实记录里
#   created_at 07:18:44 → expires_at 07:28:44，正好 10 分钟。
DEFAULT_TTL_MINUTES = 10

# ★ 需求类型。来源：线上真实记录
#   request_type="EXACT_SEATS"（有具体座位号）
#   V5 里还有 AREA / WPLUS_AREA 这类（只有区域没有具体座号）
REQUEST_TYPES = ("EXACT_SEATS", "AREA", "WPLUS_AREA", "UNKNOWN")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class QuoteRecord:
    """
    一笔报价的完整记录。

    ★ 这是【给钱之前】那一步的证据链：报价怎么算的、用的哪版规则、
      有效期到什么时候、有没有被后来的需求作废。
    """
    # ---- 身份（四元组，报价隔离的保证）----
    quote_id: str
    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    # ---- 供货与需求 ----
    provider: str = ""                    # WANDA / LIANGPIAO
    request_type: str = "UNKNOWN"         # EXACT_SEATS / AREA / ...
    cinema: str | None = None
    movie: str | None = None
    show: str | None = None               # "2026-10-12 19:15"
    selected_seats: list = field(default_factory=list)
    # ---- 钱（★ 成本与售价分开）----
    unit_cost_fen: int = 0
    unit_sell_price_fen: int = 0
    ticket_count: int = 0
    total_cost_fen: int = 0
    total_sell_price_fen: int = 0
    # ---- ★ 规则版本（没有它就无法复现旧报价）----
    pricing_rule_id: str = ""
    pricing_rule_revision: str = ""
    # ---- 状态与时间 ----
    status: str = "READY"
    created_at: datetime = field(default_factory=_now)
    expires_at: datetime | None = None
    # ---- 原始依据 ----
    metadata: dict = field(default_factory=dict)
    demand_id: str = ""
    demand_rev: int = 0

    # ---------------------------------------------------------------- 派生
    @property
    def margin_unit_fen(self) -> int:
        """单张毛利（分）。★ 负数要能看出来 —— 那是亏本报价。"""
        return self.unit_sell_price_fen - self.unit_cost_fen

    @property
    def margin_total_fen(self) -> int:
        return self.total_sell_price_fen - self.total_cost_fen

    def is_usable(self, *, now: datetime | None = None,
                  require_seats: bool = False) -> bool:
        """
        这笔报价现在还能用吗。

        ★ 三条都要过：
            ① 状态必须是 READY（SUPERSEDED/EXPIRED/CANCELLED 都不能用）
            ② 没过有效期
            ③ ★ 可选的：必须绑定了具体座位
               （下单要 seatNo/rowNo/colNo，只报了个总价的报价不能下单）
        """
        if self.status != "READY":
            return False
        now = now or _now()
        # No quotation TTL for Wanda; a show that has already started is a
        # separate availability constraint, not a quotation deadline.
        if self.provider == "WANDA" and self.show:
            try:
                starts = datetime.fromisoformat(self.show)
                if starts.tzinfo is None:
                    starts = starts.replace(tzinfo=timezone(timedelta(hours=8)))
                current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
                if current >= starts:
                    return False
            except (ValueError, TypeError):
                return False
        if self.provider != "WANDA" and self.expires_at is not None:
            exp = self.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if now >= exp:
                return False
        if require_seats and not self.selected_seats:
            return False
        return True

    def is_order_ready(self) -> bool:
        """A priced order needs confirmed quantity; Wanda area orders need no seat IDs."""
        if (not self.is_usable() or self.metadata.get('inquiry_only') or self.metadata.get("quote_kind") == "UNIT_PRICE"
                or self.ticket_count <= 0 or self.total_sell_price_fen <= 0
                or (self.provider != "WANDA" and self.expires_at is None)):
            return False
        if self.selected_seats:
            return len(self.selected_seats) == self.ticket_count
        return (self.provider == "WANDA" and self.request_type == "WPLUS_AREA"
                and self.metadata.get("quote_kind") == "ORDER_TOTAL")

    def expire_if_needed(self, *, now: datetime | None = None) -> bool:
        """过期了就翻成 EXPIRED，返回是否发生了变化。"""
        if self.status != "READY":
            return False
        now = now or _now()
        if self.provider != "WANDA" and self.expires_at is not None:
            exp = self.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if now >= exp:
                self.status = "EXPIRED"
                return True
        return False

    # ---------------------------------------------------------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        for k in ("created_at", "expires_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "QuoteRecord":
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        for k in ("created_at", "expires_at"):
            v = kwargs.get(k)
            if isinstance(v, str):
                try:
                    kwargs[k] = datetime.fromisoformat(v)
                except ValueError:
                    kwargs.pop(k, None)
        return cls(**kwargs)


def new_quote_id() -> str:
    """新报价 id。★ 用 uuid4 —— 线上记录也是 uuid 形态。"""
    import uuid
    return str(uuid.uuid4())


def default_expiry(*, minutes: int = DEFAULT_TTL_MINUTES,
                   base: datetime | None = None) -> datetime:
    return (base or _now()) + timedelta(minutes=minutes)
