"""
_demo_expiry.py —— 验证报价 30 分钟有效期真的生效

★ 这类"时间相关"的逻辑最容易被写成摆设：
  代码里写了 quote_expires_at，但没人检查它，等于没写。
  所以必须有一个测试**把时间推到过期之后**，看状态机是不是真的拦住。
"""
import sys
from datetime import timedelta
from pathlib import Path

# ★ Windows 上 stdout 默认 GBK，打不出 ↔ ★ ✓ − 这类字符会抛
#   UnicodeEncodeError。★ 必须放在【任何 print 之前】——
#   这些测试是模块级脚本，从第一行就开始跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


results = []

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import templates as tpl
from domain import conversation as conv
from domain.conversation import Conversation, ConvState, Event
from settings import DEFAULT as CONFIG




def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


print("=" * 76)
print("一、配置来源确认")
print("=" * 76)
ttl = CONFIG.recognize_quote.quote_ttl_minutes
print(f"  配置的报价有效时长 = {ttl} 分钟")
check("报价有效时长按配置为 30 分钟", ttl == 30, f"{ttl} 分钟")

print()
print("=" * 76)
print("二、刚报价时，有效期正确设置")
print("=" * 76)
c = Conversation(chat_id="t1")
c.apply(Event.BUYER_IMAGE)
d = c.apply(Event.RECOGNIZE_OK)
check("进入已报价状态", c.state is ConvState.QUOTED, c.state.value)
check("设置了失效时刻", c.quote_expires_at is not None, str(c.quote_expires_at))
left = c.seconds_left()
check("剩余时间约等于 30 分钟", left is not None and 1795 <= left <= 1800,
      f"剩余 {left:.1f} 秒")
check("此刻没有过期", not c.quote_expired())

print()
print("=" * 76)
print("三、★ 把时间推到 30 分钟之后 —— 关键验证")
print("=" * 76)

# 用注入的时间函数模拟"时间流逝"。这是唯一可靠的测法：
# 不能真的等 30 分钟，也不能改系统时钟。
FAKE = {"t": None}


def fake_now():
    return FAKE["t"]


real_now = conv.now
conv.now = fake_now          # 注入
try:
    c2 = Conversation(chat_id="t2")
    FAKE["t"] = real_now()
    c2.apply(Event.BUYER_IMAGE)
    c2.apply(Event.RECOGNIZE_OK)
    check("报价成功", c2.state is ConvState.QUOTED)
    print(f"    报价时刻: {FAKE['t']:%H:%M:%S}")

    # 往前推 29 分钟：不该过期
    FAKE["t"] = real_now() + timedelta(minutes=29)
    check("29 分钟后仍有效", not c2.quote_expired(),
          f"剩余 {c2.seconds_left():.0f} 秒")

    # 再做一次确认，应该正常进入拍单
    d29 = c2.apply(Event.BUYER_CONFIRM)
    check("29 分钟后仍能确认下单", d29.state is ConvState.AWAIT_REPRICE,
          f"{d29.state.value} / reply={d29.reply_key}")

    # ---- 推到 31 分钟 ----
    c3 = Conversation(chat_id="t3")
    FAKE["t"] = real_now()
    c3.apply(Event.BUYER_IMAGE)
    c3.apply(Event.RECOGNIZE_OK)
    FAKE["t"] = real_now() + timedelta(minutes=31)

    check("31 分钟后已过期", c3.quote_expired(),
          f"剩余 {c3.seconds_left():.0f} 秒")

    d31 = c3.apply(Event.BUYER_CONFIRM)
    check("★ 过期后回复确认 → 不再进入拍单", d31.state is not ConvState.AWAIT_REPRICE,
          f"实际状态 = {d31.state.value}")
    check("★ 过期后发出的是过期提示", d31.reply_key == "QUOTE_EXPIRED",
          f"reply_key = {d31.reply_key}")
    check("★ 过期后状态回到空闲（可重来）", d31.state is ConvState.IDLE,
          d31.state.value)
    check("★ 过期后失效时刻被清空", c3.quote_expires_at is None)

    print()
    print("  过期时实际发出的消息：")
    print(f"    {tpl.render(d31.reply_key)}")

    # 边界：正好 30 分钟整
    c4 = Conversation(chat_id="t4")
    FAKE["t"] = real_now()
    c4.apply(Event.BUYER_IMAGE)
    c4.apply(Event.RECOGNIZE_OK)
    FAKE["t"] = real_now() + timedelta(minutes=30)
    check("正好 30 分钟整 → 算过期（边界取闭区间）", c4.quote_expired())

finally:
    conv.now = real_now       # 务必还原

print()
print("=" * 76)
print("四、★ 过期后重发截图 → 拿到全新的 30 分钟")
print("=" * 76)
# ★ 这里必须用假时钟，否则只能验证"两次报价相隔几微秒所以时间接近"——
#   那是废话，证明不了任何东西。真正要证明的是：
#   **过期之后重来一次，会重新拿到完整的 30 分钟，而不是沿用旧的。**
c5 = Conversation(chat_id="t5")
FAKE["t"] = real_now()
conv.now = fake_now
try:
    c5.apply(Event.BUYER_IMAGE)
    c5.apply(Event.RECOGNIZE_OK)
    first = c5.quote_expires_at
    print(f"    第一次报价 {FAKE['t']:%H:%M:%S} → 失效 {first:%H:%M:%S}")

    # 推到过期
    FAKE["t"] = real_now() + timedelta(minutes=31)
    check("此时已过期", c5.quote_expired())

    # 重发截图 → 重新报价
    c5.apply(Event.BUYER_IMAGE)
    c5.apply(Event.RECOGNIZE_OK)
    second = c5.quote_expires_at
    print(f"    重报时刻 {FAKE['t']:%H:%M:%S} → 失效 {second:%H:%M:%S}")

    check("★ 重报后不再过期", not c5.quote_expired())
    check("★ 重报后拿到全新的 30 分钟",
          c5.seconds_left() is not None and 1795 <= c5.seconds_left() <= 1800,
          f"剩余 {c5.seconds_left():.0f} 秒")
    check("★ 新的失效时刻晚于旧的", second > first,
          f"{first:%H:%M:%S} → {second:%H:%M:%S}")
    check("★ 新失效时刻 = 重报时刻 + 30 分钟",
          abs((second - FAKE["t"]).total_seconds() - 1800) < 1,
          f"相差 {(second - FAKE['t']).total_seconds():.0f} 秒")
finally:
    conv.now = real_now

print()
print("=" * 76)
print("五、报价卡文案 vs 真实时效")
print("=" * 76)
card = tpl.render_messages("QUOTE_CARD")[0]
has_2min = "限时2分钟" in card
real_30 = CONFIG.recognize_quote.quote_ttl_minutes == 30
check("文案说 2 分钟、真实 30 分钟（已知差异）", has_2min and real_30,
      "文案是营销话术，代码按配置的 30 分钟执行")
check("过期文案里报的是真实配置值",
      str(CONFIG.recognize_quote.quote_ttl_minutes) in tpl.render("QUOTE_EXPIRED"),
      tpl.render("QUOTE_EXPIRED"))

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
