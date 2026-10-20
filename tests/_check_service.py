# 良票链路夹具使用非万达影院；万达缺桥必须阻断，见 test_city_code_cache.py。
"""
_check_service.py —— 接线层自检（假 transport，不需要 AppKey）

════════════════════════════════════════════════════════════════
★ 这个测试补的是审计发现的缺口

  审计发现 application/ticket_flow.py 是【孤儿】——
  做完了、测穿了，但没有任何生产代码引用它。
  "能跑"和"能上线"之间的差别就在这里。

  这个文件验证的是【接线】：
    · 一条买家消息能不能顺着 service → flow → 假良票 走完
    · 会话能不能跨轮次记住上下文（买家回"场次1"要靠它）
    · 关键词旁路会不会污染流程状态
    · 回调验签失败会不会拒绝
    · 会话持久化会不会把状态存回来
════════════════════════════════════════════════════════════════
"""

import json
import sys
from pathlib import Path

# ★ Windows 上 stdout 默认 GBK，打不出 ↔ ★ ✓ − 这类字符会抛
#   UnicodeEncodeError。★ 必须放在【任何 print 之前】——
#   这些测试是模块级脚本，从第一行就开始跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


RESULTS = []

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from application.ticket_flow import FlowResult  # noqa: E402
from application.service import TicketService
from domain.conversation import ConvState
from domain.quote_rules import QuoteRuleEngine, tianjin_default_rules
from integrations.liangpiao.api import LiangPiaoAPI
from integrations.liangpiao.client import LiangPiaoClient
from integrations.liangpiao.signature import sign_body


KEY, SECRET, TS = "K", "S", "1700000000"


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def section(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


class RoutingTransport:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def send(self, url, body, headers, timeout):
        path = url.split("/api/v1", 1)[-1]
        self.calls.append({"path": path, "body": json.loads(body or "{}")})
        handler = self.routes.get(path)
        if handler is None:
            raise AssertionError(f"★ 调了未登记的接口：{path}")
        return handler(self.calls[-1]["body"]) if callable(handler) else handler


def envelope(code=0, message="success", data=None):
    out = {"code": code, "message": message, "requestId": "RID"}
    if data is not None:
        out["data"] = data
    return json.dumps(out, ensure_ascii=False)


EXACT = {
    "recognizeId": "8642", "cached": False, "rawResults": {},
    "finalResults": {
        "matchLevel": "EXACT",
        "cinemaId": 1486, "cinemaName": "CGV影城", "cityName": "天津",
        "brandName": "CGV影城", "movieId": 2, "movieName": "八仙!",
        "hallName": "6号厅", "startTime": "2099-07-28T19:30:00+08:00",
        "showId": "889900", "cinemaHitNums": 1,
        "seat": [{"rowNo": 4, "colNo": 1, "seatNo": "4排1座",
                  "originalPriceFen": "3890", "areaId": "32"}],
        "seatMatched": True, "prices": [],
        "candidates": {"cinemas": [], "movies": [], "shows": []},
    },
}

CANDIDATE = {
    "recognizeId": "8669", "cached": False, "rawResults": {},
    "finalResults": {
        "matchLevel": "CANDIDATE", "cinemaId": 2035, "cinemaName": "某某影城",
        "cityName": "天津", "brandName": "金逸电影城", "movieId": 57,
        "movieName": "某某片", "hallName": None, "startTime": None, "showId": None,
        "seatMatched": False,
        "seat": [{"rowNo": 5, "colNo": 6, "seatNo": "5排6座", "areaId": None}],
        "prices": [],
        "candidates": {"cinemas": [], "movies": [], "shows": [
            {"showId": "17208231", "hall": "4号激光厅",
             "startTime": "2099-09-05T18:15:00+08:00", "dimension": "2D",
             "language": "国语", "prices": []}]},
    },
}


def confirmed():
    d = json.loads(json.dumps(EXACT))
    f = d["finalResults"]
    f.update({"showId": "17208231", "hallName": "4号激光厅",
              "startTime": "2099-09-05T18:15:00+08:00"})
    d["recognizeId"] = "8669"
    return d


DEFAULT_ROUTES = {
    "/recognize/seat-shot": (200, envelope(data=EXACT)),
    "/order/preflight": (200, envelope(data={
        "available": True, "estimateAmount": "3310", "totalAmount": "3510"})),
    "/order/create": (200, envelope(data={
        "orderNo": "LP2026TEST", "status": "SUBMITTING",
        "freezeAmount": "3510", "seatCount": 1, "duplicated": False})),
    # ★ 退票前会先查订单详情的 refundable（后加的），必须登记这条路由
    "/order/detail": (200, envelope(data={
        "orderNo": "LP2026TEST", "status": "TICKETED", "refundable": True})),
}


def make_service(routes=None, persist=False, tel="13800000000", ai=None, **kw):
    """装配一个用假 transport 的服务。

    ★ persist / tel / ai 都显式提出来而不是靠 **kw ——
      早先写死 persist=False 又允许 **kw 传同名字段，
      结果 TypeError: got multiple values。参数要么显式，要么只走一个通道。
    """
    tr = RoutingTransport(routes or DEFAULT_ROUTES)
    client = LiangPiaoClient(KEY, SECRET, base_url="https://example.test",
                             transport=tr, sleep=lambda _: None)
    svc = TicketService(LiangPiaoAPI(client),
                        QuoteRuleEngine(tianjin_default_rules()),
                        tel=tel, app_key=KEY, app_secret=SECRET,
                        ai=ai, persist=persist, **kw)
    return svc, tr


def hdrs(body, nonce):
    return {"X-App-Key": KEY, "X-Timestamp": TS, "X-Nonce": nonce,
            "X-Sign": sign_body(KEY, SECRET, TS, nonce, body)}


def main() -> int:                                    # noqa: C901
    # ------------------------------------------------------------------ 一
    section("一、★ 装配：服务能起来，依赖注入正确")
    svc, tr = make_service()
    check("TicketService 装配成功", svc is not None)
    check("tel 传进去了", svc.flow.tel == "13800000000")
    check("规则引擎接上了", len(svc.flow.rules.rules) == 6)
    check("凭证接上了", svc.app_key == KEY and svc.app_secret == SECRET)
    check("transport 是注入的（不是真的网络）", svc.flow.api.client.transport is tr)

    section("二、★ 一条买家消息走完整条链路")
    svc, tr = make_service()
    r = svc.on_message("chat-1", image_url="https://x/a.jpg", city_name="天津")
    check("发图 → 成功", r.ok, r.error)
    keys = r.template_keys
    check("★ 先发识别提示再发报价卡",
          keys[0] == "RECOGNIZING_TIP" and "QUOTE_CARD" in keys, str(keys))
    check("状态到已报价", r.state is ConvState.QUOTED, r.state.value)
    # ★ 注意这里必须包 bool()：
    #   早先写成 `r.quote is not None and r.quote.rule_name`，
    #   and 会把【字符串】原样返回而不是布尔值，最后 sum(RESULTS) 就炸了
    #   （int + str）。check() 的第一个参数是布尔断言，不是取值。
    check("★ 价格走的是规则引擎",
          bool(r.quote) and bool(r.quote.rule_name),
          r.quote.rule_name if r.quote else "-")
    check("★ 命中规则不是「无命中」降级",
          bool(r.quote) and "无命中" not in r.quote.rule_name,
          f"命中={r.quote.rule_name if r.quote else '-'}  "
          f"brand={r.quote and '见上'}")
    check("★ priceMode 由规则决定", r.quote.price_mode == "LIMIT",
          r.quote.price_mode if r.quote else "-")
    check("★ 走的是真实良票接口（假 transport 收到了）",
          [c["path"] for c in tr.calls] == ["/recognize/seat-shot", "/order/preflight"],
          str([c["path"] for c in tr.calls]))

    # 插件事件的身份必须进入报价记录，并通过订单接口返回给报价记录页。
    from domain.quote import QuoteStore
    from application.ledger import _quote_row
    quote_store = QuoteStore(":memory:")
    identity_svc, _ = make_service(quote_store=quote_store)
    identity_svc._blacklist_gate = lambda *args: None
    try:
        identity_svc.on_message(
            "chat-identity", image_url="https://x/a.jpg", city_name="天津",
            tenant_id="107", shop_id="2313315754", peer_unb="2464035965")
    except TypeError as exc:
        check("★★★ 插件身份可传入消息入口", False, str(exc))
    else:
        rec_id = identity_svc.session("chat-identity").conversation.quote_id
        rec = quote_store.get(rec_id) if rec_id else None
        check("★★★ 报价记录保存真实买家、店铺和租户",
              rec is not None and
              (rec.tenant_id, rec.shop_id, rec.buyer_id, rec.chat_id) ==
              ("107", "2313315754", "2464035965", "chat-identity"))
        check("★★★ 报价记录保存实际供货路线",
              rec is not None and rec.provider == "LIANGPIAO")
        row = _quote_row(rec) if rec is not None else {}
        check("★★★ 报价页接口返回买家、店铺和路线",
              (row.get("buyer_id"), row.get("shop_id"), row.get("provider")) ==
              ("2464035965", "2313315754", "LIANGPIAO"))
        from application import store as app_store
        saved_sessions = {}
        original_save, original_load = app_store.save_sessions, app_store.load_sessions
        try:
            app_store.save_sessions = lambda data: saved_sessions.update(data)
            app_store.load_sessions = lambda: saved_sessions
            identity_svc._save()
            identity_svc.sessions.clear()
            identity_svc._load()
        finally:
            app_store.save_sessions, app_store.load_sessions = original_save, original_load
        restored = identity_svc.session("chat-identity").conversation
        check("★★ 会话重启后保留买家和店铺身份",
              (restored.tenant_id, restored.shop_id, restored.buyer_id) ==
              ("107", "2313315754", "2464035965"))

    # 买家确认
    # ★★★ 2026-10-14 店主定的时序：
    #   「必须金额校验正确，且已付款，才可以良票建单」
    #   ⇒ 回「正确」只落本地待付款单 + 发 bubble 17，【不调 order/create】。
    #     建单在付款确认之后（flow.on_payment_confirmed）。
    r2 = svc.on_message("chat-1", "正确")
    check("★ 回「正确」→ 落本地待付款单（不建上游单）", r2.ok, r2.error)
    check("★★ 确认时【没有】调 order/create",
          "/order/create" not in [c["path"] for c in tr.calls],
          str([c["path"] for c in tr.calls]))
    check("★ 发出「拍下别付款」(bubble 17)",
          "ORDERED_NO_PAY" in r2.template_keys, str(r2.template_keys))
    check("会话状态到已拍下待改价", r2.state is ConvState.AWAIT_REPRICE,
          r2.state.value)

    # ---- 付款确认之后才建单 ----
    _conv1 = svc.session("chat-1").conversation
    r2b = svc.flow.on_payment_confirmed(_conv1, FlowResult())
    check("★★ 付款确认后 → 调了 order/create",
          "/order/create" in [c["path"] for c in tr.calls],
          str([c["path"] for c in tr.calls]))
    check("★★ 付款后拿到平台订单号", r2b.order_no == "LP2026TEST", r2b.order_no)
    check("★★ 付款后发出已下单等待", "TICKETING" in r2b.template_keys,
          str(r2b.template_keys))
    check("★★ 付款后会话状态到待出票", r2b.state is ConvState.AWAIT_TICKET,
          r2b.state.value)

    # ------------------------------------------------------------------ 二
    section("二之二、★ 出票回调 → 发取票码（走 service 而不是直接调 flow）")
    body = json.dumps({"event": "order.ticketed", "data": {
        "orderNo": "LP2026TEST", "version": 1, "seatChanged": False,
        "actualSeats": ["4排1座"],
        "tickets": [{"seatNo": "4排1座", "ticketCode": "A1B2C3"}]}},
        ensure_ascii=False)
    ack, r3 = svc.on_callback(body, hdrs(body, "n1"))
    check("回调应答是 code:0", ack == '{"code":0}', ack)
    check("发出取票码", r3 is not None and "TICKET_CODE" in r3.template_keys,
          str(r3.template_keys if r3 else None))
    check("★ 取票码进了消息",
          any("A1B2C3" in m for m in (r3.messages if r3 else [])),
          str([m[:40] for m in (r3.messages if r3 else [])]))

    section("二之三、★ 回调幂等：同一事件重推不再处理")
    ack2, r4 = svc.on_callback(body, hdrs(body, "n2"))
    check("★ 重推 → 不再处理（事件为 None）", r4 is None)
    check("★ 但应答仍是 code:0（让平台别再推）", ack2 == '{"code":0}', ack2)

    section("二之四、★ 伪造回调必须被拒")
    bad = hdrs(body, "n3")
    bad["X-Sign"] = "deadbeef"
    ack3, r5 = svc.on_callback(body, bad)
    check("★ 签名错 → 不返回事件", r5 is None)
    check("★ 签名错 → 应答不是 code:0", ack3 != '{"code":0}', ack3)

    # ------------------------------------------------------------------ 三
    section("三、★ 会话上下文：买家回「场次1」要靠它")
    routes = dict(DEFAULT_ROUTES)
    routes["/recognize/seat-shot"] = (200, envelope(data=CANDIDATE))
    routes["/recognize/confirm"] = (200, envelope(data=confirmed()))
    svc, tr = make_service(routes)

    r1 = svc.on_message("chat-c", image_url="https://x/a.jpg", city_name="天津")
    check("多结果 → 发候选", "DISAMBIG" in r1.template_keys, str(r1.template_keys))
    check("状态进待确认场次", r1.state is ConvState.PICK_SHOW, r1.state.value)
    s = svc.session("chat-c")
    check("★ 会话记住了上一轮在问候选", s.last_question == "pick", s.last_question)
    check("★ 记住了候选内容", bool(s.last_options.get("shows")),
          str(list(s.last_options.keys())))

    r2 = svc.on_message("chat-c", "场次1")
    check("★ 回「场次1」→ 走二次确认",
          "/recognize/confirm" in [c["path"] for c in tr.calls],
          str([c["path"] for c in tr.calls]))
    check("二次确认后出报价卡",
          any(k.startswith("QUOTE_CARD") for k in r2.template_keys),
          str(r2.template_keys))

    # ------------------------------------------------------------------ 四
    section("四、★ 关键词是旁路，绝不能污染流程状态")
    svc, tr = make_service()
    svc.on_message("chat-k", image_url="https://x/a.jpg", city_name="天津")
    before = svc.session("chat-k").conversation.state
    check("报价后是已报价", before is ConvState.QUOTED, before.value)

    r = svc.on_message("chat-k", "999")
    check("★ 关键词有回复", bool(r.ok and r.messages), str(r.template_keys))
    check("★ 回复的是 FAQ 内容", "常见问题" in "".join(r.messages),
          str(r.messages[0][:30] if r.messages else ""))
    after = svc.session("chat-k").conversation.state
    check("★ 关键词回复【不改变】状态", after is before, f"{before.value} → {after.value}")

    r2 = svc.on_message("chat-k", "取票")
    check("「取票」也有回复", bool(r2.ok and r2.messages), str(r2.template_keys))
    check("状态仍是已报价", svc.session("chat-k").conversation.state is before)

    section("四之二、★ 确认指令优先于关键词")
    # 即使有人把"正确"也配成关键词，也必须仍然是确认下单优先
    import qa as qa_module
    original = list(qa_module.QA_RULES)
    try:
        qa_module.QA_RULES.append(qa_module.QARule(
            qa_id="zzzzzzzzzz", keyword="正确", content="这句不该被发出去"))
        qa_module.BY_KEYWORD["正确"] = qa_module.QA_RULES[-1]
        svc, tr = make_service()
        svc.on_message("chat-p", image_url="https://x/a.jpg", city_name="天津")
        r = svc.on_message("chat-p", "正确")
        # ★ 时序改了：确认只落本地待付款单（order_no 是本地单号），
        #   上游单号要等付款后才有。这里验的是没被关键词抢走。
        check("★ 仍然是确认下单，不是发关键词",
              r.state is ConvState.AWAIT_REPRICE
              and "ORDERED_NO_PAY" in (r.template_keys or []),
              f"state={r.state.value} keys={r.template_keys}")
        check("★ 没有发出那句关键词",
              not any("不该被发出去" in m for m in r.messages),
              str([m[:30] for m in r.messages]))
    finally:
        qa_module.QA_RULES[:] = original
        qa_module.BY_KEYWORD.pop("正确", None)

    section("四之三、★ 只在已报价状态才认确认指令")
    svc, tr = make_service()
    # 空闲状态下说"正确"不该下单
    r = svc.on_message("chat-x", "正确")
    check("★ 空闲时说「正确」不下单",
          "/order/create" not in [c["path"] for c in tr.calls],
          str([c["path"] for c in tr.calls]))
    check("给了核对提示", r.ok, r.error)

    # ------------------------------------------------------------------ 五
    section("四之二、★★ 纯数字候选路由【不能吃掉】已登记关键词")
    # ★★ 这是改「候选只回数字」时踩出来的真回归：
    #   qa 表里的 999/001/002/003/004/005/111 都是【纯数字】，
    #   而我们的文案对买家承诺了「常见问题回复999」。
    #   如果候选路由先吃掉它们，买家按文案发 999 只会收到
    #   「上次识别已过期」/「重发候选」，拿不到常见问题。
    #   ⇒ 兑现给买家的指令，优先级必须高于内部流程判断。
    svc2, tr2 = make_service()
    svc2.on_message("chat-num", image_url="https://x/a.jpg", city_name="天津")
    st_before = svc2.session("chat-num").conversation.state
    check("报价后是已报价", st_before is ConvState.QUOTED, st_before.value)
    # 故意让它处于「刚问过候选」的上下文 —— 这是原来会走候选路由的条件
    svc2.session("chat-num").last_question = "pick"

    for kw, must in (("999", "常见问题"), ("001", None), ("005", None)):
        rk = svc2.on_message("chat-num", kw)
        txt = "".join(rk.messages or [])
        check(f"★★ 「{kw}」走关键词回复，不被当候选序号",
              bool(rk.ok and rk.messages) and "过期" not in txt
              and "匹配结果" not in txt,
              txt[:40])
        if must:
            check(f"★ 「{kw}」回复的是 FAQ 内容", must in txt, txt[:40])
    st_after = svc2.session("chat-num").conversation.state
    check("★★ 关键词回复不改变状态（旁路不许污染流程）",
          st_after is st_before, f"{st_before.value} → {st_after.value}")

    section("★ 但纯数字【不会被关键词吞掉】—— 它不是 qa 词")
    # ★ 这条只验「1~4 不在关键词表里」，所以候选路由能安全地认它们。
    #   （真正的候选选择行为在 _check_ticket_flow 的多轮消歧里测。）
    import qa as qa_mod
    for d in ("1", "2", "3", "4"):
        check(f"★ 「{d}」不是已登记关键词（才敢当序号用）",
              qa_mod.reply(d) is None, str(qa_mod.reply(d))[:30])
    check("★★ 而 999/001 这类【是】关键词（所以候选路由必须让位）",
          qa_mod.reply("999") is not None and qa_mod.reply("001") is not None)

    section("五、★ 会话持久化：状态能存回来")
    import tempfile
    from pathlib import Path as _P
    from application import store as store_mod

    # ★ 打桩的是 store.SESSIONS_FILE，不是 service.SESSIONS_FILE ——
    #   会话落盘已经搬进 store 层了（service 只管接线）。
    tmp = _P(tempfile.gettempdir()) / "dsh-session-test.json"
    old_file = store_mod.SESSIONS_FILE
    store_mod.SESSIONS_FILE = tmp
    try:
        if tmp.exists():
            tmp.unlink()
        svc, tr = make_service(persist=True)
        svc.sessions.clear()
        svc.on_message("chat-save", image_url="https://x/a.jpg", city_name="天津")
        check("文件已写出", tmp.exists())
        saved = json.loads(tmp.read_text(encoding="utf-8"))
        check("★ 存了状态", saved.get("chat-save", {}).get("state") == ConvState.QUOTED.value,
              str(saved.get("chat-save")))

        # 新建一个 service，看能不能读回来
        svc2, _ = make_service(persist=True)
        s2 = svc2.sessions.get("chat-save")
        check("★ 重启后状态读回来了",
              s2 is not None and s2.conversation.state is ConvState.QUOTED,
              s2.conversation.state.value if s2 else "（没读到）")
        check("★ 没把报价对象也存下来（报价会过期，带着反而危险）",
              s2 is not None and s2.conversation.quote is None,
              "quote=None")
    finally:
        store_mod.SESSIONS_FILE = old_file
        if tmp.exists():
            tmp.unlink()

    # ------------------------------------------------------------------ 六
    section("六、★ 退票也走 service")
    routes = dict(DEFAULT_ROUTES)
    routes["/order/refund"] = (200, envelope(data={"refundNo": "RF001"}))
    svc, tr = make_service(routes)
    svc.on_message("chat-r", image_url="https://x/a.jpg", city_name="天津")
    svc.on_message("chat-r", "正确")
    # ★ 时序改了：付款确认后才建上游单，建了单才谈得上退票
    svc.flow.on_payment_confirmed(svc.session("chat-r").conversation,
                                  FlowResult())
    # 先让它出票
    body = json.dumps({"event": "order.ticketed", "data": {
        "orderNo": "LP2026TEST", "version": 1, "actualSeats": ["4排1座"],
        "tickets": [{"seatNo": "4排1座", "ticketCode": "A1"}]}}, ensure_ascii=False)
    svc.on_callback(body, hdrs(body, "nr1"))

    r = svc.on_refund("chat-r")
    check("退票申请提交成功", r.ok, r.error)
    check("状态进退票处理中", r.state is ConvState.REFUNDING, r.state.value)
    check("调了 order/refund", "/order/refund" in [c["path"] for c in tr.calls])
    check("★ 文案没说「已退款」（提交≠成功）",
          not any("退款成功" in m or "退票成功" in m for m in r.messages),
          str([m[:40] for m in r.messages]))

    section("七、★ 没配 tel 时的行为（门槛放在哪一步）")
    # ★ 这条断言改过。原来要求"没 tel 就不能装配"，
    #   但那把门槛放错了地方：识别、询价、算价、答话都不需要 tel，
    #   只有【下单】需要（良票文档：tel 是下单必填）。
    #   在装配时 raise 的后果是：没配 tel 连"在吗"都回不了。
    svc_no_tel, _ = make_service(tel="")
    check("★ 没 tel 也能装配（答话不需要 tel）", svc_no_tel is not None)
    check("没 tel 时 tel 是空串，不是 None",
          svc_no_tel.flow.tel == "", repr(svc_no_tel.flow.tel))

    # ★ 真正该拦的地方：建上游单那一刻。
    #   ★★ 时序改了（店主 2026-10-14）：「必须金额校验正确且已付款才建单」
    #   ⇒ 确认那一步只落【本地待付款单】—— 本地单不需要 tel。
    #     拦下发生在 on_payment_confirmed（那时才真的要调 ch5_create）。
    r_notel = svc_no_tel.on_message("no-tel", image_url="https://x/a.jpg",
                                    city_name="天津")
    check("没 tel 时【识别和报价照常】", r_notel.ok or bool(r_notel.messages),
          str(r_notel.template_keys))
    r_confirm = svc_no_tel.on_message("no-tel", "正确")
    check("★ 没 tel 时【确认仍可落本地单】（本地单不需要 tel）",
          r_confirm.ok, r_confirm.error)
    r_build = svc_no_tel.flow.on_payment_confirmed(
        svc_no_tel.session("no-tel").conversation, FlowResult())
    check("★★ 没 tel 时【建上游单被拦下】", not r_build.ok, r_build.error)
    check("★ 说明了缺什么（运营看得懂）",
          any("tel" in n or "手机号" in n for n in r_build.notes),
          str(r_build.notes)[:80])
    check("★ 拦下时标了要人工", r_build.need_human)

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    for i, ok in enumerate(RESULTS):
        if not ok:
            print(f"  ✗ 第 {i + 1} 项失败")
    print(f"\n{passed}/{total} 项通过")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
