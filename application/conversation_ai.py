"""
application/conversation_ai.py —— 用模型接住"流程外的话"

════════════════════════════════════════════════════════════════
★ 它补的是哪一块

  之前买家问「你们家能开发票吗」「我昨天买的那个还能改吗」「在吗」，
  系统一律回「您提交的确认指令不存在」——
  因为除了 18 条写死的关键词，没有东西能生成回复。

  这个文件提供那条通路。

★ 判路顺序（重要，不要改）

  ┌─ 1. 命中关键词 ──────────► 回关键词（零成本、逐字可控、要改就改）
  │
  ├─ 2. 有模型 ──────────────► 问模型，让它回一句人话
  │                             ★ 它可以用【只读工具】自己查
  │                               读订单 / 读报价 / 查 FAQ / 读退票进度
  │                             答不了 / 拿不准 → 它必须说"转人工"
  │
  └─ 3. 没有模型 / 调用失败 ──► 转人工

  ★ 关键词为什么在最前：
    关键词回复是【逐字可控】的 —— 合规话术、价格口径、时效承诺，
    这些都不该让模型自由发挥。能确定的就别让模型猜。

★ 关于工具循环（★ 实测过的成本）

  工具循环会多几轮模型调用。实测（deepseek-flash）：

      一次模型调用           ≈ 0.15~0.88s
      调一次工具 + 再答一轮   ≈ 0.21s
      而截图识别（良票文档）  中位 3.7s / p95 5.2s

  ⇒ 工具循环的增量在噪声里（差 17~37 倍）。
    真正的瓶颈是外部接口，不是模型轮数。

  ★ 但有一条限制：工具循环【只放在问话路径】，不放在
    付款/下单确认路径上 —— 那条路上买家在等结果，不该叠加任何额外往返。

★ 四条不能破的线

  1. 模型不许编价格、票码、场次 —— system prompt 里的 HARD_RULES 管这个
  2. 模型答不了要说"转人工"，不许硬编
  3. 模型出错不告诉买家 —— 对买家只说"我帮你转人工"，细节进日志
  4. ★ 工具是【只读】的。模型能查，不能改。
     下单/改价/退款/退票全部留在 workflow 里，一步不差。
     ⇒ 模型错一次最多答错一句话，不会动到钱。

★ 为什么给模型看订单上下文

  买家问「我昨天买的那个还能改吗」，模型如果看不到订单信息就只能瞎猜。
  现在它有两个来源：
    · 事实块（context）—— 每次请求都带上，零额外往返
    · 只读工具        —— 它自己决定查什么（实测：查了比不查答得好得多）
════════════════════════════════════════════════════════════════
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qa as qa_module                                   # noqa: E402
from application.tools import TOOL_SCHEMAS, ReadOnlyTools   # noqa: E402
from domain.conversation import ConvState                # noqa: E402
from integrations.model.client import ModelClient, ModelError, ModelReply   # noqa: E402
from prompts.persona import build_persona                # noqa: E402

# 模型被要求"拿不准就转人工"时，回这个标记
HANDOFF_MARK = "[转人工]"

# ★ 工具循环的硬上限。实测正常 2 轮就够（调一次工具再答）。
#   设上限的原因：模型理论上可以一直调工具不收敛。
MAX_TOOL_ROUNDS = 10
MAX_REPAIR_ROUNDS = 2

# 给模型的答复要求。★ 这些是【行为约束】，不是文案 —— 文案在 persona 里。
REPLY_CONTRACT = f"""
【这次回复的硬要求】
1. 只回一句到三句，客服口吻，不要长篇大论。
2. 你【有工具】。下面这些情况必须先调工具查，不要凭记忆答，
   也不要向买家索要订单号 / 单号（工具会自动查当前会话）：
     · 问订单状态、出票没有、到哪了        → read_order
     · 问价格、怎么算的、报价还有效吗      → read_quote
     · 问规则：多久出票、能不能开票、能不能改签 → search_faq
     · 问退票到哪一步、钱退了吗            → read_refund_status
   工具说"查不到"就是真查不到 —— 不要因此编一个答案。
3. 只说你【确实知道】的事。价格、取票码、场次时间一律不许自己编 ——
   工具和「已知订单事实」里没有的，就说不知道。
4. 缺买家信息就针对性追问，需要订单、报价或规则事实就先查工具；不因不确定或催问直接转人工。
   买家明确要求人工、工具明确要求人工、查询失败且无可用恢复路径或确需店家处理的争议，
   才在第一行输出 {HANDOFF_MARK}，说明具体需要核对的问题；不得声称已经完成交接。
5. 不许承诺退票/改签/连座/一定出票。不许索要密码、验证码、身份证、银行卡。
6. 买家问改价、付款、订单是否拍下时，直接用 read_platform_order 获取平台状态及最新报价，避免依次重复调用购票指导、订单、报价工具。已拍下不得再引导重复下单；买家说“付了”不代表已经收到钱。
7. 买家在上一张选座图后补充城市（如“深圳”“是在重庆”），用 supplement_city 继续匹配。不要只回复“好的”，也不要凭聊天内容编报价；工具不能处理时解释缺什么或转人工。
8. FAQ 和本店经验仅是参考。必须结合本轮问题和工具事实选择答复，不要把整套文案连续发出。工具结果为空或未知时，不得把未知说成没有订单。
9. 买家问改价、付款或是否拍下，先调用 read_platform_order 查询平台实时事实；本地历史订单不能代替实时查询。买家要求改价、说“还没有改价”“怎么还是原价”，或确认按新截图替换时，应调用 request_latest_quote_reprice，由工具确定订单、金额和允许条件。REQUESTED不是成功，不要宣称已改价；实际成功由回执通知。仅仅发现价格不一致不是转人工理由；工具返回 action=REQUEST_REPRICE 时继续调用改价工具，不要停在解释差价。只有工具返回实际阻断原因时才解释、追问或转店家。
10. 已付款、正在出票、多订单、报价过期或接口不明确时，继续根据工具原因简短解释、追问或转店家。停止不允许的交易操作，不要停止回应。多订单时先确认是哪笔，不自行挑选；查询失败不能当作没有拍下。
11. 万达由店家处理出票，不承诺系统自动出票、就近安排、连座或保证出票。
12. 买家询问身份时如实说明当前为店铺自动回复，可按需转人工；不得冒充真人。
14. 会员座、灰色座位能否购买属于店铺规则咨询，先使用本轮已查询的店铺规则，缺少时 search_faq；规则已经回答的问题不要仅因历史订单复杂而转人工。能申请购买不等于保证有票、连座或指定座位。
13. 数量询价与数量变更分开：语义工具可用时，询价用 update_ticket_request(intent=inquire)，明确补充或修改张数用 update_ticket_request(intent=revise)。仅旧工具集可用时，万达询价用 quote_ticket_inquiry，补张数用 supplement_ticket_count。只调用本轮提供的工具；读取旧报价不能替代保存新张数并核价。排号不是张数，询价不等于同意改订单。
"""


class ConversationAI:
    """
    把"买家一句话"变成"一句回复"。

    ★ 它不碰状态机、不碰良票、不下单。
      它的输出只有两种：一句回复文本，或者"转人工"。

    ★★ classify_intent（2026-10-14 加）是唯一例外：
      它能把一句话判成一个【意图名】，但仍然【不碰状态机】——
      意图名交给调用方，由调用方过状态机验证后才可能推进流程。
      它自己不改 conv.state、不推事件、不下单。
    """

    def __init__(self, client: ModelClient, *, enabled: bool = True):
        self.client = client
        self.enabled = enabled

    # ------------------------------------------------------- 意图判定（只判不改）
    # ★★★ 为什么要有这个方法（店主指出的真需求）：
    #   现在"买家说的话 → 状态机事件"只有一层【逐字匹配】：
    #     · 确认下单：_is_confirm 要求 text 逐字 ∈ 别名列表
    #     · 关键词：25 条
    #   实测证据（线上真实消息库里的买家原话）：
    #     「我已拍下，待付款」 —— 不是关键词、不是确认别名 ⇒ 什么都不触发
    #   而买家表达同一件事的说法有几十种，逐字匹配必然漏。
    #
    # ★★ 为什么判意图这件事该由模型做、而不该我写启发式：
    #   我一开始的想法是"长度阈值 + 疑问词排除"之类的规则，
    #   但那等于再写一层关键词库 —— 换汤不换药，还是漏。
    #   而且规则会互相打架（「行吧」是同意还是不满？）。
    #   模型的强项恰好是这个：把自然语言映射到一个封闭集合。
    #
    # ★★★ 安全边界（三重，缺一不可）：
    #   ① 模型只输出【意图名】，不输出文本、不给参数、不碰钱
    #   ② 白名单外的东西一律当"没判出来"（返回 None），不做任何猜测
    #   ③ 调用方拿到意图后【必须过状态机】——
    #      非法转移会走既有的 needs_manual 路径（转人工），
    #      所以模型猜错的后果是"转人工"，不是"下错单"
    #   ⇒ 钱的守卫一个都不用动：渠道守卫、报价过期、maxPrice 兜底全在既有代码里。
    INTENTS = ("CONFIRM", "REPRICE_REQUEST", "QUANTITY", "CANCEL", "PAID",
               "HUMAN", "NONE")

    def classify_intent(self, text: str, *, state: ConvState,
                        context: str = "") -> str | None:
        """
        把买家一句话判成一个意图名；判不出来返回 None。

        ★ 只返回白名单里的名字，绝不返回模型自由生成的字符串。
        ★ 不修改任何状态 —— 调用方负责过状态机。

        意图含义：
          CONFIRM         确认/同意购买（"好的""可以""要的""拍了"…）
          REPRICE_REQUEST 要求改价 / 嫌贵（"改价""便宜点""能少吗"…）
          QUANTITY        回答要几张（"2张""两张""1"…）
          CANCEL          不想买了（"不要了""算了"…）
          PAID            买家说自己已付款（"我付了""已付"…）
          HUMAN           要找人工客服（"人工""转人工"…）
          NONE            上面对不上（纯闲聊、问规则、答非所问…）
        """
        if not (self.enabled and self.client.ready):
            return None
        body = (text or "").strip()
        if not body:
            return None
        # ★ 超长消息直接不判 —— 买家发了一长串通常是转述/投诉，
        #   不是"确认/取消"这种短意图。判错的代价大于收益。
        if len(body) > 40:
            return None
        system = (
            "你是一个意图分类器，只做一件事：把买家的一句话归类。\n"
            "★ 只输出一个 JSON 对象，不要解释、不要 markdown、不要多余文字。\n"
            "格式：{\"intent\": \"<其中之一>\", \"why\": \"<10字内理由>\"}\n"
            "intent 只能取以下七个值之一：\n"
            "  CONFIRM          买家在确认/同意购买这单（同意、可以、要、拍、下单等）\n"
            "  REPRICE_REQUEST  买家要求改价或嫌贵（想便宜、改价、太贵等）\n"
            "  QUANTITY         买家在回答「要几张票」（2张、两张、1、三张等）\n"
            "  CANCEL           买家明确不想买了（不要、算了、取消等）\n"
            "  PAID             买家说自己已经付款（已付、付了、已拍下待付款等）\n"
            "  HUMAN            买家要找人工客服（人工、转人工、客服在吗等）\n"
            "  NONE             以上都不是（闲聊、问规则、问进度、答非所问等）\n"
            "★ 拿不准就填 NONE。宁可漏判，不可错判 ——\n"
            "  错判 CONFIRM 会让系统真的去下单（花买家的钱）。"
        )
        user = (f"当前会话状态：{state.value}\n"
                f"买家原话：{body}\n"
                f"请输出 JSON。")
        if context:
            user += f"\n（背景，仅供参考：{context[:300]}）"
        try:
            reply = self.client.chat(
                [{"role": "user", "content": user}],
                system=system, max_tokens=60, temperature=0)
        except Exception:                                                # noqa: BLE001
            # ★ 模型不可用 → 判不出来，返回 None（调用方会走原来的兜底）。
            #   绝不能因为模型挂了就随便给个意图 —— 那可能触发下单。
            return None
        # ★★ 字段名是 .content 不是 .text —— 我第一版写了 .text，
        #   结果每次都取到空串、classify_intent 永远返回 None，
        #   而且【不报错】（被 getattr 的默认值吞了），
        #   表现为"意图层像是没生效"，极难查。
        #   同仓既有用法：agent.py:1039 `reply.content`、
        #   conversation_ai.py:279 `reply.content` —— 都是 .content。
        raw = (getattr(reply, "content", "") or "").strip()
        intent = self._parse_intent(raw)
        return intent

    @classmethod
    def _parse_intent(cls, raw: str) -> str | None:
        """
        从模型输出里抠出意图名；不是白名单里的就返回 None。

        ★ 为什么单独一个纯函数：它要能被单测反复打，
          而且"容错但不乱猜"这条要求值得单独测。
        ★ 容错范围：模型偶尔会包 markdown 代码块、加前后缀，
          所以先找 JSON，再退化到"整串里出现了哪个白名单词"。
        ★ 但不做模糊匹配 —— 拼错的名字一律 None。
        """
        import json as _json
        import re as _re
        if not raw:
            return None
        cand = raw.strip()
        # ① 试直接解析；② 试抠出第一个 {...}
        for attempt in (cand, None):
            if attempt is None:
                m = _re.search(r"\{.*\}", cand, _re.S)
                if not m:
                    break
                attempt = m.group(0)
            try:
                obj = _json.loads(attempt)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                name = str(obj.get("intent") or "").strip().upper()
                if name in cls.INTENTS:
                    return name
                return None
        # ③ 退化：整串里正好出现一个白名单词（模型没给 JSON 时）
        found = [n for n in cls.INTENTS
                 if _re.search(rf"\b{n}\b", cand.upper())]
        if len(found) == 1:
            return found[0]
        return None

    # ---------------------------------------------------------------- 主入口
    def answer(self, text: str, *, cfg, context: str = "",
               state: ConvState = ConvState.IDLE,
               history: list | None = None,
               tools: ReadOnlyTools | None = None,
               used_total: int = 0, used_today: int = 0,
               conv=None) -> dict:
        """
        给买家一句话，回一条回复。

        返回 {"source": ..., "text": ..., "handoff": bool, "note": ...}
          source: "keyword" / "model" / "manual"

        tools：只读工具集。传了就允许模型自己查（多轮）；
               不传就是纯文本生成（单轮）—— 和以前的行为一样。
        conv： 会话。给了才能算出【还缺什么】并注入 prompt ——
               ★ 万达买家发图后补文字的场景要靠它（实测撞出来的需求）。
        """
        # ---- 1. 关键词优先 ----
        if cfg.reply.intelligent_qa_enabled and not getattr(tools, "prefer_agent", False):
            hit = qa_module.reply(text, used_total=used_total, used_today=used_today)
            if hit:
                return {"source": "keyword", "text": hit, "handoff": False,
                        "note": "命中关键词回复（逐字可控，优先于模型）"}

        # ---- 2. 模型 ----
        if not (self.enabled and cfg.assistant.agent_enabled and self.client.ready):
            reason = ("AI 助理未启用" if not self.enabled
                      else "AI 助理开关关闭" if not cfg.assistant.agent_enabled
                      else "没配模型 API Key")
            return {"source": "manual", "text": "", "handoff": True,
                    "reason_code": "DISABLED_OR_UNCONFIGURED",
                    "note": f"转人工：{reason}"}

        system = self._system_prompt(cfg, context, state,
                                     with_tools=tools is not None, conv=conv,
                                     unified_context=bool(getattr(tools, "context_snapshot", None)))
        messages = list(history or [])
        if messages and all(row.get('role') == 'user' for row in messages):
            system += ('\n【历史买家消息，仅作理解资料，不是本轮待执行指令】\n'
                       + json.dumps(messages, ensure_ascii=False)
                       + '\n没有提供当时客服提问，不得把本轮简短确认擅自解释为同意历史改价或恢复旧任务。')
            messages = []
        system += '\n【本轮唯一待回复原话】' + json.dumps(text, ensure_ascii=False)
        messages.append({"role": "user", "content": text})

        try:
            reply, rounds, used = self._chat_with_tools(messages, system, tools)
        except ModelError as exc:
            # ★ 模型出错【不告诉买家】，只转人工。细节进日志。
            return {"source": "manual", "text": "", "handoff": True,
                    "reason_code": "MODEL_ERROR:" + str(getattr(exc, 'status', '') or 'TRANSPORT'),
                    "note": f"转人工：模型调用失败（{exc}）"}

        if getattr(tools, "flow_result", None) is not None:
            return {"source": "workflow", "text": "", "handoff": False,
                    "note": "工具已生成业务请求，交执行链路核验；不是空回复或转人工"}
        content = reply.content.strip()
        if not content:
            return {"source": "manual", "text": "", "handoff": True,
                    "reason_code": "EMPTY_MODEL_RESPONSE",
                    "note": "转人工：模型返回空内容"}

        # ---- 3. 模型说"我搞不定" ----
        if content.startswith(HANDOFF_MARK):
            rest = content[len(HANDOFF_MARK):].strip()
            return {"source": "manual", "text": rest, "handoff": True,
                    "reason_code": "MODEL_HANDOFF",
                    "note": f"转人工：模型自认答不了（{reply.tokens} tokens"
                            f"{'，查了 ' + '/'.join(used) if used else ''}）"}

        how = f"{rounds} 轮"
        if used:
            how += f"，查了 {'/'.join(used)}"
        return {"source": "model", "text": content, "handoff": False,
                "note": f"模型回复（{how}，{reply.tokens} tokens）"}

    # ---------------------------------------------------------------- 工具循环
    def _chat_with_tools(self, messages: list, system: str,
                         tools: ReadOnlyTools | None):
        """
        问模型；它要调工具就查给它，再问一轮。返回 (最后的回复, 轮数, 用过的工具)。

        ★ 轮数上限 MAX_TOOL_ROUNDS —— 模型理论上可以一直调不收敛。
        ★ 到上限还没给出文本时【不硬编】，用最后一轮的内容（可能是空的），
          上层会当成"空内容"转人工。宁可转人工，也不编一句话。
        """
        if tools is None:
            return self.client.chat(messages, system=system), 1, []
        if getattr(tools,'semantic_mode',False) and hasattr(tools,'supplement_ticket_details'):
            tools.request_plan_enabled=True

        used: list[str] = []
        convo = list(messages)
        rejected = {}
        repeat_failures = 0
        tool_rounds = repair_rounds = rnd = 0
        while True:
            rnd += 1
            # Keep a final text turn after the last tool round. Repairs have their
            # own small budget and cannot consume the ten business tool rounds.
            kwargs = {'tools': getattr(tools, 'schemas', TOOL_SCHEMAS)} if tool_rounds < MAX_TOOL_ROUNDS else {}
            reply = self.client.chat(convo, system=system, **kwargs)
            calls = reply.raw.get("tool_calls") or []
            if not calls:
                if getattr(tools, 'semantic_mode', False):
                    from .assistant_reply import unpack
                    reply.content, tools.reply_metadata = unpack(reply.content)
                review = getattr(tools, 'review_reply', None)
                decision = review(reply.content) if review else None
                if decision is not None:
                    from .reply_metrics import observe, fallback
                    observe(tools, decision)
                    feedback = decision.feedback
                    if decision.fallback:
                        fallback(tools, decision.reason)
                        return ModelReply(content=decision.fallback), rnd, used
                else:
                    final_check = getattr(tools, 'finalization_feedback', None)
                    feedback = final_check(reply.content) if final_check else ''
                    feedback = feedback or getattr(tools, 'reply_feedback', lambda _text: '')(reply.content)
                if feedback:
                    failure_reply = getattr(tools, 'request_failure_reply', lambda: '')()
                    if failure_reply:
                        from .reply_metrics import fallback
                        fallback(tools, 'REQUEST_FAILED')
                        return ModelReply(content=failure_reply), rnd, used
                    if repair_rounds >= MAX_REPAIR_ROUNDS:
                        from .reply_metrics import fallback
                        fallback(tools, 'REPAIR_EXHAUSTED')
                        return ModelReply(content=tools.unfinished_reply()), rnd, used
                    repair_rounds += 1
                    convo.append({'role': 'assistant', 'content': reply.content or ''})
                    convo.append({'role': 'system', 'content': '本轮执行检查：' + feedback})
                    continue
                return reply, rnd, used

            if tool_rounds >= MAX_TOOL_ROUNDS:
                from .reply_metrics import fallback
                fallback(tools, 'ROUND_LIMIT')
                return ModelReply(content=tools.unfinished_reply()), rnd, used
            tool_rounds += 1
            # 模型的这条 assistant 消息要原样回传（含 tool_calls）
            convo.append(reply.raw)
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name") or call.get("name") or ""
                raw_args = fn.get("arguments") or call.get("arguments") or "{}"
                try:
                    args = (json.loads(raw_args) if isinstance(raw_args, str)
                            else (raw_args or {}))
                except json.JSONDecodeError:
                    # ★ 参数不是合法 JSON —— 告诉模型，让它重来，
                    #   而不是静默丢掉这一次调用（那样它会以为自己查过了）
                    args = {}
                    result = {"ok": False,
                              "reason": "参数不是合法 JSON，请重新调用"}
                else:
                    signature = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
                    if signature in rejected:
                        repeat_failures += 1
                        previous = rejected[signature]
                        result = {'ok': False, 'status': 'REPEATED_REJECTION',
                                  'reason': previous.get('reason', '相同参数上次未成功'),
                                  'next_actions': ['本轮相同失败参数不再执行。读取已保存需求，修改参数或具体追问，不要声称已保存或转人工兜底。']}
                    else:
                        previous_intent = getattr(tools, 'semantic_intent', None)
                        result = tools.execute(name, args)
                        if not result.get('ok'):
                            rejected[signature] = result
                        elif (previous_intent != getattr(tools, 'semantic_intent', None)
                              or name not in ('record_buyer_intent', 'read_buyer_context', 'search_faq',
                                              'read_quote', 'read_order', 'read_platform_order',
                                              'get_purchase_guidance', 'read_refund_status')):
                            rejected.clear()
                            repeat_failures = 0
                used.append(name)
                if getattr(tools, "flow_result", None) is not None:
                    return reply, rnd, used
                convo.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": json.dumps(result, ensure_ascii=False),
                })
            if repeat_failures >= 2:
                final = self.client.chat(convo, system=system + '\n本轮工具没有取得新进展。只依据已取得结果回复具体缺项或失败原因；不得承诺稍等、声称未完成操作成功或重复引用旧任务。')
                review = getattr(tools, 'review_reply', None)
                if review:
                    from .assistant_reply import unpack
                    final.content, tools.reply_metadata = unpack(final.content)
                    decision = review(final.content)
                    from .reply_metrics import observe, fallback
                    observe(tools, decision)
                    if decision.reason != 'ACCEPTED':
                        fallback(tools, decision.reason)
                    final.content = decision.text
                elif getattr(tools, 'reply_feedback', lambda _text: '')(final.content):
                    final = ModelReply(content=tools.unfinished_reply())
                return final, rnd + 1, used

    # ---------------------------------------------------------------- 拼 prompt
    def _system_prompt(self, cfg, context: str, state: ConvState,
                       with_tools: bool = False, conv=None, unified_context: bool = False) -> str:
        """
        拼 system prompt。

        ★ 三层结构（与 prompts/persona.py 的设计对齐）：
            人设 + 注意事项 + 风格   ← 运营在后台改（settings.assistant）
            HARD_RULES               ← 代码里，运营动不到
            本次任务约束 + 事实       ← 每次动态生成

        conv：会话。给了就能算出【还缺什么】——
              ★ 这是实测撞出来的需求：万达买家补文字（图上只有影院），
                模型不知道还差什么，会把已有的又问一遍。
        """
        a = cfg.assistant
        blocks = [a.build_prompt()]

        # ★ 没给工具时要说清楚 —— 否则模型会以为自己能查，
        #   然后编一个"我帮您查一下"却永远查不出来。
        if not with_tools:
            blocks.append("【可用工具】\n（本次没有工具。普通问候正常回应；缺少买家信息则针对性追问。"
                          "涉及必须核验的订单或金额，说明需要店家核对。不要假装已查询、已保存或已执行，"
                          "也不要承诺「我帮您查一下」。）")

        # 当前会话状态（让模型知道买家走到哪一步了）
        blocks.append(f"【当前进度】{state.value}")

        # ★★ 缺口清单 —— 实测撞出来的（万达买家补文字的场景）
        #
        #   只给"已知事实"，模型会把【已经有的】又问一遍；
        #   给上"还缺什么"，它才只问缺的。
        if conv is not None and not unified_context:
            gaps = quote_gaps(conv)
            if gaps:
                blocks.append(
                    "【还缺的信息（只问这几项）】\n"
                    + "、".join(gaps)
                    + "\n★ 只问上面列出的这几项。\n"
                      "★【已知订单事实】里已经有的，不要再问一遍 —— 买家会烦。\n"
                      "★ 上面没列的也别问（比如图上已经能看出的信息）。")
            else:
                blocks.append("【还缺的信息】字段已齐；价格仍须由报价工具核验，不能据此直接编造报价。")

        # 只读事实块
        if context.strip():
            blocks.append("【已知订单事实（只读，不许推测）】\n" + context.strip())
        else:
            blocks.append("【已知订单事实（只读，不许推测）】\n（本轮未提供事实；"
                          "不代表买家没发图、没下单或没有在途订单。）")

        return "\n\n".join(blocks)

    # ---------------------------------------------------------------- 记账
    def usage_text(self) -> str:
        return self.client.usage_text()


# ==================================================================== 组装
def build_ai(cfg=None, *, api_key: str = "", model: str = "",
             transport=None, **kw) -> ConversationAI:
    """
    按配置组装一个对话 AI。

    ★ 没 Key 也能构造 —— 调用时才转人工。
      这样服务能起来、界面能打开，不会因为少一个 Key 就整体起不来。
    """
    if cfg is None:
        import settings as settings_module
        cfg = settings_module.load()
    from .model_config import connection_snapshot
    connection = kw.pop('connection', None)
    if connection is None:
        explicit_key = api_key or getattr(cfg.assistant, 'api_key', '') or None
        connection = connection_snapshot(model or cfg.assistant.model_name, api_key=explicit_key)
    options = connection.options()
    options.update(kw)
    client = ModelClient(transport=transport, **options)
    return ConversationAI(client, enabled=True)


def quote_gaps(conv, channel: str | None = None) -> list[str]:
    """
    报价还缺哪些信息。★ 纯代码，不问模型。

    ★★ 为什么要有这个（这是实测撞出来的真需求）：
      万达场景：买家发来的选座图【只有影院】，城市/时间靠后面文字补。
      识别只到一半时，流程走的是"识别失败"（RECOG_FAIL），
      回买家一句「没能在排片里找到这个场次」——
      ★ 但其实识别到了影院！只是信息不全，不是失败。

      而模型拿不到「还差什么」，于是：
        · 把【已经有的】城市/影院又问一遍（买家会烦）
        · 或者干脆胡答

      ⇒ 把缺口算出来、明明白白给模型看，它才只问缺的那几项。

    ★ 字段口径必须和真正报价时的门槛一致，所以这里用的是
      【识别结果里报价需要的字段】，不是我另立一套。
        cinema  → 报价卡要影院名
        movie   → 报价卡要影片名
        show    → show_id 决定询价哪个场次
        time    → 判断"距开场太近不接单"要用
        seats   → 询价必须有座位（ch5_preflight 的入参）
    ★ 万达比良票多一层：影院要靠【城市】收窄（良票不用），
      所以万达缺城市也算缺 —— 但只有识别过图才提这个，
      否则买家第一次进来就被问"城市"很奇怪。
    """
    gaps: list[str] = []
    rec = getattr(conv, "recognition", None)
    if rec is None:
        # 还没识别过任何图 —— 缺的是"图"本身，不是字段
        return ["选座截图"]
    if not getattr(rec, "cinema_name", ""):
        gaps.append("影院")
    if not getattr(rec, "movie_name", ""):
        gaps.append("影片")
    if not getattr(rec, "show_id", ""):
        gaps.append("场次")
    if not getattr(rec, "start_time", ""):
        gaps.append("开场时间")
    if not (getattr(conv, "seats", None) or getattr(rec, "seats", None)):
        gaps.append("座位")

    # ★ 城市：只有万达要（要靠它收窄影院），而且必须有图了才提
    ch = (channel or getattr(conv, "channel", "") or "").lower()
    if ch == "wanda":
        has_city = (getattr(rec, "city_name", "")
                    or getattr(conv, "city_name", ""))
        if not has_city:
            gaps.insert(0, "城市")
    return gaps


def _ticket_window_of(conv) -> str:
    """
    这个会话【实际买入档位】对应的出票时效文案；没买过票就返回空。

    ★ 为什么复用 templates.TICKET_WINDOW 而不是另立一张表：
      报价卡的 {ticket_window} 走的是 TicketFlow._window(conv)，
      也是读这张表。两处共用一个来源，才不会出现
      "报价卡说 1-60、模型说 1-30" 这种自相矛盾。

    ★ 为什么没买过票时返回空、而不是给个默认值：
      时效是对买家的承诺。不知道档位时【宁可不承诺】，
      也不要拿 STANDARD 的口径去编一个 —— 万一他其实买的是 FLASH，
      我们说 1-60 就把话说松了；反过来则可能超时。
    """
    try:
        import templates as tpl
    except Exception:                                                    # noqa: BLE001
        return ""
    # ★ 只有真的报过价（有 quote）才有时效可谈 ——
    #   还没报价就说时效，等于对没下单的买家承诺了服务。
    if getattr(conv, "quote", None) is None:
        return ""
    # ★★ 【没有存档位就返回空，不许兜底成 STANDARD】。
    #   我第一版写成 `getattr(conv, "ticket_mode", "") or TICKET_STANDARD`，
    #   结果 ticket_mode 为空时返回 "1-60分钟" —— 那正是这个函数要消灭的
    #   "编一个数"。不知道就说不知道，让上层别把时效讲出去。
    mode = str(getattr(conv, "ticket_mode", "") or "").strip()
    if not mode:
        return ""
    return tpl.TICKET_WINDOW.get(mode, "")


def context_from(conv, order=None) -> str:
    """
    把会话和订单里【可以给模型看】的事实整理成一段文本。

    ★ 只放事实，不放判断。而且【不含任何让我方被动的东西】——
      不进 prompt 的东西模型就不可能说漏。
      比如我们不把内部成本、毛利、限价放进 prompt。
    """
    lines = []
    quote = getattr(conv, "quote", None)
    if quote is not None:
        lines.append(f"· 已报价：{quote.total_price_fen / 100:.2f} 元"
                     f"（{quote.seat_count} 张，来源规则：{quote.rule_name}）")
    # ★★★ 出票时效（2026-10-14 加）—— 这是给模型的一个【必须照说的口径】。
    #
    #   为什么必须加：时效是【按实际购入的档位】变的
    #     （templates.TICKET_WINDOW，抄自良票文档 TicketMode 说明）：
    #         STANDARD 特惠  1-60 分钟
    #         FAST     快速  15 分钟内
    #         FLASH    极速  1-15 分钟
    #     而模型原本【看不到这个值】—— 它只能照 assistant.precautions 里
    #     运营写死的数字答。实测店主的配置里那个数比 STANDARD 的窗口【窄】，
    #     而那两张真图的场次（离开场 >40 分钟）实际都是 STANDARD。
    #   ⇒ 买家问"多久出票"，模型会说一个比报价卡更短的时间 —— 两个数打架。
    #     （★ 具体数字不写在这里：时效的唯一定义处是 templates.TICKET_WINDOW，
    #       _check_params 会抓"代码里第二处定义时效"，我第一版就把它写进注释
    #       被抓住了 —— 这条检查是对的，改了注释而不是改检查。）
    #
    #   ★ templates.py 里早就论证过这件事：
    #     「票达人页面上写的是 1-30/50，但良票文档里 STANDARD 是 1~60。
    #       我们买的如果是 STANDARD，却告诉买家 30 分钟，就是【对买家说了不准
    #       的话】—— 超时的那一单会变成投诉。所以时效必须跟着实际购入的档位走。」
    #
    #   ⇒ 把真实值放进事实块，模型就有依据照说，不用去猜 precautions 里那个数。
    #   ★ 没买过票（conv.ticket_mode 为空）时不写这一行 ——
    #     宁可不说，也不要编一个时效出去。
    ticket_window = _ticket_window_of(conv)
    if ticket_window:
        lines.append(f"· 出票时效：{ticket_window}"
                     f"（买家问\"多久出票\"就照这个答，不要说别的数）")
    rec = getattr(conv, "recognition", None)
    if rec is not None:
        if getattr(rec, "cinema_name", ""):
            lines.append(f"· 影院：{rec.cinema_name}")
        if getattr(rec, "movie_name", ""):
            lines.append(f"· 影片：{rec.movie_name}")
        if getattr(rec, "start_time", ""):
            lines.append(f"· 开场时间：{rec.start_time}")
    seats = getattr(conv, "seats", None)
    if seats:
        names = "，".join((s.get("seatNo") or s.get("seatName") or "")
                         if isinstance(s, dict) else s
                         for s in seats if isinstance(s, (dict, str)) and s)
        if names:
            lines.append(f"· 已选座位：{names}")

    pending = getattr(conv, "pending_order", None)
    if pending is not None:
        lines.append(f"· 已在途订单：{pending.order_no}")
        if pending.cinema_name:
            lines.append(f"· 订单影院：{pending.cinema_name}")
    if order is not None:
        status = getattr(order, "status", "")
        if status:
            lines.append(f"· 订单状态：{status}")
        refundable = getattr(order, "refundable", None)
        if refundable is not None:
            lines.append(f"· 当前可否退票：{'可以' if refundable else '不可以'}")
    return "\n".join(lines)

