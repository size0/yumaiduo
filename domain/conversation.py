"""
domain/conversation.py —— 会话状态机（票达人同款 · 按实渲染规格重做）

■ 依据：../票达人-文案清单.md（31 个气泡码，已校验）
■ 每个状态都标注了它对应哪个气泡码，一一可查。

════════════════════════════════════════════════════════════════
★ 三个来自票达人真实行为的设计要点

【1】识别失败【不改变会话状态】
    票达人识别失败后，买家重发截图就能继续 —— 会话没有"死掉"。
    所以 2/29/30/42/43/44/32 这些只是【回复不同】，状态不变。

【2】消歧拆成三个独立状态
    40 一个气泡码管影院/影片/场次三种歧义，
    但它们是【三个不同的等待状态】—— 因为买家要回的是不同的序号。

【3】"拍下"和"已改价"是两个状态，不是一个
    17（拍下别付款）和 18（改价完成请核对）文案完全不同，
    而且金额校验的时机也不同。挤成一个状态，就表达不了这两件事。
════════════════════════════════════════════════════════════════
"""

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

# 配置层：报价有效期、确认指令等都从 settings 读，不在这里硬编码
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from settings import DEFAULT as CONFIG   # noqa: E402


def now() -> datetime:
    """统一取当前时间。将来要测超时，注入这里就行。"""
    return datetime.now()


# ==================================================================== 状态
class ConvState(str, Enum):
    """12 个核心状态。每个都对应票达人实渲染里的气泡码。"""

    # ---- 识别与消歧 ----
    IDLE = "空闲"                    # 等买家发截图
    RECOGNIZING = "识别中"            # 正在识别（bubble 1）
    PICK_CINEMA = "待确认影院"         # 多个影院（bubble 40）
    PICK_MOVIE = "待确认影片"          # 多个影片（bubble 40）
    PICK_SHOW = "待确认场次"           # 多个场次（bubble 40）

    # ---- 报价 ----
    QUOTED = "已报价"                 # 报价卡已发（bubble 21 / 4）

    # ---- 交易 ----
    AWAIT_REPRICE = "已拍下待改价"      # 买家拍下，等改价（bubble 17）
    AWAIT_PAY = "改价完成待付款"        # 价格已锁定（bubble 18 / 22）
    AWAIT_TICKET = "待出票"            # 已付款，等上游（bubble 7）
    DELIVERED = "已交付"              # 票码已发（bubble 10 / 20 / 26）

    # ---- 异常 ----
    QT_FAIL = "金额校验失败"           # 实付与报价不符（bubble 23 / 19）
    REWORK = "需重新执行"             # 出票失败/建单失败，等买家重来（bubble 8 / 9 / 34）
    MANUAL = "待人工"                 # 最后手段

    # ---- 退票 ----
    # ★ 这两个状态是【补上来的】：良票有 order/refund + order.refunded +
    #   order.refund_rejected 三个接口/事件，而我们原先的状态机里一个落点都没有，
    #   买家申请退票时走不到任何分支。
    REFUNDING = "退票处理中"           # order/refund 已提交，等回调
    REFUNDED = "已退票"               # order.refunded

    # ---- 可自购 ----
    SELF_BUY = "建议自购"             # 报价高于自购价，引导买家自己买（bubble 3）


# 终态：到了这里不该再自动往前走
TERMINAL = {ConvState.DELIVERED, ConvState.MANUAL, ConvState.REFUNDED}


# ==================================================================== 事件
class Event(str, Enum):
    """推动会话的事件。分三类来源，绝不能混。"""

    # —— 买家动作（用户驱动）——
    BUYER_IMAGE = "buyer.image"              # 发选座截图
    BUYER_CONFIRM = "buyer.confirm"          # 回复确认指令（报价卡里的 {confirm_cmd}）
    CONFIRM_INVALID = "buyer.confirm.invalid"  # 确认指令不存在（bubble 12）
    BUYER_PICK = "buyer.pick"                # 回复候选序号（影院1 / 影片1 / 场次1）
    BUYER_ASK = "buyer.ask"                  # 问 999 / 获取票码 / 拦截订单
    BUYER_ORDERED = "buyer.ordered"          # 拍下（未付款）

    # —— 系统内部 ——
    RECOGNIZE_OK = "recog.ok"                # 识别成功
    RECOGNIZE_MULTI_CINEMA = "recog.multi.cinema"
    RECOGNIZE_MULTI_MOVIE = "recog.multi.movie"
    RECOGNIZE_MULTI_SHOW = "recog.multi.show"
    RECOGNIZE_FAIL = "recog.fail"            # ★ 带 reason，但不改状态
    QUOTE_HIGHER = "quote.higher"            # 报价高于自购价
    PICK_TIMEOUT = "pick.timeout"            # 消歧超时（bubble 41）
    QUOTE_EXPIRED = "quote.expired"          # ★ 报价超过有效时长（配置 30 分钟）

    # —— 外部事实（★ 绝不由模型判断）——
    ORDER_REPRICED = "order.repriced"        # 改价成功
    ORDER_REPRICE_FAILED = "order.reprice.failed"
    ORDER_PAID = "order.paid"                # 买家已付款 ← 出票唯一触发点
    AMOUNT_MISMATCH = "order.amount.mismatch"  # 实付与报价不符
    PAID_WITHOUT_QUOTE = "order.paid.noquote"  # 没报价就付款
    ORDERED_WITHOUT_QUOTE = "order.ordered.noquote"  # 没报价就拍下
    TICKET_OK = "ticket.ok"                  # 出票成功
    TICKET_FAIL = "ticket.fail"              # 出票失败 → 退款（bubble 9）
    CREATE_FAILED = "order.create.failed"    # 建单失败，位置价格不对（bubble 8）
    SUBSIDY_REDO = "order.subsidy.redo"      # 影院调价，卖家补贴重下（bubble 34）

    # —— 退票（★ 对应良票 5.7 申请退票 / 5.8 退票详情 + 两个回调）——
    REFUND_APPLIED = "order.refund.applied"      # 已提交退票申请
    REFUND_SUCCEEDED = "order.refund.succeeded"  # order.refunded 回调
    REFUND_REJECTED = "order.refund.rejected"    # order.refund_rejected 回调
    REPRICE_MANUAL = "order.reprice.manual"  # 改价失败转手动（bubble 28 / 31）
    QUOTE_REISSUED = "quote.reissued"        # 重新报价改价（bubble 22）


# ==================================================================== 流转表
# 读法：(当前状态, 事件) -> 新状态
TRANSITIONS: dict[tuple[ConvState, Event], ConvState] = {
    # ---- 识别 ----
    (ConvState.IDLE, Event.BUYER_IMAGE): ConvState.RECOGNIZING,
    (ConvState.DELIVERED, Event.BUYER_IMAGE): ConvState.RECOGNIZING,      # 买第二单
    (ConvState.REWORK, Event.BUYER_IMAGE): ConvState.RECOGNIZING,         # 失败后重来
    (ConvState.PICK_CINEMA, Event.BUYER_IMAGE): ConvState.RECOGNIZING,
    (ConvState.PICK_MOVIE, Event.BUYER_IMAGE): ConvState.RECOGNIZING,
    (ConvState.PICK_SHOW, Event.BUYER_IMAGE): ConvState.RECOGNIZING,
    (ConvState.RECOGNIZING, Event.RECOGNIZE_OK): ConvState.QUOTED,
    (ConvState.RECOGNIZING, Event.RECOGNIZE_MULTI_CINEMA): ConvState.PICK_CINEMA,
    (ConvState.RECOGNIZING, Event.RECOGNIZE_MULTI_MOVIE): ConvState.PICK_MOVIE,
    (ConvState.RECOGNIZING, Event.RECOGNIZE_MULTI_SHOW): ConvState.PICK_SHOW,
    # ★ 识别失败不改状态 —— 买家重发就能继续（bubble 2/29/30/42/43/44/32）
    (ConvState.RECOGNIZING, Event.RECOGNIZE_FAIL): ConvState.IDLE,

    # ---- 消歧 ----
    (ConvState.PICK_CINEMA, Event.BUYER_PICK): ConvState.QUOTED,
    (ConvState.PICK_MOVIE, Event.BUYER_PICK): ConvState.QUOTED,
    (ConvState.PICK_SHOW, Event.BUYER_PICK): ConvState.QUOTED,
    (ConvState.PICK_CINEMA, Event.PICK_TIMEOUT): ConvState.IDLE,
    (ConvState.PICK_MOVIE, Event.PICK_TIMEOUT): ConvState.IDLE,
    (ConvState.PICK_SHOW, Event.PICK_TIMEOUT): ConvState.IDLE,

    # ---- 报价 ----
    (ConvState.QUOTED, Event.QUOTE_HIGHER): ConvState.SELF_BUY,           # bubble 3
    # ★ 报价超时：配置的「报价记录有效时长」到了 → 回到空闲，引导重发
    (ConvState.QUOTED, Event.QUOTE_EXPIRED): ConvState.IDLE,
    (ConvState.QUOTED, Event.ORDERED_WITHOUT_QUOTE): ConvState.QUOTED,    # bubble 16，留在原状态
    (ConvState.QUOTED, Event.BUYER_IMAGE): ConvState.RECOGNIZING,         # 重发截图重报
    # ★ 主链路关键一步：买家回复确认指令才进入拍单流程
    (ConvState.QUOTED, Event.BUYER_CONFIRM): ConvState.AWAIT_REPRICE,     # → bubble 17
    (ConvState.QUOTED, Event.BUYER_ORDERED): ConvState.AWAIT_REPRICE,     # 直接拍下（同 17）
    # ★★★ 多轮消歧：买家选完一个候选后，会接着问下一个维度。
    #   为什么必须补这三条（真缺陷，不是补全癖）：
    #     _ask_candidate 会推 RECOGNIZE_MULTI_*，而它有两个来源状态：
    #       ① RECOGNIZING —— 识别刚回来（表里已有）
    #       ② QUOTED      —— ★ 买家选完上一轮候选之后（原来【缺这三条】）
    #     ⇒ 缺了就走非法兜底「未预料的事件：已报价 收到 recog.multi.movie，转人工」
    #     ⇒ 后果：三轮消歧（影院→影片→场次）在第二轮就转人工，
    #       多维度消歧根本走不完。
    (ConvState.QUOTED, Event.RECOGNIZE_MULTI_CINEMA): ConvState.PICK_CINEMA,
    (ConvState.QUOTED, Event.RECOGNIZE_MULTI_MOVIE): ConvState.PICK_MOVIE,
    (ConvState.QUOTED, Event.RECOGNIZE_MULTI_SHOW): ConvState.PICK_SHOW,
    # ★★★ 同理：买家答完【最后一个】待确认维度，二次确认就收敛成 EXACT，
    #   这时状态机拿到的是 RECOGNIZE_OK —— 而它原来【只从 RECOGNIZING 出发】。
    #   ⇒ 最后一轮必然走非法兜底转人工，消歧永远收不了尾。
    #   （RECOGNIZE_FAIL 一并补上：最后一轮确认失败也要能正常报错，
    #     而不是变成"转人工"。）
    (ConvState.QUOTED, Event.RECOGNIZE_OK): ConvState.QUOTED,
    (ConvState.QUOTED, Event.RECOGNIZE_FAIL): ConvState.IDLE,
    (ConvState.SELF_BUY, Event.BUYER_IMAGE): ConvState.RECOGNIZING,       # 下次再来比价

    # ---- 交易 ----
    (ConvState.AWAIT_REPRICE, Event.ORDER_REPRICED): ConvState.AWAIT_PAY,        # bubble 18
    (ConvState.AWAIT_REPRICE, Event.ORDER_REPRICE_FAILED): ConvState.AWAIT_REPRICE,  # bubble 28
    (ConvState.AWAIT_REPRICE, Event.REPRICE_MANUAL): ConvState.AWAIT_PAY,        # bubble 31
    (ConvState.AWAIT_REPRICE, Event.BUYER_IMAGE): ConvState.RECOGNIZING,
    # ★ 拍下后【直接付款】，不经过我们的改价。
    #   闲鱼上买家拍下就能付款，这不是异常流程，是很常见的一条路。
    #   所以要有这条流转；金额是否对得上由 AMOUNT_MISMATCH 那道门去查。
    (ConvState.AWAIT_REPRICE, Event.ORDER_PAID): ConvState.AWAIT_TICKET,
    (ConvState.AWAIT_PAY, Event.BUYER_IMAGE): ConvState.RECOGNIZING,             # 付款前重发
    (ConvState.AWAIT_PAY, Event.QUOTE_REISSUED): ConvState.AWAIT_PAY,            # bubble 22
    (ConvState.AWAIT_PAY, Event.ORDER_PAID): ConvState.AWAIT_TICKET,             # bubble 7

    # ---- 出票 ----
    (ConvState.AWAIT_TICKET, Event.TICKET_OK): ConvState.DELIVERED,             # bubble 10/20/26
    (ConvState.AWAIT_TICKET, Event.TICKET_FAIL): ConvState.REWORK,              # bubble 9 退款
    (ConvState.AWAIT_TICKET, Event.CREATE_FAILED): ConvState.REWORK,            # bubble 8
    (ConvState.AWAIT_TICKET, Event.SUBSIDY_REDO): ConvState.REWORK,             # bubble 34 补贴重下
    (ConvState.AWAIT_TICKET, Event.BUYER_IMAGE): ConvState.RECOGNIZING,

    # ---- 退票（★ 补上来的流程）----
    # 文档真实口径：TICKETED 后可能进入 REFUNDING；
    #     退成   → REFUNDED
    #     未退成 → 回到 TICKETED（凭证依然有效，冻结资金等散场实扣）
    (ConvState.DELIVERED, Event.REFUND_APPLIED): ConvState.REFUNDING,
    (ConvState.AWAIT_TICKET, Event.REFUND_APPLIED): ConvState.REFUNDING,
    (ConvState.REFUNDING, Event.REFUND_SUCCEEDED): ConvState.REFUNDED,
    # ★ 退票未成要回到【已交付】，不是"待出票" —— 票还在买家手里，凭证仍有效
    (ConvState.REFUNDING, Event.REFUND_REJECTED): ConvState.DELIVERED,
}


# ==================================================================== 特殊门
# 表里表达不了的判断。原则：**能自动处理就不转人工。**
GUARD_RULES: dict[tuple[ConvState, Event], dict] = {
    (ConvState.IDLE, Event.ORDERED_WITHOUT_QUOTE): {
        "target": ConvState.IDLE, "reply": "NO_QUOTE", "manual": False,
        "note": "没报价就拍下 → 提示无报价记录，请重新发图（bubble 16）",
    },
    (ConvState.IDLE, Event.PAID_WITHOUT_QUOTE): {
        "target": ConvState.QT_FAIL, "reply": "PAID_NO_QUOTE", "manual": False,
        "side_effect": "REFUND_AND_CLOSE",
        "note": "没报价就付款 → 自动退款并引导重来（bubble 19）",
    },
    (ConvState.AWAIT_REPRICE, Event.AMOUNT_MISMATCH): {
        "target": ConvState.QT_FAIL, "reply": "AMOUNT_MISMATCH", "manual": False,
        "side_effect": "REFUND_AND_CLOSE",
        "note": "实付与报价不符 → 校验失败，请重新拍下（bubble 23）",
    },
    (ConvState.AWAIT_PAY, Event.AMOUNT_MISMATCH): {
        "target": ConvState.QT_FAIL, "reply": "AMOUNT_MISMATCH", "manual": False,
        "side_effect": "REFUND_AND_CLOSE",
        "note": "实付与报价不符 → 校验失败（bubble 23）",
    },
    (ConvState.QUOTED, Event.CONFIRM_INVALID): {
        "target": ConvState.QUOTED, "reply": "CONFIRM_INVALID", "manual": False,
        "note": "确认指令不对 → 提示核对，或重发截图（bubble 12）",
    },
    # —— 人工接管期间一律不自动动作 ——
    (ConvState.MANUAL, Event.BUYER_IMAGE): {
        "target": ConvState.MANUAL, "reply": None, "manual": True,
        "note": "人工接管期间收到消息 → 只记录，不自动回复",
    },
    (ConvState.MANUAL, Event.ORDER_PAID): {
        "target": ConvState.MANUAL, "reply": None, "manual": True,
        "side_effect": None,
        "note": "人工接管期间收到付款 → 不自动出票，等店主决定",
    },
    (ConvState.MANUAL, Event.BUYER_ASK): {
        "target": ConvState.MANUAL, "reply": None, "manual": True,
        "note": "人工接管期间买家提问 → 不自动回复",
    },
}


# ==================================================================== 幂等表
# 平台是 at-least-once，重复投递是常态而不是异常。
IDEMPOTENT: dict[tuple[ConvState, Event], str] = {
    (ConvState.AWAIT_TICKET, Event.ORDER_PAID): "已付款，出票进行中",
    (ConvState.DELIVERED, Event.ORDER_PAID): "已交付，忽略重复付款",
    (ConvState.DELIVERED, Event.TICKET_OK): "已交付，忽略重复出票",
    (ConvState.REWORK, Event.TICKET_FAIL): "已失败，忽略重复失败",
    (ConvState.QT_FAIL, Event.AMOUNT_MISMATCH): "已处理过金额不符",
}


# ==================================================================== 事件 → 模板
# ★ 模板 key 与 票达人-文案清单.md 的 bubble 码一一对应（见注释）
REPLY_OF: dict[tuple[ConvState, Event], str] = {
    (ConvState.IDLE, Event.BUYER_IMAGE): "RECOGNIZING_TIP",           # bubble 1
    (ConvState.RECOGNIZING, Event.RECOGNIZE_OK): "QUOTE_CARD",        # bubble 21
    (ConvState.RECOGNIZING, Event.RECOGNIZE_MULTI_CINEMA): "DISAMBIG",  # bubble 40
    (ConvState.RECOGNIZING, Event.RECOGNIZE_MULTI_MOVIE): "DISAMBIG",   # bubble 40
    (ConvState.RECOGNIZING, Event.RECOGNIZE_MULTI_SHOW): "DISAMBIG",    # bubble 40
    (ConvState.RECOGNIZING, Event.RECOGNIZE_FAIL): "RECOG_FAIL",      # bubble 2/29/30/42/43/44/32
    (ConvState.PICK_CINEMA, Event.BUYER_PICK): "QUOTE_CARD",
    (ConvState.PICK_MOVIE, Event.BUYER_PICK): "QUOTE_CARD",
    (ConvState.PICK_SHOW, Event.BUYER_PICK): "QUOTE_CARD",
    (ConvState.PICK_CINEMA, Event.PICK_TIMEOUT): "DISAMBIG_TIMEOUT",  # bubble 41
    (ConvState.PICK_MOVIE, Event.PICK_TIMEOUT): "DISAMBIG_TIMEOUT",
    (ConvState.PICK_SHOW, Event.PICK_TIMEOUT): "DISAMBIG_TIMEOUT",
    (ConvState.QUOTED, Event.QUOTE_HIGHER): "SELF_BUY_HINT",          # bubble 3
    (ConvState.QUOTED, Event.QUOTE_EXPIRED): "QUOTE_EXPIRED",         # bubble 16
    (ConvState.QUOTED, Event.ORDERED_WITHOUT_QUOTE): "NO_QUOTE",      # bubble 16
    (ConvState.QUOTED, Event.BUYER_ORDERED): "ORDERED_NO_PAY",        # bubble 17
    (ConvState.QUOTED, Event.BUYER_CONFIRM): "ORDERED_NO_PAY",        # bubble 17（确认指令=拍下）
    (ConvState.QUOTED, Event.BUYER_IMAGE): "QUOTE_CARD_MULTI",        # bubble 4（多张截图版）
    (ConvState.AWAIT_REPRICE, Event.ORDER_REPRICED): "REPRICED_CHECK",  # bubble 18
    (ConvState.AWAIT_REPRICE, Event.ORDER_REPRICE_FAILED): "REPRICE_MANUAL_HINT",  # bubble 28
    (ConvState.AWAIT_REPRICE, Event.REPRICE_MANUAL): "REPRICED_OK",   # bubble 31
    (ConvState.AWAIT_PAY, Event.QUOTE_REISSUED): "REPRICED_AGAIN",    # bubble 22
    (ConvState.AWAIT_TICKET, Event.ORDER_PAID): "TICKETING",          # bubble 7
    # ★ 拍下后直接付款也是发这套「已下单等待」，不能因为没走改价就不说话
    (ConvState.AWAIT_REPRICE, Event.ORDER_PAID): "TICKETING",         # bubble 7
    (ConvState.AWAIT_TICKET, Event.TICKET_OK): "TICKET_CODE",         # bubble 10
    (ConvState.AWAIT_TICKET, Event.TICKET_FAIL): "TICKET_FAILED_REFUND",  # bubble 9
    (ConvState.AWAIT_TICKET, Event.CREATE_FAILED): "CREATE_FAILED",   # bubble 8
    (ConvState.AWAIT_TICKET, Event.SUBSIDY_REDO): "SUBSIDY_REDO",     # bubble 34
    (ConvState.QT_FAIL, Event.AMOUNT_MISMATCH): "AMOUNT_MISMATCH",    # bubble 23
    (ConvState.QT_FAIL, Event.PAID_WITHOUT_QUOTE): "PAID_NO_QUOTE",   # bubble 19
    # ---- 退票 ----
    (ConvState.DELIVERED, Event.REFUND_APPLIED): "REFUND_APPLIED",    # 已受理退票
    (ConvState.REFUNDING, Event.REFUND_SUCCEEDED): "REFUND_DONE",     # 退票完成
    (ConvState.REFUNDING, Event.REFUND_REJECTED): "REFUND_REJECTED",  # 退票未成，票仍有效
}


# ==================================================================== 判定
class IllegalMove(Exception):
    def __init__(self, state: ConvState, event: Event):
        self.state, self.event = state, event
        super().__init__(f"状态 {state.value} 下不允许事件 {event.value}")


@dataclass
class Decision:
    """状态机对一次事件的裁决。上层照着这个结果去做事。"""
    state: ConvState
    reply_key: str | None = None      # 该发哪个模板（None = 不说话）
    needs_manual: bool = False
    side_effect: str | None = None    # 需要执行的动作
    note: str = ""
    bubbles: tuple = ()               # 本次会用到哪些气泡码（便于对照规格）


# 事件 → 需要执行的动作
SIDE_EFFECTS = {
    Event.ORDER_REPRICED: "REPRICE",              # 调改价接口
    Event.ORDER_PAID: "TICKET_OUT",               # ★ 花钱的动作，唯一入口
    Event.TICKET_FAIL: "REFUND",                  # 自动退款
    Event.QUOTE_REISSUED: "REPRICE_AGAIN",        # 重新改价
}


@dataclass
class Conversation:
    """一个买家的会话。真实系统里必须持久化 —— 因为中间隔着好几分钟。"""
    chat_id: str
    state: ConvState = ConvState.IDLE
    tenant_id: str = ""
    shop_id: str = ""
    buyer_id: str = ""
    history: list = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    # ★ 报价单的失效时刻。进入 QUOTED 时按配置算出来，之后靠它判断过期。
    quote_expires_at: datetime | None = None

    # ---- 编排层要用的上下文 ----
    # ★ 为什么放在这里而不是编排层自己存：
    #   "买家回复确认指令"这件事，必须能找回上一次的报价和识别结果。
    #   会话是要持久化的，这些上下文跟着会话一起存才天然一致。
    #   注意用 Any 类型避免 domain 层依赖 integrations 层（保持方向单一）。
    recognition_id: str | None = None
    recognition: Any = None          # RecognitionResult，仅存放不解释
    quote: Any = None                # QuoteResult
    # ★★ 报价记录的 id（quotes 表里那一行的主键）。
    #   ★ 为什么必须有：conv.quote 是内存对象，重启就没了，
    #     而"下单要用哪笔报价"必须有个能持久化的依据。
    #     线上 V5 的订单表里 quote_id 是 UNIQUE NOT NULL ——
    #     一个订单必须绑一笔报价，绑的就是这个 id。
    #   ★ 也给"防拿旧价下单"用：下单前按它去报价记录里核
    #     这笔是否还是 READY（可能已被作废或过期）。
    quote_id: str | None = None
    quote_params: Any = None
    # ★★ 这单报价来自哪条渠道（WANDA / LIANGPIAO）——
    #    下单时靠它分流，避免万达的单走到良票下单。
    quote_channel: str = ""         # QuoteParams —— 询价与下单必须同一份
    quote_comparison: dict = field(default_factory=dict)  # Observation only; never a transaction quote.
    seats: list = field(default_factory=list)
    ticket_mode: str = ""
    city_name: str | None = None
    # ★ 买家从哪个渠道来的（"wanda" / "liangpiao" / None）。
    #   决定回话时用哪套文案（templates.text_for 按它取覆盖）。
    #   ★ 为什么放会话上而不是当参数传：
    #     同一个买家的每一轮都必须用同一个渠道的文案 ——
    #     传参的话中间漏一次就串了，而串了很难发现（文案长得像）。
    channel: str | None = None
    last_candidates: dict = field(default_factory=dict)
    last_candidates_text: str = ""
    # ★ 最近一次「买家选了候选」的状态机备注（空 = 正常推进）。
    #   为什么要存：如果买家在非候选状态下回「影院1」，
    #   状态机会判非法并转人工 —— 那条备注得能带到上层，
    #   否则出问题时只看到"转人工"三个字，查不出为什么。
    last_pick_note: str = ""
    # ★★ 影院是否已被买家确认过（2026-10-14 加）。
    #   为什么要这个：良票的影院名会被模型【截断】（rawResults.cinemaTruncated），
    #   文档 §7.1 要求截断时必须走二次确认。
    #   而我们的流程里确认就是买家从候选里选一次 ——
    #   所以记下他选过了，别反复问同一件事。
    cinema_confirmed: bool = False
    # ★★ 是否【刚谈过价】（2026-10-14 加）。
    #   为什么要这个：买家说「改价」我们只能回一句"你先拍下，拍下后我改价"
    #   （闲鱼的改价接口是针对已有订单的，没订单改不了）。
    #   ★ 但"认不认这句话"要不要每次都问模型？不能 ——
    #     如果"只说话"的意图在所有状态下都有效，那每条落到兜底的消息
    #     都要多打一次模型（费钱、慢、模型调用次数这个指标也失真）。
    #   ⇒ 收窄成：只有【刚报过价 / 刚谈过价】的时候才认「改价」。
    #     那时候买家说改价才有意义；在"空闲"状态下说改价，
    #     他不知道价、我们也没报，那句话不构成改价请求。
    price_talk_open: bool = False
    pending_order: Any = None        # PendingOrder

    def _touch_quote(self) -> None:
        """刚报价/重新报价 —— 按配置重置报价有效期。"""
        ttl = CONFIG.recognize_quote.quote_ttl_minutes
        self.quote_expires_at = None if self.channel == "wanda" else now() + timedelta(minutes=ttl)
    def quote_expired(self) -> bool:
        """报价是否已过期。配置的有效时长说了算，不按文案里的"2分钟"。"""
        if self.channel == "wanda" or self.state is not ConvState.QUOTED or self.quote_expires_at is None:
            return False
        return now() >= self.quote_expires_at

    def seconds_left(self) -> float | None:
        """报价还剩多少秒。已过期返回 0，没报价返回 None。"""
        if self.channel == "wanda" or self.quote_expires_at is None:
            return None
        return max(0.0, (self.quote_expires_at - now()).total_seconds())

    def apply(self, event: Event, payload: dict | None = None) -> Decision:
        """收到一个事件，决定下一步。这是状态机唯一的入口。"""
        payload = payload or {}
        before = self.state

        # ---- 第 0 道：报价超时 ----
        # ★ 这一道必须在最前面：过期的报价单不该再被"确认"，
        #   否则买家会按一个失效的价格下单，钱对不上。
        if self.quote_expired() and event not in (Event.BUYER_IMAGE, Event.QUOTE_EXPIRED):
            self.state = ConvState.IDLE
            self.quote_expires_at = None
            self.history.append((datetime.now(), before, event, ConvState.IDLE, "报价超时"))
            return Decision(state=ConvState.IDLE, reply_key="QUOTE_EXPIRED",
                            note=f"报价超过 {CONFIG.recognize_quote.quote_ttl_minutes} 分钟已失效，引导重发")

        # ---- 第 1 道：幂等 ----
        if (before, event) in IDEMPOTENT:
            self.history.append((datetime.now(), before, event, before, "幂等忽略"))
            return Decision(state=before, note=f"幂等忽略：{before.value} 重复收到 {event.value}")

        # ---- 第 2 道：特殊门 ----
        guard = GUARD_RULES.get((before, event))
        if guard:
            self.state = guard["target"]
            if guard["target"] is ConvState.QUOTED:
                self._touch_quote()
            self.history.append((datetime.now(), before, event, guard["target"], guard["note"]))
            return Decision(state=guard["target"], reply_key=guard.get("reply"),
                            needs_manual=guard.get("manual", False),
                            side_effect=guard.get("side_effect"), note=guard["note"])

        # ---- 第 2 道：流转表 ----
        target = TRANSITIONS.get((before, event))
        if target is None:
            self.history.append((datetime.now(), before, event, None, "非法流转"))
            return Decision(state=ConvState.MANUAL, needs_manual=True,
                            note=f"未预料的事件：{before.value} 收到 {event.value}，转人工")

        self.state = target
        # ★ 进入/重新进入已报价 → 按配置重置报价有效期（默认 30 分钟）
        if target is ConvState.QUOTED:
            self._touch_quote()
        elif target is ConvState.IDLE:
            self.quote_expires_at = None
        self.history.append((datetime.now(), before, event, target, ""))
        return Decision(state=target,
                        reply_key=REPLY_OF.get((before, event)),
                        side_effect=SIDE_EFFECTS.get(event),
                        note=f"{before.value} --{event.value}--> {target.value}")

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def timeline(self) -> str:
        if not self.history:
            return "（还没发生任何事件）"
        lines = [f"  起始: {self.state.value}"]
        for _, before, event, after, note in self.history:
            arrow = f"{before.value} --{event.value}--> {after.value if after else '×'}"
            lines.append(f"  {arrow}" + (f"   ({note})" if note else ""))
        return "\n".join(lines)
