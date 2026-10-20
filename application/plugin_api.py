"""
application/plugin_api.py —— 鱼麦多插件的 HTTP 端点

════════════════════════════════════════════════════════════════
★ 这个文件是"后端"那一侧

  插件（跑在卖家店铺里）会调这几个端点：

    POST /api/v1/events                  推来一个事件（买家消息 / 订单变化）
    POST /api/v1/events/run-due          催我们处理待处理的事件
    POST /api/v1/commands/claim          来领要执行的命令
    POST /api/v1/commands/{id}/result    报执行结果
    POST /api/v1/fulfillment/auto/run    催自动出票/发货
    POST /api/v1/recovery/inspect        崩溃后查有没有丢的回执

  ★★ 全部要过签名校验（integrations/yumaiduo/signature.py）。
    没有签名校验就等于：任何知道地址的人都能
      · 伪造买家消息（让系统去下单）
      · 领走命令（真插件就领不到了）
      · 伪造执行结果（把没发的消息标成已发）

★ 设计约定
  · 这一层【只做 HTTP 的事】：验签、解析、调下层、组响应。
    业务判断（该生成什么命令）不在这里 —— 在 runtime 层。
  · 响应形状照插件源码的读法（它读 data.commands / data.status 等）。
════════════════════════════════════════════════════════════════
"""

import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock

from . import store as store_module

# ★ 凭证文件里的字段名
CRED_SECRET = "yumaiduo_plugin_secret"
ENV_SECRET = "YUMAIDUO_WEBHOOK_SECRET"
# ★★ 插件自注册后把 webhookSecret 交接过来的文件（同机部署用）。
#
#   官方架构里【没有外部后端】—— secret 只由平台在自注册时颁发给插件进程，
#   明文只此一次。我们是插件之外的另一个进程，所以只能靠这个交接文件拿。
ENV_SECRET_FILE = "V5_BACKEND_SECRET_FILE"


def secret_file() -> str:
    """交接文件路径（环境变量指定；没配就返回空）。"""
    import os
    return (os.getenv(ENV_SECRET_FILE) or "").strip()


def _from_handoff_file() -> str:
    """
    从插件交接的文件里读 webhookSecret。

    ★ 为什么用文件而不是让人手抄：
      官方说 secret "明文只此一次"，只在自注册响应里给插件。
      ★ 我原来在界面上做了个「生成新密钥」按钮 —— 那是【错的】：
        平台签名用的是它颁发的值；我们生成一个平台不知道的值，
        验签立刻全失败。这个按钮已删。
      ⇒ 插件拿到后写到共享文件，我们读它。人不用抄、也不会抄错。
    """
    path = secret_file()
    if not path:
        return ""
    try:
        import json
        with open(path, "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("webhookSecret") or "").strip()
    except Exception:                                     # noqa: BLE001
        return ""


def plugin_secret() -> str:
    """
    插件和我们通信的密钥。

    ★★ 先说清它到底是什么（我一开始说错过了，这里更正）：

      它是【卖家在鱼麦多店铺后台配的那把"网页回调密钥"】
      （插件源码里的环境变量名就是 YUMAIDUO_WEBHOOK_SECRET）。

      ★ 一把钥匙管两个方向，两个方向都是它：
        ① 鱼麦多 → 插件：鱼麦多推买家消息给插件时带这个签名，
           插件验（server.mjs 的 verify()）。
        ② 插件 → 我们：插件把事件转发给我们时，用【同一把】签一个名
           放在 x-yumaiduo-signature 里，我们验（就是本文件做的事）。
           ★ 插件源码 backend-request.mjs:9 用的 secret 就是 cfg.secret，
             而 cfg.secret 来自 YUMAIDUO_WEBHOOK_SECRET。

      ⇒ 所以"我们验签"的含义是：确认这个请求真的来自
        【装了这把密钥的那个插件】。密钥只有卖家和插件知道。

    ★ 不是这把钥匙的东西（容易混）：
      插件去调鱼麦多发消息/改价那一步，用的是鱼麦多 SDK 的凭证
      （插件装进店铺时平台给的），和这把无关。

    ★★ 它从哪来（这里我一开始也搞错了，更正）：

      **不是"你自己随便设"的** —— 它是【平台在自注册时颁发的】：
        官方 §5.1：POST /api/v1/plugin/runtime/register
                  → data.webhookSecret   ★ 明文只此一次（库里只存 sha256）
        官方 §5.1：webhookSecret 不写进 manifest，由核心在自注册时颁发
        官方 §5.1（第 110 行）：插件每次进程启动都要自注册一次来换回它

      ⇒ 所以取值优先级是：
          ① 环境变量 YUMAIDUO_WEBHOOK_SECRET（运维显式指定，最权威）
          ② 插件自注册后交接的文件（V5_BACKEND_SECRET_FILE）★ 正常路径
          ③ 凭证文件里的 yumaiduo_plugin_secret（手动兜底 / 本地调试）

      ★ 为什么把「交接文件」排在手动值之前：
        文件里是平台刚颁发的真值；手动值可能是过期的（平台每次启动换新的）。
        手动值优先会让插件一重启、我们就验签失败 —— 而且很难查。

    ★ 一把钥匙管两个方向，两个方向都是它：
        ① 鱼麦多 → 插件：平台推买家消息时带这个签名，插件验。
        ② 插件 → 我们：插件转发给我们时用【同一把】签名，我们验。
    """
    # ★★ 2026-10-14 改：这里【不再自己实现】取值优先级，改为转调
    #   integrations 层的那一份。为什么：
    #     application/blacklist.py 也要取密钥（查买家黑名单），
    #     而它一 import 本模块就成环：
    #        blacklist → plugin_api → plugin_runtime → service → blacklist
    #     "密钥从哪来"本来就是【外部事实】（环境变量/文件路径），
    #     该由最内层 integrations 读。⇒ 规则挪到 shop_client，只有一份。
    #   ★ 第 ③ 条（凭证文件里的手动兜底）由这里读好、设进去 ——
    #     读凭证要用 application.store，那是外层的东西，
    #     integrations 不能 import 它（分层契约）。所以"读"留在这一层。
    from integrations.yumaiduo.shop_client import (
        resolve_plugin_secret, set_credential_fallback)
    try:
        data = store_module.load_credentials() or {}
        set_credential_fallback(str(data.get(CRED_SECRET) or ""))
    except Exception:                                    # noqa: BLE001
        set_credential_fallback("")
    return resolve_plugin_secret()


def save_plugin_secret(secret: str) -> None:
    data = dict(store_module.load_credentials() or {})
    data[CRED_SECRET] = (secret or "").strip()
    store_module.save_credentials(data)


# ==================================================================== 队列
_CACHE = {"store": None, "path": None}
_LOCK = RLock()


def queue():
    """
    命令队列（进程级单例）。

    ★ 为什么单例：claim 要"选中即改状态"才是原子的。
      每次新开一个 sqlite 连接的话，两个并发的 claim 可能读到同一批
      PENDING（各自连接看不到对方的未提交改动）→ 同一条被领两次。
      ★ 共用同一个 store 实例（内部有锁），才能保证只被领走一次。

    ★ 落盘位置：data/plugin-queue.sqlite3
      和报价/订单放一起（都是要跨重启保留的运营数据）。

    ★★ 路径只在【第一次】算 —— 踩过：这里每次都按 store_module.DATA
      重算路径，于是 reset_queue() 注入的临时路径被覆盖，
      测试/注入完全失效（队列还是写到了真实 data/）。
      ⇒ 有 _CACHE["path"] 就用它，没有才按默认算。
    """
    with _LOCK:
        if _CACHE["store"] is None:
            path = _CACHE["path"] or str(
                store_module.DATA / "plugin-queue.sqlite3")
            from integrations.yumaiduo.commands import CommandStore
            _CACHE["store"] = CommandStore(path)
            _CACHE["path"] = path
        return _CACHE["store"]


def reset_queue(path: str | None = None) -> None:
    """测试用：换成指定路径（None 恢复默认）。"""
    with _LOCK:
        if _CACHE["store"] is not None:
            try:
                _CACHE["store"].close()
            except Exception:                            # noqa: BLE001
                pass
        _CACHE["store"] = None
        _CACHE["path"] = None
        if path:
            from integrations.yumaiduo.commands import CommandStore
            _CACHE["store"] = CommandStore(path)
            _CACHE["path"] = path


# ==================================================================== 工具
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def event_key_of(event: dict) -> str:
    """
    事件的幂等键。

    ★ 用什么做键：插件如果带 eventId 就用它（最可靠），
      否则用 (event类型 + 会话 + 内容摘要) 拼一个。
      ★ 不能只用时间戳 —— 同一毫秒可能来两条不同消息。
    """
    for k in ("eventId", "event_id", "id"):
        v = event.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    payload = event.get("payload") or {}
    parts = [str(event.get("event") or ""),
             str(event.get("tenantId") or event.get("tenant_id") or ""),
             str(payload.get("chatId") or payload.get("chat_id") or ""),
             str(payload.get("messageId") or payload.get("message_id") or ""),
             str(payload.get("content") or "")[:200]]
    import hashlib
    return "auto-" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def session_id_of(event: dict) -> str:
    payload = event.get("payload") or {}
    for k in ("chatId", "chat_id", "sessionId", "session_id"):
        v = payload.get(k) or event.get(k)
        if v:
            return str(v)
    return ""


# ==================================================================== 端点
def handle_event(event: dict, *, verified: bool) -> dict:
    """
    收一个插件推来的事件。

    ★★ verified 必须为 True 才处理 —— 由 HTTP 层验签后传入。
      这里再判一次是"纵深防御"：万一将来有人在别处调用这个函数
      而忘了验签，也不会把伪造事件当真。

    ★ 未验签的事件【照样记录】（标成 UNVERIFIED）——
      这样能看到"有人在打我们接口"，而不是悄悄丢掉。
    """
    if not isinstance(event, dict) or not event.get("event"):
        return {"ok": False, "error": "EVENT_REQUIRED"}

    key = event_key_of(event)
    q = queue()
    raw = json.dumps(event, ensure_ascii=False)
    status = "RECEIVED" if verified else "UNVERIFIED"
    fresh, _ = q.record_event(key, str(event.get("event")), raw,
                              session_id_of(event), status=status)
    if not verified:
        return {"ok": True, "accepted": True, "verified": False,
                "event_key": key, "note": "签名未通过，只记录不处理"}
    return {"ok": True, "accepted": True, "verified": True,
            "event_key": key, "duplicate": not fresh}


# ★★ 提醒扫描的节流状态（模块级，进程内）
#   为什么节流：handle_run_due 每秒被调一次，而提醒的精度是"分钟"。
#   每秒扫一遍订单是白烧 IO。30 秒一次足够。
_REMINDER_LAST = {"at": 0.0, "made": 0}
_REMINDER_EVERY_SECONDS = 30


from .shop_scope import by_identity


@by_identity
def _apply_message_delay(cmd) -> None:
    """
    给要发出去的消息加一点延迟（设置：消息延迟 → 延迟时间）。

    ★★ 为什么在【命令层】做，而不是各个发消息的地方各写一遍：
      发消息的入口有十几处（plugin_runtime / reminders / agent…），
      逐个加必然漏。这里包一层 ⇒ 一处管全部。
    ★ 机制：设置命令的 `not_before`。
      插件的 claim_pending 已经支持"没到时间不给领"
      （见 tests/_check_plugin_queue.py 的"定时命令：没到时间不给领"），
      所以不用改插件、也不用 sleep 阻塞谁。
    ★ 只对【要发给买家的消息】生效（SEND_MESSAGE / TICKET_DELIVERY_NOTICE），
      改价/发货这类命令不加 —— 那些是趁早做更好的事，拖慢没意义。
    ★ 关闭时（默认）一点不改行为。
    """
    from datetime import datetime, timedelta, timezone
    if getattr(cmd, "not_before", ""):
        return                      # 调用方已经定了时间，别覆盖
    if str(getattr(cmd, "type", "")) not in ("SEND_MESSAGE",
                                             "TICKET_DELIVERY_NOTICE"):
        return
    try:
        import settings as _st
        cfg = _st.load().message_delay
        if not bool(getattr(cfg, "enabled", False)):
            return
        from random import randint
        lo = max(0, int(cfg.min_ms))
        ms = randint(lo, max(lo, int(cfg.max_ms)))
    except Exception:                                     # noqa: BLE001
        return                      # 读不到配置 ⇒ 不加延迟（保持原行为）
    if ms <= 0:
        return
    cmd.not_before = (datetime.now(timezone.utc)
                      + timedelta(milliseconds=ms)).isoformat()



def run_reminders(*, now=None, force: bool = False) -> int:
    """
    扫一遍订单，把该发的提醒排进队列。返回新排了几条。

    ★★ 便宜优先（handle_run_due 每秒被调）：
       ① 配置里一个提醒都没启用 ⇒ 立刻返回，连订单都不读
       ② 距上次扫描不到 30 秒 ⇒ 直接返回
    ★ 幂等交给 dedupe_key（reminder:<类型>:<订单号>），
      不靠内存标记 —— 重启/多进程下内存标记会漏。
    """
    import time as _time
    now_ts = _time.monotonic()
    if not force and (now_ts - _REMINDER_LAST["at"]) < _REMINDER_EVERY_SECONDS:
        return 0
    _REMINDER_LAST["at"] = now_ts

    try:
        import settings as _st
        cfg = _st.load()
        enabled = any(str(getattr(getattr(cfg, sec), "message", "") or "").strip()
                      for sec in ("movie_remind", "urge_receive", "urge_order", "urge_review"))
        # Shop overrides may enable reminders even when shared defaults are empty.
        from . import ledger
        payload = ledger.orders_payload(limit=None)
    except Exception:                                     # noqa: BLE001
        return 0

    from .reminders import build_reminder_commands
    try:
        cmds = build_reminder_commands(
            now=now, orders=payload.get("orders") or [], cfg=cfg)
    except Exception:                                     # noqa: BLE001
        return 0

    q = queue()
    made = 0
    for c in cmds:
        _apply_message_delay(c)
        if q.enqueue(c)[0]:
            made += 1
    _REMINDER_LAST["made"] = made
    return made


def handle_run_due(*, limit: int = 50) -> dict:
    """
    处理待处理事件 → 生成命令。

    ★ 插件每秒调一次这个（server.mjs:51 的 readingDue 定时器），
      所以它必须【便宜】：没事件时立刻返回。
    ★ 2026-10-14：这里还挂了一次【提醒扫描】（见 run_reminders）——
      同样必须便宜：没启用任何提醒就一次配置读 + 短路返回，
      启用了也最多 30 秒扫一次。
    """
    from .plugin_runtime import events_to_commands
    q = queue()
    # Read-only marketplace reconciliation, including purchases without AI quotes.
    try:
        from .platform_orders import process_due
        from .ledger import stores
        process_due(stores().orders)
    except Exception:
        pass
    # ★ 先跑提醒（它自己会节流）。放在事件处理【之前】没有特殊理由，
    #   只是"每 tick 都跑到"；出错也不该影响事件处理 ⇒ 自己吞掉异常。
    try:
        run_reminders()
    except Exception:                                     # noqa: BLE001
        pass
    # ★★ 2026-10-14：退款时序也挂在这里（延迟提交的退票申请）。
    #   为什么挂这里：退款是【直接 HTTP 调用】（不经插件），
    #   所以用不上命令队列的 not_before ⇒ 需要一个到点推进的循环，
    #   而 handle_run_due 每秒被调一次，正是这个循环。
    #   ★ 同样必须便宜：队列空时是一次文件读 + 短路返回。
    #   ★ 出错不能影响事件处理 ⇒ 自己吞掉。
    try:
        from . import refund_schedule as _rs
        # ★ service 由这里取并传进去 —— refund_schedule 不许 import
        #   plugin_runtime（那会成环：plugin_runtime → service →
        #   refund_schedule → plugin_runtime）。依赖方向只有外层→内层。
        from .plugin_runtime import get_local_service as _gls
        _svc = _gls()
        _rs.process_due(service=_svc)
        # ★★ 2026-10-14：人工介入超时关闭也挂这里（同一个到点推进循环）。
        #   ConvState.MANUAL 是终态、没有出口 ⇒ 不关的话会话永远无人应答。
        from . import manual_control as _mc
        _mc.process_due(service=_svc)
        # ★★ 2026-10-14：二次报价也挂这里（同一个到点推进循环）。
        #   报价后等了 wait_seconds 买家还没拍 ⇒ 再报一次（带优惠）。
        from .ticket_flow import second_quote_due as _sq_due
        _sq_due(service=_svc)
    except Exception:                                     # noqa: BLE001
        pass
    from . import liangpiao_sync, ledger
    if ledger.stores().orders is not None:
        liangpiao_sync.process(q, ledger.stores().orders, limit=limit)
    pending = q.pending_events(limit)
    made = []
    failed = []
    for event in pending:
        key = event_key_of(event)
        try:
            produced = events_to_commands(event)
            for cmd in produced or []:
                _apply_message_delay(cmd)
                q.enqueue(cmd)
                made.append(cmd.command_id)
            q.mark_event(key, "PROCESSED")
        except Exception as exc:                          # noqa: BLE001
            # ★ 处理失败【不标 PROCESSED】—— 下次还能重试。
            #   标了就永远丢了。
            q.mark_event(key, "FAILED")
            failed.append({"event_key": key,
                           "error": f"{type(exc).__name__}: {exc}"})
    return {"ok": True, "processed": len(pending) - len(failed),
            "commands": len(made), "failed": failed}


def handle_claim(limit: int = 10) -> dict:
    """
    插件来领命令。

    ★ 响应形状 {ok, data:{commands:[...]}} —— HTTP 层用它组包。
    """
    q = queue()
    got = q.claim_pending(limit)
    return {"ok": True, "commands": [c.to_dict() for c in got]}


def handle_result(command_id: str, body: dict) -> dict:
    """
    插件报执行结果。

    ★ 插件 POST 的是它执行函数返回的对象本身
      （execution-journal.mjs:113：backend(...'/result','POST',entry.result)），
      所以字段是 status / failure_reason / sent_message_id / execution_token /
      provider_result_status。

    ★★ 响应必须【回显】插件用来判 ack 的字段 ——
      插件 execution-journal.mjs 的 acknowledged() 判据：
          status===200
          && data.command_id === 上报的 command_id
          && data.status === 上报的 status
          && (status==='SENT' → data.sent_message_id === 上报的 sent_message_id)
          && (status==='SUCCEEDED' → data.price_after_fen === 上报的 price_after_fen)
      ⇒ 少回任何一个，插件都认为【没被 ack】，会无限重试同一条回执。
        ★ 踩过：只回 {ok, command_id, status}，漏了 sent_message_id ——
          SENT 的回执永远不确认，日志里会一直刷重试。
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "RESULT_BODY_REQUIRED"}
    reported_status = str(body.get("status") or "")
    q = queue()
    cmd = q.record_result(
        command_id,
        status=reported_status,
        sent_message_id=str(body.get("sent_message_id") or ""),
        failure_reason=str(body.get("failure_reason") or ""),
        execution_token=str(body.get("execution_token") or ""),
        provider_result_status=str(body.get("provider_result_status") or ""),
        price_after_fen=body.get("price_after_fen"), detail=body.get("detail"))
    if cmd is None:
        return {"ok": False, "error": "COMMAND_NOT_FOUND", "command_id": command_id}
    if (cmd.type == "CHANGE_ORDER_PRICE" and cmd.status == "SUCCEEDED"
            and cmd.extra.get("replacement_quote_id")):
        from . import ledger
        from .quote_reprice import settle_quote_revision
        settle_quote_revision(ledger.stores(), cmd)
        from .plugin_runtime import sync_repriced_conversation
        sync_repriced_conversation(cmd)
    if (reported_status == "SUCCEEDED" and cmd.status == "SUCCEEDED"
            and cmd.type == "CHANGE_ORDER_PRICE"
            and body.get("price_after_fen") == cmd.extra.get("price_after_fen")
            and store_module.shop_send_enabled(cmd.shop_id)):
        from . import ledger
        from .plugin_runtime import (_sent_message_ids,
                                     build_reprice_success_commands)
        stores = ledger.stores()
        order = (stores.orders.find_by_provider_order(cmd.event_id)
                 if stores.orders is not None else None)
        if order is None:
            raise RuntimeError("REPRICE_NOTICE_ORDER_NOT_FOUND")
        quote = (stores.quotes.get(order.quote_id)
                 if stores.quotes is not None else None)
        ident = {"tenant_id": cmd.tenant_id, "shop_id": cmd.shop_id,
                 "buyer_id": cmd.buyer_id, "chat_id": cmd.chat_id}
        if (order.amount_fen == cmd.extra.get("target_amount_fen")
                and not (getattr(order, "payload", {}) or {}).get("quote_revision_pending")
                and (not cmd.extra.get("replacement_quote_id")
                     or cmd.extra["replacement_quote_id"] == order.quote_id)):
            for notice in build_reprice_success_commands(
                    cmd, order, quote, _sent_message_ids(ident)):
                q.enqueue(notice)  # stable dedupe key: repeated receipts add nothing
    if (cmd.type == "CHANGE_ORDER_PRICE" and cmd.extra.get("replacement_quote_id")
            and cmd.status in {"FAILED", "UNKNOWN", "GATE_REJECTED"}
            and store_module.shop_send_enabled(cmd.shop_id)):
        from . import ledger
        from .plugin_runtime import build_revision_unconfirmed_commands, _sent_message_ids
        stores = ledger.stores()
        order = stores.orders.find_by_provider_order(cmd.event_id) if stores.orders else None
        ident = {k: getattr(cmd, k) for k in ("tenant_id", "shop_id", "buyer_id", "chat_id")}
        for notice in build_revision_unconfirmed_commands(cmd, order, _sent_message_ids(ident)):
            q.enqueue(notice)
    out = {
        "ok": True,
        "command_id": command_id,          # ★ 插件回核用
        "status": cmd.status,              # ★ 插件回核用
    }
    # ★ 回显 SENT 的 message id（插件会逐字比对）
    if cmd.status == "SENT":
        out["sent_message_id"] = cmd.sent_message_id or ""
    if cmd.status == "SUCCEEDED" and cmd.type == "CHANGE_ORDER_PRICE":
        out["price_after_fen"] = cmd.extra.get("price_after_fen")
    for k in ("execution_token", "provider_result_status"):
        if body.get(k) is not None:
            out[k] = body.get(k)
    return out


def handle_recovery_inspect() -> dict:
    """
    插件崩溃恢复时来查"有没有我领了但没报结果的命令"。

    ★ 为什么必须有：插件领了命令、执行到一半崩了，那条命令就卡在 CLAIMED。
      没有这个端点，它会永远卡着（既不会重试也不会失败）。

    ★★ 响应形状必须【严格照插件读法】（execution-journal.mjs 的 inspectRecovery）：
          if (response.status!==200 || !Array.isArray(response.data?.commands))
              throw new Error('RECOVERY_INSPECTION_FAILED');
          project(response.data.commands, 'command_id')   // 读 item.command_id/type/status/action
          project(response.data.events,   'event_id')
          project(response.data.bookings, 'booking_id')
      ⇒ 三个键【都必须是数组】。★ 踩过：我回的是 {claimed, count} ——
        插件找不到 data.commands 直接报 RECOVERY_INSPECTION_FAILED，
        然后每轮都报，日志一直刷错（真插件一起来就暴露了）。
      ★ events / bookings 我们还没有对应概念，回空数组（不是省略）——
        省略会让 Array.isArray(undefined) 为 false，照样失败。
    """
    q = queue()
    stuck = q.by_status("CLAIMED")
    commands = []
    for c in stuck:
        d = c.to_dict()
        commands.append({
            "command_id": d.get("command_id"),
            "type": d.get("type"),
            "status": d.get("status"),
            # ★ action 是插件要判"崩溃前在做什么"的字段。
            #   我们没有更细的动作阶段，就用命令类型 —— 不是编的，
            #   是"这条命令要做的动作就是它的类型"。
            "action": d.get("type"),
        })
    events = []
    for raw in q.pending_events(50):
        events.append({
            "event_id": event_key_of(raw),
            "type": str(raw.get("event") or ""),
            "status": "RECEIVED",
            "action": "PROCESS",
        })
    return {
        "ok": True,
        "commands": commands,
        "events": events,
        # ★ 空数组，不是省略：插件对三个键都做 Array.isArray
        "bookings": [],
        "count": len(commands),
    }


def handle_fulfillment_auto(*, run: bool = True) -> dict:
    """
    自动出票 / 自动发货。

    ★ 插件每轮都调（server.mjs:53 的 drain）——
      "买家付款后自动发取票码、自动发货"就是在这里发生的。
      ★ 现阶段还没有真实出票来源（店家在后台手动出票），
        所以这里只把【已交付但还没发货】的单找出来排队，
        不假装出票成功。
    """
    from .plugin_runtime import queue_shipping_for_delivered
    if not run:
        return {"ok": True, "queued": 0}
    queued = queue_shipping_for_delivered()
    return {"ok": True, "queued": queued}


# ==================================================================== 状态
def queue_status() -> dict:
    """给界面看：队列里现在有什么。"""
    q = queue()
    counts = {}
    for st in ("PENDING", "CLAIMED", "SENDING", "SENT", "WOULD_SEND",
               "FAILED", "UNKNOWN", "SUPPRESSED", "GATE_REJECTED"):
        counts[st] = len(q.by_status(st))
    counts["events_pending"] = len(q.pending_events(200))
    counts["secret_configured"] = bool(plugin_secret())
    counts["db"] = str(store_module.DATA / "plugin-queue.sqlite3")
    return counts
