"""
_check_quote_rules.py —— 报价规则引擎自检

★ 重点验的是"规则匹配对不对"和"传给良票 API 的参数对不对"。
  后者尤其重要：priceMode 和 maxPrice 传错，
  轻则询价与下单对不上账，重则被 300005 拒单或按错误模式结算。
"""

import sys
from datetime import time
from pathlib import Path

# ★ Windows 上 stdout 默认 GBK，打不出 ↔ ★ ✓ − 这类字符会抛
#   UnicodeEncodeError。★ 必须放在【任何 print 之前】——
#   这些测试是模块级脚本，从第一行就开始跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.quote_rules import (
    KNOWN_BRANDS, TIER_NAMES_FROM_PIAODAREN,
    ADD, ADJUST_NONE, ADJUST_ROUND_JIAO,
                                ADJUST_ROUND_YUAN, ADJUST_ROUND_YUAN_MINUS_01,
                                DYNAMIC_DISCOUNT, DYNAMIC_DISCOUNT_ADD, PRICE_MODE_OF_QUOTE,
                                QUOTE_ESTIMATE, QUOTE_FIXED, QuoteRule,
                                QuoteRuleEngine, _in_time_range, _adjust,
                                _round_half_up,
                                tianjin_default_rules)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def section(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


# ==================================================================== 一
section("一、规则匹配：权重优先")
rules = [
    QuoteRule(name="低权重", weight=1, brands=["A"], markup_yuan=1),
    QuoteRule(name="高权重", weight=9, brands=["A"], markup_yuan=9),
]
eng = QuoteRuleEngine(rules)
r = eng.find_rule(now=time(12, 0), brand="A")
check("权重高的先命中", r and r.name == "高权重", r.name if r else "无")

section("二、同权重时取【最新添加】的")
rules = [
    QuoteRule(name="旧的", weight=5, brands=["A"], markup_yuan=1, created_seq=1),
    QuoteRule(name="新的", weight=5, brands=["A"], markup_yuan=2, created_seq=2),
]
r = QuoteRuleEngine(rules).find_rule(now=time(12, 0), brand="A")
check("同权重取最新", r and r.name == "新的", r.name if r else "无")

section("三、命中条件：院线 / 排除影院 / 时段 / 猫淘价区间")
base = dict(weight=5, quote_mode=QUOTE_FIXED, markup_yuan=1)

r = QuoteRule(name="限A", brands=["A"], **base)
check("院线匹配命中", r.matches(now=time(12, 0), brand="A"))
check("院线不匹配不命中", not r.matches(now=time(12, 0), brand="B"))
r0 = QuoteRule(name="不限院线", brands=[], **base)
check("院线列表为空 = 不限", r0.matches(now=time(12, 0), brand="随便"))

r = QuoteRule(name="排除", excluded_cinemas=["某某影城"], **base)
check("排除影院能排除", not r.matches(now=time(12, 0), cinema="某某影城"))
check("不在排除名单则命中", r.matches(now=time(12, 0), cinema="别的影城"))

r = QuoteRule(name="猫淘价", price_left_yuan=0, price_right_yuan=999, **base)
check("原价在区间内命中", r.matches(now=time(12, 0), original_price_yuan=50))
check("原价等于左边界命中（左闭）", r.matches(now=time(12, 0), original_price_yuan=0))
check("原价等于右边界不命中（右开）",
      not r.matches(now=time(12, 0), original_price_yuan=999))
check("不传原价时不按区间筛", r.matches(now=time(12, 0), original_price_yuan=None))

section("四、★ 生效时间段必须支持跨零点")
check("普通区间 09:00-18:00 命中 12:00", _in_time_range(time(12, 0), "09:00:00", "18:00:00"))
check("普通区间 09:00-18:00 不命中 20:00",
      not _in_time_range(time(20, 0), "09:00:00", "18:00:00"))
check("★ 跨零点 23:00-08:00 命中 02:00", _in_time_range(time(2, 0), "23:00:00", "08:00:00"))
check("★ 跨零点 23:00-08:00 命中 23:30", _in_time_range(time(23, 30), "23:00:00", "08:00:00"))
check("★ 跨零点 23:00-08:00 不命中 12:00",
      not _in_time_range(time(12, 0), "23:00:00", "08:00:00"))
check("全天 00:00-23:59:59 命中任意时刻",
      all(_in_time_range(t, "00:00:00", "23:59:59")
          for t in (time(0, 0), time(12, 0), time(23, 59))))

section("五、★ 报价出票模式 → 良票 API 的 priceMode")
check("渠道一口价 → FIXED", PRICE_MODE_OF_QUOTE[QUOTE_FIXED] == "FIXED")
check("渠道预估价 → LIMIT", PRICE_MODE_OF_QUOTE[QUOTE_ESTIMATE] == "LIMIT")

eng = QuoteRuleEngine([QuoteRule(name="一口价", weight=9, quote_mode=QUOTE_FIXED,
                                 method=ADD, markup_yuan=2, capped=False)])
r = eng.quote(estimate_total_fen=10000, seat_count=2, now=time(12, 0))
check("一口价单产出 priceMode=FIXED", r.price_mode == "FIXED", r.price_mode)
check("不限价时 as_api_params 不含 maxPrice",
      "maxPrice" not in r.as_api_params(), str(r.as_api_params()))

eng = QuoteRuleEngine([QuoteRule(name="预估价", weight=9, quote_mode=QUOTE_ESTIMATE,
                                 method=ADD, markup_yuan=2, capped=True,
                                 limit_yuan=0.5)])
r = eng.quote(estimate_total_fen=10000, seat_count=2, now=time(12, 0))
check("预估价单产出 priceMode=LIMIT", r.price_mode == "LIMIT", r.price_mode)
check("限价时 as_api_params 含 maxPrice", "maxPrice" in r.as_api_params(),
      str(r.as_api_params()))
# 文档：请求中的 maxPrice 是【数字】不是字符串
check("★ maxPrice 是数字不是字符串",
      isinstance(r.as_api_params()["maxPrice"], int),
      type(r.as_api_params()["maxPrice"]).__name__)

section("六、加价与限价的算法（★ 两个 +N 含义不同）")
# 成本 100 元/张 × 2 张
#   报价金额 = 成本 + 2 元/张   → 报价 102/张，204 总   【报给买家】
#   单张限价 = 成本 + 2.5 元/张 → maxPrice 205 元        【给上游的上限】
#
# ★ 为什么限价要给到比报价还高：
#   限价是「我们最多愿意付给上游多少」。若限价低于报价，
#   上游出价空间不足 → 撮不到货 → 或直接被 300005 拒。
#   这是配置规则时的硬约束：单张限价 ≥ 报价金额。
eng = QuoteRuleEngine([QuoteRule(name="t", weight=1, method=ADD, markup_yuan=2,
                                 adjust=ADJUST_NONE, quote_mode=QUOTE_ESTIMATE,
                                 capped=True, limit_yuan=2.5)])
r = eng.quote(estimate_total_fen=20000, seat_count=2, now=time(12, 0))
check("报价金额：成本 100 + 2 = 102/张", r.unit_price_fen == 10200, f"{r.unit_price_fen}")
check("整单 = 单价 × 座位数", r.total_price_fen == 20400, f"{r.total_price_fen}")
check("单张限价：maxPrice = (100 + 2.5) × 2 = 205 元", r.max_price_fen == 20500,
      f"{r.max_price_fen}")
check("★ 限价 ≥ 报价（硬约束，否则下单会被 300005 拒）",
      r.max_price_fen >= r.total_price_fen,
      f"报价 {r.total_price_fen} vs 限价 {r.max_price_fen}")

section("六之二、★ 毛利 = 报价 − 成本")
check("单价毛利 2 元", r.margin_unit_fen == 200, f"{r.margin_unit_fen}")
check("整单毛利 4 元", r.margin_total_fen == 400, f"{r.margin_total_fen}")
check("summary 能读", "毛利" in r.summary(), r.summary())

section("六之三、★ 成本口径：优先 estimateAmount")
eng2 = QuoteRuleEngine([QuoteRule(name="t", weight=1, method=ADD, markup_yuan=0,
                                  adjust=ADJUST_NONE, capped=False)])
r2 = eng2.quote(estimate_total_fen=3310, total_amount_fen=3510, seat_count=1,
                now=time(12, 0))
check("★ 两个都给时用 estimateAmount（结算成本口径）",
      r2.cost_total_fen == 3310 and r2.cost_basis == "estimateAmount",
      f"{r2.cost_total_fen} / {r2.cost_basis}")
r3 = eng2.quote(total_amount_fen=3510, seat_count=1, now=time(12, 0))
check("只给 totalAmount 时也能算",
      r3.cost_total_fen == 3510 and r3.cost_basis == "totalAmount", r3.cost_basis)
try:
    eng2.quote(seat_count=1, now=time(12, 0))
    check("两个成本都不给应报错", False)
except ValueError:
    check("两个成本都不给时报错", True)
try:
    eng2.quote(estimate_total_fen=-1, seat_count=1, now=time(12, 0))
    check("负成本应报错", False)
except ValueError:
    check("负成本报错", True)

section("七、★ 报价不得低于成本（少赚可以，亏本不行）")
eng = QuoteRuleEngine([QuoteRule(name="负加价", weight=1, method=ADD,
                                 markup_yuan=-50, adjust=ADJUST_NONE)])
r = eng.quote(estimate_total_fen=10000, seat_count=1, now=time(12, 0))
check("★ 加价后低于成本 → 回退到成本价", r.unit_price_fen == 10000,
      f"{r.unit_price_fen}")

section("八、报价调整类型")
check("不调整", _adjust(3456, ADJUST_NONE) == 3456)
check("到角：3456 → 3460", _adjust(3456, ADJUST_ROUND_JIAO) == 3460, str(_adjust(3456, ADJUST_ROUND_JIAO)))
check("到元：3456 → 3500（四舍五入）", _adjust(3456, ADJUST_ROUND_YUAN) == 3500,
      str(_adjust(3456, ADJUST_ROUND_YUAN)))
check("到元：3449 → 3400", _adjust(3449, ADJUST_ROUND_YUAN) == 3400,
      str(_adjust(3449, ADJUST_ROUND_YUAN)))
# 到元-0.1：3490 → 3500-10 = 3490;  3456 → 3500-10 = 3490
check("到元-0.1：3456 → 3490",
      _adjust(3456, ADJUST_ROUND_YUAN_MINUS_01) == 3490,
      str(_adjust(3456, ADJUST_ROUND_YUAN_MINUS_01)))
check("到元-0.1：3490 → 3490（本来就是 34.90）",
      _adjust(3490, ADJUST_ROUND_YUAN_MINUS_01) == 3490,
      str(_adjust(3490, ADJUST_ROUND_YUAN_MINUS_01)))

section("九、★ 限价低于报价 = 规则自相矛盾，必须提前报出来")
eng = QuoteRuleEngine([QuoteRule(name="矛盾", weight=1, method=ADD, markup_yuan=10,
                                 limit_yuan=0, capped=True, adjust=ADJUST_NONE)])
r = eng.quote(estimate_total_fen=1000, seat_count=1, now=time(12, 0))
check("单价 10+10=20 元不低于成本", r.unit_price_fen == 2000)
check("maxPrice = 10 元（成本+0）", r.max_price_fen == 1000)
check("★ 报出「下单会被 300005 拒」",
      any("300005" in n for n in r.notes), str(r.notes))

section("十、没有规则命中时的降级")
eng = QuoteRuleEngine([QuoteRule(name="只服务A", weight=1, brands=["A"],
                                 method=ADD, markup_yuan=5)])
r = eng.quote(estimate_total_fen=5000, seat_count=1, now=time(12, 0), brand="B")
check("★ 不抛异常（宁可少赚也不能报不出价）", r is not None)
check("按成本报，不加价", r.unit_price_fen == 5000, f"{r.unit_price_fen}")
check("提示要补兜底规则", any("兜底" in n for n in r.notes), str(r.notes))

section("十一、座位数校验")
eng = QuoteRuleEngine([QuoteRule(name="t", weight=1)])
try:
    eng.quote(estimate_total_fen=100, seat_count=0, now=time(12, 0))
    check("座位数 0 应报错", False)
except ValueError:
    check("座位数 0 报错", True)

section("十二、★ 票达人那 6 条规则能复现")
rules = tianjin_default_rules()
check("共 6 条", len(rules) == 6, f"{len(rules)} 条")
check("只有兜底那条是启用的",
      [r.name for r in rules if r.enabled] == ["兜底全院线预估"],
      str([r.name for r in rules if r.enabled]))
check("权重是 9/8/7/6/5/4",
      [r.weight for r in rules] == [9, 8, 7, 6, 5, 4], str([r.weight for r in rules]))
check("三条一口价 + 三条预估价",
      sum(1 for r in rules if r.quote_mode == QUOTE_FIXED) == 3 and
      sum(1 for r in rules if r.quote_mode == QUOTE_ESTIMATE) == 3)
check("★ 兜底只用良票真实品牌名（不是票达人的分级名）",
      # ★ 这条断言改过：早先要求"覆盖 24 条院线"，
      #   那 24 条是票达人的【分级名】，良票 brand/list 里根本没有
      #   （只返回 "万达影城"/"CGV影城" 这种简单品牌名）。
      #   照抄分级名的后果：场次返回的 brandName 永远匹配不上规则，
      #   规则形同虚设。所以现在断言的是"用的名字确实是良票的"。
      all(b in KNOWN_BRANDS for b in
          next(r for r in rules if r.name == "兜底全院线预估").brands),
      str(next(r for r in rules if r.name == "兜底全院线预估").brands))
check("★ 票达人的分级名被单独留在一边当参考，没混进生产规则",
      not any(b in TIER_NAMES_FROM_PIAODAREN
              for r in rules for b in r.brands
              if r.name == "兜底全院线预估"),
      "兜底规则里不含分级名")

section("十三、★ 规则配置校验（在下单前拦住自相矛盾）")
from domain.quote_rules import validate_rule, validate_rules, QUOTE_SCENE


good = QuoteRule(name="正常", weight=1, brands=["A"], quote_mode=QUOTE_ESTIMATE,
                 method=ADD, markup_yuan=2, limit_yuan=2.5, capped=True,
                 adjust=ADJUST_ROUND_YUAN_MINUS_01)
check("合法规则无告警", not validate_rule(good), str(validate_rule(good)))

bad1 = QuoteRule(name="限价低于报价", weight=1, brands=["A"], method=ADD,
                 markup_yuan=2, limit_yuan=0.5, capped=True)
p = validate_rule(bad1)
check("★ 限价 < 报价 被抓到", any("300005" in x for x in p), str(p))

bad2 = QuoteRule(name="限价刚好等于报价", weight=1, brands=["A"], method=ADD,
                 markup_yuan=2, limit_yuan=2, capped=True)
check("限价 = 报价 不算错（≥ 即可）", not validate_rule(bad2), str(validate_rule(bad2)))

bad3 = QuoteRule(name="不限价", weight=1, brands=["A"], method=ADD,
                 markup_yuan=5, limit_yuan=0, capped=False)
check("不启用限价时不校验这一条", not validate_rule(bad3), str(validate_rule(bad3)))

check("场景预估价被拒", any("维护中" in x for x in
      validate_rule(QuoteRule(name="场景", weight=1, brands=["A"],
                              quote_mode=QUOTE_SCENE, method=ADD))))
check("空院线被抓到", any("院线" in x for x in
      validate_rule(QuoteRule(name="空", weight=1, brands=[], method=ADD))))
check("空名称被抓到", any("名称" in x for x in
      validate_rule(QuoteRule(name="", weight=1, brands=["A"], method=ADD))))
check("猫淘价区间为空被抓到", any("区间" in x for x in
      validate_rule(QuoteRule(name="区间", weight=1, brands=["A"], method=ADD,
                              price_left_yuan=999, price_right_yuan=0))))

# 整组校验
allbad = validate_rules([bad1, bad1])
check("整组校验能报出重复名", any("重复" in x for x in allbad), str(allbad)[:80])
check("整组校验能报出无规则启用", any("没有" in x for x in
      validate_rules([QuoteRule(name="停用的", weight=1, enabled=False, brands=["A"])])))

# ★ 票达人那 6 条：校验器应当【报出矛盾】，不是报"无问题"
#
#   两处真矛盾：
#     杂牌院线预估   单张限价 +0  低于  报价金额 +1
#     连锁B预估限价  单张限价 +0  低于  报价金额 +2
#   按文档「实际应付超过 maxPrice 时下单失败(300005)」，
#   这两条只要启用就会下单失败 —— 所以它们正好都是"停用"状态。
#   校验器能看出这一点，说明约束抓得准。
tj = tianjin_default_rules()
tj_problems = validate_rules(tj)
conflict_names = [p for p in tj_problems if "300005" in p]
check("★ 校验器报出票达人配置里的 2 处限价矛盾",
      len(conflict_names) == 2, f"{len(conflict_names)} 处")
check("★ 矛盾的是「杂牌院线预估」和「连锁B预估限价」",
      all(any(n in p for p in conflict_names)
          for n in ("杂牌院线预估", "连锁B预估限价")),
      str([p.split("]")[0] + "]" for p in conflict_names]))
check("★ 这两条恰好都是停用状态（所以没炸）",
      all(not r.enabled for r in tj if r.name in ("杂牌院线预估", "连锁B预估限价")))
# 启用的那条不该有矛盾
enabled_problems = [p for p in tj_problems
                    if p.startswith("[兜底全院线预估]") and "300005" in p]
check("★ 唯一启用的「兜底全院线预估」没有矛盾", not enabled_problems,
      str(enabled_problems))

section("★ 与 V5 的真实报价口径同步（2026-10）")
# ★ 来源：V5 生产库 business-settings.sqlite3 的 wanda_pricing，
#   以及 quotes.sqlite3 里的真实报价记录（两者已验证吻合）。
#   真实记录：成本 4500/张 → 售价 4600/张，4 张共 18400。
wanda_rules = [r for r in tj if "万达" in r.name]
check("万达规则都按 V5 配成「每张固定加 1 元」",
      bool(wanda_rules) and all(r.markup_yuan == 1.0 for r in wanda_rules),
      str([(r.name, r.markup_yuan) for r in wanda_rules]))
check("★ 万达规则取整到 1 角（rounding_fen=10，同 V5）",
      bool(wanda_rules) and all(r.rounding_fen == 10 for r in wanda_rules),
      str([(r.name, r.rounding_fen) for r in wanda_rules]))
check("★ 万达不加价限（V5 的万达 tier adjustment_bps 全为 0）",
      bool(wanda_rules) and all(not r.capped for r in wanda_rules),
      str([(r.name, r.capped) for r in wanda_rules]))

# ★★ 端到端验算：拿线上真实数字跑一遍我们的引擎，必须得出 4600 / 18400
_eng = QuoteRuleEngine([QuoteRule(
    name="万达核对", weight=9, enabled=True, brands=["万达影城"],
    quote_mode=QUOTE_FIXED, method=ADD, markup_yuan=1.0, capped=False,
    rounding_fen=10, adjust=ADJUST_NONE)])
_q = _eng.quote(seat_count=4, now=time(12, 0), estimate_total_fen=4500 * 4,
                original_total_fen=4900 * 4, brand="万达影城")
check("★★ 线上真实数字：成本4500×4 → 单张4600（与 V5 记录一致）",
      _q.unit_price_fen == 4600, f"{_q.unit_price_fen}")
check("★★ 线上真实数字：总价 18400（与 V5 记录一致）",
      _q.total_price_fen == 18400, f"{_q.total_price_fen}")

# 取整：V5 是四舍五入到角，不是向上取整
check("★ 取整是四舍五入到角（4313 → 4310，不是 4320）",
      _round_half_up(4313, 1, 10) == 4310, str(_round_half_up(4313, 1, 10)))
check("取整 4667 → 4670", _round_half_up(4667, 1, 10) == 4670)
check("取整 4600 → 4600（整数倍不多进一格）",
      _round_half_up(4600, 1, 10) == 4600)

# 上界夹逼：售价不许超过官方原价
_eng2 = QuoteRuleEngine([QuoteRule(
    name="夹逼核对", weight=9, enabled=True, brands=["万达影城"],
    quote_mode=QUOTE_FIXED, method=ADD, markup_yuan=1.0, capped=False,
    rounding_fen=10, adjust=ADJUST_NONE)])
_q2 = _eng2.quote(seat_count=1, now=time(12, 0), estimate_total_fen=5450,
                  original_total_fen=5500, brand="万达影城")
check("★★ 售价超过官方原价 → 压回原价（V5 的 _bounded）",
      _q2.unit_price_fen == 5500, str(_q2.unit_price_fen))
check("压回原价时留了说明",
      any("原价" in n for n in _q2.notes), str(_q2.notes))

# 成本高于原价 = 规则冲突，按成本报并说明
_q3 = _eng2.quote(seat_count=1, now=time(12, 0), estimate_total_fen=5600,
                  original_total_fen=5500, brand="万达影城")
check("★ 成本高于原价 → 按成本报并提示规则冲突",
      _q3.unit_price_fen == 5600 and any("规则冲突" in n for n in _q3.notes),
      f"{_q3.unit_price_fen} {_q3.notes}")

section("结果")
passed, total = sum(RESULTS), len(RESULTS)
print(f"\n{passed}/{total} 项通过")
print("=" * 74)
return_code = 0 if passed == total else 1
sys.exit(return_code)
