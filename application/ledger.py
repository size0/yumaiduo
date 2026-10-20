"""
application/ledger.py —— 订单管理页的读模型

════════════════════════════════════════════════════════════════
★ 为什么单独一个文件

  server.py 是 HTTP 门面，它不该知道"报价记录存在哪、订单怎么查"。
  那属于应用层。所以这里提供两样东西：

    1. 进程级的 QuoteStore / OrderStore（惰性打开，指向 data/ 下的 sqlite）
    2. orders_payload() —— 界面要的那一份快照

★ 为什么 store 要放进这个模块而不是 server
    server → service → server 的循环依赖踩过一次（审计抓到的）。
    把"谁持有 store"收在应用层，server 只调用，方向就单向。

★ 为什么界面读的是【快照】而不是实时算
    报价记录和订单都是"发生过的事"，界面只展示、不推导。
    要判断"这笔还能不能用"，用的是记录自己的 is_usable()，
    不是界面重新算一遍 —— 免得两边判据不一致。
════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import store as store_module


@dataclass
class _Stores:
    quotes: object = None
    orders: object = None


_CACHE: _Stores | None = None
# ★ 显式覆盖路径（测试注入用）。
#   ★ 为什么需要：store_module.QUOTES_FILE 是【导入时】就算好的常量，
#     改 store_module.DATA 不会让它变 —— 踩过：
#     测试里改了 DATA，结果报价仍写进真的 data/quotes.sqlite3，
#     把开发数据污染了（而且当时没发现，因为读也是从同一个文件读的）。
#   ★ 所以这里给一个显式入口，不用"改全局常量"这种靠不住的办法。
_OVERRIDE = {"quotes": None, "orders": None}


def set_paths(*, quotes: str | None = None, orders: str | None = None) -> None:
    """
    覆盖两个库的路径（★ 测试用；传 None 表示恢复默认）。

    ★ 调完要 close_stores()，否则缓存里还是旧路径的连接。
    """
    _OVERRIDE["quotes"] = quotes
    _OVERRIDE["orders"] = orders


def quotes_file() -> Path:
    return Path(_OVERRIDE["quotes"] or store_module.QUOTES_FILE)


def orders_file() -> Path:
    """
    订单库路径。

    ★ 对应线上 /var/lib/ticket-system-v5/liangpiao-orders.sqlite3
      ＋ transactions.sqlite3 两处 —— 我们合成一个文件（见 domain/order/store.py
      里"为什么合表"的说明）。
    """
    return Path(_OVERRIDE["orders"] or (Path(store_module.DATA) / "orders.sqlite3"))


def stores(*, fresh: bool = False) -> _Stores:
    """
    拿进程级的两个 store（惰性打开）。

    ★ fresh=True 丢掉缓存重开 —— 测试用。
    ★ 打开失败不抛：界面要能开。返回的 store 可能是 None，
      调用方自己判（界面上显示"存储不可用"比整页打不开好）。
    """
    global _CACHE
    if _CACHE is None or fresh:
        s = _Stores()
        try:
            from domain.quote import QuoteStore
            s.quotes = QuoteStore(str(quotes_file()))
        except Exception:                                # noqa: BLE001
            s.quotes = None
        try:
            from domain.order import OrderStore
            s.orders = OrderStore(str(orders_file()))
        except Exception:                                # noqa: BLE001
            s.orders = None
        _CACHE = s
    return _CACHE


def close_stores() -> None:
    global _CACHE
    if _CACHE is not None:
        for s in (_CACHE.quotes, _CACHE.orders):
            try:
                if s is not None:
                    s.close()
            except Exception:                            # noqa: BLE001
                pass
    _CACHE = None


# ==================================================================== 读模型
def _quote_row(rec) -> dict:
    """一笔报价 → 界面要的字段。★ 金额转成元，前端不再除 100。"""
    return {
        "quote_id": rec.quote_id,
        "tenant_id": rec.tenant_id,
        "shop_id": rec.shop_id,
        "buyer_id": rec.buyer_id,
        "chat_id": rec.chat_id,
        "status": rec.status,
        "status_label": store_module.QUOTE_STATUS_LABELS.get(rec.status, rec.status),
        "provider": rec.provider,
        "request_type": rec.request_type,
        "cinema": rec.cinema or "",
        "movie": rec.movie or "",
        "show": rec.show or "",
        "seats": list(rec.selected_seats or []),
        "ticket_count": rec.ticket_count,
        "unit_cost_yuan": round(rec.unit_cost_fen / 100, 2),
        "unit_sell_yuan": round(rec.unit_sell_price_fen / 100, 2),
        "total_cost_yuan": round(rec.total_cost_fen / 100, 2),
        "total_sell_yuan": round(rec.total_sell_price_fen / 100, 2),
        "margin_total_yuan": round(rec.margin_total_fen / 100, 2),
        # ★ 是否亏本：界面上要能一眼看出来
        "is_loss": rec.margin_total_fen < 0,
        "pricing_rule_id": rec.pricing_rule_id,
        "pricing_rule_revision": rec.pricing_rule_revision,
        "created_at": rec.created_at.isoformat() if rec.created_at else "",
        "expires_at": rec.expires_at.isoformat() if rec.expires_at else "",
        # ★ 用记录自己的判据，不在界面重算
        "usable": rec.is_usable(),
        "usable_for_order": rec.is_order_ready(),
    }


def _order_row(order) -> dict:
    observation = order.payload.get('platform_payment_observation') or {}
    settlement = order.payload.get('manual_settlement') or {}
    observed = bool(observation.get('paid_at') and observation.get('amount_fen'))
    paused = bool(settlement.get('fulfillment_hold'))
    attention = observed and order.payment_state != 'CONFIRMED'
    delivery = order.payload.get('platform_delivery_sync') or {}
    delivery_unverified = order.state == 'PAID' and bool(order.shipped)
    platform_finished = (delivery_unverified and delivery.get('order_id') == order.provider_order_id
                         and type(delivery.get('status')) is int and delivery['status'] == 4)
    delivery_label = ('平台已完成（交付待核对）' if platform_finished else '已发货（交付待核对）')
    return {
        "order_id": order.order_id,
        "quote_id": order.quote_id,
        # ★★ chat_id / buyer_id 必须输出。
        #   为什么：付款信号（鱼麦多 order.paid）是【按会话】找到这一单的 ——
        #   闲鱼订单号在付款时才拿得到，下单时我们手里只有会话。
        #   原先没输出 chat_id，导致"付款落到订单上"这一步找不到单，
        #   表现为：钱收到了、订单还停在 UNPAID、票永远发不出去。
        "chat_id": order.chat_id or "",
        "buyer_id": order.buyer_id or "",
        "tenant_id": order.tenant_id or "",
        "shop_id": order.shop_id or "",
        "provider": order.provider,
        "provider_order_id": order.provider_order_id,
        "platform_order_id": order.platform_order_id,
        "state": order.state,
        "state_label": '已付款（履约暂停）' if paused else '平台已付款（待核对）' if attention else delivery_label if delivery_unverified else store_module.ORDER_STATE_LABELS.get(order.state, order.state),
        "bucket": 'attention' if attention or delivery_unverified else store_module.ORDER_STATE_BUCKETS.get(order.state, "attention"),
        "payment_state": order.payment_state,
        "payment_label": ('人工成交价已核对' if paused else '金额／履约待核对') if attention else store_module.PAYMENT_STATE_LABELS.get(order.payment_state, order.payment_state),
        "platform_paid_yuan": observation.get('amount_fen', 0) / 100 if observed else None,
        "platform_shipped_observed": observed and observation.get('platform_status') in (3, 4),
        "manual_settlement": settlement,
        "can_confirm_manual_price": bool(observed and not settlement and order.provider == 'WANDA'
            and order.payment_state == 'UNPAID' and order.payload.get('manual_ticketing')
            and not order.payload.get('upstream_created') and not order.payload.get('quote_revision_pending')
            and order.state in ('CREATED','AWAITING_PAYMENT','REPRICE_PENDING')),
        "cinema": order.cinema,
        "movie": order.movie,
        "show": order.show,
        "seats": list(order.seats or []),
        "ticket_count": order.ticket_count,
        "amount_yuan": round(order.amount_fen / 100, 2),
        "paid_yuan": round(order.paid_fen / 100, 2),
        "ticket_codes": list(order.ticket_codes or []),
        "wanda_ticket_image": order.payload.get("wanda_ticket_image"),
        "liangpiao_ticket_version": order.payload.get("liangpiao_ticket_version"),
        "liangpiao_ticket_link": order.payload.get("liangpiao_ticket_link", ""),
        "shipped": bool(order.shipped),
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else "",
        "failure_code": order.failure_code or "",
        "idempotency_key": order.idempotency_key,
        "created_at": order.created_at.isoformat() if order.created_at else "",
        "updated_at": order.updated_at.isoformat() if order.updated_at else "",
        # ★ 能不能出票/发货 —— 用订单自己的判据（四道门槛）
        "can_deliver": order.can_deliver()[0],
        "can_deliver_reason": order.can_deliver()[1],
        "needs_attention": bool(attention or delivery_unverified or order.needs_attention),
    }


def orders_payload(*, limit: int = 200, filters: dict | None = None) -> dict:
    """
    订单管理页要的全部数据。

    ★ 面里三块对应界面三个标签页：
        quotes   报价记录（走过多少价、还有效吗）
        orders   订单（卖了什么、收没收到钱、票给了没有）
        buckets  分桶计数（线上 V5 的 BUCKETS 分组）
    """
    s = stores()
    out = {
        "quotes": [],
        "orders": [],
        "buckets": {},
        "labels": {
            "order_state": store_module.ORDER_STATE_LABELS,
            "payment_state": store_module.PAYMENT_STATE_LABELS,
            "quote_status": store_module.QUOTE_STATUS_LABELS,
        },
        "storage": {
            "quotes": bool(s.quotes),
            "orders": bool(s.orders),
            "quotes_file": str(quotes_file()),
            "orders_file": str(orders_file()),
        },
        "counts": {"quotes": 0, "orders": 0},
    }

    if s.quotes is not None:
        # ★ 先落地过期状态：界面上的"有效/已过期"要跟库一致，
        #   不能只靠读取时判断（那样列表里两者长得一样）。
        try:
            s.quotes.expire_stale()
        except Exception:                                # noqa: BLE001
            pass
        rows = _all_quotes(s.quotes)
        out["quotes"] = [_quote_row(r) for r in (rows if filters is not None else rows[:limit])]
        out["counts"]["quotes"] = len(rows)

    if s.orders is not None:
        rows = _all_orders(s.orders)
        projected = [_order_row(o) for o in rows]
        if hasattr(s.orders, 'platforms'):
            from .platform_orders import merge_rows
            projected = merge_rows(projected, s.orders.platforms)
        out["orders"] = projected if filters is not None else projected[:limit]
        out["counts"]["orders"] = len(projected)
        buckets: dict = {}
        for row in projected:
            b = row['bucket']
            buckets[b] = buckets.get(b, 0) + 1
        buckets["all"] = len(projected)
        out["buckets"] = buckets

    if filters is not None:
        from .record_view import enrich, select
        enrich(out)
        kind = filters.get('kind', 'quotes')
        if kind not in ('quotes','orders'): kind = 'quotes'
        out['delivery_candidates'] = [r for r in out['orders'] if r['provider']=='WANDA' and r['payment_state']=='CONFIRMED' and not r['shipped'] and not r.get('platform_shipped_observed') and not r['wanda_ticket_image'] and r['state'] in ('PAID','AWAITING_TICKET','TICKETED')]
        out['quote_summary'] = [{k:r[k] for k in ('status','created_at','margin_total_yuan','is_loss')} for r in out['quotes']]
        scoped = select(out[kind], {**filters, 'status': 'all'}, kind)
        out['counts'][kind] = len(scoped)
        if kind == 'orders':
            out['buckets'] = {'all': len(scoped)}
            for row in scoped:
                bucket = row.get('bucket', 'attention')
                out['buckets'][bucket] = out['buckets'].get(bucket, 0) + 1
        else:
            out['quote_summary'] = [{k:r[k] for k in ('status','created_at','margin_total_yuan','is_loss')} for r in scoped]
        selected = select(scoped, filters, kind)
        try: offset = max(0, int(filters.get('offset', 0)))
        except (TypeError,ValueError): offset = 0
        out['page'] = {'total': len(selected), 'offset': offset, 'size': 50,
                       'more': offset + 50 < len(selected)}
        out[kind] = selected[offset:offset+50]
        out['orders' if kind=='quotes' else 'quotes'] = []
    return out


def _all_quotes(store) -> list:
    """
    取全部报价（新的在前）。

    ★ 为什么不按身份查：这是【运营看的后台】，要看全部。
      买家侧的报价隔离靠 by_identity，两者用途不同，别混。
    """
    with store.lock:
        rows = store.connection.execute(
            "SELECT payload FROM quote_records").fetchall()
    from domain.quote import QuoteRecord
    out = []
    for (raw,) in rows:
        try:
            out.append(QuoteRecord.from_dict(__import__("json").loads(raw)))
        except Exception:                                # noqa: BLE001
            continue
    out.sort(key=lambda r: r.created_at or __import__("datetime").datetime.min,
             reverse=True)
    return out


def _all_orders(store) -> list:
    with store.lock:
        rows = store.connection.execute(
            "SELECT payload FROM orders ORDER BY created_at DESC").fetchall()
    from domain.order import Order
    import json as _json
    out = []
    for (raw,) in rows:
        try:
            out.append(Order.from_dict(_json.loads(raw)))
        except Exception:                                # noqa: BLE001
            continue
    return out


# ==================================================================== 出票动作
def confirm_payment(order_id: str, *, online_order_id: str = "",
                    detail: dict | None = None,
                    tenant_id: str = "") -> dict:
    """Serialize payment confirmation with pending screenshot reprices."""
    s = stores()
    if s.orders is None:
        return {"ok": False, "error": "订单存储不可用"}
    from .shop_scope import scope
    order = s.orders.get(order_id)
    with scope(getattr(order, 'tenant_id', ''), getattr(order, 'shop_id', '')), s.orders.lock:
        return _confirm_payment_locked(order_id, online_order_id=online_order_id,
                                       detail=detail, tenant_id=tenant_id)


def _confirm_payment_locked(order_id: str, *, online_order_id: str = "",
                            detail: dict | None = None,
                            tenant_id: str = "") -> dict:
    """
    收到【真实闲鱼付款】后，把这笔订单标成已付款。

    ★★ 为什么必须有这一步（不能在下单时就标 CONFIRMED）：
        付款状态是【发货闸门】——
            plugin_runtime.queue_shipping_for_delivered:
                # ★ 未付款的不发（钱没到手就发货，钱收不回来）
                if order.get("payment_state") != "CONFIRMED": continue
        下单时我们还不知道买家付没付（他是在闲鱼平台付的）。
        ⇒ 只有鱼麦多推来 order.paid（插件会附上 client.orders.get 的
          真实订单详情）才能确认。

    ★ 只允许 UNPAID → CONFIRMED，【不许反向】：
        已经确认付款的单被一个迟到/伪造的事件降级成未付款，
        会让"该发货的不发" —— 比漏发更糟的是账对不上。

    ★ detail 是插件拉到的线上订单详情。有它就核一遍状态，
      没有（插件没附）也接受 —— 但记在 payload 里，事后能看出依据强弱。

    ★★★ 2026-10-14 加【严格金额校验】（店主明确要求）：
      店主原话：「必须严格校验，之前不是设计了金额校验失败取消订单？」

      设计本来就在，只是没接线：
        · 事件  AMOUNT_MISMATCH = "order.amount.mismatch"（实付与报价不符）
        · 转移  AWAIT_REPRICE / AWAIT_PAY + amount.mismatch → QT_FAIL
        · 动作  side_effect = "REFUND_AND_CLOSE"（自动退款 + 关单）
        · 文案  bubble 23「您手速太快了！订单金额校验失败，辛苦重新拍下不要付款哦」
      但这四处都没有代码去触发 —— 而买家实付金额就在
      detail["payment"]（PluginOrderView.payment，字符串·分）。

      ★ 为什么必须【严格】（分文不差），不设容差：
        改价的金额是我们按规则自己算的（店主：万达每张固定加 1 元），
        所以"买家实付"只可能等于我们报价，或者流程出错了。
        对不上就是流程问题，值得拦下来让人看一眼 ——
        而拦晚了的代价是【按错的金额出票】，票发给买家、钱收不回来。
      ★ 为什么宁可不确认付款，也不"先确认再补"：
        付款状态是发货闸门（见上面 queue_shipping_for_delivered）。
        金额不对却置了 CONFIRMED ⇒ 店家出票后系统会把票直接发出去。
      ★ 拿不到 payment 时怎么办：不动、不拦（返回 ok=True 但记 note）。
        没有证据就不能指认买家付错了 —— 记下"这次没核到金额"，
        事后能从 payload 里看出哪些单是弱证据。
    """
    s = stores()
    if s.orders is None:
        return {"ok": False, "error": "订单存储不可用"}
    order = s.orders.get(order_id)
    if order is None:
        return {"ok": False, "error": "ORDER_NOT_FOUND"}

    if (order.payload or {}).get('manual_settlement', {}).get('fulfillment_hold'):
        return {"ok": False, "error": "MANUAL_RELEASE_REQUIRED"}

    if (order.payload or {}).get("quote_revision_pending"):
        return {"ok": False, "error": "REPRICE_REVISION_PENDING"}

    if tenant_id and getattr(order, "tenant_id", "") not in ("", tenant_id):
        return {"ok": False, "error": "PAYMENT_TENANT_MISMATCH"}

    if order.payment_state == "CONFIRMED":
        # 幂等：重复的 order.paid 不报错，也不要再改
        return {"ok": True, "already": True, "order_id": order_id}

    # ★ 有线上详情就核状态：1 = 待付款，绝不能当已付款
    status = None
    if isinstance(detail, dict) and detail:
        status = detail.get("orderStatus")
        if status == 1:
            return {"ok": False, "error": "ORDER_NOT_PAID"}
        if status is not None and status not in (2, 3, 4):
            return {"ok": False, "error": f"ORDER_NOT_DELIVERABLE:{status}"}

    # ================= ★★★ 严格金额校验（店主 2026-10-14 要求）=================
    #
    #   买家实付 = detail["payment"]（PluginOrderView.payment，字符串·分）。
    #   我们的报价 = order.amount_fen（下单时按 quote.total_price_fen 落的）。
    #
    #   ★ 分文不差。不设容差 —— 见 docstring 里的理由。
    #   ★ 不符时【不置 CONFIRMED】，把差异原样返回，
    #     由调用方（plugin_runtime）推 AMOUNT_MISMATCH 事件 →
    #     状态机 QT_FAIL + side_effect=REFUND_AND_CLOSE → 自动退款关单 +
    #     发 bubble 23「您手速太快了！…辛苦重新拍下不要付款哦」。
    #   ★ 这一步【必须】在置 CONFIRMED 之前：付款状态是发货闸门，
    #     先置了就等于放行出票。
    paid_fen = None
    raw_paid = (detail or {}).get("payment") if isinstance(detail, dict) else None
    if raw_paid not in (None, ""):
        try:
            # 文档：payment 是「实付（分）」的【字符串】
            paid_fen = int(str(raw_paid).strip())
        except (TypeError, ValueError):
            paid_fen = None
    quoted_fen = int(order.amount_fen or 0)
    # ★★ 2026-10-14 接线：「设置 → 校验与下架 → 校验金额」
    #   语义（从 settings.py 的字段定义读出来的，不是我定的）：
    #     on（默认）= 金额不符就【拒绝确认付款】—— 这一条是店主的硬规矩：
    #        「必须金额校验正确，且已付款，才可以良票建单」
    #     off = 记下差异但放行（客服手工核过、或平台有活动价时用）
    #   ★ 默认 True，所以【默认行为一个字都没变】。
    #   ★ 关掉它仍然会把差异写进 payload.amount_mismatch（下面那段），
    #     所以事后对账查得出来 —— 不是"关了就瞎了"。
    try:
        import settings as _st
        _verify_amount = bool(_st.load().amount_check.verify_amount)
    except Exception:                                     # noqa: BLE001
        _verify_amount = True          # ★ 读不到配置就按最严的来（fail closed）
    if (_verify_amount and paid_fen is not None and quoted_fen
            and paid_fen != quoted_fen):
        # ★ 把差异记到订单上（事后对账/客服查证都要用），但不改 payment_state
        try:
            _p = dict(order.payload or {})
            _p["amount_mismatch"] = {
                "paid_fen": paid_fen,
                "quoted_fen": quoted_fen,
                "diff_fen": paid_fen - quoted_fen,
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "online_order_id": str(online_order_id or ""),
            }
            # ★★ 证据也要写下来 —— 这是【提前 return】的那条路，
            #   如果不在这里写，这笔"金额不符"在订单上就只剩一个差异数字，
            #   看不出当时依据有多强（有没有线上详情、有没有核到金额）。
            #   （2026-10-14 复核时发现：正常路径写了 evidence，这条路漏了。）
            _ev = {"online_order_status": status}
            _ev["paid_fen"] = paid_fen
            _ev["quoted_fen"] = quoted_fen
            _ev["amount_checked"] = "STRICT_MISMATCH"
            _p["payment_evidence"] = _ev
            order.payload = _p
            order.touch()
            s.orders.put(order)
        except Exception:                                    # noqa: BLE001
            pass
        return {
            "ok": False,
            "error": "AMOUNT_MISMATCH",
            "order_id": order_id,
            "paid_fen": paid_fen,
            "quoted_fen": quoted_fen,
            "diff_fen": paid_fen - quoted_fen,
        }
    # ======================================================================

    order.payment_state = "CONFIRMED"
    # ★ 钱到了 → 订单进入"待出票"（店家在订单管理页看得到）
    if order.state in ("AWAITING_PAYMENT", "CREATED"):
        order.state = "PAID"
    order.paid_fen = order.paid_fen or order.amount_fen
    if online_order_id:
        order.platform_order_id = str(online_order_id)
    payload = dict(order.payload or {})
    payload["payment_confirmed_at"] = datetime.now(timezone.utc).isoformat()
    payload["payment_signal"] = "yumaiduo.order.paid"
    # ★ 记下依据强弱：有真实订单详情 = 强证据；没有 = 只凭事件
    #   ★★ 2026-10-14 补上金额字段 —— 原来只记了 orderStatus，
    #     连"这次到底核没核到金额"都看不出来。
    _ev = {"online_order_status": status} if status is not None else {
        "note": "插件未附线上订单详情，仅凭 order.paid 事件"}
    if paid_fen is not None:
        _ev["paid_fen"] = paid_fen
        _ev["quoted_fen"] = quoted_fen
        _ev["amount_checked"] = "STRICT_MATCH"
    else:
        _ev["amount_checked"] = "NOT_AVAILABLE"
    if isinstance(detail, dict) and detail:
        if detail.get("quantity") is not None:
            # 文档：quantity 是闲鱼订单上的件数。★ 只记录，【不拿来算张数】
            #   店主定的纪律：闲鱼提交的订单数量不作为校验 ——
            #   买家需要多张但拍下的数量为 1 是正常的，靠改价补。
            _ev["order_quantity"] = detail.get("quantity")
        if detail.get("postFee") is not None:
            _ev["post_fee"] = detail.get("postFee")
    payload["payment_evidence"] = _ev
    order.payload = payload
    order.touch()
    s.orders.put(order)
    return {"ok": True, "order_id": order_id}


def submit_delivery(order_id: str, *, ticket_codes: list, operator: str,
                    provider_order_id: str = "", source: str = "MANUAL",
                    replaces: str = "") -> dict:
    """
    出票后台交来一次出票结果。★ 这是 UI 上"标记出票"按钮走的路。

    ★ 它必须在【同一个订单库】上操作 ——
      否则界面提交的出票结果和订单库里的不是一份。
    """
    from application.wanda_delivery import DeliveryError, DeliverySubmission, \
        WandaDelivery
    s = stores()
    if s.orders is None:
        return {"ok": False, "error": "订单存储不可用"}
    d = WandaDelivery(s.orders)
    try:
        cmd = d.submit(order_id,
                       DeliverySubmission(
                           submission_id=f"ui-{order_id}-{len(d.history(order_id))}",
                           ticket_codes=[c for c in ticket_codes if c],
                           provider_order_id=provider_order_id,
                           source=source,
                           replaces_text_command_id=replaces),
                       operator=operator)
    except DeliveryError as exc:
        return {"ok": False, "error": exc.code, "message": exc.message}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    # ★ 提交完就发出去（界面上的按钮语义是"出票并通知买家"）
    try:
        d.send(cmd.command_id)
        d.authorize_delivery(cmd.command_id, operator=operator)
        d.queue_followups(cmd.command_id)
    except DeliveryError as exc:
        return {"ok": False, "error": exc.code, "message": exc.message,
                "command_id": cmd.command_id}
    return {"ok": True, "command_id": cmd.command_id,
            "order": _order_row(s.orders.get(order_id))}
