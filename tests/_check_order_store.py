"""
_check_order_store.py —— 订单管理（建单 / 幂等 / 支付 / 交付门槛 / 批准）

★ 覆盖的每一条都有线上依据
    建单幂等      v5-liangpiao-<quote_id>（liangpiao_orders.idempotency_key UNIQUE）
    一报价一单    liangpiao_orders.quote_id UNIQUE
    绑定金额      order_quote_bindings.amount_fen
    支付幂等      order_payment_receipts 主键 (tenant_id, event_id)
    事件指纹      payload.payment_event_fingerprint
    人工批准 60s  payload.human_approval.expires_at
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

from domain.order import (APPROVAL_TTL_SECONDS, Approval, Order,  # noqa: E402
                          OrderStore, PaymentReceipt, QuoteBinding,
                          idempotency_key_for, new_order_id)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ★ 线上真实数据（买家 2002808420 / 会话 65091694508 / 报价 b896fbce-…）
IDENT = {"tenant_id": "107", "shop_id": "2313315754",
         "buyer_id": "2002808420", "chat_id": "65091694508"}
QUOTE = "b896fbce-449e-4f94-86db-9cc8032f7ea2"


def make_order(**kw):
    base = dict(order_id=new_order_id(), quote_id=QUOTE, **IDENT,
                provider="WANDA", cinema="常州新北万达广场店",
                movie="生化危机：爆发夜", show="2026-10-12 19:15",
                seats=["9排16", "9排17", "9排18", "9排19"],
                ticket_count=4, amount_fen=18400,
                idempotency_key=idempotency_key_for(QUOTE, provider="WANDA"))
    base.update(kw)
    return Order(**base)


def main() -> int:                                       # noqa: C901
    store = OrderStore(":memory:")

    section("一、★ 幂等键形态（照线上 v5-liangpiao-<quote_id>）")
    k = idempotency_key_for(QUOTE, provider="WANDA")
    check("键含 provider 与 quote_id", k == f"v5-wanda-{QUOTE}", k)
    check("★ 同一报价多次算出同一把键（才能拦住重复下单）",
          idempotency_key_for(QUOTE, provider="WANDA") == k)
    check("不同渠道的键不同（不串渠道）",
          idempotency_key_for(QUOTE, provider="LIANGPIAO") != k)

    section("二、建单")
    o = make_order()
    store.create(o)
    check("建单成功", store.get(o.order_id) is not None)
    check("初始状态 CREATED（未付款）", store.get(o.order_id).state == "CREATED")
    check("find_by_idempotency 能查到",
          store.find_by_idempotency(o.idempotency_key) is not None)
    check("find_by_quote 能查到", store.find_by_quote(QUOTE) is not None)

    section("三、★★ 两道唯一约束（防重复下单）")
    try:
        store.create(make_order())
        check("★ 重放同一幂等键被拦", False, "居然建成了")
    except ValueError as exc:
        check("★ 重放同一幂等键被拦", "ORDER_IDEMPOTENT_REPLAY" in str(exc),
              str(exc)[:56])
    try:
        store.create(make_order(idempotency_key="another-key"))
        check("★ 同一报价建第二单被拦", False, "居然建成了")
    except ValueError as exc:
        check("★ 同一报价建第二单被拦",
              "ORDER_QUOTE_ALREADY_USED" in str(exc), str(exc)[:56])
    check("没有报价不许建单",
          _raises(lambda: store.create(make_order(
              quote_id="", idempotency_key="k2"))), "ORDER_QUOTE_REQUIRED")
    check("没有幂等键不许建单",
          _raises(lambda: store.create(make_order(
              quote_id="q-x", idempotency_key=""))),
          "ORDER_IDEMPOTENCY_REQUIRED")

    section("四、★ 订单↔报价绑定（记下绑定时的金额）")
    b = QuoteBinding(order_id=o.order_id, quote_id=QUOTE, **IDENT,
                     ticket_count=4, amount_fen=18400)
    store.bind_quote(o.order_id, b)
    got = store.get(o.order_id)
    check("绑定金额落进订单", got.amount_fen == 18400, str(got.amount_fen))
    check("绑定记录留在 payload 里",
          (got.payload.get("quote_binding") or {}).get("quote_id") == QUOTE)
    check("绑定状态是线上的 BOUND_TO_ORDER",
          (got.payload.get("quote_binding") or {}).get("status")
          == "BOUND_TO_ORDER")

    section("五、★★ 支付回执：幂等 + 指纹冲突")
    r1 = PaymentReceipt(tenant_id="107", event_id="evt-1", order_id=o.order_id,
                        amount_fen=18400, fingerprint="fp-aaa",
                        state="CONFIRMED")
    fresh, _ = store.record_payment(r1)
    check("首次记入", fresh is True)
    fresh2, old = store.record_payment(r1)
    check("★ 重放同一事件不重复记", fresh2 is False)
    check("重放返回的是已有那条", old.fingerprint == "fp-aaa")
    conflict = False
    try:
        store.record_payment(PaymentReceipt(tenant_id="107", event_id="evt-1",
                                            fingerprint="fp-DIFFERENT"))
    except ValueError as exc:
        conflict = "PAYMENT_EVENT_CONFLICT" in str(exc)
    check("★★ 同 event_id 指纹不同 → 报冲突（不当重放忽略）", conflict)
    check("回执能按订单查回来",
          len(store.payments_for(o.order_id)) == 1)
    check("★ 幂等键是 (tenant_id, event_id) —— 不同租户同 event_id 互不影响",
          store.record_payment(PaymentReceipt(tenant_id="999",
                                              event_id="evt-1"))[0] is True)

    section("六、★★ 交付门槛（四道，缺一不可）")
    cur = store.get(o.order_id)
    ok, why = cur.can_deliver()
    check("① 未付款不许交付", not ok and "未确认付款" in why, why)

    cur.payment_state = "CONFIRMED"
    cur.state = "PAID"
    ok, why = cur.can_deliver()
    check("② 没取票码不许交付", not ok and "取票码" in why, why)

    cur.ticket_codes = ["2032 5500 7913 72"]
    ok, why = cur.can_deliver()
    check("③ 付款 + 有码 → 可以交付", ok, why)

    cur.state = "DELIVERED"
    ok, why = cur.can_deliver()
    check("④ 终态不许再交付", not ok and "终态" in why, why)

    section("七、★★ 人工批准带【有效期】（线上真实是 60 秒）")
    check("默认有效期就是 60 秒（线上 approved_at→expires_at）",
          APPROVAL_TTL_SECONDS == 60, str(APPROVAL_TTL_SECONDS))
    ap = Approval.issue(approval_id="ap-1", operator_id="admin")
    check("刚签发的批准有效", ap.is_valid())
    old_ap = Approval.issue(
        approval_id="ap-2", operator_id="admin",
        now=datetime.now(timezone.utc) - timedelta(seconds=61))
    check("★ 61 秒前的批准已过期", not old_ap.is_valid())
    # ★ 必须把付款态一起设上 —— 上一节的改动只落在内存对象上，
    #   store.get 拿回来的仍是 UNPAID，会被第①道门槛先拦掉。
    cur2 = store.get(o.order_id)
    cur2.payment_state = "CONFIRMED"
    cur2.state = "PAID"
    cur2.ticket_codes = ["code"]
    cur2.approval = old_ap
    ok, why = cur2.can_deliver()
    check("★ 批准过期 → 不许交付，要重批",
          not ok and "批准已过期" in why, why)
    cur2.approval = ap
    ok, why = cur2.can_deliver()
    check("换新批准后可以交付", ok, why)

    section("八、★ 身份隔离")
    other = dict(IDENT, buyer_id="9999999999")
    check("别的买家看不到这单", store.by_identity(other) == [])
    check("本人能看到", len(store.by_identity(IDENT)) == 1)
    for bad in ({"tenant_id": "107"}, {}):
        check(f"★ 身份不全要报错 {bad}",
              _raises(lambda b=bad: store.by_identity(b), "ORDER_IDENTITY"))

    section("九、按上游订单号反查（回调只给上游单号）")
    cur3 = store.get(o.order_id)
    cur3.provider_order_id = "1493664033"
    store.put(cur3)
    check("能按 provider_order_id 找到订单",
          (store.find_by_provider_order("1493664033") or Order("", "", "", "",
                                                              "", "")).order_id
          == o.order_id)
    check("查不存在的上游单号返回 None",
          store.find_by_provider_order("nope") is None)

    section("十、★★ 下单流程必须真的把订单落库（踩过：订单页永远空的）")
    # ★ 为什么查这个：domain/order 的存储写好了、测试也全过，
    #   但【生产流程里没有任何地方创建订单】—— 下单只写进
    #   conv.pending_order（内存）。结果订单管理页永远是空的，
    #   客户问"订单怎么没有数据"。
    #   ★ 教训：存储层测试全绿 ≠ 功能可用。要断言【真的有人写】。
    import inspect
    sys.path.insert(0, str(ROOT))
    from application.ticket_flow import TicketFlow
    flow_src = inspect.getsource(TicketFlow)
    check("★ 下单流程里有落库动作（_persist_order）",
          "_persist_order" in flow_src, "")
    check("★ 下单成功后调用了它",
          "self._persist_order(" in flow_src)
    check("★★ 落库不是静默失败（有 _order_error 留痕）",
          "_order_error" in flow_src)
    # 装配层要把 order_store 递下去，否则 store 永远是 None
    from application import service as service_module
    svc_src = inspect.getsource(service_module)
    check("★ build_service 把 order_store 递给了 TicketService",
          "order_store=order_store" in svc_src)
    check("★ TicketService 又递给了 TicketFlow",
          "order_store=order_store" in inspect.getsource(
              service_module.TicketService.__init__))
    check("★★ 用的是 ledger 的同一个实例（不是各开一个连接）",
          "ledger.stores()" in svc_src)

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} 项通过")
    print("=" * 74)
    return 0 if passed == total else 1


def _raises(fn, marker=""):
    """调用 fn，若抛 ValueError 且含 marker 则返回 True。"""
    try:
        fn()
    except ValueError as exc:
        return marker in str(exc) if marker else True
    except Exception:                                    # noqa: BLE001
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
