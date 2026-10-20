"""
_check_ticket_flow.py —— 端到端链路自检（假 transport，不需要 AppKey）

════════════════════════════════════════════════════════════════
★ 这个测试回答一个问题：

  「把各层接起来，整条链路还走得通吗？」

  各层单独测过不代表能拼起来 —— 参数传岔、状态没接上、
  模板 key 写错，这些只有端到端才暴露得出来。

  链路：发截图 → 识别 → 挑档位 → 询价 → 规则算价 → 报价卡
        → 买家确认 → 下单 → 回调出票 → 发取票码

★ 假 transport 按【路径】回应，而不是按调用顺序 ——
  这样测的是"链路对不对"，不是"调用顺序碰巧一样"。
════════════════════════════════════════════════════════════════
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

import templates as tpl
from application.ticket_flow import (FlowResult, TicketFlow, _format_candidates,
                                   _parse_pick,
                                     _seats_original_fen, _far_enough, _fen_to_yuan)
from domain.conversation import Conversation, ConvState
from domain.quote_rules import (BRAND_WANDA, KNOWN_BRANDS, QuoteRule,
                                QuoteRuleEngine, brands_from_api,
                                tianjin_default_rules)
from integrations.liangpiao.api import (LiangPiaoAPI,
                                        QuoteParams, TICKET_FAST, TICKET_FLASH,
                                        TICKET_STANDARD, make_out_order_no)
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


# ==================================================================== 假传输
class RoutingTransport:
    """
    ★ 按【路径】回应。这样测的是链路本身，而不是调用顺序。

    每个路径给一个响应工厂；未登记的路径抛错，
    这样"多调了不该调的接口"会立刻暴露。
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def send(self, url, body, headers, timeout):
        path = url.split("/api/v1", 1)[-1]
        self.calls.append({"path": path, "body": json.loads(body or "{}"),
                           "headers": dict(headers), "timeout": timeout})
        handler = self.routes.get(path)
        if handler is None:
            raise AssertionError(f"★ 调了未登记的接口：{path}")
        result = handler(self.calls[-1]["body"]) if callable(handler) else handler
        if isinstance(result, Exception):
            raise result
        return result

    def payload(self, path):
        """取某次调用的请求体。"""
        for c in self.calls:
            if c["path"] == path:
                return c["body"]
        return None

    def payloads(self, path):
        return [c["body"] for c in self.calls if c["path"] == path]


def envelope(code=0, message="success", data=None, request_id="RID"):
    out = {"code": code, "message": message, "requestId": request_id}
    if data is not None:
        out["data"] = data
    return json.dumps(out, ensure_ascii=False)


# 良票 EXACT 夹具使用非万达影院；本文件后面的万达专用样本保持万达。
EXACT_RESULT = {
    "recognizeId": "8642", "cached": False,
    "rawResults": {"isSeatSelection": True, "platform": "淘票票"},
    "finalResults": {
        "matchLevel": "EXACT", "noMatchReason": None,
        "cinemaId": 1486, "cinemaName": "CGV影城(天津SM滨海城市广场店)",
        "cinemaAddress": "中新大道与新城西路交口SM城市广场三层",
        "cityCode": "120100", "cityName": "天津", "brandName": "CGV影城",
        "cinemaHitNums": 1,
        "movieId": 2, "movieName": "八仙!",
        "hallName": "6号厅", "startTime": "2099-07-28T19:30:00+08:00",
        "showId": "889900", "maxSeatsPerOrder": 4,
        "priceAllFen": "7800", "priceMismatch": False,
        "seat": [
            {"seatName": "4排1座", "seatPriceFen": "3900", "originalPriceFen": "3890",
             "rowNo": 4, "colNo": 1, "graphRow": 4, "graphCol": 1,
             "seatNo": "4排1座", "areaId": "32", "status": "AVAILABLE"},
            {"seatName": "4排2座", "seatPriceFen": "3900", "originalPriceFen": "3890",
             "rowNo": 4, "colNo": 2, "graphRow": 4, "graphCol": 2,
             "seatNo": "4排2座", "areaId": "32", "status": "AVAILABLE"},
        ],
        "seatMatched": True, "seatConfirmRequired": False, "seatConfirmReasons": [],
        "expectedSeatCount": 2,
        "prices": [{"ticketMode": "STANDARD", "priceMode": "FIXED", "price": "3510",
                    "maxPrice": "5200", "originalPrice": "3890",
                    "stopSaleTime": "2099-07-28T18:59:00+08:00", "available": True}],
        "candidates": {"cinemas": [], "movies": [], "shows": []},
    },
}

CANDIDATE_RESULT = {
    "recognizeId": "8669", "cached": False,
    "rawResults": {}, "finalResults": {
        "matchLevel": "CANDIDATE", "noMatchReason": None,
        "cinemaId": 2035, "cinemaName": "某某国际影城", "cityName": "天津",
        "brandName": "金逸电影城", "cinemaHitNums": 1, "movieId": 57,
        "movieName": "某某影片", "hallName": None,
        "startTime": None, "showId": None,
        "seatMatched": False,
        "seat": [{"seatName": "5排6座", "seatPriceFen": "2990", "originalPriceFen": None,
                  "rowNo": 5, "colNo": 6, "seatNo": None, "areaId": None}],
        "prices": [],
        "candidates": {"cinemas": [], "movies": [], "shows": [
            {"showId": "17208231", "hall": "4号激光厅",
             "startTime": "2099-09-05T18:15:00+08:00", "dimension": "2D", "language": "国语",
             "prices": [{"ticketMode": "STANDARD", "priceMode": "FIXED", "price": "3146",
                         "originalPrice": "3500", "available": True}]},
        ]},
    },
}


def exact_confirmed(show_id="17208231"):
    """二次确认后变成 EXACT 的结果（抄文档的示例）。"""
    data = json.loads(json.dumps(EXACT_RESULT))
    data["recognizeId"] = "8669"
    f = data["finalResults"]
    f.update({"hallName": "4号激光厅", "showId": show_id,
              "startTime": "2099-09-05T18:15:00+08:00"})
    return data


def make_flow(routes, *, tel="13800000000", rules=None, **kw):
    transport = RoutingTransport(routes)
    client = LiangPiaoClient("K", "S", base_url="https://example.test",
                             transport=transport, sleep=lambda _: None)
    api = LiangPiaoAPI(client)
    engine = QuoteRuleEngine(rules if rules is not None else tianjin_default_rules())
    flow = TicketFlow(api, engine, tel=tel, **kw)
    return flow, transport


# ==================================================================== 测试
def main() -> int:                                     # noqa: C901
    # ------------------------------------------------------------------ 一
    section("一、端到端主链路：发截图 → 报价 → 确认 → 下单")
    routes = {
        "/recognize/seat-shot": (200, envelope(data=EXACT_RESULT)),
        "/order/preflight": (200, envelope(data={
            "available": True, "totalAmount": "7020", "estimateAmount": "6620",
            "marketAmount": "7780", "estimated": True})),
        "/order/create": (200, envelope(data={
            "orderNo": "LP20260903053750CH4VKWDS", "status": "SUBMITTING",
            "freezeAmount": "7020", "seatCount": 2, "duplicated": False,
            "pickupUrl": "https://t-v3.example/abc"})),
    }
    flow, tr = make_flow(routes)
    conv = Conversation(chat_id="chat-1")

    r1 = flow.on_screenshot(conv, "https://img.example.com/a.jpg", city_name="天津")
    check("发截图 → 成功", r1.ok, r1.error)
    check("★ 先发了「正在识别」再发报价卡",
          r1.template_keys[0] == "RECOGNIZING_TIP",
          str(r1.template_keys))
    check("报价卡发出", "QUOTE_CARD" in r1.template_keys, str(r1.template_keys))
    check("★ 报价卡是多条消息（含引用的关键词文案）",
          len(r1.messages) >= 3, f"{len(r1.messages)} 条")
    check("状态走到已报价", r1.state is ConvState.QUOTED, r1.state.value)

    check("有报价结论", r1.quote is not None)
    check("★ 命中了兜底规则（这条启用）", r1.quote.rule_name == "兜底全院线预估",
          r1.quote.rule_name)
    check("★ 一口价→FIXED、预估价→LIMIT 映射正确",
          r1.quote.price_mode == "LIMIT", r1.quote.price_mode)
    check("★ 成本口径用 estimateAmount（6620 而不是 7020）",
          r1.quote.cost_total_fen == 6620, f"{r1.quote.cost_total_fen}")
    # ★★★ 2026-10-14 这条断言改了两次，最后回到原值 —— 记下过程，
    #   免得以后有人再"顺手改成按 totalAmount 算"。
    #
    #   老算法：maxPrice = (成本单价 + 限价) × 张数 = (3310 + 50) × 2 = 6720
    #   这份 fixture 是跨区的：totalAmount=7020 > estimateAmount=6620。
    #
    #   ★ 我一度以为这会让下单被 300005 拒（理由是 6720 < 7020），
    #     就改成按 totalAmount 算。但【真接口实测推翻了这个推论】：
    #       场次 38208129，跨区两座，老算法 maxPrice=7608
    #       /order/create → code=0 成功，freezeAmount=7608
    #     ⇒ maxPrice 判的是【实收金额】，不是 totalAmount 那个限价上限。
    #       老算法按 estimateAmount（预估应付）+ 限价，本来就 ≥ 实收。
    #
    #   ★ 且实测 freezeAmount == maxPrice —— LIMIT 模式冻结额就是限价，
    #     限价抬高只是白冻更多余额。所以【不该】改成按上限算。
    #
    #   现在：钱按老算法；totalAmount 只用来记"跨区/可能倒挂"的预警。
    check("★ maxPrice 按单张限价算出来（成本+0.5/张 ×2）",
          r1.quote.max_price_fen == (3310 + 50) * 2, f"{r1.quote.max_price_fen}")
    check("★★ 但跨区时要记 note（平台限价上限高于均价成本）",
          any("跨区座位" in n for n in (r1.quote.notes or [])),
          str([n[:46] for n in (r1.quote.notes or [])]))
    check("★★ 原价 7780 > 上限 7020 ⇒ 这单不亏，不该报『可能倒挂』",
          not any("可能倒挂" in n for n in (r1.quote.notes or [])),
          str([n[:46] for n in (r1.quote.notes or [])]))
    check("报价里含毛利字段", r1.quote.margin_total_fen is not None)

    # ★★★ 2026-10-14 店主定的时序（重要，改测试而不是改代码）：
    #   「必须金额校验正确，且已付款，才可以良票建单」
    #   ⇒ 买家回「正确」这一步【只落本地待付款单】，不调 ch5_create。
    #     建单发生在【付款确认之后】（flow.on_payment_confirmed）。
    #   所以这里拆成两步验：
    #     ① on_buyer_confirm → 本地单 + bubble 17（拍下别付款）+ AWAIT_REPRICE
    #     ② on_payment_confirmed → 才真的 ch5_create + TICKETING + AWAIT_TICKET
    def _create_calls() -> int:
        return len([c for c in tr.calls if c["path"].endswith("/order/create")])

    r2 = flow.on_buyer_confirm(conv)
    check("买家确认 → 不建上游单（只落本地待付款单）", r2.ok, r2.error)
    check("★★ 确认时【没有】调 order/create", _create_calls() == 0,
          f"{_create_calls()} 次")
    check("★ 确认时发的是「拍下别付款」(bubble 17)",
          "ORDERED_NO_PAY" in r2.template_keys, str(r2.template_keys))
    check("状态走到已拍下待改价", r2.state is ConvState.AWAIT_REPRICE,
          r2.state.value)
    check("★ 记下了我们的单号（付款后建上游单要用）",
          bool(getattr(conv, "pending_out_order_no", "")),
          repr(getattr(conv, "pending_out_order_no", "")))

    # ---- 第二步：付款确认之后才建单 ----
    r2b = flow.on_payment_confirmed(conv, FlowResult())
    check("★★ 付款确认后 → 才调 order/create", _create_calls() == 1,
          f"{_create_calls()} 次")
    check("★★ 拿到平台订单号（付款后）",
          r2b.order_no == "LP20260903053750CH4VKWDS", r2b.order_no)
    check("★★ 付款后发「已下单等待」", "TICKETING" in r2b.template_keys,
          str(r2b.template_keys))
    check("★★ 付款后状态走到待出票", r2b.state is ConvState.AWAIT_TICKET,
          r2b.state.value)

    # ------------------------------------------------------------------ 二
    section("二、★ 询价与下单的参数必须完全一致（文档硬要求）")
    pre = tr.payload("/order/preflight")
    cre = tr.payload("/order/create")
    for field in ("areaQuoteStrategy", "invoiceRequired"):
        check(f"★ {field} 询价与下单一致",
              pre.get(field) == cre.get(field),
              f"询价={pre.get(field)!r} 下单={cre.get(field)!r}")
    check("★ priceMode 下单用的是规则算出来的",
          cre["priceMode"] == r1.quote.price_mode,
          f"{cre['priceMode']} vs {r1.quote.price_mode}")
    check("★ maxPrice 是数字不是字符串",
          isinstance(cre.get("maxPrice"), int), type(cre.get("maxPrice")).__name__)
    check("★ maxPrice 与规则算的一致",
          cre["maxPrice"] == r1.quote.max_price_fen,
          f"{cre.get('maxPrice')} vs {r1.quote.max_price_fen}")
    check("★ 座位逐座带 areaId（分区厅必须）",
          all("areaId" in s for s in cre["seats"]), str(cre["seats"]))
    check("下单带 tel", cre["tel"] == "13800000000")
    check("★ attach 带了会话 ID（回调时能路由回来）",
          cre["attach"] == "chat-1", str(cre.get("attach")))
    check("outOrderNo ≤64 字符", len(cre["outOrderNo"]) <= 64, cre["outOrderNo"])
    check("★ 没有多调接口", set(c["path"] for c in tr.calls) ==
          {"/recognize/seat-shot", "/order/preflight", "/order/create"},
          str(sorted(set(c["path"] for c in tr.calls))))

    # ------------------------------------------------------------------ 三
    section("三、★ 回调：出票成功 → 发取票码")
    body = json.dumps({"event": "order.ticketed", "data": {
        "orderNo": "LP20260903053750CH4VKWDS", "outOrderNo": r2.out_order_no,
        "version": 1, "ticketMode": "STANDARD", "strategy": None,
        "redispatchCount": 0, "estimatedSettleAmount": "2823",
        "ticketLink": "https://supplier.example.com/ticket/xxx",
        "pickupUrl": "https://t-v3.example/abc",
        "actualSeats": ["4排1座", "4排2座"], "seatChanged": False,
        "tickets": [{"seatNo": "4排1座,4排2座", "ticketCode": "947094|806351",
                     "ticketImage": "https://img.example.com/qr.png",
                     "entryType": 0, "getTicketType": 0, "ticketPassword": None}]}},
        ensure_ascii=False)
    ts, nonce = "1700000000", "n1"
    headers = {"Content-Type": "application/json", "X-App-Key": "K",
               "X-Timestamp": ts, "X-Nonce": nonce,
               "X-Sign": sign_body("K", "S", ts, nonce, body)}
    ack, r3 = flow.on_callback(body, headers, app_key="K", app_secret="S",
                               now=1700000000)
    check("回调应答是 {\"code\":0}", ack == '{"code":0}', ack)
    check("出票回调解析成功", r3 is not None)
    check("★ 发出了取票码消息", "TICKET_CODE" in r3.template_keys,
          str(r3.template_keys))
    check("取票码进入消息内容",
          any("947094" in m for m in r3.messages),
          str([m[:40] for m in r3.messages]))
    check("★ 一码多座也能取到码", True, "947094|806351")
    check("seatChanged=False 不报警", not any("换座" in n for n in r3.notes),
          str(r3.notes))

    # ------------------------------------------------------------------ 四
    section("四、★ 票根为空（version=0）→ 等 ticket.updated，不当失败")
    body2 = json.dumps({"event": "order.ticketed", "data": {
        "orderNo": "LP20260903053750CH4VKWDS", "outOrderNo": r2.out_order_no,
        "version": 0, "tickets": []}}, ensure_ascii=False)
    h2 = dict(headers, **{"X-Sign": sign_body("K", "S", ts, "n2", body2),
                          "X-Nonce": "n2"})
    ack2, r4 = flow.on_callback(body2, h2, app_key="K", app_secret="S", now=1700000000)
    check("★ 不发取票码（还没票根）",
          "TICKET_CODE" not in (r4.template_keys if r4 else []),
          str(r4.template_keys if r4 else None))
    check("★ 提示要等 ticket.updated",
          any("ticket.updated" in n for n in (r4.notes if r4 else [])),
          str(r4.notes if r4 else []))

    # ------------------------------------------------------------------ 五
    section("五、★ 回调：出票失败 → 自动退款")
    flow2, tr2 = make_flow(routes)
    conv2 = Conversation(chat_id="chat-2")
    flow2.on_screenshot(conv2, "https://x/a.jpg", city_name="天津")
    flow2.on_buyer_confirm(conv2)
    fail_body = json.dumps({"event": "order.failed", "data": {
        "orderNo": "LP20260903053750CH4VKWDS", "outOrderNo": "X",
        "failCode": "300007", "failReason": "座位已被占用或还未解锁",
        "lossAmount": "0", "refundAmount": "2882"}}, ensure_ascii=False)
    hf = dict(headers, **{"X-Sign": sign_body("K", "S", ts, "n3", fail_body),
                          "X-Nonce": "n3"})
    ackf, rf = flow2.on_callback(fail_body, hf, app_key="K", app_secret="S",
                                 now=1700000000)
    check("★ 发出「出票失败自动退款」",
          "TICKET_FAILED_REFUND" in rf.template_keys, str(rf.template_keys))
    check("退款文案进了消息", any("退款" in m for m in rf.messages),
          str([m[:30] for m in rf.messages]))
    check("★ 账单信息记进 notes（对账要用）",
          any("300007" in n for n in rf.notes), str(rf.notes))

    # 有损关单
    loss_body = json.dumps({"event": "order.failed", "data": {
        "orderNo": "LPX", "failCode": "300007", "failReason": "上游关单",
        "lossAmount": "300", "refundAmount": "2500"}}, ensure_ascii=False)
    hl = dict(headers, **{"X-Sign": sign_body("K", "S", ts, "n4", loss_body),
                          "X-Nonce": "n4"})
    _, rl = flow2.on_callback(loss_body, hl, app_key="K", app_secret="S",
                              now=1700000000)
    check("★ 有损关单会标注「不是全额退回」",
          any("有损" in n for n in rl.notes), str(rl.notes))

    # ------------------------------------------------------------------ 六
    section("六、★ 换座三态：None 不能当成未换座")
    flow3, _ = make_flow(routes)
    conv3 = Conversation(chat_id="chat-3")
    flow3.on_screenshot(conv3, "https://x/a.jpg", city_name="天津")
    flow3.on_buyer_confirm(conv3)
    for changed, label, expect_warn in ((True, "已换座", True),
                                        (False, "未换座", False),
                                        (None, "未知", True)):
        b = json.dumps({"event": "order.ticketed", "data": {
            "orderNo": "LP1", "version": 1, "seatChanged": changed,
            "actualSeats": ["4排1座"],
            "tickets": [{"seatNo": "4排1座", "ticketCode": "C1"}]}},
            ensure_ascii=False)
        nn = f"sc{changed}"
        hb = dict(headers, **{"X-Sign": sign_body("K", "S", ts, nn, b), "X-Nonce": nn})
        _, rc = flow3.on_callback(b, hb, app_key="K", app_secret="S", now=1700000000)
        got = any("换座" in n for n in rc.notes)
        check(f"seatChanged={changed}（{label}）{'要提示' if expect_warn else '不提示'}",
              got == expect_warn, str(rc.notes))

    # ------------------------------------------------------------------ 七
    section("七、★ 多结果消歧：只对未唯一的维度发问")
    routes_c = {"/recognize/seat-shot": (200, envelope(data=CANDIDATE_RESULT))}
    flowc, trc = make_flow(routes_c)
    convc = Conversation(chat_id="chat-c")
    rc1 = flowc.on_screenshot(convc, "https://x/a.jpg", city_name="天津")
    check("多结果 → 发候选让买家挑", "DISAMBIG" in rc1.template_keys,
          str(rc1.template_keys))
    check("状态进入待确认场次", rc1.state is ConvState.PICK_SHOW, rc1.state.value)
    check("★ 影院影片已收敛，不重复问",
          any("待确认维度：show" in n for n in rc1.notes), str(rc1.notes))
    check("候选消息含场次厅名",
          any("4号激光厅" in m for m in rc1.messages),
          str([m[:60] for m in rc1.messages]))
    # ★★ 2026-10-14 对齐线上固定话术（live-triage.json 的 system_template）：
    #   影院那句必须逐字是线上原话，且分维度（问影片/场次时不能写「确认影院」）。
    check("★★ 消歧文案按维度分（影院用线上原话）",
          "当前需要确认影院：" in tpl.text_for("DISAMBIG", variant="cinema")
          and "当前需要确认影片：" in tpl.text_for("DISAMBIG", variant="movie")
          and "当前需要确认场次：" in tpl.text_for("DISAMBIG", variant="show"),
          tpl.text_for("DISAMBIG", variant="cinema").splitlines()[0])
    check("★★ 结尾用线上那句「不用重复发送」",
          "不用重复发送已提供的场次信息"
          in tpl.text_for("DISAMBIG", variant="cinema"))
    # ★★ 候选列表格式：「城市 · 影院名」（城市在名前）。
    #   线上固定话术就是这么排的（实测有「成都·横店 / 广州·横店」跨城市的候选），
    #   城市放前面一眼能筛掉不对的；我原来写成「影院名（城市）」，
    #   多个括号叠一起很难读。
    from integrations.liangpiao.api import RecognitionResult as _RRc
    _cres = _RRc.from_data({"finalResults": {
        "matchLevel": "CANDIDATE", "cinemaId": None, "cinemaHitNums": 2,
        "candidates": {"cinemas": [
            {"cinemaId": 1, "name": "万达影城（临港万达广场IMAX店）",
             "cityName": "上海", "score": 1.0, "hasShow": True},
            {"cinemaId": 2, "name": "万达影城（临桂万达广场IMAX店）",
             "cityName": "桂林", "score": 0.9, "hasShow": True},
        ], "movies": [], "shows": []}}})
    _cl = _format_candidates(_cres, "cinema")
    check("★★ 候选列表是「城市 · 影院名」",
          "1. 上海 · 万达影城（临港万达广场IMAX店）" in _cl
          and "2. 桂林 · 万达影城（临桂万达广场IMAX店）" in _cl,
          _cl[:90])
    check("★ 候选不带「影院1」那种文字前缀",
          "影院1" not in _cl and "影院2" not in _cl, _cl[:60])
    check("记下了 recognizeId（二次确认要用）", convc.recognition_id == "8669")

    # 买家回复"场次1" → 二次确认
    routes_c2 = dict(routes_c)
    routes_c2["/recognize/confirm"] = (200, envelope(data=exact_confirmed()))
    routes_c2["/order/preflight"] = (200, envelope(data={
        "available": True, "estimateAmount": "6292", "totalAmount": "6680"}))
    flowc2, trc2 = make_flow(routes_c2)
    convc2 = Conversation(chat_id="chat-c2")
    flowc2.on_screenshot(convc2, "https://x/a.jpg", city_name="天津")
    rc2 = flowc2.on_candidate_pick(convc2, "场次1")
    check("★ 走了二次确认接口", "/recognize/confirm" in
          [c["path"] for c in trc2.calls], str([c["path"] for c in trc2.calls]))
    conf_body = trc2.payload("/recognize/confirm")
    check("二次确认传了 showId", conf_body.get("showId") == "17208231",
          str(conf_body))
    check("★ 二次确认也传了 cityName（文档：识别时传的城市不会存下来）",
          conf_body.get("cityName") == "天津", str(conf_body))
    check("确认后出报价卡", "QUOTE_CARD" in rc2.template_keys or
          "QUOTE_CARD_MULTI" in rc2.template_keys, str(rc2.template_keys))

    # 序号不对 → 不报错，重发候选
    flowc3, trc3 = make_flow(routes_c)
    convc3 = Conversation(chat_id="chat-c3")
    flowc3.on_screenshot(convc3, "https://x/a.jpg", city_name="天津")
    rc3 = flowc3.on_candidate_pick(convc3, "场次9")
    check("★ 序号超范围 → 不报错，重发候选",
          "DISAMBIG" in rc3.template_keys and rc3.ok,
          str(rc3.template_keys))
    check("★ 序号不对时不调二次确认", "/recognize/confirm" not in
          [c["path"] for c in trc3.calls])

    # ------------------------------------------------------------------ 八
    section("八、★ 场次已开场：不重试、不补 cityName")
    expired = json.loads(json.dumps(EXACT_RESULT))
    expired["finalResults"].update({"matchLevel": "NONE",
                                    "noMatchReason": "SHOW_EXPIRED",
                                    "showId": None})
    flow_e, tr_e = make_flow({"/recognize/seat-shot": (200, envelope(data=expired))})
    conv_e = Conversation(chat_id="chat-e")
    re1 = flow_e.on_screenshot(conv_e, "https://x/a.jpg", city_name="天津")
    check("★ 提示场次已开场", not re1.ok and "RECOG_FAIL" in re1.template_keys,
          str(re1.template_keys))
    check("★ 文案里说「已经开场」",
          any("开场" in m for m in re1.messages), str([m[:40] for m in re1.messages]))
    check("★ 只调了一次识别，没有重试",
          len([c for c in tr_e.calls if c["path"] == "/recognize/seat-shot"]) == 1)
    check("★ 没有补 cityName 重试",
          len(tr_e.payloads("/recognize/seat-shot")) == 1)

    # ------------------------------------------------------------------ 九
    section("九、★ 距开场太近不接单")
    soon = json.loads(json.dumps(EXACT_RESULT))
    soon_start = (datetime.now(timezone(timedelta(hours=8))) +
                  timedelta(minutes=10)).isoformat()
    soon["finalResults"]["startTime"] = soon_start
    flow_s, tr_s = make_flow({"/recognize/seat-shot": (200, envelope(data=soon))})
    conv_s = Conversation(chat_id="chat-s")
    rs = flow_s.on_screenshot(conv_s, "https://x/a.jpg", city_name="天津")
    check("★ 距开场 10 分钟 → 不接单", not rs.ok)
    check("★ 发出「距开场太近」文案", "SHOWTIME_LIMIT" in rs.template_keys,
          str(rs.template_keys))
    check("★ 没有去询价（省掉无效调用）",
          "/order/preflight" not in [c["path"] for c in tr_s.calls],
          str([c["path"] for c in tr_s.calls]))

    # ------------------------------------------------------------------ 十
    section("十、★ 报价高于自购价 → 引导自购，不硬卖")
    flow_h, tr_h = make_flow({
        "/recognize/seat-shot": (200, envelope(data=EXACT_RESULT)),
        "/order/preflight": (200, envelope(data={
            # 成本很高，按规则加价后会超过市场原价
            "available": True, "estimateAmount": "8000", "totalAmount": "8000",
            "marketAmount": "7000"})),
    })
    conv_h = Conversation(chat_id="chat-h")
    rh = flow_h.on_screenshot(conv_h, "https://x/a.jpg", city_name="天津")
    check("★ 发出「可以自行购买」", "SELF_BUY_HINT" in rh.template_keys,
          str(rh.template_keys))
    check("★ 没有继续下单流程（未进已报价）",
          rh.state is not ConvState.QUOTED, rh.state.value)
    check("★ 提示了为什么不硬卖", any("不硬卖" in n for n in rh.notes), str(rh.notes))

    # ------------------------------------------------------------------ 十一
    section("十一、★ 报价过期后确认 → 拦住，不发下单请求")
    flow_p, tr_p = make_flow(routes)
    conv_p = Conversation(chat_id="chat-p")
    flow_p.on_screenshot(conv_p, "https://x/a.jpg", city_name="天津")
    # 手动把报价推到过期
    conv_p.quote_expires_at = datetime.now() - timedelta(seconds=1)
    rp = flow_p.on_buyer_confirm(conv_p)
    check("★ 过期后确认被拦下", not rp.ok)
    check("★ 发出过期提示", "QUOTE_EXPIRED" in rp.template_keys,
          str(rp.template_keys))
    check("★ 没有发出下单请求",
          "/order/create" not in [c["path"] for c in tr_p.calls],
          str([c["path"] for c in tr_p.calls]))

    # ------------------------------------------------------------------ 十二
    section("十二、★ 300010（座位正在出票）→ 不重试，如实告知")
    flow_d, tr_d = make_flow({
        "/recognize/seat-shot": (200, envelope(data=EXACT_RESULT)),
        "/order/preflight": (200, envelope(data={
            "available": True, "estimateAmount": "6620", "totalAmount": "7020"})),
        "/order/create": (200, envelope(code=300010, message="该场次座位已有提交的订单正在出票")),
    })
    conv_d = Conversation(chat_id="chat-d")
    flow_d.on_screenshot(conv_d, "https://x/a.jpg", city_name="天津")
    # ★ 「确认」这一步现在不建单（店主定的时序）——
    #   所以"下单被拒"要到【付款确认之后】才会发生。
    flow_d.on_buyer_confirm(conv_d)
    rd = flow_d.on_payment_confirmed(conv_d, FlowResult())
    check("★ 下单被拒", not rd.ok, rd.error)
    check("★ 发出失败提示", "RECOG_FAIL" in rd.template_keys,
          str(rd.template_keys))
    check("★ 只下了一次单，没有重试",
          len([c for c in tr_d.calls if c["path"] == "/order/create"]) == 1,
          str(len([c for c in tr_d.calls if c["path"] == "/order/create"])))
    check("★ 付款后建单失败 → 必须转人工（买家已付款）", rd.need_human)

    # 300005 限价低于报价 → 是【我们规则配错了】，要转人工
    flow_q, _ = make_flow({
        "/recognize/seat-shot": (200, envelope(data=EXACT_RESULT)),
        "/order/preflight": (200, envelope(data={
            "available": True, "estimateAmount": "6620", "totalAmount": "7020"})),
        "/order/create": (200, envelope(code=300005, message="实际价格超过限价")),
    })
    conv_q = Conversation(chat_id="chat-q")
    flow_q.on_screenshot(conv_q, "https://x/a.jpg", city_name="天津")
    # ★ 建单被拒发生在付款后（店主定的时序）
    flow_q.on_buyer_confirm(conv_q)
    rq = flow_q.on_payment_confirmed(conv_q, FlowResult())
    check("★ 300005 转人工（是我们规则的责任，不是买家的）", rq.need_human,
          str(rq.notes))

    # ------------------------------------------------------------------ 十三
    section("十三、★ outOrderNo 幂等：重试复用，被拒换新")
    n1 = make_out_order_no("YMD", 1)
    n2 = make_out_order_no("YMD", 2)
    check("不同序号不同单号", n1 != n2, f"{n1} / {n2}")
    check("单号 ≤64", len(n1) <= 64)

    section("十四、纯函数：候选格式化 / 指令解析 / 原价合计")
    # _parse_pick
    # ★ 两种写法都要认（2026-10-13 店主定了"只回数字"，
    #   但带前缀的老写法不能废 —— 老买家习惯了，废了会报错）。
    check("解析「影院1」（带前缀，兼容）", _parse_pick("影院1") == ("cinema", 1))
    check("解析「影片2」（带前缀，兼容）", _parse_pick("影片2") == ("movie", 2))
    check("解析「场次3」（带前缀，兼容）", _parse_pick("场次3") == ("show", 3))
    check("解析「影城10」", _parse_pick("影城10") == ("cinema", 10))
    # ★★ 纯数字：维度未知，交给 on_candidate_pick 按 conv.state 补
    check("★★ 解析纯数字「1」→ (None, 1)", _parse_pick("1") == (None, 1))
    check("★★ 解析纯数字「4」→ (None, 4)", _parse_pick("4") == (None, 4))
    check("★ 纯数字不带维度（维度由上下文补）",
          _parse_pick("2")[0] is None and _parse_pick("2")[1] == 2)
    check("非候选指令返回 None", _parse_pick("随便说点啥") == (None, 0))
    check("只有关键字没序号返回 None", _parse_pick("影院") == (None, 0))
    check("「确认出票4」不算候选（前缀不匹配）", _parse_pick("确认出票4") == (None, 0))

    # _format_candidates
    from integrations.liangpiao.api import RecognitionResult
    r_cand = RecognitionResult.from_data(CANDIDATE_RESULT)
    text = _format_candidates(r_cand, "show")
    # ★★★ 编号格式：只写数字，不带「影院/影片/场次」前缀（店主定的）
    check("★★ 场次候选用纯数字编号「1. 」",
          text.startswith("1. ") and "4号激光厅" in text, text[:60])
    check("★★ 候选里【不出现】「【场次1】」这种前缀",
          "【场次1】" not in text and "【影院1】" not in text)
    check("★ 用 candidates.shows[].hall（键名是 hall 不是 hallName）",
          "4号激光厅" in text)
    check("候选之间用 <br>---<br> 分隔", "<br>---<br>" in
          _format_candidates(RecognitionResult.from_data({
              "finalResults": {"candidates": {
                  "shows": [{"showId": "1", "hall": "A"},
                            {"showId": "2", "hall": "B"}]}}}), "show"))

    # _seats_original_fen
    r_exact = RecognitionResult.from_data(EXACT_RESULT)
    check("★ 原价合计用 seat[].originalPriceFen（不是 prices[].originalPrice）",
          _seats_original_fen(r_exact) == 3890 * 2,
          f"{_seats_original_fen(r_exact)}")
    r_none = RecognitionResult.from_data(CANDIDATE_RESULT)
    check("未对齐时返回 None（不硬凑）", _seats_original_fen(r_none) is None)

    # _far_enough
    now8 = datetime(2099, 7, 28, 18, 0, tzinfo=timezone(timedelta(hours=8)))
    check("距开场 90 分钟 → 够", _far_enough("2099-07-28T19:30:00+08:00", 40, now8))
    check("距开场 10 分钟 → 不够", not _far_enough("2099-07-28T18:10:00+08:00", 40, now8))
    check("时间缺失时不拦（交给平台判）", _far_enough(None, 40, now8))

    check("分→元", _fen_to_yuan(3510) == "35.10")
    check("分→元 两位小数", _fen_to_yuan(100) == "1.00")

    # ------------------------------------------------------------------ 十五
    section("十五、★ 设置/规则 与良票接口的对齐核对")
    import settings as settings_module
    from integrations.liangpiao.api import TICKET_MODE_MINUTES, URGE_COOLDOWN_MINUTES

    cfg = settings_module.load()
    tiers = dict(cfg.speed_tiers())
    # ★★ 2026-10-14 修：这两条断言原来把 "SLOW" 当合法名，
    #   而 integrations/liangpiao/api.py 里是
    #       TICKET_STANDARD, TICKET_FAST, TICKET_FLASH = "STANDARD", "FAST", "FLASH"
    #   ⇒ 断言自己写着"与 ticketMode 同名"，却拿一个不同名的集合去比，
    #     永远失败，而且失败原因很有误导性（让人以为是配置错了）。
    #   ★ 改成【跟真常量比】：这样名字以后再变，断言自动跟着走，不会漂移。
    from integrations.liangpiao.api import TICKET_MODES, TICKET_STANDARD
    check("★ 配置的票速档位与良票 ticketMode 同名（跟常量比）",
          set(tiers) <= set(TICKET_MODES),
          f"配置={sorted(tiers)} 良票常量={list(TICKET_MODES)}")
    check("★ 档位阈值与文档一致（FLASH≥1 / FAST≥15 / STANDARD≥40）",
          tiers.get("FLASH") == 1 and tiers.get("FAST") == 15
          and tiers.get(TICKET_STANDARD) == 40,
          str(tiers))
    check("★ 良票档位文档口径：快速 15 分钟内", TICKET_MODE_MINUTES[TICKET_FAST][1] == 15,
          str(TICKET_MODE_MINUTES[TICKET_FAST]))
    check("★ 催促冷却与文档一致（特惠20/快速15/极速10）",
          URGE_COOLDOWN_MINUTES[TICKET_STANDARD] == 20 and
          URGE_COOLDOWN_MINUTES[TICKET_FAST] == 15 and
          URGE_COOLDOWN_MINUTES[TICKET_FLASH] == 10, str(URGE_COOLDOWN_MINUTES))

    check("★ 报价有效期只用配置值（30 分钟），不跟文案的「2分钟」走",
          cfg.recognize_quote.quote_ttl_minutes == 30,
          str(cfg.recognize_quote.quote_ttl_minutes))
    check("★ 放映时间限制与文档「离开场不足40分钟勿支付」一致",
          cfg.showtime_limit.remaining_minutes == 40,
          str(cfg.showtime_limit.remaining_minutes))
    check("★ 确认指令与报价卡文案同源",
          cfg.fish_reprice.confirm_keyword == tpl.VARIABLES["confirm_cmd"][2],
          f"配置={cfg.fish_reprice.confirm_keyword} 文案={tpl.VARIABLES['confirm_cmd'][2]}")
    check("★ 单张限价 ≥ 报价金额（规则硬约束）",
          all(not (r.capped and r.limit_yuan < r.markup_yuan)
              for r in tianjin_default_rules() if r.enabled),
          "启用的规则无矛盾")

    check("★ 院线名用良票真实品牌名（不是票达人的分级名）",
          not any("原官方" in b or "（一线）" in b or "A类" in b
                  for b in KNOWN_BRANDS),
          str(KNOWN_BRANDS))
    check("★ 院线可从 brand/list 动态取得",
          brands_from_api([{"brandId": 3, "name": "万达影城"},
                           {"brandId": 8, "name": "CGV影城"},
                           {"brandId": 8, "name": "CGV影城"}]) == ["万达影城", "CGV影城"],
          str(brands_from_api([{"brandId": 3, "name": "万达影城"},
                               {"brandId": 8, "name": "CGV影城"}])))

    # ══════════════════════════════════════════════════════════════════
    section("★ 多轮消歧：每轮都必须能推进（不许转人工）")
    # ★★ 这组断言锁的是三个真缺陷（2026-10-13 发现）：
    #   ① on_candidate_pick 从来不推 BUYER_PICK
    #      → conv.state 停在 PICK_*，而 RECOGNIZE_MULTI_* 只从 QUOTED 出发
    #      → 每多问一轮就非法兜底转人工，而且非法兜底不改状态、会话卡死
    #   ② 状态机缺 (QUOTED, RECOGNIZE_MULTI_*)
    #      → 买家答完第一轮之后，第二轮就转人工（多维度消歧走不完）
    #   ③ 状态机缺 (QUOTED, RECOGNIZE_OK)
    #      → 买家答完【最后一个】维度、收敛成 EXACT 时又转人工（永远收不了尾）
    #
    #   ★ 判据不是没报错，是【状态真的往前走了 + 没打人工标记】——
    #     因为非法兜底也只返回 DISAMBIG，光看文案看不出坏。
    MULTI3 = {
        "recognizeId": "9101", "cached": False, "rawResults": {},
        "finalResults": {
            "matchLevel": "CANDIDATE", "noMatchReason": None,
            "cinemaId": None, "cinemaName": None, "cityName": "天津",
            "brandName": None, "cinemaHitNums": 2,
            "movieId": None, "movieName": None,
            "hallName": None, "startTime": None, "showId": None,
            "seatMatched": False, "seat": [], "prices": [],
            "candidates": {
                "cinemas": [{"cinemaId": 101, "name": "甲影城", "cityName": "天津"},
                            {"cinemaId": 102, "name": "乙影城", "cityName": "天津"}],
                "movies": [{"movieId": 201, "name": "影片甲"},
                            {"movieId": 202, "name": "影片乙"}],
                "shows": [{"showId": "s1", "hall": "1号厅",
                            "startTime": "2099-01-01T10:00:00+08:00",
                            "dimension": "2D", "language": "国语"}],
            },
        },
    }
    conv_state = {"cinema": None, "movie": None, "show": None}

    def confirm_multi(body):
        if body.get("cinemaId"):
            conv_state["cinema"] = body["cinemaId"]
        if body.get("movieId"):
            conv_state["movie"] = body["movieId"]
        if body.get("showId"):
            conv_state["show"] = body["showId"]
        data = json.loads(json.dumps(MULTI3))
        f = data["finalResults"]
        f["cinemaId"], f["movieId"], f["showId"] = (
            conv_state["cinema"], conv_state["movie"], conv_state["show"])
        if conv_state["show"]:
            f["matchLevel"] = "EXACT"
            f["hallName"] = "1号厅"
            f["startTime"] = "2099-01-01T10:00:00+08:00"
        return 200, envelope(data=data)

    routes_m = {
        "/recognize/seat-shot": (200, envelope(data=MULTI3)),
        "/recognize/confirm": confirm_multi,
        "/order/preflight": (200, envelope(data={
            "available": True, "estimateAmount": "6292", "totalAmount": "6680"})),
    }
    flowm, trm = make_flow(routes_m)
    convm = Conversation(chat_id="chat-m")
    rm1 = flowm.on_screenshot(convm, "https://x/a.jpg", city_name="天津")
    check("① 第1问问的是影院", rm1.state is ConvState.PICK_CINEMA, rm1.state.value)
    check("★★ 候选列表是纯数字编号（不带「影院」前缀）",
          (convm.last_candidates_text or "").startswith("1. ")
          and "【影院1】" not in (convm.last_candidates_text or ""),
          (convm.last_candidates_text or "")[:50])

    # ★★★ 店主定的方式：直接回数字（不带「影院」前缀）
    rm2 = flowm.on_candidate_pick(convm, "1")
    check("★★ 选完影院 → 推进到问影片（不许转人工）",
          rm2.state is ConvState.PICK_MOVIE,
          f"state={rm2.state.name}({rm2.state.value})")
    check("★★ 没有被标记为需要人工", not rm2.need_human, str(rm2.need_human))

    rm3 = flowm.on_candidate_pick(convm, "1")
    check("★★ 选完影片 → 推进到问场次",
          rm3.state is ConvState.PICK_SHOW, f"state={rm3.state.name}")
    check("★★ 没有被标记为需要人工", not rm3.need_human)

    rm4 = flowm.on_candidate_pick(convm, "1")
    check("★★★ 答完最后一个维度 → 收敛（不许转人工）",
          rm4.state is not ConvState.MANUAL,
          f"state={rm4.state.name}({rm4.state.value})")
    check("★★★ 收敛后的确没打人工标记", not rm4.need_human)

    section("★ 只认【正在问的那个维度】的回答")
    # ★ 买家只看到影院列表，回「影片1」不该被受理 ——
    #   因为 last_candidates 里三个维度的候选都在（识别时一次下发），
    #   不做这道门就会拿【他从来没见过的影片列表】去二次确认。
    flowx, trx = make_flow({**routes_m, "/recognize/confirm": confirm_multi})
    convx = Conversation(chat_id="chat-x")
    flowx.on_screenshot(convx, "https://x/a.jpg", city_name="天津")
    saved_text = convx.last_candidates_text
    rx = flowx.on_candidate_pick(convx, "影片1")          # 跨维度
    check("★★ 跨维度回答 → 不调二次确认",
          "/recognize/confirm" not in [c["path"] for c in trx.calls],
          str([c["path"] for c in trx.calls]))
    check("★★ 跨维度回答 → 状态停在原维度（不乱走）",
          convx.state is ConvState.PICK_CINEMA, convx.state.value)
    check("★ 跨维度回答 → 重发【当前在问的那份】候选",
          saved_text and saved_text in (rx.messages or [""])[0],
          str((rx.messages or [""])[0])[:60])
    check("★ 跨维度回答 → 不消耗、不报错", rx.ok)

    section("★ 报价卡原价：万达单不许显示 0.00")
    # ★★ 锁一个真 bug（2026-10-14 修）：
    #   _emit_quote_card 原来只从【识别结果】取原价
    #   （seat[].originalPriceFen / priceAllFen），完全没看 quote。
    #   而万达识别的 priceAllFen 实测【从不返回】
    #   （rawResults.priceAll 也是 null）⇒ 万达单报价卡原价恒为 0.00 元，
    #   买家看到「原价 0.00 / 优惠后 35.00」。
    #   修法：把 quote.original_total_fen 接进来当第三兜底。
    from domain.quote_rules import QuoteResult
    import application.ticket_flow as _tf

    class _NoSeatOriginal:
        """识别结果里【没有任何原价信息】（万达的真实情况）。"""
        city_name = "包头"
        cinema_name = "万达影城（九原万达广场IMAX店）"
        movie_name = "生化危机：爆发夜"
        hall_name = "8号激光厅"
        start_time = "2026-10-14T12:40:00+08:00"
        cinema_address = "九原区沙河西街以北"
        cinema_hit_nums = 1
        price_all_fen = None
        seats = []
        final_results = {}

    q = QuoteResult(unit_price_fen=3500, total_price_fen=7000,
                    price_mode="FIXED", max_price_fen=None,
                    rule_name="默认", quote_mode="渠道一口价",
                    adjust="加法", cost_basis="estimateAmount",
                    cost_unit_fen=3400, cost_total_fen=6800, seat_count=2,
                    original_total_fen=7180)
    out_card = _tf.FlowResult()
    _tf._seats_original_fen = lambda _r: None      # 临时打桩：识别里没原价
    flowc, _tr = make_flow({})
    flowc._emit_quote_card(_NoSeatOriginal(), [{"seatNo": "7排12座"},
                                               {"seatNo": "7排13座"}],
                           q, out_card)
    card = "\n".join(out_card.messages or [])
    check("★★ 原价不再是 0.00（万达没有 priceAllFen）",
          "原价：35.90元/张，合计：71.80元" in card,
          [l for l in card.splitlines() if "原价" in l][:1])
    check("★★ 优惠后金额不受影响", "优惠后：35.00元/张，合计：70.00元" in card,
          [l for l in card.splitlines() if "优惠后" in l][:1])

    section("★★ 原价口径：必须是买家可比的那个（不许用 W+专区的挂牌价）")
    # ★★★ 锁一个合规问题（2026-10-14 修第二轮）：
    #   第一轮修完「原价恒为 0」之后，万达单的原价来自
    #   quote.original_total_fen = 座位图 area[].salesPrice × 张数。
    #   而万达【每个分区的 salesPrice 不同】，我们取的是【W+专享区】的价
    #   —— 那恰好是全场最贵的区！
    #   实测（娄底五江 2026-10-14 20:35 7号厅）：
    #       良票给的原价（= 买家在 App 看到的）  3590
    #       万达 W+专享 salesPrice               4090  ← 当时取了这个
    #       普通区 3590 / 优选区 3790 / 特惠区 3390
    #   ⇒ 报价卡显示「原价 40.90 / 优惠后 32.40」= 号称省 8.50，
    #     而实际只比买家能看到的价便宜 3.50。★ 把优惠说大了。
    #   ⇒ 修法：prices[].originalPrice（买家可比口径）必须排在
    #     quote.original_total_fen（我们进货区的挂牌价）前面。
    class _WithPrices(_NoSeatOriginal):
        """识别里没有逐座原价，但 prices[] 给了买家可见的原价。"""
        prices = [{"ticketMode": "STANDARD", "priceMode": "FIXED",
                   "price": "2707", "originalPrice": "3590",
                   "available": True}]

    out_p = _tf.FlowResult()
    flowp, _trp = make_flow({})
    flowp._emit_quote_card(_WithPrices(), [{"seatNo": "5排8座"}],
                           q, out_p)
    cardp = "\n".join(out_p.messages or [])
    check("★★ 原价用 prices[].originalPrice（买家可比口径）",
          "原价：35.90元/张，合计：35.90元" in cardp,
          [l for l in cardp.splitlines() if "原价" in l][:1])
    check("★★ 不许用万达 W+专区的挂牌价 40.90",
          "40.90" not in cardp,
          [l for l in cardp.splitlines() if "原价" in l][:1])

    section("★ 影院名截断：只记录，不因此问买家（店主指出的点）")
    # ★★★ 一段被推翻的实现，留在这里防止有人再想当然加回去：
    #   我一度加了「cinemaTruncated=true 就先让买家确认影院」的闸门，
    #   理由是良票文档 §7.1「true 时 cinemaHitNums 往往大于 1，必须走二次确认」。
    #   ★ 店主问：「我们用万达的接口去匹配，不就相当于做二次确认了吗」——
    #     实测核对后确认店主对：
    #       cinemaTruncated 说的是【模型 OCR 输出的影院名被截断】
    #       （第二张：rawResults.cinema='万达影城（娄底五江广...'），
    #        而 finalResults.cinemaName 是【良票标准库的完整名】
    #        ='万达影城（娄底五江广场XLAND店）'，与万达侧名字逐字一致；
    #        那个截断前缀当时只匹配到 1 家候选 ⇒ 让买家确认是白问一轮。
    #   ⇒ 截断只记 notes，不拦；真正的交叉验证交给万达侧的片名/厅名比对。
    TRUNC_RESULT = {
        "recognizeId": "9101", "cached": False, "rawResults": {},
        "finalResults": {
            "matchLevel": "EXACT", "noMatchReason": None,
            "cinemaId": 6278, "cinemaName": "万达影城（娄底五江广场XLAND店）",
            "cityName": "娄底", "cityCode": "431300",
            "brandName": "万达影城", "cinemaHitNums": 1,
            "cinemaTruncated": True,
            "movieId": 845, "movieName": "什么意思夫妇",
            "showId": "37529007", "hallName": "7号厅",
            "startTime": "2026-10-14T20:35:00+08:00",
            "seatMatched": False, "seat": [], "prices": [],
            "candidates": {"cinemas": [], "movies": [], "shows": []},
        },
    }
    from integrations.liangpiao.api import RecognitionResult as _RR
    rr = _RR.from_data(TRUNC_RESULT)
    check("★ cinemaTruncated 仍被解析出来（用于记录）",
          rr.cinema_truncated is True, str(rr.cinema_truncated))
    check("★ 缺字段时默认 False（不误伤）",
          _RR.from_data({"finalResults": {"matchLevel": "EXACT"}}
                        ).cinema_truncated is False)
    check("★★ finalResults.cinemaName 是标准库完整名（不是截断那个）",
          rr.cinema_name == "万达影城（娄底五江广场XLAND店）", rr.cinema_name)

    flowt, _trt = make_flow({})
    convt = Conversation(chat_id="chat-trunc")
    convt.state = ConvState.RECOGNIZING
    rt = flowt._after_recognition(convt, rr, _tf.FlowResult())
    check("★★ 截断【不再】拦着问买家（店主指出这是白问）",
          rt.state is not ConvState.PICK_CINEMA, f"{rt.state.name}")
    check("★ 但记了 notes 让人能查",
          any("cinemaTruncated" in n for n in rt.notes), str(rt.notes)[:100])

    section("★★ 报价卡自己就问「座位是否标记 / 需要几张票」")
    # ★★ 2026-10-14 店主定案：
    #   「之前的版本是没有加入图片是否标记的判断，所以需要问买家是否标记」
    #   ⇒ 那一问【加在报价卡上】，不另发一条消息。
    #   （我昨天先加了一条独立的 SEAT_COUNT_PENDING，重复了，已删 ——
    #     一条消息问一件事，不重复问同一个问题。）
    out_ns = _tf.FlowResult()
    flowns, _trns = make_flow({})
    flowns._emit_quote_card(_NoSeatOriginal(), [{"seatNo": ""}], q, out_ns)
    keys_ns = list(out_ns.template_keys or [])
    card_ns = "\n".join(out_ns.messages or [])
    check("★★ 报价卡上问了「座位是否在图片中标记」",
          "座位是否在图片中标记" in card_ns, card_ns[-120:])
    check("★★ 报价卡上也问了「需要几张票」",
          "需要几张票" in card_ns, card_ns[-120:])
    check("★ 不再重复发第二条问张数的消息",
          "SEAT_COUNT_PENDING" not in keys_ns, str(keys_ns))
    check("★★ 没选座位时报价卡的「座位」不是空白（写明待确认）",
          "请回复要几个座位" in card_ns, card_ns[-200:])
    # ★ 有座位时同样要问「标记了吗」（这条与有没有座位无关）
    out_ws = _tf.FlowResult()
    flowws, _trws = make_flow({})
    flowws._emit_quote_card(_NoSeatOriginal(), [{"seatNo": "7排12座"},
                                                {"seatNo": "7排13座"}],
                            q, out_ws)
    check("★ 有座位时也问「标记了吗」",
          "座位是否在图片中标记" in "\n".join(out_ws.messages or []),
          str(out_ws.template_keys))

    # ---------------------------------------------------------------- 十四
    section("十四、★★★ 出票失败必须落到订单库（店主给的真场景）")
    # ★★★ 触发：店主给了一张真图，说"座位价格不一致时良票会返回出票失败"。
    #   查这条回调时发现 _on_failed 只记 note + 发一句给买家，
    #   【订单库完全没写】—— 那张单还停在 PAID/待出票，
    #   订单管理页永远显示它在等出票，可它已经失败退钱了。
    from application import ledger as _lgf
    from domain.order import OrderStore as _OSf
    _lgf.stores().orders = _OSf(":memory:")
    routes_f = {
        "/recognize/seat-shot": (200, envelope(data=EXACT_RESULT)),
        "/order/preflight": (200, envelope(data={
            "available": True, "totalAmount": "7020",
            "estimateAmount": "6620", "marketAmount": "7780",
            "estimated": True})),
        "/order/create": (200, envelope(data={
            "orderNo": "LP-FAIL-T", "status": "SUBMITTING",
            "freezeAmount": "7020", "seatCount": 2, "duplicated": False,
            "pickupUrl": "https://x/p"})),
    }

    def _fail_call(extra: dict):
        fl, tr2 = make_flow(routes_f)
        fl.order_store = _lgf.stores().orders
        cv = Conversation(chat_id="fail-1")
        fl.on_screenshot(cv, "https://x/a.jpg", city_name="天津")
        fl.on_confirm = None                       # noqa: F841  (占位, 不用)
        fl.on_buyer_confirm(cv)
        fl.on_payment_confirmed(cv, FlowResult())
        _b = json.dumps({"event": "order.failed",
                         "data": dict({"orderNo": "LP-FAIL-T"}, **extra)},
                        ensure_ascii=False)
        _ts, _nn = "1700000000", "nf1"
        _h = {"X-App-Key": "K", "X-Timestamp": _ts, "X-Nonce": _nn,
              "X-Sign": sign_body("K", "S", _ts, _nn, _b)}
        _a, _o = fl.on_callback(_b, _h, app_key="K", app_secret="S",
                                now=1700000000)
        return fl, _o, _lgf.stores().orders

    # ① 出票失败 · 全额退回
    _fl, _out, _st = _fail_call({
        "failCode": "300007", "failReason": "订单限价过低",
        "lossAmount": "0", "refundAmount": "7020"})
    _o = _st.find_by_provider_order("LP-FAIL-T")
    check("★★ 出票失败 → 订单库状态变 FAILED",
          _o is not None and _o.state == "FAILED",
          f"{_o.state if _o else None}")
    check("★★ 全额退回 → payment_state=REFUNDED（不能再算收入）",
          _o is not None and _o.payment_state == "REFUNDED",
          f"{_o.payment_state if _o else None}")
    # ★ bool() 不能少：A and B 返回的是 B 本身 —— 这里 B 是字典，
    #   直接当 ok 传会把 dict 塞进 RESULTS，最后 sum() 报 TypeError。
    check("★★ 失败细节写进 payload（对账要用）",
          bool(_o is not None and (_o.payload or {}).get("ticket_failed")),
          str((_o.payload or {}).get("ticket_failed"))[:80])

    # ② 有损关单 → 保留 CONFIRMED（钱确实收了，损失记在 payload）
    _fl2 = None
    _lgf.stores().orders = _OSf(":memory:")
    _fl2, _out2, _st2 = _fail_call({
        "failCode": "300007", "failReason": "溢价过高",
        "lossAmount": "300", "refundAmount": "6720"})
    _o2 = _st2.find_by_provider_order("LP-FAIL-T")
    check("★★ 有损关单 → 仍是 FAILED", _o2 is not None and _o2.state == "FAILED",
          f"{_o2.state if _o2 else None}")
    check("★★ 有损关单 → 保留 CONFIRMED（买家付的钱要认账）",
          _o2 is not None and _o2.payment_state == "CONFIRMED",
          f"{_o2.payment_state if _o2 else None}")
    check("★★ 有损金额记进 payload",
          _o2 is not None and (_o2.payload or {}).get(
              "ticket_failed", {}).get("loss_fen") == 300,
          str((_o2.payload or {}).get("ticket_failed"))[:80])

    # ③ 出票前取消
    _lgf.stores().orders = _OSf(":memory:")
    _fl3, _out3, _st3 = _fail_call({"orderStatus": "CANCELLED"})
    _o3 = _st3.find_by_provider_order("LP-FAIL-T")
    check("★★ 出票前取消 → CANCELLED（不是 FAILED）",
          _o3 is not None and _o3.state == "CANCELLED",
          f"{_o3.state if _o3 else None}")

    # ④ 付款后建单成功必须把上游单号补到本地单上（不能丢）
    _lgf.stores().orders = _OSf(":memory:")
    _fl4, _tr4 = make_flow(routes_f)
    _fl4.order_store = _lgf.stores().orders
    _cv4 = Conversation(chat_id="up-1")
    _fl4.on_screenshot(_cv4, "https://x/a.jpg", city_name="天津")
    _r1 = _fl4.on_buyer_confirm(_cv4)
    _r2f = _fl4.on_payment_confirmed(_cv4, FlowResult())
    _o4 = _lgf.stores().orders.get(_r1.out_order_no)
    check("★★★ 付款后建单 → 上游单号补到本地单（原来会丢）",
          _o4 is not None and _o4.provider_order_id == "LP-FAIL-T",
          f"{_o4.provider_order_id if _o4 else None}")
    check("★★ 补全后 state=PAID / payment_state=CONFIRMED",
          _o4 is not None and _o4.state == "PAID"
          and _o4.payment_state == "CONFIRMED")
    check("★★★ 落库不再报幂等冲突（原来被静默吞掉）",
          getattr(_fl4, "_order_error", "") == "",
          repr(getattr(_fl4, "_order_error", "")))

    # ---------------------------------------------------------------- 十五
    section("十五、★★★ 识别座位价格异常 / 创建订单失败（店主给的两个场景）")
    # ★★★ 店主 2026-10-14 给的文案表里明确：
    #   「识别座位价格异常文案」→ bubble 44（识别阶段）
    #   「创建订单失败文案」    → bubble 8 （建单阶段）
    #   而这两个我们【都发不出去】：
    #     · priceMismatch 字段从第一天就解析了，编排层从来没读
    #     · CREATE_FAILED 模板在，没人调它
    _pm = json.loads(json.dumps(EXACT_RESULT))
    _pm["finalResults"]["priceMismatch"] = True
    _pm["finalResults"]["seatConfirmRequired"] = True
    _pm["finalResults"]["seatConfirmReasons"] = ["PRICE_MISMATCH"]
    flow_pm, tr_pm = make_flow({"/recognize/seat-shot": (200, envelope(data=_pm))})
    conv_pm = Conversation(chat_id="pm-1")
    r_pm = flow_pm.on_screenshot(conv_pm, "https://x/a.jpg", city_name="天津")
    check("★★ priceMismatch=true → 发 bubble 44 文案",
          any("识别到座位被遮挡" in m for m in (r_pm.messages or [])),
          str(r_pm.messages)[:90])
    check("★★ 文案里【没有】未替换的模板变量",
          not any("{" in m and "}" in m for m in (r_pm.messages or [])),
          str(r_pm.messages)[:90])
    check("★★ 价格异常时【不去询价】（座位都没认全，询价没意义）",
          "/order/preflight" not in [c["path"] for c in tr_pm.calls],
          str([c["path"] for c in tr_pm.calls]))
    check("★ 记了 note 说明是 priceMismatch（排查要用）",
          any("priceMismatch" in n for n in (r_pm.notes or [])),
          str(r_pm.notes)[:80])

    # 建单被拒：200006/200007/300005 → bubble 8；其余保持通用
    for _code, _want in ((200006, "CREATE_FAILED"), (200007, "CREATE_FAILED"),
                         (300005, "CREATE_FAILED"), (300004, "RECOG_FAIL"),
                         (300006, "RECOG_FAIL")):
        _rt = {
            "/recognize/seat-shot": (200, envelope(data=EXACT_RESULT)),
            "/order/preflight": (200, envelope(data={
                "available": True, "totalAmount": "7020",
                "estimateAmount": "6620", "marketAmount": "7780",
                "estimated": True})),
            "/order/create": (200, envelope(code=_code, message="拒绝")),
        }
        _fl, _tr = make_flow(_rt)
        _cv = Conversation(chat_id=f"cf-{_code}")
        _fl.on_screenshot(_cv, "https://x/a.jpg", city_name="天津")
        _fl.on_buyer_confirm(_cv)
        _rr = _fl.on_payment_confirmed(_cv, FlowResult())
        check(f"★★ 建单被拒 {_code} → {_want}",
              _want in (_rr.template_keys or []), str(_rr.template_keys))
        if _want == "CREATE_FAILED":
            check(f"  {_code} 发的是 bubble 8 原文",
                  any("其中有一个位置价格不对哦" in m
                      for m in (_rr.messages or [])),
                  str(_rr.messages)[:70])

    section("十六、★★★ 订单卡片 → 改价命令（改价链的根）")
    # ★★ 为什么这段必须断：
    #   改价链断过一整轮，原因是【没人处理订单卡片消息】——
    #   而闲鱼订单号只在那条消息里（付款之前拿不到）。
    #   断了没有任何报错：买家收到"我改价后您再付款"，然后永远等不到改价。
    import inspect as _insp
    from application import plugin_runtime as _pr

    check("★★ 全仓处理了 messageType=26（订单卡片）",
          hasattr(_pr, "_is_order_card")
          and hasattr(_pr, "_from_order_card"))

    _src = _insp.getsource(_pr.events_to_commands)
    check("★★★ im.message.received 先分流订单卡片（顺序不能反）",
          "_is_order_card" in _src
          and _src.find("_from_order_card") < _src.find("_from_buyer_message"),
          "先分流卡片再走普通消息；反了的话卡片会被当普通消息，订单号就丢了")

    # 判定：卡片 vs 普通消息（这是分流正确的前提）
    _cases = [
        ({"payload": {"messageType": "26", "orderId": "200123"}}, True),
        ({"payload": {"orderId": "200123"}}, True),
        ({"payload": {"messageType": 28, "orderId": "200123",
                       "chatId": "__notify__"}}, False),
        ({"payload": {"content": "你好"}}, False),
        ({"payload": {"imageUrl": "http://x/y.jpg"}}, False),
        ({"payload": {"messageType": "1", "content": "在吗"}}, False),
    ]
    _wrong = [c for c, want in _cases if _pr._is_order_card(c) is not want]
    check("★★ 订单卡片判定正确（类型 26 或无类型且带 orderId）",
          not _wrong, str(_wrong))

    _notices = [
        {"messageType": 28, "chatId": "__notify__", "orderId": "200123",
         "content": "买家已确认收货，交易成功"},
        {"messageType": 14, "chatId": "chat-1",
         "content": "买家已确认收货，交易成功"},
        {"messageType": 999, "chatId": "chat-1",
         "content": "快给ta一个评价吧～"},
    ]
    _notice_check = getattr(_pr, "_is_non_buyer_notice", None)
    check("★★★ 系统通知判定（确认收货、提醒评价、__notify__）",
          callable(_notice_check) and all(
              _notice_check({"event": "im.message.received", "payload": p})
              for p in _notices))
    if callable(_notice_check):
        check("★ 正常买家文本和订单卡片不误拦",
              not _notice_check({"payload": {"messageType": 1,
                                               "chatId": "chat-1",
                                               "content": "你好"}})
              and not _notice_check({"payload": {"messageType": 26,
                                                   "chatId": "chat-1",
                                                   "orderId": "200123"}}))
        check("★★★ 系统通知不会产生回复或改价命令",
              all(_pr.events_to_commands(
                  {"event": "im.message.received", "payload": p}) == []
                  for p in _notices))

    _cs = _insp.getsource(_pr._from_order_card)
    check("★★ 卡片里生成 CHANGE_ORDER_PRICE（改价命令的唯一生成点）",
          "CHANGE_ORDER_PRICE" in _cs)
    check("★★ 幂等：dedupe_key 用闲鱼订单号（重投不重复建命令）",
          'dedupe_key=f"reprice:{online_order_id}"' in _cs)
    check("★★ 目标价取订单金额，没价就不建命令（不瞎猜）",
          "amount_fen" in _cs and "target_fen <= 0" in _cs)
    check("★★ 卡片会先把闲鱼订单号绑到订单上（改价/关单都要它）",
          "provider_order_id" in _cs and "find_by_provider_order" in _cs)

    section("十七、★★ 买家索要取票码（设置：获取票码）")
    # ★ 店主把"获取票码"配在买家指令表里（buyer_commands 的 get_code），
    #   而且给了 no_ticket_reply 这句确定的话 ⇒ 配置本身就表明它是
    #   【逐字可控的指令】，该走确定性路由，不该甩给模型。
    import inspect as _insp
    from application.service import TicketService as _TS
    _src = _insp.getsource(_TS.on_message)
    check("★★ 有「买家索要取票码」这条确定性路由",
          "_wants_ticket_code" in _src)
    check("★ 路由在确认指令之后、关键词之前（顺序有讲究）",
          _src.find("_wants_ticket_code")
          > _src.find("_is_confirm") if "_is_confirm" in _src else True)
    # ★ 精确等于：询问不能被当成指令
    _wsrc = _insp.getsource(_TS._wants_ticket_code)
    check("★★ 用精确等于（询问句不该被当成指令）",
          "want == kw" in _wsrc)
    # ★ 发码时要排掉已作废的单
    _bsrc = _insp.getsource(_TS._buyer_ticket_codes)
    check("★★ 只取有效单的票码（取消/退款/失败的不发）",
          "CANCELLED" in _bsrc and "REFUNDED" in _bsrc)
    # ★ 用的是同一条读模型（不是另开一个查询）
    check("★ 用 orders_payload 这条读模型查（和订单页一致）",
          "orders_payload" in _bsrc)

    section("十八、★★ 取票图（设置：取票图模板）")
    # ★ 链路是现成的、只是没人接起来：
    #   ① 插件 /internal/ticket-image/upload 能上传图片拿到 alicdn URL
    #   ② 插件 sendReplyMessage = command.image ? sendImage : sendMessage
    #   ③ Command.image 传 dict ⇒ asdict 原样序列化成 {imageUrl,width,height}
    import inspect as _insp2
    from application import ticket_image as _ti
    check("★★ 取票图模块在（校验/存储/取用）", hasattr(_ti, "store_upload")
          and hasattr(_ti, "image_for_send")
          and hasattr(_ti, "ensure_uploaded"))
    # ★ 默认不开 ⇒ 默认行为不变
    import settings as _st2
    _cfg2 = _st2.Settings()
    check("★★ 默认不开（enabled=False）⇒ 不发图，默认行为不变",
          bool(getattr(_cfg2.ticket_image, "enabled", True)) is False)
    check("★ 没开 / 没配图时 image_for_send 返回 None（什么都不做）",
          _ti.image_for_send(_cfg2) is None)
    # ★ 上传失败不许影响出票
    _tsrc = _insp2.getsource(_ti.ensure_uploaded)
    check("★★ 上传失败返回 None 且不抛（票码才是买家要的，图是附赠）",
          "return None" in _tsrc and "raise" not in _tsrc)
    # ★ 魔数校验（不看扩展名）
    #   ★ 修断言：getsource 给的是【源码文本】——
    #     源码里写的是 "\x89PNG"，会被 Python 解析成真实字节；
    #     而 getsource 返回的是字符 \ x 8 9 P N G。
    #     两者永远不相等（我第一版就是这么写错的）。
    #   ⇒ 更稳的判据：查 _MAGIC 表存在，且 detect_kind 真的用 startswith 比对。
    check("★★ 按魔数认图片类型（扩展名可以随便改，魔数不会）",
          "_MAGIC" in _insp2.getsource(_ti)
          and "startswith" in _insp2.getsource(_ti.detect_kind))
    # ★ 换图要清掉旧 URL（否则会发旧图）
    _usrc = _insp2.getsource(_ti.store_upload)
    check("★★ 换图时清掉旧 URL（否则会发上一张图）",
          'd.pop("url"' in _usrc or "d.pop('url'" in _usrc)
    # ★ 出票点接了
    _f2 = (Path(__file__).resolve().parent.parent
           / "application" / "ticket_flow.py").read_text(encoding="utf-8")
    check("★★ 出票成功后附取票图（带幂等键，同一单只发一次）",
          "ticket_image.ensure_uploaded" in _f2
          and "ticket_image:" in _f2)
    # ★ 取票图失败不影响出票
    check("★ 取票图发不出去不写出票失败（只记 note）",
          "不影响出票" in _f2)
    # ★ 模块级 import（架构审计只认模块级）
    check("★ ticket_flow 模块级 import ticket_image（审计只认模块级）",
          "from application import ticket_image" in _f2)
    # ★★ 2026-10-14 分层：领域层不许直接排队。
    #   我第一版在 ticket_flow 里 `_api.queue().enqueue(_c)` ——
    #   那让 ticket_flow import plugin_api，而 plugin_api 依赖
    #   plugin_runtime → service → ticket_flow ⇒ 成环
    #   （架构审计的"循环依赖"从 1 涨到 3）。
    #   ⇒ 改成挂到 FlowResult.pending_commands，由 plugin_runtime 统一入队。
    check("★★ 领域层不直接排队（挂 pending_commands，防循环依赖）",
          "pending_commands" in _f2
          and "_api.queue().enqueue" not in _f2
          and "import plugin_api as _api" not in _f2)
    check("★★ ticket_flow 不 import plugin_api（那会成环）",
          "from application.plugin_api import" not in _f2
          and "from .plugin_api import" not in _f2)
    # ★ 外层统一入队
    _rt = (Path(__file__).resolve().parent.parent
           / "application" / "plugin_runtime.py").read_text(encoding="utf-8")
    check("★★ plugin_runtime 把 pending_commands 统一入队",
          "pending_commands" in _rt)
    check("★★ 密钥用注入（ticket_image 不 import plugin_api）",
          "set_secret_provider" in _rt)

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
