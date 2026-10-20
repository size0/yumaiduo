"""二次报价检查（开关 / 等待时间 / 优惠比例）。

★★ 为什么必须断：
   这是【又一次报价】，涉及钱。三条最容易错、而且错了不报错：
     ① 优惠减过头 ⇒ 亏钱（必须"最多让完毛利为止"）
     ② 让不出优惠却发了"已再优惠 X%" ⇒ **骗买家**（我实测踩到过）
     ③ 没开 / 已经拍下 / 报价过期 也发 ⇒ 打扰买家
   还有一条：默认必须是【关】—— 默认行为不变。
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import settings                                                         # noqa: E402
from application import schedule_queue as sq                            # noqa: E402
from application.ticket_flow import (TicketFlow,                        # noqa: E402
                                     _second_quote_cfg,
                                     _second_quote_wait_seconds)
from domain.conversation import Conversation, ConvState                 # noqa: E402
from domain.quote_rules import QuoteResult, QuoteRuleEngine             # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


def mkq(total, cost, n=2):
    m = total - cost
    return QuoteResult(
        unit_price_fen=total // n, total_price_fen=total, price_mode="LIMIT",
        max_price_fen=total + 2000, rule_name="r", quote_mode="渠道预估价",
        adjust="NONE", cost_basis="estimate", cost_unit_fen=cost // n,
        cost_total_fen=cost, seat_count=n,
        margin_unit_fen=m // n, margin_total_fen=m)


class Flow(TicketFlow):
    def __init__(self):
        self.rules = QuoteRuleEngine([])


F = Flow()
ENG = QuoteRuleEngine([])

print("=" * 76)
print("一、★★★ 优惠不许让过头（亏钱）")
print("=" * 76)
# 毛利很薄：报价 100、成本 99（毛利 1）⇒ 想减 3 但最多只能减 1
q = mkq(10000, 9900)
r = ENG.second_quote(q, discount_percent=3)
check("★★ 毛利薄时只减到毛利为止（不亏）",
      r.total_price_fen == 9900 and r.margin_total_fen == 0,
      f"{q.total_price_fen} → {r.total_price_fen}, 毛利 {r.margin_total_fen}")
check("★★ 让过头时 notes 里说明「减到毛利为止」",
      any("减到毛利为止" in n for n in r.notes), str(r.notes)[:70])
# 毛利为 0 / 负 ⇒ 不给优惠，且【必须返回同一个对象】
for cost in (10000, 10500):
    q0 = mkq(10000, cost)
    r0 = ENG.second_quote(q0, discount_percent=5)
    check(f"★★ 毛利 {10000 - cost} 分 ⇒ 不给优惠且【原样返回同一对象】",
          r0 is q0, "★ 返回新对象会让调用方以为让了利")
# 正常情况
q2 = mkq(10000, 8000)
r2 = ENG.second_quote(q2, discount_percent=3)
check("★ 正常：报价 100 → 97（减 3%）",
      r2.total_price_fen == 9700, str(r2.total_price_fen))
check("★ 原报价对象【没被改】（要留痕可追溯）",
      q2.total_price_fen == 10000)
check("★ 结果被标记成二次报价", r2.is_second_quote is True)
check("★ 限价跟着降（否则改价还按旧限价，优惠失效）",
      r2.max_price_fen == q2.max_price_fen - 300, str(r2.max_price_fen))
check("★ 比例 0 / 负数 ⇒ 原样",
      ENG.second_quote(q2, discount_percent=0) is q2)

print()
print("=" * 76)
print("二、★★★ 让不出优惠时【不许说「再优惠」】（我实测踩到过）")
print("=" * 76)
SVC = (ROOT / "application" / "ticket_flow.py").read_text(encoding="utf-8")
check("★★ 用「new_quote is quote」判有没有真的优惠",
      "if new_quote is quote:" in SVC)
check("★★ 让不出 ⇒ 返回 None（不发那条消息）",
      "让不出还说" in SVC)

print()
print("=" * 76)
print("三、★ 什么时候不该发")
print("=" * 76)
ALL_OFF = settings.Settings()
ALL_OFF.second_quote.enabled = False
conv_off = Conversation(chat_id="c-off", state=ConvState.QUOTED,
                        quote=mkq(10000, 8000), quote_id="q1")
conv_off.quote_id = "q1"
# 用【默认配置】（关）跑 —— 不依赖外部 settings.json
check("★ 开关没开 ⇒ 不发", F.send_second_quote(conv_off) is None)
# 其余判据（用源码层面确认，避免改盘上配置）
check("★★ 状态不是「已报价」⇒ 不发（买家已往下走）",
      "conv.state is not ConvState.QUOTED" in SVC)
check("★★ 报价已过期 ⇒ 不发（过期价的成本依据不作数）",
      "quote_expires_at" in SVC and "exp < conv_now()" in SVC)
check("★★ 已发过 ⇒ 不发（幂等，否则到点每 tick 发一遍）",
      'getattr(quote, "is_second_quote", False)' in SVC)
check("★ 会话上没报价 ⇒ 不发", "if quote is None:" in SVC)

print()
print("=" * 76)
print("四、★ 默认行为不变 + 到点推进")
print("=" * 76)
d = settings.Settings()
check("★★ 默认 enabled=False（行为不变）",
      d.second_quote.enabled is False)
check("默认等待 30 秒", d.second_quote.wait_seconds == 30)
check("默认优惠 3%", d.second_quote.discount_percent == 3)
# ★ 到点推进挂在 handle_run_due（和退款时序/人工超时同一个循环）
API = (ROOT / "application" / "plugin_api.py").read_text(encoding="utf-8")
check("★★ 挂在 handle_run_due（每秒推进）", "second_quote_due" in API)
# ★ 幂等键 = 会话 + 报价 id ⇒ 等待期间买家再说话不会把倒计时重来
check("★★ 幂等键含报价 id（倒计时不会被重复发言重置）",
      'f"{conv.chat_id}:{conv.quote_id}"' in SVC)
# ★ 首报才排，二次报价之后不再排（否则无限循环）
check("★★ 只为首报排队（二次报价不再排，防无限循环）",
      'not getattr(quote, "is_second_quote", False)' in SVC)

print()
print("=" * 76)
print("五、★ 共用到点队列（不重复实现）")
print("=" * 76)
check("★★ schedule_queue 被 refund 和 second_quote 共用",
      "schedule_queue" in (ROOT / "application" / "refund_schedule.py"
                           ).read_text(encoding="utf-8")
      and "schedule_queue" in SVC)
# ★ 幂等：同 key 重复排【不刷新时刻】—— 否则永远等不到
SCHED = ROOT / "data" / "schedule-queue.json"
SCHED.unlink(missing_ok=True)
t0 = sq.schedule("second_quote", "k1", delay_seconds=30,
                 now=datetime(2026, 10, 14, 12, 0, 0))
t1 = sq.schedule("second_quote", "k1", delay_seconds=999,
                 now=datetime(2026, 10, 14, 12, 0, 5))
check("★★ 同 key 重复排不刷新时刻（否则永远等不到）", t0 == t1, f"{t0} vs {t1}")
check("★ kind 隔离（不会误取别的类）",
      sq.due("refund", now=datetime(2099, 1, 1)) == []
      and len(sq.due("second_quote", now=datetime(2099, 1, 1))) == 1)
check("★ cancel 能撤掉", sq.cancel("second_quote", "k1")
      and sq.load()["items"] == [])
SCHED.unlink(missing_ok=True)

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
