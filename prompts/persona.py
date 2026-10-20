"""
prompts/persona.py —— system prompt 的【不可改层】

■ 分工（重要，别混）：

  prompts/persona.py（本文件，代码里，运营动不到）
      └─ HARD_RULES  绝对不能做什么 ← 风控资产，改了要重新审计

  settings.py（存数据库，后台 UI 可改）
      └─ persona      你是谁、业务怎么运转      ← 运营可改
      └─ precautions   注意事项                ← 运营可改
      └─ reply_style   说话风格                ← 运营可改

  为什么要分？因为"能说什么"要灵活（运营天天调），
  "绝对不能做什么"要刚性（改一次就要重新审计）。
  混在一个文本框里，运营调语气时会不小心动到风控条款。

■ 模型无状态：每次请求都要重新告诉它。
  组装函数 build_persona() 就是干这个的，每次调用都重新拼一遍。
"""

# ==================================================================== 不可改层
# ★ 这几条是底线。它们不放数据库，因为它们是"出事才有代价"的那一类。
HARD_RULES = """【绝对不能做】以下每一条违反都会造成真实损失
1. 不许承诺或暗示"可以退票/可以改签"——聊天记录是平台判责依据
2. 不许承诺保证连座、保证原座或就近安排；座位偏好不是已确认的出票结果
3. 不许自己编造价格、取票码、场次时间——只能用系统给的值
4. 不许说"一定有票/包出票"——出票成败由上游决定
5. 不许向买家索要密码、验证码、身份证号、银行卡号
6. 不许引导买家离开当前平台交易（微信、私下转账、外部收款码）
7. 买家询问身份时如实说明当前为店铺自动回复，可按需转人工；不得冒充真人
8. 不确定时先区分缺少买家信息、需要查询事实、确需人工处理；能追问或查询就继续，不猜、不编，也不默认转人工"""

DECISION_RULES = """【统一处理原则：动作与事实约束优先于人设、风格和经验中的泛化建议】
1. 本轮原话决定本轮任务，历史只用于理解。问候、感谢或已完成事项的收尾简短回应；确认购票选择则继续相应流程。不要仅因历史存在问题再次转人工、查询或催付。
2. 信息缺失时只问缺项；有工具可查时先查，再依据结果回复。买家明确要求人工、工具明确要求人工处理、查询失败且无可用恢复路径、或确需店家处理的争议才转人工。普通询价、补信息、会员座咨询和买家催问本身都不是转人工理由。
3. 渠道业务规则必须来自当前渠道的已核实规则或工具。万达不沿用良票出票时限、就近调座或自动出票承诺；渠道不明时先核实，不能从店铺人设推断所有订单的渠道。
4. 买家说要几张、几排、哪个日期，是需求信息；工具成功保存或核价后才说已记录、已更新或已报价。只读取旧报价不等于修改本次需求。询价不等于确认替换旧订单。
5. 含糊数字、指代、否定或备选，结合可靠上下文仍不明确就针对性追问。不得补造排座号、擅选上午晚上或把不确定位置说成已确认。未提供图像或识别事实时，只能确认收到图片，不能声称已看清圈选位置。
6. 改价、是否拍下或付款相关问题，先查询平台订单，再决定下一步。没有查询结论就不能断言未下单、引导重新拍下或付款。查询失败不等于没有订单。已提交申请不等于成功，业务完成必须有工具或平台确认。
7. 风格配置只约束语气与长短；其中“不确定就转人工”等笼统建议不得代替上述查询、追问与核验流程。不能声称已经转人工，除非确实发起交接。
"""


# ==================================================================== 预设风格
# ★★ 这三个预设的来历（值得记一笔）
#
#   `settings.assistant.style_presets` 这个字段一直是
#   ["default", "conservative", "warm"]，接口也返回它，
#   但【三个预设的文本从来不存在于生产代码里】——
#   它们只硬编码在 demos/style_demo.py 里，那个文件是教学演示，
#   生产代码拿不到。
#
#   结果是：build_persona 曾经收一个 style 参数"用于选择预设"，
#   但函数体一个字都没用它 —— 因为没东西可选。
#   参数收下了、没实现，比没有这个参数更糟：读代码的人会去找那三个预设。
#
#   ⇒ 现在把文本搬进来，让预设真的能用。
#     预设的作用是【一键填入 reply_style 输入框】，运营填完还能手改 ——
#     它不是"另一套运行时机制"，就是几个默认值。
STYLE_PRESETS = {
    "default": """- 像闲鱼上做生意的店主，口语、简短、不客套
- 一条消息不超过 3 行，手机上一屏能看完
- 可以用"嗷""哈""辛苦""宝宝"这类闲鱼口吻
- 不要用"您好，请问有什么可以帮您"这种客服腔
- 该急的时候要显出来（比如催付款、提醒别连下两单）""",
    "conservative": """- 专业、克制、信息优先
- 完整句子，不用语气词
- 不需要显得热情，把事说清楚最重要""",
    "warm": """- 亲切、有耐心，多用语气词和表情
- 主动解释为什么，让买家安心
- 出错时先道歉再给方案""",
}


def style_preset(name: str) -> str:
    """取一个预设风格的正文（不含「【说话风格】」标题）。取不到就返回默认。"""
    return STYLE_PRESETS.get(name, STYLE_PRESETS["default"])


# ==================================================================== 组装
def build_persona(custom: dict | None = None, *, style: str | None = None) -> str:
    """
    把 system prompt 拼出来。

    参数：
      custom —— 运营在后台填的三字段：
                {"persona": ..., "precautions": ..., "reply_style": ...}
                ★ 传了 custom 就用它（后台填的优先）。
      style  —— 预设风格名（default / conservative / warm）。
                ★ 关键字参数，只在【custom 没给 reply_style】时生效 ——
                  运营手填的内容永远优先于预设。

    ★ 每次请求都调用它。模型不记得上一次，你不告诉它，它就不知道。
    """
    if custom:
        persona = custom.get("persona", "").strip()
        precautions = custom.get("precautions", "").strip()
        reply_style = custom.get("reply_style", "").strip()
    else:
        from settings import DEFAULT_PERSONA, DEFAULT_PRECAUTIONS, DEFAULT_REPLY_STYLE
        persona, precautions, reply_style = DEFAULT_PERSONA, DEFAULT_PRECAUTIONS, DEFAULT_REPLY_STYLE

    # ★ 预设的优先级：运营手填 > 预设 > 默认
    if not reply_style and style:
        reply_style = style_preset(style)

    # Migrate the historical default at rendering time without editing shop data.
    reply_style = reply_style.replace(
        '不确定的事就说"我帮你转人工看下"，不要编。',
        '不确定时先查可查事实，缺信息只问缺项；确需店家处理时再说明转人工，不要编。')

    blocks = []
    if persona:
        blocks.append(persona)
    blocks.append(HARD_RULES)
    if precautions:
        # 注意：precautions 是【在 HARD_RULES 之上的店铺补充】，不是替代
        blocks.append("【店铺补充注意事项】\n" + precautions)
    if reply_style:
        blocks.append("【说话风格】\n" + reply_style)

    return "\n\n".join(blocks)


if __name__ == "__main__":
    from settings import load
    s = load()
    prompt = build_persona({
        "persona": s.persona, "precautions": s.precautions, "reply_style": s.reply_style,
    })
    print(prompt)
    print(f"\n（共 {len(prompt)} 字）")

