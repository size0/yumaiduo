"""退款时序：什么时候【提交】退票申请。

★★ 先说清这组设置管的是哪件事（我查了才敢下手）
   退款是良票平台做的，我们拦不住（`_on_refunded` 是平台告诉我们"退完了"）。
   但**"提交退票申请"这个动作是我们做的**：
       service.on_refund → flow.on_refund_request → api.ch5_refund
   ⇒ 所以 weekdays / time_start / delay_seconds 管的是【提交那一刻】，
     不是"钱什么时候到账"。

★ 三个开关各自的语义：
   weekdays       哪几天提交。不在列表里的日期 ⇒ 不提交，告诉买家等
   time_start/end 时段：只在这个时段内提交
   delay_seconds  延迟提交：买家申请后先等这么多秒再提交
                  （给买家一个反悔/补充信息的机会；V5 也有这个行为）

★★ 为什么不复用命令队列的 not_before
   那是【发给插件的命令】用的。而提交退票是直接的 HTTP 调用
   （api.ch5_refund），不经插件 ⇒ not_before 用不上。
   ⇒ 自己记一个"到点时间"，由 handle_run_due（每秒被调一次）来推进。
     形状和 reminders.py 一样：纯函数 + 一个便宜的后台扫描。

★ 存哪：data/refund-schedule.json。★ 为什么不进 settings.json：
  这是【运行时状态】，不是配置。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FILE = Path(__file__).resolve().parent.parent / "data" / "refund-schedule.json"
_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ==================================================================== 时间窗
def weekday_name(now: datetime) -> str:
    """'周一'…'周日'。★ 用 datetime.weekday()（周一=0），不用 isoweekday 混。"""
    return _WEEKDAYS[now.weekday()]


def _parse_hhmmss(text: str, default: tuple) -> tuple:
    """把 'HH:MM:SS' / 'HH:MM' 解析成 (时, 分, 秒)。解析不了用 default。"""
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


def in_window(cfg, *, now: datetime | None = None) -> tuple:
    """
    现在能不能提交退票申请。返回 (能不能, 原因)。

    ★ 两条判据都要过：星期 + 时段。
    ★ 默认值下【永远能提交】（七天全选、00:00:00~23:59:59）——
      所以默认行为一个字不变。
    ★ 边界含头含尾：23:59:59 当天仍算能提交。
    ★★ 2026-10-14 修：支持【跨午夜】的时段（如 22:00~06:00）。
      原来只判 `start <= now <= end`，跨午夜时永远不成立
      （start 比 end 大）⇒ 那种配置会变成"永远不许"。
      我是做「夜间识别时段」（默认 23:30~08:00，正是跨午夜）
      时发现的 —— 同一个判断在那边必须对，这边也得一起对。
    """
    now = now or datetime.now()
    try:
        days = list(getattr(cfg.order_refund, "weekdays", None) or [])
        start_raw = getattr(cfg.order_refund, "time_start", "") or "00:00:00"
        end_raw = getattr(cfg.order_refund, "time_end", "") or "23:59:59"
    except Exception:                                     # noqa: BLE001
        return True, ""
    # ★ 空列表 = 没配 = 不限制（不是"七天都不许"）。
    #   这个取舍很重要：如果按"七天都不许"理解，配置一坏就永远退不了。
    if days and weekday_name(now) not in days:
        return False, f"今天（{weekday_name(now)}）不是退款处理日"
    # ★★ 时段判定【共用】time_window.in_range —— 不在这里再写一份。
    #   为什么：我下一步要做「夜间识别时段」（默认 23:30~08:00，跨午夜），
    #   那个也需要同一套判定。两份实现迟早分叉（这次就发现原来那份
    #   不支持跨午夜）⇒ 抽出来，两处共用。
    from application.time_window import in_range
    if not in_range(start_raw, end_raw, now=now):
        return False, f"现在不在退款处理时段（{start_raw}~{end_raw}）"
    return True, ""


def next_window_hint(cfg, *, now: datetime | None = None) -> str:
    """给买家的一句话（不在窗口时用）。★ 不猜"具体几点"，只说规则。"""
    now = now or datetime.now()
    try:
        days = list(getattr(cfg.order_refund, "weekdays", None) or [])
        start_raw = getattr(cfg.order_refund, "time_start", "") or "00:00:00"
    except Exception:                                     # noqa: BLE001
        return "稍等"
    if days and weekday_name(now) not in days:
        return "退款申请会在我们处理退款的日期第一时间提交"
    return f"退款申请会在 {start_raw} 之后第一时间提交"


# ==================================================================== 到点记录
KIND = "refund"


def load() -> dict:
    """
    读本类任务的视图。★ 为了兼容原来的调用方，形状仍是 {"pending": [...]}，
    但数据存在【共用队列】里（application/schedule_queue.py）。

    ★ 为什么不自己再存一份：那就成了两份落盘格式、两套清理逻辑，
      修一处忘一处。共用一个队列，kind 区分类型。
    """
    from application import schedule_queue as _sq
    items = [x for x in _sq.load()["items"] if x.get("kind") == KIND]
    return {"pending": items}


def schedule(order_no: str, chat_id: str, *, delay_seconds: int = 0,
             now: datetime | None = None) -> str:
    """
    记一条"到点提交退票"。返回要提交的时刻（ISO）。

    ★ 幂等：同一个单号重复记【只保留最早的那个】——
      买家反复说"我要退"，不该越排越晚。
      （幂等由 schedule_queue 保证：同 (kind, key) 不刷新时刻。）
    """
    from application import schedule_queue as _sq
    return _sq.schedule(KIND, str(order_no), delay_seconds=delay_seconds,
                        now=now, chat_id=str(chat_id))


def cancel(order_no: str) -> bool:
    """撤掉一条（买家反悔 / 已经退掉了）。"""
    from application import schedule_queue as _sq
    return _sq.cancel(KIND, str(order_no))


def due_now(cfg, *, now: datetime | None = None) -> list:
    """
    到点了、而且在时间窗里 ⇒ 该提交的那些单。

    ★ 两个条件缺一不可：
      到点（delay_seconds 到了）+ 在窗口（weekdays/time_start 允许）
      不在窗口就【留在队列里等】—— 不是丢掉。
    """
    now = now or datetime.now()
    ok, _ = in_window(cfg, now=now)
    if not ok:
        return []
    from application import schedule_queue as _sq
    return _sq.due(KIND, now=now)


def process_due(*, now: datetime | None = None, cfg=None,
                service=None) -> int:
    """
    把到点的退票申请提交掉。返回提交了几条。

    ★★ 为什么必须【重新拿到会话和订单】再提交：
      延迟提交是"以后"做的事，而那时：
        · 买家可能已经反悔（会话没了 / 订单不在了）
        · 订单可能已经退掉了（平台回调先到了）
      两种情况都【不能提交】—— 提交一个不存在的单会拿到
      500003（重复申请）或直接报错，还可能把已经退掉的单再退一次。
      ⇒ 提交前必须核对：会话还在、待处理单号还是它、状态还允许。
    ★ 幂等：提交后（无论成败）都把这条从队列里去掉 ——
      一直留在队列里会每秒重试，那是打自己的接口。
      失败时靠买家再问一次（而不是无声重试）。
    """
    now = now or datetime.now()
    if cfg is None:
        try:
            import settings as _st
            cfg = _st.load()
        except Exception:                                 # noqa: BLE001
            return 0
    from . import schedule_queue
    items = schedule_queue.due(KIND, now=now)
    if not items:
        return 0
    # ★★ 不 import plugin_runtime —— 那会成环：
    #     plugin_runtime → service → refund_schedule → plugin_runtime
    #   （我第一版就是 module 内 import 它，审计当场抓出来循环依赖 1→2）
    #   ⇒ 由调用方（plugin_api.handle_run_due）把 service 传进来。
    #     依赖方向只有【外层 → 内层】。
    if service is None:
        return 0
    done = 0
    for item in items:
        order_no = str(item.get("order_no") or "")
        chat_id = str(item.get("chat_id") or "")
        ok = False
        try:
            s = service.session(chat_id)
            conv = getattr(s, "conversation", None)
            from .shop_scope import scope, apply_settings
            from copy import deepcopy
            with scope(getattr(conv, 'tenant_id', ''), getattr(conv, 'shop_id', '')):
                effective = apply_settings(deepcopy(cfg))
            if not in_window(effective, now=now)[0]:
                continue
            pending = getattr(conv, "pending_order", None)
            # ★ 核对：还是那个单、还在待处理 ⇒ 才提交
            if (pending is not None
                    and str(getattr(pending, "order_no", "")) == order_no):
                service.flow.on_refund_request(conv)
                done += 1
                ok = True
        except Exception:                                 # noqa: BLE001
            ok = False
        # ★ 无论成败都出队（成败都不该每秒重试）
        cancel(order_no)
        if not ok:
            continue
    return done
