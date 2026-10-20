"""时间窗 + 夜间识别提醒检查。

★★ 为什么必须断：
   1. **跨午夜**是这里最容易错的地方 —— 我原来那份退款时序判定
      只写了 `start <= now <= end`，跨午夜（start > end）时**永远不成立**
      ⇒ 那种配置会变成"永远不许"。而夜间识别【默认就是跨午夜】
      （23:30~08:00），所以这个判据必须是对的。
   2. 夜间提醒是「**补充**」话术 ⇒ 必须是追加，不是替换识别结果。
   3. 默认行为必须不变（默认时段之外不提醒）。
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from application.time_window import describe, in_range, parse_hhmmss   # noqa: E402
from application.ticket_flow import _night_cfg                          # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


print("=" * 76)
print("一、★★ 跨午夜（这里最容易错）")
print("=" * 76)
# 夜间识别默认就是 23:30~08:00
check("★ 23:29:59 不在（还没到）",
      not in_range("23:30", "08:00", now=datetime(2026, 10, 14, 23, 29, 59)))
check("★★ 23:30:00 在（含头）",
      in_range("23:30", "08:00", now=datetime(2026, 10, 14, 23, 30, 0)))
check("★★ 00:00:00 在（跨过午夜）",
      in_range("23:30", "08:00", now=datetime(2026, 10, 15, 0, 0, 0)))
check("★★ 02:48:00 在（凌晨）",
      in_range("23:30", "08:00", now=datetime(2026, 10, 15, 2, 48, 0)))
check("★★ 08:00:00 在（含尾）",
      in_range("23:30", "08:00", now=datetime(2026, 10, 15, 8, 0, 0)))
check("★ 08:00:01 不在",
      not in_range("23:30", "08:00", now=datetime(2026, 10, 15, 8, 0, 1)))
check("★ 白天 12:00 不在",
      not in_range("23:30", "08:00", now=datetime(2026, 10, 15, 12, 0, 0)))
# ★ 反面：如果不支持跨午夜，上面有一半会错。用一个"必然错"的写法对照
_navie_ok = (23, 30, 0) <= (2, 48, 0) <= (8, 0, 0)
check("★★ 对照：朴素写法 `start <= now <= end` 在跨午夜时必然错",
      _navie_ok is False, "所以必须分开判")

print()
print("=" * 76)
print("二、不跨午夜的普通时段")
print("=" * 76)
check("09:59:59 不在", not in_range("10:00", "18:00", now=datetime(2026, 10, 14, 9, 59, 59)))
check("10:00:00 在（含头）", in_range("10:00", "18:00", now=datetime(2026, 10, 14, 10, 0, 0)))
check("18:00:00 在（含尾）", in_range("10:00", "18:00", now=datetime(2026, 10, 14, 18, 0, 0)))
check("18:00:01 不在", not in_range("10:00", "18:00", now=datetime(2026, 10, 14, 18, 0, 1)))

print()
print("=" * 76)
print("三、坏输入不许把功能变成「永远关闭」")
print("=" * 76)
t = datetime(2026, 10, 14, 12, 0, 0)
for a, b in (("", ""), ("abc", "xyz"), ("99:99:99", ""), ("25:00", "26:00")):
    check(f"★ 坏输入 ({a!r},{b!r}) ⇒ 回退成全天（True）",
          in_range(a, b, now=t) is True)
check("★ parse_hhmmss 坏值回默认",
      parse_hhmmss("99:99:99", (1, 2, 3)) == (1, 2, 3))
check("★ parse_hhmmss 认 HH:MM",
      parse_hhmmss("07:05", (0, 0, 0)) == (7, 5, 0))

print()
print("=" * 76)
print("四、describe（跨午夜要说人话）")
print("=" * 76)
check("★ 跨午夜写「次日」", "次日" in describe("23:30", "08:00"),
      describe("23:30", "08:00"))
check("★ 普通时段照常", describe("10:00", "18:00") == "10:00~18:00",
      describe("10:00", "18:00"))

print()
print("=" * 76)
print("五、★ 夜间识别：是「补充」不是替换")
print("=" * 76)
got = _night_cfg()
check("★ 能读到 (开始, 结束, 话术)", isinstance(got, tuple) and len(got) == 3,
      str(got)[:60])
check("★★ 默认跨午夜 23:30~08:00", got[0] == "23:30" and got[1] == "08:00")
check("★ 默认有话术且提到「早上8点」", "8点" in got[2] or "8" in got[2],
      got[2][:40])
# ★ 接线形状：追加载 messages，且失败不影响识别
_src = (ROOT / "application" / "ticket_flow.py").read_text(encoding="utf-8")
check("★★ 提醒是【追加】到 messages（不替换识别结果）",
      "out.messages.append(_msg)" in _src)
check("★★ 放在识别【之前】（识别要 5 秒，不该等完才说）",
      _src.find("_night_cfg()") < _src.find("ch7_recognize(image_url"))
check("★★ 提醒失败不影响识别（只记 note）",
      "不影响识别" in _src)
# ★ 时段判定必须共用，不许在 ticket_flow 里再写一份
check("★★ 时窗判定共用 time_window（不在 ticket_flow 里再写一份）",
      "from application.time_window import in_range" in _src)
# ★ 退款时序也用的是同一份
_ref = (ROOT / "application" / "refund_schedule.py").read_text(encoding="utf-8")
check("★★ 退款时序也用同一份 time_window（两处不各写一套）",
      "from application.time_window import in_range" in _ref)

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
