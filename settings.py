"""
settings.py —— 系统配置（票达人「功能设置」页同款）

■ 依据：../票达人-配置规格.md
■ 这个文件回答一个问题：**哪些行为是可调的，默认值是多少。**

════════════════════════════════════════════════════════════════
★ 三层结构，各管一件事

  settings.py      哪些可调、默认多少      ← 本文件
  conversation.py  什么时候做什么          ← 状态机
  templates.py     说什么话                ← 文案表

  比如「报价记录有效时长 30 分钟」：
    settings 定义它 = 30
    conversation 用它判断过期
    templates 的过期文案引用它

★ 每个字段都带 UI 元数据（label/control/min/max）
  这样配置页**不用手写 HTML** —— 前端照着元数据自动渲染控件。
  加一个配置项，UI 自动多一行，不会忘。
════════════════════════════════════════════════════════════════
"""

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints


# ==================================================================== 控件类型
class C:
    """UI 控件类型。前端照这个决定渲染成什么。"""
    SWITCH = "switch"        # 开关
    CHECKS = "checks"        # 复选框组（多选）
    RADIO = "radio"          # 单选组
    NUMBER = "number"        # 数字
    FLOAT = "float"          # 小数
    TEXT = "text"            # 单行输入
    TEXTAREA = "textarea"    # 多行输入
    TIME = "time"            # 单个时间
    TIME_RANGE = "time_range"        # 时间范围
    NUMBER_RANGE = "number_range"    # 数字范围（如延迟 100-500 毫秒）
    SORTABLE = "sortable"    # 可排序列表（优先报价模式）
    PICKER = "picker"        # 按钮弹窗（首次打招呼）
    # ★★ 2026-10-14 新增：选一张图上传。
    #   为什么要有它：取票图是【店主的图】，得有个地方传上来。
    #   ★ 图的内容【不进 settings.json】—— 那是一段 base64，
    #     混进去会让配置文件难读、每次读盘都变慢。
    #     ⇒ 存 data/ticket-image.json，由 application/ticket_image.py 管；
    #       这个控件只负责把图交给后端存下来。
    IMAGE = "image"          # 图片上传


# ==================================================================== 未接线注册表
# ★★★ 这是什么、为什么要有它
#
#   设置页上有 42 个控件属于"能动、能存、但没有任何代码读它"。
#   它们的危害是【骗人】：运营以为关掉了某个功能，其实没有 ——
#   比"功能没做"更糟，因为后者一眼就知道没有。
#
#   ★ 为什么不直接删掉：
#     删了以后想接上去要重写字段 + 界面 + 保存白名单（三处）；
#     标清楚只改一处 —— 接上去时把这里对应那行删掉即可。
#     而且删掉之后，运营会以为"本来就没这功能"，
#     不如显式写着"未接线"，让人知道这是【已知的未完成】。
#
#   ★★ 维护规则（有一条断言盯着）：
#     任何控件，如果【不】在 UNWIRED 里，就必须真的有人读它的值。
#     以后新增一个"存了没人读"的字段 ⇒ 断言失败。
#     这就是防"摆设再次长出来"的机制。
#
#   ★ 键是 section 名（settings.py 里的 dataclass 名），值是 {字段名: 原因}。
#     写"原因"而不是只写字段名：三个月后才分得清
#     "没来得及做" 和 "故意不做"。
UNWIRED: dict[str, dict[str, str]] = {
    # ★ 2026-10-14：「优先报价模式」已接线，从这里摘掉了。
    #   （它是 recognize_quote 段里唯一一项，所以整段删除。
    #     保留空字典会让"这一段还有未接线项"看起来像真的。）
    # 还没接
    "fish_reprice": {
        "need_user_confirm": "还没接",   # 确认改价方式
        "unique_command": "还没接",   # 指令模式
    },
    # 付款回调里没做金额校验分支；没有下架触发点
    "amount_check": {
        "offshelf_reply": "尚未接入商品下架事件",   # 下架商品咨询回复
    },
    # ★★ 2026-10-14：「出票及放单」已接线，整段摘掉。
    #   · 出票方式（手动/自动）→ ticket_flow._release_mode()
    #       手动 = 不自动建单，只推状态 + 提示店家去后台出票
    #   · 换座状态（允许/不允许）→ ticket_flow._allow_seat_change()
    #       真传进 api.ch5_create(allow_seat_change=...) 的 payload
    #       ★ 它原来是个【静默失效】的开关：payload 里一直有 allowSeatChange
    #         字段，但调用处从来没传 ⇒ 永远用签名默认 True。
    #   · 票速设置 → 早就接了（ticket_flow._pick_ticket_mode）
    #
    # ★★ 2026-10-14：「出票溢价」已接线，整段摘掉。
    #   它替掉了 domain/quote_rules.py 里写死的 FALLBACK_LIMIT_YUAN = 3：
    #   没规则命中时，兜底限价 = 成本合计 + 张数 × 「溢价上限」元/张。
    #   ★ 未启用开关时【仍然用原来的 3】（不是 0）——
    #     默认就改成 0 会让一部署就悄悄收紧所有兜底限价，
    #     方向还是"更容易下单失败"。那是拿配置变更改钱，店主没要求这个。
    # 重放单还没实现（没有重试循环）
    "reorder": {
        "max_retries": "重放单还没实现（没有重试循环）",   # 重新放单次数
        "add_limit_percent": "重放单还没实现（没有重试循环）",   # 每次添加限价比例
    },
    # 发货闸门没接自动/强制分支
    "order_ship": {

    },
    # 退款流程还是人工（没有自动退款/时序判断）
    "order_refund": {
        "return_info": "影票业务未实现实物退货信息",   # 退货信息
    },
    # 取票图只有一种模板
    "ticket_image": {
    },
    # ★★ 2026-10-14：「获取票码」已经接完了，从这里摘掉。
    #   原来标着"无票场景没接话术"，但我查过：它其实【早就接了】——
    #     service.py `_wants_ticket_code()` 读 keyword、
    #     `_buyer_ticket_codes()` 查已出票的单，
    #     没单时读 `no_ticket_reply`。
    #   ★ 那条标记是过期的假信息：界面因此把这个填得好好的框
    #     标成「未接线」还置灰，让人以为没用。
    #   ⇒ 教训：标记要跟着代码走，否则它会比没有标记更误导。
    #
    # ★★ 2026-10-14：「黑名单」已接线，从「没有数据源」里摘掉。
    #   数据源查清了：鱼麦多 `GET /im/blacklist/:peerUnb`
    #   （scope `im.buyer_blacklist.read`），链路是
    #     后端 → 插件 /internal/blacklist/query → 平台
    #   见 application/blacklist.py。
    #   ★ 但要如实说明【当前还未生效】：实测那个租户还没同意这个权限，
    #     查询返回 403 ⇒ 我们按 fail-open 处理（查不到就照常回复）。
    #     租户去应用市场订阅页勾选同意后，这个话术才会真的生效。
    #   ★ 所以这里【不再标"未接线"】（代码确实接了），
    #     但界面上那条 hint 要写明"还差租户同意"（见 blacklist.py 注释）。
    # 还没接
    "call_manual": {
        "keyword": "还没接",   # 呼叫人工关键词
    },
}


def unwired_reason(section_key: str, field_name: str) -> str:
    """这个控件是不是未接线；是的话给出原因，否则空串。"""
    return (UNWIRED.get(section_key) or {}).get(field_name, "")


def opt(**kw) -> Any:
    """字段元数据。用 field(metadata=opt(...)) 挂上去。"""
    return kw


# ==================================================================== AI助理默认
# ★ 这三段是【运营可改】的部分：商店是谁、要注意什么、怎么说话。
#   它们会和 prompts/persona.py 里的 HARD_RULES（不可改层）拼在一起。
#   分层的理由：语气天天调，风控条款不能天天调。
DEFAULT_PERSONA = """你是一家影票代订网店的客服。

业务怎么运转：
  买家发来选座截图 → 系统识别出影院场次座位并报价 → 买家拍下
  → 系统改价 → 买家付款 → 系统自动出票并把取票码发给他。

你的位置：
  你是这条链路上的对话层。报价、改价、出票由系统完成，你不做这些决定。
  遇到和你掌握的信息不一致的情况，以系统为准，不要自己判断。"""

DEFAULT_PRECAUTIONS = """1. 买家问「多久出票」，按【实际购入档位】回答，不要写死一个数字：
   - 慢票(STANDARD) 1-60 分钟
   - 快票(FAST)     15 分钟内
   - 急速票(FLASH)  1-15 分钟
   这三个数字来自良票文档的 TicketMode 说明。
   ★★ 曾经这里写死成「1-30 分钟（最迟 50 分钟）」——
     那是票达人页面上的说法，与文档的 STANDARD 1~60 分钟不符。
     告诉买家比实际更短的时间，超时的那一单会变成投诉。
     模板那边已经改成按档位渲染了，这段也要一致，否则会教模型说错话。
2. 买家要求拦截订单，告诉他可以发「拦截订单」，但要说明不保证拦截成功。
3. 买家问能不能退改，如实说明付款后不支持退改签，不要含糊。
4. 买家催单，先安抚，不要承诺具体分钟数以外的时限。
5. 报价明显高于买家自己能买到的价格时，如实建议他自己买，不要硬卖。"""

DEFAULT_REPLY_STYLE = """说话短，像闲鱼上正常的卖家，不像客服机器人。
多用"哈""哦""~"这类语气词，但别堆。
每次只解决一件事，不要一次抛一堆信息。
不确定时先查可查事实，缺信息只问缺项；确需店家处理时再说明转人工，不要编。"""

# ★★ 本店经验
#
#   ★★★ 这里是【空的】，我没有填任何内容。
#
#   我不知道你店里哪些问法最多、哪些坑踩过、哪些口径必须统一 ——
#   这些只有你知道。编一份假的填进来，比空着更糟：
#   模型会照着编的东西答买家，而你以为那是自己写的。
#
#   所以下面只留结构说明，运营照着填。
DEFAULT_EXPERIENCE = """（这里空着，等你自己填。）

填什么：
1. 买家常问、但又不值得单独配一条关键词回复的问题
2. 你们店自己的口径（比如出票时效、能不能选座）
3. 容易引起纠纷、必须统一说法的
4. 哪些情况明显要转人工

怎么写（模型最容易照做的形式）：
    问法 → 怎么答
    问法 → 转人工

例：
    （示例格式，内容请替换成你自己的）

    （注意：上面这些都是格式示例，不是你的经验。删掉重写。）"""



# ==================================================================== 报价类
@dataclass
class RecognizeQuote:
    """识别报价"""
    auto_recognize: bool = field(default=True, metadata=opt(
        label="自动识别报价", control=C.SWITCH))
    quote_priority: list = field(default_factory=lambda: [
        "简易报价规则", "专业报价规则", "API报价规则", "比价模式",
    ], metadata=opt(label="优先报价模式", control=C.SORTABLE,
                    hint="拖动排序，靠上的优先；点前面的圆点可停用",
                    all_options=["简易报价规则", "专业报价规则", "API报价规则", "比价模式"],
                    # ★★★ 2026-10-14：哪几个模式真的实现了。
                    #   店主确认："现在我们设计的报价规则就是简易报价规则，
                    #             专业报价规则更加复杂，我没有设计。"
                    #   ⇒ 只有「简易」有实现；其余三个是【曾经设想过的方向】，
                    #     不是"功能坏了"。所以标出来，而不是留着让人以为能用。
                    #   ★ 为什么标"未实现"而不是删掉：
                    #     删掉之后没人知道曾经规划过这几条路；
                    #     标出来，界面会显示「未实现」且不可拖动/不可启用，
                    #     代码里也不会被误当成"已支持"。
                    implemented=["简易报价规则"]))
    quote_priority_enabled: list = field(default_factory=lambda: [
        "简易报价规则", "专业报价规则", "API报价规则",
    ], metadata=opt(label="启用的报价模式", control=C.CHECKS, hidden=True))
    # ★ 这就是"限时2分钟"的真相：实际是 30 分钟。
    #   文案里的"限时2分钟秒杀"是营销压力话术，不是真实时效。
    quote_ttl_minutes: int = field(default=30, metadata=opt(
        label="良票报价记录有效时长", control=C.NUMBER, min=1, max=1440, unit="分钟",
        hint="仅用于良票。万达报价不设时间有效期；更新购票需求后使用最新报价。"))
    keyword_cinema: str = field(default="影院", metadata=opt(
        label="影院关键字（兼容用法）", control=C.TEXT, placeholder="粉丝回复示例：影院1",
        hint="当前主流程直接回复 1/2/3；只有使用“影院1”这类带前缀回复时才会用到。"))
    keyword_movie: str = field(default="影片", metadata=opt(
        label="影片关键字（兼容用法）", control=C.TEXT, placeholder="粉丝回复示例：影片1"))
    keyword_show: str = field(default="场次", metadata=opt(
        label="场次关键字（兼容用法）", control=C.TEXT, placeholder="粉丝回复示例：场次1"))


@dataclass
class FallbackQuote:
    """兜底报价"""
    enabled: bool = field(default=True, metadata=opt(label="兜底报价开关", control=C.SWITCH))
    mode: str = field(default="预估限价", metadata=opt(
        label="兜底报价模式", control=C.RADIO, options=["预估限价", "一口价"],
        disabled_options=["一口价"]))
    price_capped: bool = field(default=True, metadata=opt(
        label="兜底是否限价", control=C.RADIO, options=["启用限价", "不启用限价"],
        values=[True, False]))


@dataclass
class PriceMismatch:
    """截图金额与识别座位票价不一致时的处理。"""
    mode: str = field(default="按识别失败处理", metadata=opt(
        label="处理方式", control=C.RADIO,
        options=["按识别成功处理", "按识别失败处理"],
        hint="按识别成功处理时仍必须调用渠道接口核价，截图金额不会作为售价。"))
    message: str = field(default=(
        "亲，识别到座位价格与截图总价不一致，请重新选座后再发送截图~"), metadata=opt(
        label="票价不一致话术", control=C.TEXTAREA,
        tokens=["粉丝昵称", "影院名", "影片名", "放映时间"]))


@dataclass
class SecondQuote:
    """二次报价"""
    enabled: bool = field(default=False, metadata=opt(label="二次报价开关", control=C.SWITCH))
    wait_seconds: int = field(default=30, metadata=opt(
        label="等待拍下时间", control=C.NUMBER, min=5, max=600, unit="秒"))
    discount_percent: int = field(default=3, metadata=opt(
        label="二次报价优惠比例", control=C.NUMBER, min=1, max=10, unit="%"))


@dataclass
class ShowtimeLimit:
    """放映时间限制"""
    remaining_minutes: int = field(default=40, metadata=opt(
        label="放映剩余分钟", control=C.NUMBER, min=0, max=360, unit="分钟",
        hint="距离开场不足这么多分钟就不报价"))
    message: str = field(default=(
        "您好，您截图的影片场次距离开始放映不足[限制间隔]分钟，"
        "无法优惠购票，建议您选择其他场次"), metadata=opt(
        label="放映时间限制话术", control=C.TEXTAREA,
        tokens=["粉丝昵称", "放映时间", "限制间隔"]))


@dataclass
class NightRecognize:
    """夜间识别"""
    start: str = field(default="23:30", metadata=opt(
        label="提醒时段", control=C.TIME_RANGE, pair="end"))
    end: str = field(default="08:00", metadata=opt(hidden=True))
    message: str = field(default=(
        "当前时间段，特惠渠道系统维护，可能会出票失败，"
        "建议早上8点后重新发选座图识别后下单~"), metadata=opt(
        label="补充提醒话术", control=C.TEXTAREA, tokens=["粉丝昵称"]))


# ==================================================================== 下单类
@dataclass
class UrgeOrder:
    """催促下单"""
    delay_minutes: int = field(default=60, metadata=opt(
        label="订单催促提醒时间", control=C.NUMBER, min=0, max=120, unit="分钟"))
    message: str = field(default=(
        "您查询的在[影院名]于[放映时间]放映的影片[影片名]，"
        "优惠购票通道即将关闭，如需观影请尽快按提示下单"), metadata=opt(
        label="订单催促提醒话术", control=C.TEXTAREA,
        tokens=["影院名", "影片名", "放映时间", "场次名称", "座位编号", "影院地址"]))


@dataclass
class FishReprice:
    """闲鱼改价"""
    auto_reprice: bool = field(default=True, metadata=opt(label="自动改价开关", control=C.SWITCH))
    need_user_confirm: bool = field(default=False, metadata=opt(
        label="确认改价方式", control=C.RADIO,
        options=["需要用户确认", "无需用户确认"], values=[True, False]))
    unique_command: bool = field(default=False, metadata=opt(
        label="指令模式", control=C.RADIO,
        options=["唯一指令", "固定指令"], values=[True, False]))
    # ★ 买家确认改价要回复的字。报价卡文案里的 {confirm_cmd} 直接读这里。
    confirm_keyword: str = field(default="正确", metadata=opt(
        label="确认关键字", control=C.TEXT, placeholder="请输入关键字",
        hint="报价卡文案里的 {confirm_cmd} 读的就是这个值"))
    confirm_aliases: list = field(default_factory=lambda: ["正确"], metadata=opt(
        label="也接受的同义指令", control=C.CHECKS, hidden=True))


@dataclass
class AmountCheck:
    """校验与下架"""
    verify_amount: bool = field(default=True, metadata=opt(label="校验金额", control=C.SWITCH))
    offshelf_reply: str = field(default=(
        "您咨询宝贝已售罄或下架啦！辛苦动动小手点击我的头像，"
        "进主页随便挑一个在售的影票宝贝私信我~"), metadata=opt(
        label="下架商品咨询回复", control=C.TEXTAREA))


# ==================================================================== 出票类
@dataclass
class TicketRelease:
    """出票及放单"""
    mode: str = field(default="自动", metadata=opt(
        label="出票方式", control=C.RADIO, options=["手动", "自动"]))
    allow_seat_change: bool = field(default=True, metadata=opt(
        label="换座状态", control=C.RADIO, options=["允许", "不允许"], values=[True, False]))
    # ★★ 2026-10-14 修：这是一个【复选框组 → 三个标量字段】的控件。
    #   keys 按 options 顺序一一对应，告诉前端每一项该写哪个字段。
    #   ★ 原来没有 keys ⇒ 三个复选框全写进 speed_flash，
    #     speed_fast / speed_slow 永远改不了，
    #     而 speed_flash 一保存就变成数组（[] 被当成 False）。
    speed_flash: bool = field(default=True, metadata=opt(
        label="票速设置", control=C.CHECKS,
        options=["启用慢票（40分钟以上）", "启用快票（15分钟以上）", "启用急速票（1分钟）"],
        keys=["speed_slow", "speed_fast", "speed_flash"],
        group_label="票速设置"))
    speed_fast: bool = field(default=True, metadata=opt(hidden=True))
    speed_slow: bool = field(default=True, metadata=opt(hidden=True))


@dataclass
class TicketPremium:
    """出票溢价"""
    enabled: bool = field(default=False, metadata=opt(label="调整限价", control=C.SWITCH))
    cap_yuan: float = field(default=0.00, metadata=opt(
        label="溢价上限", control=C.FLOAT, min=0, max=1000, unit="元"))


@dataclass
class Reorder:
    """重放订单"""
    max_retries: int = field(default=0, metadata=opt(
        label="重新放单次数", control=C.NUMBER, min=0, max=5, unit="次"))
    add_limit_percent: float = field(default=1.0, metadata=opt(
        label="每次添加限价比例", control=C.FLOAT, min=0.5, max=5, step=0.1, unit="%"))


@dataclass
class OrderIntercept:
    """订单拦截"""
    keyword: str = field(default="拦截订单", metadata=opt(
        label="订单拦截关键字", control=C.TEXT, maxlength=50))
    message: str = field(default=(
        "系统已为您发起拦截，但不保证拦截成功。"
        "拦截成功订单将出票失败处理，请您留意下消息~"), metadata=opt(
        label="订单拦截话术", control=C.TEXTAREA,
        tokens=["影院名", "影片名", "放映时间", "场次名称", "座位编号", "影院地址"]))


@dataclass
class OrderAutoIntercept:
    """订单自动拦截"""
    enabled: bool = field(default=False, metadata=opt(
        label="订单自动拦截开关", control=C.SWITCH))
    message: str = field(default="您申请了退款，系统已自动进行出票拦截，请等待出票结果",
                         metadata=opt(label="订单自动拦截话术", control=C.TEXTAREA))


# ==================================================================== 发货类
@dataclass
class OrderShip:
    """订单发货"""
    auto_ship: bool = field(default=True, metadata=opt(label="自动发货", control=C.SWITCH))
    force_ship: bool = field(default=True, metadata=opt(
        label="强制发货开关", control=C.RADIO, options=["关闭", "开启"], values=[False, True]))
    force_ship_reply: str = field(default=(
        "收到您的退款申请后，我们第一时间为您提交了系统拦截。"
        "但由于特惠购票系统是全自动出票，在拦截结果出来前，影院系统已经成功出票。"
        "小店已为您转为正常发货，请您按时观影。"), metadata=opt(
        label="强制发货回复", control=C.TEXTAREA))


@dataclass
class OrderRefund:
    """订单退款"""
    # ★ 四个独立复选框，各自决定"这种情况要不要自动退款"。
    #   勾选状态由使用者自己定，这里不预设对错。
    auto_refund_on: list = field(default_factory=lambda: ["出票失败", "校验金额/改价失败"],
                                 metadata=opt(
        label="自动取消/同意退款", control=C.CHECKS,
        options=["出票失败", "校验金额/改价失败", "创建订单失败", "无有效报价记录"],
        hint="勾选的情况会自动退款，未勾的需要人工处理"))
    weekdays: list = field(default_factory=lambda: [
        "周一", "周二", "周三", "周四", "周五", "周六", "周日"], metadata=opt(
        label="星期", control=C.CHECKS,
        options=["周一", "周二", "周三", "周四", "周五", "周六", "周日"]))
    time_start: str = field(default="00:00:00", metadata=opt(
        label="时间段", control=C.TIME_RANGE, pair="time_end"))
    time_end: str = field(default="23:59:59", metadata=opt(hidden=True))
    delay_seconds: int = field(default=0, metadata=opt(
        label="延迟订单退款时间", control=C.NUMBER, min=0, max=600, unit="秒"))
    return_info: dict = field(default_factory=dict, metadata=opt(
        label="退货信息", control=C.PICKER, picker="return_info"))


@dataclass
class TicketImageTemplate:
    """取票图模板"""
    enabled: bool = field(default=False, metadata=opt(label="取票图模板开关", control=C.SWITCH))
    # ★★ 2026-10-14：「模板1」就是【店主传的那一张图】。
    #   为什么不用一行字存 URL：图是店主传上来的，得有个上传入口。
    #   ⇒ 这个控件把图交给 application/ticket_image.py 存起来；
    #     上传到平台拿 alicdn URL 是【出票时】才做（那时插件才一定在跑）。
    #   ★ 值仍然保留 template 这个名字（不是新字段）——
    #     以后要多种模板（模板1/模板2），这里就是入口，不用改结构。
    template: str = field(default="模板1", metadata=opt(
        label="取票图", control=C.IMAGE, picker="ticket_image", preview=True))


@dataclass
class GetTicketCode:
    """获取票码"""
    keyword: str = field(default="获取票码", metadata=opt(
        label="获取票码关键词", control=C.TEXT, maxlength=10))
    no_ticket_reply: str = field(default="您还没有出票成功的订单~", metadata=opt(
        label="无出票回复", control=C.TEXTAREA))


# ==================================================================== 提醒类
@dataclass
class MovieRemind:
    """观影提醒"""
    advance_minutes: int = field(default=10, metadata=opt(
        label="观影提醒时间", control=C.NUMBER, min=0, max=60, unit="分钟"))
    message: str = field(default=(
        "您购买的【[影片名]】开始检票了，记得及时取票观影哦~\n\n"
        "取票后辛苦收货好评一下，小店垫资垫不起了[大哭]"), metadata=opt(
        label="观影提醒话术", control=C.TEXTAREA,
        tokens=["粉丝昵称", "影院名", "影片名", "放映时间"]))


@dataclass
class UrgeReceive:
    """催收货"""
    delay_minutes: int = field(default=0, metadata=opt(
        label="催收货提醒时间", control=C.NUMBER, min=0, max=30, unit="分钟"))
    message: str = field(default="", metadata=opt(
        label="催收货提醒话术", control=C.TEXTAREA,
        tokens=["粉丝昵称", "影院名", "影片名", "放映时间", "场次名称", "座位编号", "影院地址"]))


@dataclass
class UrgeReview:
    """催好评"""
    delay_minutes: int = field(default=0, metadata=opt(
        label="催好评提醒时间", control=C.NUMBER, min=0, max=30, unit="分钟"))
    message: str = field(default="", metadata=opt(
        label="催好评提醒话术", control=C.TEXTAREA,
        tokens=["粉丝昵称", "影院名", "影片名", "放映时间", "场次名称", "座位编号", "影院地址"]))


@dataclass
class Blacklist:
    """黑名单"""
    reply: str = field(default="系统升级维护中，建议您咨询其他商家", metadata=opt(
        label="黑名单用户回复话术", control=C.TEXTAREA))


# ==================================================================== 互动类
@dataclass
class ReplySetting:
    """回复"""
    greeting_configured: bool = field(default=False, metadata=opt(
        label="首次打招呼设置", control=C.PICKER, picker="greeting", action="设置"))
    greeting_message_types: list = field(default_factory=lambda: ["文字", "图片"],
                                         metadata=opt(hidden=True))
    greeting_interval_hours: int = field(default=0, metadata=opt(
        label="间隔时长（小时）", control=C.NUMBER, min=0, max=48,
        hint="单人触发间隔时长"))
    intelligent_qa_enabled: bool = field(default=True, metadata=opt(
        label="智能问答功能开关", control=C.SWITCH))


@dataclass
class MessageDelay:
    """消息延迟"""
    enabled: bool = field(default=False, metadata=opt(label="消息发送延迟", control=C.SWITCH))
    min_ms: int = field(default=100, metadata=opt(
        label="延迟时间", control=C.NUMBER_RANGE, pair="max_ms", unit="毫秒",
        hint="随机延迟，让回复像真人而不是机器人秒回"))
    max_ms: int = field(default=500, metadata=opt(hidden=True))


@dataclass
class ManualIntervention:
    """人工介入"""
    close_after_hours: int = field(default=1, metadata=opt(
        label="关闭人工介入延迟", control=C.NUMBER, min=0, max=36, unit="小时"))
    close_on_status: list = field(default_factory=lambda: ["已发货", "已退款", "交易关闭"],
                                  metadata=opt(
        label="关闭人工介入订单状态", control=C.CHECKS,
        options=["已发货", "已退款", "交易关闭"]))


@dataclass
class CallManual:
    """呼叫人工"""
    keyword: str = field(default="人工客服", metadata=opt(
        label="呼叫人工关键词", control=C.TEXT, maxlength=10))
    reply: str = field(default="已为您呼叫人工客服，请稍等", metadata=opt(
        label="呼叫人工应答", control=C.TEXTAREA))


# ==================================================================== 分组导航
# 左侧导航：5 大类 → 各配置节。顺序就是展示顺序。
NAV: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("报价", [
        ("recognize_quote", "识别报价", "从截图识别出场次座位，并算出报价"),
        ("price_mismatch", "票价不一致处理", "截图金额与识别票价不一致时的处理"),
        ("fallback_quote", "兜底报价", "正常报价失败时的兜底策略"),
        ("second_quote", "二次报价", "买家犹豫时给的二次优惠"),
        ("showtime_limit", "放映时间限制", "距开场太近就不接单"),
        ("night_recognize", "夜间识别", "夜间系统维护时段的提醒"),
    ]),
    ("下单", [
        ("urge_order", "催促下单", "报价后迟迟不拍下的提醒"),
        ("fish_reprice", "闲鱼改价", "改价流程与确认指令"),
        ("amount_check", "校验与下架", "实付金额校验、下架商品回复"),
    ]),
    ("出票", [
        ("ticket_release", "出票及放单", "出票方式、换座、票速档位"),
        ("ticket_premium", "出票溢价", "允许在限价上浮动的额度"),
        ("reorder", "重放订单", "出票失败后重新放单的次数与加价"),
        ("order_intercept", "订单拦截", "买家主动要求拦截订单"),
        ("order_auto_intercept", "订单自动拦截", "买家申请退款时自动拦截"),
    ]),
    ("发货", [
        ("order_ship", "订单发货", "自动发货与强制发货"),
        ("order_refund", "订单退款", "什么情况自动退款、什么时段退"),
        ("ticket_image", "取票图模板", "出票成功后附带的取票示意图"),
        ("get_ticket_code", "获取票码", "买家索要取票码时怎么回"),
    ]),
    ("提醒", [
        ("movie_remind", "观影提醒", "开场前的提醒"),
        ("urge_receive", "催收货", "提醒买家确认收货"),
        ("urge_review", "催好评", "提醒买家给好评"),
        ("blacklist", "黑名单", "黑名单用户统一回复"),
    ]),
    ("互动", [
        ("reply", "回复", "首次打招呼、智能问答开关"),
        ("message_delay", "消息延迟", "发送前的随机延迟"),
        ("manual", "人工介入", "什么时候撤出人工、恢复自动"),
        ("call_manual", "呼叫人工", "买家呼叫人工的关键词与应答"),
    ]),
]


# ==================================================================== AI助理
@dataclass
class Assistant:
    """
    AI 助理配置。

    ★ 这是**独立于业务配置**的一层：
      业务配置（下面的 Settings）决定"机器怎么运转"，
      助理配置决定"对话层怎么说话"。

      所以两套分开存、分开读，不要混在一起 ——
      运营调语气的时候不该动到报价有效期。
    """
    # 开关
    agent_enabled: bool = True

    # 业务范围（多选）
    business_scope_options: list = field(default_factory=lambda: ["不限制", "影票代订"])
    business_scope: list = field(default_factory=lambda: ["影票代订"])

    # 时机：这两个值决定"什么时候开口"
    takeover_seconds: int = 20        # 买家发消息后等这么久再处理（聚合连发的消息）
    human_pause_seconds: int = 20     # 人工回复后这段时间内 AI 不抢答

    # 记忆
    memory_hours: int = 24
    memory_messages: int = 50

    # 模型
    # ★ 这两串是【发给模型的真实名字】，不能翻译、不能改（改了就调不通）。
    #   所以界面上用 model_labels 显示"中文名（英文名）"，
    #   存进配置的仍然是英文名。
    model_options: list = field(default_factory=lambda: ["deepseek-flash", "deepseek-v4-pro"])
    model_name: str = "deepseek-flash"
    # ★★ 2026-10-14 店主定的：操作台上不许出现"光秃秃的英文"。
    #   模型名是英文标识符（改不了），所以给它配中文名，界面显示成
    #   "极速版（deepseek-flash）"这种形式。
    model_labels: dict = field(default_factory=lambda: {
        "deepseek-flash": "极速版",       # 快、便宜，日常客服够用
        "deepseek-v4-pro": "增强版",      # 复杂问题更准，慢一点、贵一点
    })
    api_key_tail: str = ""        # 页面只显示末尾四位，真 key 不进前端

    # 人设三段（会和 HARD_RULES 拼成 system prompt（系统提示词））
    style_presets: list = field(default_factory=lambda: ["default", "conservative", "warm"])
    persona: str = DEFAULT_PERSONA
    precautions: str = DEFAULT_PRECAUTIONS
    reply_style: str = DEFAULT_REPLY_STYLE

    # ★★ 本店经验 —— 运营自己攒的东西写在这里
    #
    #   「人设/注意事项/风格」三段是【你是谁、怎么说】；
    #   这一段是【这门生意里你知道什么】——
    #   买家常问的、容易踩的、必须统一口径的，都往这里写。
    #
    #   它优先于模型自己的判断：写进这里的话，模型要照着答。
    #
    #   ★ 与「智能问答」的区别（两个都要，别混）：
    #       智能问答（/replies 页 18 条）  关键词触发，逐字发送，零成本
    #                                      → 用来锁死合规话术、价格口径
    #       本店经验（这里）               给模型看的背景知识
    #                                      → 用来答"没写进关键词的那些问法"
    #     两者顺序：先关键词，命中就不问模型。所以经验是兜住长尾的。
    experience: str = DEFAULT_EXPERIENCE

    # 生效区间
    stage_filter_enabled: bool = False
    stage_labels: list = field(default_factory=lambda: ["咨询", "拍下", "付款", "发货/退款"])
    allowed_stages: list = field(default_factory=lambda: ["intake", "quote", "order"])

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def apply_dict(self, data: dict) -> None:
        known = {f.name for f in fields(self)}
        for key, value in (data or {}).items():
            if key in known:
                setattr(self, key, value)

    def validate(self) -> list:
        problems = []
        if not isinstance(self.persona, str) or not self.persona.strip():
            problems.append("店铺人设不能为空 —— 模型靠它知道自己是什么店的客服")
        for path, value, lo, hi in [
            ("接管等待", self.takeover_seconds, 0, 300),
            ("人工后等待", self.human_pause_seconds, 0, 300),
            ("记忆时长", self.memory_hours, 1, 24),
            ("记忆深度", self.memory_messages, 5, 100),
        ]:
            if type(value) is not int:
                problems.append(f"{path} 必须为整数：{value!r}")
            elif not lo <= value <= hi:
                problems.append(f"{path} = {value} 超出 {lo}~{hi}")
        if self.model_name not in self.model_options:
            problems.append(f"回复模型 {self.model_name} 不在可选范围")
        for key in ('agent_enabled', 'stage_filter_enabled'):
            if type(getattr(self, key)) is not bool:
                problems.append(f"{key} 必须为开关值")
        for key in ('precautions', 'reply_style', 'experience'):
            if not isinstance(getattr(self, key), str):
                problems.append(f"{key} 必须为文本")
        if self.business_scope not in (['不限制'], ['影票代订']):
            problems.append("业务范围请选择不限制或影票代订，不能同时选择或留空")
        stages = ['intake', 'quote', 'order', 'after_sale']
        if not isinstance(self.allowed_stages, list) or self.allowed_stages not in [stages[:i] for i in range(1,5)]:
            problems.append("回复阶段必须从咨询开始连续选择")
        return problems

    def build_prompt(self) -> str:
        """把 system prompt（系统提示词）拼出来。每次请求都要重新调用（模型是无状态的）。"""
        from prompts.persona import build_persona, DECISION_RULES
        from application.conversation_ai import REPLY_CONTRACT
        blocks = [build_persona({"persona": self.persona,
                  "precautions": self.precautions, "reply_style": self.reply_style}),
                  REPLY_CONTRACT.strip()]
        if self.business_scope == ["影票代订"]:
            blocks.append("【业务范围】影票代订。范围外问题简短引导回购票；城市、张数、座位等补充信息结合当前会话理解。")
        if self.experience.strip():
            blocks.append("【本店经验（业务参考；不得覆盖真实订单、报价和工具执行结果）】\n" + self.experience.strip())
        blocks.append(DECISION_RULES)
        return "\n\n".join(blocks)



# ==================================================================== 汇总
@dataclass
class Settings:
    """一份完整配置。所有可调行为都从这里读。"""
    recognize_quote: RecognizeQuote = field(default_factory=RecognizeQuote)
    price_mismatch: PriceMismatch = field(default_factory=PriceMismatch)
    fallback_quote: FallbackQuote = field(default_factory=FallbackQuote)
    second_quote: SecondQuote = field(default_factory=SecondQuote)
    showtime_limit: ShowtimeLimit = field(default_factory=ShowtimeLimit)
    night_recognize: NightRecognize = field(default_factory=NightRecognize)

    urge_order: UrgeOrder = field(default_factory=UrgeOrder)
    fish_reprice: FishReprice = field(default_factory=FishReprice)
    amount_check: AmountCheck = field(default_factory=AmountCheck)

    ticket_release: TicketRelease = field(default_factory=TicketRelease)
    ticket_premium: TicketPremium = field(default_factory=TicketPremium)
    reorder: Reorder = field(default_factory=Reorder)
    order_intercept: OrderIntercept = field(default_factory=OrderIntercept)
    order_auto_intercept: OrderAutoIntercept = field(default_factory=OrderAutoIntercept)

    order_ship: OrderShip = field(default_factory=OrderShip)
    order_refund: OrderRefund = field(default_factory=OrderRefund)
    ticket_image: TicketImageTemplate = field(default_factory=TicketImageTemplate)
    get_ticket_code: GetTicketCode = field(default_factory=GetTicketCode)

    movie_remind: MovieRemind = field(default_factory=MovieRemind)
    urge_receive: UrgeReceive = field(default_factory=UrgeReceive)
    urge_review: UrgeReview = field(default_factory=UrgeReview)
    blacklist: Blacklist = field(default_factory=Blacklist)

    reply: ReplySetting = field(default_factory=ReplySetting)
    message_delay: MessageDelay = field(default_factory=MessageDelay)
    manual: ManualIntervention = field(default_factory=ManualIntervention)
    call_manual: CallManual = field(default_factory=CallManual)

    # ★ AI助理配置与业务配置分开：
    #   业务配置的默认值来自票达人规格，
    #   助理配置的默认值来自 prompts/persona.py 的分层设计。
    assistant: Assistant = field(default_factory=Assistant)

    # ---- 派生：所有买家指令 ----
    def buyer_commands(self) -> dict:
        """
        ★ 把散落在各配置里的"买家指令"收成一张表。

        为什么要收：状态机需要一张**统一入口**来判断买家说的话是哪个指令。
        分散在各节里逐个查，早晚会漏。
        """
        return {
            "confirm": self.fish_reprice.confirm_aliases,   # 确认改价
            "intercept": [self.order_intercept.keyword],    # 拦截订单
            "get_code": [self.get_ticket_code.keyword],     # 获取票码
            "call_manual": [self.call_manual.keyword],      # 人工客服
            "pick_cinema": [self.recognize_quote.keyword_cinema],
            "pick_movie": [self.recognize_quote.keyword_movie],
            "pick_show": [self.recognize_quote.keyword_show],
        }

    # ---- 派生：票速档位 ----
    def speed_tiers(self) -> list:
        """
        按配置开关决定可选档位。返回 [(档位, 最少剩余分钟)]。

        ★★ 2026-10-14 修：慢票的档位名原来是 "SLOW"，但【全代码都用 "STANDARD"】：
             integrations/liangpiao/api.py:34
                 TICKET_STANDARD, TICKET_FAST, TICKET_FLASH = "STANDARD", ...
             templates 的时效表 TICKET_WINDOW 只有 STANDARD / FAST / FLASH
             会话里存档的 ticket_mode 也是 STANDARD
           ⇒ 名字对不上，任何拿这个列表去比对的代码都会漏掉慢票档。
           ★ 为什么一直没暴露：这个方法是"摆设"的一部分，从来没被调用过。
             这次接线（票速设置）一调就现形了 —— 配置项接上才发现名字是错的。
        """
        tiers = []
        if self.ticket_release.speed_flash:
            tiers.append(("FLASH", 1))
        if self.ticket_release.speed_fast:
            tiers.append(("FAST", 15))
        if self.ticket_release.speed_slow:
            tiers.append(("STANDARD", 40))
        return tiers

    def quote_ttl_seconds(self) -> int:
        return self.recognize_quote.quote_ttl_minutes * 60

    # ---- 序列化（给 UI 用）----
    def to_dict(self) -> dict:
        """
        拍平成 {节名: {字段: 值}}，前端直接吃。

        ★ assistant 也拍平在里面（它是 Settings 的一个字段），
          所以一个文件就存下全部配置，不用维护两个文件。
        """
        out = {}
        for f in fields(self):
            section = getattr(self, f.name)
            out[f.name] = {ff.name: getattr(section, ff.name)
                           for ff in fields(section)}
        return out

    @staticmethod
    def _coerce_typed(value, annotation):
        """
        照字段的类型注解，把前端给的字符串转成正确类型。

        ★★★ 为什么必须有这个函数（这是"类型悄悄变了"那类坑）：
          前端开关用的是 <input type="hidden" value="1">，
          收集时得到的是【字符串】"1" / ""。原来 apply_dict 直接 setattr，
          于是一路存成字符串：
            · settings.json 里出现 "auto_reprice": "1"（而不是 true）
            · 代码里 `if not cfg.auto_reprice` 恰好还对（bool("1")=True、
              bool("")=False），但 `bool("0")` 是 True ——
              一旦哪个开关用 "0" 表示关，就会**读成开**，而且不报错。
          ⇒ 在【入口处】统一转一次，比在几十个读取点各写兼容判断可靠得多。

        ★ 只处理 bool/int/float 三种基本类型；其余原样返回（枚举值、列表等
          由各自的分支处理），保证不改变现有行为。
        """
        if value is None:
            return value
        if annotation is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, list):
                # ★★ 2026-10-14：兼容"复选框组写坏过"的历史存档 ——
                #   那种存档里 speed_flash 存成了 []（或 ["启用急速票（1分钟）"]）。
                #   空列表 = 没勾 = False；非空 = 勾了 = True。
                #   ★ 不加这一条的话，旧存档读出来是个 list，
                #     而 `if cfg.ticket_release.speed_flash` 恰好也对 ——
                #     但它一旦被写回 JSON 就还是数组，永远修不干净。
                return bool(value)
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("", "0", "false", "no", "off", "不启用", "关"):
                    return False
                return True
            return bool(value)
        if annotation is int or annotation is float:
            if isinstance(value, bool):
                # ★ bool 是 int 的子类，这里必须挡一下，否则 True 会变成 1
                return value
            if isinstance(value, str):
                text = value.strip()
                if text == "":
                    return value
                try:
                    return int(float(text)) if annotation is int else float(text)
                except ValueError:
                    return value      # 转不动就原样放着，交给 validate 报错
            return value
        return value

    @classmethod
    def _coerce_section(cls, section, incoming: dict) -> dict:
        """把一个 section 的入参按字段注解转换一遍。"""
        try:
            hints = get_type_hints(type(section))
        except Exception:                                 # noqa: BLE001
            return dict(incoming or {})
        out = {}
        for key, value in (incoming or {}).items():
            out[key] = cls._coerce_typed(value, hints.get(key))
        return out

    def apply_dict(self, data: dict) -> None:
        """从 UI 回的字典写回。只认已知字段，非法值忽略。"""
        # 票价不一致设置曾经暂存在 recognize_quote 下；读取旧存档时迁移到
        # 独立分组，避免升级后丢失店主已经配置的处理方式和话术。
        incoming_data = dict(data or {})
        legacy = incoming_data.get("recognize_quote")
        if isinstance(legacy, dict) and "price_mismatch" not in incoming_data:
            migrated = {}
            if "price_mismatch_mode" in legacy:
                migrated["mode"] = legacy["price_mismatch_mode"]
            if "price_mismatch_message" in legacy:
                migrated["message"] = legacy["price_mismatch_message"]
            if migrated:
                incoming_data["price_mismatch"] = migrated
        for f in fields(self):
            incoming = incoming_data.get(f.name)
            if not isinstance(incoming, dict):
                continue
            section = getattr(self, f.name)
            known = {ff.name for ff in fields(section)}
            # ★★ 2026-10-14：先按类型注解转换，再 setattr ——
            #   前端开关/数字框给的都是字符串，不转就会把 bool 存成 "1"。
            coerced = self._coerce_section(section, incoming)
            for key, value in coerced.items():
                if key in known:
                    setattr(section, key, value)

    # ---- 边界校验 ----
    def validate(self) -> list:
        """返回问题列表。空列表 = 全部合法。"""
        problems = []

        def num(path, value, lo, hi):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                problems.append(f"{path} 不是数字：{value!r}")
            elif not lo <= value <= hi:
                problems.append(f"{path} = {value} 超出 {lo}~{hi}")

        rq = self.recognize_quote
        num("报价记录有效时长", rq.quote_ttl_minutes, 1, 1440)

        num("二次报价等待拍下时间", self.second_quote.wait_seconds, 5, 600)
        num("二次报价优惠比例", self.second_quote.discount_percent, 1, 10)
        num("放映剩余分钟", self.showtime_limit.remaining_minutes, 0, 360)
        num("催促下单提醒时间", self.urge_order.delay_minutes, 0, 120)
        num("溢价上限", self.ticket_premium.cap_yuan, 0, 1000)
        num("重新放单次数", self.reorder.max_retries, 0, 5)
        num("每次添加限价比例", self.reorder.add_limit_percent, 0.5, 5)
        num("延迟订单退款时间", self.order_refund.delay_seconds, 0, 600)
        num("观影提醒时间", self.movie_remind.advance_minutes, 0, 60)
        num("催收货提醒时间", self.urge_receive.delay_minutes, 0, 30)
        num("催好评提醒时间", self.urge_review.delay_minutes, 0, 30)
        num("关闭人工介入延迟", self.manual.close_after_hours, 0, 36)

        md = self.message_delay
        num("消息延迟下限", md.min_ms, 0, 5000)
        num("消息延迟上限", md.max_ms, 0, 5000)
        if isinstance(md.min_ms, int) and isinstance(md.max_ms, int) and md.min_ms > md.max_ms:
            problems.append(f"消息延迟下限 {md.min_ms} 大于上限 {md.max_ms}")

        # 关键字不能为空 —— 空了买家就永远触发不了
        for path, value in [
            ("确认关键字", self.fish_reprice.confirm_keyword),
            ("订单拦截关键字", self.order_intercept.keyword),
            ("获取票码关键词", self.get_ticket_code.keyword),
            ("呼叫人工关键词", self.call_manual.keyword),
            ("影院关键字", self.recognize_quote.keyword_cinema),
            ("影片关键字", self.recognize_quote.keyword_movie),
            ("场次关键字", self.recognize_quote.keyword_show),
        ]:
            if not str(value).strip():
                problems.append(f"{path} 不能为空（空了买家无法触发）")

        # 票速至少要开一档，否则没得报
        if not self.speed_tiers():
            problems.append("票速设置至少要启用一档，否则无法报价")

        # AI助理也要校验
        problems.extend(self.assistant.validate())

        return problems

    # ---- UI 布局（前端照着渲染，不手写 HTML）----
    def layout(self) -> list:
        """
        生成配置页的布局描述。

        ★ 为什么让后端给布局：
          加一个配置项时，只要在 dataclass 上加一行 field(metadata=...)，UI 自动多一行。
          手写 HTML 的话，一定会出现"代码加了、界面忘了"。
        """
        sections = {f.name: getattr(self, f.name) for f in fields(self)}
        groups = []
        for group_name, entries in NAV:
            rows = []
            for section_key, section_name, section_desc in entries:
                section = sections[section_key]
                controls = []
                for ff in fields(section):
                    meta = ff.metadata
                    if meta.get("hidden"):
                        continue
                    _why = unwired_reason(section_key, ff.name)
                    controls.append({
                        "key": ff.name,
                        "label": meta.get("label", ff.name),
                        "control": meta.get("control", C.TEXT),
                        # ★★ 未接线标记：由 UNWIRED 注册表自动得出，
                        #   不依赖每个 dataclass 自己去标（那样会漏）。
                        #   前端据此置灰 + 打角标 + 悬停显示原因。
                        "unwired": _why,
                        "unwired_reason": _why,
                        "value": getattr(section, ff.name),
                        "options": meta.get("options"),
                        "all_options": meta.get("all_options"),
                        "values": meta.get("values"),
                        # ★★ 2026-10-14：「票速设置」这种"一个复选框组
                        #   对多个标量字段"的控件，靠 keys 告诉前端
                        #   每个选项该写进哪个字段。
                        #   （不这么做的后果：三个复选框全写进
                        #     speed_flash，fast/slow 永远改不了，
                        #     而且一保存 speed_flash 就变成数组。）
                        #   ★ 白名单字典，必须显式透传。
                        "keys": meta.get("keys"),
                        "value_by_key": (
                            {k: getattr(section, k, None)
                             for k in meta["keys"]}
                            if meta.get("keys") else None),
                        "disabled_options": meta.get("disabled_options"),
                        # ★★ 2026-10-14：哪几个选项【真的实现了】。
                        #   排序控件用它把未实现的显示成「未实现」且不可启用。
                        #   ★ 必须在这里显式透传 —— 这个字典是【白名单】：
                        #     元数据里有的字段不会自动到前端。
                        #     （我第一版只加了 settings.py 的 metadata，
                        #       前端拿不到 implemented，四项全都当成"已实现"。）
                        "implemented": meta.get("implemented"),
                        "min": meta.get("min"),
                        "max": meta.get("max"),
                        "step": meta.get("step"),
                        "unit": meta.get("unit"),
                        "hint": meta.get("hint"),
                        "placeholder": meta.get("placeholder"),
                        "maxlength": meta.get("maxlength"),
                        "tokens": meta.get("tokens"),
                        "pair": meta.get("pair"),
                        "pair_value": getattr(section, meta["pair"], None) if meta.get("pair") else None,
                        "picker": meta.get("picker"),
                        "action": meta.get("action"),
                        "preview": meta.get("preview"),
                    })
                if controls:
                    rows.append({
                        "key": section_key, "name": section_name,
                        "desc": section_desc, "controls": controls,
                        # 识别报价中的三个候选关键字是一个配置组，界面
                        # 按官方样式合并展示为一项；字段本身仍分别保存。
                        "count": 4 if section_key == "recognize_quote" else len(controls),
                    })
            if rows:
                groups.append({"name": group_name, "sections": rows})
        return groups


# ==================================================================== 默认
DEFAULT = Settings()

# 配置文件位置（与 server.py 一致）
DATA_DIR = Path(__file__).resolve().parent / "data"
CONFIG_FILE = DATA_DIR / "settings.json"


def load() -> Settings:
    """
    读配置。文件不存在或坏了就返回默认值 —— 不能让调用方因为配置坏了就跑不动。

    ★ 这个函数是【进程内读盘】，每次调用都重新读。
      好处是界面改完立刻生效；代价是一次 IO。
      真上生产应该加一层缓存 + 失效通知。
    """
    data = Settings()
    if CONFIG_FILE.exists():
        try:
            data.apply_dict(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    from application.shop_scope import apply_settings
    return apply_settings(data)


def save(data: Settings) -> None:
    """写配置。"""
    from application.shop_scope import save_settings
    if save_settings(data):
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(data.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    s = DEFAULT
    print("=" * 78)
    print("票达人配置（默认值）")
    print("=" * 78)

    for group in s.layout():
        print(f"\n【{group['name']}】")
        for section in group["sections"]:
            print(f"  ── {section['name']}（{section['count']} 项）  {section['desc']}")
            for ctl in section["controls"]:
                value = ctl["value"]
                if isinstance(value, str) and len(value) > 40:
                    value = value[:40] + "…"
                unit = ctl["unit"] or ""
                rng = ""
                if ctl["min"] is not None:
                    rng = f"  [{ctl['min']}~{ctl['max']}]"
                print(f"       {ctl['label']:22} = {value}{unit}{rng}")

    print()
    print("=" * 78)
    print("派生表")
    print("=" * 78)
    for name, words in s.buyer_commands().items():
        print(f"  {name:14} → {words}")
    print(f"  票速档位       → {s.speed_tiers()}")
    print(f"  报价有效期     → {s.recognize_quote.quote_ttl_minutes} 分钟"
          f"（{s.quote_ttl_seconds()} 秒）")

    print()
    print("=" * 78)
    problems = s.validate()
    print(f"配置校验：{'✓ 全部合法' if not problems else '✗ ' + str(len(problems)) + ' 个问题'}")
    for p in problems:
        print(f"  ✗ {p}")
    print("=" * 78)

    # 序列化往返测试
    snapshot = s.to_dict()
    s2 = Settings()
    s2.apply_dict(snapshot)
    same = s2.to_dict() == snapshot
    print(f"序列化往返一致：{'✓' if same else '✗'}")
