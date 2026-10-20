"""
integrations/yumaiduo/commands.py —— 插件命令队列

════════════════════════════════════════════════════════════════
★ 这个队列解决什么问题

  我们不能直接"叫插件去发一条消息" —— 插件在卖家的店铺里，
  网络方向是【它来问我们】，不是我们推给它。

  ⇒ 所以我们把要做的事写成一条【命令】存下来，
    插件每隔几秒来问一次"有没有要我做的"，领走、执行、回报。

     我们：产生命令（PENDING）
     插件：claim 领走（CLAIMED）→ 执行 → POST 结果（SENT / FAILED）

★★ 四个必须守住的点（每个都有线上依据）

  ① 幂等键（dedupe_key）—— 同一条命令只能产生一次
     一条"给买家发改价通知"的命令，如果生成了两次，
     买家会收到两条一样的消息。用 event_id 做去重。

  ② claim 是【原子】的 —— 同一个命令不能被两个插件实例领走
     插件可能多开（重试、灰度）。两个实例同时 claim 到同一条命令，
     买家就会收到两遍。
     ★ V5 用 SELECT ... WHERE status='PENDING' 再 UPDATE，
       我在同一把锁里做"选中即改状态"，保证只有一个赢。

  ③ not_before —— 命令可以【定时】执行
     比如报价后要等 30 秒再催。V5 的做法：没到时间就 claim 不到。
     ★ 时间戳读不出来的话【也不给领】（fail closed）——
       宁可晚一点执行，不能让一个外部写操作提前跑。

  ④ 状态机单向 —— 已发出的不许回退
     PENDING → CLAIMED → SENT / FAILED / SUPPRESSED
     ★ SENT 是终态。一条已经发给买家的消息不能"取消发"，
       所以不能让状态从 SENT 回到 PENDING。
════════════════════════════════════════════════════════════════
"""

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock

# ★ 命令类型。★ 这六个是 V5 用到的全部（domain/conversation/commands.py）：
#   SEND_MESSAGE        给买家发消息
#   CHANGE_ORDER_PRICE  改价
#   POLISH_SHOP         抛商品（擦亮）
#   DELIVER_TICKET      出票（发取票码）
#   TICKET_DELIVERY_NOTICE  出票通知
#   SHIP_ORDER          发货
#   CLOSE_FAILED_ORDER  关掉一张不该继续的单（2026-10-14 补）
#       ★ 插件端 failed-order-close.mjs 早就实现了它（live 闸门 + 身份校验
#         + 只许已付款未发货 + 调 /authorize-close + orders.cancel + 读回），
#         但后端从来没生成过这个类型、/authorize-close 也一律回
#         NOT_IMPLEMENTED —— 所以这条路一次都没通过。
#       ★ 用途：金额不符（AMOUNT_MISMATCH）、没报价就付款（PAID_WITHOUT_QUOTE）
#         这两条 side_effect=REFUND_AND_CLOSE 的转移。
#       ★ 它【不退钱】：文档明确 cancel 不会自动退买家，
#         退钱要买家发起退款申请、卖家同意。
COMMAND_TYPES = ("SEND_MESSAGE", "CHANGE_ORDER_PRICE", "POLISH_SHOP",
                 "DELIVER_TICKET", "TICKET_DELIVERY_NOTICE", "SHIP_ORDER",
                 "CLOSE_FAILED_ORDER")

# ★ 状态。取值与 V5 一致，另加插件会报的几种。
#
# ★★ SENDING 是"已经授权、正在发"的中间状态：
#     插件 message-history.mjs 的注释里明确提到
#     "Authorization may already have moved CLAIMED to SENDING"。
#     它的用处：授权那一刻就把 CLAIMED 推到 SENDING，
#     这样超时重试时不会【重复授权同一个动作】。
# ★★ WOULD_SEND 是【shadow 模式】的结果（插件 execute-command.mjs:27）：
#     result = {status:'WOULD_SEND', provider_result_status:'shadow'}
#     意思是"本该发但没真发" —— 影子模式不碰闲鱼，只演练。
#     ★ 踩过：状态表里没有它，被当成未知结果 → 标成 FAILED，
#       于是影子模式下每条命令都显示"失败"，看不出真实情况。
#     ★ 它必须是个【独立的终态】，不能混进 SENT（那会以为真发出去了）。
STATUSES = ("PENDING", "CLAIMED", "SENDING", "SENT", "FAILED", "SUPPRESSED",
            "UNKNOWN", "GATE_REJECTED", "WOULD_SEND", "SUCCEEDED")

# ★★ 可以授权的状态。
#
#   ★ 为什么必须【只】是这两个（踩过）：
#     我第一版写成了"不是终态就放行"，于是 PENDING（还没被任何插件实例领走）
#     也拿到了授权 —— 那意味着别的实例可以拿着授权去执行
#     一条不属于它的命令。V5 的判据是 command.status == 'CLAIMED'。
#   ★ SENDING 也放行：那是同一实例的授权重试
#     （它已经过了一次授权，正在发）。
AUTHORIZABLE_STATUSES = ("CLAIMED", "SENDING")

# ★★ 哪些状态是【真正终结】的 —— 只有这两个不许被改写。
#
#   ★ 为什么 FAILED 和 UNKNOWN 不是终态（这是我第一版写错的地方）：
#     插件源码里临时失败报的是 UNKNOWN（execution-journal.mjs 会重试），
#     之后才报确定结果。如果把 UNKNOWN/FAILED 当终态挡住重报，
#     ★ 那次【真的发出去了】的消息会被永远记成失败 ——
#     于是系统以为没发、可能再发一遍，买家收到两条。
#
#   ★ 为什么 SENT 是终态：一条已经发给买家的消息不能"取消发"，
#     也不能被改回失败（否则会重发）。
#   ★ 为什么 SUPPRESSED 是终态：它是"确定不做了"（前置失败），
#     没有重试的语义。
IMMUTABLE_STATUSES = ("SENT", "SUPPRESSED", "WOULD_SEND", "SUCCEEDED")

# ★ 执行被判定的结果（插件报的 status 全集）。留着便于校验。
PLUGIN_RESULT_STATUSES = ("SENT", "FAILED", "SUPPRESSED", "UNKNOWN",
                          "UNKNOWN_RESULT", "GATE_REJECTED",
                          "WOULD_SEND", "SUCCEEDED")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS yumaiduo_commands (
    command_id TEXT PRIMARY KEY,
    event_id TEXT,
    session_id TEXT,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cmd_status ON yumaiduo_commands (status, created_at);
CREATE INDEX IF NOT EXISTS idx_cmd_session ON yumaiduo_commands (session_id);

-- ★ 事件表：插件推来的买家消息落这里。
--   为什么要存（而不是收到就处理完丢掉）：
--     · 事件可能重复推（插件重试）—— 要能按 event_key 去重
--     · 处理失败要能重放 —— 丢了就永远丢了
CREATE TABLE IF NOT EXISTS yumaiduo_events (
    event_key TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    session_id TEXT,
    raw TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_evt_status ON yumaiduo_events (status, received_at);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Command:
    """一条待执行（或已执行）的命令。"""
    command_id: str
    type: str
    status: str = "PENDING"
    event_id: str = ""
    session_id: str = ""
    # 身份四元组 —— 插件按这个知道该操作哪个店哪个买家
    tenant_id: str = ""
    shop_id: str = ""
    buyer_id: str = ""
    chat_id: str = ""
    # 内容
    text: str = ""
    image: str = ""
    # ★ 幂等键：同一条命令只能产生一次（用 event_id 派生）
    dedupe_key: str = ""
    # ★ 定时：没到时间 claim 不到
    not_before: str = ""
    # 依赖：前置命令必须已发出（比如"出票"要在"改价通知"之后）
    depends_on_command_id: str = ""
    # 执行结果
    sent_message_id: str = ""
    failure_reason: str = ""
    created_at: str = field(default_factory=lambda: _now().isoformat())
    state_changed_at: str = ""
    # ★★ 插件要读的额外扁平字段。
    #
    #   ★ 为什么需要这个（照插件源码一条条对出来的）：
    #     插件执行时读的是【顶层字段】：
    #       command.shop_id / tenant_id / buyer_id / chat_id   → 身份，已有
    #       command.history_message_ids                      → 发消息前查重
    #       command.order_id                                 → 改价/发货/出票要
    #       command.delivery_provider / delivery_contract     → 出票路径判断
    #       command.provider_order_id / execution_token       → 出票
    #       command.ticket_version / quote_id                 → 出票版本
    #     少一个，插件那条路径就会走到异常分支（比如
    #     message-history.mjs:26 会因为 history_message_ids 不是数组直接 SUPPRESSED）。
    #   ★ 用 extra 而不是全塞进 dataclass：这些字段只有部分命令用得上，
    #     全加成字段会让 SEND_MESSAGE 也背一堆 None。
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """
        给插件的形状：★ 全部【扁平】在顶层。

        ★ 为什么必须扁平：插件读 command.shop_id 而不是 command.identity.shop_id。
          嵌套的话插件读不到，会一路走到失败分支。
        """
        d = asdict(self)
        extra = d.pop("extra", None) or {}
        d.update(extra)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Command":
        """
        从存下来的 payload 还原。

        ★★ 必须用这个、不能 Command(**payload) ——
          to_dict() 把 extra 里的字段【扁平】展开了，
          直接展开成参数会 TypeError（踩过：
          unexpected keyword argument 'history_message_ids'）。

        ★ 已知字段直接取，其余全塞回 extra ——
          这样将来插件新增要读的字段，存下来也能原样还回去。
        """
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs = {}
        extra = dict(data.get("extra") or {})
        for k, v in (data or {}).items():
            if k == "extra":
                continue
            if k in known:
                kwargs[k] = v
            else:
                extra[k] = v
        kwargs["extra"] = extra
        return cls(**kwargs)

    def is_due(self, *, now: datetime | None = None) -> bool:
        """
        到时间了吗。

        ★ 时间戳读不出来 → 返回 False（不执行）。
          fail closed：宁可晚一点，不能让外部写操作提前跑。
        """
        if not self.not_before:
            return True
        due = _parse(self.not_before)
        if due is None:
            return False
        return due <= (now or _now())


class CommandStore:
    """命令队列的存储。"""

    def __init__(self, database: str = ":memory:"):
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.lock = RLock()
        with self.lock:
            self.connection.executescript(_SCHEMA)
            self.connection.commit()

    # ---------------------------------------------------------------- 事件
    def record_event(self, event_key: str, event_type: str, raw: str,
                     session_id: str = "", status: str = "RECEIVED") -> tuple:
        """
        记一个插件推来的事件。返回 (是否新记, 已有或本条的状态)。

        ★ 幂等：同 event_key 再来就当重放，不重复处理。
          插件网络重试很常见，不去重会重复回复买家。

        ★★ status 必须能由调用方给（默认 RECEIVED）——
          踩过：这里写死了 "RECEIVED"，插件签名没过的伪造事件
          也被标成"待处理"，于是 run-due 会去处理伪造消息
          （可能让系统照着伪造内容下单）。
          未验签的要用 "UNVERIFIED"，那个状态【不在】待处理里。
        """
        with self.lock:
            row = self.connection.execute(
                "SELECT status FROM yumaiduo_events WHERE event_key=?",
                (event_key,)).fetchone()
            if row is not None:
                return False, row[0]
            try:
                self.connection.execute(
                    "INSERT INTO yumaiduo_events (event_key, event_type,"
                    " session_id, raw, status, received_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (event_key, event_type, session_id, raw, status,
                     _now().isoformat()))
                self.connection.commit()
            except sqlite3.IntegrityError:
                return False, status
        return True, status

    def mark_event(self, event_key: str, status: str) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE yumaiduo_events SET status=?, processed_at=? "
                "WHERE event_key=?", (status, _now().isoformat(), event_key))
            self.connection.commit()

    def pending_events(self, limit: int = 50) -> list:
        """还没处理完的事件（用于重放）。"""
        with self.lock:
            rows = self.connection.execute(
                "SELECT raw FROM yumaiduo_events WHERE status='RECEIVED' "
                "ORDER BY received_at LIMIT ?", (max(0, min(limit, 200)),)).fetchall()
        out = []
        for (raw,) in rows:
            try:
                out.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        return out

    # ---------------------------------------------------------------- 建命令
    def enqueue(self, command: Command) -> tuple:
        """
        入队。返回 (是否新入队, 命令)。

        ★ 按 dedupe_key 去重：同一个 dedupe_key 只能有一条。
          已经有的话返回已有的那条（不报错）——
          因为"重复产生同一条命令"是正常的（事件被重放），不是异常。
        """
        if command.type not in COMMAND_TYPES:
            raise ValueError(f"COMMAND_TYPE_UNKNOWN: {command.type}")
        if not command.dedupe_key:
            raise ValueError("COMMAND_DEDUPE_KEY_REQUIRED")
        if not command.command_id:
            command.command_id = str(uuid.uuid4())
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM yumaiduo_commands WHERE dedupe_key=?",
                (command.dedupe_key,)).fetchone()
            if row is not None:
                return False, Command.from_dict(json.loads(row[0]))
            try:
                self.connection.execute(
                    "INSERT INTO yumaiduo_commands (command_id, event_id,"
                    " session_id, type, status, dedupe_key, created_at, payload)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (command.command_id, command.event_id or None,
                     command.session_id or None, command.type, command.status,
                     command.dedupe_key, command.created_at,
                     json.dumps(command.to_dict(), ensure_ascii=False)))
                self.connection.commit()
            except sqlite3.IntegrityError:
                row = self.connection.execute(
                    "SELECT payload FROM yumaiduo_commands WHERE dedupe_key=?",
                    (command.dedupe_key,)).fetchone()
                return False, Command.from_dict(json.loads(row[0]))
        return True, command

    # ---------------------------------------------------------------- 读
    def get(self, command_id: str) -> Command | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM yumaiduo_commands WHERE command_id=?",
                (command_id,)).fetchone()
        return Command.from_dict(json.loads(row[0])) if row else None

    def by_status(self, status: str) -> list:
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload FROM yumaiduo_commands WHERE status=? "
                "ORDER BY created_at", (status,)).fetchall()
        return [Command.from_dict(json.loads(r[0])) for r in rows]

    # ---------------------------------------------------------------- 领命令
    def claim_pending(self, limit: int = 10, *,
                      now: datetime | None = None) -> list:
        """
        插件来领待执行的命令。

        ★★ 三个原子性要求：
            ① 同一命令不能被领两次（同一个锁里"选中即改状态"）
            ② 没到 not_before 的不给
            ③ 前置依赖没成功的不给（并把它标成 SUPPRESSED）
        """
        limit = max(0, min(int(limit or 0), 100))
        now = now or _now()
        claimed = []
        with self.lock:
            rows = self.connection.execute(
                "SELECT command_id, payload FROM yumaiduo_commands "
                "WHERE status='PENDING' ORDER BY created_at").fetchall()
            for command_id, raw in rows:
                if len(claimed) >= limit:
                    break
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                cmd = Command.from_dict(payload)
                # ② 没到时间 → 跳过（定时命令）
                if not cmd.is_due(now=now):
                    continue
                # ③ 前置依赖
                if cmd.depends_on_command_id:
                    parent = self.get(cmd.depends_on_command_id)
                    # ★★ 父命令只有在 SENT 之后才算"过了"。
                    #   非 SENT（含 FAILED / UNKNOWN / GATE_REJECTED）
                    #   一律拦 —— UNKNOWN 意味着"可能没发出去"，
                    #   那种情况下接着发货是错的（钱和票对不上）。
                    wanda_part = (cmd.type == "TICKET_DELIVERY_NOTICE"
                                  and (cmd.extra.get("wanda_template_part") is True
                                       or cmd.extra.get("supplier_template_part") is True))
                    if wanda_part and parent is not None and parent.status in ("PENDING", "CLAIMED"):
                        continue  # Wait for an actual receipt; never suppress in-flight parts.
                    successful_ship = (wanda_part and parent is not None
                                       and parent.type == "SHIP_ORDER" and parent.status == "SUCCEEDED")
                    if parent is None or (parent.status != "SENT" and not successful_ship):
                        # ★ 前置没成功 → 这条主动放弃，不要一直挂着
                        self._set_status(command_id, "SUPPRESSED",
                                         "PRECEDING_COMMAND_NOT_SENT",
                                         now=now)
                        continue
                self._set_status(command_id, "CLAIMED", None, now=now)
                cmd.status = "CLAIMED"
                claimed.append(cmd)
        return claimed

    def _set_status(self, command_id: str, status: str,
                    failure_reason: str | None = None,
                    *, now: datetime | None = None) -> None:
        """改状态。★ 只允许单向：终态不许回退。"""
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM yumaiduo_commands WHERE command_id=?",
                (command_id,)).fetchone()
            if row is None:
                return
            payload = json.loads(row[0])
            if payload.get("status") in IMMUTABLE_STATUSES and status != payload["status"]:
                # ★ 已发出的不许回退 —— 一条发给买家的消息不能"取消发"
                return
            payload["status"] = status
            payload["state_changed_at"] = (now or _now()).isoformat()
            if failure_reason is not None:
                payload["failure_reason"] = failure_reason
            self.connection.execute(
                "UPDATE yumaiduo_commands SET status=?, payload=? "
                "WHERE command_id=?", (status,
                                       json.dumps(payload, ensure_ascii=False),
                                       command_id))
            self.connection.commit()

    # ---------------------------------------------------------------- 回执
    def record_result(self, command_id: str, *, status: str = "",
                      ok: bool | None = None,
                      sent_message_id: str = "",
                      failure_reason: str = "",
                      execution_token: str = "",
                      provider_result_status: str = "",
                      price_after_fen: int | None = None, detail: dict | None = None) -> Command | None:
        """
        插件报执行结果。

        ★ 插件报的是 status 字符串（execution-journal.mjs:113 直接 POST 结果对象），
          取值见 PLUGIN_RESULT_STATUSES。为了兼容也接受 ok=True/False。

        ★★ 关键区分：
             SENT / SUPPRESSED  → 终态，之后不许改
             FAILED / UNKNOWN / GATE_REJECTED → 【可以再报】
               ★ 插件对临时失败先报 UNKNOWN，重试后报确定结果。
                 如果挡着重报，那次真发出去的消息会被永远记成失败。
        """
        cmd = self.get(command_id)
        if cmd is None:
            return None
        if cmd.status in IMMUTABLE_STATUSES:
            return cmd
        # 归一化 status
        if not status:
            status = "SENT" if ok else "FAILED"
        status = str(status).strip().upper()
        if status == "UNKNOWN_RESULT":
            # ★ 插件里 UNKNOWN 和 UNKNOWN_RESULT 是一个意思：
            #   "写操作可能落了，但没拿到确认" —— 归成一个码，便于查。
            status = "UNKNOWN"
        if status not in STATUSES:
            self._set_status(command_id, "FAILED",
                             f"UNKNOWN_RESULT_STATUS:{status}")
            return self.get(command_id)

        if status == "SUCCEEDED" and cmd.type == "SHIP_ORDER":
            import hashlib
            raw = f"{cmd.command_id}|{cmd.type}|{cmd.tenant_id}|{cmd.shop_id}|{cmd.event_id}"
            expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
            proof = detail if isinstance(detail, dict) else {}
            if (execution_token != expected or str(proof.get("orderId")) != cmd.event_id
                    or str(proof.get("accountUnb")) != cmd.shop_id
                    or type(proof.get("orderStatus")) is not int or proof["orderStatus"] not in (3, 4)):
                self._set_status(command_id, "UNKNOWN", "SHIP_RESULT_NOT_VERIFIED")
                return self.get(command_id)
            self._set_status(command_id, "SUCCEEDED", "")
            return self.get(command_id)

        if status == "SUCCEEDED":
            target = cmd.extra.get("target_amount_fen")
            if (cmd.type != "CHANGE_ORDER_PRICE" or type(target) is not int
                    or type(price_after_fen) is not int
                    or price_after_fen != target):
                self._set_status(command_id, "FAILED",
                                 "REPRICE_RESULT_AMOUNT_MISMATCH")
                return self.get(command_id)
            with self.lock:
                row = self.connection.execute(
                    "SELECT payload FROM yumaiduo_commands WHERE command_id=?",
                    (command_id,)).fetchone()
                payload = json.loads(row[0])
                payload["status"] = "SUCCEEDED"
                payload["failure_reason"] = ""
                payload["price_after_fen"] = price_after_fen
                payload["provider_result_status"] = provider_result_status
                payload["state_changed_at"] = _now().isoformat()
                self.connection.execute(
                    "UPDATE yumaiduo_commands SET status=?, payload=? "
                    "WHERE command_id=?", ("SUCCEEDED",
                                     json.dumps(payload, ensure_ascii=False),
                                     command_id))
                self.connection.commit()
            return self.get(command_id)

        if status == "SENT":
            # ★ sent_message_id 要一起写进去 —— 回调/去重都靠它
            with self.lock:
                row = self.connection.execute(
                    "SELECT payload FROM yumaiduo_commands WHERE command_id=?",
                    (command_id,)).fetchone()
                payload = json.loads(row[0])
                payload["status"] = "SENT"
                payload["sent_message_id"] = sent_message_id or ""
                if execution_token:
                    payload["execution_token"] = execution_token
                if provider_result_status:
                    payload["provider_result_status"] = provider_result_status
                payload["state_changed_at"] = _now().isoformat()
                self.connection.execute(
                    "UPDATE yumaiduo_commands SET status=?, payload=? "
                    "WHERE command_id=?", ("SENT",
                                           json.dumps(payload, ensure_ascii=False),
                                           command_id))
                self.connection.commit()
            return self.get(command_id)

        # ★ WOULD_SEND（影子模式）不是失败 —— 它是合法的演练结果。
        #   记成本该发，不带 failure_reason（否则看着像出错）。
        if status == "WOULD_SEND":
            self._set_status(command_id, "WOULD_SEND", "")
            return self.get(command_id)

        detail = failure_reason or (
            "EXECUTION_UNCONFIRMED" if status == "UNKNOWN" else "EXECUTION_FAILED")
        self._set_status(command_id, status, detail)
        return self.get(command_id)

    def close(self) -> None:
        self.connection.close()


def dedupe_for(event_id: str, *, kind: str, part: str = "") -> str:
    """
    派生幂等键。

    ★ 用 event_id 做底：一个事件触发的同一种命令只产生一次。
      ★ 加 part 是为了"一个事件要发两条消息"的场景（V5 的 reply_part_1/2）——
        两条消息属于同一个事件，但必须各自有独立的幂等键，
        否则第二条会被当成重复被挡掉。
    """
    base = f"{event_id}:{kind}"
    return f"{base}:{part}" if part else base


def defer(seconds: int, *, now: datetime | None = None) -> str:
    """算出 not_before 时间戳（ISO）。"""
    return ((now or _now()) + timedelta(seconds=seconds)).isoformat()
