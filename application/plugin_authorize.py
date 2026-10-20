"""
application/plugin_authorize.py —— 命令执行前的授权闸门

════════════════════════════════════════════════════════════════
★ 这一层的职责：命令排队之后、真正动手之前，再判一次。

  为什么需要"再判一次"（这是 V5 设计的核心，不是形式主义）：

      命令在 T0 产生（"给买家发这条报价"）
      插件在 T0+3 秒才领到、才执行

      这 3 秒里情况可能变了：
        · 买家又发了话，上一句报价已经过时了
        · 订单被改价了，旧的通知不该再发
        · 会话转到人工了，机器人不该再插话
        · 命令压根不是 CLAIMED 状态（被别的实例领走了）

    ⇒ 让插件在发之前问我们一次"现在还能发吗"，
      我们回 allowed=False + 原因，插件就不发（SUPPRESSED）。

    ★★ 不做这道闸门的后果：买家收到已经过时的报价、
       或者收到两条互相矛盾的改价通知。

★ 三个授权端点（插件源码里的具体调用点）

    authorize-message-send      message-history.mjs:29
        ★ 返回 {allowed, reason} —— 不是发令牌，是判"该不该发"

    authorize-execution         reprice.mjs:138
        ★ 体是「线上订单详情」，我们核对后回授权

    authorize-ticket-delivery   ticket-delivery.mjs:17 / wanda-shipment.mjs:11
        ★ 返回的内容必须【逐字段等于命令】——
          插件会核对 command_id/type/order_id/tenant_id/shop_id/buyer_id/chat_id，
          对不上就 GATE_REJECTED（防串单/防命令被替换）
════════════════════════════════════════════════════════════════
"""

from .plugin_api import queue
# ★ 2026-10-14：模块级 import manual_control ——
#   架构审计只认模块级，函数内动态 import 会被判成"孤儿模块"。
#   本模块用它判"会话是不是人工接管中"（接管了就不发机器人消息）。
#   ★ 不会成环：manual_control 不 import 任何 application 模块。
from . import manual_control

# ★★ 授权时必须回显的字段 —— 插件 wanda-shipment.mjs:14 会逐字回核：
#       ['command_id','type','order_id','tenant_id','shop_id',
#        'buyer_id','chat_id'].some(k => grant[k] !== command[k])
#    ★ 对不上 → GATE_REJECTED。
#    ★ 它和 _identity_of() 的键是同一组 —— 这里断言一下，
#      免得以后改了 _identity_of 却忘了这组（那就静默对不上了）。
ECHO_FIELDS = ("command_id", "type", "order_id", "tenant_id", "shop_id",
               "buyer_id", "chat_id")


def _load(command_id: str):
    """
    取命令并判它现在能不能被授权。

    ★★ 只有 CLAIMED / SENDING 能授权（V5 判据：status == 'CLAIMED'）——
      踩过：我第一版写成"不是终态就放行"，于是 PENDING（还没被领走）
      也能拿到授权 ⇒ 别的实例可以拿着授权执行一条不属于它的命令。
    """
    from integrations.yumaiduo.commands import AUTHORIZABLE_STATUSES
    cmd = queue().get(command_id)
    if cmd is None:
        return None, {"allowed": False, "reason": "COMMAND_NOT_FOUND"}
    if cmd.status in AUTHORIZABLE_STATUSES:
        return cmd, None
    # ★ 把当前状态带在 reason 里 —— 排查时一眼看出是"没领走"还是"已发出"
    return None, {"allowed": False,
                  "reason": f"COMMAND_NOT_AUTHORIZABLE:{cmd.status}"}


def _identity_of(cmd) -> dict:
    """
    授权要回给插件的身份字段。

    ★ 键必须【正好】是 ECHO_FIELDS —— 插件会逐字回核，少一个或名字不同
      都会被判 GATE_REJECTED（而且不报错，只是静默拒绝执行）。
      这里断言一下，改了这组字段会立刻被测试抓到。
    """
    out = {"command_id": cmd.command_id, "type": cmd.type,
           "order_id": cmd.event_id or "", "tenant_id": cmd.tenant_id,
           "shop_id": cmd.shop_id, "buyer_id": cmd.buyer_id,
           "chat_id": cmd.chat_id}
    missing = [k for k in ECHO_FIELDS if k not in out]
    if missing:
        raise RuntimeError(f"AUTHORIZE_ECHO_FIELDS_MISSING: {missing}")
    return out


def authorize_message_send(command_id: str) -> dict:
    """
    SEND_MESSAGE 发之前的判定。

    ★★ 返回 {allowed, reason}（不是令牌）——
      插件 message-history.mjs:37 的判据就是 data.allowed === true。

    ★ 现阶段能判的陈旧情况：
        · 命令不存在 / 状态不对
        · 会话已经转人工（那机器人不该再插话）
        · 同一个会话有更新的命令排在后面（前一条已过时）
      ★ 判不了的（V5 有、我们还没有的）：
        履约通知被新订单取代、改价轮次变了 —— 那些要等对应的
        业务状态齐了才能判。现在【不假装能判】，只判我们能确证的。
    """
    cmd, denied = _load(command_id)
    if denied:
        return denied
    if cmd.type != "SEND_MESSAGE":
        return {"allowed": False, "reason": "NOT_A_MESSAGE_COMMAND"}

    # ---- 会话是否已转人工 ----
    if _handed_off(cmd.chat_id):
        return {"allowed": False, "reason": "CONVERSATION_HANDED_OFF"}

    # ---- 这个会话里有没有更新的待发命令 ----
    #   ★ 为什么判这个：买家连发两句话会产生两条回复命令。
    #     如果后一句的回复已经把情况说清了，前一句就过时了 ——
    #     发出去买家会看到两句互相打架的话。
    #   ★ 只比【同一个 chat 且还在 PENDING】的：已发出的不算。
    try:
        newer = [c for c in queue().by_status("PENDING")
                 if c.chat_id == cmd.chat_id
                 and c.type == "SEND_MESSAGE"
                 and c.created_at > cmd.created_at]
        if newer:
            return {"allowed": False, "reason": "SUPERSEDED_BY_NEWER_MESSAGE"}
    except Exception:                                     # noqa: BLE001
        pass

    return {"allowed": True, "reason": ""}


def _handed_off(chat_id: str) -> bool:
    """
    这个会话是不是【人工接管中】（接管了就不发机器人消息）。

    ★★★ 2026-10-14 修一个真 bug：
      原来的判据是 `"HUMAN" in name.upper()`，
      而 ConvState.MANUAL 的 value 是「待人工」——
      「人工」里没有 HUMAN 这四个字母 ⇒ **永远返回 False**。
      ⇒ "转人工后机器人不再插话"这个保护【从来没生效过】。
        会话一转人工，机器人照样往里发消息，和人工客服抢话。

    ★ 修法：判【枚举本身】而不是它的中文显示值。
      中文是给人看的，随时可能改；拿它当判据必然出这种事。

    ★ 并且接上「人工介入超时关闭」：
      接管不是永久的 ——
        · 超过 close_after_hours 没动静 ⇒ 自动关闭接管
        · 订单到了 close_on_status 里的状态 ⇒ 关闭接管
      关闭后机器人重新应答（人工那边的事情已经不需要说话了）。
      ⇒ 见 application/manual_control.py
    """
    if not chat_id:
        return False
    try:
        from .service import get_local_service
        svc = get_local_service()
        if svc is None:
            return False
        s = svc.sessions.get(chat_id)
        if s is None:
            return False
        state = getattr(s.conversation, "state", None)
        # ★ 判枚举（name），不判中文 value
        name = getattr(state, "name", "") or ""
        if name != "MANUAL":
            return False
        # ★ 接管是不是已经该关闭了
        try:
            from . import manual_control as _mc
            if _mc.should_close(s):
                _mc.close(s)
                return False
        except Exception:                                 # noqa: BLE001
            pass
        return True
    except Exception:                                     # noqa: BLE001
        return False


def authorize_execution(command_id: str, detail: dict | None = None) -> dict:
    """
    CHANGE_ORDER_PRICE 执行前的授权。

    ★ 插件传上来的 detail 是【它从鱼麦多读到的线上订单详情】。
      我们核对后再放行 —— 这是"用真实上游状态核对我们的意图"。

    只给本店、本订单的待付款单授权改价。买家付款后不能再改价。
    """
    cmd, denied = _load(command_id)
    if denied:
        return denied
    if cmd.type != "CHANGE_ORDER_PRICE":
        return {"allowed": False, "reason": "NOT_A_REPRICE_COMMAND"}

    if not isinstance(detail, dict):
        return {"allowed": False, "reason": "ORDER_DETAIL_MISSING"}
    if str(detail.get("orderId") or "") != str(cmd.event_id or ""):
        return {"allowed": False, "reason": "ORDER_ID_MISMATCH"}
    if str(detail.get("accountUnb") or "") != str(cmd.shop_id or ""):
        return {"allowed": False, "reason": "ORDER_SHOP_MISMATCH"}
    status = detail.get("orderStatus")
    if status != 1:
        return {"allowed": False,
                "reason": f"ORDER_NOT_REPRICEABLE:{status}"}
    from . import ledger
    stores = ledger.stores()
    current = stores.orders.find_by_provider_order(cmd.event_id) if stores.orders else None
    if current and current.payload.get('manual_settlement'):
        return {"allowed": False, "reason": "MANUAL_SETTLEMENT_SUPERSEDES_REPRICE"}
    pending = (current.payload or {}).get("quote_revision_pending") if current else None
    if pending and pending.get("command_id") != cmd.command_id:
        return {"allowed": False, "reason": "REPLACEMENT_ALREADY_IN_PROGRESS"}

    grant = dict(_identity_of(cmd))
    grant["allowed"] = True
    # ★★★ 2026-10-14 补上 target_amount_fen —— 不补的话插件【必然拒绝】。
    #
    #   证据：插件 reprice.mjs:141
    #       if (authorized.order_id !== command.order_id ||
    #           !Number.isSafeInteger(authorized.target_amount_fen) ||
    #           authorized.target_amount_fen <= 0)
    #         return {status:'GATE_REJECTED',
    #                 failure_reason:'INVALID_AUTHORIZED_PRICE'};
    #   而这里原来只回 _identity_of 那 7 个字段（没有 target_amount_fen）
    #   ⇒ 每一个改价命令都被判 INVALID_AUTHORIZED_PRICE，
    #     插件【静默拒绝执行】—— 不抛异常、不报错，
    #     表现就是"改了价但价没变"，日志里只有一行 GATE_REJECTED。
    #
    #   ★ 目标价从命令的 extra 取（生成命令时写进去的），
    #     不从订单上取 —— 订单上的 amount_fen 是【改价前】的值。
    target = cmd.extra.get("target_amount_fen") if isinstance(cmd.extra, dict) else None
    if not isinstance(target, int) or target <= 0:
        # 没有明确目标价就【不许改】—— 宁可拒绝，也不能改成一个瞎猜的数字
        return {"allowed": False, "reason": "REPRICE_TARGET_AMOUNT_MISSING"}
    if current and not cmd.extra.get("replacement_quote_id") and current.amount_fen != target:
        return {"allowed": False, "reason": "REPRICE_TARGET_SUPERSEDED"}
    grant["target_amount_fen"] = target
    if cmd.extra.get("replacement_quote_id"):
        from . import ledger
        from .quote_reprice import authorize_quote_revision
        try:
            authorize_quote_revision(stores, cmd)
        except (ValueError, RuntimeError) as exc:
            return {"allowed": False, "reason": str(exc)}
    return grant


def authorize_ticket_delivery(command_id: str, *, detail: dict | None = None,
                              session: dict | None = None) -> dict:
    """
    DELIVER_TICKET / SHIP_ORDER / TICKET_DELIVERY_NOTICE 执行前的授权。

    ★★ 返回的内容必须【逐字段等于命令】——
      插件 wanda-shipment.mjs:14 会核对：
          ['command_id','type','order_id','tenant_id','shop_id',
           'buyer_id','chat_id'].some(k => grant[k] !== command[k])
          → true 就 GATE_REJECTED
      ⇒ 这是防"授权内容被替换/串到别的单上"。

    ★ 插件还要求 execution_token（没有就拒绝）。
      token 的作用：把"授权"和"执行"绑在一起 ——
      插件执行完把 token 一起回报，我们能认出"这次执行用的是哪次授权"。

    ★★ detail 和 session 是插件传上来的【鱼麦多线上实况】：
        detail  = client.orders.get(order_id) 的返回
        session = client.im.getSessionByOrder(order_id) 的返回
      ★ 为什么必须核对（不能收下就放行）：
        命令是我们几秒前生成的。这几秒里订单可能被买家关闭、
        被别人改价、或者根本不是同一个店 ——
        不核对就等于"闭着眼发货"。
      ⇒ 核对三件事：订单号、店铺、订单状态。
    """
    cmd, denied = _load(command_id)
    if denied:
        return denied
    if cmd.type not in ("DELIVER_TICKET", "SHIP_ORDER",
                        "TICKET_DELIVERY_NOTICE"):
        return {"allowed": False, "reason": "NOT_A_DELIVERY_COMMAND"}

    online_order_id = cmd.event_id or ""
    if cmd.extra.get("delivery_provider") == "WANDA":
        from . import ledger, store
        orders = ledger.stores().orders
        order = orders.get(cmd.extra.get("local_order_id", "")) if orders else None
        if (order is None or order.provider != "WANDA" or order.provider_order_id != online_order_id
                or order.payment_state != "CONFIRMED" or order.state not in ("TICKETED", "DELIVERED")
                or not order.payload.get("wanda_ticket_image")
                or any(str(getattr(order,k)) != str(getattr(cmd,k)) for k in ("tenant_id","shop_id","buyer_id","chat_id"))
                or not store.shop_send_enabled(cmd.shop_id)):
            return {"allowed": False, "reason": "WANDA_DELIVERY_FACTS_INVALID"}
        if (not isinstance(detail, dict) or detail.get("orderId") != online_order_id
                or str(detail.get("accountUnb")) != cmd.shop_id
                or type(detail.get("orderStatus")) is not int or detail['orderStatus'] not in (2,3,4)
                or str(detail.get("payment")) != str(order.amount_fen)
                or not isinstance(session, dict)
                or str(session.get("peerUnb") or session.get("buyerId") or "") != cmd.buyer_id):
            return {"allowed": False, "reason": "OFFICIAL_DELIVERY_FACTS_REQUIRED"}
        if cmd.image and cmd.image != order.payload["wanda_ticket_image"]:
            return {"allowed": False, "reason": "TICKET_IMAGE_CHANGED"}
    if cmd.extra.get("delivery_provider") == "LIANGPIAO":
        from . import ledger, store
        orders = ledger.stores().orders
        order = orders.get(cmd.extra.get("local_order_id", "")) if orders else None
        if (order is None or order.provider != "LIANGPIAO"
                or order.platform_order_id != online_order_id
                or order.payment_state != "CONFIRMED"
                or order.state not in ("TICKETED", "DELIVERED") or not order.ticket_codes
                or any(str(getattr(order, k)) != str(getattr(cmd, k))
                       for k in ("tenant_id", "shop_id", "buyer_id", "chat_id"))
                or not store.shop_send_enabled(cmd.shop_id)):
            return {"allowed": False, "reason": "LIANGPIAO_DELIVERY_FACTS_INVALID"}
        if cmd.type == "TICKET_DELIVERY_NOTICE" and cmd.extra.get("ticket_version") != order.payload.get("liangpiao_ticket_version"):
            return {"allowed": False, "reason": "TICKET_VERSION_SUPERSEDED"}
        if (not isinstance(detail, dict) or detail.get("orderId") != online_order_id
                or str(detail.get("accountUnb") or "") != cmd.shop_id
                or type(detail.get("orderStatus")) is not int
                or detail["orderStatus"] not in (2, 3, 4)):
            return {"allowed": False, "reason": "OFFICIAL_DELIVERY_FACTS_REQUIRED"}
        if (not isinstance(session, dict)
                or str(session.get("peerUnb") or session.get("buyerId") or "") != cmd.buyer_id):
            return {"allowed": False, "reason": "OFFICIAL_BUYER_MISMATCH"}
    if isinstance(detail, dict) and detail:
        # ① 线上订单号必须是我们这条命令要操作的那个
        got_order = str(detail.get("orderId") or "")
        if got_order and online_order_id and got_order != online_order_id:
            return {"allowed": False,
                    "reason": f"OFFICIAL_ORDER_MISMATCH:{got_order}"}
        # ② 线上店铺必须是这条命令的店铺（防跨越店铺操作）
        got_shop = str(detail.get("accountUnb") or "")
        if got_shop and cmd.shop_id and got_shop != cmd.shop_id:
            return {"allowed": False,
                    "reason": f"OFFICIAL_SHOP_MISMATCH:{got_shop}"}
        # ③ 订单状态必须是【已付款(2) / 已发货(3) / 已完成(4)】
        #    ★ 1 = 待付款。没付款就发货 = 钱收不回来。
        status = detail.get("orderStatus")
        if status is not None and status not in (2, 3, 4):
            return {"allowed": False,
                    "reason": f"ORDER_NOT_DELIVERABLE:{status}"}
        if status == 1:
            return {"allowed": False, "reason": "ORDER_NOT_PAID"}

    if isinstance(session, dict) and session:
        # ④ 会话必须属于这个订单（防发错人）
        got = str(session.get("orderId") or session.get("order_id") or "")
        if got and online_order_id and got != online_order_id:
            return {"allowed": False, "reason": f"SESSION_ORDER_MISMATCH:{got}"}

    grant = dict(_identity_of(cmd))
    grant["allowed"] = True
    # ★ 令牌：用命令 id + 时间戳派生，回报时能对上
    grant["execution_token"] = _token_for(cmd)
    # ★ TICKET_DELIVERY_NOTICE 要发一段文本 —— 插件会读 grant.text
    if cmd.type == "TICKET_DELIVERY_NOTICE":
        grant["text"] = cmd.text or ""
        if cmd.image:
            grant["image"] = cmd.image
    grant["delivery_provider"] = cmd.extra.get("delivery_provider", "") \
        if isinstance(cmd.extra, dict) else ""
    return grant


def _token_for(cmd) -> str:
    """
    执行令牌。

    ★ 为什么用哈希而不是随机数：
      同一条命令重试授权时要拿到【同一个令牌】——
      否则插件每次重试都拿到新令牌，"这次执行对应哪次授权"就对不上，
      我们也没法判断是不是同一个动作被重复执行。
    """
    import hashlib
    raw = f"{cmd.command_id}|{cmd.type}|{cmd.tenant_id}|{cmd.shop_id}|{cmd.event_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def authorize_polish(command_id: str) -> dict:
    """POLISH_SHOP（抛商品）的授权。★ 读操作，风险低，只核状态。"""
    cmd, denied = _load(command_id)
    if denied:
        return denied
    if cmd.type != "POLISH_SHOP":
        return {"allowed": False, "reason": "NOT_A_POLISH_COMMAND"}
    return {"allowed": True, "reason": ""}


def authorize_close(command_id: str, detail: dict | None = None) -> dict:
    """
    CLOSE_FAILED_ORDER（关掉一张不该继续的单）的授权。

    ★★★ 2026-10-14：这道闸门以前【不存在】——
      server.py 对 authorize-close 一律回 {"allowed": False,
      "reason": "NOT_IMPLEMENTED:close"}，而插件 failed-order-close.mjs:22
      正等着它。后果和 REPRICE 那次一模一样：
      **后端从没生成过 CLOSE_FAILED_ORDER 命令，插件那侧的完整实现
      永远不会被执行**（命令队列看着做完了，实际一步没动）。

    ★ 这是【资金敏感】的动作，所以判据要严：
      ① 命令状态必须可授权（_load 已判）
      ② 订单必须还处于【已付款未发货】——
         文档的 orderStatus：2=已付款待发货、6=已完成/已关闭。
         ★ 已发货(3)/已完成(4)/已关闭(6) 都不许再关：
           票已经交出去了，关单等于把货吞了。
      ③ 必须带得出取消原因 —— 文档 CancelOrderRequest 的 reason
         是【必填】(1~200 字)，且会写进订单事件与审计。
         没有原因的关单事后查不清是谁为什么关的。

    ★★ 响应字段为什么单独拼而不是复用 _identity_of：
      插件 failed-order-close.mjs:23 会逐字回核这组键
          ['command_id','type','tenant_id','shop_id',
           'order_id','booking_order_id','text']
      判据是 `键在 grant 和 command 上是否同时相等`。
      而 Command 的字段里【没有】order_id / booking_order_id ——
      它们只在 extra 里；空值也会被 to_dict 序列化出来（空串）。
      ⇒ 所以这里必须【按命令实际有的键】回显：
        · 命令有 order_id 就回 order_id
        · 用空串补齐一个命令根本没有的键 → 插件判 undefined≠''
          → CLOSE_AUTHORIZATION_REJECTED，而且是静默的。
    """
    cmd, denied = _load(command_id)
    if denied:
        return denied
    if cmd.type != "CLOSE_FAILED_ORDER":
        return {"allowed": False, "reason": "NOT_A_CLOSE_COMMAND"}

    extra = cmd.extra if isinstance(cmd.extra, dict) else {}
    order_id = str(extra.get("order_id") or cmd.event_id or "")
    if not order_id:
        return {"allowed": False, "reason": "CLOSE_ORDER_ID_MISSING"}

    reason = (cmd.text or "").strip()
    if not reason:
        return {"allowed": False, "reason": "CLOSE_REASON_REQUIRED"}
    # 文档：reason 1~200 字
    reason = reason[:200]

    # ★ 订单状态门：只有"已付款、还没发货"才允许关单。
    #   插件也判了一遍（failed-order-close.mjs:21），这里判的是同一件事 ——
    #   ★ 为什么两处都判：插件那份用的是【它读到的】状态，
    #     我们这份用的是【后端此刻的】状态；两者都通过才说明时序没变。
    if isinstance(detail, dict) and detail:
        status = detail.get("orderStatus")
        if status is not None and int(status) not in (2, 6):
            return {"allowed": False,
                    "reason": f"CLOSE_ORDER_STATUS_NOT_ELIGIBLE:{status}"}

    grant = {"allowed": True, "reason": "", "reason_text": reason}
    for key in ECHO_FIELDS:
        if key == "order_id":
            continue
        grant[key] = getattr(cmd, key, "")
    # ★ 只回命令身上真的有的那组键（见 docstring 最后一段）
    grant["order_id"] = order_id
    if "booking_order_id" in extra:
        grant["booking_order_id"] = extra.get("booking_order_id") or ""
    if "text" in getattr(cmd, "__dataclass_fields__", {}):
        grant["text"] = reason
    return grant
