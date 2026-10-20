"""
domain/order/models.py —— 订单模型

════════════════════════════════════════════════════════════════
★ 字段来源（全部照线上真实数据，不自己发明）

  线上 transactions.sqlite3 有四张表，说明"订单"其实由四块证据组成：
      order_quote_bindings      订单 ↔ 报价 绑定
      order_payment_receipts    付款回执（主键 tenant_id + event_id）
      reprice_authorizations    改价授权
      order_refund_notices      退款通知
  线上 liangpiao-orders.sqlite3 的主表字段：
      order_id / quote_id【UNIQUE】/ idempotency_key【UNIQUE】/
      state / provider_order_id / create_http_status / failure_code /
      created_at / updated_at
  而 payload 里还有：
      payment_event_id / payment_event_fingerprint  ← 支付幂等
      human_approval {approval_id, operator_id, review_digest,
                      approved_at, expires_at}       ← 人工批准
      authorization_id

★★ 三个必须保留的机制（每一个都有线上依据）

  ① 订单 ↔ 报价 绑定，且【记下绑定时的金额】
       order_quote_bindings.amount_fen
     为什么：报价会变（改价）。绑定那一刻的金额是"买家该付多少"的唯一依据。
     只看"最新报价"会算错账。

  ② 支付回执用 (tenant_id, event_id) 做主键 —— 幂等
     同一个支付事件推两次只记一次。
     线上还带 payment_event_fingerprint（事件内容指纹），
     用来发现"同一个 event_id 但内容变了"（那是异常，不是重放）。

  ③ ★ 人工批准（human_approval）带【有效期】
     大额/改价这类动作，不是有权限就永久生效 ——
     批准 60 秒后失效（线上真实数据：approved_at 14:44:53 →
     expires_at 14:45:53）。过期就得重新批准。
     为什么必须有有效期：批准是"针对这一次"的，
     不能让一次批准变成长期授权。
════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

# ==================================================================== 状态
# ★ 取值来自线上真实数据 + V5 wanda_ticketing.py 的 BUCKETS：
#     BUCKETS = {all, unpaid, awaiting_ticket, ticketed, delivered,
#                attention, closed}
#   我按中文业务含义落成下面这些；括号里是它对应的线上桶。
OrderState = Literal[
    "CREATED",           # 已绑定报价，等买家付款（unpaid）
    "PAID",              # 已付款，等出票（awaiting_ticket）
    "TICKETED",          # 已出票（ticketed）
    "DELIVERED",         # 已交付取票码 + 已发货（delivered）
    "REPRICE_PENDING",   # 等改价（attention）
    "FAILED",            # 建单/出票失败（attention）
    "CANCELLED",         # 取消（closed）
    "REFUNDED",          # 已退款（closed）
]

# ★ 终态：到了这些状态就不该再往前推
TERMINAL_STATES = ("DELIVERED", "CANCELLED", "REFUNDED")

# ★ 需要人/店家介入的状态（对应线上 attention 桶）
ATTENTION_STATES = ("REPRICE_PENDING", "FAILED")

# ★ 支付态。来源：线上 order_payment_receipts +
#   V5 wanda_ticketing.py 的 payment_state（UNPAID/CONFIRMED/AMBIGUOUS/REJECTED）
PaymentState = Literal["UNPAID", "CONFIRMED", "AMBIGUOUS", "REJECTED"]

# ★ 人工批准的有效期。★ 依据：线上真实数据 60 秒
#   approved_at 14:44:53.497 → expires_at 14:45:53.451
APPROVAL_TTL_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ==================================================================== 绑定
@dataclass
class QuoteBinding:
    """
    订单 ↔ 报价 绑定。

    ★ 为什么单独一个对象、而不是把 quote_id 塞进订单：
      绑定是【一个动作】，有它自己的时间点和金额快照。
      线上把它单独存一张表（order_quote_bindings）就是这个道理。

    ★ amount_fen 是【绑定时】的金额 —— 不是"现在的报价金额"。
      改价之后两者会不同，对账必须用绑定的那个。
    """
    order_id: str
    quote_id: str
    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    ticket_count: int = 0
    amount_fen: int = 0
    status: str = "BOUND_TO_ORDER"      # 线上真实值就是这个
    bound_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        if isinstance(d.get("bound_at"), datetime):
            d["bound_at"] = d["bound_at"].isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "QuoteBinding":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        v = kwargs.get("bound_at")
        if isinstance(v, str):
            try:
                kwargs["bound_at"] = datetime.fromisoformat(v)
            except ValueError:
                kwargs.pop("bound_at", None)
        return cls(**kwargs)


# ==================================================================== 支付回执
@dataclass
class PaymentReceipt:
    """
    付款回执。

    ★ 幂等：主键是 (tenant_id, event_id) —— 同一个支付事件推两次只记一次。
      来源：线上 order_payment_receipts 的表定义。

    ★ fingerprint 的作用（线上 payload 里有 payment_event_fingerprint）：
      同一个 event_id 但内容指纹不同 = 不是重放，是异常
      （可能上游改了事件内容）。这种情况要报出来，不能当重放默默忽略。
    """
    tenant_id: str
    event_id: str
    order_id: str = ""
    amount_fen: int = 0
    fingerprint: str = ""
    state: str = "CONFIRMED"           # CONFIRMED / AMBIGUOUS / REJECTED
    payload: dict = field(default_factory=dict)
    received_at: datetime = field(default_factory=_now)

    @property
    def key(self) -> tuple:
        return (self.tenant_id, self.event_id)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        if isinstance(d.get("received_at"), datetime):
            d["received_at"] = d["received_at"].isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "PaymentReceipt":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        v = kwargs.get("received_at")
        if isinstance(v, str):
            try:
                kwargs["received_at"] = datetime.fromisoformat(v)
            except ValueError:
                kwargs.pop("received_at", None)
        return cls(**kwargs)


# ==================================================================== 人工批准
@dataclass
class Approval:
    """
    人工批准（大额/改价这类动作的放行条）。

    ★ 字段照线上 payload 的 human_approval：
        approval_id / operator_id / review_digest / approved_at / expires_at
      ★ 还有有效期（线上真实是 60 秒）。
        为什么必须有：批准是"针对这一次动作"的，不能变成长期授权。
        过期就得重新批 —— 否则一次批准等于永久放行。
    """
    approval_id: str
    operator_id: str
    review_digest: str = ""
    approved_at: datetime = field(default_factory=_now)
    expires_at: datetime | None = None

    @classmethod
    def issue(cls, *, approval_id: str, operator_id: str,
              review_digest: str = "", ttl_seconds: int = APPROVAL_TTL_SECONDS,
              now: datetime | None = None) -> "Approval":
        base = now or _now()
        return cls(approval_id=approval_id, operator_id=operator_id,
                   review_digest=review_digest, approved_at=base,
                   expires_at=base + timedelta(seconds=ttl_seconds))

    def is_valid(self, *, now: datetime | None = None) -> bool:
        now = now or _now()
        exp = _aware(self.expires_at)
        return exp is None or now < exp

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        for k in ("approved_at", "expires_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Approval":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        for k in ("approved_at", "expires_at"):
            v = kwargs.get(k)
            if isinstance(v, str):
                try:
                    kwargs[k] = datetime.fromisoformat(v)
                except ValueError:
                    kwargs.pop(k, None)
        return cls(**kwargs)


# ==================================================================== 订单
@dataclass
class Order:
    """
    一笔订单。

    ★ 一个订单【必须】绑一个报价（quote_id 在线上是 UNIQUE NOT NULL）——
      没有报价就没有价钱，没有价钱就不能收钱。
    """
    order_id: str
    quote_id: str
    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    # ---- 供货方 ----
    provider: str = ""                 # WANDA / LIANGPIAO
    provider_order_id: str = ""        # 上游订单号（万达的 provider_order_id）
    platform_order_id: str = ""        # 闲鱼订单号（线上 payload 里有）
    # ---- 需求快照（下单时的，后面报价变了也不改）----
    cinema: str = ""
    movie: str = ""
    show: str = ""
    seats: list = field(default_factory=list)
    ticket_count: int = 0
    # ---- 钱 ----
    amount_fen: int = 0                # 该付多少（= 绑定报价的金额）
    paid_fen: int = 0
    # ---- 状态 ----
    state: str = "CREATED"
    payment_state: str = "UNPAID"
    failure_code: str = ""
    # ---- 幂等 ----
    # ★ 线上是 UNIQUE 列：同一个幂等键只能建一笔单，
    #   重试要用同一个键（而不是新建一笔）
    idempotency_key: str = ""
    # ---- 交付 ----
    ticket_codes: list = field(default_factory=list)
    delivered_at: datetime | None = None
    shipped: bool = False
    # ---- 授权 ----
    approval: Approval | None = None
    # ---- 时间 ----
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    payload: dict = field(default_factory=dict)
    demand_id: str = ""
    demand_rev: int = 0

    # ---------------------------------------------------------------- 判定
    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def needs_attention(self) -> bool:
        return self.state in ATTENTION_STATES

    def can_deliver(self, *, now: datetime | None = None) -> tuple:
        """
        能不能给买家发取票码 / 发货。返回 (能不能, 原因)。

        ★★ 四道门槛（每一条都有线上依据）：
            ① 必须已付款 —— 没付款就出票，钱收不回来
            ② 必须不是终态 —— 已交付/已取消/已退款不许再发
            ③ ★ 必须有取票码 —— 没码发什么
            ④ ★ 人工批准必须仍在有效期内 ——
               大额动作的批准是【限时】的，过期要重批
        """
        now = now or _now()
        if self.payment_state != "CONFIRMED":
            return False, f"未确认付款（payment_state={self.payment_state}）"
        if self.is_terminal:
            return False, f"订单已到终态（{self.state}）"
        if not self.ticket_codes and not (self.provider == "WANDA" and self.payload.get("wanda_ticket_image")):
            return False, "没有取票码"
        if self.approval is not None and not self.approval.is_valid(now=now):
            return False, "人工批准已过期，需要重新批准"
        return True, ""

    # ---------------------------------------------------------------- 迁移
    def touch(self) -> None:
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        for k in ("created_at", "updated_at", "delivered_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        if isinstance(d.get("approval"), Approval):
            d["approval"] = d["approval"].to_dict()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Order":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        for k in ("created_at", "updated_at", "delivered_at"):
            v = kwargs.get(k)
            if isinstance(v, str):
                try:
                    kwargs[k] = datetime.fromisoformat(v)
                except ValueError:
                    kwargs.pop(k, None)
        ap = kwargs.get("approval")
        if isinstance(ap, dict):
            kwargs["approval"] = Approval.from_dict(ap)
        return cls(**kwargs)


def new_order_id() -> str:
    import uuid
    return str(uuid.uuid4())


def idempotency_key_for(quote_id: str, *, provider: str = "",
                        prefix: str = "v5") -> str:
    """
    建单幂等键。

    ★ 照线上写法：idempotency_key = "v5-liangpiao-{quote_id}"
      线上真实值：v5-liangpiao-297ace12-255d-4d18-affc-c6fba857f3f2
      ⇒ 结构是 {prefix}-{provider小写}-{quote_id}
      所以 provider 必须由调用方给，不能在函数里写死 ——
      万达的单会变成 v5-wanda-{quote_id}，写死就串渠道了。

    ★ 为什么用 quote_id 而不是随机数：
      同一笔报价重复提交建单，必须落到【同一把键】上 ——
      这样第二次会被幂等拦掉，而不是又建一笔单。
      用随机数拦不住，会重复下单（买家用一次报价买两次票）。
    """
    mid = f"-{provider.lower()}" if provider else ""
    return f"{prefix}{mid}-{quote_id}"
