"""提醒引擎：按订单事实 + 配置，定时给买家发提醒。

★★ 为什么要有这个模块
   设置页里有一组"提醒"配置（观影提醒 / 催收货 / 催好评 / 催促下单…），
   但代码里【连功能都没有】—— 不是没读配置，是没有定时器、没有触发点。
   ⇒ 这个模块把那类功能做出来，并顺便把配置接上。

★★ 挂在哪：插件【每秒】调 POST /api/v1/events/run-due
   （server.mjs 的 readingDue 定时器）⇒ 我们的 handle_run_due 每秒被调一次。
   提醒就是"到点了没有"的检查，挂在那个 tick 上最自然，不需要新进程/新定时器。
   ★ 但那句话也说了「它必须便宜：没事件时立刻返回」
     ⇒ 这里的扫描必须：① 有开关时先短路 ② 每 N 秒才真扫一次。

★★ 幂等：靠 dedupe_key（order_id + 提醒类型）。
   队列的 enqueue 会按 dedupe_key 去重，所以同一单同一提醒只发一次。
   ★ 不靠 payload 里的标记 —— 那种标记在多进程/重试下会漏；
     靠数据库唯一键才可靠。

★ 时间怎么算：
   · 观影提醒：show 是"2026-10-20 19:30"这种文本 ⇒ 尽力解析；
     解析不出来就【不发】（宁可少发，也不要在错误的时刻发）。
   · 催收货：按订单 shipped/delivered_at 之后过了多久。
"""
import datetime as _dt
import logging
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations.yumaiduo.commands import Command                     # noqa: E402

_LOG = logging.getLogger(__name__)


def render_reminder_message(text: str, order: dict) -> str | None:
    """Replace settings tokens with order facts; withhold incomplete messages.

    Only declared tokens are interpreted: bracketed emoji such as [大哭]
    and tokens contained in order values must remain literal.
    """
    seats = order.get("seats") or []
    values = {
        "影院名": order.get("cinema"),
        "影片名": order.get("movie"),
        "放映时间": order.get("show"),
        "座位编号": "、".join(str(s) for s in seats) if isinstance(seats, (list, tuple)) else seats,
        "场次名称": order.get("show_name"),
        "影院地址": order.get("cinema_address"),
        "粉丝昵称": order.get("buyer_name"),
    }
    pattern = r"\[(" + "|".join(values) + r")\]"
    needed = re.findall(pattern, text)
    if any(not str(values[key] or "").strip() for key in needed):
        _LOG.warning("Reminder withheld: missing template facts for order %s",
                     order.get("order_id", ""))
        return None
    return re.sub(pattern, lambda m: str(values[m.group(1)]).strip(), text)


# ==================================================================== 时间解析
_SHOW_PATTERNS = (
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%m-%d %H:%M", "%m/%d %H:%M", "%Y年%m月%d日 %H:%M",
)


def parse_show_time(text: str, *, now: _dt.datetime | None = None):
    """
    把场次文本解析成 datetime。解析不出来返回 None。

    ★ 为什么要带 now：`10-20 19:30` 这种没有年份，
      要补成【今年】；如果补完已经过去很久，说明是明年的场次。
    ★ 解析失败就返回 None，调用方【不要兜底成"现在就发"】——
      在错误的时刻发观影提醒比不发更糟（买家以为记错了场次）。
    """
    s = str(text or "").strip()
    if not s:
        return None
    now = now or _dt.datetime.now()
    # 先从文本里抠出"日期 时间"那一段（前面可能带影院名）
    m = re.search(r"(\d{1,4}[-/年]\d{1,2}[-/月]\d{1,2}日?\s+\d{1,2}:\d{2})", s)
    if not m:
        m = re.search(r"(\d{1,2}[-/月]\d{1,2}日?\s+\d{1,2}:\d{2})", s)
    if not m:
        return None
    frag = m.group(1).replace("年", "-").replace("月", "-").replace("日", "")
    for pat in _SHOW_PATTERNS:
        try:
            dt = _dt.datetime.strptime(frag.strip(), pat)
        except ValueError:
            continue
        # ★★ 时区语义（踩过一次，写清楚免得再错）：
        #   订单里的「2026-10-20 19:30」是【影院的本地时间】（北京时间），
        #   不是 UTC。我原先把解析结果无条件当 UTC ⇒ 在北京算窗口偏 8 小时，
        #   观影提醒永远不会在正确的时刻触发。
        #   ⇒ 规则：naive 时间按【本地时区】理解；比较前两边都转成 naive 本地。
        #     不做跨时区换算 —— 影院和系统在同一地。
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        if now.tzinfo is not None:
            now = now.astimezone().replace(tzinfo=None)
        if pat.startswith("%m") or "%m-%d" in pat:
            dt = dt.replace(year=now.year)
            # 补年份后若已过去超过 12 小时，认为是明年的场次
            if dt < now - _dt.timedelta(hours=12):
                dt = dt.replace(year=now.year + 1)
        return dt
    return None


# ==================================================================== 提醒定义
def r_movie_remind(now, order, cfg):
    """观影提醒：距开场 advance_minutes 分钟时提醒一次。"""
    st = parse_show_time(order.get("show"), now=now)
    if st is None:
        return None
    lead = int(cfg.movie_remind.advance_minutes)
    # ★ 两边统一成 naive 本地再比（见 parse_show_time 的时区说明）
    st_cmp = st.astimezone().replace(tzinfo=None) if st.tzinfo else st
    now_cmp = now.astimezone().replace(tzinfo=None) if now.tzinfo else now
    delta = (st_cmp - now_cmp).total_seconds() / 60
    # ★ 窗口：距开场 [lead-2, lead] 分钟之间。用区间而不是"="，
    #   因为 tick 是整秒的、不可能正好命中某一分钟。
    if not (lead - 2 <= delta <= lead):
        return None
    msg = (getattr(cfg.movie_remind, "message", "") or "").strip()
    cinema = str(order.get("cinema") or "").strip()
    show = str(order.get("show") or "").strip()
    seats = "、".join(str(s) for s in (order.get("seats") or []))
    where = " ".join(x for x in (cinema, show, seats) if x)
    return (msg, where) if msg else None


def r_urge_receive(now, order, cfg):
    """催收货：发货后 delay_minutes 分钟还没确认，提醒一次。"""
    if not order.get("shipped"):
        return None
    raw = order.get("delivered_at") or order.get("updated_at")
    when = _parse_iso(raw)
    if when is None:
        return None
    delay = int(cfg.urge_receive.delay_minutes)
    # ★ 统一成 naive 本地再比（naive 时间 = 本地时间）
    _n = now.astimezone().replace(tzinfo=None) if now.tzinfo else now
    _w = when.astimezone().replace(tzinfo=None) if when.tzinfo else when
    mins = (_n - _w).total_seconds() / 60
    if not (delay <= mins <= delay + 5):
        return None
    # ★ 已确认收货的不再催（state 到 DELIVERED / 交易完成）
    if str(order.get("state") or "") in ("DELIVERED", "CANCELLED", "REFUNDED"):
        return None
    msg = (getattr(cfg.urge_receive, "message", "") or "").strip()
    return (msg, "") if msg else None


def r_urge_order(now, order, cfg):
    """催促下单：报价已发、买家还没拍下，过了 delay_minutes 催一次。"""
    if str(order.get("state") or "") not in ("CREATED", "AWAITING_PAYMENT"):
        return None
    if str(order.get("payment_state") or "") == "CONFIRMED":
        return None
    when = _parse_iso(order.get("created_at"))
    if when is None:
        return None
    delay = int(cfg.urge_order.delay_minutes)
    # ★ 统一成 naive 本地再比（naive 时间 = 本地时间）
    _n = now.astimezone().replace(tzinfo=None) if now.tzinfo else now
    _w = when.astimezone().replace(tzinfo=None) if when.tzinfo else when
    mins = (_n - _w).total_seconds() / 60
    if not (delay <= mins <= delay + 5):
        return None
    msg = (getattr(cfg.urge_order, "message", "") or "").strip()
    return (msg, "") if msg else None


def r_urge_review(now, order, cfg):
    """催好评：订单已交付、过了 delay_minutes 还没评价，提醒一次。"""
    if str(order.get("state") or "") not in ("DELIVERED", "TICKETED"):
        return None
    when = _parse_iso(order.get("delivered_at"))
    if when is None:
        return None
    delay = int(cfg.urge_review.delay_minutes)
    # ★ 统一成 naive 本地再比（naive 时间 = 本地时间）
    _n = now.astimezone().replace(tzinfo=None) if now.tzinfo else now
    _w = when.astimezone().replace(tzinfo=None) if when.tzinfo else when
    mins = (_n - _w).total_seconds() / 60
    if not (delay <= mins <= delay + 5):
        return None
    msg = (getattr(cfg.urge_review, "message", "") or "").strip()
    return (msg, "") if msg else None


def _parse_iso(raw):
    """
    ISO 文本 → 带时区的 datetime。

    ★★ naive 输入按【本地时区】理解，不假设 UTC。
      为什么：库里存的 created_at / delivered_at 如果是 naive，
      那是本机时间（这台机器就在影院所在时区）。
      假设成 UTC 会让所有时间差偏 8 小时 —— 提醒全错。
    """
    if not raw:
        return None
    if isinstance(raw, _dt.datetime):
        return raw if raw.tzinfo else raw.astimezone()
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


# ★★ 提醒清单（数据驱动）：加一个提醒 = 加一行
#   每项：(名字, 判断函数, 配置里的 section 名（用哪个开关控制），去重 kind)
REMINDERS = (
    ("movie_remind", r_movie_remind, None),
    ("urge_receive", r_urge_receive, None),
    ("urge_order", r_urge_order, None),
    ("urge_review", r_urge_review, None),
)


# ==================================================================== 扫描
def build_reminder_commands(*, now=None, orders=None, cfg=None) -> list:
    """
    扫一遍订单，返回该发的提醒命令。纯函数（订单和配置都可注入）⇒ 好测。

    ★ 不去读盘、不建队列 —— 那些交给调用方。这样这个函数能单测。
    """
    # ★ 默认用【本地时区】（影院所在时区），不是 UTC
    now = now or _dt.datetime.now().astimezone()
    if cfg is None or orders is None:
        return []
    out = []
    for o in orders:
        oid = str(o.get("order_id") or "")
        if not oid:
            continue
        if str(o.get("state") or "") in ("CANCELLED", "REFUNDED"):
            continue
        ident = {
            "tenant_id": o.get("tenant_id") or "",
            "shop_id": o.get("shop_id") or "",
            "buyer_id": o.get("buyer_id") or "",
            "chat_id": o.get("chat_id") or "",
        }
        # 没有会话就发不出去（SEND_MESSAGE 要 chat_id）
        if not ident["chat_id"]:
            continue
        from copy import deepcopy
        from .shop_scope import scope, apply_settings
        with scope(ident['tenant_id'], ident['shop_id']):
            order_cfg = apply_settings(deepcopy(cfg))
        for name, fn, _ in REMINDERS:
            try:
                got = fn(now, o, order_cfg)
            except Exception:                             # noqa: BLE001
                continue
            if not got:
                continue
            text, extra_note = got
            if not str(text or "").strip():
                continue
            text = render_reminder_message(str(text), o)
            if text is None:
                continue
            cmd = Command(
                command_id=str(uuid.uuid4()),
                type="SEND_MESSAGE",
                event_id=oid,
                session_id=ident["chat_id"],
                text=str(text),
                # ★ 幂等靠 dedupe_key：同一单同一类提醒只发一次
                dedupe_key=f"reminder:{name}:{oid}",
                extra={"order_id": oid, "reminder": name,
                       "history_message_ids": []},
                **ident)
            out.append(cmd)
    return out
