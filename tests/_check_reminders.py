"""验证提醒引擎：时间解析 + 四种提醒的触发条件 + 幂等。

★★ 为什么要测得这么细：提醒是【自动发消息】——
   算错时间就会在错误的时刻打扰买家（半夜发"快开场了"），
   或者重复发（同一单发三次"确认收货"）。这两种都比"不发"更糟。
"""
import datetime as dt
import sys
from pathlib import Path

# ★★ 用【脚本自身位置】定位项目根，不用 " . " ——
#   run_checks 是从【上级目录】跑的（python ticket-agent\tests\...），
#   写 "." 会 ModuleNotFoundError: No module named application。
#   （这个坑仓库里其它测试都踩过，注释里写着。）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from application.reminders import (parse_show_time,                    # noqa: E402
                                   build_reminder_commands)
import settings                                                         # noqa: E402

# ★ 用【本地时区】—— 订单里的场次文本就是影院的本地时间
NOW = dt.datetime(2026, 10, 14, 12, 0).astimezone()

# ★★ 显式设好配置，不依赖默认值。
#   实测默认是 advance_minutes=10 / urge_order.delay_minutes=60 /
#   urge_receive.delay_minutes=0 / urge_review.delay_minutes=0 ——
#   我第一版测试假设了 60/30/60/180，两条断言全挂。
#   ★ 教训：测试要【显式给出前提】。依赖默认值的话，
#     以后调默认值会让测试挂掉，而那时功能其实没坏 —— 白查一场。
cfg = settings.Settings()
cfg.movie_remind.advance_minutes = 60
cfg.urge_order.delay_minutes = 30
cfg.urge_receive.delay_minutes = 60
cfg.urge_review.delay_minutes = 180
ok = fail = 0


def ck(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}")
    else:
        fail += 1
        print(f"  [FAIL] {label}  {detail}")


print("=" * 76)
print("① 场次时间解析（解析不出来就必须返回 None，不许兜底）")
print("=" * 76)
CASES = [
    ("2026-10-20 19:30", True),
    ("万达影城 2026-10-20 19:30", True),
    ("10-20 19:30", True),
    ("2026/10/20 19:30", True),
    ("2026年10月20日 19:30", True),
    ("明天晚上", False),
    ("", False),
    ("19:30", False),
]
for text, should in CASES:
    got = parse_show_time(text, now=NOW)
    ck(f"{text!r} → {got}", (got is not None) == should,
       f"期望 {'解析得出' if should else 'None'}")

# 补年份的边界：MM-DD 已过去 ⇒ 算明年
got = parse_show_time("01-05 19:30", now=NOW)
ck("跨年：01-05 已过去 ⇒ 补成明年", got and got.year == 2027, str(got))

print()
print("=" * 76)
print("② 观影提醒：只在 advance_minutes 附近触发")
print("=" * 76)


def mk(**kw):
    base = dict(order_id="o1", quote_id="q1", tenant_id="t", shop_id="s",
                buyer_id="b", chat_id="c", state="PAID",
                payment_state="CONFIRMED", seats=["4排1座"],
                cinema="万达影城", movie="测试影片", amount_fen=4600, shipped=False,
                ticket_codes=[], payload={}, created_at=NOW.isoformat())
    base.update(kw)
    return base


# 距开场 60 分钟（默认 advance_minutes=60）⇒ 该发
o = mk(show=(NOW + dt.timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M"))
cmds = build_reminder_commands(now=NOW, orders=[o], cfg=cfg)
ck("距开场 60 分钟 ⇒ 发观影提醒",
   any(c.extra.get("reminder") == "movie_remind" for c in cmds),
   str([c.extra.get("reminder") for c in cmds]))

# 距开场 300 分钟 ⇒ 不发
o2 = mk(show=(NOW + dt.timedelta(minutes=300)).strftime("%Y-%m-%d %H:%M"))
cmds2 = build_reminder_commands(now=NOW, orders=[o2], cfg=cfg)
ck("距开场 300 分钟 ⇒ 不发",
   not any(c.extra.get("reminder") == "movie_remind" for c in cmds2))

# 场次解析不出来 ⇒ 不发（不兜底）
o3 = mk(show="明天晚上七点半")
cmds3 = build_reminder_commands(now=NOW, orders=[o3], cfg=cfg)
ck("场次解析不出 ⇒ 不发（不兜底成「现在就发」）",
   not any(c.extra.get("reminder") == "movie_remind" for c in cmds3))

# advance_minutes 改成 120 ⇒ 60 分钟时不该发
cfg2 = settings.Settings()
cfg2.movie_remind.advance_minutes = 120
cmds4 = build_reminder_commands(now=NOW, orders=[o], cfg=cfg2)
ck("把提醒时间改成 120 ⇒ 60 分钟时不发（配置真的生效）",
   not any(c.extra.get("reminder") == "movie_remind" for c in cmds4))

print()
print("=" * 76)
print("③ 催收货 / 催促下单：靠时间差 + 状态")
print("=" * 76)
shipped = mk(order_id="o2", shipped=True, state="TICKETED",
             delivered_at=(NOW - dt.timedelta(minutes=60)).isoformat())
c5 = build_reminder_commands(now=NOW, orders=[shipped], cfg=cfg)
ck("发货满 60 分钟 ⇒ 催收货",
   any(c.extra.get("reminder") == "urge_receive" for c in c5))

done = mk(order_id="o3", shipped=True, state="DELIVERED",
          delivered_at=(NOW - dt.timedelta(minutes=60)).isoformat())
c6 = build_reminder_commands(now=NOW, orders=[done], cfg=cfg)
ck("已交付的订单不再催收货",
   not any(c.extra.get("reminder") == "urge_receive" for c in c6))

# ★ payment_state 必须给 UNPAID —— mk() 的默认是 CONFIRMED，
#   而 r_urge_order 的第一条判断就是已付款的不催下单。
#   （我第一版漏了这个前提，断言假失败。）
# ★ payment_state 必须给 UNPAID —— mk() 的默认是 CONFIRMED，
#   而 r_urge_order 第一条判断就是"已付款的不催下单"。
#   （我第一版漏了这个前提，断言假失败。）
waiting = mk(order_id="o4", state="AWAITING_PAYMENT",
             payment_state="UNPAID",
             show="2026-10-15 19:30",
             created_at=(NOW - dt.timedelta(minutes=35)).isoformat())
c7 = build_reminder_commands(now=NOW, orders=[waiting], cfg=cfg)
ck("报价发出 35 分钟还没拍 ⇒ 催促下单",
   any(c.extra.get("reminder") == "urge_order" for c in c7))

paid = mk(order_id="o5", state="PAID", payment_state="CONFIRMED",
          created_at=(NOW - dt.timedelta(minutes=35)).isoformat())
c8 = build_reminder_commands(now=NOW, orders=[paid], cfg=cfg)
ck("已付款的不再催促下单",
   not any(c.extra.get("reminder") == "urge_order" for c in c8))

print()
print("=" * 76)
print("④ 不该发的：已取消 / 退款 / 没会话")
print("=" * 76)
for st in ("CANCELLED", "REFUNDED"):
    c = build_reminder_commands(
        now=NOW, cfg=cfg,
        orders=[mk(state=st, show=(NOW + dt.timedelta(minutes=60))
                   .strftime("%Y-%m-%d %H:%M"))])
    ck(f"{st} 的单不发任何提醒", not c)
c = build_reminder_commands(
    now=NOW, cfg=cfg,
    orders=[mk(chat_id="", show=(NOW + dt.timedelta(minutes=60))
               .strftime("%Y-%m-%d %H:%M"))])
ck("没有会话的单不发（发不出去）", not c)

print()
print("=" * 76)
print("⑤ 幂等：dedupe_key 让同一单同一提醒只发一次")
print("=" * 76)
c9 = build_reminder_commands(now=NOW, orders=[o], cfg=cfg)
keys = [c.dedupe_key for c in c9]
ck("dedupe_key 形如 reminder:<类型>:<订单号>",
   all(k.startswith("reminder:") and k.endswith(":o1") for k in keys),
   str(keys))
ck("同一单同一类型的 key 完全相同（才能去重）",
   len(set(keys)) == len(keys), str(keys))

print()
print("=" * 76)
print(f"结果: {ok}/{ok + fail} 项通过")
print("=" * 76)
sys.exit(0 if fail == 0 else 1)
