"""
application/plugin_runtime.py —— 插件事件 → 命令（业务转换层）

════════════════════════════════════════════════════════════════
★ 这一层在做什么

  上层的 plugin_api 只管 HTTP；下层的 commands 只管存命令。
  中间的"这个事件该产生哪些命令"就是这里 —— 也是唯一需要
  懂业务的地方。

      插件推来 {event, tenantId, payload}
                ↓
      events_to_commands(event)  ← 本文件
                ↓
      [Command(SEND_MESSAGE, ...), Command(CHANGE_ORDER_PRICE, ...)]

★★ 最重要的一个设计决定：买家消息要过【完整的编排流程】

  买家的消息不能被当成"随便回一句"。它要走 service.on_message ——
  也就是识别截图 → 报价 → 话术风险 → 关键词 → 状态机。
  因为买家的消息可能在推进一整个订单流程。

  ⇒ events_to_commands 里对 im.message.received 的处理是：
      ① 取出文字 / 图片 URL
      ② 调 TicketService.on_message（同一套编排，不另写一条路）
      ③ 把它【要发的每条消息】各变成一条 SEND_MESSAGE 命令

  ★ 为什么"每条消息一条命令"而不是合成一条：
    V5 就是这么做的（reply_part_1/2）。分开的好处是
    每条消息各自有发送结果 —— 第一条失败不影响第二条重试，
    出问题也能精确定位是哪一条没发出去。
════════════════════════════════════════════════════════════════
"""

import uuid
from datetime import datetime, timezone

from . import store as store_module
from .plugin_api import queue
from .ticket_flow import FlowResult  # noqa: F401
from integrations.yumaiduo.commands import Command, dedupe_for


from .shop_scope import by_identity


@by_identity
def build_purchase_notice_commands(ident: dict, order, quotes) -> list[Command]:
    """Send the existing purchase notice once per placed, unpaid order."""
    import templates as tpl
    if (not _shop_allows_send(ident) or not order.provider_order_id
            or order.payment_state != "UNPAID"
            or order.state not in {"CREATED", "AWAITING_PAYMENT", "REPRICE_PENDING"}
            or any(not ident.get(k) or str(getattr(order, k, "")) != ident[k]
                   for k in ("tenant_id", "shop_id", "buyer_id", "chat_id"))):
        return []
    quote = quotes.get(order.quote_id) if quotes else None
    mode = str((getattr(quote, "metadata", None) or {}).get("ticket_mode") or "STANDARD")
    parts = tpl.render_messages("PURCHASE_NOTICE", {
        "ticket_window": ("以店家实际出票进度为准" if order.provider == "WANDA"
                          else tpl.TICKET_WINDOW.get(mode, tpl.TICKET_WINDOW["STANDARD"])),
    }, channel=str(order.provider).lower())
    text = "\n\n".join(part for part in parts if part.strip())
    if not text.strip():
        return []
    return [Command(
        command_id=str(uuid.uuid4()), type="SEND_MESSAGE",
        event_id=str(order.provider_order_id), session_id=ident["chat_id"],
        text=text, dedupe_key=f"purchase_notice:{ident['tenant_id']}:{ident['shop_id']}:{order.provider_order_id}",
        extra={"history_message_ids": _sent_message_ids(ident)}, **ident)]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ==================================================================== 服务实例
_SERVICE = {"svc": None}


def get_local_service():
    """
    进程级的编排服务（本进程内直接调用，不走 HTTP）。

    ★ 为什么不复用 server.py 的 _service：
      那样 plugin_runtime（应用层）要 import server（入口层）——
      方向反了，正是审计里"内层不许依赖外层"要抓的。

    ★ 为什么必须缓存：每次重建会丢掉会话里的报价上下文
      （conv.quote / seats），买家下一句话就接不上了。
      而且报价/订单是落盘的，缓存实例只是复用同一个写入者。
    """
    if _SERVICE["svc"] is None:
        # ★★ 2026-10-14 依赖注入：把"怎么取插件密钥"告诉 ticket_image。
        #   为什么在这里注入：本模块是【最外层】的接线处，
        #   只有它 import plugin_api 不成环。ticket_image（内层）
        #   通过注入拿到密钥 ⇒ 依赖方向只有外层→内层。
        try:
            from . import ticket_image as _ti
            from .plugin_api import plugin_secret as _ps
            _ti.set_secret_provider(_ps)
        except Exception:                                 # noqa: BLE001
            pass        # 注入失败 ⇒ 取票图上传会失败，但出票不受影响
        try:
            from .service import build_service
            cred = store_module.load_credentials() or {}
            _SERVICE["svc"] = build_service(
                tel=cred.get("tel") or "",
                app_key=cred.get("app_key") or "",
                app_secret=cred.get("app_secret") or "",
                base_url=cred.get("base_url") or None,
                persist=True)
        except Exception:                                 # noqa: BLE001
            # ★ 装配不起来也不能让插件的轮询崩 —— 返回 None，
            #   调用方跳过这个事件（下次还能重试）。
            return None
    return _SERVICE["svc"]


def reset_local_service() -> None:
    """测试/配置变更时丢掉实例，下次重新装配。"""
    _SERVICE["svc"] = None


def _identity(event: dict) -> dict:
    """从事件里取身份四元组。

    ★★ 鱼麦多 IM 事件用 `accountUnb` 表示店铺，`peerUnb` 表示买家。
      两者都必须传给回发命令；缺一个就无法正确寻址。

    ★★ 2026-10-14 补：buyer_id 要认 `peerUnb`。
      为什么：官方文档的 IM 事件 payload 里，买家标识的字段名是
      **peerUnb**（"发来消息的买家 unb"），payload 里【没有 buyerId】。
      而我们原来只认 buyerId / buyer_id ⇒ buyer_id 一直是空串。
      ★ 后果不是"少个字段"，而是**功能悄悄失效**：
        · 黑名单按 (租户, peerUnb) 查 —— 空 peerUnb 根本查不了
        · 插件回复时又用 `peerUnb: command.buyer_id`
          （见 reply-image.mjs）—— 空的 buyer_id 会让寻址失败
      ⇒ 顺位：buyerId（有些路径会带）→ peerUnb（IM 官方字段）。
        两个都试，不覆盖。
    """
    payload = event.get("payload") or {}
    return {
        "tenant_id": str(event.get("tenantId") or event.get("tenant_id") or ""),
        "shop_id": str(event.get("shopId") or event.get("shop_id")
                       or payload.get("shopId") or payload.get("shop_id")
                       or event.get("accountUnb") or event.get("account_unb")
                       or payload.get("accountUnb") or payload.get("account_unb")
                       or ""),
        "buyer_id": str(event.get("buyerId") or event.get("buyer_id")
                        or payload.get("buyerId") or payload.get("buyer_id")
                        # ★ 官方 IM payload 的买家标识字段名
                        or payload.get("peerUnb") or payload.get("peer_unb")
                        or ""),
        "chat_id": str(payload.get("chatId") or payload.get("chat_id")
                       or event.get("chatId") or event.get("chat_id") or ""),
    }


def _extract_message(payload: dict) -> tuple:
    """
    从买家消息事件里取出（文字, 图片 URL）。

    ★ 官方图片消息（messageType=2）把 URL 放在 imageUrls 数组里，
      content 也可能是同一个 URL；还兼容 imageUrl / picUrl 等单值字段。
      ★ 文字可能是 content / text / message。
    """
    text = ""
    for k in ("content", "text", "message", "msg"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    image = ""
    for k in ("imageUrl", "image_url", "picUrl", "pic_url", "imgUrl",
              "image", "url"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip().startswith("http"):
            image = v.strip()
            break
    if str(payload.get("messageType") or "") == "2":
        urls = payload.get("imageUrls")
        if not image and isinstance(urls, list):
            image = next((v.strip() for v in urls
                          if isinstance(v, str) and v.strip().startswith("http")), "")
        if not image and text.startswith("http"):
            image = text
        if image and text == image:
            text = ""
    # ★ 有的平台把图片放在 content 里（JSON 字符串）
    if not image and text.startswith("{") and "http" in text:
        import json
        try:
            obj = json.loads(text)
            for k in ("imageUrl", "image_url", "url", "picUrl"):
                v = obj.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    image = v
                    text = ""
                    break
        except (ValueError, TypeError):
            pass
    return text, image


# ==================================================================== 转换
def events_to_commands(event: dict) -> list:
    from .shop_scope import scope
    ident = _identity(event)
    with scope(ident['tenant_id'], ident['shop_id']):
        commands = _scoped_events_to_commands(event)
        history = None
        for command in commands:
            if (getattr(command, 'type', '') == 'SEND_MESSAGE' and str(command.text or '').strip()
                    and all(ident.values())
                    and all(str(getattr(command, k, '')) == v for k, v in ident.items())
                    and not isinstance(command.extra.get('history_message_ids'), list)):
                if history is None:
                    history = _sent_message_ids(ident)
                command.extra['history_message_ids'] = list(history)
        return commands


def _scoped_events_to_commands(event: dict) -> list:
    """
    一个事件 → 若干条命令。

    ★ 事件类型（插件 subscribes 里声明的，见 yumaiduo.plugin.json）：
        im.message.received   买家发来消息  ← 主流程入口
        order.paid            买家付款
        order.shipped         已发货
        order.finished        已完成
        order.refund.applied  申请退款
    """
    kind = str(event.get("event") or "")
    from .order_status_sync import is_delivery_notice, sync_delivery
    from .order_status_sync import is_close_notice
    if (kind.startswith('order.') or is_delivery_notice(event) or is_close_notice(event)
            or kind == 'im.message.received' and _is_order_card(event)):
        from . import ledger
        from .platform_orders import observe_event
        observe_event(event, ledger.stores().orders, trusted_event=True)
    if is_delivery_notice(event):
        from . import ledger
        try:note=sync_delivery(event,ledger.stores().orders)
        except Exception as exc:note='发货状态查询失败，保留本地状态待核查：'+type(exc).__name__
        return [_note_command(str(event.get('id') or event.get('eventId') or ''),_identity(event),note)]
    if kind == "im.message.received":
        # 线上会把确认收货、提醒评价等系统通知也作为 IM 事件投递。
        # 它们不是买家提问；带 orderId 的 __notify__ 更不能进订单卡片分支。
        from .order_status_sync import is_close_notice, sync_closed
        if is_close_notice(event):
            from . import ledger
            try:
                note = sync_closed(event, ledger.stores().orders)
            except Exception as exc:
                note = '关闭状态查询失败，保留本地状态待核查：' + type(exc).__name__
            return [_note_command(str(event.get('id') or event.get('eventId') or ''), _identity(event), note)]
        if _is_non_buyer_notice(event):
            return []
        # ★★ 先判【订单卡片消息】（messageType=26）——
        #   它和普通文本消息走的是同一个事件类型，但语义完全不同：
        #   普通消息是"买家在说话"，卡片消息是"买家拍下了，这是订单号"。
        #   ⇒ 必须先分流，否则卡片会被当普通消息处理，
        #     闲鱼订单号就丢了（改价、关单都要那个号）。
        if _is_order_card(event):
            return _from_order_card(event)
        return _from_buyer_message(event)
    if kind == "order.paid":
        return _from_order_paid(event)
    # 其余事件暂时只记录（不产生命令）——
    # ★ 不假装处理：没有对应行为的事件，静默丢弃比乱发命令好。
    return []



# ==================================================================== 订单卡片
# ★ 闲鱼把"买家拍下了"这件事发成一条 messageType=26 的 IM 消息，
#   消息体里带 orderId。这是我们在【付款之前】唯一能拿到闲鱼订单号的地方。
ORDER_CARD_MESSAGE_TYPE = "26"

# ★★ 订单的【已完结】状态：找"这一单还在进行中"时要排除它们。
#   ★ 为什么用具名常量而不是在几处各写一遍状态名：
#     我第一版在 _from_order_card 里写死了 ("CREATED", "AWAITING_PAYMENT")，
#     而【买家确认后】订单其实是 REPRICE_PENDING（待改价）——
#     状态名对不上 ⇒ 找不到那一单 ⇒ 又补落一张（重复落单）。
#     ★ 状态全集以后还会变，枚举法每次都得跟着改，所以判"未完结"更稳。
ORDER_CLOSED_STATES = ("DELIVERED", "CANCELLED", "REFUNDED", "FAILED")


def _is_non_buyer_notice(event: dict) -> bool:
    """Classify platform envelope types, never let notification wording reach AI.

    Delivery/closed order synchronization runs before this chat-only filter.
    Buyer text/images and order cards retain their own normal handlers.
    """
    payload = event.get("payload") or {}
    if str(payload.get("chatId") or payload.get("chat_id") or "") == "__notify__":
        return True
    mt = str(payload.get("messageType") or payload.get("message_type") or "")
    return mt in ('14', '28', '999')


def _is_order_card(event: dict) -> bool:
    """这条 IM 消息是不是「订单卡片」。
    ★ 判据：messageType == 26（闲鱼的规定）。
      另外也认 payload 里直接带 orderId 的情况（不同路径字段是并集）。
    """
    payload = event.get("payload") or {}
    mt = str(payload.get("messageType")
             or payload.get("message_type") or "").strip()
    if mt == ORDER_CARD_MESSAGE_TYPE:
        return True
    # 兼容未给类型的旧事件；带其他类型的系统通知也可能有 orderId。
    if mt:
        return False
    return bool(payload.get("orderId") or payload.get("order_id"))


def _from_order_card(event: dict) -> list:
    """
    订单卡片 → ①绑订单号 ②生成改价命令。

    ★★ 为什么改价命令在这里建（而不是买家说"确认"的时候）：
      改价要调 orders.changePrice(orderId, …)，orderId 是【闲鱼的订单号】。
      买家回"确认"时他还没拍下，那个号根本不存在。
      只有这条卡片消息带来那个号 ⇒ 命令只能在这里建。

    ★ 为什么只建一次：dedupe_key 用闲鱼订单号 ——
      同一条卡片重投（插件会重试）不会重复建命令。
    """
    payload = event.get("payload") or {}
    ident = _identity(event)
    online_order_id = str(payload.get("orderId")
                          or payload.get("order_id") or "").strip()
    if not ident["chat_id"] or not online_order_id:
        return []

    event_id = str(event.get("eventId") or event.get("event_id")
                   or payload.get("messageId") or payload.get("message_id")
                   or online_order_id)

    commands: list = []
    try:
        from . import ledger
        s = ledger.stores()
    except Exception:                                     # noqa: BLE001
        return []

    # A new platform order must never overwrite a different platform binding.
    identity_keys = ("tenant_id", "shop_id", "buyer_id", "chat_id")
    if not all(ident.get(k) for k in identity_keys):
        return []
    # A purchase notice belongs to the placed order, not to whichever quote the
    # buyer is currently discussing. Keep this independent of quote readiness.
    bound = s.orders.find_by_provider_order(online_order_id) if s.orders else None
    if bound is not None:
        if any(str(getattr(bound, k, "")) != ident[k] for k in identity_keys):
            return [_note_command(event_id, ident, "订单号身份冲突，未关联")]
        commands.extend(build_purchase_notice_commands(ident, bound, s.quotes))
    svc = get_local_service()
    sess = svc.session(ident["chat_id"]) if svc else None
    if sess is None:
        return commands
    conv = sess.conversation
    if any(str(getattr(conv, k, "")) != ident[k] for k in identity_keys):
        return [_note_command(event_id, ident, "订单卡片身份与会话不一致，未关联")]
    from .purchase_continuation import remember_card
    sess.pending_order_card = remember_card(sess, event)
    svc._save()
    latest = s.quotes.by_identity(ident) if s.quotes else []
    current = latest[0] if latest else None
    # A verified platform purchase is an explicit acceptance of the current demand.
    if bound is None:
        from .order_recovery import bind_wanda_card
        recovered = bind_wanda_card(sess, s, ident, event.get('_order_detail'))
        if recovered:
            current = s.quotes.get(recovered.quote_id)
            svc._save()
    from .request_plan import has_pending
    if (current is None or not current.is_order_ready()
            or has_pending(sess.conversation,sess.image_context)
            or sess.image_context.get("unverified_image")):
        return commands + [_note_command(event_id, ident, "已记录新拍订单，等待当前场次及张数确认；未关联旧报价")]
    target = s.orders.find_by_provider_order(online_order_id) if s.orders else None
    if target is not None and any(str(getattr(target, k, "")) != ident[k] for k in identity_keys):
        return [_note_command(event_id, ident, "订单号身份冲突，未关联")]
    if target is None and s.orders:
        candidates = [o for o in s.orders.by_identity(ident)
                      if o.quote_id == current.quote_id
                      and not o.provider_order_id and not o.platform_order_id
                      and o.state in ("CREATED", "AWAITING_PAYMENT", "REPRICE_PENDING")
                      and o.payment_state == "UNPAID"]
        if len(candidates) == 1:
            target = candidates[0]
        elif not candidates and conv.quote_id == current.quote_id:
            # Build only from current verified conversation facts. No supplier order is placed.
            out = svc.flow.on_buyer_confirm(conv)
            if out.ok:
                candidates = [o for o in s.orders.by_identity(ident)
                              if o.quote_id == current.quote_id and not o.provider_order_id
                              and not o.platform_order_id and o.payment_state == "UNPAID"]
                if len(candidates) == 1:
                    target = candidates[0]
    if target is None:
        return [_note_command(event_id, ident, "新订单暂缺可关联的当前整单报价，已保留订单卡片")]
    if target.quote_id != current.quote_id:
        return commands + [_note_command(event_id, ident, "平台订单已关联其他报价，需核验后走报价替换流程；未覆盖")]
    if (target.payment_state != "UNPAID" or target.paid_fen or target.shipped or target.ticket_codes
            or target.state not in ("CREATED", "AWAITING_PAYMENT", "REPRICE_PENDING")):
        return [_note_command(event_id, ident, "订单已付款或处理中，未改价")]
    if not target.provider_order_id:
        target.provider_order_id = online_order_id
        target.payload = dict(target.payload or {})
        target.payload.update(order_card_at=_now_iso(), order_card_event_id=event_id)
        s.orders.put(target)
    sess.pending_order_card = {}
    svc._save()

    # ---- ② 生成改价命令（目标价 = 这笔单的报价总额）----
    #   ★ 只在真有目标价时才建 —— 没价可改就不发命令（宁可不发，也不乱改）
    target_fen = 0
    if target is not None:
        target_fen = int(getattr(target, "amount_fen", 0) or 0)
    if target is None or target_fen <= 0:
        return commands

    if bound is None:
        commands.extend(build_purchase_notice_commands(ident, target, s.quotes))

    # ★★ 2026-10-14 接线：「设置 → 闲鱼改价 → 自动改价开关」
    #   off ⇒ 只把闲鱼订单号绑上（上面做完了），【不排改价命令】——
    #   订单会停在"待改价"，等店家手工改。
    #   ★ 默认 True，所以默认行为不变。
    #   ★ 为什么绑订单号这一步不受开关影响：绑定是"记事实"，
    #     不改价也要知道这个单号（关单、查单都要它）。
    try:
        import settings as _st
        if not _st.load().fish_reprice.auto_reprice:
            commands.append(_note_command(
                event_id, ident,
                "自动改价开关=关，已绑定订单号但未排改价命令（等人工改价）"))
            return commands
    except Exception:                                     # noqa: BLE001
        pass                        # 读不到配置就照旧自动改（保持原行为）

    # Only a current full-order quote may authorize an order-card reprice.
    bound_quote = s.quotes.get(target.quote_id) if target is not None and s.quotes else None
    latest_quotes = s.quotes.by_identity(ident) if s.quotes else []
    unit_pending = bool(latest_quotes and latest_quotes[0].metadata.get("quote_kind") == "UNIT_PRICE")
    if (target_fen <= 0 or bound_quote is None or not bound_quote.is_order_ready()
            or bound_quote.total_sell_price_fen != target_fen or unit_pending):
        commands.append(_note_command(event_id, ident, "尚无可用于改价的整单报价，等待确认张数或更新报价"))
        return commands

    commands.append(Command(
        command_id=str(uuid.uuid4()),
        type="CHANGE_ORDER_PRICE",
        event_id=online_order_id,          # ★ 授权时 order_id 取的就是 event_id
        session_id=ident["chat_id"],
        dedupe_key=f"reprice:{online_order_id}",
        extra={"target_amount_fen": target_fen,
               "order_id": online_order_id},
        **ident))
    return commands


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def _shop_allows_send(ident: dict) -> bool:
    """
    这个店铺允许我们自动发消息吗（店主在「店铺管理」页的开关）。

    ★★ 为什么闸门加在【这里】（产生命令的地方），而不是插件执行时：
      插件执行时拦只能拦住"发出去"，而这边的命令还是会一条条排进队列、
      一条条判失败 —— 队列里堆一堆必然失败的 SEND_MESSAGE，
      出问题时根本看不出"是这个店的开关关了"。
      ⇒ 在产生处就拦掉，队列干净、日志也干净。

    ★ 它和插件里那个 LIVE_SEND_ALLOWLIST 不是一回事：
      插件那个是"真发模式下的安全闸门"（防止误开 live 到处发）；
      这个是"店主的业务开关"（这个店在放假/整改，别自动发言）。
      两层都要有：一个防事故，一个给运营用。

    ★ 读不到配置时返回 True（照常发）：
      配置故障就静默静音全部店铺，是比"多发了消息"严重得多的故障。
    """
    try:
        from . import store as _store
        return bool(_store.shop_send_enabled(ident.get("shop_id") or ""))
    except Exception:                                     # noqa: BLE001
        return True


def _note_command(event_id: str, ident: dict, text: str) -> Command:
    """只用来记账的命令（不进买家视线）。

    ★ 用 SEND_MESSAGE 类型但 text 为空、把说明放 extra.note ——
      这里【故意不发明细给买家】：绑定成功/失败是内部记账，
      买家不该看到"已绑定闲鱼订单号"这种话。
    """
    return Command(
        command_id=str(uuid.uuid4()),
        type="SEND_MESSAGE",
        event_id=event_id,
        session_id=ident.get("session_id") or "",
        text="",
        dedupe_key=f"note:{event_id}",
        extra={"note": text, "internal_only": True},
        **ident)


def _from_buyer_message(event: dict) -> list:
    """
    买家消息 → 走完整编排 → 把要回的每条消息变成命令。

    ★★ 这是"真实数据进来"的那一步：
      识别截图、算报价、写报价记录，全都发生在 on_message 里面。
    """
    payload = event.get("payload") or {}
    ident = _identity(event)
    text, image = _extract_message(payload)
    if not text and not image:
        return []

    event_id = str(event.get("eventId") or event.get("event_id")
                   or payload.get("messageId") or payload.get("message_id")
                   or uuid.uuid4().hex)
    session = ident["chat_id"]

    # ---- 调编排层（和 HTTP 的 /api/message 走同一条路）----
    # ★ get_local_service 就在本模块（不是 service.py）——
    #   踩过：写成 `from .service import get_local_service`，
    #   ImportError 让每个买家消息事件都处理失败。
    svc = get_local_service()
    if svc is None:
        return []
    previous_quote_id = (getattr(svc.session(session).conversation, "quote_id", "")
                         if image else "")
    try:
        result = svc.on_message(session, text=text, image_url=image,
                                city_name=payload.get("cityName")
                                or payload.get("city_name"),
                                channel=payload.get("channel"),
                                tenant_id=ident.get("tenant_id") or "",
                                # ★★ 2026-10-14：黑名单要按 (店铺, 买家 unb) 查。
                                #   peerUnb 是官方 IM payload 里的买家标识
                                #   （见 _identity 的注释）；没有它就只能不查。
                                shop_id=ident.get("shop_id") or "",
                                peer_unb=ident.get("buyer_id") or "",
                                remote_message_id=str(payload.get('remoteMessageId') or payload.get('messageId') or ''))
    except Exception:                                     # noqa: BLE001
        # ★ 编排抛异常不能把插件的轮询打断 —— 抛出去的话
        #   run-due 整个失败，之前处理过的事件也跟着回滚。
        #   记一条失败事件，下次重试。
        raise

    # ---- 把要发的消息各变成一条命令 ----
    commands = []
    # ★★★ 2026-10-14 店主定的：店铺级「自动发送」开关。
    #   关掉某个店 ⇒ 不往那个店发任何自动消息（包括 AI 回复和报价卡）。
    #   ★ 拦在【产生处】：命令不排队，队列和日志都干净；
    #     在插件执行时拦只会堆一堆必然失败的命令。
    #   ★ 记账命令（_note_command）仍然照发 —— 那是内部记录，
    #     不是对买家发言，关掉开关不该让我们的排查也瞎掉。
    _may_send = _shop_allows_send(ident)
    if not _may_send:
        commands.append(_note_command(
            event_id, ident,
            f"店铺 {ident.get('shop_id') or '(未知)'} 的「自动发送」是关闭的，"
            f"本次不发送任何自动消息"))
    # ★★ 插件发消息前会查重（message-history.mjs:25）：
    #   它要求 command.history_message_ids 是【数组】，
    #   不是数组就直接 SUPPRESSED（不发）。
    #   ⇒ 所以每条 SEND_MESSAGE 都必须带上这个字段。
    #   ★ 值从哪来：以前发出过的消息 id（按这个买家查）。
    history = _sent_message_ids(ident)
    revision_commands = []
    if (getattr(result, "ok", False) and not getattr(result, "need_human", False)
            and any(k in ("QUOTE_CARD", "QUOTE_CARD_MULTI")
                    for k in getattr(result, "template_keys", []))):
        quote_id = getattr(svc.session(session).conversation, "quote_id", "")
        if quote_id and quote_id != previous_quote_id:
            import settings as _st
            pending_card = svc.session(session).pending_order_card
            if pending_card:
                revision_commands = _from_order_card(pending_card)
            elif _st.load().fish_reprice.auto_reprice:
                from . import ledger
                from .quote_reprice import build_quote_reprice
                revision_commands = build_quote_reprice(ledger.stores(), ident, quote_id)
    # A replacement order gets its single notice after verified success.
    messages = [] if any(c.type == "CHANGE_ORDER_PRICE" for c in revision_commands) else list(getattr(result, "messages", None) or [])
    commands.extend(revision_commands)
    for i, body in enumerate(messages):
        if not isinstance(body, str) or not body.strip():
            continue
        if not _may_send:
            break                     # 开关关了 ⇒ 一条都不排
        part = f"reply_part_{i + 1}" if len(messages) > 1 else "reply"
        commands.append(Command(
            command_id=str(uuid.uuid4()),
            type="SEND_MESSAGE",
            event_id=event_id,
            session_id=session,
            text=body,
            dedupe_key=dedupe_for(event_id, kind="reply", part=part),
            extra={"history_message_ids": history},
            **ident))
    # ---- ★★ 2026-10-14 编排层挂上来的命令（如取票图）----
    #   ticket_flow 是【领域层】，不许 import plugin_api（会成环），
    #   所以它只把命令挂到 result.pending_commands 上，由这里统一入队。
    #   ★ 这些命令自带 dedupe_key（如 ticket_image:<单号>），
    #     所以不用再套 dedupe_for —— 套了会覆盖掉它自己的幂等键。
    for _pc in (getattr(result, "pending_commands", None) or []):
        commands.append(_pc)
    # ---- 状态机要求的动作也要变成命令 ----
    side = getattr(getattr(result, "side_effect", None), "value", None) \
        or getattr(result, "side_effect", None)
    if side == "REPRICE":
        # ★★★ 2026-10-14 补上 target_amount_fen —— 不补的话这条命令【必然被拒】。
        #
        #   链路：命令 → 插件 reprice.mjs → POST /authorize-execution →
        #         后端 authorize_execution 要回 target_amount_fen
        #   （插件判据：!Number.isSafeInteger(target_amount_fen) → 直接 GATE_REJECTED）
        #   ⇒ 没有目标价，插件连改价接口都不会调。
        #
        #   ★ 目标价取【报价总额】—— 就是买家应该付的钱。
        #     transportFee 由插件固定传 0（包邮），见 reprice.mjs:145。
        _target = int(getattr(result.quote, "total_price_fen", 0) or 0)
        commands.append(Command(
            command_id=str(uuid.uuid4()),
            type="CHANGE_ORDER_PRICE",
            event_id=event_id, session_id=session,
            dedupe_key=dedupe_for(event_id, kind="reprice"),
            extra={"target_amount_fen": _target} if _target > 0 else {},
            **ident))
    elif side == "REFUND_AND_CLOSE":
        # ★★★ 2026-10-14：这条动作【早就写在状态机里】，但没人执行它。
        #   触发它的两个转移：
        #     (IDLE, order.paid.noquote)                  ← 没报价就付款（bubble 19）
        #     (AWAIT_REPRICE/AWAIT_PAY, order.amount.mismatch) ← 金额不符（bubble 23）
        #   两处的意图都是「关掉这张单，让买家重来」。
        #
        #   ★★ 关单命令【不在这里发】，而是由 _from_order_paid 发。
        #      为什么：关单要的是【闲鱼订单号】（插件调
        #      client.orders.cancel(orderId, …)），而那个号只有
        #      付款事件里才有（event.payload.orderId / target.platform_order_id）。
        #      这条路上能拿到的只有【我们自己的单号】result.order_no，
        #      拿它去关闲鱼的单会是一个不存在的单号。
        #      ⇒ 这里只记一条显眼的 note，让人知道这单要关。
        #   ★ 那状态下推进/文案呢：照旧由状态机的 reply（bubble 19/23）给，
        #     不在这里拼。
        #
        #   ★ 为什么留一个 pass 而不是整段删掉：删了这个 side_effect 就
        #     变成"没人认识"，将来读代码的人会以为它已实现。留着，
        #     真正的实现放在 _from_order_paid 里（那里才有闲鱼订单号）。
        pass
    return commands


def sync_repriced_conversation(command: Command) -> None:
    """Advance only the matching live quote after its verified revision."""
    from copy import deepcopy
    from domain.conversation import ConvState, Event
    from . import ledger
    from .quote_reprice import revision_conversation_matches
    order = ledger.stores().orders.find_by_provider_order(command.event_id)
    svc = get_local_service()
    session = svc.sessions.get(command.chat_id) if svc else None
    if not order or not session:
        return
    conv = session.conversation
    if not revision_conversation_matches(order, deepcopy(conv)):
        return
    if conv.state == ConvState.QUOTED:
        conv.apply(Event.BUYER_ORDERED)
    if conv.state == ConvState.AWAIT_REPRICE:
        conv.apply(Event.ORDER_REPRICED)
    conv.pending_out_order_no = order.payload.get("out_order_no") or order.order_id
    if svc.persist:
        svc._save()


def _sent_message_ids(ident: dict) -> list:
    """
    这个买家【已经发出过】的消息 id。

    ★ 插件用它做发送前查重（避免重复发同一句）。
      查不到就返回空数组 —— ★ 必须是数组，不能是 None，
      否则插件直接 SUPPRESSED。
    """
    try:
        q = queue()
        out = []
        for cmd in q.by_status("SENT"):
            if all(cmd.to_dict().get(k) == ident.get(k)
                   for k in ("tenant_id", "shop_id", "buyer_id", "chat_id")):
                if cmd.sent_message_id:
                    out.append(cmd.sent_message_id)
        return out
    except Exception:                                     # noqa: BLE001
        return []


@by_identity
def build_revision_unconfirmed_commands(command: Command, order, history: list) -> list[Command]:
    """Report an unresolved revision without claiming any transaction outcome."""
    import templates as tpl
    if (order is None or command.status not in {"FAILED", "UNKNOWN", "GATE_REJECTED"}
            or not command.extra.get("replacement_quote_id")
            or str(order.provider_order_id) != command.event_id
            or any(str(getattr(order, k, "")) != getattr(command, k)
                   for k in ("tenant_id", "shop_id", "buyer_id", "chat_id"))
            or order.payment_state != "UNPAID"
            or order.state not in {"CREATED", "AWAITING_PAYMENT", "REPRICE_PENDING"}
            or order.quote_id != command.extra.get("previous_quote_id")
            or not tpl.text_for("REPRICED_AGAIN", str(order.provider).lower()).strip()):
        return []
    pending = (order.payload or {}).get("quote_revision_pending") or {}
    if pending and pending.get("command_id") != command.command_id:
        return []
    return [Command(
        command_id=str(uuid.uuid4()), type="SEND_MESSAGE",
        event_id=command.event_id, session_id=command.session_id,
        tenant_id=command.tenant_id, shop_id=command.shop_id,
        buyer_id=command.buyer_id, chat_id=command.chat_id,
        text="本次新截图的改价尚未确认完成，请先不要付款或重复下单，需店家核对订单后再确认。",
        dedupe_key=f"reprice_unconfirmed:{command.command_id}",
        extra={"history_message_ids": list(history)})]


@by_identity
def build_reprice_success_commands(command: Command, order, quote,
                                   history_message_ids: list) -> list[Command]:
    """Build the buyer notice only after the plugin has verified the new price."""
    if (str(getattr(order, "provider_order_id", "")) != command.event_id
            or str(getattr(order, "tenant_id", "")) != command.tenant_id
            or str(getattr(order, "shop_id", "")) != command.shop_id
            or str(getattr(order, "buyer_id", "")) != command.buyer_id
            or str(getattr(order, "chat_id", "")) != command.chat_id
            or type(command.extra.get("target_amount_fen")) is not int
            or order.amount_fen != command.extra["target_amount_fen"]):
        raise ValueError("REPRICE_NOTICE_ORDER_MISMATCH")

    # A platform price change does not renew the supplier quote. In particular,
    # never invite payment on an order rebound to an expired quote.
    if quote is not None:
        if str(quote.status) == "EXPIRED":
            return []
        expiry = getattr(quote, "expires_at", None)
        if quote.provider != "WANDA" and expiry is not None and expiry <= datetime.now(expiry.tzinfo):
            return []

    amount = command.extra["target_amount_fen"]
    import templates as tpl
    channel = str(getattr(quote, "provider", "") or getattr(order, "provider", "")).lower()
    template_key = ("REPRICED_AGAIN" if command.extra.get("replacement_quote_id")
                    else "REPRICED_CHECK")
    if not tpl.text_for(template_key, channel).strip():
        return []
    fallback = (f"已为您改价至 {amount / 100:.2f} 元，请核对订单中的影院、影片、"
                "场次和座位，确认无误后再付款。付款后不支持退改签。")
    messages = [fallback]
    if (quote is not None and quote.quote_id == order.quote_id
            and quote.total_sell_price_fen == amount
            and all(str(getattr(quote, k, "")) == getattr(order, k, "")
                    for k in ("tenant_id", "shop_id", "buyer_id", "chat_id"))):
        meta = quote.metadata or {}
        values = {
            "city": str(meta.get("city_name") or "").strip(),
            "cinema": str(quote.cinema or "").strip(),
            "movie": str(quote.movie or "").strip(),
            "hall": str(meta.get("hall_name") or "").strip(),
            "show_time": str(quote.show or "").strip(),
            "seats": "、".join(quote.selected_seats or []),
            "cinema_addr": str(meta.get("cinema_address") or "").strip(),
            "total_price": f"{amount / 100:.2f}",
            "unit_price": f"{amount / max(1, getattr(quote, 'ticket_count', 1)) / 100:.2f}",
            "count": str(getattr(quote, "ticket_count", 1)),
        }
        if all(values.values()):
            rendered = tpl.render_messages(template_key, values,
                                           channel=channel)
            if rendered and not any("{" in text or "}" in text
                                    for text in rendered):
                # One command keeps the full check together. Separate pending
                # parts can suppress each other at authorize-message-send.
                messages = ["\n\n".join(rendered)]

    return [Command(
        command_id=str(uuid.uuid4()), type="SEND_MESSAGE",
        event_id=command.event_id, session_id=command.session_id,
        tenant_id=command.tenant_id, shop_id=command.shop_id,
        buyer_id=command.buyer_id, chat_id=command.chat_id,
        text=text, dedupe_key=f"reprice_notice:{command.command_id}:{i}",
        extra={"history_message_ids": list(history_message_ids)})
        for i, text in enumerate(messages)]


def _from_order_paid(event: dict) -> list:
    """
    买家付款。

    ★ 万达流程的关键一步：付款之后【不能自动出票】——
      要店家在出票后台手动出票。所以这里只生成一句
      "已确认付款，这单需要店家处理出票"的通知。
      （线上真实话术见 V5 application/order_replies.py 的 PAYMENT_MANUAL）

    ★★ 但付款这件事必须【落到订单上】：
      下单时我们标的是 payment_state=UNPAID（还不知道他付没付），
      而付款状态是【发货闸门】——
          queue_shipping_for_delivered: 未付款的不发
      ⇒ 这里要把它置成 CONFIRMED，否则店家出了票也永远发不出去。

    ★ 线上订单详情（插件附的 event._order_detail，来自
      client.orders.get(orderId)）用来核状态：orderStatus==1 是待付款，
      绝不能当已付款 —— 钱没到手就出票/发货，钱收不回来。
    """
    payload = event.get("payload") or {}
    ident = _identity(event)
    event_id = str(event.get("eventId") or event.get("event_id")
                   or payload.get("orderId") or payload.get("order_id")
                   or uuid.uuid4().hex)
    online_order_id = str(payload.get("orderId")
                          or payload.get("order_id") or "")
    from . import ledger
    from .order_recovery import recover_identity, bind_wanda_card
    stores = ledger.stores()
    service = get_local_service()
    if not ident['chat_id'] or not ident['buyer_id']:
        resolved = recover_identity(event, ident, stores, service)
        if resolved is None:
            return [_note_command(event_id, ident, '付款订单未能唯一关联买家，保留待核查；未确认付款或出票')]
        ident = resolved
    session = getattr(service, 'sessions', {}).get(ident['chat_id'])
    if stores.orders.find_by_provider_order(online_order_id) is None and session is not None:
        bind_wanda_card(session, stores, ident, event.get('_order_detail'), paid=True)
        service._save()

    # ---- 把付款落到订单上（★ 只在能找到单时才动）----
    #   查找顺序：
    #     ① 之前记过这个闲鱼单号 → 直接命中（最精确）
    #     ② 用会话找我们自己的单（买家下单时记的 chat_id）
    detail = event.get("_order_detail") or payload.get("_order_detail") or None
    # ★ 编排层（TicketFlow）要先拿到 —— 付款确认后要它建单/出文案
    svc = get_local_service()
    note = ""
    # ★ 付款到底落上没有。金额不符时 confirm_payment 会拒，
    #   那种情况【绝不能】建单 —— 这是店主定的那条时序的前半句。
    payment_ok = False
    target = None
    try:
        from . import ledger
        s = ledger.stores()
        if s.orders is not None and online_order_id:
            target = s.orders.find_by_provider_order(online_order_id)
        note_extra = ""
        if target is None and s.orders is not None:
            # ★★ 用会话找未付款的万达单，取【最近一笔】。
            #   为什么取最近而不是第一笔：
            #     买家改了场次/座位会再下一单，上一单可能还没取消 ——
            #     这时会有两笔未付款。店主说"改一个即可"，
            #     而真正付的是他刚改的那一单（最近的那笔）。
            #     orders_payload 已按 created_at 倒序，取第一个即可。
            #   ★ 但把候选数记下来 —— 有多笔时事后能看出我们选了哪笔。
            cands = [
                r for r in (ledger.orders_payload().get("orders") or [])
                if (r.get("chat_id") == ident["chat_id"]
                    # ★★ 2026-10-14：原来写死 provider == "WANDA"，
                    #   而店主定的时序改了之后【良票也有待付款单】
                    #   （确认时不建上游单，只落本地待付款记录）。
                    #   写死万达会让良票的付款找不到单 → 状态落不上、
                    #   建单也不会发生。所以两条渠道都要认。
                    and (not online_order_id or r.get('provider_order_id') == online_order_id)
                    and all(str(r.get(k) or '') == ident[k] for k in ('tenant_id','shop_id','buyer_id'))
                    and r.get("provider") in ("WANDA", "LIANGPIAO")
                    and r.get("payment_state") != "CONFIRMED"
                    and r.get("state") not in ("CANCELLED", "REFUNDED",
                                               "DELIVERED"))]
            if cands:
                target = s.orders.get(str(cands[0].get("order_id") or ""))
                if len(cands) > 1:
                    others = [str(c.get("order_id")) for c in cands[1:]]
                    note_extra = (f"（同一会话 {len(cands)} 笔未付款单，"
                                  f"按最近一笔处理；其余 {others}）")
        if target is not None:
            from .payment_sync import observe
            if all(str(getattr(target, key)) == ident[key] for key in ('tenant_id','shop_id','buyer_id','chat_id')):
                observe(s.orders, target, detail, source='order.paid')
                if getattr(target, 'provider', '') == 'WANDA':
                    from .payment_sync import facts
                    payment = facts(target, detail, 'order.paid')
                    if payment and payment['amount_fen'] != target.amount_fen:
                        return [_note_command(event_id, ident, '平台已付款，成交金额与自动报价不同，保留付款事实并等待人工确认；不自动退款或履约')]
            if (target.payload or {}).get('manual_settlement', {}).get('fulfillment_hold'):
                return [_note_command(event_id, ident, '人工成交价已登记，履约保持暂停；不重放付款后操作')]
            if (target.payload or {}).get('recovered_from_order_card'):
                from .order_recovery import verified_payment
                if not verified_payment(target, detail):
                    return [_note_command(event_id, ident, '付款状态、身份或实付金额未核实一致，暂停处理；未改价或退款')]
            if (target.payload or {}).get("quote_revision_pending"):
                return [_note_command(event_id, ident,
                                      "改价结果尚未确认，付款事件保留待人工核查，暂停出票")]
            res = ledger.confirm_payment(
                target.order_id, online_order_id=online_order_id,
                detail=detail if isinstance(detail, dict) else None,
                tenant_id=str(event.get("tenantId") or ""))
            if res.get("error") == "REPRICE_REVISION_PENDING":
                return [_note_command(event_id, ident,
                                      "改价结果尚未确认，付款事件保留待人工核查，暂停出票")]
            if res.get('already') and (target.payload or {}).get('recovered_from_order_card'):
                return [_note_command(event_id, ident, '付款已登记，不重复处理或发送')]
            if res.get("already") and (target.payload or {}).get("quote_revisions"):
                return [_note_command(event_id, ident,
                                      "此换价订单付款已登记，不重复建单；未完成履约需人工核查")]
            if res.get("error") == "AMOUNT_MISMATCH":
                # ★★★ 实付与报价不符（店主 2026-10-14：「必须严格校验」）。
                #
                #   这件事的设计【早就在状态机里】：
                #     (AWAIT_REPRICE | AWAIT_PAY, amount.mismatch) → {
                #         target: QT_FAIL,
                #         reply:  AMOUNT_MISMATCH,        ← bubble 23 文案
                #         side_effect: REFUND_AND_CLOSE,  ← 自动退款 + 关单
                #     }
                #   缺的只是"谁来推这个事件" —— 现在这里推。
                #
                #   ★ 为什么走状态机而不是在这里直接调 ch5_cancel：
                #     状态机已经声明了「金额不符该做什么」（目标态 + 文案 + 动作），
                #     再在调用方写一遍会出现两处不一致、
                #     以及"到底谁负责退款"的分叉。
                #   ★ 文案也不自己拼 —— 事件的 reply 会带出 bubble 23。
                #
                #   ★ 返回值：本函数返回的是"要发的消息命令"，
                #     而这个事件要落成【状态机流转】。所以把事件挂在第一条
                #     命令的 extra 上，由调用方（handle_event）取出来推给会话。
                #     为什么不在这里直接改会话：本函数的职责是"事件 → 命令"，
                #     会话状态的唯一权威在 conversation.apply()。
                # ★★ 两条命令，因为它们要做两件不同的事：
                #     ① SEND_MESSAGE（带 state_event）→ 推进状态机 + 发 bubble 23
                #     ② CLOSE_FAILED_ORDER → 真的把这张错价单关掉
                #   ★ 关单命令【必须在这里发】而不是在 _from_buyer_message 里：
                #     插件调 client.orders.cancel(orderId, …) 要的是
                #     【闲鱼订单号】，那个号只有付款事件里才有。
                return [
                    Command(
                        command_id=str(uuid.uuid4()), type="SEND_MESSAGE",
                        event_id=event_id, session_id=ident["chat_id"],
                        text="",                  # ★ 文案由状态机的 reply 给
                        dedupe_key=dedupe_for(event_id, kind="amount_mismatch"),
                        extra={
                            "amount_mismatch": True,
                            "state_event": "order.amount.mismatch",
                            "paid_fen": res.get("paid_fen"),
                            "quoted_fen": res.get("quoted_fen"),
                            "diff_fen": res.get("diff_fen"),
                            "order_id": target.order_id,
                            # ★ 闲鱼订单号也给出去 —— 关单命令和排查都要用
                            "platform_order_id": online_order_id,
                            "payment_note": (
                                f"★★★ 金额校验失败（严格）：实付 "
                                f"{res.get('paid_fen')} 分"
                                f" ≠ 报价 {res.get('quoted_fen')} 分"
                                f"（差 {res.get('diff_fen')} 分）"
                                f" → 未标已付款；关单 + 让买家重拍（bubble 23）"),
                        },
                        **ident),
                    Command(
                        command_id=str(uuid.uuid4()), type="CLOSE_FAILED_ORDER",
                        event_id=event_id, session_id=ident["chat_id"],
                        text="订单金额与报价不符，本单关闭后请重新下单",
                        dedupe_key=dedupe_for(event_id, kind="close_failed_order"),
                        extra={"order_id": online_order_id,
                               "booking_order_id": target.order_id,
                               "side_effect": "REFUND_AND_CLOSE"},
                        **ident),
                ]
            if res.get("ok"):
                payment_ok = True
                note = "付款已落到订单 " + target.order_id + note_extra
            else:
                note = f"★ 付款落库失败：{res.get('error')}"
        else:
            note = "★ 没找到对应的本地订单，付款状态没落上（人工核一下）"
    except Exception as exc:                                  # noqa: BLE001
        note = f"★ 付款落库异常：{type(exc).__name__}: {exc}"

    # ★★★ 2026-10-14：付款之后要【真的做事】，不能只发一句通知。
    #
    #   店主定的时序：「必须金额校验正确，且已付款，才可以良票建单」。
    #   ⇒ 确认时（on_buyer_confirm）已经不建上游单了，
    #     所以 ch5_create 只能发生在这里。
    #   ★ 之前这个函数的结尾只是拼一句"已确认付款，这单需要店家处理出票"
    #     —— 那是万达的语义（店家手动出票）。良票没有这一步就会
    #     【永远不建单】：买家付了钱、我们什么都没下给上游。
    #
    #   ★ 三件事按顺序：
    #     ① 付款确实落上了（res.ok）—— 金额不符时 confirm_payment 会拒，
    #        那种情况绝不建单
    #     ② 拿到这个会话的 Conversation（FlowResult 要靠它出文案）
    #     ③ 调 flow.on_payment_confirmed —— 它自己做最后两道门
    #        （金额/张数）再决定建不建
    cmds: list = []
    flow = getattr(svc, "flow", None) if svc is not None else None
    conv = None
    if flow is not None and target is not None:
        try:
            conv = svc.session(target.chat_id).conversation
            if getattr(target, 'provider', '') == 'WANDA' and (target.payload or {}).get('recovered_from_order_card'):
                from .order_recovery import payment_conversation
                conv = payment_conversation(target, s.quotes)
        except Exception:                                    # noqa: BLE001
            conv = None
    template_silent = False
    if (flow is not None and conv is not None
            and payment_ok and target is not None):
        if (target.payload or {}).get("quote_revisions"):
            from copy import deepcopy
            from .quote_reprice import revision_conversation_matches
            conv = deepcopy(conv)
            if not revision_conversation_matches(target, conv):
                return [_note_command(event_id, ident,
                                      "已确认付款；会话报价与已付款订单版本不一致，暂停自动出票待核查")]
        try:
            paid_fen = None
            if isinstance(detail, dict) and detail.get("payment") not in (None, ""):
                try:
                    paid_fen = int(str(detail["payment"]).strip())
                except (TypeError, ValueError):
                    paid_fen = None
            fr = FlowResult(
                channel=getattr(conv, "channel", None))
            flow.on_payment_confirmed(conv, fr, paid_fen=paid_fen)
            note = (note + " ｜ " if note else "") + \
                "已按付款后时序处理（良票建单 / 万达等店家出票）"
            messages = list(getattr(fr, "messages", None) or [])
            template_silent = bool(fr.template_keys) and not messages
            history = _sent_message_ids(ident)
            for i, body in enumerate(messages):
                if not isinstance(body, str) or not body.strip():
                    continue
                cmds.append(Command(
                    command_id=str(uuid.uuid4()), type="SEND_MESSAGE",
                    event_id=event_id, session_id=ident["chat_id"],
                    text=body,
                    dedupe_key=dedupe_for(event_id, kind="paid_reply",
                                          part=(i + 1)),
                    extra={"history_message_ids": history},
                    **ident))
            for n in (getattr(fr, "notes", None) or []):
                if isinstance(n, str) and n.strip():
                    note = (note + " ｜ " if note else "") + n
        except Exception as exc:                              # noqa: BLE001
            # ★ 建单/编排抛异常不能让事件处理整个失败（否则轮询回滚、
            #   这个付款事件会反复重放）。记下来让人看见。
            note = (note + " ｜ " if note else "") + \
                f"★★★ 付款后处理异常：{type(exc).__name__}: {exc}"

    if not cmds and template_silent:
        cmds = [_note_command(event_id, ident, "此付款步骤的回复文案已留空，不发送消息")]
    if not cmds:
        # 兜底：编排没给出文案（或没接上）时，至少回一句
        cmds = [Command(
            command_id=str(uuid.uuid4()), type="SEND_MESSAGE",
            event_id=event_id, session_id=ident["chat_id"],
            text=("已确认付款，这单需要店家处理出票，请勿重复付款。" if payment_ok
                  else "平台付款信息已收到，但购票信息尚未关联核实，需要店家核对；请勿重复付款。"),
            dedupe_key=dedupe_for(event_id, kind="paid_notice"),
            **ident)]
    if note:
        # ★ 把落库/建单结果附在第一条命令的 extra 上 —— 出问题时能追
        extra = dict(cmds[0].extra or {})
        extra["payment_note"] = note
        cmds[0].extra = extra

    # ---- ★★★ 2026-10-14 店铺级「自动发送」开关：统一收口 ----
    #   ★ 为什么在这里收口、而不是在三个产生点各写一次：
    #     这个函数里有三处产生 SEND_MESSAGE（金额不符通知、付款后回复、
    #     兜底提示）。逐处加判断迟早漏一处 —— 而"漏一处"的后果是
    #     关掉开关后买家仍然收到消息，且看起来开关是生效的。
    #   ★ 只挡「对买家发言」的命令（有 text 的 SEND_MESSAGE）：
    #     · 内部记账命令（text 为空、extra.internal_only）保留 ——
    #       关掉开关不该让我们的排查也瞎掉
    #     · 非 SEND_MESSAGE 命令（如 CLOSE_FAILED_ORDER）保留 ——
    #       关的是"说话"，不是"止损"。错价单该关还是要关。
    if not _shop_allows_send(ident):
        _kept = [c for c in cmds
                 if not (getattr(c, "type", "") == "SEND_MESSAGE"
                         and str(getattr(c, "text", "") or "").strip())]
        if len(_kept) != len(cmds):
            _kept.append(_note_command(
                event_id, ident,
                f"店铺 {ident.get('shop_id') or '(未知)'} 的「自动发送」是关闭的，"
                f"拦下 {len(cmds) - len(_kept)} 条对买家的消息"))
        cmds = _kept
        if not cmds:
            return []
    return cmds


# ==================================================================== 发货
def queue_shipping_for_delivered() -> int:
    """
    已出票的订单 → 排 SHIP_ORDER + TICKET_DELIVERY_NOTICE 命令。

    ★ 这就是"出票后自动发货"：
      店家/系统把取票码交进来 → 订单有了取票码 → 这里把后续两件事排上。

    ★★ 判据为什么是「有取票码」而不是「state == DELIVERED」：
      踩过 —— 原来只找 state==DELIVERED 的单，但那个状态只有在我们
      自己调 wanda_delivery.queue_followups() 时才会设上，
      而真实出票走的是 POST /api/order/deliver（那条路只写 ticket_codes）。
      ⇒ 结果：真实出票的单永远等不到发货命令，买家拿不到"已发货"，
        而系统以为一切正常。
      ⇒ 改成按【事实】判：有取票码 = 该发货了。

    ★ dedupe_key 用 order_id：同一单只排一次。
      命令可能执行失败，需要用同一把键重试；
      而且"排过"和"发成功"是两件事。
    """
    # ★★ 2026-10-14 接线：「设置 → 订单发货 → 自动发货」
    #   auto_ship=off ⇒ 不自动排发货命令（等店家在订单页手工发）。
    #   ★ 默认 True ⇒ 行为不变。
    #   ★ 注意：这里【不读】force_ship ——
    #     那个开关的语义是"退款拦截失败后仍转正常发货"，接在退款那条路上。
    #     我一度把它接到这里的付款闸门，破坏了"未付款不发"，已撤回。
    q = queue()
    made = 0
    try:
        from . import ledger
        payload = ledger.orders_payload(limit=None)
    except Exception:                                     # noqa: BLE001
        return 0
    from .shop_scope import scope
    for order in payload.get("orders") or []:
        with scope(order.get('tenant_id',''), order.get('shop_id','')):
            made += _queue_order_delivery(order, q)
    return made


def _queue_order_delivery(order, q):
    from . import ledger
    import settings
    if not settings.load().order_ship.auto_ship:
        return 0
    made = 0
    codes = order.get("ticket_codes") or []
    qr_image = order.get("wanda_ticket_image") if order.get("provider") == "WANDA" else None
    if not codes and not qr_image:
        return made
    # ★ 已取消/已退款的单不发
    if order.get("state") in ("CANCELLED", "REFUNDED", "FAILED"):
        return made
    # ★★ 付款状态是发货闸门：未付款的一律不发（钱没到手就发货，钱收不回来）。
    #   ★ 2026-10-14 我一度在这里接了「订单发货 → 强制发货开关」，
    #     让未付款的也能发 —— 那是【错的】，已撤回。
    #     理由两条：
    #       ① 这行注释就是钱上的硬闸门，不该被一个界面开关绕过；
    #       ② 那个开关的默认文案写的是
    #          「收到您的退款申请后…已自动出票…小店已为您转为正常发货」
    #          ⇒ 它管的是【退款拦截失败后仍转正常发货】，
    #            不是"允许未付款发货"。我按 label 猜错了语义。
    #     force_ship 现在接在退款那条路上（见 ticket_flow 的拦截失败分支）。
    if order.get("payment_state") != "CONFIRMED":
        return made
    oid = str(order.get("order_id") or "")
    if not oid:
        return made
    ident = {
        "tenant_id": order.get("tenant_id") or "",
        "shop_id": order.get("shop_id") or "",
        "buyer_id": order.get("buyer_id") or "",
        "chat_id": order.get("chat_id") or "",
    }
    # Freeze Wanda delivery messages in the durable shipment command. Old
    # commands without this marker keep their original notice behavior.
    wanda = order.get("provider") == "WANDA"
    online_id = str(order.get("provider_order_id") or "") if wanda else oid
    liangpiao = order.get("provider") == "LIANGPIAO"
    if liangpiao:
        online_id = str(order.get("platform_order_id") or "")
        if order.get("liangpiao_ticket_version") is None:
            return made
    if not online_id or not _shop_allows_send(ident):
        return made
    delivery_extra = {}
    if wanda and not qr_image:
        return made  # Wanda now requires its order-specific QR image.
    if wanda:
        import templates as delivery_templates
        parts = delivery_templates.render_messages("TICKET_CODE", {
            "ticket_code": "",
            "ticket_link": "", "cinema": order.get("cinema") or "",
            "movie": order.get("movie") or "", "show_time": order.get("show") or "",
        }, channel="wanda")
        parts = [p for p in parts if p.strip() != "（配图：取票示意图）"]
        delivery_extra["wanda_delivery_parts"] = parts
        delivery_extra["wanda_ticket_image"] = qr_image
        delivery_extra["local_order_id"] = oid
    lp_parts = None
    if liangpiao:
        import templates as delivery_templates
        lp_parts = delivery_templates.render_messages("TICKET_CODE", {
            "ticket_code": "、".join(str(c) for c in codes),
            "ticket_link": order.get("liangpiao_ticket_link") or "",
            "cinema": order.get("cinema") or "", "movie": order.get("movie") or "",
            "show_time": order.get("show") or "", "seats": "、".join(order.get("seats") or []),
        }, channel="liangpiao")
        lp_parts = [p for p in lp_parts if p.strip() and p.strip() != "（配图：取票示意图）"]
        with ledger.stores().orders.lock:
            current = ledger.stores().orders.get(oid)
            saved = current.payload.get("liangpiao_delivery_parts") or {}
            if saved.get("version") == order['liangpiao_ticket_version']:
                lp_parts = saved["messages"]
            else:
                if current.payload.get("liangpiao_ticket_version") != order['liangpiao_ticket_version']:
                    return made
                current.payload["liangpiao_delivery_parts"] = {
                    "version": order['liangpiao_ticket_version'], "messages": lp_parts}
                ledger.stores().orders.put(current)
        delivery_extra["local_order_id"] = oid
    # ① 发货命令
    ship = Command(
        command_id=str(uuid.uuid4()), type="SHIP_ORDER",
        event_id=online_id, session_id=ident["chat_id"],
        dedupe_key=dedupe_for(oid, kind="ship"),
        # ★ 插件 ticket-delivery/wanda-shipment 会读这几个字段：
        #   order_id 用来查线上订单、delivery_provider 用来选哪条交付路径
        extra={"order_id": online_id, "delivery_provider": order.get("provider") or "",
               "provider_order_id": order.get("provider_order_id") or "", **delivery_extra},
        **ident)
    created, ship = q.enqueue(ship)
    if created:
        made += 1
    if liangpiao:
        previous = ship.command_id
        last = None
        for index, message in enumerate(lp_parts):
            notice = Command(command_id=str(uuid.uuid4()), type="TICKET_DELIVERY_NOTICE",
                event_id=online_id, session_id=ident["chat_id"], text=message,
                dedupe_key=dedupe_for(oid, kind="lp_ticket", part=f"{order['liangpiao_ticket_version']}:{index}"),
                extra={"order_id": online_id, "local_order_id": oid, "history_message_ids": [],
                       "delivery_provider": "LIANGPIAO", "supplier_template_part": True, "ticket_version": order['liangpiao_ticket_version']}, **ident)
            notice.depends_on_command_id = previous
            created, last = q.enqueue(notice)
            made += int(created)
            previous = last.command_id
        if ship.status == "SUCCEEDED":
            with ledger.stores().orders.lock:
                current = ledger.stores().orders.get(oid)
                if current and current.state not in ("FAILED", "CANCELLED", "REFUNDED"):
                    changed = not current.shipped
                    current.shipped = True
                    if (last and last.status == "SENT" and current.state != "DELIVERED"
                            and current.payload.get("liangpiao_ticket_version") == order['liangpiao_ticket_version']):
                        current.state = "DELIVERED"
                        current.delivered_at = datetime.now(timezone.utc)
                        changed = True
                    if changed:
                        ledger.stores().orders.put(current)
        return made
    if wanda and not ship.extra.get("wanda_ticket_image"):
        return made  # Historical commands must be reconciled, never reused as QR delivery.
    if "wanda_delivery_parts" in ship.extra:
        qr = Command(command_id=str(uuid.uuid4()), type="TICKET_DELIVERY_NOTICE",
            event_id=ship.event_id, session_id=ident["chat_id"], text="", image=ship.extra["wanda_ticket_image"],
            dedupe_key=dedupe_for(oid, kind="notice", part="qr"),
            extra={"order_id": ship.event_id, "local_order_id": oid, "history_message_ids": [],
                   "delivery_provider": "WANDA", "wanda_template_part": True}, **ident)
        qr.depends_on_command_id = ship.command_id
        created, qr = q.enqueue(qr)
        made += int(created)
        previous = qr.command_id
        for index, message in enumerate(ship.extra["wanda_delivery_parts"]):
            notice = Command(command_id=str(uuid.uuid4()), type="TICKET_DELIVERY_NOTICE",
                event_id=ship.event_id, session_id=ident["chat_id"], text=message,
                dedupe_key=dedupe_for(oid, kind="notice", part=str(index)),
                extra={"order_id": ship.event_id, "history_message_ids": [], "delivery_provider": "WANDA", "wanda_template_part": True, "local_order_id": oid},
                **ident)
            notice.depends_on_command_id = previous
            created, notice = q.enqueue(notice)
            made += int(created)
            previous = notice.command_id
        with ledger.stores().orders.lock:
            current = ledger.stores().orders.get(oid)
            last = q.get(previous)
            if current and current.state not in ("FAILED", "CANCELLED", "REFUNDED"):
                if ship.status == "SUCCEEDED":
                    current.shipped = True
                if ship.status == "SUCCEEDED" and last and last.status == "SENT":
                    current.state = "DELIVERED"
                    if not current.delivered_at:
                        current.delivered_at = datetime.now(timezone.utc)
                ledger.stores().orders.put(current)
        return made
    # ② 出票通知（发那段文本给买家）
    notice_text = _notice_text(order)
    if notice_text:
        # ★ 依赖发货命令：发货没成功就不发通知
        #   （通知说"已发货"而实际没发，是骗买家）
        notice = Command(
            command_id=str(uuid.uuid4()), type="TICKET_DELIVERY_NOTICE",
            event_id=oid, session_id=ident["chat_id"],
            text=notice_text,
            dedupe_key=dedupe_for(oid, kind="notice"),
            extra={"order_id": oid, "history_message_ids": [],
                   "delivery_provider": order.get("provider") or ""},
            **ident)
        notice.depends_on_command_id = ship.command_id
        if q.enqueue(notice)[0]:
            made += 1
    return made


def _notice_text(order: dict) -> str:
    """
    出票通知的文本。

    ★ 形态照线上（V5 application/order_replies.py 的 PAYMENT_MANUAL 那一套）：
      取票码已经在交付里发过了，这里只说"已出票、已发货"。
      ★ 带上影院场次 —— 买家可能同时在问好几单，不带就对不上是哪一单。
    """
    tpl = getattr(store_module, "TICKET_DELIVERY_NOTICE_TEXT", "")
    if tpl:
        return tpl
    cinema = str(order.get("cinema") or "").strip()
    show = str(order.get("show") or "").strip()
    where = " ".join(x for x in (cinema, show) if x)
    if where:
        return f"{where} 已出票、已发货，请注意查收～"
    return "已出票，票已发出，请注意查收～"
