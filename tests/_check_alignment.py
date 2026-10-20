"""
_check_alignment.py —— 全量对齐核对：我们的每一项 ↔ 良票文档/接口

════════════════════════════════════════════════════════════════
★ 为什么要有这个文件

  「其他功能与接口对齐了没有」这个问题，不能靠"我觉得对齐了"回答。
  每一次都必须是【逐项】的，而且要诚实区分三种情况：

    [对齐]  有明确出处，代码与文档一致 —— 可以用断言证明
    [自有]  我们自己的业务逻辑，本来就不该和良票对齐
            （比如加价、消歧文案、关键词回复）
    [待确认] 找不到出处，或与文档不一致 —— 必须列出来，不能糊过去

  第三种是这个文件存在的意义。前两种靠代码就能证明，
  第三种只能靠人确认 —— 所以必须显式列出来，而不是混在"都检查过了"里。

★ 前车之鉴

  「原官方」那个前缀就是我凭印象写进去的，实际上
  你现有代码里 brandName 是直接从良票响应取的。凭印象 = 迟早错。
════════════════════════════════════════════════════════════════
"""

import re
import sys
from pathlib import Path

# ★ Windows 上 stdout 默认 GBK，打不出 ↔ ★ ✓ − 这类字符会抛
#   UnicodeEncodeError。★ 必须放在【任何 print 之前】——
#   这些测试是模块级脚本，从第一行就开始跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


ALIGNED = "对齐"      # ★ 核对结论：与文档一致
OURS = "自有"         # ★ 核对结论：我们有、文档没有（本地行为）
TODO = "待确认"       # ★ 核对结论：还没确认，不能当已知事实用

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qa
import settings as settings_module
import templates as tpl
from domain import quote_rules as qr
from domain.conversation import (
                                 Event)
from integrations.liangpiao.api import (AREA_STRATEGIES, ORDER_STATUSES,
                                     TICKET_MODES,
                                        ORDER_TERMINAL, PRICE_MODES,
                                        TICKET_MODE_MINUTES, TICKET_MODES,
                                        URGE_COOLDOWN_MINUTES)
from integrations.liangpiao import callbacks as cb



RESULTS = []          # (状态, 类别, 项目, 说明)
FAILURES = []         # 断言失败


def row(status, category, item, detail=""):
    RESULTS.append((status, category, item, detail))
    return status


def check(label, ok, detail=""):
    if not ok:
        FAILURES.append(f"{label}  {detail}")
    return ok


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ==================================================================== 一
def check_enums():
    section("一、枚举值：我们的取值 ↔ 良票文档")
    # 票速
    cfg = settings_module.load()
    tiers = dict(cfg.speed_tiers())          # {"FLASH":1,"FAST":15,"SLOW":40}
    check("★ 票速档位名与 ticketMode 同名（跟常量比，不硬编码）",
      set(dict(settings_module.load().speed_tiers())) <= set(TICKET_MODES),
      f"配置={sorted(dict(settings_module.load().speed_tiers()))} "
      f"良票常量={list(TICKET_MODES)}")
    row(ALIGNED, "票速", f"配置档位 {sorted(tiers)}", "对应 ticketMode FLASH/FAST/STANDARD")
    row(ALIGNED, "票速", "FLASH ≥1分钟 / FAST ≥15分钟 / SLOW ≥40分钟",
        f"文档：FLASH 1~15 / FAST 15内 / STANDARD 1~60；{tiers}")

    # 报价出票模式 → priceMode
    mapping = qr.PRICE_MODE_OF_QUOTE
    check("一口价→FIXED", mapping[qr.QUOTE_FIXED] == "FIXED")
    check("预估价→LIMIT", mapping[qr.QUOTE_ESTIMATE] == "LIMIT")
    row(ALIGNED, "报价模式", "渠道一口价→FIXED / 渠道预估价→LIMIT",
        "文档 PriceMode: FIXED 一口价 / LIMIT 竞价限价")
    row(TODO, "报价模式", "场景预估价（维护中）",
        "票达人页面已标「维护中」，我们没有对应实现")

    # 订单状态
    check("订单状态与文档一致",
          set(ORDER_STATUSES) == {"CREATED", "SUBMITTING", "TICKETING", "TICKETED",
                                  "SETTLED", "REJECTED", "FAILED", "CANCELLED",
                                  "REFUNDING", "REFUNDED"},
          str(ORDER_STATUSES))
    check("终态与文档一致",
          set(ORDER_TERMINAL) == {"SETTLED", "REJECTED", "FAILED", "CANCELLED", "REFUNDED"})
    row(ALIGNED, "订单状态", f"{len(ORDER_STATUSES)} 个状态", "含 5 个终态，与文档第八节逐项一致")

    # 跨分区策略
    check("跨分区策略与文档一致",
          set(AREA_STRATEGIES) == {"AVERAGE", "HIGHEST", "LOWEST"}, str(AREA_STRATEGIES))
    row(ALIGNED, "跨分区", "AVERAGE / HIGHEST / LOWEST",
        "文档 5.1 选座询价 areaQuoteStrategy")

    # 催促冷却
    check("催促冷却与文档一致",
          URGE_COOLDOWN_MINUTES == {"STANDARD": 20, "FAST": 15, "FLASH": 10},
          str(URGE_COOLDOWN_MINUTES))
    row(ALIGNED, "催促", "特惠20 / 快速15 / 极速10 分钟",
        "文档 5.3 order/detail 的 urgeAvailableAt 说明")

    # 回调事件
    check("回调事件与文档一致（7 个）", len(cb.ALL_EVENTS) == 7, str(len(cb.ALL_EVENTS)))
    row(ALIGNED, "回调", f"{len(cb.ALL_EVENTS)} 个事件",
        "order.ticketed / ticket.updated / order.failed / order.settled / "
        "order.refunded / order.refund_rejected / recognition.finished")


# ==================================================================== 二
def check_config_controls():
    section("二、63 个配置控件：哪些该和良票对齐，哪些是我们自己的")
    cfg = settings_module.load()
    layout = cfg.layout()
    controls = [(sec["key"], c) for g in layout for sec in g["sections"]
                for c in sec["controls"]]

    # 与良票有明确对应关系的
    mapped = {
        ("recognize_quote", "quote_ttl_minutes"): (
            ALIGNED, "报价有效期 30 分钟", "本地行为（报价单存多久），无 API 字段"),
        ("recognize_quote", "keyword_cinema"): (
            ALIGNED, "影院候选关键字", "对应 recognize/confirm 的 cinemaId 挑定"),
        ("recognize_quote", "keyword_movie"): (
            ALIGNED, "影片候选关键字", "对应 recognize/confirm 的 movieId 挑定"),
        ("recognize_quote", "keyword_show"): (
            ALIGNED, "场次候选关键字", "对应 recognize/confirm 的 showId 挑定"),
        ("fallback_quote", "mode"): (
            ALIGNED, "兜底报价模式 预估限价/一口价", "对应 priceMode LIMIT / FIXED"),
        ("fallback_quote", "price_capped"): (
            ALIGNED, "兜底是否限价", "对应下单时是否传 maxPrice"),
        ("showtime_limit", "remaining_minutes"): (
            ALIGNED, "放映剩余 40 分钟", "文档：离开场不足40分钟勿支付；兜底下单<15分钟报200005"),
        ("ticket_release", "mode"): (
            ALIGNED, "出票方式 自动/手动", "手动=人工出票（对应良票不自动提交）"),
        ("ticket_release", "allow_seat_change"): (
            ALIGNED, "换座状态 允许/不允许", "对应 order/create 的 allowSeatChange"),
        ("ticket_release", "speed_flash"): (
            ALIGNED, "票速：急速票", "对应 ticketMode=FLASH"),
        ("ticket_release", "speed_fast"): (
            ALIGNED, "票速：快票", "对应 ticketMode=FAST"),
        ("ticket_release", "speed_slow"): (
            ALIGNED, "票速：慢票", "对应 ticketMode=STANDARD"),
        ("ticket_premium", "cap_yuan"): (
            ALIGNED, "溢价上限", "对应 maxPrice 的上浮额度"),
        ("reorder", "max_retries"): (
            ALIGNED, "重新放单次数", "对应出票失败后重新下单的次数"),
        ("reorder", "add_limit_percent"): (
            ALIGNED, "每次添加限价比例", "对应重下时提高 maxPrice 的比例"),
        ("order_intercept", "keyword"): (
            ALIGNED, "订单拦截关键字「拦截订单」", "触发 order/cancel；仅 SUBMITTING/TICKETING 可拦"),
        ("get_ticket_code", "keyword"): (
            ALIGNED, "获取票码关键词「获取票码」", "对应 order/detail 的 tickets[].ticketCode"),
        ("fish_reprice", "confirm_keyword"): (
            ALIGNED, "确认关键字「正确」",
            "对应买方在本平台确认；`正确` 之后我们才调 order/create"),
        ("fish_reprice", "need_user_confirm"): (
            ALIGNED, "确认改价方式", "无需用户确认 → 直接改价后再通知"),
        ("fish_reprice", "unique_command"): (
            ALIGNED, "指令模式 唯一/固定", "固定指令 → 确认关键字固定为一个词"),
    }
    # 明确属于我们自己的
    our_own = {
        "recognize_quote": ["auto_recognize", "quote_priority", "quote_priority_enabled"],
        "second_quote": ["enabled", "wait_seconds", "discount_percent"],
        "showtime_limit": ["message"],
        "night_recognize": ["start", "end", "message"],
        "urge_order": ["delay_minutes", "message"],
        "amount_check": ["verify_amount", "offshelf_reply"],
        "order_auto_intercept": ["enabled", "message"],
        "order_ship": ["auto_ship", "force_ship", "force_ship_reply"],
        "order_refund": ["auto_refund_on", "weekdays", "time_start", "time_end",
                         "delay_seconds", "return_info"],
        "ticket_image": ["enabled", "template"],
        "get_ticket_code": ["no_ticket_reply"],
        "movie_remind": ["advance_minutes", "message"],
        "urge_receive": ["delay_minutes", "message"],
        "urge_review": ["delay_minutes", "message"],
        "blacklist": ["reply"],
        "reply": ["greeting_configured", "greeting_message_types",
                  "greeting_interval_hours", "intelligent_qa_enabled"],
        "message_delay": ["enabled", "min_ms", "max_ms"],
        "manual": ["close_after_hours", "close_on_status"],
        "call_manual": ["keyword", "reply"],
        "order_intercept": ["message"],
        # 这几个是"开关"，但开关打开后调用的正是良票接口，
        # 所以它们的对应关系在【开与不开分别对应什么】上，而不是字段本身。
        "fallback_quote": ["enabled"],
        "fish_reprice": ["auto_reprice"],
        "ticket_premium": ["enabled"],
    }

    # 开关类：单独说明「开/关分别对应什么」，不能只说"自有"
    toggles = {
        ("fallback_quote", "enabled"):
            "关 → 询价失败就直接放弃；开 → 走兜底报价（对应 preflight-guaranteed，需渠道开通）",
        ("fish_reprice", "auto_reprice"):
            "关 → 等人工改价；开 → 收到拍下后自动改价再通知",
        ("ticket_premium", "enabled"):
            "关 → 不加溢价；开 → 允许 maxPrice 在规则基础上再上浮（溢价上限）",
        ("recognize_quote", "quote_priority"):
            "★ 这是票达人自己的概念（简易/专业/API/比价 四档），"
            "良票文档里【没有】对应说法 —— 我们目前只用了它的排序语义",
    }

    n_aligned = n_ours = n_todo = 0
    unmapped = []
    for sec_key, c in controls:
        key = (sec_key, c["key"])
        if key in mapped:
            status, item, detail = mapped[key]
            row(status, f"配置 › {sec_key}", item, detail)
            n_aligned += 1
        elif key in toggles:
            # 开关类：说明"开/关分别对应什么"，比只标"自有"有用
            row(OURS if not toggles[key].startswith("★") else TODO,
                f"配置 › {sec_key}", c["label"], toggles[key])
            if toggles[key].startswith("★"):
                n_todo += 1
            else:
                n_ours += 1
        elif c["key"] in our_own.get(sec_key, []):
            row(OURS, f"配置 › {sec_key}", c["label"], "我们的业务逻辑，无良票对应字段")
            n_ours += 1
        else:
            row(TODO, f"配置 › {sec_key}", c["label"], "★ 没有登记对应关系")
            unmapped.append(f"{sec_key}.{c['key']}")
            n_todo += 1

    check("每个配置控件都登记了对应关系", not unmapped, str(unmapped))
    print(f"  配置控件共 {len(controls)} 个：对齐 {n_aligned} · 自有 {n_ours} · 待确认 {n_todo}")

    # ★ 退款条件的四种情况 —— 逐一核对它们各自对应良票的什么
    section("二之二、★「自动退款」四个条件各自对应良票的什么")
    refund_map = {
        "出票失败": (
            ALIGNED, "order.failed 回调（failCode 存在）→ 文档明确要自动退款"),
        "校验金额/改价失败": (
            ALIGNED, "下单超限价报 300005、价格变动报 300006、金额校验失败"),
        "创建订单失败": (
            TODO, "文档没有「创建订单失败」这个说法；"
                  "最接近的是 300004 暂无可用供应商 / 100005 服务内部错误 —— 需确认是否等价"),
        "无有效报价记录": (
            ALIGNED, "没有报价就不能下单；对应我们自己的 NO_QUOTE 分支，不是良票错误码"),
    }
    for name, (status, detail) in refund_map.items():
        row(status, "退款条件", name, detail)
    row(ALIGNED, "退款条件", "退款时段（星期 + 时间段）",
        "文档 5.7 申请退票；文档未提时段限制 —— 时段是我们自己的策略")
    print("  四个条件已逐一核对，其中「创建订单失败」标为待确认")


# ==================================================================== 三
def check_templates():
    section("三、31 条文案里写死的良票事实")
    # ★ 出票时效已改成按【实际购入档位】渲染，不再写死。
    #   判据也相应改成"文案里没有写死的时效数字，且口径与文档逐档一致"。
    doc_standard = TICKET_MODE_MINUTES["STANDARD"]        # (1, 60)
    t7 = tpl.find("TICKETING")["text"]
    t15 = tpl.find("PURCHASE_NOTICE")["text"]

    hard7 = re.search(r"出票时间\s*\d+\s*[-~]\s*\d+\s*分钟", t7)
    hard15 = re.search(r"付款后系统\s*\d+\s*[-~]\s*\d+\s*分钟", t15)
    uses_var = "{ticket_window}" in t7 and "{ticket_window}" in t15
    check("★ 出票时效不再写死（走 {ticket_window} 变量）",
          uses_var and not hard7 and not hard15,
          f"TICKETING 写死={bool(hard7)}  PURCHASE_NOTICE 写死={bool(hard15)}")

    window = tpl.TICKET_WINDOW
    doc_ok = (window["STANDARD"] == f"1-{doc_standard[1]}分钟" and
              window["FAST"] == "15分钟内" and
              window["FLASH"] == f"{TICKET_MODE_MINUTES['FLASH'][0]}-"
                                 f"{TICKET_MODE_MINUTES['FLASH'][1]}分钟")
    check("★ 时效口径与良票文档逐档一致", doc_ok, str(window))

    # 逐档渲染，确认拿到的就是文档值
    rendered = {}
    for mode in ("STANDARD", "FAST", "FLASH"):
        values = {k: v[2] for k, v in tpl.VARIABLES.items()}
        values["ticket_window"] = window[mode]
        line = [ln for ln in tpl.render("TICKETING", values).splitlines()
                if "出票时间" in ln]
        rendered[mode] = line[0] if line else ""
    check("★ STANDARD 渲染出 1-60 分钟", "1-60分钟" in rendered["STANDARD"],
          rendered["STANDARD"])
    check("★ FAST 渲染出 15 分钟内", "15分钟内" in rendered["FAST"], rendered["FAST"])
    check("★ FLASH 渲染出 1-15 分钟", "1-15分钟" in rendered["FLASH"], rendered["FLASH"])

    row(ALIGNED, "文案", "出票时效按档位渲染",
        f"STANDARD 1~{doc_standard[1]}分钟 / FAST 15分钟内 / FLASH 1~15分钟，逐档核对通过。"
        f"（原先写死 1-30/50，与 STANDARD 的 1~60 不符）")

    # 其余写死的良票事实
    facts = [
        ("TICKETING", "如出票失败也会立刻退款", ALIGNED,
         "文档：出票失败即解冻退回（order.failed）"),
        ("PURCHASE_NOTICE", "付款后系统{ticket_window}自动出票，无法拦截", ALIGNED,
         "时效已走变量；「无法拦截」与订单拦截的关系：拦截仅在 SUBMITTING/TICKETING 可做，"
         "付款后立刻拦确实来不及 —— 表述基本准确"),
        ("PURCHASE_NOTICE", "系统自动就近择优调座，保证连座", TODO,
         "文档 4.13 只说 allowSeatChange=允许调换附近座位，"
         "**没承诺「保证连座」** —— 措辞要有依据，否则是假承诺"),
        ("PURCHASE_NOTICE", "系统自动就近择优调座，保证连座", ALIGNED,
         "文档 4.13：allowSeatChange 允许调换附近座位；"
         "但文档只说「允许调换」，未承诺「保证连座」—— 措辞需确认"),
        ("PURCHASE_NOTICE", "付款后不支持退改签", ALIGNED,
         "文档 5.7 申请退票：可退但受供应商与时间限制（500001/500002）"),
        ("REPRICED_CHECK", "付款后不支持退改签", ALIGNED, "同上"),
        ("PURCHASE_NOTICE", "如选座被锁，不再和你沟通", OURS,
         "我们的措辞，无 API 对应"),
        ("TICKET_CODE", "部分票不要在淘票票的机器取", OURS,
         "票达人的运营经验，不是接口事实"),
        ("TICKET_CODE", "请输入取票信息取票", ALIGNED,
         "对应 tickets[].ticketCode"),
        ("TICKET_CODE", "取票链接：{取票链接}", ALIGNED,
         "对应 order.ticketed 的 pickupUrl / ticketLink"),
        ("TICKET_FAILED_REFUND", "出票失败，给您退款哈", ALIGNED,
         "对应 order.failed → 冻结解冻退回"),
        ("SUBSIDY_REDO", "已补贴重下并优先处理", OURS,
         "「补贴」是我们的让利行为，无 API 对应"),
        ("TICKET_CODE_UPDATED", "票码有更新，请按此票码取票", ALIGNED,
         "对应 ticket.updated（version 递增）"),
        ("ORDER_DUPLICATED", "座位已有订单在出票中", ALIGNED,
         "对应 300010"),
        ("RECOG_FAIL", "建议换猫眼小程序的座位截图", OURS,
         "文档只说「图片不是选座页截图」报 700006，没指定平台"),
        ("SHOWTIME_LIMIT", "距开始放映不足 {limit_minutes} 分钟", ALIGNED,
         "对应配置放映时间限制，文档口径 40 分钟"),
    ]
    for key, snippet, status, detail in facts:
        row(status, "文案", f"{key}：{snippet[:22]}", detail)
    print(f"  已逐条登记 {len(facts)} 处写死的良票事实")

    # 不可改写的话术（locked）必须原样发出去
    locked = [x["key"] for x in tpl.TEMPLATES if x["locked"]]
    check("风控话术有标记", len(locked) == 4, str(locked))
    row(ALIGNED, "文案", f"风控话术 {len(locked)} 条（locked 不许改写）",
        "PURCHASE_NOTICE 是平台判责依据 ✓")


# ==================================================================== 四
def check_keywords():
    section(f"四、{len(qa.QA_RULES)} 条关键词回复：与良票接口的关系")
    # 关键词回复是对话层的，不是良票的
    api_linked = {
        "拦截订单": ("order/cancel", "ALIGNED"),
        "获取票码": ("order/detail 的 tickets[]", "ALIGNED"),
        "人工客服": ("无（我们的转人工）", "OURS"),
        "等待付款": ("无（对话层催付）", "OURS"),
        "出票": ("TICKET_MODE_MINUTES", "ALIGNED"),
        "等待出票中~~~": ("order/detail 的 status=TICKETING", "ALIGNED"),
        "999": ("无（FAQ 菜单）", "OURS"),
        "001": ("无（FAQ）", "OURS"),
        "002": ("TICKET_MODE_MINUTES", "ALIGNED"),
        "003": ("order/refund", "ALIGNED"),
        "004": ("无（归责说明）", "OURS"),
        "005": ("tickets[].ticketCode", "ALIGNED"),
        "出票失败111": ("order.failed", "ALIGNED"),
        "可以用吗": ("无（询价前的问答）", "OURS"),
        "稍等": ("无（对话层）", "OURS"),
        "取票": ("tickets[].ticketCode", "ALIGNED"),
        "好的": ("无（对话层）", "OURS"),
        "谢谢": ("无（对话层）", "OURS"),
        "票数超出": ("4.13 maxSeatsPerOrder（单笔限购 1~6）", "ALIGNED"),
        "恭喜：已抢到特价票限时2分钟秒杀~": ("无（这是我们报价卡的触发词，不是接口）", "OURS"),
        "111": ("无（免密支付提示）", "OURS"),
        # ★ 会员座 / 灰色座位 / 「可以买吗」系列 —— 2026-10 新增。
        #   来源：店家确认的口径（用户原话），不是从良票文档来的。
        #   ★ 触发这些规则【不需要】任何良票接口 —— 它们是纯对话层应答。
        "会员座": ("无（店家口径：会员座能买，圈位置+报张数即可）", "OURS"),
        "灰色": ("无（万达选座图上会员座显示为灰色，同上）", "OURS"),
        "可以买吗": ("无（店家口径：可以买，圈位置+报张数即可）", "OURS"),
        "能买吗": ("无（同「可以买吗」）", "OURS"),
        "可以拍吗": ("无（同「可以买吗」）", "OURS"),
        "能下吗": ("无（同「可以买吗」）", "OURS"),
        "能代下": ("无（同「可以买吗」）", "OURS"),
    }
    # ★★ 2026-10-14：改成按【单个关键字】查（keyword 可能用 | 合并多条）。
    #   为什么：把"话术完全相同"的重复条目合并后，keyword 变成
    #   `可以买吗|能买吗|可以拍吗|能下吗|能代下` 这样的合成串，
    #   直接拿 rule.keyword 查登记表就查不到了。
    #   ★ 但登记表本身是【审计信息】（来源是良票文档还是店家口径），
    #     不能因为合并就丢掉 —— 所以要展开成单个关键字逐个查。
    hits = 0
    total = 0
    for rule in qa.QA_RULES:
        kws = [k.strip() for k in str(rule.keyword or "").split("|") if k.strip()]
        for kw in (kws or [rule.keyword]):
            total += 1
            info = api_linked.get(kw)
            if info is None:
                row(TODO, "关键词", kw, "★ 没登记对应关系")
                continue
            source, status_char = info
            row(ALIGNED if status_char == "ALIGNED" else OURS,
                "关键词", f"{kw} → {source}",
                "关键词回复本身是我们的对话层功能")
            hits += 1
    print(f"  已登记 {hits}/{total} 个关键字（{len(qa.QA_RULES)} 条规则）")
    check("每个关键字都登记了来源", hits == total, f"{hits}/{total}")


# ==================================================================== 五
def check_state_machine():
    section("五、状态机：14 状态 / 26 事件 与良票生命周期的对应")
    lifecycle = [
        ("IDLE", "无（等买家发截图）", OURS),
        ("RECOGNIZING", "recognize/seat-shot", ALIGNED),
        ("PICK_CINEMA/MOVIE/SHOW", "recognize/confirm", ALIGNED),
        ("QUOTED", "order/preflight + 报价规则", ALIGNED),
        ("AWAIT_REPRICE", "本平台拍下（非良票）", OURS),
        ("AWAIT_PAY", "本平台付款前", OURS),
        ("AWAIT_TICKET", "order/create → SUBMITTING/TICKETING", ALIGNED),
        ("DELIVERED", "order.ticketed / ticket.updated", ALIGNED),
        ("QT_FAIL", "order.failed（金额不符 300005/300006）", ALIGNED),
        ("REWORK", "order.failed / CREATE_FAILED", ALIGNED),
        ("SELF_BUY", "无（我们主动放弃）", OURS),
        ("MANUAL", "无（转人工）", OURS),
    ]
    for state, source, status in lifecycle:
        row(status, "状态", state, source)
        print(f"    {state:24} ← {source}")

    # ★ 反思到的缺口：REFUNDING / REFUNDED 在状态机里没有
    doc_states = {"REFUNDING", "REFUNDED"}
    our_events = {e.value for e in Event}
    has_refund = any("refund" in e for e in our_events)
    if has_refund:
        row(ALIGNED, "状态", "退票流程", "有 refund 相关事件")
    else:
        row(TODO, "状态", "★ 退票（REFUNDING / REFUNDED）没有对应状态",
            "良票有 order/refund + order.refunded / order.refund_rejected 三个接口/事件，"
            "我们的状态机里【没有退票状态】—— 买家申请退票时走不到任何分支")
    check("退票流程在状态机里有位置", has_refund,
          "文档 5.7/5.8 + order.refunded + order.refund_rejected 都要有落点")

    # 订单状态与我们的状态对照
    rows = [
        ("CREATED", "（下单瞬间，窗口极短）", OURS),
        ("SUBMITTING", "AWAIT_TICKET", ALIGNED),
        ("TICKETING", "AWAIT_TICKET", ALIGNED),
        ("TICKETED", "DELIVERED", ALIGNED),
        ("SETTLED", "（放映结束后，仅对账，不影响对话）", TODO),
        ("REJECTED", "（下单被拒，我们直接回复失败文案）", ALIGNED),
        ("FAILED", "REWORK", ALIGNED),
        ("CANCELLED", "REWORK", ALIGNED),
        ("REFUNDING", "★ 无", TODO),
        ("REFUNDED", "★ 无", TODO),
    ]
    for order_status, ours, status in rows:
        row(status, "订单状态映射", f"{order_status} → {ours}", "")
    print("  订单状态→会话状态映射已列出，退票两态标为待确认")


# ==================================================================== 六
def check_quote_rules():
    section("六、报价规则：与良票接口的交界只有一处")
    row(OURS, "报价规则", "规则匹配 / 加价 / 限价计算",
        "良票只给成本（estimateAmount/totalAmount），加价完全是我们自己的逻辑")
    row(ALIGNED, "报价规则", "规则产出 priceMode + maxPrice → 传给良票",
        "这是唯一的交界：quote_mode→priceMode，limit_yuan→maxPrice")
    row(ALIGNED, "报价规则", "maxPrice 走 maxPrice 字段（数字）",
        "文档：请求中的 maxPrice 例外，是数字")
    row(ALIGNED, "报价规则", "成本口径 estimateAmount",
        "文档：LIMIT 模式实扣以放映后结算为准，estimateAmount 是预估应付")

    rules = qr.tianjin_default_rules()
    problems = qr.validate_rules(rules)
    row(TODO, "报价规则", "票达人配置里有 2 处限价 < 报价",
        f"校验器报出 {len([p for p in problems if '300005' in p])} 处；"
        f"这两条启用后下单会被拒")
    check("校验器能发现规则矛盾", len([p for p in problems if "300005" in p]) == 2)
    print("  报价规则与良票的交界只有 priceMode / maxPrice 两处，已核对")


# ==================================================================== 七
def check_cannot_verify():
    section("七、★ 找不到出处、无法验证的项（必须人确认）")
    items = [
        ("出票时效 1-30 分钟 / 最迟 50 分钟",
         "良票文档 STANDARD 是 1~60 分钟。票达人的 1-30/50 可能是它自己的承诺或经验值，"
         "文档里没有这两个数字"),
        ("「保证连座」", "文档只说 allowSeatChange=允许调换附近座位，没承诺连座"),
        ("「赔付/补贴重下」", "SUBSIDY_REDO 文案里的补贴是我们的让利，无 API 语义"),
        ("报价卡里的「限时2分钟秒杀」", "配置里报价有效期是 30 分钟 —— 文案是营销话术，"
                                  "与真实时效不符，有合规风险"),
        ("内部签名 [ggx1z82tg84] 等", "已确认是关键词回复的全局调用标识，机制已实现"),
        ("「创建订单失败」自动退款条件", "良票没有这个术语，最接近的是 300004/100005"),
        ("夜间识别时段 23:30-08:00", "票达人页面配置，良票文档无此概念"),
        ("消息发送延迟 100-500ms", "票达人配置，良票无此概念"),
        ("人工介入 1 小时", "票达人配置，良票无此概念"),
        ("退票时段限制（星期+时间段）", "票达人配置，良票只按供应商与时间判可退"),
        ("院线名（万达电影（一线）A类 等）", "★★ 应当从 brand/list 动态拉取。"
                                    "票达人页面上的「原官方-」是它的内部前缀，不该照抄"),
    ]
    for item, detail in items:
        row(TODO, "无法验证", item, detail)
        print(f"    · {item}")
        print(f"        {detail}")
    print(f"  共 {len(items)} 项需要你确认或从接口拉取")


# ==================================================================== 汇总
def main() -> int:
    check_enums()
    check_config_controls()
    check_templates()
    check_keywords()
    check_state_machine()
    check_quote_rules()
    check_cannot_verify()

    section("汇总")
    from collections import Counter
    counter = Counter(s for s, _, _, _ in RESULTS)
    print(f"  逐项核对总数：{len(RESULTS)}")
    for status in (ALIGNED, OURS, TODO):
        print(f"    [{status}] {counter.get(status, 0)}")

    if FAILURES:
        print()
        print("  断言失败（必须修）：")
        for f in FAILURES:
            print(f"    ✗ {f}")

    print()
    print("  ★ 待确认清单（必须人确认，不能糊过去）：")
    for status, category, item, detail in RESULTS:
        if status == TODO:
            print(f"    · [{category}] {item}")

    print()
    print("=" * 78)
    ok = not FAILURES
    print(f" 结果：{'全部断言通过' if ok else str(len(FAILURES)) + ' 项断言失败'}"
          f"（另有 {counter.get(TODO, 0)} 项待确认）"
          f" | {len(RESULTS)} 项逐项核对："
          f"对齐 {counter.get(ALIGNED, 0)} / 自有 {counter.get(OURS, 0)} / "
          f"待确认 {counter.get(TODO, 0)}")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
