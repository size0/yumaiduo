"""抽出共用的「时间窗」判定。

★★ 为什么要抽
   我这轮要做「夜间识别时段」（默认 23:30~08:00），它是【跨午夜】的。
   而退款时序里已经有一份时间窗判定（`refund_schedule.in_window`），
   那份原来只支持 `start <= now <= end` —— 跨午夜时永远不成立。
   ⇒ 如果在夜间识别里【再写一份】，就会出现：
        · 两份实现的边界/跨午夜处理必然慢慢分叉
        · 修了一处忘了另一处
     这正是我在关键词话术那次刚消灭掉的老毛病。
   ⇒ 抽成 time_window.py，两处都用它。

★ 语义（三个取舍，都写明理由）：
   · 边界【含头含尾】
   · 跨午夜支持（start > end 时按"或"判）
   · 解析不了的时间串 ⇒ 回默认值（绝不能因为一个手误变成"永远不许"）
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def parse_hhmmss(text: str, default: tuple) -> tuple:
    """把 'HH:MM:SS' / 'HH:MM' / 'HH' 解析成 (时, 分, 秒)。解析不了用 default。"""
    parts = str(text or "").strip().split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return default
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        return default
    return (h, m, s)


def in_range(start_raw: str, end_raw: str, *,
             now: datetime | None = None) -> bool:
    """
    现在在不在这段时间里（支持跨午夜）。

    ★ 边界含头含尾。
    ★ start > end ⇒ 跨午夜（如 23:30~08:00）：
        现在 >= start 【或】 现在 <= end
    ★ 解析不了就用默认（全天），不返回 False ——
      一个手误不该让功能变成"永远关闭"。
    """
    now = now or datetime.now()
    sh, sm, ss = parse_hhmmss(start_raw, (0, 0, 0))
    eh, em, es = parse_hhmmss(end_raw, (23, 59, 59))
    start, end = (sh, sm, ss), (eh, em, es)
    t = (now.hour, now.minute, now.second)
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def describe(start_raw: str, end_raw: str) -> str:
    """给运营看的时段描述（跨午夜时说明一下，免得看的人以为配错了）。"""
    s = str(start_raw or "").strip() or "00:00:00"
    e = str(end_raw or "").strip() or "23:59:59"
    sh, sm, _ = parse_hhmmss(s, (0, 0, 0))
    eh, em, _ = parse_hhmmss(e, (23, 59, 59))
    if (sh, sm) > (eh, em):
        return f"{s}~次日 {e}"
    return f"{s}~{e}"
