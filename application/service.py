"""
application/service.py —— 把编排层接到 HTTP 上

════════════════════════════════════════════════════════════════
★ 这个文件补的是一个真实的架构缺口

  审计发现：application/ticket_flow.py（845 行）是【孤儿】——
  没有任何生产代码引用它。也就是说整条链路做完了、测穿了，
  但【接不进任何地方】，只能靠测试脚本调用。

  这就是"能跑"和"能上线"之间的差别。这个文件负责补上。

★ 它做三件事

  1. 会话存取：一个 chat_id 对应一个 Conversation
     （真实系统里要落库；这里先用内存 + JSON 落盘，够用且可换）
  2. 把一条买家消息翻译成编排层的调用
  3. 把 FlowResult 变成"要发出去的若干条消息"

★ 它【不做】的事（都在各自该在的层里）

  不判断业务规则   → domain/
  不拼文案        → templates.py
  不管 HTTP       → server.py
  不认识良票协议   → integrations/
  这里只负责"接线"和"记住上一次说到哪了"。
════════════════════════════════════════════════════════════════
"""

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qa as qa_module                                   # noqa: E402
import settings as settings_module                       # noqa: E402
# ★★ 2026-10-14：补上 templates 的 import。
#   我在 _finish 里写了 `tpl.text_for("GREETING", ...)`，但【没有 import】——
#   `tpl` 是个未定义的名字，用的时候抛 NameError，
#   而它被我自己写的 `except Exception: pass` 静默吞掉了 ⇒
#   打招呼永远发不出去，而且【一点报错都看不到】。
#   ★ 教训：`except: pass` 会把"名字写错"和"真的发不出去"混成一件事。
import templates as tpl                                  # noqa: E402
from application import conversation_ai as ai_module      # noqa: E402
from application import store                             # noqa: E402
from application.conversation_ai import build_ai          # noqa: E402
from application.ticket_flow import FlowResult, TicketFlow   # noqa: E402
from domain.conversation import (Conversation, ConvState, Event,     # noqa: E402
                                 TRANSITIONS)
from domain.quote_rules import QuoteRuleEngine           # noqa: E402
from domain import quote_rules as qr                     # noqa: E402
from integrations.liangpiao.api import LiangPiaoAPI      # noqa: E402
from integrations.liangpiao.client import LiangPiaoClient    # noqa: E402


# ==================================================================== 会话
@dataclass
class Session:
    """
    一个买家的会话状态。

    ★ 为什么把 Conversation 和"上一轮问了什么"放一起：
      买家回"场次1"时，必须知道上一次给的是哪几个候选。
      这两件事天然属于同一个会话，分开放早晚会不一致。

    ★ history 是给【模型】看的对话记录，不是状态机的一部分。
      状态机看 Conversation.state；模型看 history。
      两者职责不同：状态机决定"能不能下单"，模型决定"怎么说话"。
    """
    chat_id: str
    conversation: Conversation
    last_question: str = ""            # 上一次问的是什么（pick/screenshot/...）
    last_options: dict = field(default_factory=dict)
    updated_at: str = ""
    # ★ 对话历史（模型无状态，每轮都要重发给它）
    history: list = field(default_factory=list)
    # 最近一次查到的订单详情（给模型看的只读事实）
    last_order_detail: object = None
    image_context: dict = field(default_factory=dict)
    last_ai_trace: dict = field(default_factory=dict)
    pending_order_card: dict = field(default_factory=dict)
    message_lock: object = field(default_factory=RLock, repr=False)
    # ★★ 2026-10-14：「上次打招呼的时刻」。空串 = 从没打过。
    #   用途：决定"首次打招呼"要不要发（间隔时长内不重复打）。
    #   ★ 必须落盘 —— 重启后忘了会重复打招呼。
    last_greeting_at: str = ""
    # ★★ 2026-10-14：「从这一刻起人工接管」。空串 = 没有接管。
    #   为什么必须单独记：原来只有 updated_at，而它【每条消息都刷新】——
    #   买家一直说话就把超时无限往后推，接管永远不会被自动关闭。
    #   这是「人工介入超时关闭」能工作的前提。
    manual_since: str = ""

    def remember(self, user_text: str, ai_text: str) -> None:
        """记一轮对话。只留 user/assistant 两种角色。"""
        stamp = datetime.now(timezone.utc).isoformat()
        if user_text:
            self.history.append({"role": "user", "content": user_text, "created_at": stamp})
        if ai_text:
            self.history.append({"role": "assistant", "content": ai_text, "created_at": stamp})
        # 兜住上限，别让会话无限长（memory_messages 是给模型看的窗口，
        # 这里存两倍，留点余量）
        if len(self.history) > 200:
            self.history = self.history[-120:]

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat(timespec="seconds")


# ==================================================================== 服务
class TicketService:
    """
    编排层的 HTTP 门面。

    ★ 依赖注入：api / 规则引擎 / 电话都从外面传。
      这样测试时能塞假 transport，生产时塞真的 —— 开关在调用方手里。
    """

    def __init__(self, api: LiangPiaoAPI, rules: QuoteRuleEngine, *,
                 tel: str, prefix: str = "YMD",
                 notify_url: str | None = None,
                 app_key: str = "", app_secret: str = "",
                 ai=None, persist: bool = True,
                 quote_store=None, revision: str = "", order_store=None, rules_provider=None):
        # ★★ 万达渠道：桥（只读）+ 本地租约台账。
        #   桥配不起来（没密钥/没装）就传 None —— 这单走良票，
        #   渠道判定会自然落到良票，不会让整个服务装不起来。
        wanda_bridge = None
        wanda_book = None
        try:
            from integrations.wanda.bridge import (BridgeConfig,
                                                   LeaseBook,
                                                   WandaBridgeClient)
            _cfg = BridgeConfig.from_env()
            if _cfg.key or _cfg.has_app_signature:
                wanda_bridge = WandaBridgeClient(_cfg)
                wanda_book = LeaseBook()
        except Exception:                                    # noqa: BLE001
            wanda_bridge = None
            wanda_book = None

        self.flow = TicketFlow(api, rules, tel=tel, prefix=prefix,
                               notify_url=notify_url,
                               # ★★ 报价记录存储：给了就每次出报价卡时落一笔。
                               #   这样"这价怎么算的、用哪版规则、何时过期"
                               #   都追得回来（线上 V5 是 quotes.sqlite3）。
                               quote_store=quote_store, revision=revision,
                               # ★★ 订单库：下单成功时落一笔 Order ——
                               #   订单管理页数据的唯一来源。
                               order_store=order_store,
                               # ★★ 万达桥：报价时按影院 ID 判渠道 + 取会员价
                               wanda_bridge=wanda_bridge,
                               wanda_lease_book=wanda_book,
                               rules_provider=rules_provider)
        self.quote_store = quote_store
        self.order_store = order_store
        self.app_key = app_key
        self.app_secret = app_secret
        # ★ 对话 AI：没传就按配置自己装一个。
        #   装不起来（没 Key）也没关系 —— 调用时它会说"转人工"。
        self.ai = ai if ai is not None else build_ai()
        self.persist = persist
        self.sessions: dict[str, Session] = {}
        # 回调幂等用（真实系统要持久化，这里先用内存）
        self.seen_nonces: set = set()
        self.seen_events: set = set()
        # ★★ 最近一次黑名单查询的结论（给日志/排查用）。
        #   为什么要留这个：黑名单最可能的故障是"没授权"，
        #   而那时它【静默跳过】（为了不误拦人）——
        #   没有这个字段就完全看不出"它其实查了、只是没查成"。
        self._last_blacklist_note = ""
        if persist:
            self._load()

    # ---------------------------------------------------------------- 会话
    def session(self, chat_id: str) -> Session:
        s = self.sessions.get(chat_id)
        if s is None:
            s = Session(chat_id=chat_id, conversation=Conversation(chat_id=chat_id))
            self.sessions[chat_id] = s
        return s

    def reset(self, chat_id: str) -> None:
        """清掉一个会话（买家说"重新来"时用）。"""
        self.sessions.pop(chat_id, None)
        if self.persist:
            self._save()

    def _load(self) -> None:
        """
        从磁盘读回会话。

        ★ 只恢复"状态 + 上一轮问的候选"，不恢复报价对象 ——
          报价有 30 分钟有效期，跨重启还留着反而是隐患。
          过期了让买家重发截图，比拿着一个可能失效的报价去下单安全。
        """
        saved = store.load_sessions()
        if not saved:
            return
        for chat_id, row in (saved or {}).items():
            try:
                conv = Conversation(chat_id=chat_id,
                                    state=ConvState(row.get("state") or ConvState.IDLE.value),
                                    tenant_id=str(row.get("tenant_id") or ""),
                                    shop_id=str(row.get("shop_id") or ""),
                                    buyer_id=str(row.get("buyer_id") or ""),
                                    channel=row.get("channel") or None)
            except ValueError:
                conv = Conversation(chat_id=chat_id)
            conv.demand_id = str(row.get("demand_id") or "")
            conv.demand_rev = int(row.get("demand_rev") or 0)
            conv.demand_inquiry = bool(row.get('demand_inquiry', False))
            conv.requested_order_no = str(row.get('requested_order_no') or '')
            conv.quote_comparison = row.get('quote_comparison') or {}
            self.sessions[chat_id] = Session(
                chat_id=chat_id, conversation=conv,
                image_context=row.get("image_context", {}) or {},
                pending_order_card=row.get("pending_order_card", {}) or {},
                last_ai_trace=row.get("last_ai_trace", {}) or {},
                history=row.get("history", [])[-50:],
                last_question=row.get("last_question", ""),
                last_options=row.get("last_options", {}) or {},
                updated_at=row.get("updated_at", ""),
                manual_since=row.get("manual_since", ""),
                last_greeting_at=row.get("last_greeting_at", ""))

    def _save(self) -> None:
        out = {}
        for chat_id, s in self.sessions.items():
            out[chat_id] = {
                "state": s.conversation.state.value,
                "tenant_id": s.conversation.tenant_id,
                "shop_id": s.conversation.shop_id,
                "buyer_id": s.conversation.buyer_id,
                "channel": s.conversation.channel,
                "demand_id": getattr(s.conversation, "demand_id", ""),
                "demand_rev": getattr(s.conversation, "demand_rev", 0),
                "demand_inquiry": getattr(s.conversation, "demand_inquiry", False),
                "requested_order_no": getattr(s.conversation, "requested_order_no", ''),
                "quote_comparison": getattr(s.conversation, "quote_comparison", {}),
                "image_context": s.image_context,
                "pending_order_card": s.pending_order_card,
                "last_ai_trace": s.last_ai_trace,
                "history": s.history[-50:],
                "last_question": s.last_question,
                "last_options": s.last_options,
                "updated_at": s.updated_at,
                # ★ 接管标记要落盘：进程重启后不该把"人工正在处理"
                #   这件事忘掉（忘了机器人就会去抢话）。
                "manual_since": getattr(s, "manual_since", ""),
                "last_greeting_at": getattr(s, "last_greeting_at", ""),
            }
        store.save_sessions(out)

    # ================================================================ 消息入口
    def on_message(self, chat_id: str, text: str = "", **kwargs) -> FlowResult:
        # Serialize turns so a delayed model response cannot amend a newer image.
        from .shop_scope import scope
        conv = self.session(chat_id).conversation
        tenant = kwargs.get('tenant_id') or conv.tenant_id
        shop = kwargs.get('shop_id') or conv.shop_id
        with scope(tenant, shop), self.session(chat_id).message_lock:
            return self._on_message(chat_id, text, **kwargs)

    def _on_message(self, chat_id: str, text: str = "", *,
                   image_url: str = "", city_name: str | None = None,
                   channel: str | None = None,
                   tenant_id: str = "", shop_id: str = "", peer_unb: str = "",
                   config=None, remote_message_id: str = "") -> FlowResult:
        """
        买家发来一条消息 —— 整个系统的唯一入口。

        ★ 判路顺序（★ 这个顺序是有讲究的，别随手改）：
            0. 黑名单            → 统一话术挡掉（可选的加固，查不到就跳过）
            1. 有图片            → 走截图识别
            2. 是候选序号        → 走二次确认
            3. 是确认指令        → 走下单
            4. 命中关键词回复     → 回关键词（不改变会话状态）
            5. 是"付好了"这类    → 推进状态机
            6. 都不是            → 交给对话 AI 回一句，或转人工

        ★ 为什么关键词在确认指令【后面】：
          "正确"是确认下单的指令，是要花钱的动作。
          万一有人把"正确"也配成了关键词，必须仍然是【确认优先】——
          否则买家回"正确"只会收到一句废话，单永远下不出去。

        ★ 为什么模型在【最后】：
          能逐字确定的（关键词、确认指令）就别让模型自由发挥。
          模型只兜长尾 —— 那些没写进关键词、状态机也判不出来的问话。

        ★★ shop_id / peer_unb 是【2026-10-14 加的可选参数】：
          黑名单要按 (租户, 买家 unb) 查，而 unb 只有插件事件里有
          （payload.peerUnb）。不传 ⇒ 不查黑名单（测试和内部调用走这条路）。
        """
        s = self.session(chat_id)
        s.current_message_id = str(remote_message_id or '')
        if tenant_id:
            s.conversation.tenant_id = str(tenant_id)
        if shop_id:
            s.conversation.shop_id = str(shop_id)
        if peer_unb:
            s.conversation.buyer_id = str(peer_unb)
        text = (text or "").strip()

        # ★ 配置【只读一次】，然后一路传下去。
        #   早先 on_message 读一次、_answer_with_ai 又读一次 ——
        #   同一个请求里读到两份配置，改了盘上的文件就会出现"前面按新的、
        #   后面按旧的"，而且测试没法注入配置。
        #
        #   config 参数只为测试而留：生产永远走 settings_module.load()。
        cfg = config if config is not None else settings_module.load()
        semantic_ready = (cfg.assistant.agent_enabled
                          and getattr(getattr(self, 'ai', None), 'enabled', False)
                          and getattr(getattr(getattr(self, 'ai', None), 'client', None), 'ready', False))

        # ---- 0. ★★ 黑名单（可选的加固）----
        # ★★ 2026-10-14 接线：「设置 → 黑名单 → 黑名单用户回复话术」
        #   为什么放在【最前面】：名单上的人不该走识别/询价/下单 ——
        #   那既浪费上游调用，也可能真给他建了单。
        #   ★★ 两条硬约束（见 application/blacklist.py 的模块注释）：
        #      · 查不到 ⇒ 照常回复（fail-open）。
        #        黑名单是加固，不是主流程；没授权/插件不通时
        #        绝不能让整个店不能自动回复。
        #      · 没有买家标识（peer_unb）⇒ 不查。
        #        拿 chat_id 去当 peerUnb 查会查出错误结果
        #        （chatId 是会话号，不是买家号）。
        if peer_unb:
            _blk = self._blacklist_gate(cfg, shop_id, peer_unb, s)
            if _blk is not None:
                return _blk

        # ★★ 2026-10-14 接线：「设置 → 回复 → 首次打招呼设置 / 间隔时长」
        #   ★ 先算"要不要打招呼"，真正【追加】放到最后（见函数末尾）——
        #     打招呼不该拦住正常流程：买家发选座图，我们照样识别+报价。
        _greet_due = self._greeting_due(s, cfg, has_image=bool(image_url))

        # ---- 1. 图片 → 识别 ----
        if image_url:
            # ★★ 2026-10-14 接线：「设置 → 识别报价 → 自动识别报价」
            #   关掉时【不自动识别】—— 走系统里已有的"转人工"路径
            #   （AI_HANDOFF 模板 + need_human），不发明第三种行为。
            #   ★ 默认开着 ⇒ 默认行为一个字不变。
            #   ★ 为什么不用新话术：这句话系统里已经有了，
            #     再加一条只是多一份要维护、可能不一致的文案。
            if not bool(getattr(cfg.recognize_quote, "auto_recognize", True)):
                out = FlowResult(ok=True, event=Event.BUYER_IMAGE,
                                 state=s.conversation.state,
                                 channel=s.conversation.channel)
                out.need_human = True
                out.template_keys.append("AI_HANDOFF")
                out.notes.append(
                    "★ 自动识别报价=关 ⇒ 不自动识别这张选座图，转人工（等店家手动录）")
                s.last_question = "screenshot"
                return self._finish(s, out, greet_due=_greet_due)
            from datetime import timezone
            from copy import deepcopy
            previous_image = deepcopy(s.image_context)
            previous_conv = deepcopy(s.conversation.__dict__)
            s.image_context = {k: str(getattr(s.conversation, k, "") or "")
                               for k in ("tenant_id", "shop_id", "buyer_id", "chat_id")}
            s.image_context.update(url=image_url, channel=channel or s.conversation.channel,
                                   demand_id=getattr(s.conversation, 'demand_id', ''),
                                   demand_rev=getattr(s.conversation, 'demand_rev', 0),
                                   created_at=datetime.now(timezone.utc).isoformat())
            if (previous_image.get('purchase_demand_id') == getattr(s.conversation, 'demand_id', '')
                    and all(previous_image.get(k) == s.image_context.get(k)
                            for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id'))):
                for key in ('purchase_started_at', 'purchase_demand_id', 'prior_platform_order_ids', 'quantity_evidence'):
                    if key in previous_image:
                        s.image_context[key] = deepcopy(previous_image[key])
            from application.buyer_assistant import explicit_ticket_count
            count = None if semantic_ready else explicit_ticket_count(text)
            if city_name:
                s.image_context["city"] = city_name
            if count is not None:
                s.image_context["ticket_count"] = count
            params = {"city_name": city_name, "channel": channel}
            pending_request = previous_image.get('awaiting_image_request')
            from .request_state import project
            if not pending_request:
                details = previous_image.get('ticket_details') or {}
                if details.get('pending') and details.get('position_preference'):
                    pending_request = details
            if (not previous_image.get('url')
                    and project(s.conversation, previous_image, cfg.assistant.memory_hours)['expired']):
                pending_request = None
                # Sequence recovery and result preservation must see the same
                # sanitized snapshot, not re-read expired pre-image fields.
                previous_image = {k: v for k, v in previous_image.items()
                                  if k not in ('awaiting_image_request', 'ticket_details',
                                               'agent_request_plan')}
            if pending_request:
                from .seat_request import scoped_request
                identity = {k: str(getattr(s.conversation, k, '') or '')
                            for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')}
                pending_request = scoped_request(pending_request, previous_image, s.conversation,
                                                identity, cfg.assistant.memory_hours)
            if pending_request and all(previous_image.get(k) == s.image_context.get(k)
                                       for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')):
                params['buyer_request'] = pending_request
                if not city_name:
                    from .seat_request import preimage_city
                    saved_city=preimage_city(pending_request,previous_image,s.conversation,cfg.assistant.memory_hours)
                    if saved_city:
                        params['city_name']=saved_city
                        s.image_context['city']=saved_city
            if count is not None:
                params["ticket_count"] = count
            result = self.flow.on_screenshot(s.conversation, image_url, **params)
            from .image_sequence import recover_sequence
            result = recover_sequence(self, s, previous_image, image_url, result, cfg)
            from .image_context import preserve_image_result
            result = preserve_image_result(s, previous_image, previous_conv, image_url, result)
            if pending_request and not result.ok and 'POSITION_SELECTION_REQUIRED' not in result.template_keys:
                s.image_context['awaiting_image_request'] = pending_request
            if text and semantic_ready:
                s.remember("买家发送了截图", "")
                s.last_question = 'screenshot'
                return self._answer_with_ai(s, text, cfg, greet_due=False)
            if text:
                from .ticket_details import extract
                from .buyer_assistant import BuyerAssistantTools
                details = extract(text)
                if details:
                    tool = BuyerAssistantTools(self, s, text, cfg, lambda _q: [])
                    tool.supplement_ticket_details(evidence=text, **details)
                    if tool.flow_result is not None:
                        result = tool.flow_result
            s.remember("买家发送了选座截图" + ("，附言：" + text if text else ""), "\n".join(result.messages))
            s.last_question = "screenshot"
            self._remember_options(s)
            return self._finish(s, result, greet_due=_greet_due)

        if not text:
            return self._finish(s, FlowResult(ok=False, error="空消息",
                                              channel=s.conversation.channel), greet_due=_greet_due)

        # With a working assistant, semantic interpretation precedes legacy
        # phrase dispatch. The tool layer still validates all business actions.
        if (cfg.assistant.agent_enabled and getattr(getattr(self, "ai", None), "enabled", False)
                and getattr(getattr(getattr(self, "ai", None), "client", None), "ready", False)):
            # Numeric selections are replies to a concrete system menu, not
            # natural-language intent. Preserve the existing validated picker.
            from .ticket_flow import _parse_pick
            _, pick_index = _parse_pick(text,
                keyword_cinema=cfg.recognize_quote.keyword_cinema,
                keyword_movie=cfg.recognize_quote.keyword_movie,
                keyword_show=cfg.recognize_quote.keyword_show)
            if (pick_index > 0 and s.conversation.state in
                    (ConvState.PICK_CINEMA, ConvState.PICK_MOVIE, ConvState.PICK_SHOW)
                    and s.last_question in ('screenshot', 'pick') and qa_module.reply(text, channel=s.conversation.channel or s.conversation.quote_channel or "") is None):
                picked = self.flow.on_candidate_pick(s.conversation, text)
                self._remember_options(s)
                return self._finish(s, picked, greet_due=_greet_due)
            # Keep the explicitly configured interception command on its
            # existing guarded workflow until an equivalent tool is available.
            if self._wants_intercept(text, cfg):
                return self._finish(s, self.flow.request_intercept(s.conversation, cfg=cfg), greet_due=_greet_due)
            return self._answer_with_ai(s, text, cfg, greet_due=False)

        from .image_context import handle_pending_image
        from .ticket_details import extract, cinema_confirmation
        details = extract(text, pending=bool(s.image_context.get('ticket_details')))
        confirmed_cinema = cinema_confirmation(s, text)
        if confirmed_cinema:
            details = {'cinema':confirmed_cinema}
        if (s.image_context.get('ticket_details') and text.strip()==
                (s.image_context.get('recognition_facts') or {}).get('cinema_name')):
            details = {'cinema':text.strip()}
        if s.image_context and details:
            from .schedule_image import recover_pending
            recover_pending(self, s, cfg)
            from .buyer_assistant import BuyerAssistantTools
            tool = BuyerAssistantTools(self, s, text, cfg, lambda _q: [])
            tool.supplement_ticket_details(evidence=text, **details)
            if tool.flow_result is not None:
                s.remember(text, '\n'.join(tool.flow_result.messages))
                return self._finish(s, tool.flow_result, greet_due=False)
        pending_result = handle_pending_image(self, s, text, cfg)
        if pending_result is not None:
            s.remember(text, "\n".join(pending_result.messages))
            return self._finish(s, pending_result, greet_due=False)

        # Explicit quantity supplements resume the image workflow even without AI.
        from application.buyer_assistant import BuyerAssistantTools, explicit_ticket_count
        if s.image_context and explicit_ticket_count(text) is not None:
            tool = BuyerAssistantTools(self, s, text, cfg, lambda _q: [])
            answer = tool.supplement_ticket_count(text)
            if tool.flow_result is not None:
                result = tool.flow_result
                s.remember(text, "\n".join(result.messages))
                self._remember_options(s)
                return self._finish(s, result, greet_due=False)
            result = FlowResult(channel=s.conversation.channel)
            result.messages.append(answer.get("reason", "请确认当前购票信息。"))
            s.remember(text, "\n".join(result.messages))
            return self._finish(s, result, greet_due=False)

        # ---- 2. 候选序号 → 二次确认 ----
        #   ★ 两种写法都进这条路：
        #       「影院1」→ kind='cinema', index=1
        #       「1」    → ★ kind=None, index=1（维度未知）
        #     判据用 index > 0 同时覆盖两者：
        #       _parse_pick 只在【真的解析出序号】时才返回正 index，
        #       其它一律 (None, 0)。
        #   ★★ 但【已登记的关键词优先】——
        #     我们的文案对买家承诺了「常见问题回复999」，qa 表里还有
        #     001/002/003/004/005/111，这些都是【纯数字】。
        #     如果候选路由先吃掉它们，买家按文案发"999"就只会收到
        #     "上次识别已过期 / 重发候选"，而拿不到常见问题。
        #     ⇒ 兑现给买家的指令，优先级高于内部流程判断。
        #   ★ 纯数字只在【上次问的就是候选】时才当序号 ——
        #     否则买家随口发个"1"会被误当成选座。
        #   ★ 维度由 on_candidate_pick 按 conv.state 补上（只有它知道在问哪个）。
        from application.ticket_flow import _parse_pick
        # ★ 2026-10-14 接线：把配置的识别关键字传下去
        #   （设置 → 识别报价 → 影院/影片/场次关键字）
        kind, index = _parse_pick(
            text,
            keyword_cinema=cfg.recognize_quote.keyword_cinema,
            keyword_movie=cfg.recognize_quote.keyword_movie,
            keyword_show=cfg.recognize_quote.keyword_show)
        is_known_keyword = bool(text) and qa_module.reply(text, channel=s.conversation.channel or s.conversation.quote_channel or "") is not None
        if (index > 0 and not is_known_keyword
                and s.last_question in ("screenshot", "pick")):
            result = self.flow.on_candidate_pick(s.conversation, text)
            self._remember_options(s)
            return self._finish(s, result, greet_due=_greet_due)

        # ---- 3. ★ 确认指令优先于关键词 ----
        #   配置里"正确"是确认指令。如果哪天有人把同一个词配成关键词回复，
        #   必须仍然是"确认下单"优先 —— 否则买家回"正确"只会收到一句废话，
        #   单永远下不出去。
        if self._is_confirm(text, cfg, s):
            return self._finish(s, self.flow.on_buyer_confirm(s.conversation), greet_due=_greet_due)

        # ---- 3.5. ★★ 买家索要取票码（确定性指令）----
        # ★★ 2026-10-14 接线：「设置 → 获取票码 → 获取票码关键词 / 无出票回复」
        #   为什么放这里（确认指令之后、关键词之前）：
        #     · 它在语义上是一条【指令】，和"正确"同级，不该被关键词吃掉
        #     · 但也不能抢在确认指令前面（确认要花钱，优先级最高）
        #   ★ 命中就 return —— 模型不会跑，所以不存在"回两遍"。
        if self._wants_ticket_code(text, cfg):
            codes = self._buyer_ticket_codes(chat_id)
            out = FlowResult(ok=True, state=s.conversation.state,
                             channel=s.conversation.channel)
            if codes:
                out.messages.append("您的取票码：" + "，".join(codes))
                out.template_keys.append("TICKET_CODE")
            else:
                # 没有已出票成功的单 ⇒ 用配置的话（默认"您还没有出票成功的订单~"）
                out.messages.append(
                    str(getattr(cfg.get_ticket_code, "no_ticket_reply", "")
                        or "您还没有出票成功的订单~"))
            # ★ 不改变会话状态：它是旁路查询，不是流程推进
            return self._finish(s, out, greet_due=_greet_due)

        # ---- 3.6. ★★ 买家要求拦截订单（确定性指令）----
        # ★★ 2026-10-14 接线：「设置 → 订单拦截 → 关键字 / 话术」
        #   为什么放在取票码那条之后、关键词之前：同级指令，
        #   都在"确认指令"之后（确认要花钱，优先级最高）。
        #   ★ 命中就 return —— 模型不会跑。
        #   ★ 这条【会真的去关单】（CLOSE_FAILED_ORDER），
        #     所以只认【精确等于】，不做模糊匹配。
        if self._wants_intercept(text, cfg):
            result = self.flow.request_intercept(s.conversation, cfg=cfg)
            return self._finish(s, result, greet_due=_greet_due)

        # Natural language belongs to the assistant, including questions that
        # happen to contain a confirmation/payment keyword. Platform events
        # remain the only authority for transaction results.
        if (cfg.assistant.agent_enabled and getattr(getattr(self, "ai", None), "enabled", False)
                and getattr(getattr(getattr(self, "ai", None), "client", None), "ready", False)):
            return self._answer_with_ai(s, text, cfg, greet_due=False)

        # ---- 4. 关键词回复 ----
        if cfg.reply.intelligent_qa_enabled:
            reply = qa_module.reply(text, channel=s.conversation.channel or s.conversation.quote_channel or "")
            if reply:
                out = FlowResult(ok=True, state=s.conversation.state, channel=s.conversation.channel)
                out.messages.append(reply)
                out.template_keys.append(f"QA:{text}")
                # ★ 关键词回复【不改变】会话状态 —— 它是旁路，不是流程
                return self._finish(s, out, greet_due=_greet_due)

        # ---- 5. 都不匹配 → 交给状态机判定 ----
        conv = s.conversation
        if conv.state is ConvState.AWAIT_PAY and _looks_like_paid(text):
            out = FlowResult(ok=True, state=conv.state,
                             channel=conv.channel)
            out.messages.append("收到，付款状态以平台订单核验为准，请稍等核对。")
            return self._finish(s, out, greet_due=_greet_due)

        # ---- 5.5. ★★★ 意图判定 → 事件（逐字匹配兜不住的都靠这一步）----
        #
        #   ★ 为什么需要（店主指出的真需求，有实证）：
        #     "买家的话 → 状态机事件"原先只有【逐字匹配】这一层：
        #       · 确认下单：_is_confirm 要求 text 逐字 ∈ 别名列表
        #       · 关键词：25 条
        #     线上真实消息库里抓到的买家原话：「我已拍下，待付款」——
        #     不是关键词、不是确认别名 ⇒ 什么都不触发，单下不出去。
        #     而同一件事买家有几十种说法 ⇒ 逐字匹配必然漏。
        #
        #   ★★ 三重安全边界（缺一不可）：
        #     ① 模型只输出【意图名】，不输出文本、不给参数、不碰钱
        #     ② 名字必须在我这张映射表里（表外的直接忽略）
        #     ③ ★ 映射成的那个事件必须对当前状态【合法】——
        #        合法性查的是状态机自己的 TRANSITIONS，
        #        所以模型猜错的后果是"什么都不做/转人工"，不是"下错单"
        #     ⇒ 钱的守卫一个都不用动：渠道守卫、报价过期、
        #        maxPrice 兜底、下单幂等全在既有代码里。
        #
        #   ★ 为什么放在关键词(4)和 _looks_like_paid(5)【之后】：
        #     能逐字确定的就别让模型参与（更快、更可控、零成本）。
        #     模型只兜长尾 —— 和整条链路的既有纪律一致。
        hit = self._intent_to_event(conv, text, cfg)
        if hit is not None:
            event, note, reply_key = hit
            # ★★ 只回一句话的那一类（event=None）：不碰状态机、不碰钱。
            if event is None:
                out = FlowResult(ok=True, state=conv.state, channel=conv.channel)
                out.notes.append(f"★ 意图判定（模型）：{note}")
                if reply_key:
                    out.add(reply_key)
                return self._finish(s, out, greet_due=_greet_due)
            decision = conv.apply(event)
            out = FlowResult(ok=True, state=decision.state, event=event,
                             channel=conv.channel)
            out.notes.append(f"★ 意图判定（模型）：{note}")
            if decision.needs_manual:
                # ★ 模型判了、但状态机不允许 —— 不硬来，转人工。
                out.need_human = True
                out.notes.append(
                    f"★ 意图 {event.value} 在 {conv.state.value} 不合法 → 转人工"
                    f"（没有改变会话状态）")
                return self._finish(s, out, greet_due=_greet_due)
            if decision.reply_key:
                # ★ 时效按实际购入档位给（和关键词路径同一口径）
                out.add(decision.reply_key,
                        {"ticket_window": self.flow._window(conv)})
            return self._finish(s, out, greet_due=_greet_due)

        # ---- 6. ★★ 流程外的问话 → 让模型接住 ----
        #
        #  这一步以前【根本不存在】。买家问「你们家能开发票吗」、
        #  「我昨天买的那个还能改吗」、甚至「在吗」，
        #  系统都回同一句「您提交的确认指令不存在」—— 5 种问法 1 种回复。
        #
        #  ★ 判路顺序：关键词（第 4 步）已经先试过了。
        #    能逐字确定的就别让模型自由发挥；模型只兜长尾。
        return self._answer_with_ai(s, text, cfg, greet_due=_greet_due)

    # ------------------------------------------------------- 意图 → 事件（模型）
    def _intent_to_event(self, conv, text: str, cfg):
        """
        让模型把买家一句话判成意图，再决定【怎么处理】。

        返回三选一：
          (Event, 说明, None)        → 推进状态机（走已有的确定性处理）
          (None, 说明, 模板key)       → ★ 只回一句话、不动状态机、不动钱
          None                       → 判不出来 / 状态不允许（退回兜底答话）

        ★★★ 这个方法的全部价值在【它拒绝什么】上：
          ① AI 没配 / 没开  → None（退回原来的行为，不因为没模型就出问题）
          ② ★ 当前状态下【一个意图都处理不了】 → 不打模型，直接 None
          ③ 模型判出表外的名字 → None（不猜）
          ④ ★ 那个事件对当前状态【不合法】 → None
             合法性不另写一套，直接查状态机自己的 TRANSITIONS ——
             这样"允许模型推进哪些流程"永远和状态机保持一致，
             不会出现"这里放行、状态机不认"的分叉。
          ⇒ 模型能影响的只有【状态机本来就允许的那几个转移】。
            猜错最多是判成 NONE（什么都不发生），不会下错单。

        ★★ 关于第 ② 条（为什么值得单独做）：
          第一版没有它，结果是【每条落到兜底的消息都多打一次模型】——
          哪怕当前在"空闲"状态、根本没有任何意图能推进流程。
          既费钱又慢，而且会让"模型调用次数"这个观测指标失真。
          这里改成先看状态机：把 INTENT_EVENTS 里的事件逐个拿去问
          "在你这个状态下合法吗"，一个都不合法就别浪费那次调用。
          ★ 注意这不是"关键词库换皮"—— 它判的是【结构上有没有可能】，
            而不是"买家这句话像不像某句话"。
            只要某个状态下有合法意图，全部说法仍然交给模型判。

        ★★★ 为什么会有"只回一句话"这一类（2026-10-14 店主选 C）：
          买家说「改价」（真实数据 102 条），闲鱼的改价接口是
          【针对已有订单】的 —— 没有订单就没有可改的对象，
          所以"先改价再让他拍"在闲鱼上根本做不到。
          店主定的做法（C）：告诉他"你先拍下，拍下后我给你改价"。
          ⇒ 那只是一句话，【不需要碰状态机】。
          但"该不该说这句"仍然要模型判意图（说法有几十种），
          而"说了之后什么都不动"由代码保证（event=None）。
        """
        ai = self._ai_for_config(cfg)
        if ai is None or not getattr(ai, "enabled", False):
            return None
        from .assistant_scope import allowed
        if not allowed(cfg.assistant, conv, self.order_store):
            return None
        # ★ 状态驱动 + 语境驱动短路：这个状态下一个意图都处理不了、
        #   而且当前也不在"刚谈过价"的语境里，就别问模型了。
        #   ★ 为什么"只说话"的意图也要看语境（不是无脑放行）：
        #     如果它在所有状态下都有效，那每条落到兜底的消息都要多打一次模型。
        #     「改价」只在【刚报过价】时才有意义 —— 那时买家知道价、
        #     说改价才构成请求。在"空闲"状态说改价，我们没报过价，
        #     那句话不构成改价请求，认了反而会回一句莫名其妙的话。
        usable = {name: ev for name, ev in INTENT_EVENTS.items()
                  if (conv.state, ev) in TRANSITIONS}
        speakable = tuple(n for n in INTENT_MESSAGES
                          if n == "REPRICE_REQUEST"
                          and getattr(conv, "price_talk_open", False))
        if not usable and not speakable:
            return None
        name = ai.classify_intent(text, state=conv.state)
        if not name or name == "NONE":
            return None
        # ★ 先看"只说话"这一类（它不需要状态机许可，因为什么都不动）
        if name in speakable and name not in usable:
            return (None, f"{text[:24]!r} → {name} → 只回一句话（不动状态）",
                    INTENT_MESSAGES[name])
        event = usable.get(name)
        if event is None:
            # ★ 表里有、但这个状态用不上 —— 记一笔便于以后按真实数据决定，
            #   但绝不放行。
            return None
        return event, f"{text[:24]!r} → {name} → {event.value}", None

    # ---------------------------------------------------------------- 兜底答话
    def _ai_for_config(self, cfg):
        ai = self.ai
        from . import model_config
        connection = model_config.connection_snapshot(cfg.assistant.model_name)
        expected = connection.options()
        if any(getattr(getattr(ai, 'client', None), k, v) != v for k, v in expected.items()):
            return ai_module.build_ai(cfg, connection=connection)
        return ai

    def _answer_with_ai(self, session, text: str, cfg,
                        *, greet_due: bool = False) -> FlowResult:
        """
        用模型回一句，或者决定转人工。

        ★ 这一步以前【根本不存在】。买家问「你们家能开发票吗」、
          「我昨天买的那个还能改吗」、甚至「在吗」，
          系统都回同一句「您提交的确认指令不存在」—— 5 种问法 1 种回复。

        ★ 三条线：
          · 模型答得了     → 发它的回复
          · 答不了/没Key/失败 → 转人工（而不是甩"指令不存在"）
          · ★ 模型说什么都【不改变会话状态】—— 它只管说话，管不了钱
        """
        out = FlowResult(ok=True, state=session.conversation.state,
                         channel=session.conversation.channel)
        conv = session.conversation
        from .assistant_scope import allowed
        if not allowed(cfg.assistant, conv, self.order_store):
            out = FlowResult(ok=True, state=conv.state, channel=conv.channel)
            out.notes.append("AI 阶段限制：本阶段不自动回复")
            return self._finish(session, out, greet_due=False)
        # 会话历史（模型无状态，每次都要重新告诉它）
        history = self._history(session, cfg)

        from .buyer_assistant import BuyerAssistantTools
        assistant_tools = BuyerAssistantTools(self, session, text, cfg,
                                               (lambda keyword: _faq_lookup(keyword, channel=conv.channel or conv.quote_channel or ""))
                                               if cfg.reply.intelligent_qa_enabled else None)
        assistant_tools.enable_semantic_routing()
        if self.persist:
            from .official_dialogue import read_dialogue
            assistant_tools.official_dialogue = read_dialogue(session, cfg.assistant)
            if assistant_tools.official_dialogue['status'] == 'AVAILABLE':
                # The platform snapshot excludes recalled replies and includes
                # manual seller messages that our local history does not know.
                history = []
                assistant_tools.official_buyer_history = [
                    {'role': 'user', 'content': row['text'], 'created_at': row['at']}
                    for row in assistant_tools.official_dialogue['messages'] if row['speaker'] == 'buyer']
        from .semantic_request import INSTRUCTIONS
        relevant_faq = ("\n万达店铺座位用语：买家说中间位置、中央区域，结合万达选座图指灰色会员座位。可以申请购买，请买家圈出位置并说明张数；具体余座需核对，不保证指定座位或连座。若明确说的是其他区域，以买家说明为准。这类可否购买咨询无需直接转人工。" if conv.channel == 'wanda' else "")
        from .assistant_context import build, render
        try:
            context = render(build(assistant_tools))
            ai = self._ai_for_config(cfg)
            result = ai.answer(
                text, cfg=cfg,
                context=INSTRUCTIONS + "\n" + context + relevant_faq
                        + "\n当前渠道是万达时，由店家处理出票，不得承诺自动出票。",
                state=conv.state, history=history,
                # ★★ 只读工具 —— 模型可以自己查订单/报价/FAQ/退票进度。
                #   身份隔离靠【数据源绑定当前会话】实现：工具不接收 chat_id，
                #   所以模型无论怎么问都只能查到这个买家自己的东西。
                tools=assistant_tools,
                # ★★ 传会话 —— 让 prompt 里能算出【还缺什么】。
                #   实测需求：万达买家发图（图上只有影院）后补文字，
                #   模型不知道还差什么，会把已有的又问一遍。
                conv=conv)
        except Exception as exc:                     # noqa: BLE001
            # ★ 兜底失败也绝不能抛给买家
            result = {"source": "manual", "text": "", "handoff": True,
                      "reason_code": "AI_EXCEPTION:" + type(exc).__name__,
                      "note": f"转人工：AI 层异常（{type(exc).__name__}: {exc}）"}

        session.last_ai_trace = {
            "source": result.get("source"),
            "reason_code": result.get("reason_code", ""),
            "tools": [name for name, _ in assistant_tools.calls],
            "has_flow_result": assistant_tools.flow_result is not None,
            "buyer_intent": getattr(assistant_tools, 'semantic_intent', None),
            "demand_id": getattr(conv, 'demand_id', ''),
            "demand_rev": getattr(conv, 'demand_rev', 0),
            "official_dialogue_status": getattr(assistant_tools, 'official_dialogue', {}).get('status', 'NOT_QUERIED'),
            "official_dialogue_count": len(getattr(assistant_tools, 'official_dialogue', {}).get('messages', [])),
            "seller_review_active":bool(getattr(assistant_tools,'seller_context',{}).get('active')),
            "seller_message_ids":[r['message_id'] for r in getattr(assistant_tools,'seller_context',{}).get('messages',[])],
        }
        from .reply_metrics import snapshot
        session.last_ai_trace["reply_metrics"] = snapshot(assistant_tools)
        session.last_ai_trace["reply_metrics"]["final_outcome"] = result.get("source")
        if assistant_tools.flow_result is not None:
            flow_result = assistant_tools.flow_result
            self._record_reply_metrics(session, conv.channel)
            flow_result.notes.append("AI 工具返回业务流程结果")
            session.remember(text, "\n".join(flow_result.messages))
            self._remember_options(session)
            return self._finish(session, flow_result)

        original_reply = result.get("text") or ""
        assessment = assistant_tools.review_reply(original_reply)
        if assessment.reason != "ACCEPTED" and assessment.text != original_reply:
            from .reply_metrics import fallback
            fallback(assistant_tools, assessment.reason)
        session.last_ai_trace["reply_metrics"] = snapshot(assistant_tools)
        session.last_ai_trace["reply_metrics"]["final_outcome"] = result.get("source")
        result["text"] = assessment.text
        session.last_ai_trace["reply_policy"] = assessment.reason
        session.last_ai_trace["context_version"] = getattr(assistant_tools, "context_snapshot", {}).get("version", "UNAVAILABLE")
        if result["text"] != original_reply:
            result["note"] = (result.get("note") or "") + "；统一回复校验：" + assessment.reason
        self._record_reply_metrics(session, conv.channel)
        session.remember(text, result.get("text") or "")

        if result["source"] == "model" and result["text"]:
            out.messages.append(result["text"])
            out.template_keys.append("AI_REPLY")
            out.notes.append(result["note"])
            return self._finish(session, out, greet_due=greet_due)

        # ---- 转人工 ----
        out.need_human = True
        out.notes.append(result["note"])
        # 模型可能给了一句安抚话，先发它；没有就用配置里的转人工话术
        # Send one handoff message; retain the model's relevant explanation.
        if result.get("text"):
            out.messages.append(result["text"])
        # ★ 用配置里的转人工话术（call_manual.reply），不另造一份 ——
        #   同一个意思写两处，早晚会不一致。
        if not result.get("text"):
            out.messages.append(cfg.call_manual.reply or "已为您呼叫人工客服，请稍等")
        out.template_keys.append("AI_HANDOFF")
        return self._finish(session, out, greet_due=greet_due)

    def _record_reply_metrics(self, session, channel):
        if not self.persist:
            return
        from . import store
        try:
            store.append_reply_metrics({"channel": channel or "", "reply_metrics": session.last_ai_trace["reply_metrics"]})
        except Exception as exc:  # noqa: BLE001 -- diagnostics must not block a reply
            # Monitoring failures must not suppress a buyer reply.
            session.last_ai_trace["reply_metrics_write_error"] = type(exc).__name__

    def _history(self, session, cfg) -> list:
        """按配置的记忆窗口，取出要带给模型的最近几轮。"""
        limit = max(2, int(cfg.assistant.memory_messages or 50))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.assistant.memory_hours)
        rows = []
        for row in session.history:
            try:
                stamp = datetime.fromisoformat(row.get("created_at", ""))
                if stamp.tzinfo is None or stamp < cutoff:
                    continue
                if row.get("role") in ("user", "assistant") and isinstance(row.get("content"), str):
                    rows.append({"role": row["role"], "content": row["content"]})
            except (ValueError, TypeError, AttributeError):
                continue
        return rows[-limit:]

    def on_callback(self, raw_body: str, headers: dict) -> tuple[str, FlowResult | None]:
        """良票回调。★ 应答体必须原样返回给平台。"""
        return self.flow.on_callback(
            raw_body, headers,
            app_key=self.app_key, app_secret=self.app_secret,
            seen_nonces=self.seen_nonces, seen_events=self.seen_events)

    def on_refund(self, chat_id: str) -> FlowResult:
        from .shop_scope import scope
        conv = self.session(chat_id).conversation
        with scope(conv.tenant_id, conv.shop_id):
            return self._on_refund_scoped(chat_id)

    def _on_refund_scoped(self, chat_id: str) -> FlowResult:
        """
        买家要求退票。

        ★★ 2026-10-14 接线：退款时序（设置 → 订单退款 → 星期 / 时间段 / 延迟）
          ★ 这一组管的是【我们提交退票申请的那一刻】，不是"钱什么时候到账"
            ——钱是良票退的，我们拦不住（`_on_refunded` 是平台告诉我们"退完了"）。
            而我们确实做"提交"这个动作：flow.on_refund_request → ch5_refund。
          ★ 三条判据：
              时段/星期  不在窗口 ⇒ 不提交，也不排队，明说什么时候提交
              延迟       在窗口   ⇒ 排一条"到点提交"（handle_run_due 推进）
              都没有     默认配置 ⇒ 立刻提交，行为和以前一模一样
          ★ 默认 weekdays 是七天全选、时段 00:00:00~23:59:59、delay=0
            ⇒ 默认行为一个字不变。
        """
        s = self.session(chat_id)
        cfg = settings_module.load()
        now = datetime.now()
        from application import refund_schedule as rs

        ok_window, why = rs.in_window(cfg, now=now)
        if not ok_window:
            # ★ 不在窗口【不排队】——
            #   排了也没人能推进（窗口外 handle_run_due 不会提交），
            #   而且买家会以为"已经申请了"。明说更诚实。
            out = FlowResult(ok=True, state=s.conversation.state,
                             channel=s.conversation.channel)
            out.notes.append(f"退款时序：{why} ⇒ 没有提交申请")
            out.messages.append(
                "好的，不过现在还不到我们处理退款的时间 —— "
                + rs.next_window_hint(cfg, now=now) + "，到时我这边直接给您提交~")
            return self._finish(s, out)

        _delay = 0
        try:
            _delay = int(getattr(cfg.order_refund, "delay_seconds", 0) or 0)
        except Exception:                                 # noqa: BLE001
            _delay = 0
        _pending = getattr(s.conversation, "pending_order", None)
        if _delay > 0 and _pending is not None and getattr(_pending, "order_no", ""):
            due = rs.schedule(str(_pending.order_no), chat_id,
                              delay_seconds=_delay, now=now)
            out = FlowResult(ok=True, state=s.conversation.state,
                             channel=s.conversation.channel)
            out.notes.append(
                f"退款时序：延迟 {_delay} 秒，已排到 {due} 提交（等 handle_run_due 推进）")
            out.messages.append(
                f"好的，{_delay} 秒后我这边就给您提交退票申请，"
                "如果还想改主意现在说一声就行~")
            return self._finish(s, out)

        return self._finish(s, self.flow.on_refund_request(s.conversation))

    # ---------------------------------------------------------------- 内部
    def _is_confirm(self, text: str, cfg, session: Session) -> bool:
        """
        这句话是不是确认指令。

        ★ 只在【已报价】状态才认。否则买家随口说"正确"就会被当成下单，
          那是很危险的误判 —— 下单要花钱。
        """
        if session.conversation.state is not ConvState.QUOTED:
            return False
        words = cfg.fish_reprice.confirm_aliases or [cfg.fish_reprice.confirm_keyword]
        return text in words

    def _blacklist_gate(self, cfg, shop_id: str, peer_unb: str,
                        session: Session) -> "FlowResult | None":
        """
        黑名单闸门。命中 ⇒ 返回"统一话术"的结果；否则返回 None（继续正常流程）。

        ★★ 2026-10-14 接线：「设置 → 黑名单 → 黑名单用户回复话术」
           话术来自配置（默认「系统升级维护中，建议您咨询其他商家」）。

        ★★★ 三条必须守住的（这条闸门很容易写危险）：
          ① 查不到 ⇒ 返回 None（照常回复）。绝不因为"查不了黑名单"
             就把买家挡掉 —— 那是拿一个小功能的风险换掉整条业务线。
          ② 只在【确定查到】且【确实在名单里】才挡。
             ok=False 和 blacklisted=False 是两件事，不能混。
          ③ 挡掉时【不改会话状态】。名单上的人只是拿一句统一话术；
             万一店主后来把他移出名单，会话还能接着正常走。
        """
        try:
            from . import blacklist as bl
            got = bl.check(shop_id, peer_unb)
        except Exception as exc:                              # noqa: BLE001
            # 查黑名单本身抛异常也不能影响回复（宁可漏挡，不可误挡）
            self._last_blacklist_note = f"黑名单查询异常：{exc}"
            return None

        self._last_blacklist_note = bl.status_text(got)
        if not (got.get("ok") and got.get("blacklisted")):
            return None

        reply = str(getattr(cfg.blacklist, "reply", "") or "").strip()
        if not reply:
            # ★ 配了空话术怎么办：不发空消息（那等于静默丢弃买家的消息）。
            #   空话术 = 店主没想好要说什么 ⇒ 按"不拦"处理，照常回复。
            self._last_blacklist_note += "｜话术为空，按不拦处理"
            return None

        out = FlowResult(ok=True, state=session.conversation.state,
                         channel=session.conversation.channel)
        out.messages.append(reply)
        out.template_keys.append("BLACKLIST")
        # ★ 不改变会话状态：这是"旁路拦截"，不是流程推进
        return self._finish(session, out, greet_due=False)

    def _wants_ticket_code(self, text: str, cfg) -> bool:
        """
        这句话是不是"我要取票码"这条指令。

        ★ 精确等于（照 _is_confirm 的风格）：
          包含匹配会让"我获取票码了吗"这种【询问】也命中 —— 而那是问句，
          该让模型答，不该走这条确定性路由。
        ★ 关键字来自配置（设置 → 获取票码 → 获取票码关键词）。
        """
        want = (text or "").strip()
        if not want:
            return False
        kw = str(getattr(cfg.get_ticket_code, "keyword", "") or "").strip()
        return bool(kw) and want == kw

    def _wants_intercept(self, text: str, cfg) -> bool:
        """
        这句话是不是"拦截订单"这条指令。

        ★ 精确等于（照 _is_confirm / _wants_ticket_code 的风格）：
          包含匹配会让"拦截订单要多久"这种【询问】也命中 ——
          而那是问句，该让模型答；而这条会真的去关单。
        ★ 关键字来自配置（设置 → 订单拦截 → 订单拦截关键字）。
        """
        want = (text or "").strip()
        if not want:
            return False
        kw = str(getattr(cfg.order_intercept, "keyword", "") or "").strip()
        return bool(kw) and want == kw

    def _buyer_ticket_codes(self, chat_id: str) -> list:
        """
        这个买家【已出票成功】的取票码（可能多条）。

        ★ 为什么用 orders_payload() 而不是 by_identity：
          by_identity 要身份四元组（tenant/shop/buyer/chat），
          而 Conversation 上只有 chat_id —— 拿不到另外三个。
          orders_payload() 是订单页也在用的同一条读模型，带 chat_id，
          过滤一下就是"这个会话的单"。
        ★ 只取【有取票码】且【没被取消/退款】的：
          取消或退款的单，票码已经作废，发出去会让买家白跑一趟。
        """
        cid = str(chat_id or "")
        if not cid:
            return []
        try:
            from . import ledger
            rows = ledger.orders_payload().get("orders") or []
        except Exception:                                     # noqa: BLE001
            return []
        out = []
        for o in rows:
            if str(o.get("chat_id") or "") != cid:
                continue
            if str(o.get("state") or "") in ("CANCELLED", "REFUNDED", "FAILED"):
                continue
            for c in (o.get("ticket_codes") or []):
                c = str(c).strip()
                if c and c not in out:
                    out.append(c)
        return out

    def _remember_options(self, session: Session) -> None:
        """
        记住上一轮给的候选，买家回「场次1」时要用。

        ★ 候选【内容】是从会话上取（conv.last_candidates）。
          早先签名里多了一个 result 参数，函数体里从来没用过 ——
          这类「看着像在传数据」的废参数最容易误导读者。
        """
        conv = session.conversation
        session.last_question = (
            "pick" if conv.state in (ConvState.PICK_CINEMA, ConvState.PICK_MOVIE,
                                     ConvState.PICK_SHOW)
            else "screenshot")
        session.last_options = getattr(conv, "last_candidates", {}) or {}

    def _finish(self, session: Session, result: FlowResult,
                *, greet_due: bool = False) -> FlowResult:
        # ★★ 2026-10-14：首次打招呼在这里【追加】。
        #   ★ 为什么放 _finish：on_message 有 11 个返回点，每个都改早晚漏。
        #     所有出口都过这里 ⇒ 一处就够。
        #   ★ 为什么是【追加】不是返回：打招呼不该拦住正常流程 ——
        #     买家发选座图，我们照样识别+报价，只是多一句招呼。
        #   ★ 只有【真的发出去了】才记时刻 ——
        #     否则一次异常会把"已经打过招呼"永久写进会话，以后再也不打。
        if greet_due:
            try:
                _gtext = tpl.text_for(
                    "GREETING", channel=getattr(result, "channel", None))
                if _gtext:
                    result.messages.append(_gtext)
                    result.template_keys.append("GREETING")
                    session.last_greeting_at = datetime.now().isoformat(
                        timespec="seconds")
            except Exception as _exc:                             # noqa: BLE001
                # ★★ 留痕，不静默。
                #   我第一版这里是 `pass` —— 结果 `tpl` 没 import（NameError）
                #   被它吞掉，打招呼永远发不出去而【一点报错都看不到】。
                #   兜底可以"不让它影响主流程"，但必须让人看得见。
                result.notes.append(
                    f"★ 打招呼没发出去（不影响回复）："
                    f"{type(_exc).__name__}: {_exc}")
        # ★★ 2026-10-14：所有出口都过这里 ⇒ 在这一个地方维护"人工接管"标记，
        #   不用在每处转人工的代码里各写一遍（那样早晚漏一处）。
        #   · 会话进了"待人工" ⇒ 打上接管时刻（幂等：已在接管中不刷新）
        #   · 会话离开了"待人工" ⇒ 清掉（人工已经处理完/流程自己走回去了）
        try:
            from . import manual_control as _mc
            _st = getattr(getattr(session, "conversation", None), "state", None)
            if getattr(_st, "name", "") == "MANUAL":
                _mc.mark(session)
            elif str(getattr(session, "manual_since", "") or ""):
                session.manual_since = ""
        except Exception:                                     # noqa: BLE001
            pass
        session.touch()
        if self.persist:
            self._save()
        return result

    def _greeting_due(self, session: Session, cfg, *,
                      has_image: bool = False) -> bool:
        """
        这次要不要打招呼。

        ★ 三条判据（都要过）：
            ① greeting_configured = 开（默认关 ⇒ 默认行为一个字不变）
            ② 消息类型在 greeting_message_types 里（默认 文字+图片）
            ③ 距上次打招呼已超过 greeting_interval_hours
               （0 = 不限制 ⇒ 每次都算"首次"）
        ★ 文案来自 GREETING 模板（店主确认过的口径，不要自己改写）。
        """
        try:
            rs = cfg.reply
            if not bool(getattr(rs, "greeting_configured", False)):
                return False
            types = list(getattr(rs, "greeting_message_types", None) or [])
            if types:
                want = "图片" if has_image else "文字"
                # ★ 空列表 = 没配 ⇒ 不限制（不是"什么类型都不打"）。
                #   按"都不打"理解的话，配置一被清空招呼就永远发不出去。
                if want not in types:
                    return False
            hours = int(getattr(rs, "greeting_interval_hours", 0) or 0)
            if hours <= 0:
                return True
            last = str(getattr(session, "last_greeting_at", "") or "")
            if not last:
                return True
            try:
                prev = datetime.fromisoformat(last)
            except ValueError:
                return True       # 存坏了 ⇒ 当作没打过（宁可多打一次）
            now = datetime.now()
            # ★ 时区对齐：一边有一边没有会让减法抛异常
            if prev.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=prev.tzinfo)
            elif prev.tzinfo is None and now.tzinfo is not None:
                prev = prev.replace(tzinfo=now.tzinfo)
            return (now - prev) >= timedelta(hours=hours)
        except Exception:                                     # noqa: BLE001
            return False


# ==================================================================== 工厂
def _quote_priority(cfg) -> list:
    """
    读「优先报价模式」清单。

    ★ 为什么要单独一个函数：它要防住两种"读不到"的情况 ——
      ① cfg 是别人传进来的鸭子类型（测试里常见），没有 recognize_quote
      ② 配置里的值不是 list（手改过文件、旧版本遗留）
      两种都返回 [] ⇒ 上层按"没指定模式"处理（用全部规则），
      而不是抛异常把报价整条链路打断。
    """
    try:
        section = getattr(cfg, "recognize_quote", None)
        value = getattr(section, "quote_priority", None)
        return [str(x) for x in value] if isinstance(value, (list, tuple)) else []
    except Exception:                                     # noqa: BLE001
        return []


def build_service(*, tel: str, app_key: str = "", app_secret: str = "",
                  base_url: str | None = None,
                  rules: QuoteRuleEngine | None = None,
                  transport=None, notify_url: str | None = None,
                  model_api_key: str = "", model_name: str = "",
                  persist: bool = True) -> TicketService:
    """
    按配置组装一个可用的服务。

    ★ 这是【生产装配点】：只有这里知道"用哪个 transport、哪份规则、哪个电话"。
      测试可以传假 transport，跑起来完全不碰网络。
    """
    import os
    notify_url = notify_url or os.getenv("LIANGPIAO_NOTIFY_URL", "").strip() or None
    cfg = settings_module.load()
    client = LiangPiaoClient(
        app_key or "NO_KEY", app_secret or "NO_SECRET",
        base_url=base_url or "https://portal-web-v3.liangpiao.net.cn",
        transport=transport)
    api = LiangPiaoAPI(client)
    engine = rules
    # ★ 规则版本号 —— 写进每份报价，进而写进报价记录。
    #   ★ 取设置层里的 revision（运营每改一次规则，它就变），
    #     这样"这笔报价按哪版规则算的"才追得回来。
    try:
        revision = str(getattr(cfg, "revision", "") or "")
    except Exception:                                    # noqa: BLE001
        revision = ""
    if engine is None:
        # ★ 从 store 读，不从 server 读 ——
        #   早先写 `from server import load_rules`，
        #   造成 server → service → server 的循环依赖（审计抓到的）。
        #
        # ★★★ 2026-10-14：「优先报价模式」在这里生效。
        #   取清单里第一个“已启用且已实现”的模式，用它的规则集。
        #   现在只有「简易」实现了，而它=全部规则
        #   ⇒ 过滤后与过滤前是同一批规则，报价逐分不变。
        #   等做「专业报价」时只要在 qr.MODE_RULES 里给它一份规则集，
        #   这个控件立刻就是真的（界面和配置都不用再动）。
        _rules, _mode, _note = qr.select_rules_by_priority(
            store.load_rules(), _quote_priority(cfg))
        if _note:
            print(f"[报价] {_note}")
        engine = QuoteRuleEngine(_rules, revision=revision)
        engine.mode = _mode
    elif not engine.revision:
        engine.revision = revision

    # ★★ 报价记录 + 订单存储。
    #   ★ 只在 persist=True 时开 —— 测试与本地演练不该往盘上写。
    #   ★★ 用 ledger.stores() 拿【同一个实例】而不是自己再开一个：
    #     下单流程写订单、订单管理页读订单，必须是同一份。
    #     各开一个 sqlite 连接指向同一文件虽然也能读写，
    #     但进程内两套缓存/锁不一致，出问题很难查。
    quote_store = None
    order_store = None
    if persist:
        try:
            from application import ledger
            _s = ledger.stores()
            quote_store, order_store = _s.quotes, _s.orders
        except Exception:                                # noqa: BLE001
            quote_store = order_store = None

    # ★ 对话 AI：没传 Key 也照样装 —— 它会转人工。
    #   服务能起来比什么都重要：界面要能打开、关键词要能回。
    ai = build_ai(cfg, api_key=model_api_key,
                  model=model_name or cfg.assistant.model_name)

    return TicketService(api, engine, tel=tel, app_key=app_key,
                         app_secret=app_secret, notify_url=notify_url,
                         ai=ai, persist=persist, quote_store=quote_store,
                         revision=revision, order_store=order_store,
                         rules_provider=_shop_rule_engine if persist else None)


def _looks_like_paid(text: str) -> bool:
    """买家说"付好了/已付款"之类的话。★ 只用于推进状态，不用于判断金额。"""
    return any(w in text for w in ("付好", "已付", "付款了", "付了", "拍好了"))


# ==================================================================== 意图 → 事件
# ★★ 模型判出来的意图名 → 状态机事件。★ 不在表里的意图一律忽略（不猜）。
#
#   ★ 这张表是【唯一】允许模型影响流程的入口。
#     加一条进来之前先问两个问题：
#       ① 这个事件在哪些状态下合法？（看 domain.conversation 的 TRANSITIONS）
#     ② 猜错的后果是什么？
#          · CONFIRM 猜错 → 在 QUOTED 状态下会真的去下单（花买家的钱）
#            ⇒ 所以它必须排在关键词和逐字确认【之后】，且仍然过状态机。
#            ⇒ 而且 prompt 里明确写了"拿不准填 NONE，宁可漏判不可错判"。
#          · PAID 猜错 → 只是把"AWAIT_PAY"当已付款，等真实付款信号来会再对一次。
#   ★ 其余意图（REPRICE / CANCEL）我【暂时不接】——
#     它们各自还有"改多少价""退不退得了"这些参数要谈，
#     现在只有一个名字，接了也只能回一句"转人工"，没有收益。
#     等真实会话数据来（店主会给我）再按真问题加。
INTENT_EVENTS = {
    "CONFIRM": Event.BUYER_CONFIRM,
    # Payment can only be confirmed by verified platform events.
}

# ★★ 只回一句话、【不碰状态机、不碰钱】的意图。
#
#   ★ 为什么单独一张表、而不是塞进 INTENT_EVENTS：
#     INTENT_EVENTS 里的东西会过状态机 → 可能推进流程 → 可能动钱。
#     这张表里的【永远只发一条固定文案】—— 就算模型判错了，
#     代价也只是"多说了一句该说的话"，不会下单、不会改价、不会退款。
#   ★ 所以它不需要"状态合法性"那道闸门（什么都不动，谈不上合法不合法）。
#
#   加一条进来之前先问：这件事是不是【只是说句话】？
#   只要它要改任何状态/金额/订单，就必须走 INTENT_EVENTS 那条路。
INTENT_MESSAGES = {
    # 买家要求改价（真实数据 102 条）。店主选 C：
    # 「告诉他先拍下，拍下后我给你改价」
    "REPRICE_REQUEST": "REPRICE_REQUEST",
}


def _overlap(a: str, b: str) -> int:
    """a、b 的最长公共子串长度（用于"都不完整包含"时的贴切度）。

    ★ 为什么需要它：买家可能把两个问句的说法混着说
      （"出票失败" 同时出现在「出票失败怎么办」和「出票失败退款了吗」里）。
      这时按公共子串长度挑更贴切的那条。
    ★ 朴素实现（O(n*m)）：问句都很短，够用；别为这点事引入依赖。
    """
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _faq_lookup(keyword: str, *, channel: str | None = None) -> str | None:
    """
    给模型的 FAQ 查询工具用。

    ★ 它和"关键词回复"共用同一份数据（qa.py），但【用法不同】：
        关键词回复：买家原话【精确命中】关键字才回 —— 逐字可控
        FAQ 工具  ：模型拿一个关键词来【查】—— 允许模糊

      两件事不能混：前者要"一个标点都不能变"，后者要"能查到相关条目"。

    ★★ 2026-10-14：知识库升级。
      每条规则多了一个 questions（"买家会怎么问"），这个函数优先按它找。
      原因：知识库里的关键字是「002」这种编号，买家问的是「多久能出票」，
      按关键字子串匹配永远命不中 ⇒ 模型查不到知识库。
      ★ 注意 questions【只在这里用】——
        match()（决定发什么话那条路）保持"逐字可控"，不受影响。

    ★ 找不到就返回 None —— 工具的返回值会是「FAQ 里没有…，这条要转人工」，
      让模型知道"没这条规则"，而不是自己编一个。
    """
    kw = (keyword or "").strip()
    if not kw:
        return None

    # ★★ 2026-10-14 顺序很重要（踩过）：先问句、后关键字。
    #   原来第一步是 qa_module.reply(kw)，而它是【子串匹配】⇒
    #     「取票码用不了怎么办」含「取票」⇒ 立刻返回 014（怎么取票），
    #     「出票失败怎么办」含「出票」⇒ 立刻返回 002（出票时长）
    #     ⇒ 特意为这两个问题写的 005 / 011 永远没机会。
    #   ⇒ 正确优先级：具体压笼统 —— 问句命中优先于子串命中。
    #   ★ 关键字那条【没删】，放在最后兜底：按编号查（"002"）照旧能用。

    # 1) 按【同义问句】找（这是知识库真正能用的关键）
    #   为什么必须有这一步：知识库里的关键字是「002」这种编号，
    #   而买家问的是「多久能出票」——
    #   原来按关键字子串匹配永远命不中 ⇒ 模型查不到 ⇒ 只能自己编或转人工。
    #   ⇒ 每条规则带一组"买家会怎么问"，这里先按它找。
    #   ★ 为什么问句排在"关键字包含"前面：
    #     问句是特意为这个问题写的，比子串碰撞准。
    #     例："出票"这个关键字会碰撞一堆问法，
    #     而"多久能出票"明确指向 002 那条完整答案。
    #   ★ 双向包含：买家的词可能比问句长（"请问多久能出票啊"），
    #     也可能短（"多久出票"）—— 两种都算命中。
    # ★ 不是"第一个命中就返回"——那样会让【笼统的问句】抢走具体的。
    #   实测：「取票码用不了怎么办」命中了 014 的「取票码在哪」，
    #   而正确答案是 005（取票码用不了：换人工、录音、凭录音退款）。
    #   ⇒ 按【匹配长度】打分，取最贴切的那条：
    #       完全相等            → 分最高
    #       问句被买家整句包含   → 按问句长度给分（越长越具体）
    #       买家的话被问句包含   → 按买家话长度给分
    # ★★ 打分口径：这次查询【覆盖了问句的多少字】—— 覆盖越多越贴切。
    #   为什么不用"问句长度"：笼统的问句可以很长，但只覆盖一点点。
    #   例：「取票码无效」查
    #         「取票码无效」(全覆盖 5 字)  应赢
    #         「取票码在哪」(只覆盖"取票码" 3 字)
    #   完全相等 / 问句整句被包含 → 给一个大基础分，
    #   这样"很短的精确问句"不会被"很长的笼统问句"压过去。
    best = None       # (分数, 问句, 规则)
    for rule in qa_module.QA_RULES:
        for q in (getattr(rule, "questions", None) or []):
            q = str(q).strip()
            if not q:
                continue
            if q == kw:
                score = 10000 + len(q)
            elif q in kw:
                # 问句整句被买家的话包含 ⇒ 覆盖了 len(q) 个字
                score = 5000 + len(q)
            elif kw in q:
                # 买家的话是问句的一部分 ⇒ 覆盖了 len(kw) 个字
                score = 100 + len(kw)
            else:
                # 都不包含，试试公共子串长度（如"出票失败"在两句里都有）
                score = _overlap(kw, q)
                if score < 2:
                    continue
            if best is None or score > best[0]:
                best = (score, q, rule)
    if best is not None:
        return qa_module.render_rule(best[2], channel=channel)

    # 2) 问句都没命中 ⇒ 退回原来的关键字路（按编号查、精确关键字）
    hit = qa_module.reply(kw, channel=channel)
    if hit:
        return hit

    # 3) 模糊：关键词是不是某条的内容/关键字的一部分
    #    ★ 一个条目可能有多个关键字（| 分隔），逐个比
    for rule in qa_module.QA_RULES:
        for k in str(rule.keyword or "").split("|"):
            k = k.strip()
            if not k:
                continue
            if kw in k or k in kw:
                return qa_module.render_rule(rule, channel=channel)
    # 4) 再宽一点：在内容里找
    for rule in qa_module.QA_RULES:
        if kw and kw in (rule.content or ""):
            return qa_module.render_rule(rule, channel=channel)
    return None


if __name__ == "__main__":
    print("=" * 74)
    print("接线自检：确认 service 能装配起来")
    print("=" * 74)
    svc = build_service(tel="13800000000", app_key="K", app_secret="S",
                        persist=False)
    print(f"  ✓ TicketService 装配成功")
    print(f"    flow.tel       = {svc.flow.tel}")
    print(f"    报价规则数      = {len(svc.flow.rules.rules)}")
    print(f"    持久化         = {svc.persist}")
    s = svc.session("demo-chat")
    print(f"  ✓ 会话可取：state={s.conversation.state.value}")
    out = svc.on_message("demo-chat", "999")
    print(f"  ✓ 关键词回复：{out.messages[0][:40] if out.messages else '（无）'}")
    out = svc.on_message("demo-chat", "随便说点啥")
    print(f"  ✓ 兜底回复：{out.messages[0][:40] if out.messages else '（无）'}")


def _shop_rule_engine(conv):
    """Resolve rules from the conversation identity for each quote, never UI state."""
    cfg = settings_module.load()
    tenant, shop = str(conv.tenant_id or ""), str(conv.shop_id or "")
    if bool(tenant) != bool(shop):
        raise ValueError("报价缺少完整店铺身份")
    rows, mode, _ = qr.select_rules_by_priority(store.load_rules(tenant, shop), _quote_priority(cfg))
    engine = QuoteRuleEngine(rows, revision=str(getattr(cfg, "revision", "") or ""))
    engine.mode = mode
    return engine
