"""
integrations/liangpiao/callbacks.py —— 回调验签与事件解析

════════════════════════════════════════════════════════════════
★ 文档原文（两条都要照做）

  「签名规则与开放 API 完全相同（同样的四个请求头、同样的秒级时间戳与
    HMAC 算法），可直接复用你的验签代码。**请验签后再处理，不要裸信任回调来源。**」

  「你需要在 10 秒内返回 {"code":0}（**平台只看响应体，不看 HTTP 状态码**），
    否则按 15s/30s/1m/5m/15m/30m/1h/3h/6h/12h 退避重试，累计约 24 小时。」

  「同一事件可能因重试被推送多次，请按 orderNo + event 幂等处理
    （**票根类再加 version**）。」

★ 三个容易做错的地方

【1】推送体只有两层，没有信封
      {"event": "order.ticketed", "data": {...}}
    没有 code / requestId。别按 API 响应的形状去解析。

【2】HTTP 状态码不是应答
     平台只看响应体。所以必须把 body 写成 {"code":0}，
     哪怕你内部处理失败 —— 处理失败该进你自己的重试队列，不是让平台重推。
     ★ 反过来说：**验签失败绝不能返回 {"code":0}**，那等于接受了伪造回调。

【3】order.failed 覆盖三种情况，字段是【可选的】
     出票失败     有 failCode / lossAmount / refundAmount
     出票前取消   没有 failCode / lossAmount / refundAmount
     下单未受理   两项都是 "0"
     ⇒ 解析时三个字段都当可选，缺失即视为全额退回。
════════════════════════════════════════════════════════════════
"""

import hmac
import json
from dataclasses import dataclass, field
from hashlib import sha256

# 事件名
EVENT_TICKETED = "order.ticketed"
EVENT_TICKET_UPDATED = "ticket.updated"
EVENT_FAILED = "order.failed"
EVENT_SETTLED = "order.settled"
EVENT_REFUNDED = "order.refunded"
EVENT_REFUND_REJECTED = "order.refund_rejected"
EVENT_RECOGNITION_FINISHED = "recognition.finished"

ALL_EVENTS = (EVENT_TICKETED, EVENT_TICKET_UPDATED, EVENT_FAILED, EVENT_SETTLED,
              EVENT_REFUNDED, EVENT_REFUND_REJECTED, EVENT_RECOGNITION_FINISHED)

# 票根类事件：幂等键要再加 version
TICKET_EVENTS = (EVENT_TICKETED, EVENT_TICKET_UPDATED)

# 应答体。平台只看这个。
ACK_BODY = '{"code":0}'

# 时间戳容差（与 API 一致：偏差超过 5 分钟拒绝）
TIMESTAMP_TOLERANCE_SECONDS = 300


class CallbackError(Exception):
    """回调本身不合法（验签失败、格式不对）。"""


# ==================================================================== 验签
def verify(app_key: str, app_secret: str, headers: dict, raw_body: str,
           *, now: int | None = None, tolerance: int = TIMESTAMP_TOLERANCE_SECONDS,
           seen_nonces: set | None = None) -> None:
    """
    验证回调来源。不通过就抛 CallbackError。

    ★ 和开放 API 用同一套签名算法 —— 但这里验的是【进来的】请求，
      所以用自己的 app_key/app_secret 反算 X-Sign 再比对。

    ★ 必须用 hmac.compare_digest 而不是 == ：
      字符串比较会短路，理论上能被计时攻击逐字节猜出签名。
      这不是杞人忧天 —— 安全代码里这是标准动作。

    ★ 还要查时间戳与 nonce：
      只有签名对不够，重放一个旧请求签名也是对的。
    """
    # 头名大小写不敏感（不同网关会改大小写）
    lower = {k.lower(): v for k, v in headers.items()}

    def head(name: str) -> str:
        return str(lower.get(name.lower(), "") or "")

    got_key, ts, nonce, sign = (head("X-App-Key"), head("X-Timestamp"),
                                head("X-Nonce"), head("X-Sign"))
    if not all([got_key, ts, nonce, sign]):
        raise CallbackError("回调缺少签名请求头")

    if got_key != app_key:
        raise CallbackError(f"X-App-Key 不匹配：{got_key!r}")

    # 时间戳
    try:
        ts_int = int(ts)
    except ValueError as exc:
        raise CallbackError(f"X-Timestamp 不是整数：{ts!r}") from exc
    if len(ts) != 10:
        raise CallbackError(f"X-Timestamp 不是 10 位秒级：{ts!r}")
    if now is not None and abs(now - ts_int) > tolerance:
        raise CallbackError(f"回调时间戳超出 {tolerance} 秒容差：{ts}")

    # nonce 防重放
    if seen_nonces is not None:
        if nonce in seen_nonces:
            raise CallbackError(f"nonce 重复（重放）：{nonce}")
        seen_nonces.add(nonce)

    # 签名：四段拼接，与发出去时完全一致
    expected = hmac.new(
        app_secret.encode("utf-8"),
        f"{got_key}{ts}{nonce}{raw_body}".encode("utf-8"),
        sha256).hexdigest()

    if not hmac.compare_digest(expected, sign):
        raise CallbackError("签名校验失败")


# ==================================================================== 事件
@dataclass
class CallbackEvent:
    """
    一个解析好的回调事件。

    ★ 幂等键（dedupe_key）在这里就算好，不让调用方各算各的 ——
      文档要求 orderNo + event，票根类再加 version，规则统一放一处才不会漏。
    """
    event: str
    data: dict
    raw: dict = field(default_factory=dict)

    # ---- 公共字段 ----
    @property
    def order_no(self) -> str:
        return str(self.data.get("orderNo", ""))

    @property
    def out_order_no(self) -> str:
        return str(self.data.get("outOrderNo", ""))

    @property
    def version(self) -> int:
        return int(self.data.get("version") or 0)

    @property
    def dedupe_key(self) -> str:
        """
        幂等键。

        票根类（order.ticketed / ticket.updated）：event + orderNo + version
          为什么带 version：票根会变更，version 递增，是【新内容】不是重复。
        其余：event + orderNo
        """
        if self.event in TICKET_EVENTS:
            return f"{self.event}:{self.order_no}:{self.version}"
        return f"{self.event}:{self.order_no}"

    @property
    def is_ticket_event(self) -> bool:
        return self.event in TICKET_EVENTS

    # ---- order.failed 的三种情况 ----
    @property
    def fail_code(self) -> str | None:
        """★ 可选字段。出票前取消的场景【没有】它。"""
        value = self.data.get("failCode")
        return str(value) if value not in (None, "") else None

    @property
    def loss_amount_fen(self) -> int:
        """上游有损关单转嫁的损失（分）。缺失即视为 0。"""
        return _to_int(self.data.get("lossAmount"))

    @property
    def refund_amount_fen(self) -> int:
        """★ 可选。取消场景没有它 —— 那种情况是【全额退回】。"""
        value = self.data.get("refundAmount")
        if value in (None, ""):
            return -1                     # -1 = 未知，调用方据此判定"全额退回"
        return _to_int(value)

    @property
    def is_cancelled(self) -> bool:
        """
        是不是"出票前取消"（而不是出票失败）。

        ★ 判据：没有 failCode。
          文档：「出票前取消场景无此字段」。
          这两种情况走同一个事件，但处置完全不同 ——
          取消是买家/我们主动撤单（全额退），失败是上游没出成票（可能有损）。
        """
        return self.event == EVENT_FAILED and self.fail_code is None

    @property
    def is_full_refund(self) -> bool:
        """是否全额退回。字段缺失 = 全额。"""
        if self.event != EVENT_FAILED:
            return False
        return self.refund_amount_fen < 0 or self.loss_amount_fen == 0

    # ---- order.ticketed / ticket.updated ----
    @property
    def tickets(self) -> list:
        return list(self.data.get("tickets") or [])

    @property
    def actual_seats(self) -> list | None:
        """
        ★ 入场座位以这个为准，不要拿 tickets[].seatNo 去猜。
        文档：票码张数对不上时 tickets[].seatNo 为空串。
        """
        value = self.data.get("actualSeats")
        return list(value) if value else None

    @property
    def seat_changed(self) -> bool | None:
        """
        ★ 三态：True 已换座 / False 未换座 / None 对不上或未出票。
        **None 不能当成未换座** —— 那会把"不知道"当成"没换"，可能骗到买家。
        """
        return self.data.get("seatChanged")

    @property
    def ticket_codes(self) -> list:
        """取出所有取票码。一张票上可能有多个码（用 | 分隔）。"""
        codes = []
        for t in self.tickets:
            code = (t or {}).get("ticketCode")
            if code:
                codes.append(str(code))
        return codes

    @property
    def settle_amount_fen(self) -> int | None:
        """
        实扣金额（分）。

        ★ 文档：`settleAmount` 是 `string | null`，**结算前为 null**。

        ★★ 这里踩过一个坑：早先返回 `int`，用 `_to_int` 把 null 吞成 0。
          后果是——如果 `order.settled` 到了但 `settleAmount` 还是 null，
          `冻结 − 实扣` 会算成「退回了全部冻结」，
          看起来像赚了一整单，实际是**假账**。
          返回 None 才能让调用方分辨"结算了但金额未到"和"实扣 0 元"。
        """
        value = self.data.get("settleAmount")
        return None if value in (None, "") else _to_int(value)

    @property
    def freeze_amount_fen(self) -> int:
        return _to_int(self.data.get("freezeAmount"))

    @property
    def estimated_settle_fen(self) -> int | None:
        """
        预计结算价（分）。

        ★ 文档：出票成功即有值，**即这一单你的成本价**，
          与放映结束后的正式结算同一套换算口径，正常情况与最终实扣一致。
          ⇒ 出票后就能知道成本，不用等散场。
        """
        value = self.data.get("estimatedSettleAmount")
        return None if value in (None, "") else _to_int(value)

    def needs_order_detail_lookup(self) -> bool:
        """
        是否需要回查订单详情。

        ★ 两种情况必须回查：
          · order.ticketed 但 tickets 为空（version=0）
            —— 文档：上游确认出票但票根暂取不到，补齐后会再推 ticket.updated
          · 订单类回调【不含】影院/影片/厅名/开场时间
            —— 要发给买家取票消息就必须回查或用自己的存档
        """
        if self.event in TICKET_EVENTS and not self.tickets:
            return True
        return self.event in (EVENT_TICKETED,)


# ==================================================================== 解析
def parse(raw_body: str) -> CallbackEvent:
    """把推送体解析成事件。不验签（验签单独做，责任分开）。"""
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise CallbackError(f"回调不是合法 JSON：{raw_body[:200]}") from exc
    if not isinstance(payload, dict):
        raise CallbackError("回调不是对象")
    event = payload.get("event")
    if not event:
        raise CallbackError("回调缺 event 字段")
    if event not in ALL_EVENTS:
        # ★ 未知事件不抛异常 —— 平台随时可能加新事件，
        #   抛异常会导致我们回非 0 应答，然后被无限重推。
        #   记下来、回 0、跳过，才是正确的演进方式。
        pass
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CallbackError("回调缺 data 字段")
    return CallbackEvent(event=str(event), data=data, raw=payload)


def handle(app_key: str, app_secret: str, headers: dict, raw_body: str,
           *, now: int | None = None, seen_nonces: set | None = None,
           seen_events: set | None = None) -> tuple[CallbackEvent | None, str]:
    """
    一站式处理：验签 → 解析 → 幂等去重。返回 (事件或None, 应答体)。

    ★ 这个函数的返回契约很重要：
      · 验签失败        → 事件 None，应答 **不是** {"code":0}
                          （接受了伪造回调比丢掉一条回调严重得多）
      · 幂等命中        → 事件 None，应答 {"code":0}
                          （已经处理过了，回 0 让平台别再推）
      · 处理成功        → 事件，应答 {"code":0}
      · 未知事件        → 事件 None，应答 {"code":0}（别让平台无限重推）
    """
    try:
        verify(app_key, app_secret, headers, raw_body,
               now=now, seen_nonces=seen_nonces)
    except CallbackError as exc:
        # ★ 绝不回 {"code":0}
        return None, json.dumps({"code": 401, "message": str(exc)},
                                ensure_ascii=False)

    try:
        event = parse(raw_body)
    except CallbackError as exc:
        return None, json.dumps({"code": 400, "message": str(exc)},
                                ensure_ascii=False)

    if event.event not in ALL_EVENTS:
        return None, ACK_BODY

    if seen_events is not None:
        if event.dedupe_key in seen_events:
            return None, ACK_BODY
        seen_events.add(event.dedupe_key)

    return event, ACK_BODY


def _to_int(value) -> int:
    """金额一律是【分、字符串】。宽松转，坏了就 0。"""
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0
