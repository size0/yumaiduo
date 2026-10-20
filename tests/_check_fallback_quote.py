"""兜底报价检查（enabled / mode / price_capped）。

★★ 为什么必须断：
   这三个设置直接决定【没规则命中时报多少钱】——
   报错价是要亏钱的，而且"不启用限价"意味着 maxPrice=None（钱上不封顶）。
   ★ 最关键的一条：**默认必须与接线前完全一致**（预估 + 限价）。
     我接线前的行为是写死的 price_mode="LIMIT" + 有兜底限价。
"""
import sys
from datetime import time as T
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import settings                                                         # noqa: E402
from application.ticket_flow import (_fallback_quote_enabled,           # noqa: E402
                                     _fallback_quote_kwargs)
from domain.quote_rules import (QUOTE_ESTIMATE, QUOTE_FIXED,            # noqa: E402
                                QuoteRuleEngine)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


# ★ 空规则集 ⇒ 必然"没规则命中"，正好是兜底那条路
ENGINE = QuoteRuleEngine([])


def quote(**kw):
    base = dict(seat_count=2, now=T(12, 0), estimate_total_fen=10000,
                original_total_fen=12000)
    base.update(kw)
    return ENGINE.quote(**base)


print("=" * 76)
print("一、★★★ 默认必须与接线前一致")
print("=" * 76)
d = settings.Settings()
check("默认 enabled=True", d.fallback_quote.enabled is True)
check("默认 mode=预估限价", d.fallback_quote.mode == "预估限价",
      d.fallback_quote.mode)
check("默认 price_capped=启用限价（布尔 True）",
      d.fallback_quote.price_capped is True,
      repr(d.fallback_quote.price_capped))
# ★★ 这里有个坑我踩了：price_capped 是【布尔】不是显示文本。
#   前端 radio 的 values=[True, False] ⇒ 回来的是真布尔值。
#   我第一版写成 `str(x) != "不启用限价"` ⇒ True→"True"、False→"False"，
#   两个都不等于"不启用限价" ⇒ **永远得到 True**（不启用限价被当成启用）。
#   ⇒ 这组断言用【布尔】验证，并且必须验证 False 真的能传下去。
c_uncap = settings.Settings()
c_uncap.fallback_quote.price_capped = False
check("★★ 布尔 False ⇒ capped=False（不是永远 True）",
      _fallback_quote_kwargs(c_uncap)["fallback_capped"] is False,
      str(_fallback_quote_kwargs(c_uncap)))
c_uncap2 = settings.Settings()
c_uncap2.fallback_quote.price_capped = "不启用限价"
check("★ 字符串形态也认（兼容老数据/手写配置）",
      _fallback_quote_kwargs(c_uncap2)["fallback_capped"] is False)
check("★ 默认 kwargs = 预估 + 限价",
      _fallback_quote_kwargs(d) == {"fallback_mode": QUOTE_ESTIMATE,
                                    "fallback_capped": True,
                                    # ★★ 2026-10-14 接线「出票溢价」时多出来的键：
                                    #   兜底时"成本上增加多少元/张"。
                                    #   未启用「出票溢价」⇒ 用原来的 3（行为不变）。
                                    "fallback_limit_yuan": 3},
      str(_fallback_quote_kwargs(d)))
# ★★ 2026-10-14：「出票溢价」替掉写死的 3 —— 验它真的能改数
from application.ticket_flow import _fallback_quote_kwargs as _fbk   # noqa: E402
_c5 = settings.Settings()
_c5.ticket_premium.enabled = True
_c5.ticket_premium.cap_yuan = 5.0
check("★★ 启用「出票溢价」+ 5 元 ⇒ 兜底按 5 元/张",
      _fbk(_c5)["fallback_limit_yuan"] == 5.0,
      str(_fbk(_c5)["fallback_limit_yuan"]))
_c0 = settings.Settings()
_c0.ticket_premium.enabled = True
_c0.ticket_premium.cap_yuan = 0.0
check("★★ 启用 + 0 元 ⇒ 兜底不加价（= 成本就是限价）",
      _fbk(_c0)["fallback_limit_yuan"] == 0.0)
_coff = settings.Settings()
_coff.ticket_premium.enabled = False
_coff.ticket_premium.cap_yuan = 99.0
check("★★★ 未启用 ⇒ 仍然是 3（不是 0、也不看 cap_yuan）"
      " —— 不让部署悄悄改钱",
      _fbk(_coff)["fallback_limit_yuan"] == 3,
      str(_fbk(_coff)["fallback_limit_yuan"]))
# ★★ 不传参数 ⇒ 与接线前完全一致（接线前写死 LIMIT + 有 maxPrice）
q0 = quote()
check("★★ 不传参数的 price_mode = LIMIT（= 接线前写死的值）",
      q0.price_mode == "LIMIT", q0.price_mode)
check("★★ 不传参数有兜底限价（不是 None）", q0.max_price_fen is not None,
      str(q0.max_price_fen))
# ★ 传默认配置 ⇒ 和不传一模一样
q1 = quote(**_fallback_quote_kwargs(d))
check("★★ 传默认配置的结果与不传【完全相同】",
      (q1.price_mode, q1.max_price_fen) == (q0.price_mode, q0.max_price_fen))

print()
print("=" * 76)
print("二、模式：预估限价 → LIMIT；一口价 → FIXED")
print("=" * 76)
q2 = quote(fallback_mode=QUOTE_FIXED)
check("★★ 一口价 ⇒ price_mode=FIXED（报价即成交价）",
      q2.price_mode == "FIXED", q2.price_mode)
q3 = quote(fallback_mode=QUOTE_ESTIMATE)
check("★★ 预估限价 ⇒ price_mode=LIMIT（报价是上限）",
      q3.price_mode == "LIMIT", q3.price_mode)
check("★ quote_mode 也跟着变（能追溯当时用的哪种模式）",
      q2.quote_mode == QUOTE_FIXED and q3.quote_mode == QUOTE_ESTIMATE)
# ★ 配置坏掉时要保守：只有明确"一口价"才 FIXED
c_bad = settings.Settings()
c_bad.fallback_quote.mode = "胡说八道"
check("★★ 模式配坏 ⇒ 保守按预估价（不轻易承诺成交价）",
      _fallback_quote_kwargs(c_bad)["fallback_mode"] == QUOTE_ESTIMATE)

print()
print("=" * 76)
print("三、限价：不启用 ⇒ maxPrice=None（钱上不封顶）")
print("=" * 76)
q4 = quote(fallback_capped=True)
check("★ 启用限价 ⇒ maxPrice 有值", q4.max_price_fen is not None,
      str(q4.max_price_fen))
q5 = quote(fallback_capped=False)
check("★★ 不启用限价 ⇒ maxPrice=None", q5.max_price_fen is None,
      str(q5.max_price_fen))
# ★★ 必须留痕 —— "钱上不封顶"是会出事的配置，排查时要能看出是故意的
check("★★ 不启用限价会【留痕】（排查时要知道是故意不封顶）",
      any("未启用" in n for n in q5.notes), str(q5.notes)[:80])
check("★ 启用了也留痕（写明限价怎么算出来的）",
      any("兜底限价" in n for n in q4.notes))

print()
print("=" * 76)
print("四、开关：关掉 ⇒ 不报价（这是唯一「关掉会改变结果」的）")
print("=" * 76)
c_off = settings.Settings()
c_off.fallback_quote.enabled = False
check("★★ enabled=False 时 _fallback_quote_enabled 返回 False",
      _fallback_quote_enabled(c_off) is False)
check("★ 默认（开）返回 True", _fallback_quote_enabled(d) is True)
# ★ 闸门在共同入口 _build_quote（万达/良票两条路都过它）
FLOW = (ROOT / "application" / "ticket_flow.py").read_text(encoding="utf-8")
check("★★ 闸门在 _build_quote（两条渠道都管住）",
      "_fallback_quote_enabled()" in FLOW)
check("★★ 关掉时明确【不报价】并说明原因（不是静默少赚）",
      "没有规则命中时【不报价】" in FLOW)
check("★ 关掉时给买家一句解释（不是甩错误）",
      "暂时报不了价" in FLOW)

print()
print("=" * 76)
print("五、★ 领域层不许读配置（分层）")
print("=" * 76)
QR = (ROOT / "domain" / "quote_rules.py").read_text(encoding="utf-8")
check("★★ domain/quote_rules.py 没有 import settings",
      "import settings" not in QR)
check("★★ 用参数传进来（fallback_mode / fallback_capped）",
      "fallback_mode: str" in QR and "fallback_capped: bool" in QR)
check("★★ 两个参数都有默认值 ⇒ 不传的调用方行为不变",
      "fallback_mode: str = QUOTE_ESTIMATE" in QR
      and "fallback_capped: bool = True" in QR)
# ★ 两处调用点都传了
check("★★ 万达/良票两处调用点都传了", FLOW.count("_fallback_quote_kwargs()") == 2,
      str(FLOW.count("_fallback_quote_kwargs()")))

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
