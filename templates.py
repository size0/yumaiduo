"""
templates.py —— 回复文案表（票达人同款 · 按实渲染规格重做）

■ 依据：../票达人-文案清单.md（31 个气泡码，已由 _check_bubbles.py 校验）
■ 每条模板都标注 bubble 码，可直接和票达人对照。

════════════════════════════════════════════════════════════════
★ 三条设计要点

【1】locked = 风控话术，**不许模型改写**
    17（不要付款）、18（付款后不支持退改签）、15（购票须知）、1（严禁连下两单）
    这几条是平台判责依据，必须逐字发送。

【2】一个模板 key 可以对应多个 bubble 码
    比如 RECOG_FAIL 对应 2/29/30/42/43/44/32 —— 靠 {失败原因} 区分。

【3】渠道分层
    全局层 + 万达/良票覆盖层。渠道留空 = 沿用全局。
════════════════════════════════════════════════════════════════
"""

# ==================================================================== 阶段
STAGES = [
    ("intake", "咨询与识别"),
    ("quote", "报价"),
    ("order", "拍单与改价"),
    ("payment", "付款核验"),
    ("ticket", "取票交付"),
    ("aftercare", "售后与订单状态"),
]

# 引用展开来自 qa.py（关键词回复表的 [全局调用标识ID]）
import qa          # noqa: E402
# 配置来源：确认指令等由 settings 统一决定，文案不硬编码
from settings import DEFAULT as CONFIG   # noqa: E402

# ==================================================================== 出票时效口径
# ★ 这张表直接抄自良票文档（TicketMode 说明），不是我编的：
#     STANDARD 特惠  1~60 分钟
#     FAST     快速  15 分钟内
#     FLASH    极速  1~15 分钟（院线直出）
#
# ★ 为什么不能写死成「1-30分钟（最迟50分钟）」：
#   票达人页面上写的是 1-30/50，但良票文档里 STANDARD 是 1~60。
#   我们买的如果是 STANDARD，却告诉买家 30 分钟，就是【对买家说了不准的话】——
#   超时的那一单会变成投诉。所以时效必须跟着实际购入的档位走。
#
# ★ 必须定义在 VARIABLES 之前 —— VARIABLES 里要用它。
TICKET_WINDOW = {
    "STANDARD": "1-60分钟",
    "FAST": "15分钟内",
    "FLASH": "1-15分钟",
}

# ==================================================================== 变量
VARIABLES = {
    "city":          ("城市", "识别出的城市", "周口"),
    "cinema":        ("影院名", "识别出的影院", "周口奥斯卡影城"),
    "cinema_addr":   ("影院地址", "影院详细地址", "陈州街回族街道七一路中段天下诚商场6楼（近中国邮政）"),
    "hall":          ("影厅名", "影厅", "6号高清激光厅"),
    "movie":         ("影片名", "影片", "蜘蛛侠：崭新之日"),
    "show_time":     ("放映时间", "场次时间", "2026-09-21 21:25:00"),
    "seats":         ("座位信息", "座位列表", "4排1座,4排2座"),
    "origin_unit":   ("单座位原价", "官方原价/张", "34.90"),
    "origin_total":  ("整单原价", "官方原价合计", "69.80"),
    "unit_price":    ("单座位报价", "我们的报价/张", "23.49"),
    "total_price":   ("整单报价", "我们的报价合计", "46.98"),
    "count":         ("票数", "当前订单的购票张数", "2"),
    "marked_count":  ("图片标记数", "图片中明确圈选或勾选的位置数", "2"),
    # ★ 2026-10-14：二次报价的优惠比例（读自配置）。
    #   ★ 值是百分比数字（如 "3"），不带 % —— 模板里自己写 % 号，
    #     这样中文/英文标点都好改。
    "discount_percent": ("优惠比例", "二次报价的优惠百分比（读自配置）",
                         str(CONFIG.second_quote.discount_percent)),
    "ticket_code":   ("取票信息", "★ 值自带「取票码」三字", "取票码 34309589"),
    "ticket_link":   ("取票链接", "取票链接", "https://ticket.example.com/demo"),
    "confirm_cmd":   ("确认指令", "买家要回复的字（读自配置）", CONFIG.fish_reprice.confirm_keyword),
    "quote_ttl_minutes": ("报价有效时长", "分钟（读自配置）", str(CONFIG.recognize_quote.quote_ttl_minutes)),
    "fail_reason":   ("失败原因", "识别失败原因", "影院场次暂不可售"),
    "candidates":    ("二次确认信息", "候选列表", "【影院1】周口奥斯卡影城（周口）<br>---<br>【影院2】示例星光影城（示例市）"),
    "pick_kind":     ("候选标识", "消歧对象类型", "影院"),
    "discount":      ("优惠总金额", "总优惠额", "22.82"),
    "limit_minutes": ("限制间隔", "距开场不足多少分钟不接单（读自配置）",
                      str(CONFIG.showtime_limit.remaining_minutes)),
    "ticket_window": ("出票时效", "★ 必须跟着实际购入的档位走，不能写死",
                      TICKET_WINDOW["STANDARD"]),
    "pickup_link":   ("取票链接", "★ 良票下单即返回的免登录取票页（不含价格）",
                      "https://t-v3.example/abc"),
    "fee_amount":    ("手续费", "退票手续费", "0.00"),
    "refund_amount": ("退还金额", "退票实际退还", "70.20"),
    "fail_reason":   ("失败原因", "识别失败或退票未成的原因", "影院场次暂不可售"),
}

# ==================================================================== 模板

# ==================================================================== 模板
def _t(key, bubble, name, stage, trigger, variables, notes, text, locked=False,
       multi=False, channel_text=None, variants=None):
    """
    造一条模板。

    ★ channel_text 一开始就是【空 dict】，不是缺字段。
      理由：前端读 `item.channel_text?.[channel]`，
      字段缺失和空对象在 JS 里都能安全处理，但
      【保存后再读回来】时，空 dict 会落进 JSON 里当 {}，
      少一个字段却要每次 get —— 现在直接给全，省一层判断。

    ★ channel_text 现在可以传入了（2026-10-13 加）。
      为什么要：万达和良票的**取票链接**只有良票有 ——
      良票下单就返回免登录取票页，万达是店家手动出票、没有链接。
      TICKETING 的全局文案里那行 `取票链接：{pickup_link}` 对万达会是空的，
      会把买家引到一个不存在的链接上。

    ★ variants 现在也可以传入了（2026-10-13 加）。
      为什么要：同一个模板 key 在不同场景下措辞不同。
      最典型是 RECOG_FAIL —— 同业线上预览里 29/2/30/42/43/44/32
      【各有各的话】，而我们原先共用一条 + {fail_reason} 变量。
      ⇒ 调用方传 values={"_variant": "show_started"} 就选到对应那条；
        不传就用默认 text（向后兼容，现有调用一行都不用改）。
    """
    return {"key": key, "bubble": bubble, "name": name, "stage": stage,
            "trigger": trigger, "variables": variables, "notes": notes,
            "text": text, "locked": locked, "multi": multi,
            "channel_text": dict(channel_text or {}),
            "variants": dict(variants or {})}


TEMPLATES = [
    _t("WANDA_UNIT_PRICE", "—", "万达单价报价 · 待确认张数", "quote",
       "仅万达：场次和单价已确认，但买家尚未确认张数时发送；张数已确认则发送整单报价卡", ["city", "cinema", "movie", "show_time", "hall", "unit_price", "marked_count"],
       ["状态：单价已报价，等待买家补充张数。无需先选座。", "买家补充明确张数后重新核价，转为整单报价；此单价不能用于改价或下单。", "可使用影片、场次、影厅、城市、影院、单价参数；留空则不发送。"],
       "{city}｜{cinema}\n影片：{movie}\n场次：{show_time}｜{hall}\n优惠价：{unit_price}元/张\n需要几张票呢？如需指定位置，可以在图上标记后发我。",
       variants={
           "marked": "{city}｜{cinema}\n影片：{movie}\n场次：{show_time}｜{hall}\n优惠价：{unit_price}元/张\n需要几张票呢？",
           "reselect": "{city}｜{cinema}\n影片：{movie}\n场次：{show_time}｜{hall}\n同座位区域参考价：{unit_price}元/张\n原标记位置暂不可售，请重新选择同类型可售座位并确认购票张数。"}),
    # ---------------------------------------------------------------- 咨询与识别
    # ★★ 2026-10-14 新增：二次报价（接线「设置 → 二次报价」）
    #   ★ 它做什么：报价后等了 wait_seconds 买家还没拍下，
    #     就再报一次（带一点优惠），给他一个下单的理由。
    #   ★ 为什么只是一句话、不再发一张报价卡：
    #     重发报价卡需要【原始的识别结果】（影院/影厅/座位…），
    #     而那是几分钟前的东西，进程重启后就没了。硬发会缺字段。
    #   ⇒ 所以这条只说【新价格】，会话上的报价也同步改成新价，
    #     之后下单/改价自然用新价（金额不会前后不一致）。
    #   ★ 优惠上限：最多把毛利让完，绝不低于成本
    #     （见 domain/quote_rules.py 的 second_quote）。
    _t("SECOND_QUOTE", "—", "二次报价", "quote",
       "首次报价后 wait_seconds 内买家没拍下",
       ["total_price", "discount_percent", "origin_total"],
       ["★ 优惠最多让完毛利，绝不倒贴（引擎里保证）。",
        "★ 口径请店主确认 —— 这是我按字段 label 写的，不是抄来的。"],
       "亲，看您还在考虑～这个价格我再给您让一点：\n"
       "现在 {total_price} 元（已再优惠 {discount_percent}%）\n"
       "位置我这边先给您留着，拍下就能安排出票~"),

    # ★★ 2026-10-14 新增：首次打招呼（接线「设置 → 回复 → 首次打招呼设置」）
    #   ★ 这条文案是【店主确认过的口径】，不要自己改写。
    #   ★ 键名用 GREETING：这个名字是这类"招呼语"的通用叫法，
    #     也方便以后和别的配置对齐。
    _t("GREETING", "—", "首次打招呼", "chat",
       "买家第一次进来（发文字或图片）且距上次打招呼已超过间隔时长",
       [], ["★ 只在【第一次】打招呼 —— 否则每句话都回一遍，像机器人。",
            "★ 开关是「首次打招呼设置」；间隔时长决定多久才再打一次。",
            "★ 这条文案是店主确认过的口径，不要自己改写。"],
       "你好，发我想看的场次或选座截图，我帮你查价。"),

    _t("RECOGNIZING_TIP", "1", "识别中提示", "intake",
       "买家发出选座截图后【立即】发送，再去做耗时识别",
       [], ["★ 风控话术：「严禁连下两单」不许删改。",
            "必须【先发】再做识别 —— 否则买家会静默等 4~5 秒。"],
       "请核对确认后再拍，严禁连下两单，须上单出票或退款后再下新单嗷~",
       locked=True),

    _t("WANDA_QUOTE_FAILED", "", "万达核价失败", "quote",
       "万达图片已识别，但会员成本或所选座位区域暂未核实，未生成报价",
       ["fail_reason"], ["核价失败不等于识图失败，不引导重复更换截图。"],
       "图片中的影院、场次已识别，但暂时无法报价：{fail_reason}。\n请先不要按旧报价付款，待店家核价；无需重复发送同一张截图。"),

    _t("RECOG_FAIL", "2 / 29 / 30 / 42 / 43 / 44 / 32", "识别失败（原因可变）", "intake",
       "识别不出场次/座位，或场次不可售/已开场/座位超限/被遮挡",
       ["fail_reason", "_variant"],
       ["★ 7 个气泡码共用这条模板，但【每个码的话不一样】，所以用 variants 分场景。",
        "  不传 _variant 就用默认（= 同业 bubble 2 的措辞）。",
        "识别失败【不改变会话状态】—— 买家重发就能继续。",
        "★ 措辞依据：同业线上预览里 29/2/30/42/43/44/32 各有各的话。"],
       "失败的原因：【{fail_reason}】\n建议换猫眼小程序的座位截图再发我下~",
       variants={
           # bubble 29
           "seat_missing": "未识别到您选择的座位信息，辛苦选择一下座位信息截图发我哦",
           # bubble 30（★ 同业已有一句「需要距离开场前一小时购买」）
           "show_stopped": "此场次已停止购票，建议您选择其他场次截图发送获取报价\n-\n"
                            "需要距离开场前一小时购买哈",
           # bubble 42
           "show_started": "亲，您选择的场次已开场，无法继续购票，"
                            "请更换其他场次后重新发送截图~",
           # bubble 43
           "seat_limit": "亲，{fail_reason}，请减少座位数量后重新发送截图~",
           # bubble 44
            # bubble 44
            "seat_blocked": "亲，识别到座位被遮挡，请重新选座后再发送截图~",
            # ★★★ bubble 44 的另一个触发源（2026-10-14 补）
            #   店主明确：**识别阶段**的「座位价格异常」也走这条文案。
            #   判据是识别结果里的 priceMismatch（良票文档 2210 行：
            #     「priceMismatch=true 通常意味着漏识别了座位」）。
            #   ★ 为什么不复用 seat_blocked：两者文案【逐字相同】，
            #     但触发原因不同 —— 分开命名，排查时一眼看出是
            #     "价格对不上"还是"座位被遮挡"。
            #   ★ 文案必须与 bubble 44 逐字一致（店主给的权威原文）。
            "seat_price_abnormal": "亲，识别到座位被遮挡，请重新选座后再发送截图~",
            "seat_price_abnormal_configured": "{price_mismatch_message}",
           # bubble 32
           "need_pick": "识别失败，{fail_reason}！",
           # ★★ 下面两个不是「识别失败」，是【下单阶段】被拒 ——
           #   但都复用这条模板（同一个"这单没成，要买家做点什么"的位置）。
           #   良票文档 §4 座位图：「预校验在下单接口内同步进行：
           #   座位已售报 200006，孤座/情侣座违规报 200007」。
           #
           # 200006：选中的座位在下单那一刻被别人买走了
           "seat_taken": "亲，您选的座位刚被别人买走了~\n"
                         "辛苦重新发一下选座截图，我按新的座位给您报价",
           # 300006：询价之后、下单之前票价变了
           "price_changed": "亲，票价刚有变动，之前那个价格下不了了~\n"
                            "辛苦重新发一下截图，我给您重新报价",
       }),

    _t("DISAMBIG", "40", "多结果消歧", "intake",
       "识别到多个影院/影片/场次，需买家选一个",
       ["candidates", "pick_kind"],
       ["★ 一个码管三种消歧 —— 但每次只问【一个】维度，所以不需要买家区分类型。",
        "★★ 2026-10-13 店主定的：候选只写数字（1. 2. 3.），买家【回数字】即可。",
        "  「影院1」这种带前缀的写法仍然认（见 _parse_pick），老买家不会报错。",
        "★ {pick_kind} 现在只用于兜底/日志，不再进文案 —— "
        "文案里不再出现「例如：影院1」这种要买家打字的前缀。",
        "买家回错序号时【不报错】，直接重发此条。",
        "★★ 2026-10-14 对齐线上固定话术（live-triage.json 的 system_template）：",
        "  线上影院消歧的原文是",
        "      当前需要确认影院：",
        "      1. 上海 · 万达影城（临港万达广场IMAX店）",
        "      …",
        "      请回复影院编号即可，不用重复发送已提供的场次信息。",
        "  ⇒ 改用 variants 分维度：影院用线上原话；影片/场次用中性标题",
        "    （不能写死「确认影院」—— 问影片和场次时那样是错的）。",
        "  ★ 结尾那句「不用重复发送已提供的场次信息」是店家的真实意图：",
        "    买家已经发过场次信息，再让他重发一遍既浪费他时间也浪费识别费。"],
       "请确认你要购买的是哪一个（{pick_kind}）：\n{candidates}\n"
       "直接回复上面的数字即可完成确认",
       variants={
           # ★ 影院维度：逐字用线上固定话术
           "cinema": ("当前需要确认影院：\n{candidates}\n"
                      "请回复影院编号即可，不用重复发送已提供的场次信息。"),
           "movie": ("当前需要确认影片：\n{candidates}\n"
                     "请回复影片编号即可~"),
           "show": ("当前需要确认场次：\n{candidates}\n"
                    "请回复场次编号即可~"),
       }),

    _t("DISAMBIG_TIMEOUT", "41", "消歧超时", "intake",
       "买家迟迟没回复候选确认", [],
       ["超时后必须明确告知已取消，否则买家回头回复序号会困惑。"],
       "亲，候选确认已超时，本次识别已取消，请重新发送购票截图，我们将再次为您识别~"),

    # ---------------------------------------------------------------- 报价
    _t("QUOTE_CARD", "21", "报价卡", "quote",
       "识别并算价成功后发送。这是买家看到的正式报价",
       ["city", "cinema", "movie", "hall", "show_time", "seats",
        "origin_unit", "origin_total", "unit_price", "total_price", "cinema_addr",
        "confirm_cmd", "count"],
       ["★ 出现 10 次，是最核心的模板。",
        "末尾  疑似防伪签名。",
        "确认指令走 {confirm_cmd} 变量 —— 改指令只改变量，不动文案。"],
       "恭喜：已抢到特价票限时2分钟秒杀~\n\n"
       "※【{city}的】| {cinema}\n"
       "电影：{movie}\n"
       "影厅：{hall}\n"
       "场次：{show_time}\n"
       "座位：{seats}\n\n"
       "原价：{origin_unit}元/张，合计：{origin_total}元\n"
       "优惠后：{unit_price}元/张，合计：{total_price}元\n\n"
       "影院地址：【{city}的】| {cinema_addr}\n"
       "=======================\n"
       "请核对城市、影院是否一致\n"
       "张数：{count}张，请核对以上场次、座位和金额。\n"
       "如一致，直接拍下即可（拍下后我帮您改价）"),

    _t("QUOTE_CARD_MULTI", "4", "报价卡（多截图版）", "quote",
       "买家发了多张截图，或有效期内再次选座",
       ["city", "cinema", "movie", "hall", "show_time", "seats",
        "origin_unit", "origin_total", "unit_price", "total_price", "cinema_addr",
        "confirm_cmd", "count"],
       ["与 QUOTE_CARD 相同，只在分隔线后【多一行】提醒。",
        "★ 这一句防止买家以为按第一张截图出票。"],
       "恭喜：已抢到特价票限时2分钟秒杀~\n\n"
       "※【{city}的】| {cinema}\n"
       "电影：{movie}\n"
       "影厅：{hall}\n"
       "场次：{show_time}\n"
       "座位：{seats}\n\n"
       "原价：{origin_unit}元/张，合计：{origin_total}元\n"
       "优惠后：{unit_price}元/张，合计：{total_price}元\n\n"
       "影院地址：【{city}的】| {cinema_addr}\n"
       "=======================\n"
       "【多张截图以最后一张截图为准出票，注意核查订单！】\n"
       "请核对城市、影院是否一致\n"
       "张数：{count}张，请核对以上场次、座位和金额。\n"
       "如一致，直接拍下即可（拍下后我帮您改价）"),

    _t("SELF_BUY_HINT", "3", "报价高于自购价", "quote",
       "算出的价高于买家自己在 App 能买到的价格",
       [], ["不硬卖，直接告诉买家自己买更划算 —— 这是口碑资产，不是损失。",
            "末尾留一句「下次来比价」，把这次让利变成下次机会。"],
       "您现在有合适的优惠价，可以自行购买。\n\n"
       "下次没有优惠了，可以发我截图比价哈[送花]"),

    _t("SHOWTIME_LIMIT", "30", "距开场太近不接单", "quote",
       "距离开场不足配置的分钟数（默认 40 分钟）",
       ["limit_minutes"],
       ["★ 对应配置「放映时间限制 › 放映剩余分钟」。",
        "话术本身也是可配的（放映时间限制话术），这里给的是默认口径。",
        "为什么不接：出票要时间，开演前没出票买家就白等，只能退款。"],
       "您好，您截图的影片场次距离开始放映不足{limit_minutes}分钟，"
       "无法优惠购票，建议您选择其他场次"),

    _t("REPRICE_REQUEST", "", "买家要求改价 · 引导先拍下", "quote",
       "★ 买家说「改价」「便宜点」时回这条 —— 引导他先拍下，拍下后我们改价",
       [],
       ["★★ 店主选 C（2026-10-14）：「告诉他先拍下，拍下后我给你改价」",
        "  ★ 为什么是 C 而不是「先改价再让他拍」：",
        "    闲鱼的改价接口是【针对已有订单】的（orders.changePrice(orderId, ...)），",
        "    没有订单就没有可改的对象 —— 先改价在闲鱼上根本做不到。",
        "  ★ 为什么不说「你先付」：付款后这单就进了出票流程（不可拦截），",
        "    必须先改价、买家核对金额、再付款。顺序错了会出纠纷。",
        "  ★ 实测依据：线上真实会话里店家就是这么说的 ——",
        "    「请直接提交订单，拍下后先不要付款；系统会按已确认的 N 张核算并改价；",
        "      收到『改价已完成，可以付款』的通知后再付款。」",
        "  ★ 这张卡没有 bubble 码（不对应同业任何一条文案）。",
        "  ★ 它【不改变会话状态】—— 只是回一句话，钱和流程都不动。"],
       "可以的~ 你先【拍下这单】，但【先不要付款】\n"
       "拍下后我按你确认的座位和张数给你改价，\n"
       "你看到金额对了再付款哦~"),

    _t("ORDER_DUPLICATED", "300010", "同场次同座位已有单在出票", "order",
       "换了单号但对同一场次提交了完全相同的座位，且已有单在出票中",
       [],
       ["★ 平台返回 300010 —— 这是【唯一不能重试】的下单失败码。",
        "重试只会再撞一次；而且说明买家（或我们）可能重复提交了。",
        "文案要如实说明，不要含糊成「系统繁忙」—— 免得买家又拍一单。"],
       "亲，这个场次这几个座位已经有订单在出票中了～\n"
       "麻烦核实一下，避免重复下单哈\n"
       "如果您确实没拍过，跟我说一声我帮您查"),

    # ---------------------------------------------------------------- 拍单与改价
    _t("ORDERED_NO_PAY", "17", "拍下后别付款", "order",
       "买家回复确认指令、拍下订单后发送", [],
       ["★ 风控话术：「不要付款」是命令不是建议。",
        "不许模型弱化成「先别急着付款」—— 弱化就会有人提前付款，触发金额校验失败。"],
       "[ok]已收到您的订单，您拍下不要付款，我改价后，您再付款~",
       locked=True),

    _t("NO_QUOTE", "16", "没报价就拍下", "order",
       "买家在没有有效报价的情况下拍下", [],
       ["自动回复，不需要人工。引导买家重新发图走正常流程。"],
       "亲，无报价记录或原报价记录时间过久已失效！ 请重新发选座截图，按系统提示操作订票！"),

    _t("QUOTE_EXPIRED", "16", "报价已过期", "order",
       "报价超过配置的有效时长（默认 30 分钟）后买家才回复",
       ["quote_ttl_minutes"],
       ["★ 文案里说「限时2分钟秒杀」，但配置的报价记录有效时长是 30 分钟。",
        "这里按【配置的真实值】判断过期，不按文案。",
        "过期后回到空闲，买家重发截图即可 —— 不是报错，是重来。"],
       "亲，您的报价单已超过{quote_ttl_minutes}分钟有效期，已自动失效~ 请重新发选座截图，我给您重新报价！"),

    _t("REPRICED_CHECK", "18", "改价完成请核对", "order",
       "改价成功后发送。订单金额在此锁定",
       ["city", "cinema", "movie", "hall", "show_time", "seats", "cinema_addr"],
       ["★ 风控话术：「付款后不支持退改签」是平台判责依据，逐字发送。",
        "「部分截图最多一次只能识别三张票」是业务限制说明，别删。"],
       "城市、影院、座位数易识别错误，请核对与您的截图是否一致\n"
       "请核对以下信息，付款后不支持退改签\n\n"
       "※【{city}的】| {cinema}\n"
       "电影：{movie}\n"
       "影厅：{hall}\n"
       "场次：{show_time}\n"
       "座位：{seats}\n"
       "影院地址：{cinema_addr}\n\n"
       "【部分截图最多一次只能识别三张票】\n"
       "没有识别到的座位，请拍下这单后重新截图发我识别\n"
       "[号外]请您务必！务必！务必！再认真核对下出票信息~",
       locked=True),

    _t("PURCHASE_NOTICE", "15", "购票须知", "order",
       "买家付款前必须发送的须知",
       ["ticket_window"], ["★★ 风控资产，逐字发送，绝不许模型改写。",
            "拦截失败拒绝退款时，平台依据聊天记录判责，这段是唯一证据。",
            "第⑤条「影院降价不支持补差价」是常识性免责条款，别删。"],
       "[举手][举手]【购票须知】[举手][举手]\n\n"
       "①默认按上方《购票信息》出票，请核对后再支付\n"
       "②付款后系统{ticket_window}自动出票，无法拦截；付款后、出票时、出票后皆不能改签和退票；\n"
       "③如选座被锁，不再和你沟通，系统自动就近择优调座，保证连座；要求必须原座的勿拍；\n"
       "④部分订单会出票失败，则立刻退款；如离开场不足40分钟勿支付，出不了票。\n"
       "⑤影院降价不支持补差价；\n\n"
       "！请务必认真阅读并同意上述内容后，再下单！",
       locked=True),

    _t("REPRICE_MANUAL_HINT", "28", "拍下后要买家去拍", "order",
       "改价流程需要买家配合时",
       [], ["这条不报错、不指责，直接教买家怎么拍 —— 含免密支付的处理提示。"],
       "点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）"),

    _t("REPRICED_OK", "31", "改价成功（手动）", "order",
       "后台手动改价成功后", [],
       ["与 18 的区别：18 是自动改价后的完整核对，31 是手动改价后的简短通知。"],
       "改好价格了，您付款后，这边为您出票~"),

    _t("REPRICED_AGAIN", "22", "付款前重新改价", "order",
       "买家在付款前又发了新截图，系统重新报价改价",
       ["city", "cinema", "movie", "hall", "seats", "cinema_addr", "show_time",
        "unit_price", "total_price", "count"],
       ["改价成功后发送，金额、影片、场次和座位使用本次订单的实际信息。"],
       "[dh17o4vx6ew]{city}的，{cinema}，{seats}\n"
       "影院地址：{cinema_addr}\n"
       "放映时间：{show_time}\n"
       "请认真核对上面信息\n"
       "{unit_price}元一张，已为您重新改价，付款后，这边为您出票~"),

    _t("FLASH_OFFER", "37", "限时特价", "order",
       "买家没拍下、犹豫时给出的限时价",
       ["total_price"], ["⚠️ 文案承诺「2分钟内有效」，但报价卡也说「限时2分钟秒杀」。",
                         "需确认：这是真的 2 分钟时效，还是营销话术？",
                         "如果实际不是 2 分钟，是【合规风险】—— 买家可以据此投诉。"],
       "{total_price}给你，要吗宝宝！！\n【成本价】2分钟内有效"),

    _t("TICKET_MODE_CHANGED", "33", "票速变化按新价改价（★ 已停用）", "order",
       "★★★★ 店主明确说：快速票加价通道我们【不需要】，这条不启用。",
       ["city", "cinema", "cinema_addr", "movie", "hall", "show_time",
        "seats", "unit_price", "total_price", "discount"],
       ["★★★★ 2026-10-13 店主决定：【快速票加价通道不需要】。",
        "  ⇒ 这条模板保留仅为记录同业规格，运行时不使用。",
        "  ★ 为什么保留而不是删掉：",
        "    ① 同业线上预览里有这个码（bubble 33），删了以后对照时会对不上；",
        "    ② 万一以后要开快票通道，文案是逐字对齐过的，不用重新找。",
        "  ★★ 但它【不能】出现在任何发送路径上 —— 见 _check_contract 的死模板检查",
        "     和 templates.ENABLED 开关。",
        "---- 以下是同业原文（备查）----",
        "★ 依据：同业线上预览里 bubble 33 场景标签「票速变化，按新价改价」，四段话：",
        "  影院信息 / 优惠信息 / 核对提示 / 原因说明。",
        "★ 「因为距离放映时间较近，更改为快票通道会贵一点」是把涨价原因讲明白。",
        "★ {discount} 是总优惠金额（原价合计 − 成交合计）。"],
       "【影院信息】{city}-{cinema_addr}-{cinema}\n"
       "---\n"
       "【购票信息】{movie}-{hall}-{show_time}-{seats}\n"
       "---SPLIT---\n"
       "【优惠信息】{unit_price}元/张，合计{total_price}元，总优惠{discount}元\n"
       "---SPLIT---\n"
       "城市、影院、座位数易识别错误，请核对与您的截图是否一致！\n"
       "---SPLIT---\n"
       "因为距离放映时间较近，更改为快票通道会贵一点，已为您重新报价并改价，请留意"),

    _t("CONFIRM_INVALID", "12", "确认指令不存在", "order",
       "买家回复的确认指令序号不对或格式不对", [],
       ["不报错、不指责，直接给两条出路：核对指令 / 重发截图。"],
       "[尴尬] 您提交的确认指令不存在，请核对后重新发送，或直接提供选座截图帮您处理哦~"),

    # ---------------------------------------------------------------- 付款核验
    _t("AMOUNT_MISMATCH", "23", "金额校验失败", "payment",
       "买家付款金额与报价不一致（通常是没等改价就付款）",
       [], ["★ 自动处理：关单 + 引导重拍。",
            "用轻松的语气化解买家的尴尬（「手速太快」），同时明确要求重拍不要付款。"],
       "[撇嘴]您手速太快了！订单金额校验失败，辛苦重新拍下不要付款哦~~"),

    _t("PAID_NO_QUOTE", "19", "没报价就付款", "payment",
       "买家在我们还没报价的情况下就付款了", [],
       ["★ 自动处理：退款 + 引导重来。",
        "钱要马上还给买家，不要挂起等人工。"],
       "亲，我还没报价，你怎么就付款了，这边为您退款了，您可以重新发我选座截图识别~"),

    # ---------------------------------------------------------------- 取票交付
    _t("TICKETING", "7", "已下单等待出票", "ticket",
       "买家付款后、系统提交出票成功。拆【两条】发送",
       ["cinema", "ticket_window", "pickup_link"],
       ["★ 时效用的是{ticket_window}变量 —— 必须跟实际购入档位一致，不许写死。",
        "★★ {pickup_link} 是良票【下单就返回】的免登录取票页。文档明确："
        "「可提前发给顾客，页面免登录、只有观影信息与取票凭证、**不含任何价格**，"
        "出票中会自动刷新，出票后自动出现票码」。",
        "所以下单后就发 —— 买家能自己看进度，出票前就不用来问了。",
        "第二条里的【拦截订单】是买家指令 —— 买家发现信息不对时发这个。",
        "付款后是买家最焦虑的一段，必须主动安抚，不要沉默。"],
       "系统已自动下单，请耐心等待出票结果~\n"
       "出票时间{ticket_window}\n"
       "取票链接（可自行查看进度）：{pickup_link}\n"
       "自动将影院取票码发这里，请耐心等待\n\n"
       "正在加急排队对接{cinema}中。。。\n"
       "---SPLIT---\n"
       "如出票失败也会立刻退款，常见问题回复“999”\n"
       "如果下单发现信息不对，请发送【拦截订单】",
       multi=True,
       # ★★ 万达覆盖：只删掉「取票链接」那一行，其余逐字不动。
       #    为什么：{pickup_link} 是良票【下单就返回】的免登录取票页；
       #    万达是店家手动出票，没有这个链接 ——
       #    留着会把买家引到一个空链接上。
       #    ★ 风控相关的话（出票失败立刻退款 / 拦截订单）不许删。
       channel_text={
           "wanda": (
               "系统已自动下单，请耐心等待出票结果~\n"
               "出票时间{ticket_window}\n"
               "自动将影院取票码发这里，请耐心等待\n\n"
               "正在加急排队对接{cinema}中。。。\n"
               "---SPLIT---\n"
               "如出票失败也会立刻退款，常见问题回复“999”\n"
               "如果下单发现信息不对，请发送【拦截订单】"
           ),
       }),

    _t("TICKET_FAILED_REFUND", "9", "出票失败自动退款", "ticket",
       "上游出票失败或无供应商接单", [],
       ["★ 先说结果（退款），再说原因。买家最关心钱。"],
       "抱歉亲，出票失败，给您退款哈~"),

    _t("CREATE_FAILED", "8", "建单失败（位置价格不对）", "ticket",
       "建单时发现某个座位价格不对", [],
       ["★ 处理方式很具体：让买家【取消后分别重新截图】。",
        "因为一张订单里混了价格异常的座位，只能拆开重下。"],
       "其中有一个位置价格不对哦，辛苦取消分别重新截图下单哈"),

    _t("SUBSIDY_REDO", "34", "影院调价已补贴重下", "ticket",
       "上游因影院临时调最低票价而失败，卖家补贴重新下单",
       [], ["★ 这条背后是「补贴重下」这个真金白银的动作，不是纯话术。",
            "没有这个能力就不要发这条 —— 否则是骗买家。"],
       "亲，同步下影票进度～刚因影院临时调最低票价，订单暂未出票，很抱歉！"
       "已补贴重下并优先处理，取票码出了立刻发您，辛苦稍等，有问题随时找我～"),

    _t("TICKET_CODE", "10", "出票成功发取票码", "ticket",
       "出票成功，交付凭证。拆【三条】：票码 + 求关注 + 取票图",
       ["ticket_link", "ticket_code"],
       ["★ 「不要在淘票票机器取」那句是避免纠纷的关键。",
        "第二条里的【获取票码】是买家指令。"],
       "出票成功！票码已发，请您注意查收，祝您观影愉快，记着5分好评哦~！\n"
       "取票链接：{ticket_link}\n\n"
       "温馨提示：\n"
       "提前提示告知一下，部分票不要在淘票票的机器取（因为部分票不是在淘票票购买的，"
       "所有机子都可以试试）用影院的自助机或者联系前台人工取票！！！\n \n"
       "如自助机无法扫码出票，请输入{ticket_code}取票；\n"
       "如还不行，请联系前台工作人员取票；\n"
       "---SPLIT---\n"
       "亲爱的可以点个关注吗，以免下次找不到我啦\n"
       "如缺少取票码请发送【获取票码】\n"
       "---SPLIT---\n"
       "（配图：取票示意图）",
       multi=True),

    _t("TICKET_CODE_MANUAL", "26", "出票成功（手动出票）", "ticket",
       "没有识别记录，卖家手动出票的情况",
       ["ticket_code"],
       ["★ 与 TICKET_CODE 的区别：【没有取票链接那一行】。",
        "所以拆成了两个模板 —— 手动出票没有链接可给。"],
       "出票成功！票码已发，请您注意查收，祝您观影愉快，记着5分好评哦~！\n\n"
       "温馨提示：\n"
       "提前提示告知一下，部分票不要在淘票票的机器取（因为部分票不是在淘票票购买的，"
       "所有机子都可以试试）用影院的自助机或者联系前台人工取票！！！\n\n\n"
       "如自助机无法扫码出票，请输入{ticket_code}取票；\n"
       "如还不行，请联系前台工作人员取票；\n"
       "---SPLIT---\n"
       "亲爱的可以点个关注吗，以免下次找不到我啦\n"
       "---SPLIT---\n"
       "（配图：取票示意图）",
       multi=True),

    _t("TICKET_CODE_UPDATED", "20", "票码更新", "ticket",
       "上游更新了取票码", ["ticket_code"],
       ["票码变更必须主动推给买家，否则买家拿旧码去取票会失败。"],
       "票码有更新，请按此票码取票，{ticket_code}"),

    # ---------------------------------------------------------------- 退票
    _t("REFUND_APPLIED", "5.7", "退票已受理", "aftercare",
       "我们替买家提交了退票申请（order/refund 已返回退票单号）",
       [], ["★ 文档：可退与否由供应商与时间决定，"
            "所以提交成功【不等于】退票成功 —— 不要让买家以为已经退了。",
            "失败码：500001 供应商不支持退票 / 500002 已过可退时间 / "
            "500003 退票申请重复。"],
       "退票申请已经给您提交了哈~\n"
       "退票结果出来我第一时间告诉您，麻烦稍等一下[比心]"),

    _t("REFUND_NOT_ALLOWED", "5.3", "当前不可退票", "aftercare",
       "订单详情 refundable=false —— 当前不可发起主动退票",
       [], ["★ 良票订单详情里有权威字段 `refundable`：「当前是否可发起主动退票」。",
            "用它比用错误码反推准：不用提交一个必被拒的申请，",
            "也不会把「已过可退时间」误说成「供应商不支持」。",
            "★ 语气要注意：买家想退退不了会不高兴，",
            "所以要说清为什么、以及还能怎么办（票依然能用）。"],
       "亲，这一单当前不支持退票哈~\n"
       "影院和场次的可退规则不一样，这个场次是出票后不可退的。\n\n"
       "票是【正常有效】的，可以照常取票观影~\n"
       "如果您去不了，可以问问朋友有没有要看的[比心]"),

    _t("REFUND_DONE", "order.refunded", "退票完成", "aftercare",
       "收到 order.refunded 回调", ["fee_amount", "refund_amount"],
       ["★ 金额以回调的 refundAmount 为准；有手续费时 feeAmount 不为 0，要如实说。"],
       "亲，退票成功了~\n"
       "退还金额：{refund_amount} 元\n"
       "手续费：{fee_amount} 元"),

    _t("REFUND_REJECTED", "order.refund_rejected", "退票未成（票仍有效）", "aftercare",
       "收到 order.refund_rejected 回调", ["fail_reason"],
       ["★★ 最关键的一点：文档说「收到该事件后订单回到 TICKETED，"
        "**凭证依然有效**，冻结资金等散场实扣」。",
        "所以必须明确告诉买家【票还能用】—— 否则买家以为退了就不去看了，"
        "那是实打实的损失。"],
       "亲，退票没能成功，原因是：{fail_reason}\n\n"
       "您的票【依然有效】，还可以正常取票观影哈~\n"
       "取票码没有变，之前发给您的那个还能用"),
]


# ==================================================================== 停用
# ★★★★ 店主明确说不做的功能，对应的模板登记在这里。
#
# ★ 为什么是停用而不是删掉：
#     删了就查不到同业有这个码、我们为什么不做；
#     保留但明确停用，后来人一眼能看懂。
#
# ★★ 停用 = 【不发、不可选】，但仍在 TEMPLATES 里（对照/备查用）。
#     send 路径会拦住它 —— 见 render_messages 的守卫和
#     tests/_check_contract.py 的死模板检查。
# Wanda fulfillment has no Liangpiao 40-minute restriction.
for _row in TEMPLATES:
    if _row["key"] == "PURCHASE_NOTICE":
        _row["channel_text"]["wanda"] = (
            "【万达购票须知】\n"
            "①请核对影院、影片、场次、张数和金额，确认无误后再付款。\n"
            "②付款后由店家处理出票，出票成功后发送取票码。\n"
            "③指定座位以核对及实际出票结果为准，无法满足时会与您沟通。\n"
            "④临近开场请先确认能否及时出票；如出票失败，会核对订单并处理退款。\n"
            "⑤影院降价不支持补差价。")

DEFAULT_CHANNEL_TEXT = {row["key"]: dict(row.get("channel_text") or {})
                        for row in TEMPLATES}

DISABLED = {
    # 快速票加价通道：店主不需要（2026-10-13）
    # ★ 这里写【字符串】不是变量 —— 上面的报错就是漏了引号：
    #   NameError: name 'TICKET_MODE_CHANGED' is not defined
    "TICKET_MODE_CHANGED",
}


def is_disabled(key: str) -> bool:
    "这条模板是不是停用了（停用的一律不许发）。"
    return key in DISABLED


# ==================================================================== 渠道
CHANNELS = [{"key": "wanda", "name": "万达"}, {"key": "liangpiao", "name": "良票"}]


# ==================================================================== 渲染
def find(key: str) -> dict:
    row = next((t for t in TEMPLATES if t["key"] == key), None)
    if row is None:
        raise KeyError(f"未知模板: {key}")
    from application.shop_scope import template
    return template(row)


def _display_values(values: dict | None) -> dict:
    """Format buyer-facing show times in China time; preserve stored facts."""
    from datetime import datetime, timedelta, timezone

    result = dict(values or {k: v[2] for k, v in VARIABLES.items()})
    value = result.get("show_time")
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            pass  # Keep already human-readable or unknown text intact.
        else:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone(timedelta(hours=8)))
            result["show_time"] = parsed.strftime("%Y-%m-%d %H:%M")
    return result


def render(key: str, values: dict | None = None,
           channel: str | None = None) -> str:
    """
    把模板渲染成买家看到的话。

    两步（顺序不能颠倒）：
      1. 【替换变量】{city} → 周口
      2. 【展开引用】[ggx1z82tg84] → 那段关键词文案

    ★ 为什么先变量后引用：
      变量替换只动 {xxx}，引用展开只动 [xxx]，作用域不重叠，
      但先变量可以避免变量值里恰好含 [xxx] 时被误展开。

    ★ 未知变量/未知引用都原样保留（方便发现配置错误），不静默吞掉。

    channel：按渠道取文案（见 text_for）。不传就是全局。
    """
    values = _display_values(values)
    variant = str(values.pop("_variant", "") or "") or None
    text = text_for(key, channel, variant)
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return qa.REF_PATTERN.sub("", qa.resolve(text)).strip()


def text_for(key: str, channel: str | None = None,
             variant: str | None = None) -> str:
    """
    取这条模板在当前渠道下【实际要用的文案】。

    ★ 两层结构（这是"按渠道存"的核心）：
        row["text"]                       全局文案 —— 所有渠道的基准
        row["channel_text"]["wanda"]      该渠道的覆盖（可缺）

    规则：渠道有覆盖就用覆盖，没有就回落全局。
          channel 为 None 或 "global" 时直接用全局。

    ★ 为什么覆盖而不是替换：
      运营 90% 的修改是"所有渠道都该这么改"（改全局）；
      只有少数是"万达这句要不一样"（改覆盖）。
      把全局当基准，改一次就全生效，不用每个渠道各改一遍。

    variant：场景变体（见 _t 的 variants 说明）。
             ★ 优先级：渠道覆盖 > 变体 > 全局默认。
               因为渠道覆盖是运营手改的，应当压过代码里的变体。
    """
    row = find(key)
    base = row.get("text") or ""
    overrides = row.get("channel_text") or {}
    if channel and channel != "global" and channel in overrides:
        return overrides[channel]
    if not base.strip():
        return ""
    if variant:
        v = (row.get("variants") or {}).get(variant)
        if isinstance(v, str) and v.strip():
            base = v
    if not channel or channel == "global":
        return base
    overrides = row.get("channel_text") or {}
    value = overrides.get(channel)
    return value if isinstance(value, str) else base


def render_messages(key: str, values: dict | None = None,
                    channel: str | None = None) -> list:
    """
    ★ 这是最贴近真实发送行为的方法 —— 返回**按顺序要发的多条消息**。

    票达人的一条模板实际可能发出【好几条消息】，来源有两个：
      1. 模板自身的 ---SPLIT--- 分段
      2. 模板末尾引用的 [全局调用标识ID] —— 每条引用展开成【独立一条】

    为什么引用要单独成条而不是就地插入：
      报价卡末尾写 ，
      接在"如一致，回复'正确'开始购票"后面读起来是断的；
      而聊天记录里它们是分开两条发的。所以引用 = 追加消息。

    channel：按渠道取文案（见 text_for）。不传就是全局。
    """
    # ★★★★ 停用的模板一律不许发给买家。
    #   为什么要有这道守卫（而不是靠没人调用）：
    #     死代码检查只能发现现在没人调用，发现不了
    #     以后有人顺手调了一条店主明确说不做的功能。
    #     这条守卫是【硬拦截】—— 调了就抛，不给静默发送的机会。
    if is_disabled(key):
        raise KeyError(
            f"模板 {key} 已停用（见 templates.DISABLED）—— "
            f"店主明确说不做的功能，不许发送")

    values = _display_values(values)
    # ★ _variant 是【选择用哪个变体】的控制键，不是要替换的变量 ——
    #   取出来然后从 values 里删掉，免得它漏进文案。
    variant = str(values.pop("_variant", "") or "") or None
    text = text_for(key, channel, variant)
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))

    messages = []
    for part in text.split("---SPLIT---"):
        part = part.strip()
        if not part:
            continue
        # 正文（引用已摘掉）
        body = qa.REF_PATTERN.sub("", part).strip()
        if body:
            messages.append(body)
        # 每条引用追加成独立消息
        for ref_id in qa.REF_PATTERN.findall(part):
            rule = qa.BY_ID.get(ref_id)
            if rule is not None:
                resolved = qa.REF_PATTERN.sub("", qa.resolve(rule.content)).strip()
                if resolved:
                    messages.append(resolved)
    return messages


def render_parts(key: str, values: dict | None = None) -> list:
    """
    渲染成【多条消息】。保留是为了兼容 —— 等价于 render_messages()。
    """
    return render_messages(key, values)


def catalog() -> list:
    """给前端用的目录：按阶段分组，附带变量说明。"""
    groups = []
    for stage_key, stage_name in STAGES:
        rows = []
        for original in TEMPLATES:
            t = find(original["key"])
            if t["stage"] != stage_key:
                continue
            rows.append({
                "key": t["key"], "bubble": t["bubble"], "name": t["name"],
                "trigger": t["trigger"], "notes": t["notes"],
                "locked": t["locked"], "multi": t["multi"],
                # ★ 下划线开头的是【控制键】，不是要渲染的变量（如 _variant）。
                #   必须过滤掉 —— 否则这里查 VARIABLES 会 KeyError，
                #   整个 /api/templates 挂掉，回复文案页直接打不开。
                "variables": [{"key": v, "name": VARIABLES[v][0],
                               "description": VARIABLES[v][1],
                               "example": VARIABLES[v][2]}
                              for v in t["variables"]
                              if not v.startswith("_")
                              and v in VARIABLES],
                "text": t["text"],
                # ★ 渠道覆盖也发给前端 —— 不然"万达"tab 点开永远是空的，
                #   运营以为自己刚才的修改没保存。
                "channel_text": t.get("channel_text") or {},
                # ★ 场景变体也发给前端 —— 运营要能看到"这条模板有几种说法"，
                #   否则在界面上只看到默认那条，改了变体也不知道去哪改。
                "variants": t.get("variants") or {},
            })
        if rows:
            groups.append({"key": stage_key, "name": stage_name, "items": rows})
    return groups


if __name__ == "__main__":
    print(f"共 {len(TEMPLATES)} 条模板，分 {len(STAGES)} 个阶段\n")
    for stage_key, stage_name in STAGES:
        rows = [t for t in TEMPLATES if t["stage"] == stage_key]
        print(f"【{stage_name}】{len(rows)} 条")
        for row in rows:
            flags = []
            if row["locked"]:
                flags.append("风控")
            if row["multi"]:
                flags.append("多条")
            print(f"    {row['name']:18} bubble={row['bubble']:34} {'/'.join(flags)}")
    print("\n=== 渲染示例：报价卡 ===")
    print(render("QUOTE_CARD"))
    print("\n=== 渲染示例：已下单等待（拆 %d 条）===" % len(render_parts("TICKETING")))
    for i, part in enumerate(render_parts("TICKETING"), 1):
        print(f"  [{i}] {part}")
        print()
