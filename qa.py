"""
qa.py —— 智能问答表（关键词触发）

════════════════════════════════════════════════════════════════
★ 这一层和状态机是【两条并行的回复通道】

  状态机（domain/conversation.py）  —— 流程驱动
      买家发截图 → 识别 → 报价 → 拍下 → 付款 → 出票
      响应的是"买家做了什么动作"

  智能问答（本文件）                —— 关键词触发
      买家发"999" → 回 FAQ 菜单
      买家发"取票" → 回取票说明
      响应的是"买家说了什么词"

  两条通道互不干扰：关键词没命中就交给状态机，状态机没动作就走关键词。

════════════════════════════════════════════════════════════════
★ 核心机制：`[全局调用标识ID]` —— 文案片段复用

  任何文案里写 [bx98yxna9z]，就等于把那条问答的内容【插进来】。

  好处：一段话在几十个地方被引用，改一处全生效。
  证据：报价卡末尾的 [ggx1z82tg84] → 展开是"要吗 亲爱的[送花]…"

  ★ 引用可以嵌套，但必须能收敛 —— resolve() 内置了环检测。
════════════════════════════════════════════════════════════════
"""

import re
from dataclasses import dataclass, field

# 引用语法：方括号包住的小写字母数字 ID
REF_PATTERN = re.compile(r"\[([a-z0-9]{6,16})\]")


# ==================================================================== 触发类型
class Trigger(str):
    REPEAT = "重复触发"      # 同一买家可以反复触发
    ONCE = "单人限制"        # 同一买家只触发一次（配合每日限额）


WANDA_TICKETING_POLICY = (
    "万达由店家处理出票，出票时间以店家实际出票进度为准。"
    "具体是否已下单或出票，需要查询当前订单确认。"
)
UNKNOWN_TICKETING_POLICY = "出票渠道及进度尚未确认，需要先查询当前订单，不能承诺具体出票时间。"
TICKETING_CHANNEL_CONTENTS = {
    "wanda": WANDA_TICKETING_POLICY,
    "liangpiao": None,
    "*": UNKNOWN_TICKETING_POLICY,
}


@dataclass
class QARule:
    """一条智能问答规则。"""
    qa_id: str                                  # ★ 全局调用标识ID
    keyword: str                                # 触发关键字
    content: str                                # 消息内容
    trigger: str = Trigger.REPEAT               # 触发类型
    total_limit: str = "不限制"                  # 总限额
    daily_limit: str = "不限制"                  # 每日限额
    message_type: str = "文本消息"
    # ★★ 2026-10-14 新增：「买家会怎么问」的同义问句
    #   用途【只有一个】：给 agent 的 search_faq 工具做检索。
    #   ★ 为什么需要它：keyword 是"买家回复的编号"（001/002/999），
    #     而买家真实问的是「多久能出票」——
    #     按 keyword 子串匹配永远命不中 ⇒ 模型查不到知识库。
    #   ★★ 它【不】影响 match()（那条路是"逐字可控"，见 match 的注释）。
    questions: list = field(default_factory=list)
    # None inherits editable content; '*' applies when the channel is unknown.
    channel_contents: dict[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.channel_contents, dict):
            raise TypeError("channel_contents 必须是对象")
        if any(not isinstance(key, str) or not key.strip()
               or (value is not None and not isinstance(value, str))
               for key, value in self.channel_contents.items()):
            raise TypeError("渠道名称必须为非空字符串，渠道文案必须为字符串或 null")
        channels = {key.strip().lower(): value for key, value in self.channel_contents.items()}
        if self.qa_id in ("utv1qjdlqg", "a6d4dqvkluc"):
            channels = {**TICKETING_CHANNEL_CONTENTS, **channels}
        self.channel_contents = channels

    def can_trigger(self, used_total: int = 0, used_today: int = 0) -> bool:
        """是否还能触发（配额判断）。"""
        if self.total_limit != "不限制" and used_total >= int(self.total_limit):
            return False
        if self.daily_limit != "不限制" and used_today >= int(self.daily_limit):
            return False
        return True


# ==================================================================== 规则表
# ★ 来源：票达人「智能问答」页面
#
# ★★ 关于出票时效的三个数字 —— 必须知道它们不一致
#
#   票达人页面上写的是「1-30 分钟」「最迟 50 分钟」「10-30 分钟」，
#   良票文档里 STANDARD（特惠）档写的是「1~60 分钟」。
#
#   ★ 这两个都对不上。可能是：
#     (a) 票达人刻意少承诺（承诺 30 分钟，实际给 60 分钟 —— 卖家更安全）
#     (b) 票达人写的是经验值，不是接口保证
#     (c) 票达人页面过期了
#   我【无法从文档判断是哪种】。
#
#   ⇒ 处置：这些文字【原样保留】，因为它们是运营配的内容，
#     而且"少承诺"如果是有意的，我改成 60 分钟反而会让卖家吃亏。
#     但下面的 QA_CHECK 会把它标出来 —— 上线前你要拍板用哪个口径。
#     真正发给买家的报价卡/出票通知走的是 templates.TICKET_WINDOW，
#     那是按实际购入档位渲染的、与文档一致。
QA_RULES: list[QARule] = [
    # ---- 流程状态类（可用状态机主动调用）----
    QARule("a6d4dqvkluc", "等待出票中~~~", "系统已自动下单，请耐心等待出票结果~~",
           questions=["票出到哪一步了", "怎么还没出票", "出票进度", "什么时候能出票", "还在出票吗"],
           channel_contents=dict(TICKETING_CHANNEL_CONTENTS)),
    QARule("utv1qjdlqg", "出票", "出票时间1-30分钟（最迟50分钟）",
           questions=["出票要多久", "多久能出票", "多长时间出票", "出票时间", "几分钟出票", "多久出票"],
           channel_contents=dict(TICKETING_CHANNEL_CONTENTS)),
    QARule("dh17o4vx6ew", "等待付款",
           "影院地址，信息正确吗\n-\n付好我去安排出票~\n-\n"
           "注意不要提交多个订单，如果提交多个订单请不要付款，全部取消后重新发送截图报价，"
           "然后再提交一个订单",
           questions=["影院地址对吗", "地址对不对", "付款前核对什么", "可以付款了吗", "能付款了吗"]),
    QARule("ggx1z82tg84", "恭喜：已抢到特价票限时2分钟秒杀~",
           "要吗 亲爱的[送花]\n要的话您回复一下哦[拜]",
           questions=["还要不要", "这张票要吗", "抢到了吗"]),
    QARule("3ewl2vw1zr3", "999",
           "亲，以下为常见问题，请自行寻找答案\n"
           "1、如何购票？  回复“001”\n"
           "2、支付后多久出票？ 回复“002”\n"
           "3、可以改签和退票吗？回复“003”\n"
           "4、票出错了咋办？回复“004”\n"
           "5、取票码用不了咋办？回复“005”",
           questions=["常见问题", "有哪些问题", "帮我看看常见问题", "问题列表", "FAQ"]),

    # ---- FAQ 明细 ----
    QARule("p9iaick1mwo", "001",
           "选座截图发来、系统自动报价、拍下不忙支付，返回等1-3分钟，系统自动改价、付款、"
           "系统10-30分钟自动将取票码发你。",
           questions=["怎么买票", "如何购票", "购买流程", "怎么下单", "下单流程是什么", "第一次买怎么弄"]),
    QARule("jgqpx0iqqrp", "002",
           "从支付时间算，10-30分钟出票（最迟50分钟出票），出票后系统会自动将取票码发给你；"
           "如出票失败，则立刻退款。\n"
           "特惠票不存在哪家出票速度快、慢的问题，全网所有特惠票订单都会推送给后台所有供货商，"
           "出票速度取决于后台是否有人接单，一般连锁影院出票快、杂牌影院出票慢；"
           "如我这边出票失败，其他商家大概率99%也会出票失败！",
           questions=["支付后多久出票", "付款后多久出票", "多久发货", "出票失败怎么办", "会不会不出票"]),
    QARule("hre3klk29sh", "003",
           "特惠票是后台接单人通过购买兑换券、信用卡积分等途径兑换来的，不是用现金买的，"
           "如果退了或改签，兑换券、积分就失效了。\n"
           "因此系统会在你下单前、支付前多次提醒你付款后、出票时、出票后皆不能改签和退票。\n"
           "请不要擅自在影院退票，否则后果自负！",
           questions=["可以改签吗", "能退票吗", "能不能退", "改签和退票", "退改规则", "为什么不给退", "可以退吗"]),
    QARule("wpt474aht7r", "004",
           "1、座位号跟原座不一致\n"
           "在下订单那个时点，选座易被同行或他人恶意锁定，系统则不再和你沟通，自动择优调座。"
           "请查看上方《购票须知》第2段内容，支付前已多次提醒，你也是同意才下单的。"
           "这类情况不予改签和退票。\n"
           "2、城市或影院完全错了\n"
           "请查看上方记录，系统在您下单前，多次提醒您请认真核对与您的选座是否一致后，再下单。\n"
           "如是您未认真核对报价信息导致出票错误，不予改签和退票。\n"
           "如是我们原因出票错误，请申请退款，我们看到后会跟你联系协商重新出票或退票！",
           questions=["座位不对", "座位号不一致", "票出错了怎么办", "影院错了", "城市错了", "座位跟我选的不一样"]),
    QARule("bapdfgcj5oj", "005",
           "如自助机无法扫码出票，请输入取票码取票；\n"
           "如还不行，请联系前台工作人员取票并打开手机录音；\n"
           "如工作人员告知你无法出票，请将该段对话内容录音保存（尽量包含场次信息等），"
           "凭录音申请退款。\n"
           "请不要擅自在影院退票，否则后果自负！",
           questions=["取票码用不了", "取票码无效", "扫码扫不出来", "自助机取不了票", "取票机不出票", "扫码取不出票", "扫不出来票", "取票码扫不了"]),

    # ---- 出票失败 ----
    QARule("b3ummiqru8l", "出票失败111",
           "为避免影响您进场观影 建议您点击上方链接小额优惠购买哈[比心]，"
           "这边系统已自动给你退款！",
           questions=["出票失败了", "没出票", "出票失败退款了吗"]),

    # ---- 闲鱼文案（买家最常问的话）----
    QARule("ma5n7wanzjo", "可以用吗", "可以，选好座位截图发我，我给您报价",
           questions=["这个可以用吗", "能用吗", "这个可以吗"]),
    QARule("enw6852ff5i", "稍等", "好的，您考虑好了直接拍下不付款就可以哦~"),
    QARule("3zzomddqon4", "取票",
           "出票后我会发给您取票码，在影片开场前到影院的自助取票机扫码取票，"
           "或前往台找工作人员人工取票！",
           questions=["怎么取票", "取票码在哪", "在哪里取票", "怎么拿票", "取票方式"]),
    QARule("mvnraubwqzm", "好的", "[比心]"),
    QARule("kb72fy71j4m", "谢谢", "不客气呢", trigger=Trigger.ONCE, daily_limit="1"),
    QARule("kdpdte8g6cg", "票数超出",
           "如果没有识别到完整的座位，请拍下这单后重新截图发我识别剩余座位即可",
           questions=["座位没识别全", "票数不对", "少识别了座位", "座位数量不对"]),

    # ---- 会员座 / 灰色座位 ----
    #
    # ★ 这两条是【店家确认的口径】，不是我推的。
    #   线上人工回复里也是这个意思（×4 + ×4 条），
    #   而原来的 18 条规则里【一条都没有】——
    #   所以问「会员座能代订吗」只能靠模型现编或转人工。
    #
    # ★ 为什么买家会问：万达的会员座在选座图上显示为【灰色】，
    #   买家自己点不了，所以会问"灰色的是不是不能选"。
    #   口径是"能买，你圈位置就行"。
        # ★★ 2026-10-14 B 组 2 条合并：这几条话术完全相同，
    #   合成一条、keyword 用 | 分隔 —— 以后改一处就全生效。
    QARule("vw2m7k4nq8p", "会员座|灰色",
           "可以买的，辛苦在图片标记一下你需要的位置，再告诉我需要几张就行",
           questions=["会员座能买吗", "灰色座位能买吗", "会员座可以买吗", "灰色能买吗"]),

    # ---- 「可以买吗」----
    #
    # ★ 店家的口径分两种，取决于【买家发过图没有】：
    #
    #   ① 还没发图 → 先让他发图圈位置
    #        「发张选座图，在图上圈一下想要的位置，再告诉我几张就行」
    #   ② 已经发过图（图上圈了位置）→ 直接说能买
    #        「可以买，需要几张呢」
    #
    # ★ 这两条的区分【本规则做不到】—— 关键词回复不看上下文。
    #   所以这条规则的文案取【两种场景都不出错】的那个（①），
    #   而【发过图】的情形由 application/agent.py 先判、直接回"可以买"。
    #   （跟「确认下单」一样：代码知道上下文，别指望关键词/模型猜。）
    #
    # ★ 另外确认过：这里【不判断】具体座位能不能选 ——
    #   人工会根据图片核对出票，代码只要知道"圈没圈位置"就够。
        # ★★ 2026-10-14 A 组 5 条合并：这几条话术完全相同，
    #   合成一条、keyword 用 | 分隔 —— 以后改一处就全生效。
    QARule("tv6n2wq8hkl", "可以买吗|能买吗|可以拍吗|能下吗|能代下",
           "发张选座图，在图上圈一下您想要的位置，再告诉我需要几张就行",
           questions=["可以买吗", "能买吗", "可以拍吗", "能下吗", "能代下", "这个能买吗", "这个可以买吗", "你们能代下吗"]),
    # ★ 线上还有这些同义问法（都是我导出买家消息时数出来的）：
    #     「可以拍吗」×2、「能下吗」、「能代下吗」、「可以买」
    #   全部指向同一句回复 —— 口径一样，不必各写一句。
    # ⚠️ 待确认：页面里 dh17o4vx6ew 与 9psrbvfvc4c 的触发关键字都是「等待付款」，
    #    后者会覆盖前者。这里先只保留 dh17o4vx6ew（内容更完整）。
    QARule("rlr155e0n1", "111",
           "点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）",
           questions=["怎么付款", "付款方式", "在哪里付款", "怎么下单付款", "付不了款"]),
]


# ==================================================================== 索引
BY_ID: dict[str, QARule] = {rule.qa_id: rule for rule in QA_RULES}
BY_KEYWORD: dict[str, QARule] = {rule.keyword: rule for rule in QA_RULES}


def reindex() -> None:
    """
    重建索引。★ 界面改了 QA_RULES 之后【必须调这个】。

    这两个字典是模块加载时建好的。运营在界面上加一条、改一个关键字，
    如果只改 QA_RULES 不重建索引，匹配走的还是旧的 ——
    表现为"界面改了但回复没变"，很难查。

    ★ 重复关键字怎么处理：后写的覆盖先写的，但会留下第一个的顺序。
      真实系统里应该在校验阶段就拦住重复关键字（界面上会提示）。
    """
    BY_ID.clear()
    BY_ID.update({rule.qa_id: rule for rule in QA_RULES})
    BY_KEYWORD.clear()
    for rule in QA_RULES:
        BY_KEYWORD[rule.keyword] = rule            # 同名后写的赢



# ==================================================================== 引用展开
class ReferenceError(Exception):
    """引用出问题：找不到、或形成环。"""


def _channel_content(rule: QARule, channel: str | None) -> str:
    """Select policy without mutating shared rules or guessing a supplier."""
    if channel is None or not rule.channel_contents:
        return rule.content
    key = str(channel).strip().lower()
    content = rule.channel_contents.get(key, rule.channel_contents.get("*"))
    return rule.content if content is None else content


def render_rule(rule: QARule, *, channel: str | None = None) -> str:
    """Apply the same channel to the selected rule and every nested reference."""
    return resolve(_channel_content(rule, channel), channel=channel)


def reference_problems(rules: list[QARule]) -> list[str]:
    """Validate submitted rules, including channel-only cycles and references."""
    by_id = {rule.qa_id: rule for rule in rules}
    channels = {None, ""}
    for rule in rules:
        channels.update(key for key in rule.channel_contents if key != "*")

    def check(rule, channel, seen):
        if rule.qa_id in seen:
            raise ReferenceError("引用成环：" + " → ".join((*seen, rule.qa_id)))
        if len(seen) > 10:
            raise ReferenceError("引用层级过深")
        for ref in all_references(_channel_content(rule, channel)):
            if ref not in by_id:
                raise ReferenceError("引用了不存在的 ID [" + ref + "]")
            check(by_id[ref], channel, (*seen, rule.qa_id))

    problems = []
    for rule in rules:
        for channel in sorted(channels, key=lambda value: str(value)):
            try:
                check(rule, channel, ())
            except ReferenceError as exc:
                problems.append(f"{rule.keyword}（{channel or '默认'}）：{exc}")
    return problems


def resolve(text: str, *, channel: str | None = None,
            _seen: tuple = (), _depth: int = 0) -> str:
    """
    把文案里的 [全局调用标识ID] 展开成实际内容。**支持嵌套，带环检测。**

    ★ 为什么要环检测：
      如果 A 的内容里引用了 B，而 B 又引用 A，展开就会无限循环。
      真实系统里这种配置错误迟早会出现，宁可在展开时就报错，
      也不要在给买家发消息时卡死。

    找不到的 ID 原样保留 —— 方便发现配置错误，不静默吞掉。
    """
    if _depth > 10:
        raise ReferenceError(f"引用层级过深，可能是环：{_seen}")

    def replace(match: re.Match) -> str:
        qa_id = match.group(1)
        rule = BY_ID.get(qa_id)
        if rule is None:
            return match.group(0)              # 找不到就原样保留
        if qa_id in _seen:
            raise ReferenceError(f"引用成环：{' → '.join(_seen + (qa_id,))}")
        return resolve(_channel_content(rule, channel), channel=channel,
                       _seen=_seen + (qa_id,), _depth=_depth + 1)

    return REF_PATTERN.sub(replace, text)


def keywords_of(rule: QARule) -> list:
    """一条规则的全部关键字（keyword 支持 | 分隔多个说法）。

    ★ 为什么要拆：把"话术完全相同"的重复条目合并成一条时，
      关键词写成 `可以买吗|能买吗|可以拍吗`。
      ⇒ 外部想按单个关键字找规则，必须走这里，
        不能直接拿 rule.keyword 当唯一键（那只是显示用的合成串）。
    """
    return [k.strip() for k in str(rule.keyword or "").split("|") if k.strip()]


def find_by_keyword(keyword: str) -> QARule | None:
    """按【单个关键字】找规则（认得 | 分隔）。找不到返回 None。"""
    want = (keyword or "").strip()
    if not want:
        return None
    for rule in QA_RULES:
        if want in keywords_of(rule):
            return rule
    return None


def has_keyword(keyword: str) -> bool:
    """这个关键字有没有配置（配置界面/测试问的就是这个）。"""
    return find_by_keyword(keyword) is not None


def match(message: str) -> QARule | None:
    """
    买家发来一句话，看有没有命中关键词规则。

    匹配口径：**关键词包含在消息里**（不是完全相等）——
    因为买家可能说"请问999怎么用"。
    """
    text = (message or "").strip()
    if not text:
        return None
    # 先精确匹配
    if text in BY_KEYWORD:
        return BY_KEYWORD[text]
    # ★★ 2026-10-14：一个条目可以有多个关键字，用 | 分隔
    #   （合并"话术完全相同"的重复条目时用的）
    #   ★ 等价性：原来 5 条各自子串匹配，现在 5 个关键字共用一条子串匹配，
    #     命中集合完全相同 —— 纯增量，不改行为。
    for keyword in sorted(_all_keywords(), key=len, reverse=True):
        if keyword in text:
            return BY_KEYWORD.get(keyword) or _by_alias(keyword)
    return None


def _all_keywords() -> list:
    """所有关键字（含 | 分隔的展开）。"""
    out = []
    for k in BY_KEYWORD:
        out.extend(p for p in k.split("|") if p)
    for r in QA_RULES:
        out.extend(p for p in str(r.keyword).split("|") if p)
    return list(dict.fromkeys(out))


def _by_alias(keyword: str):
    """按展开后的单个关键字找回它所属的规则。"""
    for r in QA_RULES:
        if keyword in [p for p in str(r.keyword).split("|") if p]:
            return r
    return None


def reply(message: str, *, used_total: int = 0, used_today: int = 0,
          channel: str | None = None) -> str | None:
    """命中就返回展开后的文案；没命中返回 None（交给状态机处理）。"""
    rule = match(message)
    if rule is None or not rule.can_trigger(used_total, used_today):
        return None
    return render_rule(rule, channel=channel)


def all_references(text: str) -> list[str]:
    """列出文案里引用了哪些 ID（用于校验）。"""
    return REF_PATTERN.findall(text)


if __name__ == "__main__":
    print(f"共 {len(QA_RULES)} 条智能问答规则\n")
    print(f"{'ID':>14}  {'触发关键字':<16} {'触发类型':<8} {'每日':<6} 内容")
    print("-" * 96)
    for rule in QA_RULES:
        head = rule.content.splitlines()[0][:36]
        print(f"{rule.qa_id:>14}  {rule.keyword:<16} {rule.trigger:<8} {rule.daily_limit:<6} {head}")

    print()
    print("=" * 96)
    print("引用展开演示")
    print("=" * 96)
    demo = "[ggx1z82tg84]\n[kdpdte8g6cg]"
    print(f"  原文: {demo!r}")
    print(f"  展开:\n{resolve(demo)}")
