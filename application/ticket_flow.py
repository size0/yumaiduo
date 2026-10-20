"""
application/ticket_flow.py —— 编排层：把各层串成一条链路

════════════════════════════════════════════════════════════════
★ 这一层干什么

  前面几层各管一段，谁也不认识谁：
      LiangPiaoAPI   会和良票说话
      QuoteRuleEngine 会算价
      Conversation    记得住状态
      templates       会说话
      callbacks       听得懂回调

  但没有一层知道"整件事该怎么走"。这一层就是那张流程图。

  完整链路：
    买家发截图
      → recognize         拿到 matchLevel
      → EXACT?            不是就消歧 / 提示换图
      → 挑票速档位
      → preflight         拿成本
      → QuoteRuleEngine   算售价 + 决定 priceMode / maxPrice
      → 给买家报价
      → 买家确认 → create  下单（询价参数原样带过去）
      → 回调 ticketed      出票 → 发取票码
      → 回调 failed        退款 / 重来

★ 三条设计原则

【1】外部事实单向流入状态机
     良票给什么就是什么，不让规则引擎或文案去"猜"。
     识别失败、场次过期、出票失败 —— 都是事实，不是判断。

【2】询价与下单的参数必须来自同一个对象
     QuoteParams 在询价时确定，下单时原样复用。
     中间隔着给买家报价、等买家确认，很容易只改一处。

【3】每一步都能单独测
     全靠注入 transport。没有 AppKey 也能把整条链路走完。
════════════════════════════════════════════════════════════════
"""

import sys
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import templates as tpl                                   # noqa: E402
from domain.conversation import (Conversation, ConvState, Event,   # noqa: E402
                                 TRANSITIONS, now as conv_now)
from domain.quote_rules import CHANNEL_LIANGPIAO, CHANNEL_WANDA, QuoteRuleEngine, QuoteResult  # noqa: E402

# ★ 订单模型放模块级：_persist_wanda_order 和 _persist_order 都要用它。
#   ★ 踩过：原先只在 _persist_order 里局部 import，
#     我在新写的 _persist_wanda_order 里用 Order(...) 就 NameError，
#     而且被那个 except Exception 吞掉了 —— 表现为"单没落库"，
#     买家还收到了"已下单"，极难排查。
import hashlib                                             # noqa: E402
import re                                                  # noqa: E402
from datetime import datetime, timezone                    # noqa: E402

from domain.order import (Order, idempotency_key_for,        # noqa: E402
                          new_order_id)
from integrations.liangpiao import callbacks as cb        # noqa: E402
from integrations.liangpiao.api import (MATCH_EXACT, PRICE_LIMIT, # noqa: E402
                                        QuoteParams, RecognitionResult,
                                        TICKET_FAST, TICKET_FLASH,
                                        TICKET_STANDARD, LiangPiaoAPI,
                                        make_out_order_no)
from integrations.liangpiao.client import LiangPiaoError    # noqa: E402

# ★ 万达渠道：成本与场次（都只读；渠道判定按影院 ID 映射）
from application.wanda_cost import WandaCostError, resolve_cost  # noqa: E402
from application.wanda_showtime import (                     # noqa: E402
    WandaShowtimeError, fetch_shows, normalize_date, normalize_minutes,
    pick_show)
from integrations.wanda.bridge import WandaBridgeError       # noqa: E402
# ★ 2026-10-14：模块级 import ——
#   架构审计只认模块级；函数内动态 import 会被判成孤儿模块（确实没人引用）。
#   顺带也更快：每次调用都 import 一次是白开销。
#   ★ 注意：插入点必须是【括号配平】的 import 块末尾 ——
#     我第一版按“不以 import 开头的行”找插入点，结果插进了
#     `from x import (A,` 的续行中间，直接把文件弄出语法错误。
from application import ticket_image                                  # noqa: E402

# ★★★ 2026-10-14：「出票方式」的两个取值（设置 → 出票及放单 → 出票方式）
#   ★ 为什么用具名常量而不是在代码里写 "手动" 字面量：
#     中文枚举值一旦在几处各写一遍，改一个字就会出现"某处匹配不上"
#     （本项目刚踩过：SLOW vs STANDARD 那个档位名对不上，
#       导致任何拿它比对的代码都会漏掉那一档）。
RELEASE_AUTO = "自动"        # 我们调 ch5_create 自动建单（原有行为）
RELEASE_MANUAL = "手动"      # 不自动建单，提示店家去后台自己出票


def _seat_names_of(seats) -> list:
    """
    从座位列表里取座位名，**兼容两种形态**。

    ★★ 为什么必须兼容（我踩过）：
      良票那边 conv.seats 是【识别结果的 dict 列表】——
          [{"seatNo": "6排9座", "rowNo": 6, ...}]
      而万达那边我在报价时已经把它提成了【字符串列表】——
          ["6排9座"]
      我在 on_buyer_confirm 里只按 dict 提（s.get("seatNo")），
      结果万达的单 seats=[] —— 订单落库了、金额对了，但**座位是空的**。
      而买家看到的文案一切正常，极难发现。
    ⇒ 统一走这个函数，两种形态都认。
    """
    out = []
    for s in (seats or []):
        if isinstance(s, dict):
            name = str(s.get("seatNo") or s.get("name") or "").strip()
        else:
            name = str(s or "").strip()
        if name:
            out.append(name)
    return out


def _iso_to_yyyymmdd(value: str | None) -> str:
    """把识别结果的 start_time（ISO 带时区）转成 'YYYYMMDD'。"""
    s = str(value or "").strip()
    if not s:
        return ""
    return normalize_date(s[:10].replace("/", "-"))


def _hall_mismatch(wanted: str, got: str) -> bool:
    """
    两个厅名是不是【真的不一样】（而不是命名习惯的差别）。

    ★ 为什么要单独一个函数、而不是直接 != 或 in：
      两边命名习惯不同，实测见过这些组合：
          识别「7号厅」    万达「7号厅」                    → 一样
          识别「8号激光厅」 万达「8号激光厅」                 → 一样
          识别「7号厅」    万达「7号激光厅」                 → ★ 只是习惯不同
          识别「7号厅」    万达「2号厅」                     → ★ 真的不一样
          识别「8号激光厅」 万达「6号-4DX激光厅（儿童需购票）」 → ★ 真的不一样

      第一版我写成 `wanted not in got and got not in wanted`，
      「7号厅 vs 7号激光厅」被误判成"对不上"（多两个字就断了包含关系）。
      ⇒ 改成【先比厅号；厅号相同就不算不一样】。

    ★ 缺任一边就返回 False（不判）——
      没有依据时不要指认"影院识别错了"。
    """
    a, b = str(wanted or "").strip(), str(got or "").strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return False
    num_a = re.findall(r"\d+", a)
    num_b = re.findall(r"\d+", b)
    if num_a and num_a == num_b:
        return False        # 厅号一样（可能一个有「激光」一个没有）
    return True


def _verified_hall_alias(result, show, shows) -> bool:
    """Accept a generic format hall only after unique exact supplier show evidence."""
    from datetime import datetime, timezone, timedelta
    from .cinema_matching import title_key
    wanted,got=str(result.hall_name or '').upper(),str(show.hall_name or '').upper()
    formats=lambda text:{word for word in ('IMAX','4DX','CINITY','杜比') if word in text}
    if not formats(wanted) or formats(wanted)!=formats(got):
        return False
    if ('IMAXGT' in wanted)!=('IMAXGT' in got):
        return False
    # Require an actually generic format label. Chinese/full-width hall numbers
    # and other identifying words must never disappear as if they were absent.
    generic=lambda text:not re.sub(r'IMAX|4DX|CINITY|杜比|激光|巨幕|影厅|影|厅|\s','',text)
    if not generic(wanted) and not generic(got):
        return False
    try:
        china=timezone(timedelta(hours=8))
        expected=datetime.fromisoformat(result.start_time)
        expected=expected.replace(tzinfo=china) if expected.tzinfo is None else expected.astimezone(china)
        if (not show.realtime_ms or title_key(show.film_name)!=title_key(result.movie_name)
                or datetime.fromtimestamp(show.realtime_ms/1000,china).replace(second=0,microsecond=0)!=
                expected.replace(second=0,microsecond=0)):
            return False
        matches={str(row.showtime_id) for row in shows if row.realtime_ms and
                 title_key(row.film_name)==title_key(result.movie_name) and
                 datetime.fromtimestamp(row.realtime_ms/1000,china).replace(second=0,microsecond=0)==
                 expected.replace(second=0,microsecond=0)}
        return bool(matches=={str(show.showtime_id)})
    except (ValueError,TypeError,OverflowError):
        return False


def _wanda_showtime_hint(exc: "WandaShowtimeError") -> str:
    """
    万达场次解析失败 → 给买家的说法。

    ★ 按 code 分流，不要一句话盖住所有情况：
      有的要买家补信息（多场不知哪一场），有的是真没排片，
      有的要转人工（数据歧义）。
    """
    code = getattr(exc, "code", "")
    if code == "SHOW_AMBIGUOUS":
        return ("这家这场同时有好几个厅在放 —— 麻烦确认一下是哪个厅/几点，"
                "我好报准价")
    if code == "NO_SHOWS_FOR_DATE":
        return "这家那天没有排片，麻烦确认下日期或换一家"
    if code == "SHOW_NOT_FOUND":
        return "没找到完全对得上的场次，麻烦把场次信息（几点/哪个厅）发我确认"
    if code == "SHOWTIME_SHAPE_UNRECOGNIZED":
        # ★ 这是【我们的】解析问题，不是买家的问题 —— 必须转人工，别让买家反复试
        return "系统这边读场次出了点问题，已经记下来了，稍后人工帮您处理"
    return "这个场次暂时查不到，麻烦稍后再试或发我更清楚的截图"


def _wanda_cost_hint(exc: "WandaCostError") -> str:
    """万达成本解析失败 → 给买家的说法。"""
    code = getattr(exc, "code", "")
    if code == "NO_MEMBER_SEATS":
        return "这个场次没有可用的会员座位了，要不要换一场？"
    if code == 'MEMBER_AREA_PRICE_AMBIGUOUS':
        return '当前区域价格不一致，请补充具体几排几座，我按该位置核价，不会改选便宜区域'
    if code == "NO_MEMBER_PRICE":
        return "这个座位的会员价暂时查不到，稍后人工帮您确认"
    if code == "SEAT_UNAVAILABLE":
        detail = getattr(exc, "detail", None) or {}
        if detail.get("seats"):
            return "您指定的" + "、".join(detail["seats"]) + "当前不可售，请另选位置后重新核价"
        want = detail.get("want")
        avail = detail.get("available")
        if want and avail is not None:
            return f"要 {want} 张，但会员座位只剩 {avail} 个 —— 少买几张或换一场？"
        return "会员座位不够了，少买几张或换一场？"
    if code == "SEAT_NOT_IN_MAP":
        return "您圈的座位在座位图上没找到，麻烦确认一下座位号"
    if code in ('VIP_SEATS_REQUIRED','VIP_PRICE_FACTS_INVALID'):
        return '该影厅需要确认具体座位及该位置的官方原价，请补充座位后核价'
    if code == 'HALL_RULE_REQUIRED':
        return '该影厅没有可用的专用报价规则，需要店家核价'
    if code in ("CHANNEL_UNKNOWN", "SHOWTIME_LIST_UNAVAILABLE"):
        return "系统这边查价出了点问题，已经记下来了，稍后人工帮您处理"
    return "这个场次暂时查不到价，麻烦稍后再试或换一场"


# ==================================================================== 结果
@dataclass
class FlowResult:
    """
    一次编排的结论。上层（客服/机器人）照这个决定发什么消息。

    ★ messages 已经是【渲染好的多条消息】，不是模板 key ——
      编排层的终点就是"给买家发什么"，不该把渲染留给更外层。

    ★ channel 决定"按哪个渠道的文案渲染"（见 templates.text_for）。
      不设就是全局。★ 为什么不放参数而放字段：
      同一轮里 add() 会被调好几次，每次都传一遍 channel 太容易漏。
    """
    ok: bool = True
    event: Event | None = None            # 喂给状态机的事件
    state: ConvState = ConvState.IDLE     # 处理完的状态
    messages: list = field(default_factory=list)     # 要发给买家的消息
    template_keys: list = field(default_factory=list)  # 用了哪些模板（便于排查）
    quote: QuoteResult | None = None      # 报价结论（含 priceMode / maxPrice）
    order_no: str = ""
    out_order_no: str = ""
    need_human: bool = False
    error: str = ""
    notes: list = field(default_factory=list)
    channel: str | None = None            # "wanda" / "liangpiao" / None(=全局)
    # ★ 这个报价是【预估/上限】还是确定价。
    #   true 时上层不能把它当成交价承诺（见 _build_quote 里的长注释）。
    price_is_estimate: bool = False
    # ★★ 2026-10-14 新增：编排层产生的、要排队发给插件的命令。
    #   为什么要有它（分层原因）：
    #     ticket_flow 是【领域层】，不该 import plugin_api ——
    #     plugin_api 自己依赖 plugin_runtime，而 plugin_runtime 依赖
    #     service → ticket_flow，import 就成环（架构审计的"循环依赖"就是这么来的）。
    #   ⇒ 领域层只负责【造命令】，排队是外层（plugin_runtime）的事。
    pending_commands: list = field(default_factory=list)

    def add(self, key: str, values: dict | None = None) -> None:
        """加一条要发的消息。★ 按 self.channel 取文案。"""
        self.template_keys.append(key)
        self.messages.extend(tpl.render_messages(key, values, channel=self.channel))


@dataclass
class PendingOrder:
    """
    已下单、等回调的订单。

    ★ 为什么要单独存下来：
      订单类回调【不含】影院/影片/厅名/开场时间（文档明确）。
      要给买家发取票消息，就必须留着下单时的信息，或者回查订单详情。
      这里两者都做：先用自己的存档，缺了再回查。
    """
    order_no: str
    out_order_no: str
    chat_id: str
    # ★ 回调要能把状态机往前推，就必须拿到会话对象。
    #   否则回调只能"发消息"，改不了状态 —— 退票未成后就回不到「已交付」。
    conversation: Any = None
    show_id: str = ""
    cinema_name: str = ""
    movie_name: str = ""
    hall_name: str = ""
    start_time: str = ""
    seats: list = field(default_factory=list)
    ticket_mode: str = TICKET_STANDARD
    price_mode: str = PRICE_LIMIT
    quote_amount_fen: int = 0
    freeze_amount_fen: int = 0
    # 幂等用：重试复用，被拒后换新
    attempt: int = 0


# ==================================================================== 编排
from .shop_scope import by_conversation


class TicketFlow:
    """
    一条链路的编排器。

    ★ 它【不持有】Conversation，由调用方传进来 ——
      因为会话是要持久化的，不在这个对象里存。
    """

    def __init__(self, api: LiangPiaoAPI, rules: QuoteRuleEngine, *,
                 tel: str = "", prefix: str = "YMD",
                 notify_url: str | None = None,
                 quote_store=None, revision: str = "",
                 order_store=None, wanda_bridge=None, wanda_lease_book=None, rules_provider=None):
        """
        ★ tel 不再强制要求 —— 因为【报价也不需要它】。

          良票文档：tel 是【下单】的必填项。
          而识别、询价、算价、答话都不需要它。

          早先在这里 raise，导致没配 tel 时整个服务装不起来：
          连「在吗」都回不了。**门槛放错了地方** ——
          该拦的那一步（下单）没拦，不该拦的一步（装配）拦死了。

          ⇒ 现在缺 tel 也照样构造，走到下单那一步才拦，并且拦得清楚。
        """
        self.api = api
        self.rules = rules
        self.rules_provider = rules_provider
        self.tel = tel or ""
        # ★ 万达渠道的桥（只读）+ 本地租约台账。
        #   None = 没配出票系统桥 → 这单走良票（渠道判定会自然落到良票）。
        #   ★ 做成可注入是为了测试能塞假 transport，不打真网络。
        self.wanda_bridge = wanda_bridge
        self.wanda_lease_book = wanda_lease_book
        self.prefix = prefix
        self.notify_url = notify_url
        # ★★ 报价记录存储（可选）。
        #   给了就在每次出报价卡时落一笔 QuoteRecord ——
        #   这样"这价怎么算的、用的哪版规则、什么时候过期"都追得回来。
        #   ★ 默认 None：老调用方（chat.py / 各测试）不用改，
        #     行为与之前完全一致（不落库）。
        self.quote_store = quote_store
        # ★ 规则版本号 —— 写进 QuoteResult，再进报价记录。
        #   由组装层（build_service）从设置层取，TicketFlow 不自己造。
        self.revision = revision
        # ★★ 订单库（可选）。下单成功时在这里落一笔 Order ——
        #   这是订单管理页数据的唯一来源。
        #   ★ 为什么必须落库：下单前只有 conv.pending_order（内存），
        #     重启或换会话就查不到了，订单页永远是空的。
        self.order_store = order_store
        # 落库失败时把原因留在这里（不抛异常，但也不静默）
        self._order_error = ""
        # 已下单等回调的。★ 真实系统里要持久化，这里先用内存。
        self.pending: dict[str, PendingOrder] = {}
        self._seq = 0

    # ================================================================ 1 识别
    @by_conversation
    def on_screenshot(self, conv: Conversation, image_url: str,
                      *, city_name: str | None = None,
                      channel: str | None = None, ticket_count: int | None = None,
                      buyer_request: dict | None = None) -> FlowResult:
        """Render the recognition notice after this image's reply channel is known.

        Messages are queued by the caller after this method returns, so deferring
        rendering preserves delivery order without using the previous image's channel.
        """
        out = self._recognize_screenshot(conv, image_url, city_name=city_name,
                                         channel=channel, ticket_count=ticket_count,
                                         buyer_request=buyer_request)
        if "RECOGNIZING_TIP" in out.template_keys:
            # An unresolved channel must not resurrect a notice explicitly
            # cleared for one of its possible routes. Wait for route authority.
            overrides = tpl.find('RECOGNIZING_TIP').get('channel_text') or {}
            unresolved_disabled = (not out.channel and any(
                isinstance(value, str) and not value.strip() for value in overrides.values()))
            if not unresolved_disabled:
                out.messages[:0] = tpl.render_messages("RECOGNIZING_TIP", channel=out.channel)
        return out

    def _recognize_screenshot(self, conv: Conversation, image_url: str,
                             *, city_name: str | None = None,
                             channel: str | None = None,
                             ticket_count: int | None = None,
                             buyer_request: dict | None = None) -> FlowResult:
        """Recognize facts and resolve this turn's channel before returning messages."""
        conv.buyer_ticket_count = ticket_count
        if channel and not conv.channel:
            conv.channel = channel
        out = FlowResult(channel=conv.channel)
        decision = conv.apply(Event.BUYER_IMAGE)
        out.state = decision.state
        if decision.needs_manual:
            out.need_human = True
            return out
        # New image facts cannot inherit a prior image's transaction quote.
        conv.quote = None
        conv.quote_id = ""
        conv.quote_kind = ""
        conv.recognition = None
        conv.supplement_recognition = {}
        conv.quote_comparison = {}
        conv.seats = []
        # ★ 新截图 = 新一次识别 —— 上一次的"影院已确认"不能沿用。
        #   （否则第二张图影院名被截断时，我们会以为买家已经确认过，
        #     直接报价 —— 那正是这道闸门要防的事。）
        conv.cinema_confirmed = False
        # Mark the notice here; on_screenshot renders it using the final channel.
        out.template_keys.append("RECOGNIZING_TIP")

        # ★★ 2026-10-14 接线：「设置 → 夜间识别 → 提醒时段 / 补充提醒话术」
        #   ★ 字段的 label 写的是「补充提醒话术」⇒ 追加，不是替换：
        #     深夜买家【照样能下单】，我们只是提醒他这个时段有风险
        #     （特惠渠道可能维护、可能出票失败）。
        #   ★ 放在识别【之前】：这条和识别结果无关（不是"识别失败"才说），
        #     而且识别要 3.7~5 秒 —— 提醒早点发，买家不用等完才知道。
        #   ★ 默认时段 23:30~08:00，跨午夜 ⇒ 用共用的 time_window.in_range
        #     （抽出来就是因为这份判定原来不支持跨午夜）。
        try:
            from application.time_window import in_range as _in_range
            _nr = _night_cfg()
            if _nr is not None and _in_range(_nr[0], _nr[1]):
                _msg = str(_nr[2] or "").strip()
                if _msg:
                    out.messages.append(_msg)
                    out.notes.append(f"夜间识别：当前 {_nr[0]}~{_nr[1]}，已追加提醒")
        except Exception as _exc:                             # noqa: BLE001
            # ★ 提醒发不出去【不该影响识别】—— 那才是买家要的
            out.notes.append(f"★ 夜间提醒没发出去（不影响识别）：{_exc}")

        try:
            result = self.api.ch7_recognize(image_url, city_name)
        except LiangPiaoError as exc:
            # 识别失败【不改变会话状态】—— 买家重发就能继续
            out.ok = False
            out.error = str(exc)
            out.event = Event.RECOGNIZE_FAIL
            decision = conv.apply(Event.RECOGNIZE_FAIL)
            out.state = decision.state
            out.add("RECOG_FAIL", {"fail_reason": _recog_fail_reason(getattr(exc, "code", 0))})
            return out

        if buyer_request:
            from .image_request_resolution import resolve, ResolutionPending
            try:
                result,verified_cinema_id=resolve(self.api,conv,result,buyer_request)
            except ResolutionPending as exc:
                from dataclasses import asdict
                conv.supplement_recognition = asdict(result)
                conv.quote = None
                conv.quote_id = ''
                conv.quote_kind = ''
                ident = {k: getattr(conv, k, '') for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')}
                if self.quote_store is not None and all(ident.values()):
                    self.quote_store.supersede_for_identity(ident,demand_id=getattr(conv,'demand_id','') or None)
                out.ok = False
                out.event = Event.RECOGNIZE_FAIL
                out.state = conv.apply(Event.RECOGNIZE_FAIL).state
                out.template_keys = ['TEXT_IMAGE_PENDING']
                out.messages = [str(exc)]
                out.notes.append('文字与图片只读核验：' + exc.status)
                return out
            from .seat_request import reconcile
            blocked = reconcile(conv, result, buyer_request, self.quote_store, out,
                                verified_cinema_id=verified_cinema_id)
            if blocked is not None:
                return blocked
        return self._after_recognition(conv, result, out)

    def _after_recognition(self, conv: Conversation, result: RecognitionResult,
                           out: FlowResult) -> FlowResult:
        """识别回来了，按 matchLevel 分流。"""
        from .wanda_recovery import eligible, recover
        if eligible(result, self.wanda_bridge):
            try:
                result = recover(self.wanda_bridge, result)
                out.notes.append('万达只读二次匹配已核实影院、影片、场次和影厅')
            except Exception as exc:
                from .wanda_recovery import RecoveryError
                mapped = False
                city_code = (result.final_results or {}).get('cityCode')
                if city_code and result.cinema_id and self.wanda_bridge is not None:
                    try:
                        mapped = self.wanda_bridge.wanda_lookup(
                            city_id=str(city_code), cinema_id=str(result.cinema_id)).mapped is True
                    except Exception:
                        pass  # Unavailable lookup does not establish a route.
                if not mapped and '万达' not in str((result.raw_results or {}).get('cinema') or ''):
                    # A failed optional lookup does not prove Wanda routing.
                    # Keep the original provider recognition result and channel.
                    out.notes.append('只读映射查询未确认路线：' + type(exc).__name__)
                else:
                    return self._wanda_recovery_pending(conv, result, out, exc, RecoveryError)
        return self._after_resolved_recognition(conv, result, out)

    def _wanda_recovery_pending(self, conv, result, out, exc, recovery_error):
        out.ok = False
        out.event = Event.RECOGNIZE_FAIL
        out.state = conv.apply(Event.RECOGNIZE_FAIL).state
        out.messages.append(str(exc) if isinstance(exc, recovery_error) else
                            '已识别图片信息，但万达影院场次暂未核实成功，请稍后核对；不会选择异地候选。')
        out.template_keys.append('WANDA_MATCH_PENDING')
        out.channel = 'wanda'
        conv.quote = None
        conv.quote_id = ''
        conv.quote_kind = ''
        ident = {k: getattr(conv, k, '') for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')}
        if self.quote_store is not None and all(ident.values()):
            self.quote_store.supersede_for_identity(ident, demand_id=getattr(conv, 'demand_id', '') or None)
        out.notes.append('万达二次匹配未完成：' + type(exc).__name__)
        from dataclasses import asdict
        conv.supplement_recognition = asdict(result)
        return out

    def _after_resolved_recognition(self, conv, result, out):
        from dataclasses import asdict
        conv.supplement_recognition = asdict(result)
        # ★ 无论走哪条分支，都先把候选列表和识别句柄存到会话上。
        #   多结果分支要靠它 —— 买家回复"场次1"时得能定位到具体 ID。
        #   （这里踩过一次：原先只在 EXACT 分支里存，
        #     结果多结果分支买家回复候选时找不到列表，二次确认永远发不出去。）
        # For recognition failures/candidates the brand selects copy only;
        # successful quote routing below remains authoritative for the provider.
        if "万达" in str(result.brand_name or "") or "万达" in str(result.cinema_name or ""):
            out.channel = "wanda"
        supplied_count = getattr(conv, "buyer_ticket_count", None)
        if supplied_count is not None:
            if result.seats and len(result.seats) != supplied_count:
                out.ok = False
                out.event = Event.RECOGNIZE_FAIL
                out.state = conv.apply(Event.RECOGNIZE_FAIL).state
                out.add("RECOG_FAIL", {"fail_reason": "您补充的张数与截图座位数不一致，请确认要几张票。"})
                return out
            result.expected_seat_count = supplied_count
        self._normalize_quote_seats(conv, result)
        # ★★ 识别句柄在这里统一存（2026-10-14）。
        #   为什么提到这里：原来只有 _ask_candidate 里存，
        #   而 _ask_candidate 只在【没收敛】时被调。
        #   ⇒ EXACT 时 conv.recognition_id 一直是 None，
        #     真实联调里看到的就是"已经报价了，但 recognition_id=None"。
        #   对 EXACT 本身没错（不需要二次确认），但两件事会出问题：
        #     ① 多轮消歧每一轮都靠二次确认推进，句柄一旦丢就整条链断掉；
        #     ② 排查时看不出"这次识别到底有没有句柄"——None 分不清
        #        "平台没给" 还是 "我们没存"。
        conv.recognition_id = result.recognize_id
        conv.city_name = result.city_name

        # ---- ★★★ 识别到「座位价格异常」（同业 bubble 44 / 问题 1）----
        #
        #   判据是识别结果里的 `priceMismatch`（良票文档 2210 行原文：
        #     「priceMismatch=true 通常意味着漏识别了座位，建议人工复核」）。
        #   ★★★ 这个字段我们【从第一天就解析了，但编排层从来没读】——
        #     所以 bubble 44 的文案挂在模板里、却永远发不出去。
        #     店主 2026-10-14 明确指出：这张图的场景就是「识别座位价格异常」。
        #
        #   ★ 为什么放在最前面判：它是【识别层面】的问题 ——
        #     座位都没认全，后面的询价/下单都没有意义（价格会对不上）。
        #     同业把它单独归一个状态，也是这个道理。
        #   ★ seatConfirmRequired 也一起看：
        #     平台认为"这座位得让买家再确认一下"时它会是 true，
        #     seatConfirmReasons 给出原因。两者都表示"别直接往下走"。
        price_recheck_route = None
        if (getattr(result, 'price_mismatch', False) and result.match_level == MATCH_EXACT
                and result.seat_matched and not result.seat_confirm_required
                and result.seats and type(result.expected_seat_count) is int
                and len(result.seats) == result.expected_seat_count):
            try:
                price_recheck_route = (getattr(result, 'wanda_verified_route', None)
                                       or self._resolve_channel(result))
            except WandaBridgeError:
                pass  # Unknown mapping must not authorize another supplier.
        if getattr(result, 'price_mismatch', False) and price_recheck_route is not None:
            result.wanda_price_check_route = price_recheck_route
            out.notes.append('截图金额异常但座号及张数完整：仅走已映射万达实时余座及成本核验，截图金额不作原价或自购价依据')
        elif getattr(result, "price_mismatch", False) and not _price_mismatch_continue_enabled():
            out.ok = False
            out.event = Event.RECOGNIZE_FAIL
            decision = conv.apply(Event.RECOGNIZE_FAIL)
            out.state = decision.state
            out.add("RECOG_FAIL", {"_variant": "seat_price_abnormal_configured",
                                    "price_mismatch_message": _price_mismatch_message()})
            out.notes.append(
                "★★★ priceMismatch=true（良票：通常意味着漏识别了座位）——"
                "按同业 bubble 44 让买家重新选座截图，不进询价/下单"
                + (f"；seatConfirmReasons={getattr(result, 'seat_confirm_reasons', None)}"
                   if getattr(result, "seat_confirm_reasons", None) else ""))
            return out
        elif getattr(result, "price_mismatch", False):
            # "按识别成功处理" means continue only to the real supplier
            # preflight.  The screenshot total remains untrusted, and the
            # ordinary seat-count/channel guards still apply below.
            out.notes.append("票价不一致按配置继续：截图金额不作为售价，继续调用渠道接口核价")

        # ---- 场次已开场：重试和补 cityName 都没用，别浪费调用 ----
        if result.is_show_expired:
            out.ok = False
            out.event = Event.RECOGNIZE_FAIL
            decision = conv.apply(Event.RECOGNIZE_FAIL)
            out.state = decision.state
            # ★ 用 show_started 变体（= 同业 bubble 42 的措辞）：
            #   「亲，您选择的场次已开场，无法继续购票，请更换其他场次后重新发送截图~」
            #   比通用那条「失败的原因：【…】」更贴合场景 —— 买家知道要换场次。
            out.add("RECOG_FAIL", {"_variant": "show_started",
                                   "fail_reason": "您截图的场次已经开场了"})
            out.notes.append("SHOW_EXPIRED：平台只卖未开场的场次，"
                             "重试或补 cityName 都不会有可售场次")
            return out

        # ---- 猜不出场次 ----
        if result.match_level not in (MATCH_EXACT,) and not result.candidate_options():
            out.ok = False
            out.event = Event.RECOGNIZE_FAIL
            decision = conv.apply(Event.RECOGNIZE_FAIL)
            out.state = decision.state
            reason = "没能在排片里找到这个场次"
            if result.no_match_reason == "NOT_FOUND":
                reason = "这家影院这个时间点暂时没有可售场次"
            out.add("RECOG_FAIL", {"fail_reason": reason})
            return out

        # ---- 多结果：要买家挑 ----
        if not result.can_order_directly and not getattr(result, 'wanda_verified_route', None):
            return self._ask_candidate(conv, result, out)

        # ---- ★ 影院名被截断：只记录，【不】因此问买家 ----
        #
        #   我一度在这里加了一道闸门（截断就问买家确认影院），
        #   店主指出：「我们用万达的接口去匹配，不就相当于做二次确认了吗」
        #   ★ 实测核对后确认店主是对的：
        #     cinemaTruncated 说的是【模型 OCR 输出的影院名被截断】
        #     （第二张：rawResults.cinema = '万达影城（娄底五江广...'），
        #      而 finalResults.cinemaName 是【良票标准库里的完整名】
        #      = '万达影城（娄底五江广场XLAND店）'，和万达侧名字逐字一致）。
        #     截断前缀在那次识别里只匹配到 1 家候选 ⇒ 让买家确认是白问一轮。
        #
        #   ★ 真正该做的交叉验证放在【万达接口那一侧】：
        #     · 片名  ← pick_show 早就在比（万达 filmList[].filmName）
        #     · 厅名  ← 新增（见 _quote_via_wanda 里的厅名校验）
        #   那两处才是"用万达的数据核对识别结果"，而且不打扰买家。
        if result.cinema_truncated:
            out.notes.append(
                "★ 影院名被模型截断（cinemaTruncated=true）—— "
                "finalResults.cinemaName 是良票标准库名，仍按它走；"
                "真正的核对交给万达侧的片名/厅名比对")
        # ---- EXACT：可以往下走 ----
        out.event = Event.RECOGNIZE_OK
        decision = conv.apply(Event.RECOGNIZE_OK)
        out.state = decision.state
        out.notes.extend(result.warnings())
        return self._build_quote(conv, result, out)

    def _ask_candidate(self, conv: Conversation, result: RecognitionResult,
                       out: FlowResult, *, only: str | None = None) -> FlowResult:
        """
        多结果消歧。

        ★ 文档：**只对尚未唯一的维度发问**，
          cinemaId 已有值就不要再拿 candidates.cinemas 去问。

        only：强制只问这一个维度（None = 按 pending_dimensions 自己算）。
              ★ 用于"影院名被截断"那道闸门 —— 那时 matchLevel 已经是 EXACT、
                各维度都有值，pending_dimensions() 会返回空，
                但它仍然需要买家确认一下影院。
        """
        pending = result.pending_dimensions()
        # ★ only 优先：用于"影院名被截断"那道闸门 ——
        #   那时各维度都已有值（pending 为空），但要强制问一次影院。
        ask = only or (pending[0] if pending else "show")
        # 决定进哪个"待确认"状态
        event = {
            "cinema": Event.RECOGNIZE_MULTI_CINEMA,
            "movie": Event.RECOGNIZE_MULTI_MOVIE,
            "show": Event.RECOGNIZE_MULTI_SHOW,
        }.get(ask, Event.RECOGNIZE_MULTI_SHOW)

        decision = conv.apply(event)
        out.event = event
        out.state = decision.state
        # ★ recognizeId / city_name 已由 _after_recognition 统一存好，
        #   这里不再重复写 —— 两处写同一个值，将来改一处忘另一处就会不同步。

        kind = {"cinema": "影院", "movie": "影片", "show": "场次"}[ask]
        out.add("DISAMBIG", {
            "candidates": _format_candidates(result, ask),
            "pick_kind": kind,
            # ★ 分维度的文案（影院那句要逐字对齐线上固定话术）。
            #   不能写死「当前需要确认影院：」—— 问影片/场次时那样是错的。
            "_variant": ask,
        })
        out.notes.append(f"待确认维度：{ask}")
        return out

    # ================================================================ 2 选项
    @by_conversation
    def on_candidate_pick(self, conv: Conversation, raw: str) -> FlowResult:
        """
        买家回复"影院1 / 影片1 / 场次1"。

        ★ 走的是「二次确认」接口，**不重新调模型也不计费**。
        ★ 序号不对时【不报错】，直接重发候选（bubble 40 的行为）。
        """
        out = FlowResult(channel=getattr(conv, "channel", None))
        recognize_id = getattr(conv, "recognition_id", None)
        if not recognize_id:
            # 没有识别句柄 —— 相当于过期了，请重发
            decision = conv.apply(Event.RECOGNIZE_FAIL)
            out.state = decision.state
            out.add("RECOG_FAIL", {"fail_reason": "上一次识别已经过期"})
            return out

        # 解析"影院1"这样的回复
        # ★ 2026-10-14 接线：同上，把配置的识别关键字传下去
        try:
            import settings as _st
            _rq = _st.load().recognize_quote
            kind, index = _parse_pick(
                raw, keyword_cinema=_rq.keyword_cinema,
                keyword_movie=_rq.keyword_movie,
                keyword_show=_rq.keyword_show)
        except Exception:                                 # noqa: BLE001
            # 读不到配置就照旧（默认那套关键字），不因为配置坏了卡住
            kind, index = _parse_pick(raw)

        # ★ 判据用 conv.state（PICK_CINEMA / PICK_MOVIE / PICK_SHOW）——
        #   这就是"我们最后问了哪个维度"的权威来源，不用另存一个字段。
        asked = {
            ConvState.PICK_CINEMA: "cinema",
            ConvState.PICK_MOVIE: "movie",
            ConvState.PICK_SHOW: "show",
        }.get(conv.state)

        # ★★★ 店主要求：候选回复【只回数字即可】（"回复1-2-3-4就行，前面影院去掉"）。
        #
        #   为什么上下文感知放在这里、而不是让 _parse_pick 认识纯数字：
        #     service.py 的路由判据是 `kind is not None` ——
        #     如果 _parse_pick('1') 返回非 None，那么买家在【任何状态】下发"1"
        #     都会被当成候选选择，把普通聊天误判成选座。
        #     只有在 on_candidate_pick 里才知道"我们正在问哪个维度"，
        #     所以纯数字只有在这里才被解释成序号。
        if kind is None and raw.strip().isdigit() and asked is not None:
            kind = asked
            index = int(raw.strip())
        if kind is None:
            # 不是候选指令，不消耗也不报错，直接重发候选
            out.add("CONFIRM_INVALID")
            return out

        # 需要上一轮的候选列表来定位 ID
        options = getattr(conv, "last_candidates", {}) or {}
        key = {"cinema": "cinemas", "movie": "movies", "show": "shows"}[kind]

        # ★★★ 只认【我们正在问的那个维度】。
        #
        #   踩过的坑：买家只看到【影院】列表，回「影片1」也能被受理 ——
        #   因为 last_candidates 里三个维度的候选都有（识别时一次性下发的），
        #   于是拿一个【他从来没见过的影片列表】去二次确认了。
        #   后果：他选的东西和他看到的东西不是一回事，很容易选错。
        if asked is not None and kind != asked:
            # 回错维度：不报错、不消耗，重发【当前在问的那一份】候选
            out.add("DISAMBIG", {
                "candidates": getattr(conv, "last_candidates_text", ""),
                "pick_kind": {"cinema": "影院", "movie": "影片",
                              "show": "场次"}[asked],
            })
            out.notes.append(f"买家回了 {kind}，但当前在问 {asked} → 重发候选")
            return out

        items = options.get(key) or []
        if not 1 <= index <= len(items):
            # ★ 序号不对：不报错，重发同一个候选消息（票达人的行为）
            out.add("DISAMBIG", {
                "candidates": getattr(conv, "last_candidates_text", ""),
                "pick_kind": {"cinema": "影院", "movie": "影片", "show": "场次"}[kind],
            })
            return out

        chosen = items[index - 1]
        try:
            if kind == "cinema":
                result = self.api.ch7_confirm(recognize_id,
                                              cinema_id=chosen["cinemaId"],
                                              city_name=getattr(conv, "city_name", None))
            elif kind == "movie":
                result = self.api.ch7_confirm(recognize_id,
                                              movie_id=chosen["movieId"],
                                              city_name=getattr(conv, "city_name", None))
            else:
                result = self.api.ch7_confirm(recognize_id,
                                              show_id=chosen["showId"],
                                              city_name=getattr(conv, "city_name", None))
        except LiangPiaoError as exc:
            out.ok = False
            out.error = str(exc)
            # ★★ 700009「所选候选无效」—— 文档原文：
            #   「只接受本次识别返回过的候选，拿平台没给过的 ID 会得到 700009
            #     （响应里会列出可选值）」
            #   ⇒ 这时【重发候选表】比报错好：
            #     买家的图和他的选择都没错，只是候选对不上了。
            #     重发一次他就能接着挑，不用重走识别（也省一次识别费）。
            if getattr(exc, "code", 0) == 700009:
                options_text = getattr(conv, "last_candidates_text", "")
                if options_text:
                    out.ok = True
                    out.notes.append("700009：候选对不上 → 重发候选表（不重走识别）")
                    out.add("DISAMBIG", {
                        "candidates": options_text,
                        "pick_kind": {"cinema": "影院", "movie": "影片",
                                      "show": "场次"}[kind]})
                    return out
                # ★ 手里没有候选表（比如进程重启过）→ 只能让他重发。
                #   这时用 _recog_fail_reason 的统一文案，别自己另写一套。
                out.add("RECOG_FAIL", {
                    "fail_reason": _recog_fail_reason(700009)})
                return out
            out.add("RECOG_FAIL", {
                "fail_reason": _recog_fail_reason(getattr(exc, "code", 0))})
            return out

        # ★★★ 买家选了候选 —— 必须把这一轮【记进状态机】。
        #
        #   踩过的坑（真缺陷，不是风格问题）：
        #     on_candidate_pick 从来没有 conv.apply(BUYER_PICK)，
        #     于是 conv.state 一直停在 PICK_CINEMA/PICK_MOVIE/PICK_SHOW。
        #     接着 _after_recognition 会去推 RECOGNIZE_MULTI_*，
        #     而那条转移【只从 QUOTED 出发】——
        #     ⇒ 每次都走非法兜底：「未预料的事件：待确认影院 收到
        #       recog.multi.movie，转人工」
        #     ⇒ 后果①：每多问一轮就转一次人工，多轮消歧根本走不完
        #       后果②：非法兜底【不改状态】，会话一直卡在 PICK_* 上
        #
        #   ★ 三条 (PICK_*, BUYER_PICK) → QUOTED 的转移本来就在表里，
        #     说明原设计就是要推这个事件 —— 只是漏了调用。
        self._apply_pick_event(conv)

        return self._after_recognition(conv, result, out)

    def _apply_pick_event(self, conv: Conversation) -> None:
        """
        把「买家选定了一个候选」记进状态机（PICK_* → QUOTED）。

        ★ 为什么不用 apply() 返回的 reply_key 去发消息：
          apply() 会给 (PICK_*, BUYER_PICK) 返回 reply_key="QUOTE_CARD"，
          但这时【还没收敛到唯一场次】—— 报价卡发出去是错的
          （场次都没定，报什么价）。
          真正该发什么由紧接着的 _after_recognition 决定：
          收敛了就发报价卡，没收敛就问下一个维度。
        ⇒ 这里只取「状态推进」这个副作用，忽略它的 reply_key。
        """
        decision = conv.apply(Event.BUYER_PICK)
        # ★ 买家选了一次候选 = 影院已被确认（用于影院名被截断那道闸门，
        #   见 _after_recognition）。记录它，别反复问同一件事。
        conv.cinema_confirmed = True
        if decision.needs_manual:
            # 候选状态本身不在 PICK_* 里（比如会话已被人工接管）——
            # 不硬改状态，也不在这里发消息，交给上层按 needs_manual 处理。
            conv.last_pick_note = decision.note
        else:
            conv.last_pick_note = ""

    # ================================================================ 3 报价
    def _persist_quote(self, conv, result, quote, seats, ticket_mode) -> None:
        """
        把这次报价落成一条 QuoteRecord。

        ★ 字段全部来自【已经确定下来的东西】，不做额外计算：
            · 身份四元组从 conv 拿（报价隔离靠它）
            · 成本/售价从 quote 拿（★ 分开放，才能核对毛利）
            · 规则版本从 quote / self.revision 拿 ——
              没有它就无法复现旧报价
            · provider_identity 存【影院/影片/场次的 id】——
              ★ 线上报价记录里存的就是 id 而不是名字（名字会变，id 不会）

        ★ 有效期用 10 分钟：线上真实记录是 created 07:18:44 → expires 07:28:44。
        """
        from domain.quote import QuoteRecord, default_expiry, new_quote_id

        ident = {
            "tenant_id": getattr(conv, "tenant_id", "") or "default",
            "shop_id": getattr(conv, "shop_id", "") or "default",
            "buyer_id": getattr(conv, "buyer_id", "") or conv.chat_id,
            "chat_id": conv.chat_id,
        }
        seat_names = _seat_names_of(seats)
        start = getattr(result, "start_time", "") or ""
        final = getattr(result, "final_results", None) or {}
        record = QuoteRecord(
            quote_id=new_quote_id(), **ident,
            provider=getattr(conv, "quote_channel", "") or "",
            request_type="EXACT_SEATS" if seat_names else "WPLUS_AREA",
            cinema=result.cinema_name or "",
            movie=result.movie_name or "",
            show=f"{start[:10]} {start[11:16]}".strip(),
            selected_seats=seat_names,
            unit_cost_fen=quote.cost_unit_fen,
            unit_sell_price_fen=quote.unit_price_fen,
            ticket_count=(0 if getattr(conv, "quote_kind", "") == "UNIT_PRICE" else quote.seat_count),
            total_cost_fen=(0 if getattr(conv, "quote_kind", "") == "UNIT_PRICE" else quote.cost_total_fen),
            total_sell_price_fen=(0 if getattr(conv, "quote_kind", "") == "UNIT_PRICE" else quote.total_price_fen),
            pricing_rule_id=quote.pricing_rule_id or quote.rule_name,
            pricing_rule_revision=quote.pricing_rule_revision or self.revision,
            expires_at=None if getattr(conv, "quote_channel", "") == "WANDA" else default_expiry(),
            metadata={
                "quote_kind": getattr(conv, "quote_kind", "ORDER_TOTAL"),
                "pricing_rule_name": quote.rule_name,
                # Keep the verified display facts for the post-reprice check
                # after a service restart (the in-memory recognition is lost).
                "city_name": result.city_name or "",
                "hall_name": result.hall_name or "",
                "cinema_address": result.cinema_address or "",
                "price_mode": quote.price_mode,
                "max_price_fen": quote.max_price_fen,
                "ticket_mode": ticket_mode,
                "cost_basis": quote.cost_basis,
                "margin_total_fen": quote.margin_total_fen,
                "notes": list(quote.notes or []),
                # ★ 上游 id：报价记录要能追到"报的是哪家影院哪个场次"
                "provider_identity": {
                    "wanda_cinema_id": getattr(getattr(result, 'wanda_verified_route', None), 'wanda_cinema_id', ''),
                    "wanda_show_id": getattr(result, 'wanda_verified_show_id', ''),
                    "liangpiao_city_id": str(final.get("cityCode") or ""),
                    "liangpiao_cinema_id": str(result.cinema_id or ""),
                    "liangpiao_movie_id": str(result.movie_id or ""),
                    "liangpiao_show_id": str(result.show_id or ""),
                },
            })
        from .demand import bind_quote
        # Legacy unit tests may omit identity; production quotes require it.
        if all(getattr(conv, k, '') for k in ident):
            bind_quote(conv, record, self.quote_store)
        self.quote_store.create(record)
        # ★ 记在会话上，下单时要按它取（而不是按"最新一条报价"）
        conv.quote_id = record.quote_id


    def _pick_ticket_mode(self, start_time, enabled_tiers=None):
        """挑票速档位。★ 带 enabled_tiers，但对老签名的实现做兼容回退。

        ★ 为什么要回退：这个方法是鸭子类型调用的（测试里用假 API），
          给它加参数会让所有只实现两参的假对象报 TypeError。
          ⇒ 先带参数调；报 TypeError 就不带参数再调一次。
          ★ 只有 TypeError 才回退 —— 别的异常照旧抛出去，
            否则会把真 bug 吞掉。
        """
        if enabled_tiers:
            try:
                return self.api.pick_ticket_mode(
                    start_time, enabled_tiers=enabled_tiers)
            except TypeError:
                pass                      # 老签名的实现，退回两参调用
        return self.api.pick_ticket_mode(start_time)

    def _build_quote(self, conv: Conversation, result: RecognitionResult,
                     out: FlowResult) -> FlowResult:
        """
        EXACT 之后：挑档位 → 询价 → 规则算价 → 出报价卡。

        ★ 这是整条链路最关键的一步，因为 priceMode 和 maxPrice 在这里定下来，
          下单时必须原样复用。
        """
        conv.quote_comparison = {}
        # ---- 放映时间限制：距开场太近不接单 ----
        limit_minutes = 40
        try:
            import settings as settings_module
            limit_minutes = settings_module.load().showtime_limit.remaining_minutes
        except Exception:                                  # noqa: BLE001
            pass
        # ★★ 2026-10-14 接线：「设置 → 兜底报价 → 兜底报价开关」
        #   ★ 我把它理解成"没有规则命中时，要不要也照成本报一个价"。
        #     关掉 ⇒ 不报，让规则没覆盖的场景明确失败，
        #     而不是悄悄按成本卖（那样可能白干甚至亏）。
        #   ★ 默认是【开】⇒ 默认行为一个字不变。
        #   ★ 这个理解我会在报告里请店主确认 —— 它是三个设置里
        #     唯一"关掉会改变结果"的那个。
        if not _fallback_quote_enabled():
            out.ok = False
            out.notes.append(
                "兜底报价开关=关 ⇒ 没有规则命中时【不报价】"
                "（避免按成本卖出去；要报价请补一条规则，或打开这个开关）")
            out.add("RECOG_FAIL", {
                "_variant": "no_rule",
                "fail_reason": "这个场次暂时报不了价，稍等一下我再确认下~"})
            return out
        # 先按影院映射确定供应路线。万达不适用良票的开场间隔和票速限制。
        try:
            route = (getattr(result, 'wanda_verified_route', None)
                     or getattr(result, 'wanda_price_check_route', None) or self._resolve_channel(result))
        except WandaBridgeError as exc:
            out.ok = False
            out.notes.append(f"万达路线无法确认：{exc.code}")
            out.add("RECOG_FAIL", {"fail_reason": "这家万达影院的报价通道暂时未能确认，需店家核对；无需重新选座。"})
            return out
        conv.channel = out.channel = "wanda" if route is not None else "liangpiao"
        if route is not None:
            return self._quote_via_wanda(conv, route, result, result.seats or [], out)

        # ---- 挑票速档位 ----
        # ★★ 2026-10-14 接线：「出票及放单 → 票速设置」
        #   把"哪些档位启用"传下去；被停用的档位会自动退回仍启用的那个。
        #   ★ 读不到配置就不传 ⇒ 行为与接线前一致。
        try:
            import settings as _st
            _tiers = _st.load().speed_tiers()
        except Exception:                                 # noqa: BLE001
            _tiers = None
        ticket_mode = self._pick_ticket_mode(result.start_time, _tiers)
        # 40分钟是 STANDARD 慢票的安全门槛；场次在40分钟内时，
        # 仍允许已启用的 FAST/FLASH 档位继续询价。若票速配置停用了
        # 这些档位而回退到 STANDARD，才明确不报价。
        if not _far_enough(result.start_time, limit_minutes) and ticket_mode == TICKET_STANDARD:
            out.ok = False
            out.add("SHOWTIME_LIMIT", {"limit_minutes": str(limit_minutes)})
            return out

        # ---- 询价参数：只在这里生成一次 ----
        params = QuoteParams()

        seats = result.seats or []

        # ════════════════════════════════════════════════════════════
        # ★★ 渠道分流：成本从哪来
        #
        #   万达 → 座位图 area[].wPlusActivity.price（桥，只读）
        #          ★ 没选座位也能报：那代表买家要【会员座位】，
        #            取 W+专享区的会员价（见 application/wanda_cost.py）
        #
        #   良票 → preflight 选座询价
        #          ★ 这个接口的 seats 是【必填 1~6】
        #            （良票文档 §5.1 + 我们 _seat_payload 的校验），
        #            所以良票这条【没座位号询不到价】—— 是渠道限制，
        #            不是我们的设计选择。这时要明确告诉买家"需要圈一下座位"，
        #            而不是含糊地说"询不到价"。
        # ════════════════════════════════════════════════════════════
        return self._quote_via_liangpiao(conv, result, seats, ticket_mode,
                                        params, out)

    # ------------------------------------------------------------ 渠道判定
    def _resolve_channel(self, result: RecognitionResult):
        """
        这单走万达还是良票。返回 CinemaRoute；走良票返回 None。

        ★★ 按【影院 ID】判定，不按名字 ——
          良票「万达影城（宁德万达广场激光IMAX店）」
          万达「宁德万达广场店」，两套命名完全不同，
          我实测按名字归一化匹配只有 35.9% 命中率。
        ★ 映射库不可用要抛出来（不能当"没映射"处理，那会让万达单
          静默退化成良票单且没有告警）。
        """
        brand_is_wanda = "万达" in str(result.brand_name or "") or "万达" in str(result.cinema_name or "")
        if self.wanda_bridge is None or not (result.cinema_id and result.city_name):
            if brand_is_wanda:
                raise WandaBridgeError("WANDA_ROUTE_UNRESOLVED", "万达通道或影院身份不完整")
            return None
        # ★★ 品牌兜底：影院名里写着「万达」，就是万达的单。
        #   为什么必须留这一手（2026-10-14 加）：
        #     查城市码要调良票的城市表接口。那个接口一旦抖动/超时，
        #     _liangpiao_city_code 会【静默返回空】，
        #     我们就带着万达的识别结果一路降到【良票分支】——
        #     而良票 seats 必填 1~6，没座位号就报 seat_missing，
        #     买家收到「未识别到您选择的座位信息」，而他发的明明是万达的图。
        #   实测复现：用没有 ch4_city_list 的假 api 跑一张万达的图，
        #     _resolve_channel 返回 None → 走良票 → seat_missing。
        #   ★ brandName 是识别结果自带的，不依赖任何接口 —— 拿它作判据不会失败。
        try:
            city_code = self._liangpiao_city_code(result.city_name)
        except Exception:                                    # noqa: BLE001
            city_code = ""
        if not city_code:
            if brand_is_wanda:
                # ★ 不静默降级：这是一张万达的图，只是城市码查不到。
                #   报明确错误（上层会转人工/重试），绝不冒充"没映射"。
                raise WandaBridgeError(
                    "CITY_CODE_UNAVAILABLE",
                    f"识别到这是万达的影院（brandName={result.brand_name!r}），"
                    f"但查不到城市 {result.city_name!r} 的 cityCode —— "
                    f"不能降级到良票（那会把万达单报错价或直接拒绝）")
            return None
        try:
            route = self.wanda_bridge.wanda_lookup(
                city_id=city_code, cinema_id=str(result.cinema_id))
        except WandaBridgeError:
            # 查询失败不是“确定无映射”，不能悄悄切换供应商。
            raise
        # 已识别为万达但未映射时阻断，不能静默切到良票。
        # 非万达影院确认无映射后仍可使用良票。
        if not route.use_wanda and brand_is_wanda:
            raise WandaBridgeError("WANDA_MAPPING_REQUIRED", "万达影院尚未完成身份映射")
        return route if route.use_wanda else None

    def _liangpiao_city_code(self, city_name: str) -> str:
        """
        城市名 → 良票 cityCode。

        ★ 为什么需要这一步：识别结果里只有 cityName（'宁德'），
          而影院映射接口要的是良票的 cityCode（'350900'）。
        ★ 缓存：城市表基本不变，一个进程里查一次就够，
          不必每单都打一次良票。
        """
        cache = getattr(self, "_city_code_cache", None)
        if cache is None:
            cache = self._city_code_cache = {}
        key = str(city_name or "").strip()
        if not key:
            return ""
        cached = cache.get(key)
        if isinstance(cached, str) and cached.strip():
            return cached
        # Only successful lookups are reusable. Also discard negative entries
        # left by an earlier lookup so a transient failure can recover.
        cache.pop(key, None)
        code = ""
        try:
            rows = self.api.ch4_city_list(keyword=key)
        except Exception:                                    # noqa: BLE001
            rows = []
        for row in (rows or []):
            if not isinstance(row, dict):
                continue
            if str(row.get("name") or "").strip() == key:
                code = str(row.get("code") or "").strip()
                break
        if not code and rows:
            # 名字没完全相等就用第一条（关键字查询本来就是模糊匹配）
            first = rows[0] if isinstance(rows[0], dict) else {}
            code = str(first.get("code") or "").strip()
        if code:
            cache[key] = code
        return code

    # ------------------------------------------------------------ 万达报价
    def _quote_via_wanda(self, conv, route, result: RecognitionResult,
                         seats: list, out: FlowResult) -> FlowResult:
        """
        万达渠道报价：场次匹配 → 座位图取会员价 → 规则算售价。

        ★ 不建单、不锁座、不取消 —— 全是只读接口。
        ★ 没选座位也能报：那代表买家要【会员座位】，
          取 W+专享区的会员价（见 application/wanda_cost.py）。
        """
        bridge = self.wanda_bridge
        # 账号：显式传，防后端静默回退到别的号
        book = self.wanda_lease_book
        scope = f"quote:{result.recognize_id or result.show_id or 'x'}"
        try:
            if book is not None:
                lease = book.get(scope)
                if lease is None:
                    phone = bridge.pick_account()
                    book.acquire(scope, phone)
                else:
                    phone = lease.phone
            else:
                phone = bridge.pick_account()
        except WandaBridgeError as exc:
            out.ok = False
            out.add("RECOG_FAIL", {"fail_reason": f"万达账号不可用：{exc}"})
            return out

        city_code = '' if getattr(result, 'wanda_verified_route', None) else self._liangpiao_city_code(result.city_name or "")

        # ① 场次（★ 必须自己滤日期 —— 桥的 showDate 参数被上游忽略）
        day = _iso_to_yyyymmdd(result.start_time)
        try:
            shows = fetch_shows(bridge,
                                wanda_cinema_id=route.wanda_cinema_id,
                                date=day)
            show = pick_show(
                shows, date=day,
                minutes=normalize_minutes(str(result.start_time or "")[11:16]),
                film_name=result.movie_name or "")
            if getattr(result, 'wanda_verified_route', None):
                from .wanda_recovery import normalized
                from datetime import datetime, timezone, timedelta
                expected = datetime.fromisoformat(result.start_time)
                if expected.tzinfo is None:
                    expected = expected.replace(tzinfo=timezone(timedelta(hours=8)))
                actual = datetime.fromtimestamp(show.realtime_ms / 1000, timezone(timedelta(hours=8)))
                if (str(show.showtime_id) != str(result.wanda_verified_show_id)
                        or actual != expected
                        or normalized(show.film_name) != normalized(result.movie_name)
                        or normalized(show.hall_name) != normalized(result.hall_name)):
                    raise WandaShowtimeError('SHOW_NOT_FOUND', '已核实场次发生变化，停止本次报价')
        except WandaShowtimeError as exc:
            out.ok = False
            out.add("RECOG_FAIL", {"fail_reason": _wanda_showtime_hint(exc)})
            return out

        # ---- ★★★ 厅名校验：这才是"用万达接口做二次确认"里真正缺的一块 ----
        #
        #   背景（店主指出的点）：我们已经拿【片名】在万达的排片里定位场次了
        #   （pick_show 的 film_name），那本身就是一次交叉验证。
        #   ★ 但【厅名】从来没校验过 —— WandaShow 有 hall_name，没人用它。
        #
        #   为什么厅名是关键：
        #     识别结果里有 cinema_id（良票侧）+ hall_name + movie_name + start_time。
        #     如果影院识别错了，那片名在【错影院】的同一时刻/相近时刻排片里
        #     很可能还在（热门片每个厅都放），⇒ 光靠片名定位不出错。
        #     而厅名是更细的指纹：截图上是 7号厅，那这家影院这个时间的这场
        #     就该在 7号厅。
        #   ⇒ 片名对不上 → pick_show 已经会报 SHOW_NOT_FOUND（早就有）
        #     厅名对不上 → ★ 以前没人管，现在在这里拦
        #
        #   ★ 只提醒一次（买家确认过就走），且只加备注不拦死：
        #     厅名两边命名习惯可能不同（「7号厅」vs「7号激光厅」），
        #     硬拦会把正常单挡在门外。做法是明确说出来让人看见。
        wanted_hall = str(result.hall_name or "").strip()
        got_hall = str(getattr(show, "hall_name", "") or "").strip()
        if _hall_mismatch(wanted_hall, got_hall) and not _verified_hall_alias(result,show,shows):
            out.notes.append(
                f"★★ 厅名对不上：识别是「{wanted_hall}」，"
                f"万达这个场次是「{got_hall}」（场次={show.showtime_id} "
                f"{show.hall_name} {show.film_name}）—— 影院可能识别错了，"
                f"请人工核一下")
            # ★ 记进 notes 让人看得见；但如果买家还没确认过影院，
            #   就顺手让他确认一轮（这是真正值得问的那种"对不上"）。
            if not getattr(conv, "cinema_confirmed", False):
                return self._ask_candidate(conv, result, out, only="cinema")

        # ② 成本（★ 没选座位 → 会员座位区；选了 → 按座位所在区）
        seat_names = [str(s.get("seatNo") or s.get("name") or "").strip()
                      for s in (seats or []) if isinstance(s, dict)]
        seat_names = [s for s in seat_names if s]
        count = len(seat_names) or int(result.expected_seat_count or 1)
        pricing_engine = self.rules_provider(conv) if self.rules_provider else self.rules
        pricing_time = conv_now().time()
        pricing_cinema = result.cinema_name or route.wanda_cinema_name or ""
        hall_policy = any(r.hall_keywords or r.excluded_hall_keywords or r.fixed_cost_fen is not None for r in getattr(pricing_engine, 'rules', []))

        def fixed_cost_for_official_price(total):
            rule = pricing_engine.find_rule(now=pricing_time, channel=CHANNEL_WANDA,
                brand=result.brand_name or "", cinema=pricing_cinema, hall_name=show.hall_name or "",
                original_price_yuan=total / 100 if total else None)
            if rule is None:
                import unicodedata
                hall = unicodedata.normalize('NFKC', show.hall_name or '').casefold()
                if any(r.channel in ('', CHANNEL_WANDA)
                       and any(unicodedata.normalize('NFKC', k).casefold() in hall
                               for k in (r.hall_keywords + (r.excluded_hall_keywords if r.enabled else [])))
                       for r in getattr(pricing_engine, 'rules', [])):
                    raise WandaCostError('HALL_RULE_REQUIRED', '该影厅没有可用的专用报价规则，请店家核价')
            return rule.fixed_cost_fen if rule is not None else None
        area_only_unavailable = False
        try:
            cost = resolve_cost(
                bridge,
                city_id=city_code,
                cinema_id=str(result.cinema_id),
                showtime_resolver=lambda _cid, _sid=show.showtime_id: _sid,
                phone=phone,
                seat_names=seat_names,
                seat_count=count,
                verified_route=(getattr(result, 'wanda_verified_route', None)
                                or getattr(result, 'wanda_price_check_route', None)),
                fixed_cost_resolver=fixed_cost_for_official_price if hall_policy else None,
                **({'require_available': True} if result.raw_results.get('buyer_text_details') or result.raw_results.get('directory_resolved_image_request') or getattr(result, 'wanda_verified_route', None) or result.price_mismatch else {}))
            if hall_policy:
                final_rule = pricing_engine.find_rule(now=pricing_time, channel=CHANNEL_WANDA,
                    brand=result.brand_name or '', cinema=pricing_cinema, hall_name=show.hall_name or '',
                    original_price_yuan=cost.original_total_fen / 100 if cost.original_total_fen else None)
                if final_rule is not None and final_rule.fixed_cost_fen is not None:
                    if (not seat_names or cost.source != 'WANDA_RULE_FIXED_COST'
                            or cost.unit_cost_fen != final_rule.fixed_cost_fen):
                        raise WandaCostError('VIP_SEATS_REQUIRED', '固定预估成本规则必须先确认具体座位及官方原价')
        except WandaCostError as exc:
            area_only_unavailable = False
            if exc.code == 'SEAT_UNAVAILABLE' and seat_names:
                try:
                    cost = resolve_cost(
                        bridge, city_id=city_code, cinema_id=str(result.cinema_id),
                        showtime_resolver=lambda _cid, _sid=show.showtime_id: _sid,
                        phone=phone, seat_names=seat_names, seat_count=count,
                        verified_route=(getattr(result, 'wanda_verified_route', None)
                                        or getattr(result, 'wanda_price_check_route', None)),
                        fixed_cost_resolver=fixed_cost_for_official_price if hall_policy else None,
                        require_available=False, allow_unavailable_area_quote=True)
                    area_only_unavailable = True
                    out.notes.append('买家标记的座位已有占用：已按相同座位区域报价，仅供换座参考，不确认原座位可出票')
                    seat_names = []
                    result.expected_seat_count = None
                except WandaCostError:
                    pass
            if area_only_unavailable:
                pass
            else:
                out.ok = False
                out.event = Event.RECOGNIZE_FAIL
                if conv.state in (ConvState.QUOTED, ConvState.RECOGNIZING):
                    conv.apply(Event.RECOGNIZE_FAIL)
                out.state = conv.state
                conv.quote = None
                conv.quote_id = ''
                conv.quote_kind = ''
                ident = {k: getattr(conv, k, '') for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')}
                if self.quote_store is not None and all(ident.values()):
                    self.quote_store.supersede_for_identity(ident, demand_id=getattr(conv, 'demand_id', '') or None)
                out.notes.append(f'万达成本查询失败：{exc.code}')
                reason = ('万达接口暂未返回所选座位区域的会员价'
                          if exc.code == 'NO_MEMBER_PRICE' else _wanda_cost_hint(exc))
                out.add('WANDA_QUOTE_FAILED', {'fail_reason': reason})
                return out
            # Successful area-only fallback continues through normal quote calculation.
            if area_only_unavailable:
                pass
            else:
                return out

        # ★★ 原价的取值优先级：
        #     ① 截图上的逐座原价（seat[].originalPriceFen）最准 —— 是针对买家选的座位
        #     ② 截图上的总价（priceAllFen）—— 识别结果本来就有
        #     ③ 万达座位图的区域原价 —— 最后兜底
        original = (cost.original_total_fen if result.price_mismatch or hall_policy or area_only_unavailable else
                    _seats_original_fen(result)
                    or (result.price_all_fen or None)
                    or cost.original_total_fen)

        # ★ 注意：cost.as_engine_kwargs() 里已经带了 original_total_fen，
        #   所以不能用 ** 展开再显式传（会 TypeError: got multiple values）。
        #   这里先取出、覆盖成上面算好的那个。
        engine_kw = cost.as_engine_kwargs()
        engine_kw["original_total_fen"] = original
        try:
            quote = pricing_engine.quote(now=pricing_time, brand=result.brand_name or "",
                cinema=pricing_cinema, hall_name=show.hall_name or "", channel=CHANNEL_WANDA,
                **{**engine_kw, **_fallback_quote_kwargs()})
        except ValueError as exc:
            out.ok = False
            out.add('WANDA_QUOTE_FAILED', {'fail_reason': '影厅报价规则无法核价，请店家核对规则和官方原价'})
            out.notes.append(str(exc))
            return out

        out.quote = quote
        out.notes.extend(quote.notes)
        # ★ 把成本来源写进备注，出问题时能一眼看出价是怎么来的
        out.notes.append(
            f"万达渠道：{show.starts_at_text} {show.hall_name} "
            f"（{cost.area_name} {'规则固定预估成本' if cost.source=='WANDA_RULE_FIXED_COST' else '会员价'} {cost.unit_cost_fen / 100:.2f} 元/座"
            f" × {cost.seat_count}）")
        out.notes.extend(cost.notes)

        # 未确认张数时只保存单价结果，不能用于改价或下单。
        reference_only = not seat_names and not result.expected_seat_count
        self_buy_total = None if area_only_unavailable else _buyer_self_buy_total(result, quote.seat_count)
        if not reference_only and self_buy_total is not None and quote.total_price_fen >= self_buy_total:
            from .quote_comparison import remember
            remember(conv,result,quote,provider='WANDA',cost_total_fen=cost.unit_cost_fen*cost.seat_count,
                     cost_source=cost.source,buyer_total_fen=self_buy_total,
                     estimated=cost.source=='WANDA_RULE_FIXED_COST')
            decision = conv.apply(Event.QUOTE_HIGHER)
            out.state, out.event = decision.state, Event.QUOTE_HIGHER
            conv.quote = None
            conv.quote_id = ''
            conv.quote_kind = ''
            ident = {k: getattr(conv, k, '') for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')}
            if self.quote_store is not None and all(ident.values()):
                self.quote_store.supersede_for_identity(ident, demand_id=getattr(conv, 'demand_id', '') or None)
            out.add('SELF_BUY_HINT')
            out.notes.append(f'万达报价 {quote.total_price_fen} ≥ 截图自购价 {self_buy_total}；不生成可交易报价')
            return out
        conv.quote_kind = "UNIT_PRICE" if reference_only else "ORDER_TOTAL"
        self._stash_quote(conv, result, seat_names, "", quote,
                          QuoteParams(), out, channel="WANDA")
        if reference_only:
            raw = result.raw_results or {}
            marked_count = raw.get('markedCount') or raw.get('marked_count')
            if not (type(marked_count) is int and 1 <= marked_count <= 99):
                raw_seats = raw.get('seat')
                marked_count = len(raw_seats) if isinstance(raw_seats, list) and raw_seats else 0
            values = {
                "city": result.city_name or "", "cinema": route.wanda_cinema_name.strip() or result.cinema_name or "",
                "movie": result.movie_name or "", "show_time": result.start_time or "",
                "hall": result.hall_name or "", "unit_price": _fen_to_yuan(quote.unit_price_fen),
                "marked_count": marked_count or quote.seat_count,
            }
            if area_only_unavailable:
                out.add("WANDA_UNIT_PRICE", {**values, "_variant": "reselect"})
            elif marked_count:
                out.add("WANDA_UNIT_PRICE", {**values, "_variant": "marked"})
            else:
                out.add("WANDA_UNIT_PRICE", values)
            return out
        return self._emit_quote_card(result,
                                     seats or [{"seatNo": ""}] * count,
                                     quote, out, cinema_display=route.wanda_cinema_name.strip())

    # ------------------------------------------------------------ 良票报价
    def _quote_via_liangpiao(self, conv, result: RecognitionResult,
                             seats: list, ticket_mode: str,
                             params: QuoteParams,
                             out: FlowResult) -> FlowResult:
        """良票渠道报价（原来的路径，行为不变）。"""
        if not seats:
            # ★ 良票的 preflight 是【选座询价】，seats 必填 1~6。
            #   ⇒ 没座位号就询不到价，这是渠道限制。
            #   文案要说清"需要圈座位"，而不是含糊地说"询不到价"。
            # ★ 用 seat_missing 变体（= 同业 bubble 29 的措辞）：
            #   「未识别到您选择的座位信息，辛苦选择一下座位信息截图发我哦」
            out.ok = False
            out.add("RECOG_FAIL", {
                "_variant": "seat_missing",
                "fail_reason": "这家的价格要按具体座位算 —— "
                               "麻烦在截图上把想要的座位圈一下，我马上报价"})
            return out

        # ★★ 按场次的单笔限购数量拦一下（maxSeatsPerOrder）。
        #   ★ 为什么值得单独拦（而不是等上游拒）：
        #     良票文档 §4 座位图原文：
        #       「maxSeatsPerOrder 是本场次的单笔限购座位数，由供货方决定、
        #         可能低于平台上限 6（部分供货方单笔仅支持 4 张）。
        #         请在选座交互上就按它限制数量：
        #         ★ 超过限购的订单会在【出票环节】被拒。更多座位的需求请分多笔订单。」
        #     ⇒ 等到出票环节才拒，买家已经付过钱了（我们这边已经落单）——
        #       那是要退款的。在询价前用已有数据拦一下，成本为零。
        #   ★ 只在识别结果里有这个值时才拦（文档：仅 EXACT 且已拉座位图时有值）。
        #     没有值就不拦 —— 不能拿"默认 6"当依据，那是编的。
        limit = result.max_seats_per_order
        if limit and len(seats) > int(limit):
            out.ok = False
            out.add("RECOG_FAIL", {
                "_variant": "seat_limit",
                "fail_reason": f"这个场次一笔最多买 {int(limit)} 张"})
            out.notes.append(
                f"maxSeatsPerOrder={limit}，选了 {len(seats)} 张 —— "
                f"在询价前拦下（否则会在出票环节被拒，得退款）")
            return out

        try:
            pre = self.api.ch5_preflight(result.show_id, seats, ticket_mode,
                                         PRICE_LIMIT, params=params)
        except LiangPiaoError as exc:
            out.ok = False
            out.error = str(exc)
            # ★★ 200006 / 200007 在这里也会出现（询价同样跑座位图预校验）。
            #   和下单分支用同一套变体 —— 同一个错，不管在哪一步被拒，
            #   给买家的话必须一致（否则同一件事两套说法）。
            code = getattr(exc, "code", 0)
            if code == 200006:
                out.add("RECOG_FAIL", {"_variant": "seat_taken",
                                       "fail_reason": "您选的座位刚被别人买走了"})
            elif code == 200007:
                out.add("RECOG_FAIL", {"_variant": "seat_blocked",
                                       "fail_reason": "所选的座位不符合选座规则"})
            elif code == 200005:
                # 场次已停售（过了截单时间）—— 让买家换场次
                out.add("RECOG_FAIL", {"_variant": "show_stopped",
                                       "fail_reason": "这个场次已经停售了"})
            elif code == 300006:
                out.add("RECOG_FAIL", {"_variant": "price_changed",
                                       "fail_reason": "票价刚变了，得重新报价"})
            else:
                out.add("RECOG_FAIL", {"fail_reason": "这个场次暂时询不到价，请稍后再试"})
            return out

        if not pre.available:
            out.ok = False
            out.add("RECOG_FAIL", {"fail_reason": pre.reason or "该场次暂不可售"})
            return out

        # ---- 报价规则算售价 ----
        # ★ 成本口径用 estimateAmount（LIMIT 模式实扣以放映后结算为准）
        quote = (self.rules_provider(conv) if self.rules_provider else self.rules).quote(
            estimate_total_fen=pre.cost_basis_fen,
            seat_count=len(seats),
            now=conv_now().time(),
            brand=result.brand_name or "",
            cinema=result.cinema_name or "",
            original_total_fen=(_seats_original_fen(result) or pre.market_amount_fen),
            # ★ 走良票渠道 → 允许命中"良票报价（V5）"这条渠道规则
            channel=CHANNEL_LIANGPIAO, hall_name=result.hall_name or '',
            # ★★★ 2026-10-14：限价要按"平台可能要付到的上限"算，不是按均价成本。
            #   跨区座位时 estimateAmount 与 totalAmount 会分开
            #   （实测 7509 vs 9353，原价 9280），按前者反推的 maxPrice
            #   低于实际应付 → 下单报 300005 失败。
            #   店主原话：「买家发送的座位图两个座位价格不一样，会建单失败」。
            cost_ceiling_fen=pre.price_ceiling_fen,
            # ★ 兜底报价的设置（模式 / 是否限价）
            **_fallback_quote_kwargs(),)
        out.quote = quote
        out.notes.extend(quote.notes)

        # ★★ 这个价是【预估/上限】还是确定价？
        #
        #   文档 5.1：「estimated  true 表示 totalAmount 是预估上限而非确定成交价」
        #   文档 1101：「报价有时效，询价后请尽快下单，下单时平台会再实时询一次，
        #               以下单结果为准；价格波动超过你的预期时可用下单的 maxPrice 兜底」
        #
        #   ⇒ 是预估价时，必须让上层知道，不能当成交价承诺出去。
        #     这个判断原来【没人做】—— estimated 字段解析了但一次没读。
        if pre.cost_basis_is_estimate:
            out.notes.append(
                f"★ 成本基准是【预估/上限】（estimateAmount="
                f"{pre.estimate_amount_fen} totalAmount={pre.total_amount_fen} "
                f"estimated={pre.estimated}）—— 报价标为预估，"
                f"下单时以 maxPrice 兜底")
            out.price_is_estimate = True

        # ★ 报价高于自购价 → 引导买家自己买（bubble 3）
        if pre.market_amount_fen and quote.total_price_fen >= pre.market_amount_fen:
            from .quote_comparison import remember
            remember(conv,result,quote,provider='LIANGPIAO',cost_total_fen=pre.cost_basis_fen,
                     cost_source='LIANGPIAO_PREFLIGHT',buyer_total_fen=pre.market_amount_fen,
                     estimated=pre.cost_basis_is_estimate,comparison_source='SUPPLIER_MARKET')
            conv.quote=None;conv.quote_id='';conv.quote_kind=''
            ident={k:getattr(conv,k,'') for k in ('tenant_id','shop_id','buyer_id','chat_id')}
            if self.quote_store is not None and all(ident.values()):
                self.quote_store.supersede_for_identity(ident,demand_id=getattr(conv,'demand_id','') or None)
            decision = conv.apply(Event.QUOTE_HIGHER)
            out.state = decision.state
            out.event = Event.QUOTE_HIGHER
            out.add("SELF_BUY_HINT")
            out.notes.append(
                f"报价 {quote.total_price_fen} ≥ 原价 {pre.market_amount_fen}，"
                f"不硬卖")
            return out

        # ---- 存下来，下单要用 ----
        self._stash_quote(conv, result, seats, ticket_mode, quote, params, out,
                          channel="LIANGPIAO")
        return self._emit_quote_card(result, seats, quote, out)

    # ------------------------------------------------------------ 共用收尾
    def _stash_quote(self, conv, result: RecognitionResult, seats: list,
                     ticket_mode: str, quote, params, out: FlowResult,
                     *, channel: str = "") -> None:
        """
        把这次报价【定下来】的东西存进会话 + 落一条报价记录。

        ★★ 为什么单独提出来：
          万达和良票两条路都要做同样这几件事，但成本来源不同。
          复制两份的话，"报价记录里存什么"会慢慢漂移 ——
          而报价记录是事后对账的唯一依据，不能两边不一致。

        ★★★ channel 为什么必须和 ticket_mode 分开（2026-10-14 拆的）：
          原先这两件事【共用了一个参数】：
              conv.ticket_mode  = ticket_mode                       ← 档位（时效）
              conv.quote_channel = "WANDA" if ticket_mode=="WANDA"  ← 渠道（分流）
          而万达分支传的是 "WANDA"（渠道名），良票分支传的是
          "STANDARD"/"FAST"/"FLASH"（档位）—— 同一个形参两种含义。
          后果（实测发现的真 bug）：
            · 万达单的 conv.ticket_mode 变成 "WANDA"，
              而时效表 TICKET_WINDOW 只有 STANDARD/FAST/FLASH
              ⇒ _window() 查不到，【靠兜底才】拿到 "1-60分钟"；
                模型那边（conversation_ai）查不到就直接没有时效。
          ★ 危险点：不能只把 "WANDA" 换成档位 ——
            那会让 quote_channel 判成 LIANGPIAO，
            万达的单被确认后会拿万达的 showtimeId 去调【良票下单】。
            那是真下单，动钱。
          ⇒ 所以拆成两个参数：
              ticket_mode = 档位（只用于时效/询价）
              channel     = "WANDA"/"LIANGPIAO"（只用于分流和文案覆盖）
        """
        if str(channel).upper() != "WANDA":
            conv.quote_kind = "ORDER_TOTAL"
        conv.quote_id = ""
        conv.quote = quote
        conv.quote_params = params
        conv.recognition = result
        conv.seats = seats
        # ★ 报过价 ⇒ 进入"刚谈过价"的语境：这时候买家说「改价」才有意义。
        #   （service 的意图层靠这个标志决定要不要认 REPRICE_REQUEST；
        #     见 domain/conversation.py 里 price_talk_open 的说明。）
        conv.price_talk_open = True
        # ★ 档位：只可能是 STANDARD/FAST/FLASH（或空 = 不知道，那就别承诺时效）
        conv.ticket_mode = ticket_mode
        # ★ 渠道：显式传；没传就退回旧行为（按档位名猜），保证兼容
        chan = str(channel or "").upper()
        if chan not in ("WANDA", "LIANGPIAO"):
            chan = "WANDA" if ticket_mode == "WANDA" else "LIANGPIAO"
        # ★★ 记下这单是哪条渠道的价 —— 下单时靠它分流。
        #   不记的话，万达的报价被确认后会拿万达的 showtimeId
        #   去调良票下单（见 on_buyer_confirm 的渠道守卫）。
        conv.quote_channel = chan
        conv.channel = out.channel = "wanda" if chan == "WANDA" else "liangpiao"
        # 报价有效期由状态机管（配置 30 分钟）
        out.state = conv.state

        # ★★ 落一笔报价记录（配了 quote_store 才落）。
        #
        #   为什么在【这里】落：这是报价【真正定下来】的那一刻 ——
        #   priceMode / maxPrice 已经确定，出门就要按它下单。
        #   早于此报价还没定；晚于此可能有别的分支 return 掉。
        #
        #   ★ 报价记录里必须存【规则版本】：规则改了之后，
        #     "这笔 184 元按哪版规则算的"只能从记录里查。
        if self.quote_store is not None:
            try:
                self._persist_quote(conv, result, quote, seats, ticket_mode)
            except Exception as exc:                     # noqa: BLE001
                # ★ 落库失败不能让买家收不到报价 ——
                #   记下来给人看，报价照发。
                out.notes.append(
                    f"★ 报价记录落库失败（{type(exc).__name__}: {exc}）"
                    f"—— 报价已发，但这次没有留下记录，请检查存储")

        # ★★ 无论如何都要有一个 quote_id。
        #   为什么：Order 模型【强制要求】订单绑一笔报价
        #   （OrderStore.create 里 `if not order.quote_id: raise`），
        #   而 quote_id 原先只在 _persist_quote 里设 ——
        #   那要求配了 quote_store。
        #   ⇒ 没配 store 时，订单会以
        #     "ValueError: ORDER_QUOTE_REQUIRED" 建不出来，
        #     而买家那边只看到"已下单"（这个异常被 except 记进 _order_error）。
        #   ★ 用一个可推导的本地 id（会话 + 报价内容哈希），
        #     这样同一会话的同一笔报价重复走到这里仍然是同一个 id。
        if not getattr(conv, "quote_id", ""):
            seed = "|".join([
                str(conv.chat_id), str(getattr(result, "show_id", "")),
                str(quote.total_price_fen), str(len(_seat_names_of(seats))),
            ])
            conv.quote_id = "local-" + hashlib.sha256(
                seed.encode()).hexdigest()[:24]

        if getattr(conv, "quote_kind", "") == "UNIT_PRICE":
            conv.quote = None
            conv.price_talk_open = False
            return

        # ★★ 2026-10-14 接线：「设置 → 二次报价 → 开关 / 等待拍下时间」
        #   ★ 排一条"到点没拍就再报一次"。到点由 handle_run_due 推进
        #     （和退款时序、人工超时同一套机制，见 schedule_queue）。
        #   ★ 幂等键 = 会话 + 报价 id ⇒ 同一次报价只排一条，
        #     买家在等待期间又发消息也不会把倒计时重来。
        #   ★ 首报本身不排（is_second_quote）—— 否则二次报价之后
        #     又排一条，会无限循环。
        try:
            if not getattr(quote, "is_second_quote", False) and not getattr(conv, 'demand_inquiry', False):
                # ★ 等多久由 _second_quote_wait_seconds() 读（收在一处）；
                #   0 = 没开 ⇒ 不排。
                _wait = _second_quote_wait_seconds()
                if _wait > 0:
                    from application import schedule_queue as _sq
                    _sq.schedule("second_quote", f"{conv.chat_id}:{conv.quote_id}",
                                 delay_seconds=_wait)
                    out.notes.append(
                        f"二次报价已排队（{_wait} 秒后，若还没拍下就再报一次）")
        except Exception as _exc:                             # noqa: BLE001
            # ★ 排队失败不影响首报
            out.notes.append(f"★ 二次报价没排上（不影响首报）：{_exc}")

    @by_conversation
    def send_second_quote(self, conv: Conversation) -> FlowResult | None:
        """
        发一次二次报价。不适合发 ⇒ 返回 None。

        ★★ 什么时候【不】发（每条都有理由，别删）：
            · 开关没开 / 配置读不到
            · 会话上根本没有报价（没东西可打折）
            · 状态已经不是【已报价】⇒ 买家往下走了（拍了/付款了/转人工了），
              再报价就是打扰
            · 报价已过期 ⇒ 过期价再打折是错的（成本可能都变了）
            · 已经发过二次报价 ⇒ 幂等（否则到点后每 tick 发一遍）
        ★ 优惠由引擎算（quote_rules.second_quote）——
          保证"最多让完毛利、绝不倒贴"。这里不自己算钱。
        """
        cfg = _second_quote_cfg()
        if cfg is None:
            return None
        quote = getattr(conv, "quote", None)
        if quote is None:
            return None
        if conv.state is not ConvState.QUOTED:
            return None
        # 报价过期就不打折了 —— 过期价的成本依据已经不作数
        exp = getattr(conv, "quote_expires_at", None)
        if conv.channel != "wanda" and exp is not None:
            try:
                if exp < conv_now():
                    return None
            except TypeError:
                return None
        if getattr(quote, "is_second_quote", False):
            return None          # ★ 幂等：已经发过就不再发
        pct = int(getattr(cfg, "discount_percent", 0) or 0)
        new_quote = self.rules.second_quote(
            quote, discount_percent=pct,
            seat_count=len(getattr(conv, "seats", None) or []) or None)
        if new_quote is quote:
            # 引擎没改（毛利<=0 或比例为 0）⇒ 说明让不出优惠。
            # ★ 这种情况【不发】那条"再优惠"的话 ——
            #   让不出还说"再让一点"是骗人。
            return None
        out = FlowResult(ok=True, state=conv.state, channel=conv.channel)
        out.quote = new_quote
        # ★ 会话上的报价换成新的 ⇒ 之后下单/改价用新价，金额不会前后不一致
        conv.quote = new_quote
        out.notes.extend(new_quote.notes)
        _n = len(getattr(conv, "seats", None) or []) or 1
        out.add("SECOND_QUOTE", {
            "total_price": _fen_to_yuan(new_quote.total_price_fen),
            "origin_total": _fen_to_yuan(
                int(getattr(quote, "original_total_fen", 0) or 0)),
            "discount_percent": str(pct),
        })
        out.notes.append(
            f"二次报价已发：{quote.total_price_fen} → "
            f"{new_quote.total_price_fen} 分（优惠 {pct}%）")
        return out


    @by_conversation
    def request_intercept(self, conv: Conversation, *,
                          cfg=None) -> FlowResult:
        """
        买家要求拦截订单 ⇒ 回那句话 + 发关单命令。

        ★★ 不保证拦截成功 —— 配置文件里的话术自己就写着
          "但不保证拦截成功。拦截成功订单将出票失败处理"。
          这话术我【原样用】，不改成"已为您拦截成功"（那是骗人）。
        ★ 关单能不能成，由插件那边判定：
          · 只在 live 模式做（shadow 只返回 WOULD_SEND）
          · 订单必须是【已付款未发货】(orderStatus 2/6)
          · 订单身份要逐字对上（orderId + shop accountUnb）
          ⇒ 所以这里"发了命令"不等于"拦截成功"，话术也照这个口径说。
        ★ 没有在途订单 / 没有平台订单号 ⇒ 不发关单命令（发了也必被拒），
          但仍然回那句"不保证成功"的话 —— 因为买家要的是"我提了"这个确认。
        """
        out = FlowResult(ok=True, state=conv.state, channel=conv.channel)
        _cfg = cfg
        if _cfg is None:
            try:
                import settings as _st
                _cfg = _st.load()
            except Exception:                                 # noqa: BLE001
                _cfg = None
        msg = ""
        if _cfg is not None:
            msg = str(getattr(_cfg.order_intercept, "message", "") or "").strip()
        out.messages.append(msg or "系统已为您发起拦截，但不保证拦截成功。")
        out.template_keys.append("ORDER_INTERCEPT")

        pending = getattr(conv, "pending_order", None)
        online = str(getattr(pending, "provider_order_id", "") or "")
        local_no = str(getattr(pending, "order_no", "") or "")
        if not online:
            # ★ 没有平台订单号就【不发】关单命令 ——
            #   插件会用 client.orders.get(orderId) 校验身份，
            #   空号必然报 CLOSE_ORDER_IDENTITY_MISMATCH，
            #   发出去只是浪费一次调用 + 留一条失败记录。
            mark_intercept_attempted(pending)
            out.notes.append(
                "★ 拦截：没有平台订单号 ⇒ 没发关单命令"
                "（订单可能还没付款/还没同步到平台号）")
            return out
        try:
            from integrations.yumaiduo.commands import Command as _Cmd
            import uuid as _uuid
            _ident = {
                "tenant_id": str(getattr(pending, "tenant_id", "") or ""),
                "shop_id": str(getattr(pending, "shop_id", "") or ""),
                "buyer_id": str(getattr(pending, "buyer_id", "") or ""),
                "chat_id": str(conv.chat_id or ""),
            }
            out.pending_commands.append(_Cmd(
                command_id=str(_uuid.uuid4()), type="CLOSE_FAILED_ORDER",
                event_id=local_no, session_id=_ident["chat_id"],
                text="买家要求拦截订单",
                # ★ 幂等：同一张单只发一次拦截（买家反复说"拦截订单"不该反复关）
                dedupe_key=f"intercept:{online}",
                extra={"order_id": online, "booking_order_id": local_no,
                       "side_effect": "INTERCEPT_BY_BUYER"},
                **_ident))
            mark_intercept_attempted(pending)
            out.notes.append(
                f"拦截：已排关单命令（平台单号 {online}）"
                f"—— 成不成由插件按「已付款未发货」判定")
        except Exception as _exc:                             # noqa: BLE001
            out.notes.append(f"★ 拦截关单命令没排上：{_exc}")
        return out


    def _emit_quote_card(self, result: RecognitionResult, seats: list,
                         quote, out: FlowResult, *, cinema_display: str = "") -> FlowResult:
        """出报价卡（两条渠道共用）。"""
        # ★★★ 原价的四个来源，按【买家可比性】排序（2026-10-14 修两轮）：
        #
        #   ① 截图上的逐座原价（seat[].originalPriceFen）
        #      —— 针对买家选的那几个座位，最准
        #   ② 良票 prices[].originalPrice × 张数
        #      ★★ 这是【买家在 App 上看到的口径】，也是唯一"同一场次所有人
        #         都看到同一个数"的基准 —— 拿它比价才说得通
        #   ③ 截图上的总价（priceAllFen）
        #   ④ 成本侧兜底（quote.original_total_fen —— 万达是
        #      area[].salesPrice × 张数，良票是 preflight 的 marketAmount）
        #
        #   ★ 第一轮坑：原价原来【只看 ①②（旧编号）】，完全没看 quote。
        #     而万达识别的 priceAllFen 实测【从不返回】（rawResults.priceAll=null）
        #     ⇒ 万达单的报价卡原价恒为 0.00 元，买家看到「原价 0.00 / 优惠后 35.00」。
        #
        #   ★★ 第二轮坑（更严重，是合规问题）：
        #     修了第一轮之后，万达单的原价来自 ④ = 座位图 area[].salesPrice。
        #     而万达【每个分区的 salesPrice 不同】，我们取的是【W+专享区】的价
        #     —— 那恰好是全场最贵的区！
        #     实测（娄底五江 2026-10-14 20:35 7号厅）：
        #         良票给的原价（买家看到的） = 3590
        #         万达 W+专享 salesPrice      = 4090  ← 我们取了这个
        #         普通区 3590 / 优选区 3790 / 特惠区 3390
        #     ⇒ 报价卡显示「原价 40.90 / 优惠后 32.40」= 号称省 8.50 元，
        #       而实际只比买家能看到的价便宜 3.50 元。★ 把优惠说大了。
        #     ⇒ 所以 ② 必须排在 ④ 前面：用买家可比的口径，不用我们进货区的挂牌价。
        prices_original = None
        # ★ 用 getattr 防御：这个函数在测试里会被传各种 stub，
        #   而"原价取哪个"是纯展示逻辑，不该因为缺个字段就整条报价卡炸掉。
        for row in (getattr(result, "prices", None) or []):
            if not isinstance(row, dict):
                continue
            try:
                value = int(row.get("originalPrice") or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                prices_original = value * max(1, len(seats))
                break
        origin_total = ((getattr(quote, 'original_total_fen', None) or 0)
                        if getattr(result, 'price_mismatch', False) or getattr(quote,'cost_basis','') in ('WANDA_VIP_FIXED_COST','WANDA_RULE_FIXED_COST') else _seats_original_fen(result)
                        or prices_original
                        or result.price_all_fen
                        or getattr(quote, "original_total_fen", None)
                        or 0)
        n = max(1, len(seats))
        # ★★★ 没选座位时，报价卡的「座位：」「合计：」都会误导（2026-10-14 加）。
        #
        #   实测（两张真图都是这个状态）：识别返回 seat=[]，
        #   因为买家截图时【还停在「请先选座」】（良票文档：这不是错误）
        #   ⇒ 报价卡出现两处误导：
        #        「座位：」          ← 空的
        #        「合计：32.40元」   ← 按【1 张】算的，看着像整单成交价
        #
        #   ★ 店主的决定（选项 A）：还是按 1 张报单价，
        #     但【明确写出来】—— 按实际张数结算，张数待确认。
        #   ★ 为什么不擅自改成"先问几张"：
        #     店主早前明确说过「没选座位也报价」。改成先问会违背那条决定。
        #   ★ 为什么必须写出来而不能含糊：
        #     不写，买家会以为合计就是 32.40 元；等他拍下按 2 张算，
        #     金额对不上就变成纠分。
        no_seat = not any((s.get("seatNo") or "").strip() for s in seats)
        variables = {
            "city": result.city_name or "",
            "cinema": cinema_display or result.cinema_name or "",
            "movie": result.movie_name or "",
            "hall": result.hall_name or "",
            "show_time": result.start_time or "",
            "seats": "，".join(s.get("seatNo") or "" for s in seats),
            "origin_unit": _fen_to_yuan(origin_total // n),
            "origin_total": _fen_to_yuan(origin_total),
            "unit_price": _fen_to_yuan(quote.unit_price_fen),
            "total_price": _fen_to_yuan(quote.total_price_fen),
            "cinema_addr": result.cinema_address or "",
        }
        if no_seat:
            variables["seats"] = "根据图片标记的位置出票"
        variables["count"] = str(quote.seat_count)
        # 万达整单报价始终使用店主配置的报价卡。
        # 票数及影院匹配数不能作为切换万达文案的依据。
        use_multi = (out.channel != "wanda" and len(seats) > 1
                     and result.cinema_hit_nums > 1)
        out.add("QUOTE_CARD_MULTI" if use_multi else "QUOTE_CARD", variables)
        # ★★ 2026-10-14 店主定案：报价卡上【本来就有】那一问 ——
        #   「★ 座位是否在图片中标记？需要几张票？」
        #   （加这一问的原因：之前的版本没有"图片是否标记"的判断，
        #     所以要问买家；座位数也常识别不出，一并问。）
        #   ⇒ 我昨天加的这条 SEAT_COUNT_PENDING 就【重复】了，已去掉。
        #     一条消息问一件事、不重复问同一个问题。
        return out

    # ================================================================ 4 下单
    @by_conversation
    def on_buyer_confirm(self, conv: Conversation) -> FlowResult:
        """
        买家回复确认指令（配置里的「正确」）→ 下单。

        ★ 三个必须守住的点：
          1. 用【同一个 QuoteParams】—— 询价与下单传的值必须一致
          2. maxPrice 用规则算出来的，且【数字】不是字符串
          3. outOrderNo 重试复用；被拒（REJECTED）后要换新的
        """
        out = FlowResult(channel=getattr(conv, "channel", None))
        quote = getattr(conv, "quote", None)
        result = getattr(conv, "recognition", None)
        seats = getattr(conv, "seats", []) or []

        if quote is None or result is None or (not seats and not (
                getattr(conv, "quote_channel", "") == "WANDA"
                and getattr(conv, "quote_kind", "") == "ORDER_TOTAL"
                and quote.seat_count > 0)):
            # 没有有效报价 —— bubble 16
            out.ok = False
            out.add("NO_QUOTE")
            return out

        # 进状态机（会做报价过期检查）
        decision = conv.apply(Event.BUYER_CONFIRM)
        out.event = Event.BUYER_CONFIRM
        out.state = decision.state
        if decision.reply_key:
            out.add(decision.reply_key)
        if decision.state is ConvState.IDLE:
            # 过期被拦下了
            out.ok = False
            return out

        # ★★★ 万达：店家【手动出票】，插件不调任何下单接口。
        #
        #   为什么这样是对的（不是"还没做完"）：
        #     · 万达的单由店家在出票系统/万达后台手动建 —— 我们不碰
        #     · 我们的职责是：把这单落下来（店家能在订单管理页看到）、
        #       告诉买家出票时间
        #     · 票出来后店家在订单管理页填票码 →
        #       POST /api/order/deliver → WandaDelivery.send() 自动发给买家
        #     ★ 这和良票的区别只在"票从哪来"：
        #         良票：我们调 ch5_create，票码走回调
        #         万达：店家手动出，票码走 /api/order/deliver
        #
        #   ★ 为什么不接出票系统的下单接口：
        #     下单要处理锁座超时/支付失败/重复下单/退款/幂等/补偿，
        #     出票系统已经有一整套（wplus-recovery 就是给探针做补偿的）。
        #     我们重做一遍只会把状态机分裂到两个系统。
        if getattr(conv, "quote_channel", "") == "WANDA":
            self._seq += 1
            out_order_no = make_out_order_no(self.prefix, self._seq)
            order_id = self._persist_wanda_order(
                conv, result, quote, seats, out_order_no)
            out.order_no = order_id
            out.out_order_no = out_order_no

            # ★★ 等【真实付款信号】，不在这里推 ORDER_PAID。
            #   付款由鱼麦多的 order.paid 事件驱动
            #   （plugin_runtime._from_order_paid，它那边本来就有这条）。
            #   ⇒ 这里给买家"已下单、请付款"的提示；
            #     出票时效等付款确认后再讲。
            out.notes.append(
                "★ 万达渠道：已落单（待付款），等鱼麦多 order.paid 事件；"
                "付款后店家在订单管理页手动出票，"
                "票码由店家填入（POST /api/order/deliver）")
            out.add("TICKETING", {"cinema": result.cinema_name or "",
                                  "ticket_window": self._window(conv),
                                  "pickup_link": ""})
            return out

        # ★★★ 2026-10-14 店主定的时序（重要，别改回去）：
        #   「必须金额校验正确，且已付款，才可以良票建单」
        #
        #   ⇒ 所以这里【不调 ch5_create】。买家刚说"确认"的时候
        #     还没付款、也没有可校验的实付金额；那时候建上游单
        #     一旦后面金额不符或买家不付，就是一张撤不掉的上游单。
        #   ⇒ 这一段只做三件事：
        #     ① 生成我们自己的单号
        #     ② 落一条【本地待付款单】（订单管理页能看到他在等付款）
        #     ③ 推 BUYER_CONFIRM → AWAIT_REPRICE，发 bubble 17（拍下别付款）
        #   真正的 ch5_create 在 on_payment_confirmed 里 —— 那时
        #   金额已核对（严格）、付款已确认。
        #
        #   ★ 为什么良票现在和万达一样"先只落本地单"：
        #     两条渠道的【付款时机】本来就一样（买家在闲鱼付），
        #     所以"先落本地单、等付款信号"对两边都成立。
        #     区别只在付款后：良票要 ch5_create，万达只要通知店家。
        self._seq += 1
        out_order_no = make_out_order_no(self.prefix, self._seq)
        self._persist_order(conv, result, quote, seats, None, out_order_no)
        out.out_order_no = out_order_no
        out.notes.append(
            "★ 良票渠道：已落【本地待付款单】，等鱼麦多 order.paid 事件；"
            "★ 付款 + 金额校验通过后才调 ch5_create（店主定的时序）")
        # ★ 复用单号，供付款时建上游单使用（回调/幂等都靠它）
        conv.quote_params = getattr(conv, "quote_params", None)
        setattr(conv, "pending_out_order_no", out_order_no)
        out.add("ORDERED_NO_PAY")
        return out

    # ============================================================ 4.5 付款后建单
    @by_conversation
    def on_payment_confirmed(self, conv: Conversation, out: FlowResult,
                             *, paid_fen: int | None = None) -> FlowResult:
        """
        【付款已确认】之后才做的事。

        店主定的时序：「必须金额校验正确，且已付款，才可以良票建单」。
        所以这个方法是良票唯一允许调 ch5_create 的地方。

        ★ 三道门，全过才建单：
          ① 金额严格相等（分文不差，由 ledger.confirm_payment 判）
             —— 不相等的话调用方【不会】调到这里（见 plugin_runtime）
          ② 报价还有效、张数/座位和报价时一致
          ③ 上游建单成功
        ★ 建单失败怎么办：
          良票文档：order.failed = 「出票失败、出票前取消，或开放 API
          下单未受理(未冻结)；冻结已释放或不曾冻结」
          ⇒ 失败时良票自动释放全额冻结，我们不需要自己有退款能力。
          买家侧的钱由店家在闲鱼退（退款命令类型插件端还没有，见
          plugin_runtime 的 REFUND_AND_CLOSE 注释）。
        """
        quote = getattr(conv, "quote", None)
        result = getattr(conv, "recognition", None)
        seats = getattr(conv, "seats", []) or []
        if quote is None or result is None or (not seats and not (
                getattr(conv, "quote_channel", "") == "WANDA"
                and getattr(conv, "quote_kind", "") == "ORDER_TOTAL"
                and quote.seat_count > 0)):
            out.ok = False
            out.add("NO_QUOTE")
            return out
        # ★★★ 第零道门：状态机必须允许「付款」这一步（演练抓到的漏洞）。
        #   只靠"能进来"判断是不够的 —— 演练里有这么一条：
        #     状态还是【已报价】（买家根本没确认），直接调这个方法，
        #     结果【真的建了单】。因为 conv.apply(ORDER_PAID) 走到后面才执行，
        #     而它非法时不会拦住前面的 ch5_create。
        #   ⇒ 先问状态机：(当前状态, order.paid) 合不合法。
        #     不合法就什么都不做 —— 没确认过的会话不该建单。
        if (conv.state, Event.ORDER_PAID) not in TRANSITIONS:
            out.ok = False
            out.notes.append(
                f"★★★ 付款确认被状态机拦住：{conv.state.value} 下不允许"
                f" order.paid（买家可能还没确认过）—— 没有建单")
            return out
        if not self.tel:
            out.ok = False
            out.need_human = True
            out.notes.append("★ 已在闲鱼付款，但缺购票手机号 tel，建不了单 —— "
                             "赶紧到功能设置里填，或人工处理这单")
            out.add("RECOG_FAIL", {"fail_reason": "店铺还没配购票手机号"})
            return out

        # ---- ★★ 第一道门：金额严格核对（双保险，主判据在 ledger 里）----
        #   为什么这里再核一遍：ledger.confirm_payment 判的是"闲鱼实付 vs
        #   我们落的 order.amount_fen"；而这里判的是"我们即将下给良票的
        #   金额 vs 会话里的报价"。两者都相等，才说明三处一致。
        expect = int(quote.total_price_fen or 0)
        if paid_fen is not None and int(paid_fen) != expect:
            out.ok = False
            out.notes.append(
                f"★★★ 建单前金额门拦住：实付 {paid_fen} ≠ 报价 {expect} 分")
            out.add("AMOUNT_MISMATCH")
            return out
        # ---- 第二道门：张数与座位必须和报价时一致 ----
        seat_names = _seat_names_of(seats)
        verified_count = (int(result.expected_seat_count or 0)
                          if getattr(conv, "quote_channel", "") == "WANDA" and not seat_names
                          else len(seat_names))
        if int(quote.seat_count or 0) != verified_count:
            out.ok = False
            out.notes.append(
                f"★★★ 建单前张数门拦住：报价 {quote.seat_count} 张"
                f" ≠ 现在 {len(seat_names)} 张")
            out.add("AMOUNT_MISMATCH")
            return out

        # ---- ★ 万达：没有上游可建，付款确认后直接等店家出票 ----
        if getattr(conv, "quote_channel", "") == "WANDA":
            decision = conv.apply(Event.ORDER_PAID)
            out.state = decision.state
            out.event = Event.ORDER_PAID
            out.add("TICKETING", {"cinema": result.cinema_name or "",
                                  "ticket_window": self._window(conv),
                                  "pickup_link": ""})
            out.notes.append("★ 万达：付款已确认，等店家在订单管理页手动出票")
            return out

        # ---- ★★★ 2026-10-14 接线：「设置 → 出票及放单 → 出票方式」----
        #   ★ 规格文档：单选「手动 / 自动」，默认【自动】。
        #   ★ 语义（照万达那条分支的成例）：
        #       自动 = 现在这样：我们调 ch5_create 在良票建单
        #       手动 = 【不自动建单】，只推进状态 + 提示店家去后台自己出票
        #   ★ 放在"金额门/张数门"之后、"建单"之前：
        #     两道门仍然要过（手动模式也不能拿错价单去人工建单），
        #     只是最后一步不由我们做。
        #   ★ 为什么店主想要这个：某些单他想自己在后台挑（比如座位有讲究），
        #     不希望机器先把单建掉 —— 建掉的单再改很麻烦。
        if _release_mode() == RELEASE_MANUAL:
            decision = conv.apply(Event.ORDER_PAID)
            out.state = decision.state
            out.event = Event.ORDER_PAID
            out.add("TICKETING", {"cinema": result.cinema_name or "",
                                  "ticket_window": self._window(conv),
                                  "pickup_link": ""})
            out.notes.append(
                "★ 出票方式=手动：付款已确认，但【没有自动在良票建单】——"
                "请店家在订单管理页/良票后台手动出票。"
                "（想恢复自动建单：设置 → 出票及放单 → 出票方式 选「自动」）")
            return out

        # ---- ★★★ 良票：到这里才允许建单（金额已核对、付款已确认）----
        out_order_no = (getattr(conv, "pending_out_order_no", "")
                        or make_out_order_no(self.prefix, self._seq))
        try:
            created = self.api.ch5_create(
                out_order_no, result.show_id, seats,
                getattr(conv, "ticket_mode", TICKET_STANDARD),
                quote.price_mode, self.tel,
                max_price_fen=quote.max_price_fen,
                params=getattr(conv, "quote_params", None),
                notify_url=self.notify_url,
                attach=conv.chat_id[:512],
                # ★★ 2026-10-14 接线：「设置 → 出票及放单 → 换座状态」
                #   规格默认"不允许"（allowSeatChange=False）—— 也就是
                #   "宁愿不出票，也不要换成别的座位"。
                #   ★ 原来这里【没传】这个参数，于是永远用签名里的默认 True
                #     ⇒ 界面上选了"不允许"也照样允许换座。
                #     api.ch5_create 的 payload 里一直有 allowSeatChange 这个字段，
                #     只是没人把配置喂给它。
                #   ★ 读不到配置就传 True（= 签名默认，保持原行为）：
                #     配置坏了不该让下单直接失败。
                allow_seat_change=_allow_seat_change(),
            )
        except LiangPiaoError as exc:
            out.ok = False
            out.error = str(exc)
            out.need_human = True
            code = getattr(exc, "code", 0)
            out.notes.append(
                f"★★★ 付款后建单失败 code={code} —— 买家已付款，"
                f"良票会自动释放冻结（order.failed）；"
                f"闲鱼侧要店家退款给买家（插件端暂无退款命令类型）")
            hint = {300010: "同场次同座位已有单在出票中（不重试）",
                    300004: "这个场次暂时没有供货商",
                    300005: "限价低于应付 —— 规则配错了",
                    200006: "座位刚被别人买走",
                    200007: "所选座位不符合选座规则",
                    300006: "票价刚变了"}.get(code, "下单没成功")
            # ★★★ 2026-10-14：按店主给的文案表接上 bubble 8
            #   （模板名「创建订单失败文案」，原文：
            #     「其中有一个位置价格不对哦，辛苦取消分别重新截图下单哈」）。
            #
            #   ★ 为什么这两类用 CREATE_FAILED 而不是通用 RECOG_FAIL：
            #     它的处置方式【不一样】—— 通用那条是"重新发截图"，
            #     这条是"【取消后分别重新截图】下单"：
            #     因为一张单里混了价格/状态不一致的座位，只能拆开重下。
            #     说错了买家会反复重发同一张图，永远下不成。
            #   · 200006 座位已售 / 200007 选座违规 → 座位本身有问题
            #   · 300005 实际价超限价 → 我们给的价与这批座位的实付对不上
            #     （正是"位置价格不对"的字面意思）
            #   ★ 其余（无供应商/票价变动/重复单）保持通用文案：
            #     它们不是"座位价格不对"，套 bubble 8 会误导买家去拆单。
            if code in (200006, 200007, 300005):
                out.add("CREATE_FAILED")
            else:
                out.add("RECOG_FAIL", {"fail_reason": f"{hint}，这单要人工处理"})
            return out

        # ---- 建单成功：把上游单号补进本地单 + 记 pending ----
        conv.pending_order = PendingOrder(
            order_no=created.order_no, out_order_no=out_order_no,
            chat_id=conv.chat_id, conversation=conv, show_id=result.show_id,
            cinema_name=result.cinema_name or "",
            movie_name=result.movie_name or "",
            hall_name=result.hall_name or "",
            start_time=result.start_time or "",
            seats=seats,
            ticket_mode=getattr(conv, "ticket_mode", TICKET_STANDARD),
            price_mode=quote.price_mode, quote_amount_fen=quote.total_price_fen,
            freeze_amount_fen=created.freeze_amount_fen)
        self.pending[created.order_no] = conv.pending_order
        out.order_no = created.order_no
        out.out_order_no = out_order_no
        # ★ 本地单补上上游单号与冻结额（原来落的是待付款记录）
        self._persist_order(conv, result, quote, seats, created, out_order_no)

        decision = conv.apply(Event.ORDER_PAID)
        out.state = decision.state
        out.event = Event.ORDER_PAID
        out.add("TICKETING", {"cinema": result.cinema_name or "",
                              "ticket_window": self._window(conv),
                              "pickup_link": created.pickup_url or "下单后自动生成"})
        if created.duplicated:
            out.notes.append("命中幂等：这是首次下单的结果，没有重复建单")
        return out

    def _persist_order(self, conv, result, quote, seats, created,
                       out_order_no: str) -> None:
        """
        把这一单落进订单库（订单管理页/出票后台的数据来源）。

        ★ 字段全部来自【已经确定下来的东西】：
            · 上游单号 created.order_no —— 回调/查详情都用它
            · 金额用 quote.total_price_fen —— 报价那一刻定下的
            · 状态 PAID：良票下单即扣款（freeze_amount_fen 是冻结额），
              所以下单成功就是"已付款、待出票"
            · 幂等键用 quote_id —— 同一笔报价只建一笔单

        ★★ created 可以为 None（2026-10-14 改）：
          店主定的时序是「金额校验正确 + 已付款，才允许良票建单」。
          所以买家一确认、还没付款、还没调 ch5_create 之前，
          我们只有一张【本地待付款单】没有任何上游结果。
          那种情况下用 order_id=out_order_no、state=AWAITING_PAYMENT、
          payment_state=UNPAID 先落下来（订单管理页要能看到他在等付款），
          等付款后真正建单时再补上游单号与冻结额。

        ★ 落库失败不能让买家收不到消息 —— 记 note 给人看，流程照走。
        """
        if self.order_store is None:
            return
        # ★ 清掉上一次的落库错误 —— 否则排查时看到的是陈旧原因，
        #   会误判"这次也没落上"。（踩过：调试输出里那条错误其实来自上一单。）
        self._order_error = ""
        try:
            created = created or None
            # ★ 没有上游单号时用我们自己的单号当 order_id ——
            #   它是这一单在【我们系统里】的身份，付款后建单成功再补
            #   provider_order_id（上游单号）。
            order_id = (getattr(created, "order_no", "") or out_order_no)
            # ★ 幂等：已存在就直接返回，不重复建
            if self.order_store.get(order_id) is not None:
                return
            # ★★★ 2026-10-14：付款后建单成功时，要把【确认时落的本地待付款单】
            #   【就地补全】，而不是再插一条。
            #
            #   踩过的坑（真发生，而且是静默的）：
            #     确认时落本地单，order_id=YMD-…，幂等键
            #       idempotency_key_for(quote_id) = v5-liangpiao-local-<quote_id>
            #     付款后建单成功，上游单号 LP-… 与本地单号【不同】，
            #     于是 order_id 变了、get(order_id) 查不到，
            #     于是走 create() → 撞上同一个幂等键 →
            #       ValueError: ORDER_IDEMPOTENT_REPLAY
            #     ★ 而这个 ValueError 被下面的 except 归到"存储问题"，
            #       只记 _order_error 就把落库跳过了：
            #         get('LP-…') = None        ← 上游单没落上
            #         _order_error = 'ORDER_IDEMPOTENT_REPLAY…'
            #     结果：订单库里只剩一张【待付款】的僵尸单，
            #     而那张单的钱早就收了、票也在出 ——
            #     订单管理页看不到它，出票失败回调也找不到它。
            #
            #   ★ 为什么按幂等键找：它就是"这一笔报价只建一笔单"的凭据。
            #     找到说明"本地单已经存在"，那就把上游信息补上去。
            _ikey = idempotency_key_for(
                getattr(conv, "quote_id", "") or out_order_no,
                provider="LIANGPIAO")
            if created is not None:
                _prev = self.order_store.find_by_idempotency(_ikey)
                if _prev is None:
                    _prev = self.order_store.find_by_quote(
                        getattr(conv, "quote_id", "") or "")
                if _prev is not None and _prev.order_id != order_id:
                    self._attach_upstream(self.order_store, _prev, created,
                                          out_order_no)
                    return
            seats_names = [s.get("seatNo") or s.get("seatName") or ""
                           for s in (seats or []) if isinstance(s, dict)]
            seats_names = [s for s in seats_names if s]
            start = getattr(result, "start_time", "") or ""
            paid = created is not None
            order = Order(
                order_id=order_id,
                quote_id=getattr(conv, "quote_id", "") or "",
                demand_id=getattr(conv, "demand_id", ""),
                demand_rev=getattr(conv, "demand_rev", 0),
                tenant_id=getattr(conv, "tenant_id", "") or "default",
                shop_id=getattr(conv, "shop_id", "") or "default",
                buyer_id=getattr(conv, "buyer_id", "") or conv.chat_id,
                chat_id=conv.chat_id,
                provider="LIANGPIAO",
                provider_order_id=str(getattr(created, "order_no", "") or ""),
                cinema=result.cinema_name or "",
                movie=result.movie_name or "",
                show=f"{start[:10]} {start[11:16]}".strip(),
                seats=seats_names,
                ticket_count=len(seats_names),
                amount_fen=quote.total_price_fen,
                # ★ 两种情形分开：
                #   created 有值 → 上游单已建、冻结完成 → 已付待出票
                #   created 为 None → 只是本地待付款记录（钱还没动）
                paid_fen=quote.total_price_fen if paid else 0,
                state="PAID" if paid else "AWAITING_PAYMENT",
                payment_state="CONFIRMED" if paid else "UNPAID",
                idempotency_key=idempotency_key_for(
                    getattr(conv, "quote_id", "") or out_order_no,
                    provider="LIANGPIAO"),
                payload={
                    "out_order_no": out_order_no,
                    "pickup_url": getattr(created, "pickup_url", "") or "",
                    "freeze_amount_fen": getattr(created, "freeze_amount_fen", 0),
                    "price_mode": quote.price_mode,
                    "ticket_mode": getattr(conv, "ticket_mode", ""),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    # ★ 时序标记：让订单管理页能分辨"上游单还没建"
                    "upstream_created": paid,
                })
            self.order_store.create(order)
        except Exception as exc:                          # noqa: BLE001
            # ★ 不抛：落库失败不该让买家收不到"已下单"的回复。
            #   但要把原因留下来 —— 订单管理页会显示"存储不可用"，
            #   这里再补一句具体原因，排查时才不用猜。
            #
            # ★★ 但【编程错误必须炸出来】，不能和"存储不可用"混为一谈：
            #    我在这里踩过 —— 写 _persist_wanda_order 时漏了 Order 的
            #    import，NameError 被这个 except 吞掉，
            #    表现是"单没落库，买家却收到已下单"，极难查。
            #    ⇒ NameError / TypeError / AttributeError 这类是【我写错了】，
            #      不是环境问题，必须让它冒出来（测试会当场抓到）。
            if isinstance(exc, (NameError, TypeError, AttributeError,
                                ImportError)):
                raise
            # ★★★ 幂等键被占用 —— 说明"这一笔报价已经有一张单了"，
            #   那就【就地补全】而不是把它当存储错误丢掉。
            #   这是上面那段预检的兜底：预检靠 find_by_idempotency /
            #   find_by_quote，万一对不上（老数据、字段缺失），
            #   create() 会在这里撞键 —— 那一刻我们仍然知道该更新谁。
            if "ORDER_IDEMPOTENT_REPLAY" in str(exc) and created is not None:
                _dup = None
                try:
                    _dup = self.order_store.find_by_idempotency(_ikey)
                    if _dup is None:
                        _dup = self.order_store.find_by_quote(
                            getattr(conv, "quote_id", "") or "")
                except Exception:                             # noqa: BLE001
                    _dup = None
                if _dup is not None:
                    self._attach_upstream(self.order_store, _dup, created,
                                          out_order_no)
                    self._order_error = ""
                    return
            self._order_error = f"{type(exc).__name__}: {exc}"

    def _attach_upstream(self, store, order, created, out_order_no: str) -> None:
        """
        把【上游单号与冻结额】补到已存在的本地单上。

        ★ 为什么单独一个方法：有两条路会走到这里 ——
          ① 落库前的幂等预检命中了本地待付款单
          ② create() 撞上 ORDER_IDEMPOTENT_REPLAY 的兜底
        两处都写一遍必然有一天改一处忘另一处。

        ★ 只动"上游信息 + 付款状态"，不动 order_id：
          order_id 是这张单在我们系统里的身份，改了会断掉所有已有引用；
          上游单号放 provider_order_id（回调/查详情都用它）。
        """
        order.provider_order_id = str(getattr(created, "order_no", "") or "")
        order.state = "PAID"
        order.payment_state = "CONFIRMED"
        order.paid_fen = order.paid_fen or order.amount_fen
        p = dict(order.payload or {})
        p["upstream_created"] = True
        p["out_order_no"] = out_order_no
        p["pickup_url"] = getattr(created, "pickup_url", "") or ""
        p["freeze_amount_fen"] = getattr(created, "freeze_amount_fen", 0)
        order.payload = p
        order.touch()
        store.put(order)

    def _persist_wanda_order(self, conv, result: RecognitionResult, quote,
                             seats: list, out_order_no: str) -> str:
        """
        万达单落库（★ 没有上游单号 —— 店家手动出票）。

        ★ 与良票 _persist_order 的差别：
            良票：provider_order_id = 良票返回的 order_no（回调要用它）
            万达：★ 没有上游单号。order_id 用我们自己的 out_order_no，
                  等店家出票时他可以选择把万达单号填进来
                  （POST /api/order/deliver 的 provider_order_id 字段）。

        ★★ payment_state 必须是 UNPAID，**不能**标 CONFIRMED。
          ══════════════════════════════════════════════════════════
          我第一版写成 CONFIRMED，理由是"店家填票码要求它是 CONFIRMED"。
          ★ 那是本末倒置：为了让下游放行，就把"收到钱"说成真的。
          正确答案在【鱼麦多】那边 —— 付款信号来自真实闲鱼订单：
              plugin 推 order.paid 事件 → plugin_runtime._from_order_paid
              （注释原文：「万达流程的关键一步：付款之后不能自动出票」）
          而且付款状态是【发货闸门】：
              plugin_runtime.queue_shipping_for_delivered
                  # ★ 未付款的不发（钱没到手就发货，钱收不回来）
                  if order.get("payment_state") != "CONFIRMED": continue
          ⇒ 这里标 UNPAID，等 order.paid 到了再置 CONFIRMED。
            在那之前店家就算填了票码也不会发货 —— 这正是我们要的保护。
        """
        order_id = out_order_no
        if self.order_store is None:
            return order_id
        try:
            start = getattr(result, "start_time", "") or ""
            seat_names = _seat_names_of(seats)
            order = Order(
                order_id=order_id,
                quote_id=getattr(conv, "quote_id", "") or "",
                demand_id=getattr(conv, "demand_id", ""),
                demand_rev=getattr(conv, "demand_rev", 0),
                tenant_id=getattr(conv, "tenant_id", "") or "default",
                shop_id=getattr(conv, "shop_id", "") or "default",
                buyer_id=getattr(conv, "buyer_id", "") or conv.chat_id,
                chat_id=conv.chat_id,
                provider="WANDA",
                # ★ 没有上游单号；店家出票时可以补 provider_order_id
                provider_order_id="",
                cinema=result.cinema_name or "",
                movie=result.movie_name or "",
                show=f"{start[:10]} {start[11:16]}".strip(),
                seats=seat_names,
                ticket_count=len(seat_names)
                or int(getattr(result, "expected_seat_count", 0) or 1),
                amount_fen=quote.total_price_fen,
                paid_fen=0,
                # ★ 我们下了单（待付款）
                state="AWAITING_PAYMENT",
                # ★★ 还没收到钱 —— 等鱼麦多 order.paid 事件
                payment_state="UNPAID",
                idempotency_key=idempotency_key_for(
                    getattr(conv, "quote_id", "") or out_order_no,
                    provider="WANDA"),
                payload={
                    "out_order_no": out_order_no,
                    "manual_ticketing": True,
                    "upstream_created": False,
                    "price_mode": quote.price_mode,
                    "quote_channel": "WANDA",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
            self.order_store.create(order)
        except Exception as exc:                          # noqa: BLE001
            # ★ 不抛：落库失败不该让买家收不到"已下单"的回复
            self._order_error = f"{type(exc).__name__}: {exc}"
        return order_id

    # ================================================================ 4.5 须知
    @by_conversation
    def purchase_notice(self, conv: Conversation) -> FlowResult:
        """
        发购票须知（风控话术，locked）。

        ★ 必须在买家付款前发。文案第②条含出票时效，所以同样要传
          {ticket_window} —— 否则就绕开了档位口径，又变成写死。
        """
        out = FlowResult(channel=getattr(conv, "channel", None))
        out.add("PURCHASE_NOTICE", {"ticket_window": self._window(conv)})
        return out

    # ================================================================ 4.6 退票
    @by_conversation
    def on_refund_request(self, conv: Conversation) -> FlowResult:
        """
        买家要求退票 → 提交申请。

        ★ 文档三条失败码要分开处置：
            500001 供应商不支持退票  → 告知不能退，票仍有效
            500002 已过可退时间      → 告知已过时间
            500003 退票申请重复      → 告知已提交过，不要重复申请
        ★ 提交成功【不等于】退票成功 —— 结果等回调。
        """
        out = FlowResult(channel=getattr(conv, "channel", None))
        pending = getattr(conv, "pending_order", None)
        if pending is None or not pending.order_no:
            out.ok = False
            out.add("NO_QUOTE")
            out.notes.append("没有在途订单，无法退票")
            return out

        # ★★ 先查 `refundable` 再提交。
        #   文档：这个字段就是「当前是否可发起主动退票」。
        #   早先解析了却从来不用 —— 结果就是对着一个不能退的单去提交，
        #   拿回 500001/500002，再翻译成"这家供应商不支持退票"。
        #   绕了一圈，而且可能把"已过可退时间"说成"供应商不支持"。
        #   ⇒ 有权威字段就先用它，别用错误码反推。
        try:
            detail = self.api.ch5_order_detail(pending.order_no)
            if detail is not None and detail.refundable is False:
                out.ok = False
                out.add("REFUND_NOT_ALLOWED")
                out.notes.append(
                    f"订单详情 refundable=false（当前不可发起主动退票）"
                    f"—— 直接告知买家，不去提交必被拒的申请")
                return out
        except LiangPiaoError as exc:
            # ★ 查详情失败【不阻断】退票：宁可试一次，
            #   也不要因为一次查询失败就不给买家退。
            #   ★ catch 的是基类 LiangPiaoError，不是 BusinessError ——
            #     100005（服务内部错误）抛的是 RetryableError，两者是兄弟不是父子。
            #     早先只 catch BusinessError，结果一次查询失败直接把整条链路抛穿。
            code = getattr(exc, "code", "?")
            out.notes.append(f"查退票资格失败（{code}），继续按正常流程提交")

        try:
            data = self.api.ch5_refund(pending.order_no)
        except LiangPiaoError as exc:
            # ★ 同样 catch 基类：500001/500002/500003 是 BusinessError，
            #   但限流(120001)、内部错误(100005) 是别的子类 ——
            #   只 catch BusinessError 会让那些错误直接抛穿到 HTTP 层。
            out.ok = False
            out.error = str(exc)
            code = getattr(exc, "code", 0)
            reason = {
                500001: "这家供应商不支持退票",
                500002: "已经过了可退票的时间",
                500003: "退票申请已经提交过了，不用重复申请",
                120001: "现在问的人有点多，稍等一下再试",
            }.get(code, "退票申请没能提交")
            out.add("RECOG_FAIL", {"fail_reason": reason})
            out.notes.append(f"退票申请被拒 code={code}")
            return out

        decision = conv.apply(Event.REFUND_APPLIED)
        out.state = decision.state
        out.event = Event.REFUND_APPLIED
        out.order_no = pending.order_no
        out.add("REFUND_APPLIED")
        out.notes.append(f"退票单号：{(data or {}).get('refundNo')}")

        # ★★ 2026-10-14 接线：「设置 → 订单自动拦截 → 开关 / 话术」
        #   ★ 语义：买家申请退款时【顺手把出票拦下来】——
        #     票还没出就把单拦掉，比"出了票再退"省事得多
        #     （退款要手续费、买家还要等）。
        #   ★ 只在【票还没出】时拦（走这条路说明本来就没出 ——
        #     出票成功走的是另一条回调）。所以这里不用再判状态。
        #   ★ 默认 enabled=False ⇒ 默认行为一个字不变。
        try:
            import settings as _st2
            _ai = _st2.load().order_auto_intercept
            if bool(getattr(_ai, "enabled", False)):
                _msg = str(getattr(_ai, "message", "") or "").strip()
                if _msg:
                    out.messages.append(_msg)
                    out.template_keys.append("ORDER_AUTO_INTERCEPT")
                _online = str(getattr(pending, "provider_order_id", "") or "")
                if _online:
                    from integrations.yumaiduo.commands import Command as _Cmd2
                    import uuid as _uuid2
                    _ident2 = {
                        "tenant_id": str(getattr(pending, "tenant_id", "") or ""),
                        "shop_id": str(getattr(pending, "shop_id", "") or ""),
                        "buyer_id": str(getattr(pending, "buyer_id", "") or ""),
                        "chat_id": str(conv.chat_id or ""),
                    }
                    out.pending_commands.append(_Cmd2(
                        command_id=str(_uuid2.uuid4()),
                        type="CLOSE_FAILED_ORDER",
                        event_id=str(pending.order_no or ""),
                        session_id=_ident2["chat_id"],
                        text="买家申请退款，自动拦截出票",
                        # ★ 幂等：同一张单只拦一次
                        dedupe_key=f"auto_intercept:{_online}",
                        extra={"order_id": _online,
                               "booking_order_id": str(pending.order_no or ""),
                               "side_effect": "AUTO_INTERCEPT_ON_REFUND"},
                        **_ident2))
                    mark_intercept_attempted(pending)
                    out.notes.append(
                        f"自动拦截：已排关单命令（平台单号 {_online}）")
                else:
                    out.notes.append(
                        "★ 自动拦截：没有平台订单号 ⇒ 没发关单命令")
        except Exception as _exc:                             # noqa: BLE001
            # ★ 拦截发不出去不该影响"退票已受理"这个结论
            out.notes.append(f"★ 自动拦截没排上（不影响退票受理）：{_exc}")
        return out

    @staticmethod
    def _window(conv: Conversation) -> str:
        """取当前会话购入档位对应的出票时效文案。"""
        if getattr(conv, "quote_channel", "") == "WANDA":
            return "以店家实际出票进度为准"
        mode = getattr(conv, "ticket_mode", TICKET_STANDARD) or TICKET_STANDARD
        return tpl.TICKET_WINDOW.get(mode, tpl.TICKET_WINDOW["STANDARD"])

    # ================================================================ 5 回调
    def on_callback(self, raw_body: str, headers: dict, *,
                    app_key: str, app_secret: str, now: int | None = None,
                    seen_nonces: set | None = None,
                    seen_events: set | None = None
                    ) -> tuple[str, FlowResult | None]:
        """
        处理良票回调。返回 (应答体, 编排结果或 None)。

        ★ 应答体必须原样返回给平台 —— 平台只看响应体、不看 HTTP 状态码。
        """
        event, ack = cb.handle(app_key, app_secret, headers, raw_body,
                               now=now, seen_nonces=seen_nonces,
                               seen_events=seen_events)
        if event is None:
            return ack, None

        # ★ 回调这条路上没有 conv 变量 —— 会话要从 pending 里取。
        #   （批量给 FlowResult 加 channel 时在这里踩过：NameError: conv）
        #   取不到就是 None（走全局文案），不猜。
        _pending = self.pending.get(event.order_no)
        out = FlowResult(channel=getattr(
            getattr(_pending, "conversation", None), "channel", None))
        handler = {
            cb.EVENT_TICKETED: self._on_ticketed,
            cb.EVENT_TICKET_UPDATED: self._on_ticketed,
            cb.EVENT_FAILED: self._on_failed,
            cb.EVENT_SETTLED: self._on_settled,
            cb.EVENT_REFUNDED: self._on_refunded,
            cb.EVENT_REFUND_REJECTED: self._on_refund_rejected,
            cb.EVENT_RECOGNITION_FINISHED: self._on_recognition_finished,
        }.get(event.event)
        if handler is None:
            out.notes.append(f"未处理的事件：{event.event}")
            return ack, out
        from .shop_scope import scope
        conv = getattr(_pending, 'conversation', None)
        with scope(getattr(conv, 'tenant_id', ''), getattr(conv, 'shop_id', '')):
            return ack, handler(event, out)

    def _on_ticketed(self, event: cb.CallbackEvent, out: FlowResult) -> FlowResult:
        """
        出票成功 / 票根变更。

        ★ 票码有四种形态，且 fields 可能为 null —— 必须兼容：
            一座一码 / 两座两码 / 一码多座（逗号拼接）/ 三座多码（seatNo 空串）
          入场座位只看 actualSeats。
        ★ tickets 为空（version=0）说明票根还没取到，等 ticket.updated，别当失败。
        """
        pending = self.pending.get(event.order_no)

        if not event.tickets:
            out.notes.append("出票成功但票根为空（version=0），等 ticket.updated 再发取票码")
            out.need_human = False
            return out

        codes = event.ticket_codes
        seats_text = "，".join(event.actual_seats or []) or \
            "，".join(s.get("seatNo") or "" for s in pending.seats) if pending else ""

        if event.seat_changed is True:
            out.notes.append("★ 已换座 —— 必须明确告诉买家实际座位，不能装作没变")
        elif event.seat_changed is None:
            out.notes.append("★ 换座状态未知（null）—— 不能当成未换座，"
                             "取票码消息不要断言座位号")

        variables = {
            "ticket_code": "，".join(codes),
            "ticket_link": event.data.get("ticketLink") or
                           (pending.hall_name if pending else "") or "",
            "cinema": pending.cinema_name if pending else "",
            "movie": pending.movie_name if pending else "",
            "seats": seats_text,
        }
        # bubble 10 三条：票码 + 求关注 + 取票图
        out.add("TICKET_CODE", variables)
        out.event = Event.TICKET_OK

        # ★★ 2026-10-14 接线：「设置 → 取票图模板 → 取票图模板开关」
        #   出票成功后【再发一条带图的】—— 和票码那条并存，不是替换。
        #   ★ 默认 enabled=False / 没配图 ⇒ image_for_send() 返回 None ⇒
        #     什么都不做，默认行为一个字不变。
        #   ★ 用 SEND_MESSAGE + image：插件的 sendReplyMessage 见到 image
        #     就发图、没有就发文字 ⇒ 同一条命令复用，不用新增命令类型。
        try:
            # ★ 用 ensure_uploaded：没缓存过 URL 就先上传一次。
            #   传不上去就返回 None ⇒ 不发图，但【不影响出票结果】。
            # ★★ 密钥不在这里取 —— ticket_flow 不许 import plugin_api
            #   （那会成环，见 FlowResult.pending_commands 的注释）。
            #   密钥由 ticket_image 自己从【注入的提供者】拿，
            #   注入发生在 plugin_runtime（最外层）。
            _img = ticket_image.ensure_uploaded(
                {"shop_id": getattr(pending, "shop_id", "") or ""})
        except Exception:                                 # noqa: BLE001
            _img = None
        if _img and pending is not None:
            try:
                from integrations.yumaiduo.commands import Command as _Cmd
                import uuid as _uuid
                _ident = {
                    "tenant_id": getattr(pending, "tenant_id", "") or "",
                    "shop_id": getattr(pending, "shop_id", "") or "",
                    "buyer_id": getattr(pending, "buyer_id", "") or "",
                    "chat_id": getattr(pending, "chat_id", "") or "",
                }
                if _ident["chat_id"]:
                    _c = _Cmd(
                        command_id=str(_uuid.uuid4()), type="SEND_MESSAGE",
                        event_id=str(event.order_no or ""),
                        session_id=_ident["chat_id"], text="",
                        image=_img,
                        # ★ 幂等：同一单只发一次取票图
                        dedupe_key=f"ticket_image:{event.order_no or ''}",
                        extra={"order_id": str(event.order_no or ""),
                               "history_message_ids": []},
                        **_ident)
                    # ★★ 挂到结果上，不在这里排队（领域层不碰队列）——
                    #   外层（plugin_runtime）拿到 pending_commands 后统一入队。
                    out.pending_commands.append(_c)
                    out.notes.append("取票图已排队（出票成功后附一张示意图）")
            except Exception as _exc:                     # noqa: BLE001
                # ★ 取票图发不出去【不该让出票结果失败】—— 票码才是关键
                out.notes.append(f"★ 取票图排队失败（不影响出票）：{_exc}")

        # ★ 回调必须把状态机往前推 —— 否则会话永远停在「待出票」，
        #   后面的流程（退票、二次购买）都没法走。
        if pending and pending.conversation is not None:
            decision = pending.conversation.apply(Event.TICKET_OK)
            out.state = decision.state
        out.order_no = event.order_no
        return out

    def _on_failed(self, event: cb.CallbackEvent, out: FlowResult) -> FlowResult:
        """
        出票失败 / 出票前取消 / 下单未受理 —— 三种情况走同一个事件。

        ★ 判据：**没有 failCode = 出票前取消**（文档：取消场景无此字段）。
        ★ 不要假定失败即全退：部分上游关单有损，以 lossAmount / refundAmount 对账。
        """
        out.order_no = event.order_no
        pending = self.pending.get(event.order_no)

        if event.is_cancelled:
            out.notes.append("出票前取消：冻结全额解冻")
        else:
            out.notes.append(
                f"出票失败 failCode={event.fail_code} "
                f"原因={event.data.get('failReason')} "
                f"损失={event.loss_amount_fen}分 退回={event.refund_amount_fen}分")
            if not event.is_full_refund:
                out.notes.append("★ 有损关单：不是全额退回，对账要用 lossAmount/refundAmount")

        out.event = Event.TICKET_FAIL

        # ★★ 2026-10-14 接线：「设置 → 订单退款 → 自动取消/同意退款」
        #   hint 说得很清楚："勾选的情况会自动退款，未勾的需要人工处理"。
        #   ★ 退款本身是良票平台自动做的（回调里带 refundAmount/lossAmount），
        #     我们拦不住 ⇒ 这个开关真正决定的是【说什么 + 要不要转人工】：
        #       勾了「出票失败」 ⇒ 照旧发那句话（是事实）
        #       没勾            ⇒ ① 不替平台承诺退款
        #                         ② 明确标"需人工处理"
        #                           （否则买家以为系统会退钱，等着）
        #   ★ 默认勾着「出票失败」⇒ 默认行为一个字不变。
        _refund_kind = None
        if not event.is_cancelled:
            _refund_kind = "出票失败"
        try:
            import settings as _st
            _auto_on = list(_st.load().order_refund.auto_refund_on or [])
        except Exception:                                 # noqa: BLE001
            _auto_on = ["出票失败"]        # 读不到就按默认（最保守：只认出票失败）
        # ★ 没有依据判定是哪一种 ⇒ 按人工处理，绝不瞎猜成"出票失败"
        #   （猜错就等于替平台承诺退款）
        if _refund_kind is None or _refund_kind in _auto_on:
            # bubble 9：出票失败自动退款
            out.add("TICKET_FAILED_REFUND")
        else:
            out.notes.append(
                f"★ 「{_refund_kind}」没勾自动退款 ⇒ 按人工处理"
                f"（不替平台承诺退款，已标记待人工）")
            out.need_human = True
        if pending:
            if pending.conversation is not None:
                decision = pending.conversation.apply(Event.TICKET_FAIL)
                out.state = decision.state
            self.pending.pop(event.order_no, None)

        # ★★★ 2026-10-14：出票失败必须【落到订单库】——原来完全没写。
        #
        #   原来这里只记 note + 发一句给买家，订单库里那张单还停在
        #   PAID / payment_state=CONFIRMED（"待出票"）：
        #     · 订单管理页永远显示它在等出票 —— 可它已经失败退钱了
        #     · 分桶计数里它算"待出票"，永远归不了"需关注"
        #     · 有损关单的损失（lossAmount）只在一句 note 里，对不上账
        #
        #   ★ 店主给的场景就是这条：「座位价格不一致，良票会返回出票失败」——
        #     这种单会真的发生，而且买家是【已付款】的，账必须清。
        #
        #   ★ 判据（不猜）：
        #     · is_cancelled（回调里没有 failCode）→ CANCELLED（出票前取消，全额解冻）
        #     · 否则 → FAILED（出票失败）；有损时把 loss/refund 记进 payload
        _st = "CANCELLED" if event.is_cancelled else "FAILED"
        try:
            from . import ledger as _ledger
            _os = _ledger.stores().orders
            if _os is not None:
                _o = _os.get(event.order_no)
                if _o is None:
                    # ★ 也可能是"本地单号 != 上游单号"的情况：上游单号是
                    #   付款后建单时才知道的，而本地单先落（order_id=out_order_no）。
                    #   那就按 provider_order_id 找一次。
                    _o = _os.find_by_provider_order(event.order_no)
                if _o is not None:
                    _o.state = _st
                    _p = dict(_o.payload or {})
                    _p["ticket_failed"] = {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "fail_code": event.fail_code,
                        "fail_reason": event.data.get("failReason"),
                        "loss_fen": event.loss_amount_fen,
                        "refund_fen": event.refund_amount_fen,
                        "full_refund": event.is_full_refund,
                        "cancelled": event.is_cancelled,
                    }
                    _o.payload = _p
                    # ★ 钱已经退给买家了 —— 付款状态不能还留在 CONFIRMED，
                    #   否则 queue_shipping_for_delivered 之类的后续逻辑
                    #   还会把这张单当"付过款的"处理。
                    if event.is_full_refund:
                        _o.payment_state = "REFUNDED"
                    _o.touch()
                    _os.put(_o)
                    out.notes.append(
                        f"★ 订单库已更新：{_o.order_id} → {_st}"
                        f"（payment_state={_o.payment_state}）")
                else:
                    out.notes.append("★ 订单库里找不到这张单，出票失败没落上（人工核）")
        except Exception as exc:                              # noqa: BLE001
            # ★ 不让落库失败吃掉给买家的回复；但要留痕
            out.notes.append(f"★ 出票失败落库异常：{type(exc).__name__}: {exc}")
        return out

    def _on_settled(self, event: cb.CallbackEvent, out: FlowResult) -> FlowResult:
        """
        放映结束完成实扣。

        ★ 对账以 freezeAmount / settleAmount / refundAmount 为准。
        ★★ `settleAmount` 可能为 null（结算前）。
          早先把它当 int（null→0），差额会算成"退回全部冻结"—— 假账。
          现在 null 就明确说"金额未到"，不编一个数出来。
        """
        out.order_no = event.order_no
        freeze = event.freeze_amount_fen
        settle = event.settle_amount_fen
        refund = event.refund_amount_fen

        if settle is None:
            # ★ 不猜：结算事件到了但实扣金额未到，如实记录，等下次
            out.notes.append(
                f"结算：冻结 {freeze} 分，但 settleAmount 为 null（金额未到）——"
                f"不计算差额，避免把 0 当成实扣")
            return out

        margin = freeze - settle
        out.notes.append(
            f"结算：冻结 {freeze} 实扣 {settle} 退回 {refund}（差 {margin}）")
        est = event.estimated_settle_fen
        if est is not None and est != settle:
            out.notes.append(
                f"★ 预估成本 {est} 与实扣 {settle} 不一致（差 {settle - est}），"
                f"对账要看这个差")
        return out

    def _on_refunded(self, event: cb.CallbackEvent, out: FlowResult) -> FlowResult:
        """退票完成。★ 金额以回调为准，有手续费要如实说。"""
        out.order_no = event.order_no
        fee = cb._to_int(event.data.get("feeAmount"))
        refund = cb._to_int(event.data.get("refundAmount"))
        out.event = Event.REFUND_SUCCEEDED
        pending = self.pending.get(event.order_no)
        # ★ 回调要把状态机推到「已退票」
        if pending and pending.conversation is not None:
            decision = pending.conversation.apply(Event.REFUND_SUCCEEDED)
            out.state = decision.state
        out.add("REFUND_DONE", {
            "fee_amount": _fen_to_yuan(fee),
            "refund_amount": _fen_to_yuan(refund),
        })
        out.notes.append(f"退票完成：手续费 {fee} 分 退还 {refund} 分")
        if pending:
            self.pending.pop(event.order_no, None)
        return out

    def _on_refund_rejected(self, event: cb.CallbackEvent, out: FlowResult) -> FlowResult:
        """
        退票未成。

        ★★ 文档：**订单回到 TICKETED，凭证依然有效**，冻结资金等散场实扣。
        所以必须明确告诉买家【票还能用】——
        否则买家以为退了就不去看了，那是实打实的损失。
        """
        out.order_no = event.order_no
        out.event = Event.REFUND_REJECTED
        pending = self.pending.get(event.order_no)
        # ★ 状态要回到【已交付】，不是停在退票中 —— 票还在买家手里，
        #   之后买家还要取票、还要二次购买，都从这个状态往下走。
        if pending and pending.conversation is not None:
            decision = pending.conversation.apply(Event.REFUND_REJECTED)
            out.state = decision.state
        out.add("REFUND_REJECTED", {
            "fail_reason": str(event.data.get("failReason") or "供应商未通过"),
        })
        out.notes.append(
            f"退票未成：{event.data.get('failReason')} —— 订单回到已出票，凭证仍有效")

        # ★★ 2026-10-14 接线：「设置 → 订单发货 → 强制发货开关 / 强制发货回复」
        #   ★ 语义（从 force_ship_reply 的全文读出来的，不是我猜的）：
        #       "收到您的退款申请后，我们第一时间为您提交了系统拦截。
        #        但由于特惠购票系统是全自动出票，在拦截结果出来前，
        #        影院系统已经成功出票。小店已为您转为正常发货，请您按时观影。"
        #     ⇒ 这个开关管的是【拦截失败但票已经出来了】这种情况：
        #       拦截没拦成、票却在，那就转成正常发货，别让买家的钱悬着。
        #   ★ 为什么接在这里而不是发货闸门：
        #     queue_shipping_for_delivered 的注释写明
        #     "这里【不读】force_ship —— 它的语义是退款拦截失败后仍转正常发货"。
        #     我早先把它接到发货闸门上，破坏过"未付款不发"，已撤回。
        #   ★ 默认 True ⇒ 默认行为就是"转正常发货"。
        #   ★★ 判据：这一单我们【确实提交过拦截】才用这个口径。
        #     没有拦截记录却发"拦截没成功"是编造事实。
        try:
            import settings as _st3
            _sh = _st3.load().order_ship
            if bool(getattr(_sh, "force_ship", False)) and _intercept_attempted(pending):
                _fmsg = str(getattr(_sh, "force_ship_reply", "") or "").strip()
                if _fmsg:
                    # ★★ 用 force_ship_reply 【替换】掉默认那句，不是追加。
                    #   为什么：默认那句说的是"退票未成、您的票依然有效"，
                    #   这句话说的是"拦截没成、已转正常发货"——
                    #   两句对同一件事给两个口径，买家会懵。
                    #   （我第一版过滤条件写的是"含『凭证』"，
                    #     而实际那句里没有"凭证"两个字 ⇒ 没摘掉 ⇒
                    #     两条都发出去，读起来像两件不同的事。）
                    _kept = [m for m in out.messages
                             if "退票没能成功" not in m]
                    out.messages = _kept + [_fmsg]
                    out.template_keys.append("FORCE_SHIP_REPLY")
                    out.notes.append(
                        "★ 强制发货：拦截没成但票已出 ⇒ 转正常发货（force_ship=开）"
                        "；默认那句「退票未成」已替换掉，避免两个口径打架")
                if pending is not None:
                    _ol = str(getattr(pending, "provider_order_id", "") or "")
                    if _ol:
                        from integrations.yumaiduo.commands import Command as _Cmd3
                        import uuid as _uuid3
                        _ident3 = {
                            "tenant_id": str(getattr(pending, "tenant_id", "") or ""),
                            "shop_id": str(getattr(pending, "shop_id", "") or ""),
                            "buyer_id": str(getattr(pending, "buyer_id", "") or ""),
                            "chat_id": str(getattr(pending, "chat_id", "") or ""),
                        }
                        out.pending_commands.append(_Cmd3(
                            command_id=str(_uuid3.uuid4()), type="SHIP_ORDER",
                            event_id=str(getattr(pending, "order_no", "") or ""),
                            session_id=_ident3["chat_id"],
                            text="",
                            dedupe_key=f"force_ship:{_ol}",
                            extra={"order_id": _ol},
                            **_ident3))
                        out.notes.append("强制发货：已排 SHIP_ORDER 命令")
        except Exception as _exc:                             # noqa: BLE001
            # ★ 强制发货排不上不该影响"退票未成"这个结论
            out.notes.append(f"★ 强制发货没排上（不影响退票结论）：{_exc}")
        return out

    def _on_recognition_finished(self, event: cb.CallbackEvent,
                                 out: FlowResult) -> FlowResult:
        """异步识别完成。"""
        status = event.data.get("status")
        if status == "FAILED":
            out.ok = False
            out.error = str(event.data.get("errorMsg") or "")
            out.notes.append(f"异步识别失败 code={event.data.get('errorCode')}")
            return out
        result = RecognitionResult.from_data(event.data)
        out.notes.append(f"异步识别完成：matchLevel={result.match_level}")
        return out

    # ================================================================ 工具
    def _normalize_quote_seats(self, conv: Conversation,
                               result: RecognitionResult) -> None:
        """
        把识别出的座位存到会话上，下一次候选回复要用。

        ★ 注意分清两个座位概念：
            result.seats        识别/确认后的座位（含 areaId）
            seat[].seatPriceFen 【截图上顾客看到的售价】，不是我们的应付价
        """
        conv.last_candidates = result.candidates or {}
        conv.last_candidates_text = _format_candidates(
            result, (result.pending_dimensions() or ["show"])[0])


# ==================================================================== 工具函数
def _recog_fail_reason(code: int) -> str:
    """把识别错误码翻成给买家看的话。

    ★ 文案依据：良票文档 §3 错误码表的「处理建议」列，不是我编的。
      ★ 为什么不给 700004/700005/700009 写"技术原因"：
        买家不懂"模型服务异常"是什么意思，他只需要知道【现在我该做什么】。
        所以文案一律是动作指令（重试 / 换图 / 挑候选），不是错误描述。
    """
    return {
        700002: "图片没能读取到，麻烦重新发一下截图",
        700003: "识别超时了，麻烦再发一次",
        # ★ 700004「模型服务异常」/ 700005「识别结果无法解析」
        #   文档处理建议都是「重试」。而且这两种都是【平台侧的临时故障】，
        #   不是买家的图有问题 —— 所以不能说"您的截图不对"，
        #   否则买家会白白重拍好几遍。
        700004: "识别服务刚才有点忙，麻烦再发一次截图",
        700005: "这次没能读出来，麻烦重新发一次截图",
        700006: "这张图好像不是选座页截图",
        700008: "识别记录已过期，请重新发截图",
        # ★ 700009「所选候选无效」文档原文：
        #   「只接受本次识别返回过的候选」「拿平台没给过的 ID 会得到 700009
        #     （响应里会列出可选值）」
        #   ⇒ 让买家【重新截图】是过重的（他发的图没错，是选项对不上了）。
        #     正确动作是"重新发一次，我给您重新列一遍选项"。
        700009: "刚那个选项对不上了，麻烦重新发一次截图，我给您重新列一遍",
    }.get(code, "没能识别出座位信息")


def _parse_pick(raw: str, *, keyword_cinema: str = "",
                keyword_movie: str = "", keyword_show: str = "",
                ) -> tuple[str | None, int]:
    """
    解析买家的候选回复。

    ★ 两种写法都认：
        「影院1」「影片2」「场次3」← 带前缀（原设计，兼容老买家习惯）
        「1」「2」「3」            ← ★ 2026-10-13 店主定的：只回数字
    带前缀时能直接判断维度；纯数字时【维度未知】——
    调用方（on_candidate_pick）要用"我们正在问哪个维度"补上。
    ★ 为什么纯数字不在这里定维度：
      这个函数是纯解析、无上下文。service.py 的路由判据是
      `kind is not None`，若这里对"1"也返回非 None，
      买家在任何状态下发"1"都会被当成选座。
      只有 on_candidate_pick 才知道在问哪个维度。

    ★★ 2026-10-14 接线：关键字现在真的可配了
      （设置 → 识别报价 → 影院/影片/场次关键字）。
      ★ 这段 docstring 一直写着"关键字本身也是可配的"，但代码是写死的 ——
        承诺了没做，比没承诺更误导。现在补上。
      ★ 三个参数都可选，默认空 = 用写死的那套 ⇒ 行为与接线前完全一致
        （所有单测都不用改）。
      ★ 配置值【追加】在别名后面，不是替换：
        店主把关键字配成"影院"，但买家历史上会说"影城1"/"电影1" ——
        替换掉别名就会把老买家的习惯用语弄坏。
      ★ 保持纯函数：不 import settings、不读盘（读配置是调用方的事）。
    """
    text = (raw or "").strip()
    # ★ 别名在前（老习惯优先），配置值在后（可覆盖新增叫法）
    cinema_words = tuple(w for w in ("影院", "影城", keyword_cinema) if w)
    movie_words = tuple(w for w in ("影片", "电影", keyword_movie) if w)
    show_words = tuple(w for w in ("场次", keyword_show) if w)
    for kind, words in (("cinema", cinema_words),
                        ("movie", movie_words),
                        ("show", show_words)):
        for word in words:
            if text.startswith(word):
                tail = text[len(word):].strip()
                # 支持阿拉伯数字和全角
                digits = "".join(ch for ch in tail if ch.isdigit())
                if digits:
                    return kind, int(digits)
    # ★ 纯数字：维度交给调用方按上下文补
    if text.isdigit():
        return None, int(text)
    return None, 0


def _format_candidates(result: RecognitionResult, kind: str) -> str:
    """
    把候选列表格式化成 {二次确认信息}（bubble 40 用）。

    ★ 文档提示：candidates.shows[].hall 是【该候选场次的厅名】，
      键名是 hall 不是 hallName；顶层 hallName 仅 EXACT 有值。
    ★ hasShow=true 的门店排前面优先展示。

    ★★★ 编号格式：2026-10-13 店主定的 —— 只写数字，不带「影院/影片/场次」前缀。
      理由：每次只问【一个】维度（见 _ask_candidate），所以消息本身已经
      限定了是哪一类；再加前缀只是让买家多打字。
      ⇒ 列表是「1. 甲影城（天津）」，买家回「1」即可。
      ★ 兼容性：带前缀的「影院1」仍然认（见 _parse_pick），
        老买家习惯了也不会报错。
    """
    if kind == "cinema":
        items = sorted(result.candidates.get("cinemas") or [],
                       key=lambda c: (not c.get("hasShow"), -(c.get("score") or 0)))
        # ★★ 2026-10-14 对齐线上固定话术：把【城市】放到影院名前，用「 · 」分隔。
        #   线上原文（live-triage.json 的 system_template 里那种）：
        #       1. 上海 · 万达影城（临港万达广场IMAX店）
        #       2. 桂林 · 万达影城（临桂万达广场IMAX店）
        #   ★ 为什么城市要放前面：候选可能跨城市
        #     （实测有「成都·横店 / 广州·横店」这种），
        #     城市放前面一眼扫过去就能筛掉不对的；
        #     我原来写成「影院名（城市）」在括号里，多个括号叠一起很难读。
        #   ★ 仍然【不带】「影院1」那种文字前缀 —— 那是店主 2026-10-13 定掉的：
        #     每次只问一个维度，消息已经限定了是哪一类，不必让买家多打字。
        lines = [f"{i}. {c.get('cityName') or ''} · {c.get('name')}"
                 if (c.get("cityName") or "").strip()
                 else f"{i}. {c.get('name')}"
                 for i, c in enumerate(items, 1)]
    elif kind == "movie":
        items = result.candidates.get("movies") or []
        lines = [f"{i}. {m.get('name')}" for i, m in enumerate(items, 1)]
    else:
        items = result.candidates.get("shows") or []
        lines = []
        for i, s in enumerate(items, 1):
            hall = s.get("hall") or ""
            dim = s.get("dimension") or ""
            lang = s.get("language") or ""
            lines.append(f"{i}. {hall}（{s.get('startTime')}）（{dim} {lang}）")
    return "\n".join(lines)


def _buyer_self_buy_total(result: RecognitionResult, seat_count: int) -> int | None:
    """Actual screenshot payable price, distinct from the cinema list price."""
    if (not result.seats or len(result.seats) != seat_count
            or not result.seat_matched or result.price_mismatch or result.seat_confirm_required
            or (result.raw_results or {}).get('buyer_text_details')):
        return None
    try:
        total = int(result.price_all_fen or 0)
        if total > 0:
            return total
        prices = [int(row.get('seatPriceFen') or 0) for row in result.seats]
        return sum(prices) if prices and all(price > 0 for price in prices) else None
    except (TypeError, ValueError, AttributeError):
        return None


def _seats_original_fen(result: RecognitionResult) -> int | None:
    """
    已选座位的市场原价合计（分）。

    ★ 文档明确：**已选座位的市场原价看 seat[].originalPriceFen**，
      而 prices[].originalPrice 是【场次最低区起价】，不是已选座位原价。
      分区厅里已选座位可能比它贵。
    """
    total = 0
    any_value = False
    for seat in result.seats or []:
        value = seat.get("originalPriceFen")
        if value not in (None, ""):
            any_value = True
            total += int(value)
    return total if any_value else None


def _far_enough(start_time: str | None, limit_minutes: int,
                now: datetime | None = None) -> bool:
    """距离开场是否还有足够时间。时间缺失时不拦（交给平台判）。"""
    if not start_time:
        return True
    try:
        start = datetime.fromisoformat(start_time)
    except ValueError:
        return True
    current = now or datetime.now(start.tzinfo)
    return (start - current).total_seconds() / 60 >= limit_minutes


def _price_mismatch_continue_enabled(cfg=None) -> bool:
    """Whether recognition price discrepancies may continue to live quote.

    This setting only removes the recognition-level early stop.  The actual
    channel preflight and all seat/count/route guards remain authoritative.
    """
    try:
        if cfg is None:
            import settings as _st
            cfg = _st.load()
        mismatch = getattr(cfg, "price_mismatch", None)
        value = getattr(mismatch, "mode", None)
        if value is None:  # 兼容未经过 Settings.apply_dict 的旧测试/调用方
            value = getattr(cfg.recognize_quote, "price_mismatch_mode", "")
        return str(value) == "按识别成功处理"
    except Exception:
        return False


def _price_mismatch_message(cfg=None) -> str:
    try:
        if cfg is None:
            import settings as _st
            cfg = _st.load()
        mismatch = getattr(cfg, "price_mismatch", None)
        value = getattr(mismatch, "message", None)
        if value is None:  # 兼容旧存档和直接构造的旧配置对象
            value = getattr(cfg.recognize_quote, "price_mismatch_message", "")
        value = str(value or "").strip()
        return value or "亲，识别到座位价格与截图总价不一致，请重新选座后再发送截图~"
    except Exception:
        return "亲，识别到座位价格与截图总价不一致，请重新选座后再发送截图~"


def _fallback_quote_kwargs(cfg=None) -> dict:
    """
    兜底报价的三个设置 → 传给 QuoteRuleEngine.quote 的参数。

    ★ 语义（按字段直读）：
        fallback_quote.mode         预估限价 → LIMIT；一口价 → FIXED
        fallback_quote.price_capped 启用限价 → 给 maxPrice；不启用 → None
    ★ 读不到配置 ⇒ 返回现有写死的那套（预估 + 限价）——
      不能让一个读配置失败把报价搞成"钱上不封顶"。
    """
    try:
        if cfg is None:
            import settings as _st
            cfg = _st.load()
        fb = cfg.fallback_quote
        mode = str(getattr(fb, "mode", "") or "")
        # ★★ 2026-10-14 修：price_capped 是【布尔】，不是显示文本。
        #   前端 radio 的 values=[True, False]，回来的是真布尔值。
        #   我第一版写成 `str(...) != "不启用限价"` ——
        #   把 True 变成 "True"、False 变成 "False"，
        #   两个都不等于"不启用限价" ⇒ **永远得到 True**。
        #   也就是"不启用限价"会被当成"启用限价"，
        #   而且这个错【不会报错】，只是 maxPrice 悄悄从 None 变成有值。
        #   ⇒ 按真值判，同时兼容字符串形态（老数据/手写配置）。
        _capped_raw = getattr(fb, "price_capped", True)
        if isinstance(_capped_raw, str):
            capped = _capped_raw.strip() not in ("不启用限价", "false", "False", "")
        else:
            capped = bool(_capped_raw)
        from domain.quote_rules import (FALLBACK_LIMIT_YUAN, QUOTE_ESTIMATE,
                                        QUOTE_FIXED)
        # ★★★ 2026-10-14 接线：「设置 → 出票溢价 → 调整限价 / 溢价上限」
        #   ★ 它替掉的是 domain/quote_rules.py 里写死的
        #       FALLBACK_LIMIT_YUAN = 3   （预估成本 + 3 元/张）
        #     —— 店主原话："出票溢价 不是报价规则里有单张限价吗，
        #                   预估成本上增加的金额"，以及
        #                  "后台不要硬编码 由这里控制"。
        #   ★ 只在【没有规则命中】时用得上（兜底报价那条路）：
        #     规则命中时用的是规则自己的「单张限价」，两者是不同层次 ——
        #     规则里那处管"这条规则加多少钱"，这里管"没规则时的兜底加多少"。
        #   ★ 关掉开关 ⇒ 不加价（0/张）。这时限价就等于成本，
        #     也就是"一分钱都不许超过成本" —— 配合兜底限价那是很紧的口子。
        #   ★ 读不到配置 ⇒ 用 FALLBACK_LIMIT_YUAN（= 原来那个 3），行为不变。
        #   ★★★ 关掉开关时【仍然用 FALLBACK_LIMIT_YUAN（原来那个 3）】，
        #     不是 0。
        #     ★ 为什么（我第一版写成 0，是错的）：
        #       线上现在的默认就是"3 元/张"。如果"未启用"改成 0，
        #       一部署就把所有没规则命中的兜底限价从 成本+3/张 收紧到 成本，
        #       等于【悄悄改钱】—— 而且方向是"更容易下单失败"（300005）。
        #       店主说的是"不要硬编码，由这里控制"，不是"把默认值改掉"。
        #     ⇒ 未启用 = 维持现状（3）；启用 = 按这里配的数（可以是 0，那是店主明确选的）。
        _prem = getattr(cfg, "ticket_premium", None)
        if _prem is None:
            _limit_yuan = FALLBACK_LIMIT_YUAN
        elif bool(getattr(_prem, "enabled", False)):
            try:
                _limit_yuan = max(0.0, float(getattr(_prem, "cap_yuan", 0) or 0))
            except (TypeError, ValueError):
                _limit_yuan = FALLBACK_LIMIT_YUAN
        else:
            _limit_yuan = FALLBACK_LIMIT_YUAN
        return {
            # ★ 只有明确配成"一口价"才算 FIXED；其它（含配置坏掉）都按预估价。
            #   为什么保守：FIXED 是【我们承诺成交价】，
            #   配错成 FIXED 会让买家以为那个价一定成 ⇒ 保守方向选 LIMIT。
            "fallback_mode": (QUOTE_FIXED if mode == "一口价"
                              else QUOTE_ESTIMATE),
            "fallback_capped": capped,
            "fallback_limit_yuan": _limit_yuan,
        }
    except Exception:                                     # noqa: BLE001
        return {}


def _fallback_quote_enabled(cfg=None) -> bool:
    """兜底报价开关。默认 True ⇒ 默认行为不变。"""
    try:
        if cfg is None:
            import settings as _st
            cfg = _st.load()
        return bool(getattr(cfg.fallback_quote, "enabled", True))
    except Exception:                                     # noqa: BLE001
        return True


def _release_mode(cfg=None) -> str:
    """
    「设置 → 出票及放单 → 出票方式」：手动 / 自动。

    ★ 规格文档：单选「手动 / 自动」，默认【自动】。
    ★ 读不到配置就返回「自动」（= 原行为）：
      配置坏了不该让所有单都突然不建了 —— 那是比"多建一单"严重得多的事故。
    """
    try:
        if cfg is None:
            import settings as _st
            cfg = _st.load()
        mode = str(getattr(cfg.ticket_release, "mode", "") or "").strip()
        return RELEASE_MANUAL if mode == "手动" else RELEASE_AUTO
    except Exception:                                     # noqa: BLE001
        return RELEASE_AUTO


def _allow_seat_change(cfg=None) -> bool:
    """
    「设置 → 出票及放单 → 换座状态」：不允许换座吗。

    ★ 规格文档的默认值是【不允许】（allowSeatChange=False）：
      "宁愿这单不出，也不要给买家换成别的座位"。

    ★★ 为什么这个函数必须存在（原来是个静默失效的开关）：
      api.ch5_create 的 payload 里一直有 allowSeatChange 字段，
      但调用处【从来没传过】它 ⇒ 永远走签名默认值 True（允许换座）。
      ⇒ 界面上的"换座状态"填了也没用 —— 而且失败得毫无声响。

    ★ 读不到配置就返回 True（= 原行为）：配置坏了不该让下单直接失败。
    """
    try:
        if cfg is None:
            import settings as _st
            cfg = _st.load()
        return bool(getattr(cfg.ticket_release, "allow_seat_change", True))
    except Exception:                                     # noqa: BLE001
        return True


def _second_quote_wait_seconds() -> int:
    """
    二次报价等多久（秒）。没开 / 读不到 ⇒ 0（= 不排）。

    ★★ 2026-10-14：把「wait_seconds 从哪读」收在这里 ——
      原来它散在 _stash_quote 里，从 `_second_quote_cfg()` 拿对象再取，
      读者要跨两个方法才能拼出"这个字段被读了"。
      ⇒ 读配置的代码集中在一处，一眼可见。
    """
    try:
        import settings as _st
        cfg = _st.load().second_quote
        if not bool(getattr(cfg, "enabled", False)):
            return 0
        return int(getattr(cfg, "wait_seconds", 30) or 30)
    except Exception:                                         # noqa: BLE001
        return 0


def _second_quote_cfg():
    """
    二次报价的配置（没开 / 读不到 ⇒ None）。

    ★★ 2026-10-14 改：原来它只返回"开了没"（True/None），
      而 wait_seconds / discount_percent 是【调用方】再从
      `_st.load().second_quote` 取的 —— 于是"这个字段有没有人读"
      变得很难看出来（读者要跨方法才能拼出来）。
      ⇒ 现在直接把【配置对象】返回，谁用谁取，读什么一眼可见。
    """
    try:
        import settings as _st
        cfg = _st.load().second_quote
        if not bool(getattr(cfg, "enabled", False)):
            return None
        return cfg
    except Exception:                                         # noqa: BLE001
        return None


def second_quote_due(*, now=None, service=None) -> int:
    """
    把到点的二次报价发出去。返回处理了几条。

    ★★ 什么时候【不】发（这几条都要判，否则会发错）：
        · 会话已经不在了 / 不是那个聊天
        · 会话状态已经不是【已报价】—— 说明买家已经往下走了
          （拍了、付款了、或者转人工了）⇒ 再报价就是打扰
        · 会话上的报价 id 和排队时的对不上 ⇒ 这中间又报过新的，
          旧的这条不该再发（发出去买家会看到两个不同的价）
        · 已经发过二次报价了（幂等）—— 否则到点后每 tick 都发一遍
    ★ 无论发没发，处理完都把这条从队列里去掉 ——
      留着会每秒重试（打自己的接口）。
    """
    from application import schedule_queue as _sq
    items = _sq.due("second_quote", now=now)
    if not items:
        return 0
    if service is None:
        return 0
    made = 0
    for it in items:
        key = str(it.get("key") or "")
        chat_id, _, quote_id = key.partition(":")
        try:
            sess = service.sessions.get(chat_id)
            conv = getattr(sess, "conversation", None) if sess else None
            if conv is None or conv.state is not ConvState.QUOTED:
                _sq.drop("second_quote", key)
                continue
            if str(getattr(conv, "quote_id", "")) != quote_id:
                _sq.drop("second_quote", key)
                continue
            out = service.flow.send_second_quote(conv)
            if out is not None:
                made += 1
        except Exception:                                     # noqa: BLE001
            pass
        _sq.drop("second_quote", key)
    return made


def _intercept_attempted(pending) -> bool:
    """
    这一单我们【确实提交过拦截】吗。

    ★★ 为什么要判这个：force_ship 的话术说的是
      "我们第一时间为您提交了系统拦截……但已经出票了"。
      如果【没提交过拦截】却发这句，那是**编造事实**。
      买家会以为我们真的去拦过。
    ★ 拦截记录从哪来：pending 上由 request_intercept / 自动拦截打标
      （见 mark_intercept_attempted）。
    ★ 判不出来 ⇒ False（宁可不发那句，也不编造）。
    """
    if pending is None:
        return False
    return bool(getattr(pending, "intercept_attempted", False))


def mark_intercept_attempted(pending) -> None:
    """记下"这一单提交过拦截"。由 request_intercept / 自动拦截调用。"""
    if pending is None:
        return
    try:
        pending.intercept_attempted = True
    except Exception:                                         # noqa: BLE001
        pass


def _night_cfg() -> tuple | None:
    """
    夜间识别的 (开始, 结束, 话术)。没配/读不到 ⇒ None。

    ★ 放在模块级而不是塞在 on_screenshot 里：让那个函数只关心流程。
    ★ 读不到配置返回 None（不提醒），而不是抛异常 ——
      夜间提醒是附赠的，不该因为它让识别失败。
    """
    try:
        import settings as _st
        nr = _st.load().night_recognize
        return (str(getattr(nr, "start", "") or ""),
                str(getattr(nr, "end", "") or ""),
                str(getattr(nr, "message", "") or ""))
    except Exception:                                         # noqa: BLE001
        return None


def _fen_to_yuan(fen: int) -> str:
    """分 → 元字符串，保留两位。模板里展示用。"""
    return f"{fen / 100:.2f}"
