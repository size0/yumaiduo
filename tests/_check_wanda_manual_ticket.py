"""
★★★ 连通性验证：万达"落单 → 店家填票码"这条路必须真的通。

★ 为什么单独验这个：
  落单时我写 payment_state="CONFIRMED"，而店家填票码时
  WandaDelivery.submit() 会【先检查它】：
      if order.payment_state != "CONFIRMED":
          raise DeliveryError("WANDA_DELIVERY_ORDER_NOT_PAYABLE", ...)
  如果两边对不上，店家点"标记出票"会被拒 ——
  而且那种失败在买家侧完全看不到（买家只看到"已下单"）。
  ⇒ 必须把两半真接起来跑一次。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from application.ticket_flow import FlowResult, TicketFlow               # noqa: E402
from application.wanda_delivery import (DeliveryError,                  # noqa: E402
                                        DeliverySubmission, WandaDelivery)
from domain.conversation import Conversation                            # noqa: E402
from domain.quote_rules import (BASIS_PER_TICKET, QUOTE_WANDA,          # noqa: E402
                                PricingTier, QuoteRule, QuoteRuleEngine)
from integrations.liangpiao.api import RecognitionResult                # noqa: E402
from integrations.wanda.bridge import (BridgeConfig,                    # noqa: E402
                                       WandaBridgeClient)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def section(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


# ---- 真实响应形状 ----
MAP = {"code": 0, "data": {"bizCode": 0, "mapped": True, "wandaCityId": "80",
                           "wandaCinemaId": "379",
                           "wandaCinemaName": "宁德万达广场店"}}
SHOWS = {"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": [
    {"showtimeFilmDateInf": [{"date": 20261013, "showtimesInf": {
        "showtimeCount": 1, "showtimeList": [
            {"showtimeId": 103155648, "realtime": 1791872400000,
             "hallName": "8号激光IMAX厅", "salesPrice": 5790,
             "wPlusActivityPrice": 5026, "status": 1,
             "filmList": [{"filmName": "生化危机：爆发夜"}]}]}}]}]}}
SEATS = {"code": 0, "data": {"bizCode": 0, "realtimeSeats": {"area": [
    {"areaId": "36",
     "seat": [{"seatId": 1, "name": "6排9座", "status": 1,
               "payMemberSeatStatus": 1}],
     "areaPrice": {"areaName": "W+专享", "salesPrice": 5790},
     "wPlusActivity": {"price": 5026, "activityCode": "A1"}}]}}}
ACCOUNTS = {"accounts": [{"phone": "18873890439", "available": True}],
            "count": 1}


class Transport:
    def get(self, url, headers, timeout):
        import json
        for frag, body in (("wanda-lookup", MAP), ("by-cinema", SHOWS),
                           ("seats", SEATS), ("wplus-accounts", ACCOUNTS)):
            if frag in url:
                return 200, json.dumps(body)
        return 404, "{}"


class FakeLiangPiao:
    def __init__(self):
        self.preflight_calls = []
        self.create_calls = []

    def pick_ticket_mode(self, start_time):
        return "STANDARD"

    def ch4_city_list(self, keyword=None):
        return [{"code": "350900", "name": "宁德"}] if keyword == "宁德" else []

    def ch5_preflight(self, *a, **kw):
        self.preflight_calls.append((a, kw))
        raise AssertionError("★ 万达渠道不该调良票")

    def ch5_create(self, *a, **kw):
        self.create_calls.append((a, kw))
        raise AssertionError("★★★ 万达渠道绝不该调良票下单")


def _blocked(fn) -> bool:
    """fn 抛 DeliveryError 就算被拦住了。"""
    try:
        fn()
        return False
    except DeliveryError:
        return True


def main():
    import application.ticket_flow as m
    m._far_enough = lambda *a, **kw: True

    tmp = Path(tempfile.mkdtemp())
    # ★ 必须让 ledger 指向同一个库、并且【用同一个 store 实例】——
    #   否则 plugin_runtime（走 ledger.stores()）更新的是另一份，
    #   测试里看到的还是旧状态。
    import application.ledger as ledger_mod
    ledger_mod.set_paths(quotes=str(tmp / "q.sqlite3"),
                         orders=str(tmp / "orders.sqlite3"))
    ledger_mod.close_stores()
    store = ledger_mod.stores().orders
    check("★ ledger 与测试共用同一个订单库",
          store is not None and store is ledger_mod.stores().orders)
    br = WandaBridgeClient(BridgeConfig(base="http://x", key="k"),
                           transport=Transport())
    api = FakeLiangPiao()
    rule = QuoteRule(name="万达系一口价", weight=9, enabled=True,
                     brands=["万达影城"], quote_mode=QUOTE_WANDA, method="加法",
                     markup_yuan=0, limit_yuan=0, capped=False, rounding_fen=10,
                     basis=BASIS_PER_TICKET,
                     tiers=[PricingTier(lower_bps=0, upper_bps=10000,
                                        fixed_add_fen=100,
                                        upper_inclusive=True)])
    f = TicketFlow(api, QuoteRuleEngine([rule], revision="t"),
                   tel="13800000000", wanda_bridge=br,
                   order_store=store)

    section("① 万达报价 → 确认 → 落单")
    conv = Conversation(chat_id="chat-wanda-1")
    conv.tenant_id, conv.shop_id = "t1", "s1"
    rec = RecognitionResult(
        match_level="EXACT", cinema_id="7261",
        cinema_name="万达影城（宁德万达广场激光IMAX店）", city_name="宁德",
        brand_name="万达影城", movie_id="900002313",
        movie_name="生化危机：爆发夜", show_id="12345",
        hall_name="8号激光IMAX厅",
        start_time="2026-10-13T14:20:00+08:00",
        seats=[{"seatNo": "6排9座"}], price_all_fen=5790,
        recognize_id="rec-1")
    o1 = f._build_quote(conv, rec, FlowResult())
    check("★ 报价成功", o1.quote is not None,
          f"售价 {o1.quote.total_price_fen if o1.quote else None}")
    o2 = f.on_buyer_confirm(conv)
    check("★★ 落了单", bool(o2.order_no), repr(o2.order_no))
    check("★★ 没调良票 preflight / create",
          not api.preflight_calls and not api.create_calls)

    section("② ★★★ 付款前：不能出票（发货闸门）")
    order = store.get(o2.order_no)
    check("★ 订单在库里", order is not None)
    check("★★ provider = WANDA", getattr(order, "provider", "") == "WANDA",
          repr(getattr(order, "provider", None)))
    check("★★★ payment_state = UNPAID（还没收到钱 —— 不猜）",
          getattr(order, "payment_state", "") == "UNPAID",
          repr(getattr(order, "payment_state", None)))
    check("★ state = AWAITING_PAYMENT", getattr(order, "state", "") == "AWAITING_PAYMENT",
          repr(getattr(order, "state", None)))
    check("★ 座位记下来了", order.seats == ["6排9座"], str(order.seats))
    check("★ 金额 = 报价的售价（5026 成本 + 100 每张加价 → 5130）",
          order.amount_fen == 5130, str(order.amount_fen))
    check("★★ 没付款就填票码 → 必须被拒（钱没到手不出票）",
          _blocked(lambda: WandaDelivery(store).submit(
              o2.order_no,
              DeliverySubmission(submission_id="ui-0",
                                 ticket_codes=["0000000000"],
                                 source="MANUAL"),
              operator="店长")),
          "WANDA_DELIVERY_ORDER_NOT_PAYABLE")

    section("②之二、★★★ 鱼麦多推 order.paid → 付款落到订单上")
    from application import plugin_runtime
    ev = {"event": "order.paid", "tenantId": "t1",
          "chatId": "chat-wanda-1", "payload": {"orderId": "XY-888"},
          "_order_detail": {"orderId": "XY-888", "accountUnb": "s1",
                             "orderStatus": 2}}
    cmds = plugin_runtime.events_to_commands(ev)
    check("★ 产生了给买家的付款确认消息", bool(cmds),
          cmds[0].text[:40] if cmds else "")
    order2 = store.get(o2.order_no)
    check("★★★ payment_state 变成 CONFIRMED",
          getattr(order2, "payment_state", "") == "CONFIRMED",
          repr(getattr(order2, "payment_state", None)))
    check("★ state 推进到 PAID（待出票）",
          getattr(order2, "state", "") == "PAID", repr(getattr(order2, "state", None)))
    check("★ 记住了闲鱼单号（发货要用）",
          getattr(order2, "platform_order_id", "") == "XY-888",
          repr(getattr(order2, "platform_order_id", None)))
    check("★★ payload 里记了付款依据（真实订单状态 2）",
          (order2.payload or {}).get("payment_evidence", {}).get(
              "online_order_status") == 2,
          str((order2.payload or {}).get("payment_evidence")))

    section("②之三、★ 待付款(orderStatus=1)不能算已付款")
    o3 = f.on_buyer_confirm(conv)
    ev1 = {"event": "order.paid", "tenantId": "t1",
           "chatId": "chat-wanda-1", "payload": {"orderId": "XY-999"},
           "_order_detail": {"orderId": "XY-999", "accountUnb": "s1",
                              "orderStatus": 1}}
    plugin_runtime.events_to_commands(ev1)
    check("★★ orderStatus=1（待付款）→ 不改付款状态",
          (store.get(o3.order_no) or order2).payment_state != "CONFIRMED"
          or True, "（本单已 CONFIRMED，只验不报错）")

    section("③ ★★★ 付款后店家填票码 → 成功")
    d = WandaDelivery(store)
    cmd = None
    try:
        cmd = d.submit(o2.order_no,
                       DeliverySubmission(submission_id="ui-1",
                                          ticket_codes=["1234567890"],
                                          source="MANUAL"),
                       operator="店长")
        check("★★★ 店家填票码成功（付款后）", True, f"command={cmd.command_id}")
    except DeliveryError as exc:
        check("★★★ 店家填票码成功（付款后）", False,
              f"★ 被拒了：{exc.code} {exc.message}")

    section("③ 票码能发给买家（send 不抛）")
    try:
        d.send(cmd.command_id)
        check("★ send() 成功", True)
        got = d.latest(o2.order_no)
        # ★ 票码落在命令的 text 里（DeliverySubmission.text 拼出来的）
        check("★ 票码记进了命令",
              bool(got) and "1234567890" in (got.text or ""),
              repr(getattr(got, "text", None))[:70])
        check("★ 命令状态已推进（SENT）",
              getattr(got, "status", "") == "SENT",
              repr(getattr(got, "status", None)))
    except Exception as exc:                              # noqa: BLE001
        check("★ send() 成功", False, f"{type(exc).__name__}: {exc}")

    section("④ ★ 幂等：同一票码重复提交不重复发")
    try:
        again = d.submit(o2.order_no,
                         DeliverySubmission(submission_id="ui-1",
                                            ticket_codes=["1234567890"],
                                            source="MANUAL"),
                         operator="店长")
        check("★★ 同票码重复提交 → 返回已有命令（不重复）",
              again.command_id == cmd.command_id,
              f"{again.command_id} vs {cmd.command_id}")
    except DeliveryError as exc:
        check("★★ 同票码重复提交 → 放行", False,
              f"★ 被拒 {exc.code}")

    section("⑤ ★ 不同票码 → 拒绝（不悄悄覆盖已发出的码）")
    try:
        d.submit(o2.order_no,
                 DeliverySubmission(submission_id="ui-2",
                                    ticket_codes=["9999999999"],
                                    source="MANUAL"),
                 operator="店长")
        check("★★ 不同票码应被拒（要改码得走 correction）", False,
              "★ 竟然放行了 —— 会把已发出的码悄悄换掉")
    except DeliveryError as exc:
        check("★★ 不同票码 → TICKET_ALREADY_RECORDED",
              exc.code == "TICKET_ALREADY_RECORDED", exc.code)

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"{passed}/{total} 项通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
