"""
application/agent.py —— 标准 agent：工具选择式（不是意图分类式）

════════════════════════════════════════════════════════════════
★ 它和「意图分类式」的根本区别

  意图分类式（V5 / 我前面给的）：        工具选择式（本文件）：
    买家一句话                             买家一句话
      → 模型分类成 15 个意图之一              → 模型直接选工具、填参数
      → 代码 if 意图 == X 分支                → 代码执行 + 校验
      → 状态机 84 条边                        → 一个循环

  ★ 这里【没有意图枚举】，没有状态机分支。
    「买家要报价」= 模型调了 request_quote 这个工具。
    工具名就是意图。

★ 三层分工（实测得出的，不是照抄谁）

  ① 模型做：读句子 → 选工具 + 填参数
      实测：function calling 比纯 prompt 准（意图 75% vs 58%），
            而且 schema 强制输出形状，解析失败 0/24

  ② 代码做：执行 + 校验 ★ 这是唯一能拦住钱的地方
      · 占位值拒绝 —— ★ 实测撞到的：模型会填 movie_name="未知" 来满足 required
      · 原文佐证   —— 模型说"买家说呼和浩特"，佐证必须真在买家原话里
      · 业务前置   —— 没报价不许下单；万达没圈座不许报价
      · 白名单     —— 只允许这几个工具

  ③ 累积事实放【代码】里，不放模型上下文
      ★ 理由：上下文会被压缩、会被模型误读，而事实要能审计。
        每次调用工具都把当前已确认事实注入上下文（只读）。

★ 缺口怎么办 —— 让工具报错，模型自然会去问

  实测：工具报「缺 movie_name」→ 模型就回买家「还得麻烦您告诉我：看哪部影片？」
        3 轮里它把城市、时间、张数全收齐了，代码里【没有一条 if 走缺口逻辑】。
  ★ 但前提是工具必须【严格报错】。第一版没拒绝占位值，
    模型就用 "未知" 把 required 塞满了 —— 看着成功，其实什么也没问。
════════════════════════════════════════════════════════════════
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ==================================================================== 常量
# ★ 事实字段白名单。★ 这份清单是【业务事实】，必须写死 ——
#   不能问模型"报价需要哪些字段"，那不可复现。
FACT_FIELDS = ("city_name", "cinema_name", "movie_name", "show_date", "show_time", "hall_name",
               "ticket_count", "seat_marking", "seats",
               # ★ 下面这些是【识别引擎给的权威 id】，不是名字。
               #   ★ 报价记录要存的是 id —— 名字只是输入，id 才是身份。
               #   （线上真实报价记录里存的就是
               #    provider_identity: {liangpiao_city_id, liangpiao_cinema_id}）
               "cinema_id", "cinema_code", "movie_id", "show_id",
               "cinema_address", "city_code")

# ★ 系统自己产生的字段（quote_id / order_no 等）【不该】要求买家原话佐证 ——
#   报价 id 是系统算出来的，买家原话里当然没有。
#   见 Facts.merge 的 source 参数：system 来源不校验佐证。
#   ★ 另外这些必须存 facts 而不是 Tools 对象上：Tools 每轮新建，状态会丢。
#     踩过：买家回「确认」时 create_order 报 NO_QUOTE。
#
# ★ 占位值黑名单。★ 这是第一轮实测撞出来的：
#   模型为了满足 schema 的 required，填了 movie_name="未知"。
#   不拒的话它永远不问买家。
PLACEHOLDERS = {
    "未知", "不详", "不知道", "不清楚", "未提供", "待定", "待补充",
    "?", "？", "null", "none", "n/a", "na", "-", "--", "暂无", "无",
}

# ★★ 事实来源 —— 决定要不要"逐字佐证"
#
#   image  截图识别来的
#          ★ 不需要逐字佐证：识别引擎读的就是图，它给不出文字片段。
#            而且它给的字段已经过识别层的校验（matchLevel / 格式）。
#   text   模型从【买家文字】里抽的
#          ★ 需要逐字佐证，防的是"把上一轮/截图里的信息当成这轮的"。
#   model  ★ 不允许 —— 模型自己推的不算事实（比如按票价推张数）。
#
#   ★ 这条是实测逼出来的：一开始我一律要求逐字佐证，
#     结果数字字段（ticket_count=2）也要"逐字片段"，很别扭；
#     而且截图来的字段根本给不出文字。⇒ 按来源分要求。
SOURCES = ("image", "text")

# 排期相关字段的格式（★ 格式不对 = 没给，不能拿去查接口）
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


# ==================================================================== 事实
@dataclass
class Facts:
    """
    已确认的事实。★ 存在代码里，存在这个对象上 —— 不在模型上下文里。

    来源有两种，都要经过校验才进来：
      · 买家文字 → 模型抽 → 原文佐证校验
      · 截图识别 → 识别结果（可信，但要过格式校验）
    """
    data: dict = field(default_factory=dict)
    # 每个字段的来源与佐证 —— 出问题时要能追
    provenance: dict = field(default_factory=dict)

    def get(self, key: str, default=None) -> Any:
        return self.data.get(key, default)

    def merge(self, values: dict, *, source: str, evidence: dict | None = None,
              buyer_text: str = "") -> dict:
        """
        合并新事实，返回【被接受的】字段。

        ★ 四道闸门（任何一个不过就不收）：
            ① 字段在白名单里
            ② 不是占位值
            ③ source 合法（image / text）
            ④ ★ source='text' 时必须有逐字佐证；
               source='image' 时不要求（识别引擎给不出文字）

        ★ 佐证必须【非空】且真在买家原话里。
          踩过一个漏洞：`"" in text` 永远为真 ——
          空字符串佐证会绕过整道校验。所以先判非空。

        ★★ 第五道闸门（也是实测撞出来的）：
          【识别结果给过的字段，买家文字不许覆盖】。
          实测：识别给了 cinema_name="博纳国际影城西溪IMAX店"（cinemaId 902），
                买家又发了句「杭州 西溪银泰城博纳国际影城」，
                结果 cinema_name 被改成后者 —— 而 cinema_id 还是 902
                ★ 名字和 id 就对不上了，报价记录会存错影院。
          ⇒ 由识别结果写入的字段（provenance.source == 'image'）不接受文字覆盖。
            买家真想改，得重新发图或走二次确认 —— 不能让一句话冲掉权威身份。
        """
        if source not in SOURCES:
            return {}
        # ★ 哪些字段是识别结果给的（权威）
        authoritative = {k for k, pv in self.provenance.items()
                         if pv.get("source") == "image"}
        accepted = {}
        for key, value in (values or {}).items():
            if value is None or value == "":
                continue
            if key not in FACT_FIELDS:
                continue
            if isinstance(value, str) and value.strip().lower() in PLACEHOLDERS:
                continue
            # ★ 识别结果给过的字段，买家文字不许改（见上面第五道闸门）
            if source == "text" and key in authoritative:
                continue
            proof = (evidence or {}).get(key) or ""
            if source == "text":
                # ★ 先判非空 —— 否则 "" in text 恒真
                if not proof.strip():
                    continue
                if not buyer_text or proof not in buyer_text:
                    continue
            self.data[key] = value
            self.provenance[key] = {"source": source, "evidence": proof}
            accepted[key] = value
        return accepted

    def snapshot(self) -> dict:
        """给模型看的只读快照。"""
        return dict(self.data)

    def absorb_recognition(self, rec) -> dict:
        """
        ★★ 把【识别结果】直接写进事实 —— 不经模型。

        为什么要有这条（实测撞出来的，这是本文件最重要的一处修正）：

          原先事实只能由 record_facts 工具写，也就是说：
              识别引擎数字段 → 拼成文字给模型 → 模型决定记不记得 → 才进事实
                                        ↑ ★ 这一环不可靠
          实测：同一句「两张」跑两次，一次调了 record_facts、一次没调，
                信息就丢了。座位号更是直接丢 —— 因为工具 schema 里
                压根没有 seats，模型想说也传不进来。

          而座位/城市/场次这些【识别引擎已经权威读到了】——
          让模型当搬运工，既没必要，也不可靠。

        ★ V5 就是这么分的（application/purchase_request.py）：
              def apply_outcome(self, request, outcome):
                  facts = dict(outcome.confirmed_facts)   # 识别结果直接进
              class Changes:  seats = ...        # 模型改的走另一条
          ⇒ 两条通道：识别直写 + 模型改（要佐证）。本方法实现前者。

        ★ source='image' —— 不要求买家原话佐证（识别引擎读的就是图）。

        ★★ 但【按证据级别收】，不是照单全收 —— 这一条也是实测逼出来的：
          同一张图，我第一次调识别得 matchLevel=EXACT（有 showId），
          隔一会儿再调得 matchLevel=NONE（showId 为 None，排片下架了）。
          如果不管 matchLevel 都照收，就会把【没有场次的影院】当事实，
          后面拿 None 去询价必然失败，而且买家会收到莫名其妙的报价。

             EXACT     → 影院/影片/场次/座位 全可信
             CANDIDATE → 影院与城市可信，场次不可信
             NONE      → ★ 只有 OCR 文本可信；影院/场次都不当事实
                         （线上 V5 在这种情况下一个字段都不采信，
                           见 domain/recognition/outcome.py 的
                           CANDIDATE_UNVERIFIED 分支）
        """
        if rec is None:
            return {}
        level = str(getattr(rec, "match_level", "") or "").upper()
        # ★ NONE：影院身份没落实 —— 什么都不收，让流程去要城市/重发图
        if level not in ("EXACT", "CANDIDATE"):
            return {}

        values: dict = {}
        # 城市/影院：CANDIDATE 也收（这两个是"缩小范围"用的，不承担成交）
        # ★ 字段名统一后，事实名与识别结果的属性名一致，直接取即可
        for name in ("city_name", "cinema_name"):
            v = getattr(rec, name, None)
            if v:
                values[name] = v
        # 影片/影厅/场次：★ 只有 EXACT 才收
        if level == "EXACT":
            for name in ("movie_name", "hall_name"):
                v = getattr(rec, name, None)
                if v:
                    values[name] = v
            start = getattr(rec, "start_time", None) or ""
            if isinstance(start, str) and len(start) >= 16:
                values.setdefault("show_date", start[:10])
                values.setdefault("show_time", start[11:16])
        # 座位：取 seatName（识别给的是 [{seatName, seatPriceFen, ...}]）
        rows = getattr(rec, "seats", None) or []
        names = [s.get("seatName") or s.get("seatNo") or ""
                 for s in rows if isinstance(s, dict)]
        names = [n for n in names if n]
        if names:
            values["seats"] = names
            values.setdefault("ticket_count", len(names))
            # 座位标记：识别到了已选座位就是 PRESENT
            values["seat_marking"] = "PRESENT"
        accepted = self.merge(values, source="image")
        # ★ 记下权威 id —— 报价记录要存的是 id，不是名字。
        #   ★ show_id 只在 EXACT 时记：NONE 时它是 None，记了反而误导下游。
        id_fields = [("cinema_id", "cinema_id"), ("cinema_code", "cinema_code"),
                     ("cinema_address", "cinema_address")]
        if level == "EXACT":
            id_fields += [("movie_id", "movie_id"), ("show_id", "show_id")]
        for key, attr in id_fields:
            v = getattr(rec, attr, None)
            if v not in (None, ""):
                self.data[key] = v
                self.provenance[key] = {"source": "image", "evidence": ""}
        return accepted


# ==================================================================== 工具
# ★ 工具名 = 意图。description 写"什么时候用"，required 写"必需什么"。
TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "record_facts",
        "description": (
            "★ 一发现任何购票信息就【立刻】记下来。\n"
            "买家补充了什么、识别出什么 —— 都先调这个。\n"
            "★ 不要等到信息齐了才记 —— 记得一条是一条，不要丢。\n"
            "★ 这个工具只记录，不动钱、不报价。"),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": ["facts"],
            "properties": {
                "facts": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "city_name": {"type": "string"},
                        "cinema_name": {"type": "string"},
                        "movie_name": {"type": "string"},
                        "show_date": {"type": "string",
                                      "description": "yyyy-MM-dd"},
                        "show_time": {"type": "string",
                                      "description": "HH:MM"},
                        "hall_name": {"type": "string"},
                        "ticket_count": {"type": "integer", "minimum": 1},
                        "seat_marking": {
                            "type": "string",
                            "enum": ["PRESENT", "ABSENT", "UNCERTAIN"],
                            "description": "买家在座位图上圈位置了吗"},
                        # ★★ 座位号 —— 实测这里漏过：工具 schema 里没有这个字段，
                        #   模型就算读到了座位也传不进来（additionalProperties:
                        #   False 会直接拒），结果「4张9排16 17 18 19」只记下了 4 张。
                        #   ★ 只用于【买家文字里报的座位】——
                        #     图上的座位由 Facts.absorb_recognition 直接记，不用模型转述。
                        "seats": {
                            "type": "array", "items": {"type": "string"},
                            "description": "买家文字里报的座位号，如 "
                                           "['9排16','9排17']。★ 图上识别到的"
                                           "座位不用你填，系统已经记了。"},
                    }},
                "evidence": {
                    "type": "object",
                    "description": "每个字段在【本轮买家原话】里的逐字片段。"
                                   "截图识别来的不用给。",
                    "additionalProperties": {"type": "string"}},
            }}}},
    {"type": "function", "function": {
        "name": "request_quote",
        "description": (
            "给买家算价。\n"
            "★ 只在信息【齐全】时调。不齐就先用 record_facts 记下、再问买家。\n"
            "★ 缺哪一项会失败 —— 失败时请向买家要那一项，不要编。"),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": ["cinema_name", "movie_name", "show_date", "show_time",
                         "ticket_count"],
            "properties": {
                "city_name": {"type": "string"},
                "cinema_name": {"type": "string"},
                "movie_name": {"type": "string"},
                "show_date": {"type": "string", "description": "yyyy-MM-dd"},
                "show_time": {"type": "string", "description": "HH:MM"},
                "hall_name": {"type": "string"},
                "ticket_count": {"type": "integer", "minimum": 1},
                "seat_marking": {"type": "string",
                                 "enum": ["PRESENT", "ABSENT", "UNCERTAIN"]},
                "evidence": {
                    "type": "object",
                    "description": "每个字段在【本轮买家原话】里的逐字片段。"
                                   "截图识别来的不用给。",
                    "additionalProperties": {"type": "string"}},
            }}}},
    {"type": "function", "function": {
        "name": "create_order",
        "description": "买家确认报价后下单。★ 必须先有一次成功的 request_quote。",
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {}}}},
    {"type": "function", "function": {
        "name": "request_reprice",
        "description": (
            "买家同意改价 / 要求改价时调。\n"
            "★ 触发词：「改价」「改价吧」「你改吧」「麻烦改价」「可以改价吗」。\n"
            "★ 前置：必须已经有一次成功的报价。没报价就先去报价。"),
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {}}}},
    {"type": "function", "function": {
        "name": "cancel_request",
        "description": (
            "买家说不想买了 / 不订了时调。\n"
            "★ 触发词：「不用了」「算了」「不买了」「不要了」「先不订了」。\n"
            "★ 把当前这单的意图清掉（不涉及退款 —— 退款是另一回事）。"),
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {"reason": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "search_faq",
        "description": (
            "★ 买家问【规则/流程】类问题，先调这个查，不要凭印象答。\n"
            "适用：多久出票、能不能退改、怎么拍、在哪拍、取票码怎么用、\n"
            "      出票失败怎么办、票出错了怎么办、会员座/灰色座位、\n"
            "      怎么购票、支付后多久出票、可以改签退票吗。\n"
            "★ 查到就照查到的说；查不到才说'我帮你问下店家'并转人工。"),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string",
                          "description": "买家的问题关键词，比如「出票」「退票」「取票」"}}}}},
    {"type": "function", "function": {
        "name": "answer_buyer",
        "description": (
            "回答买家的普通问题：出票要多久、能不能退改、取票怎么取、"
            "发票、会员座能不能订、流程怎么走。\n"
            "★ 答不了、涉及争议、买家投诉 —— 用 handoff_to_human，别硬答。"),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": ["reply"],
            "properties": {"reply": {"type": "string",
                                     "description": "要发给买家的话，一到三句"}}}}},
    {"type": "function", "function": {
        "name": "handoff_to_human",
        "description": "拿不准、答不了、买家不满、涉及钱和责任争议 —— 转人工。",
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {"reason": {"type": "string"}}}}},
]

TOOL_NAMES = tuple(t["function"]["name"] for t in TOOLS)

# ★ 每个字段的中文名 —— 让模型说人话，而不是说 city_name/movie_name 这种内部名
FIELD_LABELS = {
    "city_name": "城市", "cinema_name": "影院", "movie_name": "影片", "show_date": "日期",
    "show_time": "开场时间", "hall_name": "影厅", "ticket_count": "张数",
    "seat_marking": "在座位图上圈选位置", "seats": "具体排号座号",
}

# ★ 不同供货商要的字段不同（★ 这是业务事实，必须写死 ——
#   问模型"报价要哪些字段"不可复现）
REQUIRED = {
    "WANDA": ("city_name", "cinema_name", "movie_name", "show_date", "show_time",
              "ticket_count", "seat_marking"),
    "LIANGPIAO": ("cinema_name", "movie_name", "show_date", "show_time",
                  "seats"),
}


def missing_fields(facts: "Facts", provider: str) -> list:
    """
    算还缺什么。★ 纯代码，不问模型。

    ★ 为什么必须写在代码里（实测教训）：
      我把这个去掉、只给模型事实快照，它就把【所有】字段问一遍，
      包括已经确认的。因为它不知道"还缺什么"。
      ⇒ 把缺口算好、明明白白告诉它，它才只问缺的。
    """
    need = REQUIRED.get(provider.upper(), REQUIRED["WANDA"])
    return [k for k in need if not facts.get(k)]


# ==================================================================== 执行
@dataclass
class ToolOutcome:
    """一次工具执行的结果。"""
    ok: bool
    name: str = ""
    data: dict = field(default_factory=dict)
    error_code: str = ""
    message: str = ""            # ★ 给模型看的 —— 要写清"该去问买家什么"
    handoff: bool = False
    side_effect: bool = False    # 花钱的动作


class Tools:
    """
    工具执行层。★ 业务规则全在这一层 —— 这是唯一能拦住钱的地方。

    ★ 它不调真接口。真接口通过注入的 provider 来（这样能测、能不花钱）。
    """

    def __init__(self, *, provider: str = "WANDA",
                 quote_fn=None, order_fn=None, faq_fn=None):
        self.provider = provider
        self._quote = quote_fn
        self._order = order_fn
        self._faq = faq_fn
        self.quote_ok = False
        self.quote_data: dict = {}

    def execute(self, name: str, args: dict, facts: Facts,
                buyer_text: str = "") -> ToolOutcome:
        if name not in TOOL_NAMES:
            # 模型编了个工具名 —— 明确告诉它没有，别让它以为做成了
            return ToolOutcome(False, name, error_code="UNKNOWN_TOOL",
                               message=f"没有 {name} 这个工具")

        # ★★ 闸门 0：占位值 —— 必须在所有工具之前
        bad = self._reject_placeholders(args)
        if bad:
            return ToolOutcome(
                False, name, error_code="PLACEHOLDER_VALUE",
                message=f"这些字段是占位值，不算数：{'、'.join(bad)}。"
                        f"★ 请去问买家要真实信息，不要用「未知」占位。")

        if name == "record_facts":
            return self._record_facts(args, facts, buyer_text)
        if name == "request_reprice":
            return self._request_reprice(facts)
        if name == "cancel_request":
            return self._cancel_request(args, facts)
        if name == "search_faq":
            return self._search_faq(args)
        if name == "request_quote":
            return self._request_quote(args, facts, buyer_text)
        if name == "create_order":
            return self._create_order(facts)
        if name == "answer_buyer":
            return self._answer(args)
        if name == "handoff_to_human":
            return ToolOutcome(True, name, handoff=True,
                               data={"reason": args.get("reason", "")})
        return ToolOutcome(False, name, error_code="UNIMPLEMENTED")

    # ---------------------------------------------------------------- 校验
    @staticmethod
    def _reject_placeholders(args: dict) -> list:
        """
        找出占位值。

        ★ 这是第一轮实测的教训：模型为了满足 required，
          填了 movie_name="未知"。不拒的话它永远不问买家。
        """
        bad = []
        for key, value in (args or {}).items():
            if key == "evidence":
                continue
            if isinstance(value, str) and value.strip().lower() in PLACEHOLDERS:
                bad.append(key)
        return bad

    @staticmethod
    def _merge_with_evidence(args: dict, facts: Facts,
                             buyer_text: str) -> tuple[dict, list]:
        """
        把参数并进事实，返回 (接受的, 被拒的字段名)。

        ★★ 两条实测撞出来的规则：

        ① 佐证只校验【新引入的字段】，不校验【已在事实里的】。
           实测：第 3 轮买家只说"20:30"，模型为了传全参数会带上
           cinema_name / movie_name（第 1、2 轮确认过的）。一律要求"本轮佐证"
           会把老字段全拒掉 → 永远报缺字段。

        ② ★ 来源决定要不要佐证：
             source='image' → 不要求（识别引擎读的就是图）
             source='text'  → 要求（防模型把老信息当这轮的）
           实测：数字字段（ticket_count=2）要"逐字片段"很别扭，
                 截图来的字段根本给不出文字。
        """
        source = args.get("source") or "text"
        values = {k: v for k, v in args.items()
                  if k not in ("evidence", "source")}
        evidence = args.get("evidence") or {}
        known = facts.snapshot()

        to_merge, rejected = {}, []
        for key, value in values.items():
            if value is None or value == "":
                continue
            if key not in FACT_FIELDS:
                continue
            # ★ 已在事实里且没变 → 是上下文，不是新事实，放行
            if key in known and known[key] == value:
                continue
            if isinstance(value, str) and value.strip().lower() in PLACEHOLDERS:
                rejected.append(key)
                continue
            if source == "text":
                proof = (evidence.get(key) or "").strip()
                if not proof or not buyer_text or proof not in buyer_text:
                    rejected.append(key)
                    continue
            to_merge[key] = value

        accepted = facts.merge(to_merge, source=source, evidence=evidence,
                              buyer_text=buyer_text)
        return accepted, rejected

    # ---------------------------------------------------------------- 各工具
    def _record_facts(self, args: dict, facts: Facts,
                      buyer_text: str) -> ToolOutcome:
        """
        只记录，不动钱。

        ★★ 为什么要单独有这个工具（实测撞出来的）：
          一开始事实只能通过 request_quote 存进去，而 request_quote
          缺字段就会被拒 —— 于是"买家说呼和浩特"这条信息【丢了】，
          模型下一轮又问一遍城市。
          ⇒ 记录和执行必须分开：一发现信息就先记下，不必等到能报价。
        """
        values = args.get("facts") or {}
        if not isinstance(values, dict):
            return ToolOutcome(False, "record_facts",
                               error_code="BAD_FACTS",
                               message="facts 必须是个对象")
        source = args.get("source") or (
            "text" if buyer_text else "image")
        evidence = args.get("evidence") or {}
        accepted = facts.merge(values, source=source, evidence=evidence,
                              buyer_text=buyer_text)
        rejected = [k for k, v in values.items()
                    if k not in accepted and v not in (None, "")]
        # ★ 缺什么也一并告诉它，省一轮
        gaps = missing_fields(facts, self.provider)
        # ★★ 关键：信息齐了要【明确让它接着去报价】。
        #   实测撞出来的：只说"信息齐了，可以报价"，模型就以为完事了 ——
        #   它只调 record_facts，不调 request_quote，然后回买家一句废话。
        #   ⇒ 记完之后必须给下一步指令，并把还缺什么说清楚。
        if gaps:
            nxt = ("请向买家要这几项：" + "、".join(
                FIELD_LABELS.get(g, g) for g in gaps))
        else:
            nxt = "★ 信息已齐 —— 请【接着调 request_quote】报价，不要只回话。"
        return ToolOutcome(
            True, "record_facts",
            data={"recorded": accepted,
                  "rejected": rejected,
                  "still_missing": [FIELD_LABELS.get(g, g) for g in gaps],
                  "next_step": nxt},
            message=(f"记下了 {list(accepted)}"
                     + (f"；不接受 {rejected}（缺本轮原话佐证或值非法）"
                        if rejected else "")
                     + f"。{nxt}"))

    def _request_reprice(self, facts: Facts) -> ToolOutcome:
        """
        发起改价。

        ★★ 为什么必须有这个工具（实测撞出来的）：
          没有它的时候，买家说「改价吧」，模型想表达"要改价"却无工具可用，
          结果走了 handoff_to_human —— 十次里有十次转人工。
          ★ 但也【不能什么都改】：改价是花钱的动作，必须有报价在先。

        ★★ 为什么读 facts 而不是 self.quote_ok：
          报价状态必须跨轮活下来。存在 Tools 对象上每轮就丢 ——
          这正是「确认下单」报 NO_QUOTE 的根因（见 _create_order）。
        """
        quote_id = facts.get("quote_id")
        if not quote_id:
            return ToolOutcome(
                False, "request_reprice", error_code="NO_QUOTE",
                message="还没有有效报价，不能改价。★ 请先把报价做出来，"
                        "再问买家确认。")
        facts.data["reprice_status"] = "REPRICE_REQUESTED"
        return ToolOutcome(
            True, "request_reprice",
            data={"status": "REPRICE_REQUESTED", "quote_id": quote_id,
                  "tip": "已按最新报价申请改价，正在等待执行结果，"
                         "请确认改价成功后再付款。"},
            side_effect=True)

    def _cancel_request(self, args: dict, facts: Facts) -> ToolOutcome:
        """
        买家不想买了。

        ★★ 为什么必须有这个工具（实测撞出来的）：
          没有它的时候，买家说「不用了」，模型只回一句"好的那就不打扰啦"，
          【没调任何工具】—— 在途订单/待报价的状态一点没变。
          ★ 注意：这只清"购买意图"，不涉及退款。
            已经付过款的退款走另一条路，不能混。

        ★ facts 用来清掉本次意向（报价 id 等），不是多余参数。
        """
        cleared = [k for k in ("quote_id", "quote_total_fen", "reprice_status")
                   if k in facts.data]
        for k in cleared:
            facts.data.pop(k, None)
        return ToolOutcome(
            True, "cancel_request",
            data={"status": "CANCELLED",
                  "reason": args.get("reason", ""),
                  "cleared": cleared,
                  "tip": "已取消本次购票意向。如已付款需退款，请另外告知。"},
            side_effect=True)

    def _search_faq(self, args: dict) -> ToolOutcome:
        """
        查配置里的规则/流程话术。

        ★★ 为什么必须有这个工具（实测撞出来的）：
          没有它的时候，买家问「多久出票」「能退吗」，模型只能编：
            「一般下单后很快就能出票，具体时间以影院出票为准」  ← 模糊
            「退改这块我这边不能直接承诺」                  ← 逃避
          而这两条的准确答案【我们配置里本来就有】：
            「出票」那条关键词回复的文案
            「特惠代订的票付款后不支持退改签的哈」
          ⇒ 模型不知道不等于没配过。得有工具让它去查。
        """
        query = (args.get("query") or "").strip()
        if not query:
            return ToolOutcome(False, "search_faq", error_code="EMPTY_QUERY",
                               message="query 不能为空")
        if self._faq is None:
            return ToolOutcome(
                False, "search_faq", error_code="FAQ_UNAVAILABLE",
                message="规则库查不了。★ 请如实说「这个我帮你问下店家」，"
                        "然后转人工 —— 不要凭印象编答案。")
        try:
            hits = self._faq(query) or []
        except Exception as exc:                            # noqa: BLE001
            return ToolOutcome(False, "search_faq",
                               error_code="FAQ_FAILED",
                               message=f"查规则失败（{type(exc).__name__}）")
        if not hits:
            return ToolOutcome(
                False, "search_faq", error_code="FAQ_NO_HIT",
                message=f"没查到「{query}」相关的规则。"
                        f"★ 请如实说「这个我帮你问下店家」，然后转人工 —— "
                        f"不要凭印象编答案。")
        return ToolOutcome(True, "search_faq",
                           data={"query": query, "results": hits},
                           message=f"查到 {len(hits)} 条")

    def _request_quote(self, args: dict, facts: Facts,
                       buyer_text: str) -> ToolOutcome:
        accepted, rejected = self._merge_with_evidence(args, facts, buyer_text)
        if rejected:
            return ToolOutcome(
                False, "request_quote", error_code="EVIDENCE_MISSING",
                message=f"这些字段没有本轮买家原话的佐证，我不采信："
                        f"{'、'.join(rejected)}。★ 要么给出原话片段，"
                        f"要么去问买家。")

        merged = facts.snapshot()
        # ★ 必需字段 —— 缺了就报错，让模型去问（这就是"缺口循环"）
        need = ["cinema_name", "movie_name", "show_date", "show_time", "ticket_count"]
        missing = [k for k in need if not merged.get(k)]
        if missing:
            return ToolOutcome(
                False, "request_quote", error_code="NEEDS_INFORMATION",
                message=f"还不能报价，缺：{'、'.join(missing)}。"
                        f"★ 请向买家要这些，不要自己编。")
        # 格式校验 —— 格式不对等于没给
        if not DATE_RE.match(str(merged.get("show_date") or "")):
            return ToolOutcome(False, "request_quote",
                               error_code="SHOW_DATE_FORMAT",
                               message="日期格式要 yyyy-MM-dd（如 2026-10-12），"
                                       "请跟买家确认。")
        if not TIME_RE.match(str(merged.get("show_time") or "")):
            return ToolOutcome(False, "request_quote",
                               error_code="SHOW_TIME_FORMAT",
                               message="时间格式要 HH:MM（如 20:30），"
                                       "请跟买家确认。")

        # ★ 供货商特有规则：万达要圈选
        if self.provider == "WANDA":
            marking = merged.get("seat_marking")
            if marking != "PRESENT":
                return ToolOutcome(
                    False, "request_quote", error_code="SEAT_MARKING_REQUIRED",
                    message="万达这单还不能报价 —— 买家还没在座位图上圈位置。"
                            "★ 请让他在选座图上圈一下想坐的位置，再发我。")

        if self._quote is None:
            return ToolOutcome(False, "request_quote",
                               error_code="QUOTE_UNAVAILABLE",
                               message="报价服务暂时不可用，请转人工。")
        try:
            result = self._quote(merged)
        except Exception as exc:                            # noqa: BLE001
            return ToolOutcome(False, "request_quote",
                               error_code="QUOTE_FAILED",
                               message=f"询价失败（{type(exc).__name__}），"
                                       f"请转人工。")
        self.quote_ok = True
        self.quote_data = dict(result)
        # ★★ 报价状态必须写进 facts（跨轮保留）。
        #   实测撞出来的：存在 self 上，Tools 每轮新建 → 下一轮买家回「确认」，
        #   create_order 报 NO_QUOTE，说"系统里还没查到有效报价"。
        #   ★ source="system"：系统算出来的，不该要求买家原话佐证。
        for key, value in (("quote_id", result.get("quote_id")),
                           ("quote_total_fen", result.get("total_fen"))):
            if value is not None:
                facts.data[key] = value
                facts.provenance[key] = {"source": "system", "evidence": ""}
        return ToolOutcome(True, "request_quote", data=result)

    def _create_order(self, facts: Facts) -> ToolOutcome:
        # ★ 前置条件：没有成功报价不许下单。
        #   ★ 读 facts.quote_id（跨轮活着），不是 self.quote_ok（每轮丢）。
        if not facts.get("quote_id"):
            return ToolOutcome(False, "create_order", error_code="NO_QUOTE",
                               message="还没有有效报价，不能下单。"
                                       "请先成功算价，再让买家确认。")
        if self._order is None:
            return ToolOutcome(False, "create_order",
                               error_code="ORDER_UNAVAILABLE",
                               message="下单服务暂时不可用，请转人工。")
        try:
            result = self._order(facts.snapshot())
        except TypeError:
            # ★ 注入的下单函数可能不收参数（比如本地假实现）。
            #   真实现要 facts（影院/场次/座位），假的不用 —— 两种都收。
            result = self._order()
        except Exception as exc:                            # noqa: BLE001
            return ToolOutcome(False, "create_order",
                               error_code="ORDER_FAILED",
                               message=f"下单失败（{type(exc).__name__}），"
                                       f"请转人工。")
        return ToolOutcome(True, "create_order", data=result,
                           side_effect=True)

    def _answer(self, args: dict) -> ToolOutcome:
        reply = (args.get("reply") or "").strip()
        if not reply:
            return ToolOutcome(False, "answer_buyer",
                               error_code="EMPTY_REPLY",
                               message="reply 不能为空")
        return ToolOutcome(True, "answer_buyer", data={"reply": reply})


# ==================================================================== 轮回
@dataclass
class Turn:
    """一轮的完整结果（给上层做展示/审计）。"""
    messages: list = field(default_factory=list)   # 要发给买家的
    calls: list = field(default_factory=list)      # 调了哪些工具
    errors: list = field(default_factory=list)     # 工具报的错（★ 排查用）
    handoff: bool = False
    note: str = ""
    quote: dict = field(default_factory=dict)      # ★ 报价结果（要渲染成卡片）
    order: dict = field(default_factory=dict)      # ★ 下单结果


def render_quote_card(quote: dict) -> str:
    """
    把报价渲染成给买家的卡片。

    ★ 为什么要有这个（实测发现）：
      工具返回报价后，模型会自己把价格写进散文：
        「报价是：单张42.03元，2张共84.06元」—— 而且它算错过 1 分。
      ⇒ 钱【不许让模型复述】。工具算好的数字由代码渲染，模型只管说别的。
    """
    if not quote:
        return ""
    lines = []
    if quote.get("cinema_name"):
        lines.append(str(quote["cinema_name"]))
    if quote.get("movie_name"):
        lines.append(f"影片：{quote['movie_name']}")
    if quote.get("show_time"):
        lines.append(f"场次：{quote['show_time']}")
    if quote.get("seat_count"):
        lines.append(f"数量：{quote['seat_count']}张")
    if quote.get("unit_price_yuan") is not None:
        lines.append(f"价格：{quote['unit_price_yuan']:.2f}元/张")
    if quote.get("total_yuan") is not None:
        lines.append(f"总价：{quote['total_yuan']:.2f}元")
    return "\n".join(lines)


SYSTEM = """你是影票代订店的客服 agent。

★ 用工具表达你要做的事，不要只在文字里描述。
★ 每一轮我都会告诉你【已确认什么】和【还缺什么】。
  ★ 只问【还缺的】—— 已经确认的不要再问，买家会烦。

★★ 规则类问题必须先查，不许凭印象答
  买家问「可以买吗」「能代订吗」「会员座」「灰色座位」「多久出票」
  「能退吗」「怎么拍」「在哪里拍」「取票码怎么用」这类，
  ★ 一律【先调 search_faq】，然后【照查到的那句说】。
  ★ 答案在 search_faq 的结果里 —— 它是店家定好的口径。
    原文照发，不要换同义词、不要加自己编的内容。
  ★ 查到多条时用第 1 条（那是按买家问法排好的）。
  ★ 查不到才说「这个我帮你问下店家」并转人工。
  ★ 例外：「好的」「谢谢」「你好」这种打招呼/应答，直接回，不用查。

★ 工具报错说缺什么，就去问买家要那一项。
  不要用「未知」「待定」这类词占位 —— 会被拒绝。
★ 信息齐了才调 request_quote；不齐就先问。
★ 万达的影院必须在选座图上圈了位置才能报价，没圈就先请他圈。

★★ 不许在没有数据时乱承诺
  「有国庆的票么」「预售能买吗」「有没有那个场次」——
  ★ 你没查过任何排期，不许说「有的」。
  ★ 正确说法：可以帮你查，先告诉我城市/影院/影片/日期。
  ★ 唯一能直接说「可以」的，是 search_faq 查到的口径。

★ 不想买了（不用了/算了/不买了）→ 调 cancel_request。
★ 要改价（改价/你改吧/改价吧）→ 调 request_reprice。
★ 买家说「人工」「转人工」「人呢」「客服」「店家」→ 调 handoff_to_human。
★ 答不了的、有争议的、买家不满的 —— 也走 handoff_to_human。
★ 不许承诺退票/改签/连座/一定出票。不许说自己是 AI。
★ 一到三句话，口语、简短。
"""


# ★ 肯定的应答（买家同意）。
#   ★ 为什么写在代码里（实测撞出来的）：
#     买家回「确认」，模型去纠结"还缺城市"，没下单。
#     而"我们刚发过报价 + 买家说确认" → 该下单 —— 这件事【代码知道】，
#     模型不一定推得出来。所以代码先判，判不了再交给模型。
AFFIRMATIVE = {
    "确认", "确定", "是", "是的", "对", "对的", "好的", "好", "可以", "行",
    "ok", "OK", "okok", "要", "需要", "可以买", "嗯", "嗯嗯", "买了", "拍",
    "下单", "下单吧", "可以下单", "确认下单", "没错", "对头",
}

# ★ 否定（买家放弃）
NEGATIVE = {"不用了", "算了", "不买了", "不要了", "先不订了", "不订了", "取消"}

# ★ 买家在问「能不能买」——各种说法（线上真实原文数的）
CAN_I_BUY = ("可以买吗", "能买吗", "可以买不", "能买不", "可以拍吗", "能下吗",
             "能定吗", "可以订吗", "现在可以买", "可以买")

# ★ 但这些【不算】"能不能买" —— 它们问的是我们的服务能力，不是这一单。
#   实测误判：「会员座能代订吗」被 IS_CAN_I_BUY 抢走，
#   回了"可以买，需要几张呢" —— 而这时买家还没给影片/场次，太早了。
#   正确是走【会员座】那条配置规则。
NOT_CAN_I_BUY = ("代订", "代下", "代买", "代购")


def is_can_i_buy(text: str) -> bool:
    """买家是不是在问"这一单能不能买"。"""
    t = (text or "").strip()
    if any(k in t for k in NOT_CAN_I_BUY):
        return False
    return any(k in t for k in CAN_I_BUY)


def has_image_facts(facts: "Facts") -> bool:
    """
    买家【发过图】了吗。

    ★ 判据：facts 里有没有 seat_marking。
      识别一张选座图必定会写上 seat_marking（PRESENT 或 ABSENT）——
      有没有圈位置是两回事，但"发过图"这件事一定能看出来。
      ★ 完全没有 facts 的键 → 说明这一轮之前买家没发过图。

    ★★ 这里【不判断】具体座位能不能选 ——
      店家的规则是"人工会根据图片核对出票"，
      代码只要知道【圈没圈位置】，不需要知道圈的是哪几个座。
    """
    return facts.get("seat_marking") is not None


def is_affirmative(text: str) -> bool:
    """买家是不是在同意。★ 完全相等才算 —— 避免「可以买吗」被当成同意。"""
    return (text or "").strip().rstrip("。！~～！") in AFFIRMATIVE


def is_negative(text: str) -> bool:
    return (text or "").strip().rstrip("。！~～！") in NEGATIVE


def context_block(facts: "Facts", provider: str) -> str:
    """
    每轮给模型的上下文。

    ★ 这是实测逼出来的：只给"已确认事实"，模型会【把所有字段问一遍】。
      必须同时给出【明确的缺口清单】，它才只问缺的。

    ★★ 还要给出【下一步该干什么】——
      买家回「确认」时，模型去纠结"还缺城市"，没下单。
      而"刚报过价 + 买家说确认 → 下单"是【代码知道】的事。
    """
    known = facts.snapshot()
    gaps = missing_fields(facts, provider)
    lines = []
    if known:
        lines.append("【已确认（不要再问）】")
        for k, v in known.items():
            lines.append(f"  · {FIELD_LABELS.get(k, k)}：{v}")
    else:
        lines.append("【已确认】无")
    if gaps:
        lines.append("【还缺（要问的就这些）】")
        lines.append("  " + "、".join(FIELD_LABELS.get(g, g) for g in gaps))
    else:
        lines.append("【还缺】无")

    # ★ 下一步提示 —— 这一条直接决定买家回「确认」时会不会下单
    if facts.get("order_no"):
        lines.append("【下一步】已下单，等买家付款")
    elif facts.get("reprice_status"):
        lines.append("【下一步】改价已申请，等执行结果")
    elif facts.get("quote_id"):
        lines.append("【下一步】已报价 —— 买家同意就调 create_order 下单；"
                     "要改价调 request_reprice；不买了调 cancel_request")
    elif not gaps:
        lines.append("【下一步】信息齐了，调 request_quote 报价")
    else:
        lines.append("【下一步】信息不齐，问买家要缺的那几项")
    return "\n".join(lines)


def run_turn(client, tools: Tools, facts: Facts, buyer_text: str, *,
             history: list | None = None, max_steps: int = 4,
             evidence_source: str = "text", recognition=None) -> Turn:
    """
    跑一轮：把买家这句话交给模型，它可能要调几次工具。

    recognition：★ 识别结果对象。给了就【由代码直接写进事实】——
                 座位/城市/场次这些识别引擎已经读到的，不该让模型转述
                 （实测：模型转述会丢，座位号直接丢光）。
                 见 Facts.absorb_recognition。

    ★ 循环的边界：
        · 模型不再调工具（给出一句话）→ 结束
        · 调了 answer_buyer / handoff_to_human → 结束（终点动作）
        · 到 max_steps → 结束（防无限调）

    ★ evidence_source 决定这轮的佐证要求：
        'text'  买家说了话 → 从话里抽的字段要逐字佐证
        'image' 只有图 / 图的内容是识别结果 → 不要逐字佐证
                （识别引擎读的就是图，它给不出文字片段）
    """
    out = Turn()
    # ★★ 先收识别结果（代码直写，不问模型）
    if recognition is not None:
        absorbed = facts.absorb_recognition(recognition)
        if absorbed:
            out.note = f"识别结果直接入账：{list(absorbed)}"
    messages = list(history or [])
    messages.append({"role": "user", "content": buyer_text or "（空消息）"})

    # ★★ 代码先判一次「状态相关」的动作。
    #
    #   实测：买家回「确认」，模型去纠结"还缺城市"，没下单；
    #        买家说「不用了」，模型只回句话，没调任何工具。
    #   而"刚报过价 + 买家同意 → 下单"这件事，
    #   ★ 代码 100% 知道，模型只 50% 猜得到（温度 0 也不稳）。
    #   ⇒ 能代码判的就别问模型 —— 但【只覆盖无歧义的情形】，
    #     其余照旧交给模型（不然就成了写死流程）。

    # ---- ① 「可以买吗」：答案取决于【买家发过图没有】 ----
    #
    #   ★ 店家的规则：
    #       没发图     → 让他发张选座图、在图上圈一下想要的位置
    #       发过图     → 直接说"可以买，需要几张呢"
    #   ★ 关键词回复做不到这个区分（它不看上下文），
    #     所以在这里判 —— 和「确认下单」同一类：代码知道上下文。
    #   ★ 这里【不判断】具体座位 —— 人工会根据图片核对出票，
    #     代码只要知道圈没圈位置就够了。
    if is_can_i_buy(buyer_text) and not facts.get("quote_id"):
        if has_image_facts(facts):
            out.note = "代码判定：买家问可以买吗 + 之前发过图 → 直接说可以买"
            out.messages = ["可以买，需要几张呢"]
        else:
            out.note = "代码判定：买家问可以买吗 + 还没发过图 → 请他发图圈位置"
            out.messages = ["发张选座图，在图上圈一下您想要的位置，"
                            "再告诉我需要几张就行"]
        return out

    # ---- ② 已报价 + 买家明确应答 → 下单 / 取消 ----
    forced = None
    if facts.get("quote_id") and not facts.get("order_no"):
        if is_affirmative(buyer_text):
            forced = "create_order"
        elif is_negative(buyer_text):
            forced = "cancel_request"
    if forced:
        outcome = tools.execute(forced, {}, facts, "")
        out.calls.append({"name": forced, "args": {},
                          "ok": outcome.ok,
                          "error_code": outcome.error_code})
        if not outcome.ok:
            out.errors.append(f"{forced}: {outcome.error_code}")
        out.note = f"代码判定：买家{'同意' if forced == 'create_order' else '放弃'}" \
                   f"（已报价 + 明确应答）→ {forced}"
        if forced == "create_order" and outcome.ok:
            out.order = dict(outcome.data)
            out.messages = [f"好的，已提交订单，请勿重复付款～"
                            f"出票后我把取票码发您。"]
            return out
        if forced == "cancel_request" and outcome.ok:
            out.messages = ["好的，这单先给您取消啦。"
                            "有需要随时找我～"]
            return out

    for step in range(max_steps):
        system = (SYSTEM + "\n" + context_block(facts, tools.provider)
                  + f"\n【本轮信息来源】{evidence_source}"
                  + ("（截图识别，从图里来的字段不用给佐证）"
                     if evidence_source == "image" else
                     "（买家文字，从话里抽的字段必须给逐字佐证）"))
        reply = client.chat(messages, system=system, tools=TOOLS)
        calls = (reply.raw or {}).get("tool_calls") or []
        if not calls:
            text = (reply.content or "").strip()
            if text:
                out.messages.append(text)
            out.note = f"{step + 1} 轮，模型没调工具"
            # ★★ 这条路径也要 _finalize —— 漏过一次：
            #   模型调完 request_quote 后直接回一句收尾（不再调工具），
            #   走的是这里。不 finalize 的话，报价卡不渲染，
            #   而且模型复述的金额（还算错过）就原样发给买家了。
            out.messages = _finalize(out)
            return out

        messages.append(reply.raw)
        terminal = False
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = (json.loads(raw_args) if isinstance(raw_args, str)
                        else (raw_args or {}))
            except json.JSONDecodeError:
                args = {}
                outcome = ToolOutcome(False, name,
                                      error_code="BAD_ARGS_JSON",
                                      message="参数不是合法 JSON，请重调")
            else:
                # ★ 模型没声明 source 时，按本轮的实际来源定
                args.setdefault("source", evidence_source)
                outcome = tools.execute(name, args, facts, buyer_text)

            out.calls.append({"name": name, "args": args,
                              "ok": outcome.ok,
                              "error_code": outcome.error_code})
            if not outcome.ok:
                out.errors.append(f"{name}: {outcome.error_code}")

            # ★ 结果回灌给模型 —— 它看到报错就会去问买家
            messages.append({"role": "tool", "tool_call_id": call.get("id") or name,
                             "content": json.dumps(
                                 {"ok": outcome.ok,
                                  "error_code": outcome.error_code,
                                  "message": outcome.message,
                                  **outcome.data},
                                 ensure_ascii=False)})

            if name == "answer_buyer" and outcome.ok:
                out.messages.append(outcome.data.get("reply", ""))
                terminal = True
            if name == "handoff_to_human" and outcome.ok:
                out.handoff = True
                # ★★ 转人工也必须给买家一句话（实测 bug）：
                #   一开始 handoff 不带回复，结果买家发「怎么买」，
                #   收到的是【什么都没回】—— 对话就断在那里了。
                #   转人工是[减少承诺]，不是[不说话]。
                out.messages.append(render_handoff())
                terminal = True
            # ★ 报价/下单的结果【单独存】，由代码渲染 —— 不让模型复述金额。
            #   实测：模型自己写「2张共84.06元」，把 84.05 算错了 1 分。
            if name == "request_quote" and outcome.ok:
                out.quote = dict(outcome.data)
            if name == "create_order" and outcome.ok:
                out.order = dict(outcome.data)
                terminal = True

        out.note = f"{step + 1} 轮，调了 {len(calls)} 个工具"
        if terminal:
            out.messages = _finalize(out)
            return out

    out.note = f"到 {max_steps} 轮上限（模型可能没收敛）"
    out.messages = _finalize(out)
    return out


def _finalize(out: "Turn") -> list:
    """
    收尾：把报价卡插到买家消息里，并把模型复述的金额抹掉。

    ★ 为什么（实测）：模型拿到 request_quote 的成功结果后，
      会自己写一句「给您算好了：… 单价42.03元，共84.06元。要下单吗？」
      —— 金额必须是【工具算的那一份】，不许它复述。
      ⇒ 只抹掉【复述出来的数字】，保留它问的话（「要下单吗？」）。
        第一版我按「含'元'就整条丢掉」，把「要下单吗？」也丢了。
    """
    messages = [m for m in out.messages if m]
    if out.quote:
        card = render_quote_card(out.quote)
        if card:
            cleaned = []
            for m in messages:
                # ★ 只删掉复述金额的句子，不删整条
                sentences = _split_sentences(m)
                kept = [s for s in sentences if not _restates_price(s)]
                rest = "".join(kept).strip()
                if rest:
                    cleaned.append(rest)
            cleaned.append(card)
            messages = cleaned
    return messages


def render_handoff() -> str:
    """
    转人工时发给买家的话。

    ★ 为什么不接收 reason（实测）：
      模型给的 reason 是给【店家】看的内部说明
      （"规则库里没有会员座这条"），发给买家会暴露内部细节。
      ⇒ 固定话术；内部 reason 由 out.calls 记着，查日志能追。
    """
    return "稍等哈，这个我让店家来跟您说～"


def _split_sentences(text: str) -> list:
    """按中文句末标点切句，保留标点。"""
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？；\n":
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


_PRICE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*元"          # 84.06 元
    r"|[\d.]+\s*\*\*\s*元"          # **84.06 元**（markdown 加粗，实测撞到）
    r"|\*\*\s*[\d.]+\s*元\s*\*\*"   # **84.06 元**
    r"|\*\*[\d.]+\*\*\s*元")        # **84.06** 元


def _restates_price(sentence: str) -> bool:
    """这句话是不是在复述金额（含「N元」「**N 元**」这类）。"""
    return bool(_PRICE_RE.search(sentence))

# ==================================================================== 工厂
def build_client(api_key: str = "", *, model: str = "", transport=None):
    """造一个模型客户端（复用现有的 ModelClient）。"""
    from integrations.model.client import ModelClient
    return ModelClient(api_key, model=model, transport=transport)
