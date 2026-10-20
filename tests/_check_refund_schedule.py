"""退款时序检查：星期 / 时段 / 延迟。

★★ 为什么必须断：
   这三个设置直接决定"买家的退款申请什么时候被提交"。
   判错的后果是【买家以为申请了、其实没提交】，或者反过来
   【窗口外提交了、被平台拒】。
   而且默认值必须保证"行为一个字不变"——这条最容易被改坏。
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import settings                                                         # noqa: E402
from application import refund_schedule as rs                           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


def cfg_with(**kw):
    c = settings.Settings()
    for k, v in kw.items():
        setattr(c.order_refund, k, v)
    return c


ALL_DAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

print("=" * 76)
print("一、★ 默认配置必须【行为不变】（最重要的一条）")
print("=" * 76)
d = settings.Settings()
check("星期默认七天全选", list(d.order_refund.weekdays) == ALL_DAYS,
      str(d.order_refund.weekdays))
check("时段默认全天", d.order_refund.time_start == "00:00:00"
      and d.order_refund.time_end == "23:59:59")
check("延迟默认 0", d.order_refund.delay_seconds == 0)
# ★ 默认下任何时刻都在窗口内
bad = []
for t in (datetime(2026, 10, 14, 0, 0, 0), datetime(2026, 10, 14, 12, 0, 0),
          datetime(2026, 10, 14, 23, 59, 59), datetime(2026, 10, 18, 3, 0, 0)):
    if not rs.in_window(d, now=t)[0]:
        bad.append(str(t))
check("★★ 默认配置下任何时刻都能提交（行为不变）", not bad, str(bad))

print()
print("=" * 76)
print("二、星期判据")
print("=" * 76)
c = cfg_with(weekdays=["周二", "周四"])
check("周二能提交", rs.in_window(c, now=datetime(2026, 10, 13, 12, 0, 0))[0])
check("周三不能提交", not rs.in_window(c, now=datetime(2026, 10, 14, 12, 0, 0))[0])
check("周四能提交", rs.in_window(c, now=datetime(2026, 10, 15, 12, 0, 0))[0])
# ★ 空列表 = 不限制（不是"七天都不许"）——
#   这个取舍很关键：按"都不许"理解的话，配置一被清空就永远退不了钱。
c2 = cfg_with(weekdays=[], time_start="00:00:00", time_end="23:59:59")
check("★★ 空星期 = 不限制（不是七天都不许）",
      rs.in_window(c2, now=datetime(2026, 10, 14, 3, 0, 0))[0])

print()
print("=" * 76)
print("三、时段判据（含头含尾）")
print("=" * 76)
c = cfg_with(time_start="10:00:00", time_end="18:00:00")
check("09:59:59 不能提交",
      not rs.in_window(c, now=datetime(2026, 10, 14, 9, 59, 59))[0])
check("★ 10:00:00 能提交（含头）",
      rs.in_window(c, now=datetime(2026, 10, 14, 10, 0, 0))[0])
check("★ 18:00:00 能提交（含尾）",
      rs.in_window(c, now=datetime(2026, 10, 14, 18, 0, 0))[0])
check("18:00:01 不能提交",
      not rs.in_window(c, now=datetime(2026, 10, 14, 18, 0, 1))[0])
# ★ 坏时间字符串不能把设置变成"永远不许/永远允许"
for bad_val in ("", "abc", "99:99:99", "10"):
    cc = cfg_with(time_start=bad_val)
    got = rs.in_window(cc, now=datetime(2026, 10, 14, 12, 0, 0))[0]
    check(f"★ 坏时间 {bad_val!r} 不至于崩（返回 {got}）", isinstance(got, bool))

print()
print("=" * 76)
print("四、延迟提交")
print("=" * 76)
SCHED = ROOT / "data" / "refund-schedule.json"
SCHED.unlink(missing_ok=True)
base = datetime(2026, 10, 14, 12, 0, 0)
due = rs.schedule("O-1", "c-1", delay_seconds=30, now=base)
check("★ 排到 30 秒后", due == "2026-10-14T12:00:30", due)
check("★ 幂等：重复排不越排越晚",
      rs.schedule("O-1", "c-1", delay_seconds=999,
                  now=base) == due)
check("★ 未到点不捞出来", rs.due_now(cfg_with(), now=base) == [])
check("★ 到点才捞出来",
      len(rs.due_now(cfg_with(), now=datetime(2026, 10, 14, 12, 0, 31))) == 1)
# ★ 窗口外要留在队列里等，不是丢掉
c_win = cfg_with(weekdays=["周一"], time_start="00:00:00", time_end="23:59:59")
check("★★ 窗口外不捞、但记录还在（等下一个窗口，不丢）",
      rs.due_now(c_win, now=datetime(2026, 10, 14, 12, 0, 31)) == []
      and len(rs.load()["pending"]) == 1)
check("★ 撤单能撤掉", rs.cancel("O-1") and rs.load()["pending"] == [])
SCHED.unlink(missing_ok=True)

print()
print("=" * 76)
print("五、★★ 接线的形状（别退回「无声重试」）")
print("=" * 76)
import inspect                                                          # noqa: E402
_src = inspect.getsource(rs.process_due)
# ★ 提交前必须核对订单还在（延迟期间可能已经退掉了）
check("★★ 提交前核对订单还在（延迟期间可能已退掉）",
      "pending_order" in _src and "order_no" in _src)
# ★ 无论成败都出队 —— 否则每秒重试，打自己的接口
check("★★ 无论成败都出队（否则每秒重试打自己接口）",
      "cancel(order_no)" in _src)
# ★ 不许 import plugin_runtime（那会成环）
_mod = (ROOT / "application" / "refund_schedule.py").read_text(encoding="utf-8")
check("★★ 不 import plugin_runtime（依赖注入，防循环依赖）",
      "from application.plugin_runtime import" not in _mod
      and "from .plugin_runtime import" not in _mod)
# ★ service.py 里三条判据都在
_svc = (ROOT / "application" / "service.py").read_text(encoding="utf-8")
check("★★ service.on_refund 查了窗口", "in_window" in _svc)
check("★★ 不在窗口时明说，而不是假装提交了",
      "不到我们处理退款的时间" in _svc or "next_window_hint" in _svc)

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
