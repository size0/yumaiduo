"""
_check_wanda_delivery.py —— 万达出票交付

★ 覆盖的每一条都有线上依据（application/wanda_delivery.py）
    必须有人操作       if not operator: raise DELIVERY_NOT_ENABLED
    必须已付款         WANDA_DELIVERY_ORDER_NOT_PAYABLE
    内容指纹去重       同 digest → 返回已有
    内容变了要拒       TICKET_ALREADY_RECORDED
    上游单号变了要拒    WANDA_TICKET_SOURCE_CONFLICT
    改码要换版本       ticket_version + 1
    提交 5 分钟有效     delivery_expires_at = now + 5min
    发完码自动发货     SHIP_ORDER + TICKET_DELIVERY_NOTICE
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

from application.wanda_delivery import (DELIVERY_CONTRACT_QR,      # noqa: E402
                                        DELIVERY_TTL_MINUTES,
                                        DeliveryError, DeliverySubmission,
                                        WandaDelivery)
from domain.order import (Order, OrderStore, idempotency_key_for,  # noqa: E402
                          new_order_id)

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


IDENT = {"tenant_id": "107", "shop_id": "2313315754",
         "buyer_id": "2002808420", "chat_id": "65091694508"}
QUOTE = "b896fbce-449e-4f94-86db-9cc8032f7ea2"
CODE = "2032 5500 7913 72"                 # 线上真实的取票码形态


def setup(*, paid=True):
    store = OrderStore(":memory:")
    o = Order(order_id=new_order_id(), quote_id=QUOTE, **IDENT,
              provider="WANDA", cinema="常州新北万达广场店",
              movie="生化危机：爆发夜", show="2026-10-12 19:15",
              seats=["9排16", "9排17", "9排18", "9排19"],
              ticket_count=4, amount_fen=18400,
              idempotency_key=idempotency_key_for(QUOTE, provider="WANDA"))
    if paid:
        o.payment_state = "CONFIRMED"
        o.state = "PAID"
    store.create(o)
    return store, o, WandaDelivery(store)


def main() -> int:                                       # noqa: C901
    section("一、常量对齐线上")
    check("提交有效期 5 分钟（线上 delivery_expires_at 是 +5min）",
          DELIVERY_TTL_MINUTES == 5, str(DELIVERY_TTL_MINUTES))
    check("交付契约 QR_SHIP_V1（线上 SHIP_ORDER payload 里的值）",
          DELIVERY_CONTRACT_QR == "QR_SHIP_V1", DELIVERY_CONTRACT_QR)

    section("二、★★ 出票必须有人操作（第一道闸门）")
    store, o, d = setup(paid=False)
    try:
        d.submit(o.order_id, DeliverySubmission(submission_id="s1",
                                                ticket_codes=[CODE]),
                 operator="")
        check("★ 没 operator 被拒", False, "居然过了")
    except DeliveryError as exc:
        check("★ 没 operator 被拒", exc.code == "DELIVERY_NOT_ENABLED",
              exc.code)

    section("三、★★ 必须已付款（第二道闸门）")
    try:
        d.submit(o.order_id, DeliverySubmission(submission_id="s1",
                                                ticket_codes=[CODE]),
                 operator="admin")
        check("★ 未付款被拒", False, "居然过了")
    except DeliveryError as exc:
        check("★ 未付款被拒",
              exc.code == "WANDA_DELIVERY_ORDER_NOT_PAYABLE", exc.code)

    section("四、付款后提交")
    store, o, d = setup()
    sub = DeliverySubmission(submission_id="s1", ticket_codes=[CODE],
                             provider_order_id="1493664033", source="AUTO")
    cmd = d.submit(o.order_id, sub, operator="admin")
    check("提交成功，状态 PENDING", cmd.status == "PENDING", cmd.status)
    check("文本形态照线上（取票码：xxx）", cmd.text == f"取票码：{CODE}",
          cmd.text)
    check("版本从 1 开始", cmd.ticket_version == 1, str(cmd.ticket_version))
    check("★ 提交带 5 分钟有效期", cmd.expires_at is not None)
    check("有效期确实是 5 分钟",
          abs((cmd.expires_at - cmd.created_at) - timedelta(minutes=5))
          < timedelta(seconds=2))
    check("★ 记下了操作人（出票要有人负责）", cmd.operator == "admin",
          cmd.operator)
    check("取票码也写进了订单",
          store.get(o.order_id).ticket_codes == [CODE])
    check("上游订单号写进订单",
          store.get(o.order_id).provider_order_id == "1493664033")

    section("五、★★ 幂等：同内容重放 → 返回已有（不是错误）")
    again = d.submit(o.order_id, sub, operator="admin")
    check("★ 重放返回同一条命令", again.command_id == cmd.command_id)
    check("没有新建第二条", len(d.history(o.order_id)) == 1,
          str(len(d.history(o.order_id))))

    section("六、★★ 同单不同内容 → 拒绝（不覆盖已发出的码）")
    try:
        d.submit(o.order_id,
                 DeliverySubmission(submission_id="s2",
                                    ticket_codes=["OTHER CODE"],
                                    provider_order_id="1493664033"),
                 operator="admin")
        check("★ 不同内容被拒", False, "居然覆盖了")
    except DeliveryError as exc:
        check("★ 不同内容被拒",
              exc.code == "TICKET_ALREADY_RECORDED", exc.code)

    section("七、★ AUTO 来源 + 上游单号变了 → 冲突（要人工核对）")
    try:
        d.submit(o.order_id,
                 DeliverySubmission(submission_id="s3", ticket_codes=[CODE],
                                    provider_order_id="999999", source="AUTO"),
                 operator="admin")
        check("★ 上游单号冲突被拒", False, "居然过了")
    except DeliveryError as exc:
        check("★ 上游单号冲突被拒",
              exc.code == "WANDA_TICKET_SOURCE_CONFLICT", exc.code)

    section("八、★★ 改取票码要【换版本】，且只能改已发出的")
    # 还没发出 → 不许改
    try:
        d.submit(o.order_id,
                 DeliverySubmission(submission_id="s4",
                                    ticket_codes=["NEW CODE"],
                                    replaces_text_command_id=cmd.command_id),
                 operator="admin")
        check("★ 未发出的命令不许改", False, "居然改成了")
    except DeliveryError as exc:
        check("★ 未发出的命令不许改",
              exc.code == "QR_CORRECTION_REQUIRES_CONFIRMED_TEXT", exc.code)
    # 先发出去
    d.send(cmd.command_id)
    fixed = d.submit(o.order_id,
                     DeliverySubmission(submission_id="s5",
                                        ticket_codes=["NEW CODE"],
                                        replaces_text_command_id=cmd.command_id),
                     operator="admin")
    check("★ 已发出的可以改，且版本 +1",
          fixed.ticket_version == cmd.ticket_version + 1,
          f"v{fixed.ticket_version}")
    check("★ 改码是新命令，不是覆盖旧命令",
          fixed.command_id != cmd.command_id)
    check("旧命令还在（可追溯）", d.commands.get(cmd.command_id) is not None)

    section("九、★★ 发取票码：三道检查")
    store, o, d = setup()
    c2 = d.submit(o.order_id, DeliverySubmission(submission_id="t1",
                                                 ticket_codes=[CODE]),
                  operator="admin")
    d.send(c2.command_id)
    check("发出后状态 SENT", d.commands[c2.command_id].status == "SENT")
    check("★ 重复发是幂等的（不报错）",
          d.send(c2.command_id).status == "SENT")
    # 过期
    store2, o2, d2 = setup()
    c3 = d2.submit(o2.order_id, DeliverySubmission(submission_id="t2",
                                                   ticket_codes=[CODE]),
                   operator="admin")
    c3.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    try:
        d2.send(c3.command_id)
        check("★ 超期提交不许发", False, "居然发出去了")
    except DeliveryError as exc:
        check("★ 超期提交不许发", exc.code == "DELIVERY_EXPIRED", exc.code)

    section("十、★★ 发完码 → 自动发货 + 通知（你说的那一步）")
    fu = d.queue_followups(c2.command_id)
    check("★ 排了 SHIP_ORDER（自动发货）", fu.get("SHIP_ORDER") == "QUEUED",
          str(fu.get("SHIP_ORDER")))
    check("★ 排了 TICKET_DELIVERY_NOTICE（通知）",
          fu.get("TICKET_DELIVERY_NOTICE") == "QUEUED")
    after = store.get(o.order_id)
    check("订单变成 DELIVERED", after.state == "DELIVERED", after.state)
    check("标记已发货", after.shipped is True)
    check("记下交付时间", after.delivered_at is not None)
    # 没发码不许发货
    store3, o3, d3 = setup()
    c4 = d3.submit(o3.order_id, DeliverySubmission(submission_id="t3",
                                                   ticket_codes=[CODE]),
                   operator="admin")
    try:
        d3.queue_followups(c4.command_id)
        check("★ 还没发码不许发货", False, "居然发货了")
    except DeliveryError as exc:
        check("★ 还没发码不许发货", exc.code == "FOLLOWUP_BEFORE_SEND",
              exc.code)

    section("十一、★ 授权与结果记录（线上 authorize/record_result）")
    store4, o4, d4 = setup()
    c5 = d4.submit(o4.order_id, DeliverySubmission(submission_id="t4",
                                                   ticket_codes=[CODE]),
                   operator="admin")
    d4.authorize_delivery(c5.command_id, operator="admin")
    check("授权记到了命令上",
          d4.commands[c5.command_id].followups.get("authorized_by") == "admin")
    d4.record_result(c5.command_id, ok=False, detail="上游拒绝")
    check("★ 执行失败时状态翻 FAILED",
          d4.commands[c5.command_id].status == "FAILED")
    check("失败原因记下来了",
          d4.commands[c5.command_id].followups.get("result_detail")
          == "上游拒绝")

    section("十一之二、★★ 图片交付：只能发一种 + alicdn 硬校验")
    # ★ 为什么必须卡（链路文档第 3 条约束）：
    #   插件只认 alicdn 图片地址（IMAGE_HOSTS=['alicdn.com','tbcdn.cn']）。
    #   不是 alicdn 的图，插件发不出去 —— 命令会永远卡着，买家收不到票。
    #   ⇒ 宁可在入口拒掉，也不要收下一条注定发不出去的命令。
    store_i, o_i, d_i = setup()
    good_img = {"imageUrl": "https://img.alicdn.com/imgextra/i1/x.jpg",
                "width": 800, "height": 1200}

    def _img_case(img, expect_code, label):
        s9, o9, d9 = setup()
        try:
            d9.submit(o9.order_id,
                      DeliverySubmission(submission_id="i", image=img,
                                         provider_order_id="P1",
                                         source="MANUAL"),
                      operator="admin")
            got = "OK"
        except DeliveryError as exc:
            got = exc.code
        check(label, got == expect_code, f"{got}（期望 {expect_code}）")

    # 先确认合法的能过
    s_ok, o_ok, d_ok = setup()
    cmd_i = d_ok.submit(o_ok.order_id,
                        DeliverySubmission(submission_id="i0", image=good_img,
                                           provider_order_id="P1"),
                        operator="admin")
    check("★ 合法 alicdn 图能提交", cmd_i.text == "" and cmd_i.digest, "文本为空（发图不发字）")

    # ① 键必须正好是那三个
    _img_case({"imageUrl": good_img["imageUrl"], "width": 800},
              "INVALID_TICKET_IMAGE", "★ 少一个键 → 拒")
    _img_case(dict(good_img, extra="x"), "INVALID_TICKET_IMAGE",
              "★★ 多一个键 → 也拒（插件按三个键读，多给说明理解错了协议）")
    # ② 域名必须 alicdn 系
    _img_case({"imageUrl": "https://evil.example.com/x.jpg",
               "width": 800, "height": 1200}, "INVALID_TICKET_IMAGE",
              "★★ 非 alicdn 域名 → 拒（插件发不出去）")
    _img_case({"imageUrl": "http://img.alicdn.com/x.jpg",
               "width": 800, "height": 1200}, "INVALID_TICKET_IMAGE",
              "★ http（非 https）→ 拒")
    _img_case({"imageUrl": "https://user:pw@img.alicdn.com/x.jpg",
               "width": 800, "height": 1200}, "INVALID_TICKET_IMAGE",
              "★ 带用户名密码的 URL → 拒")
    # ③ 尺寸必须是正整数
    _img_case({"imageUrl": good_img["imageUrl"], "width": 0, "height": 1200},
              "INVALID_TICKET_IMAGE", "★ width=0 → 拒")
    _img_case({"imageUrl": good_img["imageUrl"], "width": "800", "height": 1200},
              "INVALID_TICKET_IMAGE", "★ width 是字符串 → 拒")
    _img_case({"imageUrl": good_img["imageUrl"], "width": True, "height": 1200},
              "INVALID_TICKET_IMAGE", "★★ width 是 bool → 拒（True 是 int 的子类）")
    _img_case({"imageUrl": good_img["imageUrl"], "width": 99999, "height": 1200},
              "INVALID_TICKET_IMAGE", "★ 超过 20000 → 拒")
    # ④ 文本与图片只能给一个
    _img_case(None, "ONE_TICKET_MESSAGE_REQUIRED", "★ 两个都没给 → 拒")
    s_b, o_b, d_b = setup()
    try:
        d_b.submit(o_b.order_id,
                   DeliverySubmission(submission_id="b", ticket_codes=[CODE],
                                      image=good_img, provider_order_id="P1"),
                   operator="admin")
        both = "OK"
    except DeliveryError as exc:
        both = exc.code
    check("★★ 文本和图片都给 → 拒（插件只能发一个，另一个会被丢掉）",
          both == "ONE_TICKET_MESSAGE_REQUIRED", both)
    # ⑤ AUTO 必须带上游订单号
    s_a, o_a, d_a = setup()
    try:
        d_a.submit(o_a.order_id,
                   DeliverySubmission(submission_id="a", ticket_codes=[CODE],
                                      source="AUTO"),
                   operator="admin")
        auto = "OK"
    except DeliveryError as exc:
        auto = exc.code
    check("★ AUTO 来源缺上游订单号 → 拒",
          auto == "WANDA_ORDER_ID_REQUIRED", auto)

    section("十二、给店家出票后台看的视图")
    store5, o5, d5 = setup()
    c6 = d5.submit(o5.order_id, DeliverySubmission(submission_id="t5",
                                                   ticket_codes=[CODE],
                                                   provider_order_id="P1"),
                   operator="admin")
    v = d5.view(o5.order_id)
    check("视图有订单状态", v["state"] == "PAID", str(v["state"]))
    check("视图有付款态", v["payment_state"] == "CONFIRMED")
    check("视图有取票码", v["ticket_codes"] == [CODE])
    check("视图有交付状态", v["delivery_status"] == "PENDING",
          str(v["delivery_status"]))
    check("★ 未提交时视图说 NOT_SUBMITTED",
          d5.view(new_order_id())["delivery_status"] == "NOT_SUBMITTED")

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} 项通过")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
