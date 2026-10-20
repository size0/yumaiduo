# 良票链路夹具使用非万达影院；万达缺桥必须阻断，见 test_city_code_cache.py。
"""
_check_refund.py —— 退票链路自检（假 transport）

════════════════════════════════════════════════════════════════
★ 为什么单独一个文件

  退票是【本次新补的流程】。原先状态机里连落点都没有：
  良票有 order/refund + order.refunded + order.refund_rejected
  三个接口/事件，我们一个都没接。

★ 退票最容易做错的一件事

  文档：「收到 order.refund_rejected 后订单回到 TICKETED，
        **凭证依然有效**，冻结资金等散场实扣。」

  ⇒ 退票没成必须明确告诉买家【票还能用】。
    否则买家以为退了就不去看了 —— 那是实打实的损失，而且是我们的话造成的。
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

from application.ticket_flow import (FlowResult,
                                   TicketFlow)
from domain.conversation import Conversation, ConvState, Event
from domain.quote_rules import QuoteRuleEngine, tianjin_default_rules
from integrations.liangpiao.api import LiangPiaoAPI
from integrations.liangpiao.client import LiangPiaoClient
from integrations.liangpiao.signature import sign_body




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


EXACT_RESULT = {
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


def make_flow(extra_routes=None):
    routes = {
        "/recognize/seat-shot": (200, envelope(data=EXACT_RESULT)),
        "/order/preflight": (200, envelope(data={
            "available": True, "estimateAmount": "3310", "totalAmount": "3510"})),
        "/order/create": (200, envelope(data={
            "orderNo": "LP2026TEST", "status": "SUBMITTING",
            "freezeAmount": "3510", "seatCount": 1, "duplicated": False})),
        # ★ 退票前会先查订单详情的 refundable —— 必须登记这条路由，
        #   否则 RoutingTransport 会抛"调了未登记的接口"。
        #   默认给 refundable=true（可退），个别用例再覆盖成 false。
        "/order/detail": (200, envelope(data={
            "orderNo": "LP2026TEST", "status": "TICKETED", "refundable": True})),
    }
    routes.update(extra_routes or {})
    tr = RoutingTransport(routes)
    client = LiangPiaoClient("K", "S", base_url="https://example.test",
                             transport=tr, sleep=lambda _: None)
    flow = TicketFlow(LiangPiaoAPI(client), QuoteRuleEngine(tianjin_default_rules()),
                      tel="13800000000")
    return flow, tr


def to_delivered(flow):
    """把会话推进到「已交付」（已出票）。"""
    conv = Conversation(chat_id="refund-chat")
    flow.on_screenshot(conv, "https://x/a.jpg", city_name="天津")
    flow.on_buyer_confirm(conv)
    # ★★ 2026-10-14 店主定的时序：确认时不建上游单，
    #    「必须金额校验正确且已付款，才可以良票建单」。
    #    ⇒ 测试要显式走一次"付款确认"，才会有 LP2026TEST 这个上游单可退。
    flow.on_payment_confirmed(conv, FlowResult())
    body = json.dumps({"event": "order.ticketed", "data": {
        "orderNo": "LP2026TEST", "version": 1, "seatChanged": False,
        "actualSeats": ["4排1座"],
        "tickets": [{"seatNo": "4排1座", "ticketCode": "A1B2"}]}}, ensure_ascii=False)
    ts, nonce = "1700000000", "n1"
    headers = {"X-App-Key": "K", "X-Timestamp": ts, "X-Nonce": nonce,
               "X-Sign": sign_body("K", "S", ts, nonce, body)}
    flow.on_callback(body, headers, app_key="K", app_secret="S", now=1700000000)
    return conv


def main() -> int:                                      # noqa: C901
    KEY, SECRET, TS = "K", "S", "1700000000"

    def hdrs(body, nonce):
        return {"X-App-Key": KEY, "X-Timestamp": TS, "X-Nonce": nonce,
                "X-Sign": sign_body(KEY, SECRET, TS, nonce, body)}

    # ------------------------------------------------------------------ 一
    section("一、★ 退票流程在状态机里有落点（原先完全没有）")
    conv = Conversation(chat_id="c1")
    check("初始是空闲", conv.state is ConvState.IDLE)
    conv.state = ConvState.DELIVERED
    d = conv.apply(Event.REFUND_APPLIED)
    check("已交付 --申请退票--> 退票处理中", d.state is ConvState.REFUNDING, d.state.value)
    d2 = conv.apply(Event.REFUND_SUCCEEDED)
    check("退票处理中 --退票完成--> 已退票", d2.state is ConvState.REFUNDED, d2.state.value)
    check("已退票是终态", conv.is_terminal)

    conv2 = Conversation(chat_id="c2")
    conv2.state = ConvState.DELIVERED
    conv2.apply(Event.REFUND_APPLIED)
    d3 = conv2.apply(Event.REFUND_REJECTED)
    check("★ 退票未成 → 回到【已交付】（不是待出票）",
          d3.state is ConvState.DELIVERED, d3.state.value)
    # ★ 「已交付」本身就是自然收尾状态（票已经交到买家手里，流程走完了）。
    #   退票未成回到这里是对的 —— 原断言写成"不是终态"是自相矛盾的。
    check("★ 回到的是自然收尾状态（票已交付，流程闭环）",
          conv2.is_terminal, conv2.state.value)
    tmp = Conversation(chat_id="c2b")
    tmp.state = ConvState.REFUNDING
    check("★ 退票处理中不是终态", not tmp.is_terminal, tmp.state.value)
    check("★ 已退票是终态",
          Conversation(chat_id="c2c", state=ConvState.REFUNDED).is_terminal)

    check("有三个退票事件", all(hasattr(Event, n) for n in
          ("REFUND_APPLIED", "REFUND_SUCCEEDED", "REFUND_REJECTED")))

    # ------------------------------------------------------------------ 二
    section("二、★ 提交退票：三条失败码分开处置")
    for code, keyword in ((500001, "不支持退票"), (500002, "过了"), (500003, "提交过")):
        flow, tr = make_flow({"/order/refund": (200, envelope(code=code, message="拒绝"))})
        conv = to_delivered(flow)
        r = flow.on_refund_request(conv)
        check(f"{code} → 提示含「{keyword}」",
              (not r.ok) and any(keyword in m for m in r.messages),
              str([m[:40] for m in r.messages]))
    check("失败码不重试（只调一次）",
          len([c for c in tr.calls if c["path"] == "/order/refund"]) == 1)

    # ------------------------------------------------------------------ 三
    section("三、提交成功 ≠ 退票成功")
    flow, tr = make_flow({"/order/refund": (200, envelope(data={"refundNo": "RF001"}))})
    conv = to_delivered(flow)
    r = flow.on_refund_request(conv)
    check("提交成功", r.ok)
    check("状态进退票处理中", r.state is ConvState.REFUNDING, r.state.value)
    check("★ 文案没让买家以为已经退了",
          any("申请" in m and "结果" in m for m in r.messages),
          str([m[:50] for m in r.messages]))
    check("退票单号记进 notes", any("RF001" in n for n in r.notes), str(r.notes))
    check("调的是 order/refund",
          [c["path"] for c in tr.calls].count("/order/refund") == 1)
    refund_body = [c["body"] for c in tr.calls if c["path"] == "/order/refund"][0]
    check("传了平台订单号", refund_body.get("orderNo") == "LP2026TEST", str(refund_body))

    # ------------------------------------------------------------------ 四
    section("四、★ 退票完成：金额如实说")
    flow, tr = make_flow({"/order/refund": (200, envelope(data={"refundNo": "RF001"}))})
    conv = to_delivered(flow)
    flow.on_refund_request(conv)
    body = json.dumps({"event": "order.refunded", "data": {
        "orderNo": "LP2026TEST", "outOrderNo": "X",
        "feeAmount": "0", "refundAmount": "7020"}}, ensure_ascii=False)
    _, r = flow.on_callback(body, hdrs(body, "n2"), app_key=KEY, app_secret=SECRET,
                            now=int(TS))
    check("发出退票完成文案", "REFUND_DONE" in r.template_keys, str(r.template_keys))
    check("★ 退还金额如实（70.20 元）",
          any("70.20" in m for m in r.messages), str([m[:60] for m in r.messages]))
    check("状态进已退票", r.event is Event.REFUND_SUCCEEDED)

    # 有手续费
    flow, tr = make_flow({"/order/refund": (200, envelope(data={"refundNo": "RF001"}))})
    conv = to_delivered(flow)
    flow.on_refund_request(conv)
    body = json.dumps({"event": "order.refunded", "data": {
        "orderNo": "LP2026TEST", "feeAmount": "500", "refundAmount": "6520"}},
        ensure_ascii=False)
    _, r = flow.on_callback(body, hdrs(body, "n3"), app_key=KEY, app_secret=SECRET,
                            now=int(TS))
    check("★ 有手续费时不隐瞒（5.00 元）",
          any("5.00" in m for m in r.messages), str([m[:80] for m in r.messages]))

    # ------------------------------------------------------------------ 五
    section("五、★★ 退票未成：必须明确告诉买家【票还能用】")
    flow, tr = make_flow({"/order/refund": (200, envelope(data={"refundNo": "RF001"}))})
    conv = to_delivered(flow)
    flow.on_refund_request(conv)
    body = json.dumps({"event": "order.refund_rejected", "data": {
        "orderNo": "LP2026TEST", "outOrderNo": "X", "refundNo": "RF001",
        "failReason": "影院不支持退票"}}, ensure_ascii=False)
    _, r = flow.on_callback(body, hdrs(body, "n4"), app_key=KEY, app_secret=SECRET,
                            now=int(TS))
    check("发出退票未成文案", "REFUND_REJECTED" in r.template_keys, str(r.template_keys))
    joined = "\n".join(r.messages)
    check("★ 文案明确说「票依然有效」", "依然有效" in joined, joined[:80])
    check("★ 文案提示取票码没变（买家不用重新要码）",
          "取票码" in joined, joined[:120])
    check("带上真实原因", "影院不支持退票" in joined, joined[:80])
    check("事件是 REFUND_REJECTED", r.event is Event.REFUND_REJECTED)
    check("★ 状态回到已交付（凭证仍有效）",
          r.state is ConvState.DELIVERED, r.state.value)

    # ------------------------------------------------------------------ 六
    section("六、没有在途订单时不能退票")
    flow, tr = make_flow()
    conv = Conversation(chat_id="c6")
    r = flow.on_refund_request(conv)
    check("★ 无订单时拒绝退票", not r.ok)
    check("★ 没有发出 order/refund 请求",
          "/order/refund" not in [c["path"] for c in tr.calls],
          str([c["path"] for c in tr.calls]))

    # ------------------------------------------------------------------ 七
    section("七、购票须知也走档位时效（不能绕开）")
    flow, tr = make_flow()
    conv = Conversation(chat_id="c7")
    r = flow.purchase_notice(conv)
    check("能发购票须知", "PURCHASE_NOTICE" in r.template_keys)
    # 手动切档位，看渲染是否跟着变
    from integrations.liangpiao.api import TICKET_FAST, TICKET_FLASH
    expected = {"STANDARD": "1-60分钟", TICKET_FAST: "15分钟内",
                TICKET_FLASH: "1-15分钟"}
    for mode, window in expected.items():
        conv.ticket_mode = mode
        got = flow.purchase_notice(conv).messages[0]
        check(f"档位 {mode} → 须知里是 {window}", window in got,
              [ln for ln in got.splitlines() if "自动出票" in ln][:1])
    # 下单后的 TICKETING 也要跟着
    for mode, window in expected.items():
        conv.ticket_mode = mode
        got = flow._window(conv)
        check(f"档位 {mode} → 取到 {window}", got == window, got)

    # ------------------------------------------------------------------ 八
    section("八、★★ refundable=false 时不提交必被拒的申请")
    flow, tr = make_flow({
        "/order/detail": (200, envelope(data={
            "orderNo": "LP2026TEST", "status": "TICKETED", "refundable": False})),
    })
    conv = to_delivered(flow)
    r = flow.on_refund_request(conv)
    check("★ 明确告知不可退（而不是提交后被拒）", not r.ok)
    check("★ 发出「当前不可退票」文案",
          "REFUND_NOT_ALLOWED" in r.template_keys, str(r.template_keys))
    check("★ 文案里说了票依然能用（不然买家以为白买了）",
          "有效" in "".join(r.messages), str(r.messages)[:120])
    check("★★ 没有调用 order/refund（省掉一次必被拒的请求）",
          "/order/refund" not in [c["path"] for c in tr.calls],
          str([c["path"] for c in tr.calls]))
    check("查了订单详情", "/order/detail" in [c["path"] for c in tr.calls])
    check("★ 依据是权威字段而不是错误码反推",
          any("refundable=false" in n for n in r.notes), str(r.notes))

    section("八之二、查详情失败不阻断退票（宁可试一次）")
    flow, tr = make_flow({
        "/order/detail": (200, envelope(code=100005, message="服务内部错误")),
        "/order/refund": (200, envelope(data={"refundNo": "RF002"})),
    })
    conv = to_delivered(flow)
    r = flow.on_refund_request(conv)
    check("★ 查详情失败仍然提交了退票", r.ok, r.error)
    check("备注里说明查资格失败",
          any("查退票资格失败" in n for n in r.notes), str(r.notes))

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
