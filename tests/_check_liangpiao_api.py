"""
_check_liangpiao_api.py —— 接口层与回调层自检（不需要 AppKey，不联网）

★ 重点验三类东西：
  1. 参数校验（在发出去之前就拦住，而不是等 API 返回 100001）
  2. 金额转换（文档：响应里金额是字符串，别让 "3510" 流进业务层）
  3. 回调验签与那三种 order.failed 的分支
"""

import json
import sys
from datetime import datetime, timedelta, timezone
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

from integrations.liangpiao import callbacks as cb
from integrations.liangpiao.api import (AREA_HIGHEST, 
                                        MATCH_NONE, NOMATCH_NOT_FOUND,
                                        NOMATCH_SHOW_EXPIRED, ORDER_TERMINAL,
                                        PRICE_FIXED, PRICE_LIMIT, TICKET_FAST,
                                        TICKET_FLASH, TICKET_STANDARD, LiangPiaoAPI,
                                        OrderDetail, QuoteParams, SeatMap,
                                        make_out_order_no, _valid_tel)
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


# ==================================================================== 夹具
class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def send(self, url, body, headers, timeout):
        self.sent.append({"url": url, "body": body, "headers": dict(headers),
                          "timeout": timeout})
        idx = min(len(self.sent) - 1, len(self.responses) - 1)
        resp = self.responses[idx]
        if isinstance(resp, Exception):
            raise resp
        return resp


def envelope(code=0, message="success", request_id="RID", data=None):
    body = {"code": code, "message": message, "requestId": request_id}
    if data is not None:
        body["data"] = data
    return json.dumps(body, ensure_ascii=False)


def make_api(responses):
    fake = FakeTransport(responses)
    client = LiangPiaoClient("KEY", "SECRET", base_url="https://example.test",
                             transport=fake, sleep=lambda _: None)
    return LiangPiaoAPI(client), fake


def sent_payload(fake, idx=0):
    return json.loads(fake.sent[idx]["body"])


# ==================================================================== 一
def main() -> int:
    section("一、金额转换（文档：响应里是字符串）")
    from integrations.liangpiao.api import fen, fen_or_none
    check('"3510" → 3510', fen("3510") == 3510)
    check('3510 → 3510', fen(3510) == 3510)
    check("None / 空串 → 0", fen(None) == 0 and fen("") == 0)
    check("坏值 → 0（不抛）", fen("abc") == 0)
    check('"35.10" → 35（宽松转，不会崩）', fen("35.10") == 35)
    check("fen_or_none 保留 None", fen_or_none(None) is None)

    section("二、QuoteParams：询价与下单共用同一份")
    qp = QuoteParams(area_quote_strategy=AREA_HIGHEST, invoice_required=True)
    check("字段名按文档", qp.to_fields() == {"areaQuoteStrategy": "HIGHEST",
                                            "invoiceRequired": True},
          str(qp.to_fields()))
    try:
        QuoteParams(area_quote_strategy="乱写")
        check("非法策略应报错", False)
    except ValueError:
        check("非法跨分区策略报错", True)

    # ★ 询价与下单必须传出完全相同的这两个字段
    api, fake = make_api([(200, envelope(data={"available": True, "totalAmount": "100"})),
                          (200, envelope(data={"orderNo": "LP1", "status": "SUBMITTING"}))])
    api.ch5_preflight("889900", [{"rowNo": 1, "colNo": 1}], TICKET_STANDARD,
                      PRICE_LIMIT, params=qp)
    api.ch5_create("OUT-1", "889900", [{"rowNo": 1, "colNo": 1}], TICKET_STANDARD,
                   PRICE_LIMIT, "13800000000", params=qp)
    pre, cre = sent_payload(fake, 0), sent_payload(fake, 1)
    check("★ 询价的 areaQuoteStrategy 与下单一致",
          pre["areaQuoteStrategy"] == cre["areaQuoteStrategy"] == "HIGHEST")
    check("★ 询价的 invoiceRequired 与下单一致",
          pre["invoiceRequired"] == cre["invoiceRequired"] is True)

    section("三、路径与超时")
    api, fake = make_api([(200, envelope(data={}))])
    api.ch4_ping()
    check("ping 路径正确", fake.sent[0]["url"].endswith("/api/v1/ping"),
          fake.sent[0]["url"])

    api, fake = make_api([(200, envelope(data={"hall": {}, "seats": []}))])
    api.ch4_seat_list("889900")
    check("★ 座位图超时 12 秒（文档要求 ≥10）", fake.sent[0]["timeout"] == 12.0,
          str(fake.sent[0]["timeout"]))

    api, fake = make_api([(200, envelope(data={"finalResults": {}}))])
    api.ch7_recognize("https://img.example.com/a.jpg", "天津")
    check("★ 识别超时 5 秒（文档 p95 5.2s）", fake.sent[0]["timeout"] == 5.0,
          str(fake.sent[0]["timeout"]))
    check("识别传了 cityName", sent_payload(fake)["cityName"] == "天津")

    section("四、参数校验（发出去之前就拦）")
    api, _ = make_api([(200, envelope(data={}))])

    try:
        api.ch4_cinema_list(longitude=117.2)
        check("经纬度只传一个应报错", False)
    except ValueError as exc:
        check("经纬度必须成对", "成对" in str(exc))
    try:
        api.ch4_cinema_list(longitude=117.2, latitude=39.0)
        check("传经纬度但没 cityCode 应报错", False)
    except ValueError as exc:
        check("传经纬度必须带 cityCode", "cityCode" in str(exc))
    try:
        api.ch4_cinema_list(page_size=500)
        check("pageSize 超 200 应报错", False)
    except ValueError:
        check("pageSize 超上限报错", True)

    for bad_mode in ("WRONG", ""):
        try:
            api.ch5_preflight("1", [{"rowNo": 1, "colNo": 1}], bad_mode, PRICE_FIXED)
            check(f"ticketMode={bad_mode!r} 应报错", False)
        except ValueError:
            check(f"非法 ticketMode {bad_mode!r} 报错", True)
    try:
        api.ch5_preflight("1", [{"rowNo": 1, "colNo": 1}], TICKET_STANDARD, "WRONG")
        check("非法 priceMode 应报错", False)
    except ValueError:
        check("非法 priceMode 报错", True)

    # 座位数 1~6
    for n, should_fail in ((0, True), (1, False), (6, False), (7, True)):
        try:
            api._seat_payload([{"rowNo": 1, "colNo": i} for i in range(1, n + 1)])
            check(f"{n} 个座位", not should_fail, "★ 本该报错")
        except ValueError:
            check(f"{n} 个座位被拒", should_fail, "★ 本该通过")

    # tel
    for tel, ok in (("13800000000", True), ("12800000000", False),
                    ("1380000000", False), ("1380000000a", False), ("", False)):
        check(f"tel {tel!r} → {'合法' if ok else '非法'}",
              _valid_tel(tel) is ok)

    # outOrderNo
    try:
        api.ch5_create("", "1", [{"rowNo": 1, "colNo": 1}], TICKET_STANDARD,
                       PRICE_FIXED, "13800000000")
        check("空单号应报错", False)
    except ValueError:
        check("空 outOrderNo 报错", True)
    try:
        api.ch5_create("X" * 65, "1", [{"rowNo": 1, "colNo": 1}], TICKET_STANDARD,
                       PRICE_FIXED, "13800000000")
        check("超长单号应报错", False)
    except ValueError:
        check("outOrderNo >64 报错", True)

    # confirm 至少给一个 ID
    try:
        api.ch7_confirm("8642")
        check("confirm 不给任何 ID 应报错", False)
    except ValueError:
        check("confirm 至少要一个 ID", True)
    try:
        api.ch7_confirm("8642", seats=[{"rowNo": 1, "colNo": 1}])
        check("confirm 给 seats 但没 showId 应报错", False)
    except ValueError:
        check("confirm 给座位必须带 showId", True)

    section("五、★ maxPrice 必须是数字")
    api, fake = make_api([(200, envelope(data={"orderNo": "LP1", "status": "SUBMITTING",
                                               "freezeAmount": "7020", "seatCount": 2}))])
    api.ch5_create("OUT-1", "889900", [{"rowNo": 1, "colNo": 1}], TICKET_STANDARD,
                   PRICE_LIMIT, "13800000000", max_price_fen=8000)
    p = sent_payload(fake)
    check("★ maxPrice 是 int", isinstance(p["maxPrice"], int), type(p["maxPrice"]).__name__)
    check("maxPrice 值正确", p["maxPrice"] == 8000)
    check("priceMode 传了 LIMIT", p["priceMode"] == "LIMIT")
    check("没有白传 allowSeatChange 以外的字段", "notifyUrl" not in p)

    section("六、响应对象：金额都转成了分")
    api, _ = make_api([(200, envelope(data={
        "orderNo": "LP20260903053750CH4VKWDS", "outOrderNo": "OUT-1",
        "status": "TICKETED", "showId": "889900", "cinemaName": "某某影城",
        "movieName": "某某片", "hallName": "6号厅", "seatCount": 2,
        "seats": [{"rowNo": 4, "colNo": 1}], "actualSeats": ["4排1座", "4排2座"],
        "seatChanged": None, "quoteAmount": "7020", "freezeAmount": "7020",
        "settleAmount": None, "estimatedSettleAmount": "2823",
        "marketAmount": "7780", "refundable": True,
        "pickupUrl": "https://t-v3.example/abc",
        "tickets": [{"seatNo": "4排1座", "ticketCode": "A1B2"}]}))])
    d = api.ch5_order_detail("LP1")
    check("quoteAmount 转分", d.quote_amount_fen == 7020)
    check("estimatedSettleAmount 转分", d.estimated_settle_fen == 2823)
    check("settleAmount 为 null → None（未结算）", d.settle_amount_fen is None)
    check("actualSeats 解析", d.actual_seats == ["4排1座", "4排2座"])
    check("★ seatChanged 为 null 时保持 None（不能当未换座）", d.seat_changed is None)
    check("已出票 → 凭证可用", d.tickets_valid)

    # ★ 已关闭订单的凭证不可用
    api, _ = make_api([(200, envelope(data={
        "orderNo": "LP2", "status": "FAILED", "seats": [],
        "tickets": [{"ticketCode": "SHOULD_NOT_USE"}]}))])
    d2 = api.ch5_order_detail("LP2")
    check("★ 已关闭订单（FAILED）即使有票码也不可用", not d2.tickets_valid,
          f"status={d2.status} tickets={len(d2.tickets)}")
    check("FAILED 是终态", d2.is_terminal)

    section("七、SeatMap：分区厅与限购")
    api, _ = make_api([(200, envelope(data={
        "hall": {"name": "6号厅", "rowCount": 12, "colCount": 20},
        "maxSeatsPerOrder": 4, "graphReliable": True,
        "areas": [{"areaId": "0", "originalPrice": "3400"}],
        "seats": [{"seatNo": "4排1座", "status": "AVAILABLE"},
                  {"seatNo": "4排2座", "status": "SOLD"}]}))])
    sm = api.ch4_seat_list("889900")
    check("单笔限购按接口给的 4（不是平台上限 6）", sm.max_seats_per_order == 4)
    check("可选座位只算 AVAILABLE", len(sm.available_seats()) == 1)
    check("识别为分区厅", sm.has_areas)
    check("graphReliable 解析", sm.graph_reliable is True)

    section("八、PreflightResult：成本口径")
    api, _ = make_api([(200, envelope(data={
        "available": True, "totalAmount": "3510", "estimateAmount": "3310",
        "marketAmount": "3890", "estimated": True}))])
    pf = api.ch5_preflight("1", [{"rowNo": 1, "colNo": 1}], TICKET_STANDARD, PRICE_LIMIT)
    check("totalAmount 转分", pf.total_amount_fen == 3510)
    check("estimateAmount 转分", pf.estimate_amount_fen == 3310)
    check("★ 成本基准优先 estimateAmount", pf.cost_basis_fen == 3310,
          str(pf.cost_basis_fen))
    check("estimated 标记解析", pf.estimated is True)

    api, _ = make_api([(200, envelope(data={"available": False, "reason": "座位被占"}))])
    pf2 = api.ch5_preflight("1", [{"rowNo": 1, "colNo": 1}], TICKET_STANDARD, PRICE_FIXED)
    check("不可购时 available=False", not pf2.available)
    check("不可购带 reason", pf2.reason == "座位被占")

    section("九、★ 识别结果：matchLevel 决定能不能直接下单")
    api, _ = make_api([(200, envelope(data={
        "recognizeId": "8642", "cached": False,
        "finalResults": {"matchLevel": "EXACT", "showId": "889900",
                         "hallName": "6号厅", "startTime": "2026-07-28T19:30:00+08:00",
                         "cinemaId": 1486, "movieId": 2, "cinemaHitNums": 1,
                         "maxSeatsPerOrder": 4, "seatMatched": True,
                         "prices": [{"ticketMode": "STANDARD"}]}}))])
    r = api.ch7_recognize("https://x/a.jpg")
    check("EXACT 可直接下单", r.can_order_directly)
    check("showId 解析", r.show_id == "889900")
    check("★ 用 hallName（目录厅名）", r.hall_name == "6号厅")
    check("★ 用 startTime（ISO 带时区）", r.start_time == "2026-07-28T19:30:00+08:00")
    check("限购取接口值 4", r.effective_max_seats == 4)
    check("无告警", not r.warnings(), str(r.warnings()))

    # CANDIDATE
    api, _ = make_api([(200, envelope(data={
        "recognizeId": "8669",
        "finalResults": {"matchLevel": "CANDIDATE", "cinemaId": 2035, "movieId": 57,
                         "showId": None, "hallName": None, "startTime": None,
                         "seatMatched": False, "cinemaHitNums": 1,
                         "candidates": {"cinemas": [], "movies": [],
                                        "shows": [{"showId": "17208231",
                                                   "hall": "4号激光厅"}]}}}))])
    r2 = api.ch7_recognize("https://x/a.jpg")
    check("CANDIDATE 不能直接下单", not r2.can_order_directly)
    check("需要挑候选", r2.needs_candidate_pick)
    check("★ 影院影片已收敛，只差场次", r2.pending_dimensions() == ["show"],
          str(r2.pending_dimensions()))
    check("★ 只对未唯一的维度发问", list(r2.candidate_options().keys()) == ["show"],
          str(list(r2.candidate_options().keys())))
    check("CANDIDATE 时 startTime 为空", r2.start_time is None)
    check("局限购退回平台上限 6", r2.effective_max_seats == 6)

    # NONE + SHOW_EXPIRED
    api, _ = make_api([(200, envelope(data={
        "recognizeId": None,
        "finalResults": {"matchLevel": "NONE", "noMatchReason": "SHOW_EXPIRED",
                         "cinemaId": 1486, "movieId": 2,
                         "candidates": {"cinemas": [], "movies": [], "shows": []}}}))])
    r3 = api.ch7_recognize("https://x/a.jpg")
    check("★ SHOW_EXPIRED 能识别", r3.is_show_expired)
    check("NONE 不能下单", not r3.can_order_directly)
    check("★ 身份已齐时不把影院影片当待确认",
          r3.pending_dimensions() == ["show"], str(r3.pending_dimensions()))
    check("★ 身份已齐 + 场次空 → 没有候选可问",
          r3.candidate_options() == {}, str(r3.candidate_options()))

    # 需要人工核查的情况
    api, _ = make_api([(200, envelope(data={
        "recognizeId": "1",
        "finalResults": {"matchLevel": "EXACT", "showId": "1", "cinemaId": 1,
                         "movieId": 1, "cinemaHitNums": 3,
                         "priceMismatch": True, "seatMatched": False,
                         "seatConfirmRequired": True,
                         "seatConfirmReasons": ["卡片遮挡"]}}))])
    r4 = api.ch7_recognize("https://x/a.jpg")
    w = r4.warnings()
    check("★ priceMismatch 报警", any("漏识别" in x for x in w), str(w))
    check("★ seatMatched=false 报警", any("座位图" in x for x in w), str(w))
    check("★ seatConfirmRequired 报警", any("核对" in x for x in w), str(w))
    check("★ 影院多命中报警（约 1/4 真实样本）", any("二次确认" in x for x in w), str(w))

    section("十、票速档位按距开场时间挑")
    api, _ = make_api([(200, envelope(data={}))])
    now = datetime(2026, 7, 28, 18, 0, tzinfo=timezone(timedelta(hours=8)))
    cases = [("2026-07-28T18:10:00+08:00", TICKET_FLASH),
             ("2026-07-28T18:30:00+08:00", TICKET_FAST),
             ("2026-07-28T20:00:00+08:00", TICKET_STANDARD)]
    for start, expect in cases:
        got = api.pick_ticket_mode(start, now=now)
        check(f"距开场 {(datetime.fromisoformat(start) - now).seconds // 60} 分钟 → {expect}",
              got == expect, got)
    check("时间缺失时退回 STANDARD", api.pick_ticket_mode(None) == TICKET_STANDARD)

    section("十一、订单号生成")
    n1 = make_out_order_no("YMD", 1)
    check("≤64 字符", len(n1) <= 64, n1)
    check("带前缀", n1.startswith("YMD-"))
    check("不同序号不同号", make_out_order_no("YMD", 1) != make_out_order_no("YMD", 2))

    # ================================================================ 回调
    section("十二、★ 回调验签")
    secret, key = "SECRET", "KEY"
    body = json.dumps({"event": "order.ticketed",
                       "data": {"orderNo": "LP1", "outOrderNo": "OUT-1",
                                "version": 1, "tickets": [{"ticketCode": "A1"}]}},
                      ensure_ascii=False)
    ts, nonce = "1700000000", "n1"
    sign = sign_body(key, secret, ts, nonce, body)
    headers = {"Content-Type": "application/json", "X-App-Key": key,
               "X-Timestamp": ts, "X-Nonce": nonce, "X-Sign": sign}

    # 正确签名
    ev, ack = cb.handle(key, secret, headers, body, now=1700000000)
    check("验签通过", ev is not None)
    check("应答是 {\"code\":0}", ack == '{"code":0}', ack)
    check("事件名解析", ev and ev.event == "order.ticketed")

    # 签名错
    bad = dict(headers, **{"X-Sign": "deadbeef"})
    ev2, ack2 = cb.handle(key, secret, bad, body, now=1700000000)
    check("★ 签名错 → 事件为 None", ev2 is None)
    check("★ 签名错 → 应答【不是】{\"code\":0}",
          ack2 != '{"code":0}', ack2)

    # body 被改
    ev3, _ = cb.handle(key, secret, headers, body + " ", now=1700000000)
    check("★ body 被改动 → 验签失败", ev3 is None)

    # 时间戳过期
    ev4, ack4 = cb.handle(key, secret, headers, body, now=1700000000 + 400)
    check("★ 时间戳超 5 分钟 → 拒绝", ev4 is None)
    check("超时戳应答不是 0", ack4 != '{"code":0}')

    # 毫秒时间戳
    ms_headers = dict(headers, **{"X-Timestamp": "1700000000000"})
    ev5, _ = cb.handle(key, secret, ms_headers, body, now=1700000000)
    check("★ 13 位毫秒时间戳 → 拒绝", ev5 is None)

    # nonce 重放
    seen = set()
    ev6, _ = cb.handle(key, secret, headers, body, now=1700000000, seen_nonces=seen)
    ev7, ack7 = cb.handle(key, secret, headers, body, now=1700000000, seen_nonces=seen)
    check("首次通过", ev6 is not None)
    check("★ nonce 重放被拒", ev7 is None)
    check("重放应答不是 0", ack7 != '{"code":0}')

    # 缺头
    ev8, _ = cb.handle(key, secret, {"X-App-Key": key}, body, now=1700000000)
    check("缺签名头被拒", ev8 is None)

    # 头名大小写不敏感
    lower = {"x-app-key": key, "x-timestamp": ts, "x-nonce": nonce, "x-sign": sign}
    ev9, _ = cb.handle(key, secret, lower, body, now=1700000000)
    check("头名大小写不敏感", ev9 is not None)

    section("十三、★ 幂等键（orderNo + event，票根类再加 version）")
    def mk(event, **data):
        d = {"orderNo": "LP1", "outOrderNo": "OUT-1"}
        d.update(data)
        return cb.CallbackEvent(event=event, data=d)

    e1, e2 = mk("order.ticketed", version=1), mk("order.ticketed", version=1)
    e3 = mk("order.ticketed", version=2)
    check("同事件同版本 → 同一个键", e1.dedupe_key == e2.dedupe_key, e1.dedupe_key)
    check("★ 票根 version 递增 → 键不同（是新内容不是重复）",
          e3.dedupe_key != e1.dedupe_key, f"{e1.dedupe_key} vs {e3.dedupe_key}")
    check("ticket.updated 也算票根类", mk("ticket.updated", version=1).is_ticket_event)
    check("order.settled 不带 version",
          mk("order.settled").dedupe_key == "order.settled:LP1",
          mk("order.settled").dedupe_key)

    section("十四、★ order.failed 的三种情况")
    # ① 出票失败
    f1 = mk("order.failed", failCode="300007",
            failReason="座位已被占用或还未解锁", lossAmount="0", refundAmount="2882")
    check("出票失败：有 failCode", f1.fail_code == "300007")
    check("出票失败：不是取消", not f1.is_cancelled)
    check("出票失败：损失 0", f1.loss_amount_fen == 0)
    check("出票失败：退回 2882", f1.refund_amount_fen == 2882)
    check("出票失败：全额退回", f1.is_full_refund)

    # ② 有损关单
    f2 = mk("order.failed", failCode="300007", failReason="上游关单",
            lossAmount="300", refundAmount="2500")
    check("★ 有损关单：不全额退", not f2.is_full_refund)
    check("有损关单：损失 300", f2.loss_amount_fen == 300)

    # ③ 出票前取消（字段更少）
    f3 = mk("order.failed", failReason="下游主动取消")
    check("★ 取消场景没有 failCode", f3.fail_code is None)
    check("★ 判定为取消", f3.is_cancelled)
    check("★ 取消场景缺 refundAmount → 视为未知(-1)", f3.refund_amount_fen == -1)
    check("★ 取消视为全额退回", f3.is_full_refund)

    # ④ 下单未受理
    f4 = mk("order.failed", failCode="300004", failReason="暂无可用供应商",
            lossAmount="0", refundAmount="0")
    check("下单未受理：两项都是 0", f4.loss_amount_fen == 0 and f4.refund_amount_fen == 0)
    check("下单未受理：全额退回", f4.is_full_refund)

    section("十五、★ 票码的四种形态")
    # 一座一码
    t1 = mk("order.ticketed", version=1, actualSeats=["8排8座"],
            tickets=[{"seatNo": "8排8座", "ticketCode": "A1B2C3D4"}])
    check("一座一码", t1.ticket_codes == ["A1B2C3D4"], str(t1.ticket_codes))
    # 两座两码
    t2 = mk("order.ticketed", version=1, actualSeats=["6排9座", "6排10座"],
            tickets=[{"seatNo": "6排9座", "ticketCode": "738201"},
                     {"seatNo": "6排10座", "ticketCode": "738202"}])
    check("两座两码", len(t2.ticket_codes) == 2, str(t2.ticket_codes))
    # 一码多座
    t3 = mk("order.ticketed", version=1, actualSeats=["3排9座", "3排8座"],
            tickets=[{"seatNo": "3排9座,3排8座", "ticketCode": "947094|806351"}])
    check("一码多座（seatNo 逗号拼接）", t3.ticket_codes == ["947094|806351"],
          str(t3.ticket_codes))
    # 三座两码
    t4 = mk("order.ticketed", version=1,
            actualSeats=["6排6座", "6排8座", "6排4座"],
            tickets=[{"seatNo": "", "ticketCode": "CODE1"},
                     {"seatNo": "", "ticketCode": "CODE2"}])
    check("★ 三座两码：ticket_codes 只有 2 个", len(t4.ticket_codes) == 2)
    check("★ 入场座位只看 actualSeats（有 3 个）", len(t4.actual_seats) == 3,
          str(t4.actual_seats))
    check("★ 票码张数与座位数对不上时不猜绑定", t4.ticket_codes != t4.actual_seats)

    # version=0 空票根要回查
    t5 = mk("order.ticketed", version=0, tickets=[])
    check("★ tickets 为空 → 需要回查订单详情", t5.needs_order_detail_lookup())
    check("有票根时仍建议回查（订单回调不含影院影片厅名）",
          t1.needs_order_detail_lookup())

    section("十六、seatChanged 三态")
    check("True", mk("order.ticketed", seatChanged=True).seat_changed is True)
    check("False", mk("order.ticketed", seatChanged=False).seat_changed is False)
    check("★ None 保持 None（不能当未换座）",
          mk("order.ticketed", seatChanged=None).seat_changed is None)

    section("十七、回调幂等去重")
    seen_events = set()
    ev_a, _ = cb.handle(key, secret, headers, body, now=1700000000,
                        seen_events=seen_events)
    ts2, nonce2 = "1700000001", "n2"
    sign2 = sign_body(key, secret, ts2, nonce2, body)
    h2 = dict(headers, **{"X-Timestamp": ts2, "X-Nonce": nonce2, "X-Sign": sign2})
    ev_b, ack_b = cb.handle(key, secret, h2, body, now=1700000001,
                            seen_events=seen_events)
    check("首次处理返回事件", ev_a is not None)
    check("★ 同一事件重推 → 不再重复处理", ev_b is None)
    check("★ 但应答仍是 {\"code\":0}（让平台别再推）", ack_b == '{"code":0}', ack_b)

    section("十八、未知事件不该让平台无限重推")
    body_u = json.dumps({"event": "order.something.new", "data": {"orderNo": "LP1"}},
                        ensure_ascii=False)
    su = sign_body(key, secret, ts, nonce, body_u)
    hu = dict(headers, **{"X-Sign": su})
    ev_u, ack_u = cb.handle(key, secret, hu, body_u, now=1700000000)
    check("★ 未知事件：不返回事件但回 0", ev_u is None and ack_u == '{"code":0}',
          ack_u)
    check("parse 不因未知事件抛异常", cb.parse(body_u).event == "order.something.new")

    section("十九、其他回调事件")
    s1 = mk("order.settled", freezeAmount="2792", settleAmount="2551", refundAmount="241")
    check("settled 金额转分",
          (s1.freeze_amount_fen, s1.settle_amount_fen) == (2792, 2551))
    check("★ freeze − settle = refund",
          s1.freeze_amount_fen - s1.settle_amount_fen ==
          cb._to_int(s1.data["refundAmount"]))
    s2 = mk("order.refunded", feeAmount="0", refundAmount="7020")
    check("refunded 金额转分", cb._to_int(s2.data["refundAmount"]) == 7020)
    s3 = mk("order.refund_rejected", refundNo="RF1", failReason="影院不支持退票")
    check("refund_rejected 解析", s3.data["refundNo"] == "RF1")

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
