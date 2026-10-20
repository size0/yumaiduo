"""
application/wanda_delivery.py —— 万达出票交付

════════════════════════════════════════════════════════════════
★ 这个流程是谁在动

  和良票【完全不同】：

    良票：我们下单 → 上游自动出票 → 回调 → 我们发取票码     （全自动）
    万达：买家付款 → 系统说"店家处理出票"
          → ★ 店家在出票后台手动出票
          → 后台把取票码交给系统
          → 系统自动发取票码 + 自动发货                   （人工介入）

  所以这里的核心不是"调接口下单"，而是【接收店家交来的出票结果】，
  并且保证这件事只发生一次、有人负责、不会拿旧结果覆盖新的。

★ 三个机制（全部照线上 V5 的 application/wanda_delivery.py）

  ① 出票必须【有人操作】（delivery_operator）
     没有 operator 直接拒绝（DELIVERY_NOT_ENABLED）。
     为什么：出票是花钱落地的动作，不能让自动流程悄悄做完。

  ② 【内容指纹】去重（delivery_review_digest）
     同一笔订单重复提交同样的内容 → 返回已有那条（幂等，不是错误）
     同样订单提交【不同】内容     → TICKET_ALREADY_RECORDED（拒绝，不覆盖）
     ★ 为什么两者要分开：重放是正常的（网络重试），
       内容变了是异常的（可能交错了码）—— 前者放行，后者必须拦。

  ③ 提交有【有效期】（delivery_expires_at，线上是 5 分钟）
     提交后 5 分钟内必须执行完，否则作废要重提。
     为什么：出票命令连着"这一刻的订单状态"，
            放太久再执行可能对的是一个已经变了的单。

★ 另外：改了取票码要【换版本】，不是覆盖（ticket_version + 1）
     线上 correction 分支就是这么做的，而且要求原命令是"已发的纯文本"。

★★ 交付内容可以是【文本】或【图片】，但只能选一种
     线上 wanda_delivery.py:68 的判据：bool(text) == bool(image) 就报错
     （ONE_TICKET_MESSAGE_REQUIRED）。
     为什么：一条命令 = 一次外部发送。两个都给的话，
     插件那边只能发一个，另一个被静默丢掉 —— 于是"我们以为发了，
     买家没收到"。

★★ 图片有硬校验（线上 wanda_delivery.py:70-77）：
     ① image 的键必须【正好】是 {imageUrl, width, height}
     ② imageUrl 必须 https + alicdn.com（或 *.alicdn.com）
     ③ width/height 必须是 int 且 0 < n <= 20000
     ★★ 为什么必须卡 alicdn：
       插件只认 alicdn 图片地址（IMAGE_HOSTS=['alicdn.com','tbcdn.cn']）。
       不是 alicdn 的图，插件发不出去 —— 命令会永远卡着，
       买家收不到票。⇒ 宁可在入口拒掉，也不要收下一条发不出去的命令。
════════════════════════════════════════════════════════════════
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from domain.order import Order, OrderStore, idempotency_key_for, new_order_id

# ==================================================================== 常量
# ★ 提交有效期：线上真实是 5 分钟
DELIVERY_TTL_MINUTES = 5

# ★ 交付契约：线上真实值（SHIP_ORDER payload 里的 delivery_contract）
DELIVERY_CONTRACT_QR = "QR_SHIP_V1"

# ★ 取票码形态：纯文本（线上真实 text 是 "取票码：2032 5500 7913 72"）
TICKET_TEXT_PREFIX = "取票码："

# ★★ 图片允许的域名。★ 照插件源码 IMAGE_HOSTS = ['alicdn.com','tbcdn.cn']
#   与线上校验（hostname == 'alicdn.com' 或 endswith('.alicdn.com')）。
IMAGE_HOSTS = ("alicdn.com", "tbcdn.cn")

# ★ 图片尺寸上限（线上是 0 < n <= 20000）
IMAGE_MAX_SIDE = 20000


class DeliveryError(Exception):
    """交付被拒。code 用于排查，message 给人看。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# ==================================================================== 提交体
@dataclass
class DeliverySubmission:
    """
    店家出票后台交来的一次出票结果。

    ★ 字段对应线上 submit 的 body：
        submission_id    本次提交的唯一标识（幂等用）
        ticket_codes     取票码（一到多个）
        provider_order_id 上游（万达）订单号
        source           AUTO / MANUAL —— 线上用来判冲突
        replaces_text_command_id  改码时要指向原命令（换版本用）
    """
    submission_id: str
    ticket_codes: list = field(default_factory=list)
    provider_order_id: str = ""
    source: str = "MANUAL"              # AUTO（自动交来）/ MANUAL（人工）
    replaces_text_command_id: str = ""
    note: str = ""
    # ★ 要发的图（替代纯文本）。形状必须【正好】是
    #   {"imageUrl": str, "width": int, "height": int}
    image: dict | None = None

    def text(self) -> str:
        """
        拼成发给买家的文本。★ 形态照线上："取票码：2032 5500 7913 72"。

        ★ 有图时【不返回文本】—— 一条命令只能发一次，
          两个都给会被线上判成 ONE_TICKET_MESSAGE_REQUIRED。
        ★★ 注意：这里返回空【不代表买家没给取票码】。
          判"调用方给了哪种内容"要用 has_codes()，
          不能用 bool(text()) —— 见 has_codes 的说明。
        """
        if self.image:
            return ""
        codes = "、".join(c for c in self.ticket_codes if c)
        return f"{TICKET_TEXT_PREFIX}{codes}" if codes else ""

    def has_codes(self) -> bool:
        """
        调用方有没有提供取票码。

        ★★ 为什么必须单独一个方法（踩过）：
          text() 在有图时会返回空串（为了让图上不带文字），
          所以用 bool(text()) 判"给了文本吗"会把
          【同时给了码和图】误判成"只给了图" ——
          于是那条命令悄悄只发了图，文本被丢掉，而没人知道。
        """
        return any(str(c).strip() for c in (self.ticket_codes or []))

    def digest(self) -> str:
        """
        内容指纹 —— 用来判"这次提交和上次是不是一样"。

        ★ 只把【会决定交付内容】的东西算进去：
          取票码 + 上游单号 + source + 图。
          ★ 不算 submission_id：它每次提交都不同，
            算进去就永远不相等，幂等就失效了。
        ★ 图的哈希要对【排序后的键】算 —— dict 顺序不该影响指纹。
        """
        raw = "|".join([
            ",".join(self.ticket_codes or []),
            self.provider_order_id or "",
            self.source or "",
            json_dumps_sorted(self.image) if self.image else "",
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def json_dumps_sorted(obj) -> str:
    """稳定序列化（键排序）—— 指纹不能受 dict 顺序影响。"""
    import json
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def validate_image(image) -> None:
    """
    校验交付图片。不合法就抛（不静默接受）。

    ★★ 三条判据逐字照线上 wanda_delivery.py:71-77：
        ① 键必须【正好】是 {imageUrl, width, height}
           ★ "正好"的意思是：多一个键也拒。因为插件按这三个键读，
             多给的字段说明调用方理解错了协议。
        ② imageUrl 必须 https + 允许的域名（alicdn 系）
        ③ width/height 必须是 int（不是 bool、不是 str）且 0<n<=20000

    ★ 为什么卡 alicdn（链路文档第 3 条约束）：
      插件只认 alicdn 图片地址。不是 alicdn 的图，插件发不出去 ——
      命令会永远卡着，买家收不到票。
      ⇒ 宁可在入口拒掉，也不要收下一条注定发不出去的命令。
    """
    if not isinstance(image, dict) or set(image) != {"imageUrl", "width", "height"}:
        raise DeliveryError("INVALID_TICKET_IMAGE",
                            "图片必须正好含 imageUrl/width/height 三个键，"
                            f"实际 {sorted(image) if isinstance(image, dict) else type(image).__name__}")
    url = urlsplit(str(image["imageUrl"]))
    host = (url.hostname or "").lower()
    host_ok = any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS)
    if url.scheme != "https" or url.username or url.password or not host_ok:
        raise DeliveryError(
            "INVALID_TICKET_IMAGE",
            f"图片地址必须是 https + {'/'.join(IMAGE_HOSTS)}，实际 {image['imageUrl']!r}")
    for k in ("width", "height"):
        v = image[k]
        # ★ type(v) is not int：排除 bool（True 是 int 的子类）
        if type(v) is not int or not 0 < v <= IMAGE_MAX_SIDE:
            raise DeliveryError(
                "INVALID_TICKET_IMAGE",
                f"{k} 必须是 1~{IMAGE_MAX_SIDE} 的整数，实际 {v!r}")


@dataclass
class DeliveryCommand:
    """
    一次出票交付命令（在系统里排队待执行）。

    ★ 对应线上 yumaiduo_commands 里 type='DELIVER_TICKET' 的那条记录。
    """
    command_id: str
    order_id: str
    quote_id: str
    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    text: str = ""
    status: str = "PENDING"             # PENDING → SENT / FAILED
    digest: str = ""
    operator: str = ""
    provider_order_id: str = ""
    ticket_version: int = 1
    delivery_provider: str = "WANDA"
    delivery_contract: str = DELIVERY_CONTRACT_QR
    expires_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: datetime | None = None
    followups: dict = field(default_factory=dict)   # 自动发货/通知的状态

    def is_expired(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        exp = self.expires_at
        if exp is None:
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now >= exp

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k in ("created_at", "sent_at", "expires_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        return d


# ==================================================================== 交付
class WandaDelivery:
    """
    万达出票交付。★ 它是"接收店家出票结果"的那一层，不调万达接口。
    """

    def __init__(self, orders: OrderStore, *, ttl_minutes: int = DELIVERY_TTL_MINUTES):
        self.orders = orders
        self.ttl_minutes = ttl_minutes
        self.commands: dict[str, DeliveryCommand] = {}

    # ---------------------------------------------------------------- 提交
    def submit(self, order_id: str, body: DeliverySubmission,
               operator: str) -> DeliveryCommand:
        """
        店家交来出票结果。返回命令（新建的，或已有的那条）。

        ★★ 三道闸门（顺序有讲究）：

            ① 必须有人操作
                 线上：`if not operator ... raise DELIVERY_NOT_ENABLED`
                 为什么第一道就查：后面所有动作都建立在"有人负责"之上。

            ② 订单必须已付款
                 没付款就出票，钱收不回来。

            ③ 幂等判据
                 同一订单 + 同样的 digest → 返回已有（重放，放行）
                 同一订单 + 不同的 digest → TICKET_ALREADY_RECORDED（拒绝）
                 ★ 拒绝而不是覆盖：覆盖会把已经发出去的码悄悄换掉。
                   要改码，必须走 correction（带 replaces_text_command_id）。
        """
        if not operator:
            raise DeliveryError("DELIVERY_NOT_ENABLED",
                                "出票必须有人操作，未提供 operator")

        order = self.orders.get(order_id)
        if order is None:
            raise DeliveryError("ORDER_NOT_FOUND", f"订单不存在：{order_id}")

        # ② 必须已付款
        if order.payment_state != "CONFIRMED":
            raise DeliveryError(
                "WANDA_DELIVERY_ORDER_NOT_PAYABLE",
                f"未确认付款（payment_state={order.payment_state}），不能出票")

        digest = body.digest()
        history = self.history(order_id)

        # ③-1 改码：换版本，不覆盖
        if body.replaces_text_command_id:
            old = self.commands.get(body.replaces_text_command_id)
            if old is None or old.status != "SENT":
                raise DeliveryError(
                    "QR_CORRECTION_REQUIRES_CONFIRMED_TEXT",
                    "改取票码只能改一条【已发出】的旧命令")
            return self._insert(order, body, operator, digest,
                                version=old.ticket_version + 1)

        # ③-2 已有记录
        if history:
            last = history[-1]
            if last.digest == digest:
                return last                      # 重放：返回已有
            if (body.source == "AUTO"
                    and last.provider_order_id != body.provider_order_id):
                # 线上：自动交来的单，上游订单号变了 → 冲突
                raise DeliveryError(
                    "WANDA_TICKET_SOURCE_CONFLICT",
                    f"上游订单号变了（{last.provider_order_id} → "
                    f"{body.provider_order_id}），需要人工核对")
            raise DeliveryError(
                "TICKET_ALREADY_RECORDED",
                "这单已经出过票了，内容不同不允许覆盖 —— "
                "要改取票码请走换码（replaces_text_command_id）")

        # ★★ 交付内容：文本 或 图片，只能给一个。
        #   线上判据：bool(text) == bool(image) → ONE_TICKET_MESSAGE_REQUIRED
        #   ★ 为什么：一条命令 = 一次外部发送。两个都给的话插件只能发一个，
        #     另一个被静默丢掉 —— "我们以为发了，买家没收到"。
        #   ★★ 判"有没有文本"要用 has_codes()，不能用 bool(body.text())：
        #      text() 在有图时故意返回空串，用它判会把"码+图都给"
        #      误判成"只给了图"（踩过，测试抓到的）。
        has_text = body.has_codes()
        has_image = bool(body.image)
        if has_text == has_image:
            raise DeliveryError(
                "ONE_TICKET_MESSAGE_REQUIRED",
                "一条交付只能发【文本】或【图片】其中一种"
                + ("（两个都给了）" if has_text else "（两个都没给）"))

        # ★★ 图片硬校验（见 validate_image 的说明）
        if has_image:
            validate_image(body.image)

        # ★ AUTO 来源必须带上游订单号（线上：WANDA_ORDER_ID_REQUIRED）
        if body.source == "AUTO" and not body.provider_order_id:
            raise DeliveryError("WANDA_ORDER_ID_REQUIRED",
                                "自动交来的出票结果必须带上游订单号")

        return self._insert(order, body, operator, digest)

    def _insert(self, order: Order, body: DeliverySubmission, operator: str,
                digest: str, version: int = 1) -> DeliveryCommand:
        now = datetime.now(timezone.utc)
        cmd = DeliveryCommand(
            command_id=new_order_id(),              # 命令 id 用 uuid
            order_id=order.order_id, quote_id=order.quote_id,
            tenant_id=order.tenant_id, shop_id=order.shop_id,
            buyer_id=order.buyer_id, chat_id=order.chat_id,
            text=body.text(), status="PENDING", digest=digest,
            operator=operator, provider_order_id=body.provider_order_id,
            ticket_version=version,
            expires_at=now + timedelta(minutes=self.ttl_minutes),
            created_at=now)
        self.commands[cmd.command_id] = cmd
        # ★ 把码写进订单（终态判定要看它）
        order.ticket_codes = list(body.ticket_codes)
        if body.provider_order_id:
            order.provider_order_id = body.provider_order_id
        self.orders.put(order)
        return cmd

    # ---------------------------------------------------------------- 查询
    def history(self, order_id: str) -> list[DeliveryCommand]:
        """这单的全部交付命令，按时间正序（新的在最后）。"""
        rows = [c for c in self.commands.values() if c.order_id == order_id]
        rows.sort(key=lambda c: c.created_at)
        return rows

    def latest(self, order_id: str) -> DeliveryCommand | None:
        rows = self.history(order_id)
        return rows[-1] if rows else None

    # ---------------------------------------------------------------- 执行
    def send(self, command_id: str) -> DeliveryCommand:
        """
        把取票码发给买家（命令置 SENT）。

        ★★ 三道检查：
            ① 命令必须存在
            ② 不能过期（超过 TTL 要重提 —— 见模块头说明）
            ③ 订单必须仍可交付（付款 + 非终态 + 有码 + 批准有效）
              ★ 这条复用 Order.can_deliver()，不在这里另写一套判据 ——
                两套判据迟早会不一致。
        """
        cmd = self.commands.get(command_id)
        if cmd is None:
            raise DeliveryError("DELIVERY_COMMAND_NOT_FOUND", command_id)
        if cmd.status == "SENT":
            return cmd                              # 幂等：已发过就返回
        if cmd.is_expired():
            raise DeliveryError(
                "DELIVERY_EXPIRED",
                f"提交已超过 {self.ttl_minutes} 分钟，需重新提交出票结果")

        order = self.orders.get(cmd.order_id)
        if order is None:
            raise DeliveryError("ORDER_NOT_FOUND", cmd.order_id)
        ok, why = order.can_deliver()
        if not ok:
            raise DeliveryError("DELIVERY_NOT_ALLOWED", why)

        cmd.status = "SENT"
        cmd.sent_at = datetime.now(timezone.utc)
        return cmd

    def authorize_delivery(self, command_id: str, operator: str) -> DeliveryCommand:
        """
        执行前的一次显式授权（线上叫 authorize_delivery）。

        ★ 为什么除 submit 之外还要一次授权：
          submit 是"店家把结果放进来"，authorize 是"现在真的发出去"。
          两者分开，中间可以有人看一眼 —— 出票是不可逆的动作。
        """
        if not operator:
            raise DeliveryError("DELIVERY_NOT_ENABLED", "授权必须有人")
        cmd = self.commands.get(command_id)
        if cmd is None:
            raise DeliveryError("DELIVERY_COMMAND_NOT_FOUND", command_id)
        cmd.followups["authorized_by"] = operator
        cmd.followups["authorized_at"] = datetime.now(timezone.utc).isoformat()
        return cmd

    def record_result(self, command_id: str, *, ok: bool,
                      detail: str = "") -> DeliveryCommand:
        """记录执行结果（线上 record_delivery_result）。"""
        cmd = self.commands.get(command_id)
        if cmd is None:
            raise DeliveryError("DELIVERY_COMMAND_NOT_FOUND", command_id)
        cmd.followups["result"] = "OK" if ok else "FAILED"
        cmd.followups["result_detail"] = detail
        if not ok:
            cmd.status = "FAILED"
        return cmd

    # ---------------------------------------------------------------- 自动发货
    def queue_followups(self, command_id: str) -> dict:
        """
        发完取票码之后要自动做的两件事（线上是 SHIP_ORDER +
        TICKET_DELIVERY_NOTICE 两条子命令）。

        ★ 这是你说的「出票后自动发送取票码，自动发货」——
          取票码是 send() 发的，发货和通知在这里排队。
        ★ 只排队，不执行 —— 执行要有凭证（execution_token），
          和出票一样属于"要有人负责"的动作。
        """
        cmd = self.commands.get(command_id)
        if cmd is None:
            raise DeliveryError("DELIVERY_COMMAND_NOT_FOUND", command_id)
        if cmd.status != "SENT":
            raise DeliveryError("FOLLOWUP_BEFORE_SEND",
                                "取票码还没发出去，不能先发货")
        cmd.followups.setdefault("SHIP_ORDER", "QUEUED")
        cmd.followups.setdefault("TICKET_DELIVERY_NOTICE", "QUEUED")
        order = self.orders.get(cmd.order_id)
        if order is not None:
            order.state = "DELIVERED"
            order.shipped = True
            order.delivered_at = datetime.now(timezone.utc)
            self.orders.put(order)
        return dict(cmd.followups)

    # ---------------------------------------------------------------- 视图
    def view(self, order_id: str) -> dict:
        """给（店家的）出票后台看的视图。"""
        order = self.orders.get(order_id)
        cmd = self.latest(order_id)
        return {
            "order_id": order_id,
            "state": order.state if order else None,
            "payment_state": order.payment_state if order else None,
            "ticket_codes": list(order.ticket_codes) if order else [],
            "shipped": bool(order.shipped) if order else False,
            "delivery": cmd.to_dict() if cmd else None,
            "delivery_status": cmd.status if cmd else "NOT_SUBMITTED",
        }


def delivery_idempotency(order: Order) -> str:
    """出票类的幂等键（与建单的键区分开，避免混用）。"""
    return idempotency_key_for(order.quote_id, provider="wanda-delivery")
