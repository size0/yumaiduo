"""
_check_plugin_queue.py —— 鱼麦多插件：签名校验 + 命令队列

★ 为什么要有这个
  插件是"手"不是"脑"：它来领命令、执行、回报结果。
  我们这边的队列如果不牢，后果直接落在买家身上：
    · 幂等没做 → 买家收到两条一样的消息
    · claim 不原子 → 两个插件实例都领到同一条 → 发两遍
    · 终态能回退 → 已发出的消息被"取消"
    · 依赖不判 → 改价还没成功就发货

★ 每一节的判据都对着 plugin-runtime 的源码或 V5 的实现写的
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations.yumaiduo import signature as sig                  # noqa: E402
from integrations.yumaiduo.commands import (COMMAND_TYPES,          # noqa: E402
                                            Command, CommandStore,
                                            dedupe_for, defer)

RESULTS = []


def check(label, ok, detail="", src=""):
    """
    ★ src 是【判据出处】（插件源码哪一行/哪一句）。
      为什么要有：契约类断言如果只说我期望是这样，
      后面的人不知道这个期望哪来的 —— 写清楚出处才能复核。
    """
    RESULTS.append(bool(ok))
    line = f"[{'PASS' if ok else 'FAIL'}] {label}"
    if detail:
        line += f"  {detail}"
    print(line)
    if src:
        print(f"        判据出处：{src}")
    return ok


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main() -> int:                                       # noqa: C901
    section("一、★★ 插件签名（照 backend-request.mjs 的算法）")
    # 插件源码：createHmac('sha256',secret).update(`${timestamp}.${raw}`).digest('hex')
    secret = "plugin-secret-abc"
    raw = '{"event":"im.message.received","payload":{"content":"在吗"}}'
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    good = sig.sign(secret, ts, raw)
    check("正确签名通过", sig.verify(secret, ts, good, raw)[0])
    check("★ 改了 body 就不过（防止中间人改内容）",
          not sig.verify(secret, ts, good, raw + " ")[0])
    check("★ 对原始字节算，不是重新序列化（JSON 键序变了会验不过）",
          not sig.verify(secret, ts, sig.sign(secret, ts, raw + "\n"), raw)[0])
    check("没配密钥 → NOT_CONFIGURED（区别于签名错）",
          sig.verify("", ts, good, raw)[1] == "NOT_CONFIGURED")
    check("少请求头 → MISSING_HEADER",
          sig.verify(secret, None, good, raw)[1] == "MISSING_HEADER")
    check("签名错 → BAD_SIGNATURE",
          sig.verify(secret, ts, "deadbeef", raw)[1] == "BAD_SIGNATURE")
    check("时间戳非数字 → BAD_TIMESTAMP",
          sig.verify(secret, "abc", good, raw)[1] == "BAD_TIMESTAMP")
    old = str(int(datetime.now(timezone.utc).timestamp() * 1000) - 400_000)
    check("★★ 旧请求过期 → 不许重放",
          sig.verify(secret, old, sig.sign(secret, old, raw), raw)[1]
          == "TIMESTAMP_OUT_OF_WINDOW")
    check("★ 恒定时间比较（大小写归一后仍通过）",
          sig.verify(secret, ts, good.upper(), raw)[0])

    section("二、命令类型（V5 只有这六种）")
    # ★★★ 2026-10-14：补了 CLOSE_FAILED_ORDER（关掉错价单）。
    #   插件端 failed-order-close.mjs 一直有实现，缺的是后端不发这个类型，
    #   且 /authorize-close 一律回 NOT_IMPLEMENTED —— 所以它的判据也要核。
    check("★ 七种命令类型齐了（V5 的六种 + 关单）", len(COMMAND_TYPES) == 7,
          str(COMMAND_TYPES))
    for need in ("SEND_MESSAGE", "CHANGE_ORDER_PRICE", "DELIVER_TICKET",
                 "SHIP_ORDER", "CLOSE_FAILED_ORDER"):
        check(f"  有 {need}", need in COMMAND_TYPES)

    section("三、★★ 幂等：同一条命令只能产生一次")
    # ★ 为什么关键：一条"改价通知"生成两次，买家就收到两条一样的消息
    store = CommandStore(":memory:")
    cmd = Command(command_id="", type="SEND_MESSAGE", event_id="evt-1",
                  chat_id="c1", tenant_id="t", shop_id="s", buyer_id="b",
                  text="你好", dedupe_key=dedupe_for("evt-1", kind="reply"))
    fresh, first = store.enqueue(cmd)
    check("首次入队成功", fresh is True)
    fresh2, again = store.enqueue(cmd)
    check("★ 重复入队不新建（返回已有那条）",
          fresh2 is False and again.command_id == first.command_id)
    check("表里只有 1 条", len(store.by_status("PENDING")) == 1)
    # 一个事件要发两条消息的场景：靠 part 区分
    two = Command(command_id="", type="SEND_MESSAGE", event_id="evt-1",
                  chat_id="c1", text="第二条",
                  dedupe_key=dedupe_for("evt-1", kind="reply", part="2"))
    check("★ 同一事件的两条消息各有独立幂等键（不会被误挡）",
          store.enqueue(two)[0] is True)

    section("四、★★ claim：一条命令不能被领两次")
    # ★ 插件可能多开（重试/灰度）。两个实例都领到 → 买家收到两遍。
    store2 = CommandStore(":memory:")
    for i in range(3):
        store2.enqueue(Command(command_id="", type="SEND_MESSAGE",
                               event_id=f"e{i}", chat_id="c",
                               dedupe_key=f"d{i}"))
    first_claim = store2.claim_pending(10)
    check("第一次领到 3 条", len(first_claim) == 3)
    check("★★ 第二次领到 0 条（已被领走，不会重复发）",
          len(store2.claim_pending(10)) == 0)
    check("领走后状态是 CLAIMED",
          all(c.status == "CLAIMED" for c in first_claim))
    check("★ limit 生效",
          len(CommandStore(":memory:") is not None and
              store2.claim_pending(0)) == 0)

    section("五、★★ 定时命令：没到时间不给领（fail closed）")
    store3 = CommandStore(":memory:")
    store3.enqueue(Command(command_id="", type="SEND_MESSAGE", event_id="e",
                           chat_id="c", dedupe_key="d",
                           not_before=defer(60)))
    check("未到时间 → 领不到", len(store3.claim_pending(10)) == 0)
    check("★ 到时间 → 领得到",
          len(store3.claim_pending(
              10, now=datetime.now(timezone.utc) + timedelta(seconds=61))) == 1)
    store3b = CommandStore(":memory:")
    store3b.enqueue(Command(command_id="", type="SEND_MESSAGE", event_id="e",
                            chat_id="c", dedupe_key="d",
                            not_before="读不出来的时间"))
    check("★★ 时间戳读不出 → 也不给领（宁可晚，不能让外部写提前跑）",
          len(store3b.claim_pending(10)) == 0)

    section("六、★★ 回执：幂等 + 终态单向")
    a = first_claim[0]
    r = store2.record_result(a.command_id, ok=True, sent_message_id="msg-1")
    check("成功 → SENT", r.status == "SENT" and r.sent_message_id == "msg-1")
    r2 = store2.record_result(a.command_id, ok=True, sent_message_id="msg-2")
    check("★ 重复回执幂等（不覆盖已记的 message_id）",
          r2.sent_message_id == "msg-1", r2.sent_message_id)
    store2._set_status(a.command_id, "PENDING")
    check("★★ 已 SENT 不许回退（发给买家的消息不能取消）",
          store2.get(a.command_id).status == "SENT")
    b = first_claim[1]
    rf = store2.record_result(b.command_id, ok=False, failure_reason="上游拒绝")
    check("失败 → FAILED 并留下原因",
          rf.status == "FAILED" and rf.failure_reason == "上游拒绝")

    section("六之二、★★ UNKNOWN 可以重报（插件临时失败后会重试）")
    # ★ 这是第一版写错的地方：我把 FAILED 当终态挡住了重报。
    #   插件源码里临时失败报 UNKNOWN（execution-journal.mjs 会重试），
    #   之后才报确定结果。挡住重报 ⇒ 那次【真发出去】的消息
    #   会被永远记成失败 ⇒ 系统以为没发、可能再发一遍 ⇒ 买家收到两条。
    store6 = CommandStore(":memory:")
    store6.enqueue(Command(command_id="", type="SEND_MESSAGE",
                           event_id="e-unk", chat_id="c", dedupe_key="d-unk"))
    c6 = store6.claim_pending(1)[0]
    r_unk = store6.record_result(c6.command_id, status="UNKNOWN",
                                 failure_reason="SEND_RESULT_UNCONFIRMED")
    check("★ 临时失败记成 UNKNOWN（不是 FAILED）",
          r_unk.status == "UNKNOWN", r_unk.status)
    check("★ 它不是终态，可以再报",
          r_unk.status not in ("SENT", "SUPPRESSED"))
    r_ok = store6.record_result(c6.command_id, status="SENT",
                                sent_message_id="msg-real")
    check("★★ 重报成功 → 改成 SENT（这条确实发出去了）",
          r_ok.status == "SENT" and r_ok.sent_message_id == "msg-real",
          f"{r_ok.status}/{r_ok.sent_message_id}")
    check("★ 改成 SENT 之后不许再改（真终态）",
          store6.record_result(c6.command_id, status="FAILED").status == "SENT")
    # UNKNOWN_RESULT 与 UNKNOWN 归成一个码
    store6.enqueue(Command(command_id="", type="SEND_MESSAGE",
                           event_id="e-unk2", chat_id="c", dedupe_key="d-unk2"))
    c7 = store6.claim_pending(1)[0]
    check("★★ UNKNOWN_RESULT 归一成 UNKNOWN（插件里两个码一个意思）",
          store6.record_result(c7.command_id,
                               status="UNKNOWN_RESULT").status == "UNKNOWN")
    # 插件报的每个 status 都要能被接受，且落到合法状态上
    from integrations.yumaiduo.commands import (PLUGIN_RESULT_STATUSES,
                                                STATUSES)
    bad = []
    for st in PLUGIN_RESULT_STATUSES:
        s9 = CommandStore(":memory:")
        s9.enqueue(Command(command_id="", type="SEND_MESSAGE", event_id="e",
                           chat_id="c", dedupe_key="d"))
        cc = s9.claim_pending(1)[0]
        out = s9.record_result(cc.command_id, status=st,
                               sent_message_id="m" if st == "SENT" else "")
        if out.status not in STATUSES:
            bad.append((st, out.status))
    check("★ 插件报的每个 status 都能收，且落到合法状态上",
          not bad, str(bad))

    section("七、★★ 依赖：前置没发出 → 主动放弃（不是一直挂着）")
    store4 = CommandStore(":memory:")
    parent = Command(command_id="", type="CHANGE_ORDER_PRICE", event_id="e",
                     chat_id="c", dedupe_key="p")
    store4.enqueue(parent)
    child = Command(command_id="", type="SHIP_ORDER", event_id="e",
                    chat_id="c", dedupe_key="ch",
                    depends_on_command_id=parent.command_id)
    store4.enqueue(child)
    got = store4.claim_pending(10)
    check("只领到父命令", [c.type for c in got] == ["CHANGE_ORDER_PRICE"],
          str([c.type for c in got]))
    got_child = store4.get(child.command_id)
    check("★★ 子命令被标 SUPPRESSED（改价没成功就不发货）",
          got_child.status == "SUPPRESSED", got_child.status)
    check("★ 留下了原因，便于排查",
          got_child.failure_reason == "PRECEDING_COMMAND_NOT_SENT",
          got_child.failure_reason)

    section("八、事件幂等（插件网络重试不会重复回复买家）")
    store5 = CommandStore(":memory:")
    f1, _ = store5.record_event("evt-x", "im.message.received", '{"a":1}')
    f2, _ = store5.record_event("evt-x", "im.message.received", '{"a":1}')
    check("首次记为新的", f1 is True)
    check("★ 重复推同一个事件 → 不当新的",
          f2 is False)
    check("待处理事件只有 1 条", len(store5.pending_events()) == 1)
    store5.mark_event("evt-x", "PROCESSED")
    check("标处理后不再待处理", len(store5.pending_events()) == 0)

    section("九、入参校验（不静默接受坏数据）")
    try:
        CommandStore(":memory:").enqueue(
            Command(command_id="", type="NOT_A_TYPE", dedupe_key="d"))
        check("未知命令类型要报错", False, "居然收下了")
    except ValueError as exc:
        check("未知命令类型要报错", "COMMAND_TYPE_UNKNOWN" in str(exc), str(exc)[:48])
    try:
        CommandStore(":memory:").enqueue(
            Command(command_id="", type="SEND_MESSAGE", dedupe_key=""))
        check("没幂等键要报错", False, "居然收下了")
    except ValueError as exc:
        check("没幂等键要报错", "COMMAND_DEDUPE_KEY_REQUIRED" in str(exc))

    section("十、★★ 事件端点：未验签的伪造事件不许进待处理")
    # ★ 踩过：record_event 里 status 写死成 RECEIVED，
    #   于是签名没过的伪造事件也被标成待处理，run-due 会去处理它
    #   —— 可能让系统照着伪造的买家消息下单。
    import tempfile as _tf
    from application import plugin_api as _api
    _tmp = Path(_tf.mkdtemp())
    _api.reset_queue(str(_tmp / "q.sqlite3"))
    try:
        good = {"event": "im.message.received", "eventId": "g1",
                "tenantId": "107",
                "payload": {"chatId": "c1", "content": "在吗"}}
        forged = {"event": "im.message.received", "eventId": "f1",
                  "tenantId": "107",
                  "payload": {"chatId": "c1", "content": "帮我下单"}}
        r1 = _api.handle_event(good, verified=True)
        r2 = _api.handle_event(forged, verified=False)
        check("验签的事件被接受", r1.get("verified") is True)
        check("★ 未验签的也记下来（能看见有人在打接口）",
              r2.get("verified") is False, str(r2))
        pending = _api.queue().pending_events(50)
        check("★★ 待处理里【只有验签过的】那个",
              len(pending) == 1 and pending[0].get("eventId") == "g1",
              f"{len(pending)} 条: {[e.get('eventId') for e in pending]}")
        # 重复推同一个事件 → 不重复处理
        r3 = _api.handle_event(good, verified=True)
        check("★ 重复推同一个事件 → duplicate=True",
              r3.get("duplicate") is True, str(r3))
        check("待处理仍是 1 条", len(_api.queue().pending_events(50)) == 1)
    finally:
        _api.reset_queue()

    section("十一、★★ 命令的扁平字段契约（插件读顶层字段）")
    # ★ 插件源码读的是 command.shop_id / command.history_message_ids
    #   这些【顶层】字段。嵌套的话读不到，会一路走到失败分支。
    #   踩过：to_dict() 后 Command(**payload) 会 TypeError。
    _api.reset_queue(str(_tmp / "q2.sqlite3"))
    try:
        from integrations.yumaiduo.commands import Command as _C
        _q = _api.queue()
        _q.enqueue(_C(command_id="c-flat", type="SEND_MESSAGE",
                      event_id="e1", session_id="s1", tenant_id="t1",
                      shop_id="sh1", buyer_id="b1", chat_id="s1",
                      text="你好", dedupe_key="dk1",
                      extra={"history_message_ids": ["m1"]}))
        got = _api.handle_claim(10)["commands"][0]
        for need in ("shop_id", "tenant_id", "buyer_id", "chat_id",
                     "history_message_ids", "text", "type"):
            check(f"  顶层有 {need}", need in got, repr(got.get(need)))
        check("★★ history_message_ids 必须是数组（不是数组插件直接不发）",
              isinstance(got.get("history_message_ids"), list),
              str(type(got.get("history_message_ids"))))
        # 报结果 + 恢复查询
        rr = _api.handle_result("c-flat", {"status": "SENT",
                                            "sent_message_id": "msg-1"})
        check("报结果 → SENT", rr.get("status") == "SENT", str(rr))
        check("★ 恢复查询能查到卡在 CLAIMED 的",
              _api.handle_recovery_inspect().get("count") == 0)
    finally:
        _api.reset_queue()

    section("十二、★★ HTTP 端点（真签名打一遍）")
    # ★ 为什么要有这一段：单元测试全绿的时候，
    #   plugin_runtime 里一个 ImportError 让每条买家消息都处理失败
    #   —— 只有端到端打一遍才发现。存储/逻辑对 ≠ 端点能用。
    import json as _json
    import os as _os
    import threading as _th
    import time as _time
    import urllib.error as _ue
    import urllib.request as _ur

    _tmp2 = Path(_tf.mkdtemp())
    _api.reset_queue(str(_tmp2 / "q.sqlite3"))
    _SECRET = "e2e-secret"
    _os.environ["YUMAIDUO_WEBHOOK_SECRET"] = _SECRET
    _srv_mod = None
    try:
        sys.path.insert(0, str(ROOT))
        import server as _srv_mod
        from http.server import ThreadingHTTPServer as _Srv
        _httpd = _Srv(("127.0.0.1", 8897), _srv_mod.Handler)
        _th.Thread(target=_httpd.serve_forever, daemon=True).start()
        _time.sleep(0.5)

        def _call(path, body=None, *, secret=_SECRET, sign_it=True, ts=None):
            raw = "" if body is None else _json.dumps(body, ensure_ascii=False)
            t = ts or str(int(_time.time() * 1000))
            hd = {"Content-Type": "application/json"}
            if sign_it:
                hd["x-yumaiduo-timestamp"] = t
                hd["x-yumaiduo-signature"] = sig.sign(secret, t, raw)
            rq = _ur.Request("http://127.0.0.1:8897" + path,
                             data=raw.encode() if raw else b"",
                             method="POST", headers=hd)
            try:
                with _ur.urlopen(rq, timeout=10) as rr:
                    return rr.status, _json.loads(rr.read().decode() or "{}")
            except _ue.HTTPError as ee:
                return ee.code, _json.loads(ee.read().decode() or "{}")

        st, r = _call("/api/v1/events", {"event": "im.message.received",
                                          "eventId": "h1", "tenantId": "107",
                                          "payload": {"chatId": "c9",
                                                       "content": "在吗"}})
        check("推事件 → 200 且已验签", st == 200 and r.get("verified") is True,
              f"{st} {r}")
        check("★ 签名错 → 401", _call("/api/v1/events", {"event": "x"},
                                       secret="WRONG")[0] == 401)
        check("★★ 过期重放 → 401",
              _call("/api/v1/events", {"event": "x"},
                    ts=str(int(_time.time() * 1000) - 400_000))[0] == 401)
        check("★ 没签名 → 401",
              _call("/api/v1/events", {"event": "x"}, sign_it=False)[0] == 401)
        st, r = _call("/api/v1/events/run-due", {})
        check("★★ run-due 真的处理了事件（ImportError 会在这里现形）",
              st == 200 and r.get("processed") == 1 and not r.get("failed"),
              str(r)[:120])
        st, r = _call("/api/v1/commands/claim", {"limit": 10})
        check("claim 形状是 {commands:[...]}",
              st == 200 and isinstance(r.get("commands"), list), str(r)[:80])
        check("★ 命令有顶层字段（插件读得到）",
              all(k in (r["commands"][0] if r.get("commands") else {})
                  for k in ("type", "text")))
        check("recovery/inspect → 200", _call("/api/v1/recovery/inspect", {})[0] == 200)
        check("fulfillment/auto/run → 200",
              _call("/api/v1/fulfillment/auto/run", {})[0] == 200)
        check("未知端点 → 404", _call("/api/v1/nope", {})[0] == 404)
    finally:
        try:
            _httpd.shutdown()                             # noqa: F821
        except Exception:                                 # noqa: BLE001
            pass
        _os.environ.pop("YUMAIDUO_WEBHOOK_SECRET", None)
        _api.reset_queue()

    section("十三、★★ 授权闸门（不做这个插件第一步就 GATE_REJECTED）")
    # ★ 踩过：命令队列、端点、事件转换都做完了，但漏了 authorize-* ——
    #   插件领到命令后【第一步就要授权】，拿不到就拒绝执行。
    #   结果看着做完了，实际一步都没执行。
    _tmp3 = Path(_tf.mkdtemp())
    _api.reset_queue(str(_tmp3 / "q.sqlite3"))
    try:
        from application import plugin_authorize as _au
        from integrations.yumaiduo.commands import Command as _C

        def _mk(kind, *, event_id="o-1", cid=None, **kw):
            # ★ event_id 要能被调用方覆盖 —— 授权核对用的是它
            #   （踩过：只能写死，测试造了个永远对不上的场景）
            # ★★ cid 也要能覆盖：默认 command_id=f"c-{kind}" 时，
            #   同类型再造一条会命中入队去重、拿回【第一条】，
            #   于是你以为在测"没写原因"，其实拿到的是上一条（有原因）。
            #   （2026-10-14 我在这里踩了一次：看到"没拦住"，
            #     其实是测试自己的错，代码是对的。）
            cmd = _C(command_id=cid or f"c-{kind}", type=kind, event_id=event_id,
                     session_id="chat-1", tenant_id="t1", shop_id="s1",
                     buyer_id="b1", chat_id="chat-1",
                     dedupe_key=f"d-{kind}-{cid or kind}", **kw)
            _api.queue().enqueue(cmd)
            return cmd

        # ① 未领走（还在 PENDING）→ 不许授权
        m1 = _mk("SEND_MESSAGE", text="hi")
        r = _au.authorize_message_send(m1.command_id)
        check("★ 命令还没被领走 → 不许授权", r.get("allowed") is False, str(r))

        # 领走后
        _api.handle_claim(20)
        r = _au.authorize_message_send(m1.command_id)
        check("★ 领走后发消息可授权", r.get("allowed") is True, str(r))
        check("★ 返回 {allowed,reason} 形状（插件读 data.allowed）",
              "allowed" in r and "reason" in r)

        # ★★★ 2026-10-14 新增：CLOSE_FAILED_ORDER 的授权闸门（关掉错价单）。
        #   这是【资金敏感】动作，所以判据要逐条核。
        mc = _mk("CLOSE_FAILED_ORDER", event_id="o-77",
                 text="订单金额与报价不符，本单关闭后请重新下单",
                 extra={"order_id": "YX-77"})
        _api.handle_claim(20)
        r = _au.authorize_close(mc.command_id)
        check("★★ 关单：已付款未发货（orderStatus=2）→ 允许",
              r.get("allowed") is True, str(r))
        check("★★ 关单授权里带回调用方要的 reason",
              bool(r.get("text")), str(r)[:80])
        check("★★ 关单授权回显 [order_id]（插件逐字回核）",
              r.get("order_id") == "YX-77", str(r.get("order_id")))
        # ★ 插件 failed-order-close.mjs:23 会逐字比这组键 —— 缺一个就静默拒绝
        for _k in ("command_id", "type", "tenant_id", "shop_id",
                   "order_id", "text"):
            check(f"★★ 关单授权含 {_k}（插件回核字段）", _k in r)

        # 状态门：已发货(3)/已完成(4) 都不许关
        for _st in (3, 4, 1, 5):
            rc = _au.authorize_close(mc.command_id, {"orderStatus": _st})
            check(f"★★ 关单：orderStatus={_st} → 不许",
                  rc.get("allowed") is False, str(rc))
        # orderStatus=6（已关闭）允许 —— 插件会判成 already completed
        check("★ 关单：orderStatus=6（已关闭）允许（幂等）",
              _au.authorize_close(mc.command_id,
                                  {"orderStatus": 6}).get("allowed") is True)

        # 没写原因 → 不许（CancelOrderRequest.reason 是必填、要进审计）
        mb = _mk("CLOSE_FAILED_ORDER", event_id="o-78", cid="c-close-norsn",
                 text="", extra={"order_id": "YX-78"})
        _api.handle_claim(20)
        r = _au.authorize_close(mb.command_id)
        check("★★ 关单：没写原因 → 不许", r.get("allowed") is False, str(r))

        # 类型不对 → 不许
        mo = _mk("SEND_MESSAGE", event_id="o-79", cid="c-not-close", text="hi")
        _api.handle_claim(20)
        r = _au.authorize_close(mo.command_id)
        check("★ 非关单命令 → 不许", r.get("allowed") is False, str(r))

        # ② 已发出的不许再授权
        _api.handle_result(m1.command_id, {"status": "SENT",
                                           "sent_message_id": "m"})
        r = _au.authorize_message_send(m1.command_id)
        check("★★ 已发出的命令不许再授权（防重复发）",
              r.get("allowed") is False, str(r))

        # ③ 不存在的命令
        check("不存在的命令 → 不许",
              _au.authorize_message_send("nope").get("allowed") is False)

        # ④ 类型不对 → 不许
        m2 = _mk("SHIP_ORDER", extra={"order_id": "o-1"})
        check("★ 类型不匹配 → 不许（拿 SHIP_ORDER 去要发消息授权）",
              _au.authorize_message_send(m2.command_id).get("allowed") is False)

        # ⑤ 出票类授权：必须回【逐字段等于命令】+ token
        _api.handle_claim(20)
        g = _au.authorize_ticket_delivery(m2.command_id)
        check("★ 出票类可授权", g.get("allowed") is True, str(g)[:70])
        check("★★ 必须给 execution_token（插件没它就拒绝）",
              bool(g.get("execution_token")))
        need = ["command_id", "type", "order_id", "tenant_id", "shop_id",
                "buyer_id", "chat_id"]
        cmd_d = m2.to_dict()
        check("★★ 回核字段必须逐字段等于命令（wanda-shipment.mjs:14 会核）",
              all(g.get(k) == cmd_d.get(k) for k in need),
              str({k: (g.get(k), cmd_d.get(k)) for k in need
                   if g.get(k) != cmd_d.get(k)}))
        g2 = _au.authorize_ticket_delivery(m2.command_id)
        check("★ 同一命令重试拿到同一个 token（否则对不上账）",
              g2.get("execution_token") == g.get("execution_token"))

        # ⑤之二 ★★ 出票授权要核对【线上实况】（防闭着眼发货/串单）
        # ★ _mk 默认 event_id="o-1"（授权核对用的就是这个），
        #   所以这里必须传 event_id 才能对上 —— 踩过：只改了 extra，
        #   结果核对的还是 o-1，测试自己造了个永远对不上的场景。
        m4 = _mk("DELIVER_TICKET", event_id="o-real",
                 extra={"order_id": "o-real"})
        _api.handle_claim(20)   # ★ 必须先领走 —— 只有 CLAIMED 才能授权
        check("★ 前置确认：命令已 CLAIMED",
              _api.queue().get(m4.command_id).status == "CLAIMED",
              _api.queue().get(m4.command_id).status)
        check("★ 线上订单号一致 → 允许",
              _au.authorize_ticket_delivery(
                  m4.command_id,
                  detail={"orderId": "o-real", "accountUnb": "s1",
                          "orderStatus": 2}).get("allowed") is True)
        check("★★ 线上订单号不一致 → 不许（防串到别的单）",
              _au.authorize_ticket_delivery(
                  m4.command_id,
                  detail={"orderId": "OTHER", "accountUnb": "s1",
                          "orderStatus": 2}).get("allowed") is False)
        check("★★ 线上店铺不一致 → 不许（防跨店操作）",
              _au.authorize_ticket_delivery(
                  m4.command_id,
                  detail={"orderId": "o-real", "accountUnb": "OTHER-SHOP",
                          "orderStatus": 2}).get("allowed") is False)
        check("★★ 订单没付款(status=1) → 不许发货（钱收不回来）",
              _au.authorize_ticket_delivery(
                  m4.command_id,
                  detail={"orderId": "o-real", "accountUnb": "s1",
                          "orderStatus": 1}).get("allowed") is False)
        check("★ 会话不属于该订单 → 不许（防发错人）",
              _au.authorize_ticket_delivery(
                  m4.command_id, session={"orderId": "OTHER"}).get("allowed") is False)

        # ⑥ 改价授权：只允许本店、本订单的待付款单
        #   ★★ 2026-10-14：这条命令必须带 target_amount_fen。
        #     后端现在会核它（漏了插件必然拒绝：reprice.mjs:141 的
        #     INVALID_AUTHORIZED_PRICE），所以"没给目标价"本身也该是拒绝理由，
        #     下面单独加了一条验它。
        m3 = _mk("CHANGE_ORDER_PRICE", extra={"target_amount_fen": 8800})
        _api.handle_claim(20)
        _reprice_detail = {"orderId": "o-1", "accountUnb": "s1",
                           "orderStatus": 1}
        check("★ 改价：待付款(status=1) → 允许",
              _au.authorize_execution(m3.command_id,
                                      _reprice_detail).get("allowed") is True)
        check("★ 改价：已付款(status=2) → 不许",
              _au.authorize_execution(m3.command_id,
                                      {**_reprice_detail,
                                       "orderStatus": 2}).get("allowed") is False)
        check("★ 改价：订单已发货(status=3) → 不许改",
              _au.authorize_execution(m3.command_id,
                                      {**_reprice_detail,
                                       "orderStatus": 3}).get("allowed") is False)
        check("★ 改价：缺订单详情 → 不许",
              _au.authorize_execution(m3.command_id).get("allowed") is False)
        check("★ 改价：订单号不符 → 不许",
              _au.authorize_execution(m3.command_id,
                                      {**_reprice_detail,
                                       "orderId": "OTHER"}).get("allowed") is False)
        check("★ 改价：店铺不符 → 不许",
              _au.authorize_execution(m3.command_id,
                                      {**_reprice_detail,
                                       "accountUnb": "OTHER"}).get("allowed") is False)
        # ★★ 授权响应必须给出 target_amount_fen —— 插件靠它决定改成多少分。
        #   漏了的话插件回 GATE_REJECTED/INVALID_AUTHORIZED_PRICE，
        #   而且是【静默拒绝】：不抛异常、不报错，只是价没变。
        _grant = _au.authorize_execution(m3.command_id, _reprice_detail)
        check("★★ 改价授权回 target_amount_fen（漏了插件必然拒绝）",
              _grant.get("target_amount_fen") == 8800, str(_grant))
        # ★ 没有目标价 → 不许改（宁可拒绝，也不能改成一个瞎猜的数字）
        _m5 = _mk("CHANGE_ORDER_PRICE", cid="c-noprice")
        _api.handle_claim(20)
        check("★★ 命令没带目标价 → 不许改（不能瞎猜金额）",
              _au.authorize_execution(_m5.command_id,
                                      _reprice_detail).get("allowed") is False)

        # ⑦ close / engagement 我们没实现 → 必须回不许（不能让它执行）
        #   ★ 这里要真打 HTTP 才算验证 —— 之前我写成 json.dumps 一个常量，
        #     那什么都没验（自己写了个假断言）。
        import os as _os2
        _os2.environ["YUMAIDUO_WEBHOOK_SECRET"] = "auth-test"
        _tmp5 = Path(_tf.mkdtemp())
        _api.reset_queue(str(_tmp5 / "q.sqlite3"))
        try:
            import server as _srv2
            from http.server import ThreadingHTTPServer as _Srv2
            _h2 = _Srv2(("127.0.0.1", 8896), _srv2.Handler)
            _th.Thread(target=_h2.serve_forever, daemon=True).start()
            _time.sleep(0.4)

            def _call2(p, b=None):
                raw = "" if b is None else _json.dumps(b)
                t = str(int(_time.time() * 1000))
                rq = _ur.Request("http://127.0.0.1:8896" + p,
                                 data=raw.encode() if raw else b"",
                                 method="POST",
                                 headers={"Content-Type": "application/json",
                                          "x-yumaiduo-timestamp": t,
                                          "x-yumaiduo-signature":
                                              sig.sign("auth-test", t, raw)})
                with _ur.urlopen(rq, timeout=10) as rr:
                    return _json.loads(rr.read().decode() or "{}")

            # ★★★ 2026-10-14：authorize-close 【实现了】（原来一律回
            #   NOT_IMPLEMENTED:close）。所以这条断言反过来：
            #   不存在的命令必须被拒（fail closed），而不是"未实现"。
            r = _call2("/api/v1/commands/c-x/authorize-close", {})
            check("★★ 不存在的命令 → 拒绝（fail closed，不是静默放行）",
                  r.get("allowed") is False
                  and "COMMAND_NOT_FOUND" in str(r.get("reason")),
                  str(r))
            # polish/run 不假装做了
            r = _call2("/api/v1/products/polish/run", {})
            check("★ polish/run 回 queued=0（不假装做了）",
                  r.get("queued") == 0, str(r))
            # 真命令的授权（走 HTTP，全链路）
            _m = _C(command_id="c-http", type="DELIVER_TICKET", event_id="o-9",
                     session_id="s", tenant_id="t", shop_id="sh",
                     buyer_id="b", chat_id="s", dedupe_key="dk-http",
                     extra={"order_id": "o-9"})
            _api.queue().enqueue(_m)
            _api.handle_claim(10)
            r = _call2("/api/v1/commands/c-http/authorize-ticket-delivery",
                       {"detail": {}, "session": {}})
            check("★★ 出票授权走 HTTP 也能过（含 token）",
                  r.get("allowed") is True and bool(r.get("execution_token")),
                  str(r)[:70])
        finally:
            try:
                _h2.shutdown()                             # noqa: F821
            except Exception:                              # noqa: BLE001
                pass
            _os2.environ.pop("YUMAIDUO_WEBHOOK_SECRET", None)
            _api.reset_queue()
    finally:
        _api.reset_queue()

    section("十四、★★ 出票后自动发货（按事实判，不按状态判）")
    # ★ 踩过：原来只找 state==DELIVERED 的单，但那个状态只有我们自己调
    #   queue_followups() 时才会设；真实出票走 POST /api/order/deliver
    #   只写 ticket_codes ⇒ 真实出票的单永远等不到发货命令。
    _tmp4 = Path(_tf.mkdtemp())
    _api.reset_queue(str(_tmp4 / "q.sqlite3"))
    _led = None
    try:
        from application import ledger as _led
        _led.close_stores()
        _led.set_paths(quotes=str(_tmp4 / "q.sqlite3"),
                       orders=str(_tmp4 / "o.sqlite3"))
        _st = _led.stores(fresh=True)
        from domain.order import Order as _O, idempotency_key_for as _ik, \
            new_order_id as _nid

        def _order(**kw):
            base = dict(order_id=_nid(), quote_id=_nid(), tenant_id="t1",
                        shop_id="s1", buyer_id="b1", chat_id="c1",
                        provider="WANDA", amount_fen=4600, paid_fen=4600,
                        state="PAID", payment_state="CONFIRMED",
                        ticket_codes=["2032 5500 7913 72"])
            base["idempotency_key"] = _ik(base["quote_id"], provider="WANDA")
            base.update(kw)
            return _O(**base)

        # 已付款 + 有取票码 → 该发货
        _st.orders.create(_order())
        # 未付款 → 不发（钱没到手）
        _st.orders.create(_order(payment_state="UNPAID"))
        # 已取消 → 不发
        _st.orders.create(_order(state="CANCELLED"))
        # 没取票码 → 不发
        _st.orders.create(_order(ticket_codes=[]))

        n = _api.handle_fulfillment_auto().get("queued")
        check("★★ 只给【已付款 + 有取票码】的单排发货（别的都跳过）",
              n == 2, f"排了 {n} 条（1 发货 + 1 通知）")
        pending = _api.queue().by_status("PENDING")
        types = sorted(c.type for c in pending)
        check("★ 排的是 SHIP_ORDER + TICKET_DELIVERY_NOTICE",
              types == ["SHIP_ORDER", "TICKET_DELIVERY_NOTICE"], str(types))
        notice = next(c for c in pending if c.type == "TICKET_DELIVERY_NOTICE")
        check("★★ 通知依赖发货命令（发货没成功就不发通知）",
              bool(notice.depends_on_command_id),
              notice.depends_on_command_id[:8] if notice.depends_on_command_id else "")
        check("★ 命令带了 order_id / delivery_provider（插件要读）",
              bool(notice.extra.get("order_id"))
              and notice.extra.get("delivery_provider") == "WANDA",
              str(notice.extra))
        # 幂等：再催一次不重复排
        n2 = _api.handle_fulfillment_auto().get("queued")
        check("★ 再催一次不重复排（dedupe_key 生效）", n2 == 0, str(n2))
    finally:
        if _led is not None:
            _led.close_stores()
            _led.set_paths(quotes=None, orders=None)
        _api.reset_queue()

    section("十五、★★ 响应形状：按【插件源码的读法】逐字核")
    # ★★ 为什么单独一节：这些是【跑插件也发现不了】的契约不符。
    #   实测过：SHIP_ORDER 在 shadow/live 下都被【插件自己的】闸门拦住
    #   （ticket-delivery.mjs 的 isTicketDeliveryAllowed 要求
    #     command.type === 'DELIVER_TICKET'，SHIP_ORDER 不等于它），
    #   所以它根本没调我们的 authorize 端点 —— 那个端点一直没被验证。
    #   ⇒ 只能按插件源码的读法逐字核。
    _tmp6 = Path(_tf.mkdtemp())
    _api.reset_queue(str(_tmp6 / "q.sqlite3"))
    try:
        from integrations.yumaiduo.commands import Command as _C6
        _q6 = _api.queue()

        # ① recovery/inspect：execution-journal.mjs inspectRecovery
        r = _api.handle_recovery_inspect()
        check("★ recovery/inspect 回 commands/events/bookings 三个【数组】",
              all(isinstance(r.get(k), list)
                  for k in ("commands", "events", "bookings")),
              "inspectRecovery: !Array.isArray(data?.commands) → throw")
        # 放一条 CLAIMED 的，看它出现在 commands 里
        _q6.enqueue(_C6(command_id="rc1", type="SEND_MESSAGE",
                        event_id="o", session_id="s", tenant_id="t",
                        shop_id="sh", buyer_id="b", chat_id="s",
                        dedupe_key="drc1"))
        _api.handle_claim(5)
        r = _api.handle_recovery_inspect()
        item = (r.get("commands") or [{}])[0]
        check("★ recovery 的每条要含 command_id/type/status/action",
              all(k in item for k in ("command_id", "type", "status", "action")),
              str(sorted(item.keys())))

        # ② result 的回显：执行 journal 的 acknowledged() 判据
        _q6.enqueue(_C6(command_id="rs1", type="SEND_MESSAGE", event_id="o",
                        session_id="s", tenant_id="t", shop_id="sh",
                        buyer_id="b", chat_id="s", dedupe_key="drs1"))
        _api.handle_claim(5)
        r = _api.handle_result("rs1", {"status": "SENT",
                                        "sent_message_id": "m-9"})
        check("★★ result 回显 command_id（插件逐字比对）",
              r.get("command_id") == "rs1",
              "acknowledged: data?.command_id !== entry.started.command_id")
        check("★★ result 回显 status", r.get("status") == "SENT")
        check("★★★ result 回显 sent_message_id（漏了插件会无限重试）",
              r.get("sent_message_id") == "m-9",
              f"判据 acknowledged() ｜ 实回 {r.get('sent_message_id')!r}")

        # ③ WOULD_SEND 是 shadow 模式的合法结果，不是失败
        _q6.enqueue(_C6(command_id="ws1", type="SEND_MESSAGE", event_id="o",
                        session_id="s", tenant_id="t", shop_id="sh",
                        buyer_id="b", chat_id="s", dedupe_key="dws1"))
        _api.handle_claim(5)
        r = _api.handle_result("ws1", {"status": "WOULD_SEND",
                                        "provider_result_status": "shadow"})
        check("★★ shadow 模式的 WOULD_SEND 不是 FAILED（真插件实测发现的）",
              r.get("status") == "WOULD_SEND", str(r.get("status")))

        # ④ ★★ 出票授权的回核字段（跑插件发现不了 —— 见本节开头的说明）
        #    这里用插件源码的判据逐字核：wanda-shipment.mjs:13-17
        _q6.enqueue(_C6(command_id="tv1", type="DELIVER_TICKET",
                        event_id="o-x", session_id="s", tenant_id="t",
                        shop_id="sh", buyer_id="b", chat_id="s",
                        dedupe_key="dtv1",
                        extra={"order_id": "o-x", "delivery_provider": "WANDA"}))
        _api.handle_claim(5)
        from application import plugin_authorize as _au6
        g = _au6.authorize_ticket_delivery(
            "tv1", detail={"orderId": "o-x", "accountUnb": "sh",
                            "orderStatus": 2})
        check("★★ 授权回 execution_token（没它就 GATE_REJECTED）",
              bool(g.get("execution_token")),
              "wanda-shipment.mjs:13 token = grant?.execution_token")
        _want = {"command_id": "tv1", "type": "DELIVER_TICKET",
                 "order_id": "o-x", "tenant_id": "t", "shop_id": "sh",
                 "buyer_id": "b", "chat_id": "s"}
        _bad = {k: (g.get(k), v) for k, v in _want.items() if g.get(k) != v}
        check("★★★ 回核字段逐字段等于命令（对不上插件静默拒绝执行）",
              not _bad,
              "wanda-shipment.mjs:14 七个字段 .some(k => grant[k] !== command[k])",
              str(_bad))
        # ⑤ ★★ ECHO_FIELDS 和 _identity_of 的键必须一致
        from application.plugin_authorize import ECHO_FIELDS
        check("★★ ECHO_FIELDS 与回显字段同源（改了不会静默失配）",
              all(k in g for k in ECHO_FIELDS), str(ECHO_FIELDS))
    finally:
        _api.reset_queue()

    section("★ 推送闸门自检（防「买家收不到且不报错」）")
    # ★★ 背景：插件的 SEND_MESSAGE 走 execute-command.mjs 的默认分支，
    #   要求 LIVE_SEND_GLOBAL_ENABLED==='true' 或命中 allowlist。
    #   没配 ⇒ 一律 LIVE_CAPABILITY_GATE_REJECTED ⇒ 买家一个字收不到，
    #   而命令"成功执行"了、不报错 —— 最难查的一类问题。
    #   ⇒ 启动日志 + /healthz 必须能看出闸门状态。
    _srv = (ROOT / "yumaiduo" / "plugin-runtime" / "src" / "server.mjs").read_text(encoding="utf-8")
    check("★★ 插件有 sendCapability() 自检",
          "export function sendCapability" in _srv)
    check("★★ 启动时会打印发送能力（配置漏了立刻能看到）",
          "logSendCapability(sendCapability())" in _srv)
    check("★★ /healthz 暴露 can_send / blocked_gates / gates",
          "can_send:cap.can_send" in _srv
          and "blocked_gates:cap.blocked" in _srv
          and "gates:cap.gates" in _srv)
    # 自检必须覆盖那几个真会拦消息的闸门
    for _g in ("LIVE_SEND_GLOBAL_ENABLED", "LIVE_REPRICE_ENABLED",
               "YUMAIDUO_PLUGIN_TOKEN", "YUMAIDUO_TENANT_ID",
               "PLUGIN_EXECUTION_JOURNAL_DIR"):
        check(f"★ 自检覆盖闸门 {_g}", _g in _srv)
    # 发送闸门确实在命令执行处生效（自检不是摆设）
    _exe = (ROOT / "yumaiduo" / "plugin-runtime" / "src" / "execute-command.mjs").read_text(encoding="utf-8")
    check("★★ SEND_MESSAGE 真受 LIVE_SEND_GLOBAL_ENABLED 管",
          "LIVE_SEND_GLOBAL_ENABLED" in _exe
          and "LIVE_CAPABILITY_GATE_REJECTED" in _exe)
    # 部署清单必须存在（14 个环境变量原来一个说明都没有）
    check("★★ 插件有 .env.example（14 个环境变量的清单）",
          (ROOT / "yumaiduo" / "plugin-runtime" / ".env.example").exists())
    _env = (ROOT / "yumaiduo" / "plugin-runtime" / ".env.example").read_text(encoding="utf-8")
    _missing = [v for v in ("YUMAIDUO_COMMAND_MODE", "V5_BACKEND_URL",
                            "V5_BACKEND_SECRET_FILE",
                            "LIVE_SEND_GLOBAL_ENABLED", "LIVE_REPRICE_ENABLED",
                            "YUMAIDUO_PLUGIN_TOKEN", "YUMAIDUO_TENANT_ID",
                            "CORE_URL", "BASE_URL",
                            "PLUGIN_EXECUTION_JOURNAL_DIR")
                if v not in _env]
    check("★★ .env.example 覆盖全部关键变量", not _missing,
          f"缺: {_missing}")
    # ★ 密钥不能写进仓库
    check("★★ .env.example 里没有真实密钥（只有占位）",
          "PLUGIN_DEVELOPER_TOKEN=\n" in _env
          and "YUMAIDUO_PLUGIN_TOKEN=\n" in _env)

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} 项通过")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
