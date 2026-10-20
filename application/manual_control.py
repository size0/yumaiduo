"""人工介入：什么时候该关闭接管。

★★ 这个模块解决的是"会话卡死在待人工"
   ConvState.MANUAL 是【终态】—— 状态机里所有事件都留在 MANUAL，
   没有任何出口。会话一旦转人工，就永远没人应答了（机器人被
   authorize_message_send 拦住，人工又未必一直盯着）。

★ 两个出口（来自设置 → 人工介入）：
    close_after_hours  接管超过这么久没动静 ⇒ 自动关闭
    close_on_status    订单到了这些状态     ⇒ 关闭
                       （默认：已发货 / 已退款 / 交易关闭 ——
                         这些状态下那边的事情已经不需要说话了）

★ "关闭接管"到底关什么（这个定义很重要，别理解错）：
    关的是【机器人要不要闭嘴】这件事，不是会话状态。
    会话状态继续停在"待人工"是对的 —— 订单还在人工那边走
    （发货、退款都是人在做）。我们要的是"机器人可以重新应答"。
  ⇒ 所以关闭 = 清掉接管标记（manual_since），状态机状态不动。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ★ 订单到了这些状态，人工介入就该关掉。
#   默认值来自 settings.OrderRefund / Manual 的字段默认 ——
#   这里再写一份是因为本模块可能被在没有配置的情况下调用（比如启动期）。
_DEFAULT_CLOSE_STATUS = ["已发货", "已退款", "交易关闭"]


def _cfg():
    try:
        import settings as _st
        return _st.load()
    except Exception:                                     # noqa: BLE001
        return None


def close_statuses(cfg=None) -> list:
    """哪些订单状态要关闭人工介入。"""
    cfg = cfg or _cfg()
    try:
        got = getattr(cfg.manual, "close_on_status", None)
        return list(got) if isinstance(got, list) else list(_DEFAULT_CLOSE_STATUS)
    except (AttributeError, TypeError):
        return list(_DEFAULT_CLOSE_STATUS)



def close_after(cfg=None) -> int:
    """接管多少小时后自动关闭。0 或负数 = 不自动关闭。"""
    cfg = cfg or _cfg()
    try:
        return int(getattr(cfg.manual, "close_after_hours", 0) or 0)
    except Exception:                                     # noqa: BLE001
        return 0


def _status_of(session) -> str:
    """订单状态（给 close_on_status 比）。取不到返回空串。"""
    try:
        order = getattr(getattr(session, "conversation", None),
                        "pending_order", None)
        for attr in ("status", "state", "order_status"):
            v = getattr(order, attr, None)
            if v:
                return str(getattr(v, "value", v))
    except Exception:                                     # noqa: BLE001
        pass
    return ""


def should_close(session, *, now: datetime | None = None,
                 cfg=None) -> bool:
    """
    这个会话的人工介入该关闭了吗。

    ★ 两条判据任一满足即可：
        ① 订单状态命中 close_on_status
        ② 接管时长超过 close_after_hours
    ★ 没记 manual_since（老数据）⇒ 用 updated_at 兜；
      两个都没有 ⇒ 用"现在"当起点（宁可多留一会儿，也不要
      因为缺字段就把接管立刻关掉 —— 那会让机器人去抢话）。
    """
    if session is None:
        return False
    now = now or datetime.now()
    from .shop_scope import scope, apply_settings
    from copy import deepcopy
    conv = getattr(session, 'conversation', None)
    with scope(getattr(conv, 'tenant_id', ''), getattr(conv, 'shop_id', '')):
        cfg = apply_settings(deepcopy(cfg)) if cfg is not None else _cfg()

    # ① 订单状态
    st = _status_of(session)
    if st and st in close_statuses(cfg):
        return True

    # ② 超时
    hours = close_after(cfg)
    if hours <= 0:
        return False                 # 不配就不自动关
    since = None
    for attr in ("manual_since", "updated_at", "created_at"):
        raw = str(getattr(session, attr, "") or "")
        if not raw:
            continue
        try:
            since = datetime.fromisoformat(raw)
            break
        except ValueError:
            continue
    if since is None:
        return False                 # 没时间依据 ⇒ 先不关（保守）
    # ★ 时区对齐：一边有时区一边没有会让减法直接抛异常。
    if since.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=since.tzinfo)
    elif since.tzinfo is None and now.tzinfo is not None:
        since = since.replace(tzinfo=now.tzinfo)
    try:
        return (now - since) >= timedelta(hours=hours)
    except TypeError:
        return False


def close(session) -> bool:
    """
    关闭人工接管：让机器人可以重新应答。

    ★ 只清标记，**不动 state**：
      state 继续停在"待人工"是对的 —— 订单还在人工那边走。
      我们要的只是"机器人可以说话了"。
    ★ 返回 True 表示"这次真的关了"（调用方可以记一条）。
    """
    if session is None:
        return False
    try:
        if not str(getattr(session, "manual_since", "") or ""):
            return False             # 本来就没人接管
        session.manual_since = ""
        return True
    except Exception:                                     # noqa: BLE001
        return False


def mark(session, *, now: datetime | None = None) -> bool:
    """
    记下"从这一刻起人工接管"。返回 True 表示这是【新】接管。

    ★ 幂等：已经在接管中的，不刷新时间 ——
      否则买家每发一句话就把超时往后推，永远不会关闭。
    """
    if session is None:
        return False
    if str(getattr(session, "manual_since", "") or ""):
        return False
    try:
        session.manual_since = (now or datetime.now()).isoformat(timespec="seconds")
        return True
    except Exception:                                     # noqa: BLE001
        return False


def due_sessions(service, *, now: datetime | None = None,
                 cfg=None) -> list:
    """所有该关闭接管的会话 id（供 handle_run_due 推进）。"""
    out = []
    try:
        items = list((getattr(service, "sessions", None) or {}).items())
    except Exception:                                     # noqa: BLE001
        return out
    for chat_id, s in items:
        try:
            state = getattr(getattr(s, "conversation", None), "state", None)
            # ★ 只看停在"待人工"的会话 —— 别的状态没有接管可关
            if getattr(state, "name", "") != "MANUAL":
                continue
            if should_close(s, now=now, cfg=cfg) and close(s):
                out.append(chat_id)
        except Exception:                                 # noqa: BLE001
            continue
    return out


def process_due(service=None, *, now: datetime | None = None, cfg=None) -> int:
    """
    把到期的接管关掉。返回关了几个。

    ★ 和退款时序同一个形状：由 handle_run_due（每秒）推进。
    ★ service 由调用方传进来 —— 本模块不 import plugin_runtime（会成环）。
    ★ 容器为空时极便宜：只是一次字典遍历。
    """
    if service is None:
        return 0
    if not close_after(cfg) and not close_statuses(cfg):
        return 0
    return len(due_sessions(service, now=now, cfg=cfg))
