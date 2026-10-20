"""
渠道分流 + 万达报价 的端到端测试（★ 不打网络，全用假对象）。

★ 要证明的三件事：
  ① 影院映射到万达 → 走万达成本（座位图会员价），不调良票 preflight
  ② 没映射到万达 → 走良票（行为与原来一致）
  ③ ★★ 万达【没选座位】也能报价 —— 取 W+专享区会员价
     （这就是原来那行 `if not seats: return` 守卫挡住的路）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from application.ticket_flow import (FlowResult,              # noqa: E402
                                     TicketFlow)
from domain.conversation import Conversation                          # noqa: E402
from domain.quote_rules import (BASIS_PER_TICKET, QUOTE_FIXED, QuoteResult,        # noqa: E402
                                PricingTier, QuoteRule, QuoteRuleEngine)
from integrations.liangpiao.api import (PreflightResult,               # noqa: E402
                                        RecognitionResult)
from integrations.wanda.bridge import (BridgeConfig,                  # noqa: E402
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
MAP_WANDA = {"code": 0, "data": {
    "bizCode": 0, "mapped": True, "wandaCityId": "80",
    "wandaCinemaId": "379", "wandaCinemaName": "宁德万达广场店",
    "sourceCinemaName": "万达影城（宁德万达广场激光IMAX店）"}}
MAP_NONE = {"code": 0, "data": {
    "bizCode": 0, "mapped": False, "reason": "NO_MAPPING"}}
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
               "payMemberSeatStatus": 1},
              {"seatId": 2, "name": "6排10座", "status": 1,
               "payMemberSeatStatus": 1}],
     "areaPrice": {"areaName": "W+专享", "salesPrice": 5790},
     "wPlusActivity": {"price": 5026, "activityCode": "A1"}},
    {"areaId": "33",
     "seat": [{"seatId": 3, "name": "1排1座", "status": 1,
               "payMemberSeatStatus": 0}],
     "areaPrice": {"areaName": "特惠区", "salesPrice": 4790},
     "wPlusActivity": {"price": 4500, "activityCode": "A1"}},
]}}}
ACCOUNTS = {"accounts": [{"phone": "18873890439", "available": True}],
            "count": 1}


class Transport:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append(url)
        if "wanda-lookup" in url:
            return 200, json.dumps(self.mapping)
        if "by-cinema" in url:
            return 200, json.dumps(SHOWS)
        if "seats" in url:
            return 200, json.dumps(SEATS)
        if "wplus-accounts" in url:
            return 200, json.dumps(ACCOUNTS)
        return 404, "{}"


class FakeLiangPiao:
    """只记调用，不做真事。★ preflight_calls 用来证明万达没走它。"""

    def __init__(self):
        self.preflight_calls = []
        self.city_calls = []
        self.create_calls = []

    def pick_ticket_mode(self, start_time):
        return "STANDARD"

    def ch4_city_list(self, keyword=None):
        self.city_calls.append(keyword)
        if keyword == "宁德":
            return [{"code": "350900", "name": "宁德"}]
        return []

    def ch5_preflight(self, *a, **kw):
        # ★ 记下调用 —— 万达渠道的用例断言它是 0 次。
        # ★★ 返回一个【最小合法响应】，而不是抛异常：
        #   原来抛 AssertionError 是为了抓"万达误调良票"，
        #   但那让所有良票用例都走不下去（"没超限就正常询价"根本无法验证）。
        #   抓误调的责任应该由【用例自己查 preflight_calls】来担，
        #   不该让假对象替所有用例做决定。
        self.preflight_calls.append((a, kw))
        return PreflightResult(
            available=True, reason="",
            total_amount_fen=5026, estimate_amount_fen=5026,
            market_amount_fen=6980, estimated=False)

    def ch5_create(self, *a, **kw):
        self.create_calls.append((a, kw))
        raise AssertionError("★★★ 万达渠道绝不该调良票下单")


def rules():
    r = QuoteRule(name="万达系", weight=9, enabled=True, brands=["万达影城"],
                  quote_mode=QUOTE_FIXED, method="加法", markup_yuan=0,
                  limit_yuan=0, capped=False, rounding_fen=10,
                  basis=BASIS_PER_TICKET,
                  tiers=[PricingTier(lower_bps=0, upper_bps=10000,
                                     fixed_add_fen=100, upper_inclusive=True)])
    return QuoteRuleEngine([r], revision="t")


def result(**kw):
    base = dict(match_level="EXACT", cinema_id="7261",
                cinema_name="万达影城（宁德万达广场激光IMAX店）",
                city_name="宁德", brand_name="万达影城",
                movie_id="900002313", movie_name="生化危机：爆发夜",
                show_id="12345", hall_name="8号激光IMAX厅",
                start_time="2026-10-13T14:20:00+08:00",
                seats=[], price_all_fen=5790, expected_seat_count=1)
    base.update(kw)
    return RecognitionResult(**base)


def flow(mapping, bridge=True):
    t = Transport(mapping)
    br = (WandaBridgeClient(BridgeConfig(base="http://x", key="k"),
                            transport=t) if bridge else None)
    api = FakeLiangPiao()
    f = TicketFlow(api, rules(), tel="13800000000", wanda_bridge=br)
    return f, api, t


def main():
    now_ok = _patch_time()

    section("① ★ 影院映射到万达 → 走万达成本（不调良票 preflight）")
    f, api, t = flow(MAP_WANDA)
    conv = Conversation(chat_id="c")
    out = f._build_quote(conv, result(seats=[{"seatNo": "6排9座"}]), _out())
    check("★ 出报价卡", out.quote is not None and "QUOTE_CARD" in [
        e[0] for e in out.events] if hasattr(out, "events") else True,
        f"quote={out.quote.total_price_fen if out.quote else None}")
    check("★★ 没有调用良票 preflight（证明走的是万达）",
          not api.preflight_calls, f"{len(api.preflight_calls)} 次")
    check("★ 成本 = 会员座位(6排9座 → W+专享) 5026",
          out.quote is not None and out.quote.cost_total_fen == 5026,
          str(out.quote.cost_total_fen if out.quote else None))
    check("★ 售价 = 5026 + 100（每张+1元，取整到角）",
          out.quote is not None and out.quote.total_price_fen == 5130,
          str(out.quote.total_price_fen if out.quote else None))
    check("★ 备注里写明了成本来源", any("W+专享" in n for n in out.notes),
          str([n for n in out.notes if "W+专享" in n])[:90])
    check("★ 走的是桥的 4 个接口（映射/场次/座位图/号池）",
          any("wanda-lookup" in u for u in t.calls)
          and any("by-cinema" in u for u in t.calls)
          and any("seats" in u for u in t.calls), str(len(t.calls)))

    section("② ★★★ 万达【没选座位】也能报价（这是原来被守卫挡掉的路）")
    f, api, t = flow(MAP_WANDA)
    conv = Conversation(chat_id="c")
    out = f._build_quote(conv, result(seats=[]), _out())
    check("★★★ 没选座位也出了报价（原来这里 return RECOG_FAIL）",
          out.quote is not None,
          str([e for e in (out.template_keys or [])])[:60] if hasattr(out, "bubbles") else "")
    check("★★ 成本取的是 W+专享 5026（不是更便宜的特惠区 4500）",
          out.quote is not None and out.quote.cost_total_fen == 5026,
          str(out.quote.cost_total_fen if out.quote else None))
    check("★ 备注说明「买家未指定座位 → 按会员座位区」",
          any("会员座位" in n for n in out.notes), str(out.notes)[:110])
    check("★ 没有 RECOG_FAIL", not any(
        (e[0] if isinstance(e, tuple) else e) == "RECOG_FAIL"
        for e in (out.template_keys or [])), str(out.template_keys)[:80])

    section("②之二、★★★ 万达确认后：落本地单，【绝不】调良票下单")
    # ★ 这是接线时留下的真 bug：on_buyer_confirm 没有渠道判断，
    #   会拿万达的 showtimeId 去良票 ch5_create。
    # ★ 修法不是拦住，而是走万达自己的路：
    #   店家手动出票 → 我们落单 + 告诉买家出票时间。
    f, api, t = flow(MAP_WANDA)
    conv = Conversation(chat_id="c")
    out = f._build_quote(conv, result(seats=[{"seatNo": "6排9座"}]), _out())
    check("★ 报价成功", out.quote is not None)
    check("★★ 会话上记下了渠道 = WANDA",
          getattr(conv, "quote_channel", "") == "WANDA",
          repr(getattr(conv, "quote_channel", None)))
    api.create_calls = []
    out2 = f.on_buyer_confirm(conv)
    check("★★★ 确认后【没有】调良票 ch5_create",
          not api.create_calls, f"{len(api.create_calls)} 次")
    check("★★ 也没有调良票 preflight", not api.preflight_calls)
    check("★★ 告诉买家出票时间（和良票同一套档位口径）",
          any("出票时间" in m for m in (out2.messages or [])),
          str(out2.messages)[:90])
    check("★ 落了一笔本地单（店家能在订单管理页看到）",
          out2.order_no != "", repr(out2.order_no))
    check("★ 备注写明了「等店家手动出票」",
          any("手动出票" in n for n in out2.notes), str(out2.notes)[:110])

    section("③ ★ 没映射到万达 → 走良票（行为保持不变）")
    f, api, t = flow(MAP_NONE)
    conv = Conversation(chat_id="c")
    out = f._build_quote(conv, result(seats=[]), _out())
    check("★★ 没映射 + 没座位 → RECOG_FAIL 且文案说清「要选座位」",
          ("RECOG_FAIL" in (out.template_keys or []))
          and any("座位" in m and ("截图" in m or "圈" in m)
                  for m in (out.messages or [])),
          str(out.messages)[:120])
    check("★ 没调 preflight（因为没座位号就询不了价）",
          not api.preflight_calls)
    check("★★ 没有调座位图/场次（良票渠道不该碰万达接口）",
          not any("seats?showtimeId" in u for u in t.calls),
          f"{len(t.calls)} 次调用")

    section("④ ★ 没配桥 → 全部走良票（服务不会被卡住）")
    f, api, t = flow(MAP_WANDA, bridge=False)
    check("★ wanda_bridge=None 时不抛异常", f.wanda_bridge is None)
    conv = Conversation(chat_id="c")
    out = f._build_quote(conv, result(seats=[]), _out())
    check("★ 依然给出「要选座位」的提示（走良票分支）",
          ("RECOG_FAIL" in (out.template_keys or []))
          and any("座位" in m and ("截图" in m or "圈" in m)
                  for m in (out.messages or [])),
          str(out.messages)[:100])

    section("⑤ ★ 城市名 → cityCode 只查一次（缓存）")
    f, api, t = flow(MAP_WANDA)
    conv = Conversation(chat_id="c")
    f._build_quote(conv, result(seats=[{"seatNo": "6排9座"}]), _out())
    f._build_quote(conv, result(seats=[{"seatNo": "6排9座"}]), _out())
    check("★★ 两次报价只查一次城市表（缓存生效）",
          len(api.city_calls) == 1, f"{len(api.city_calls)} 次")

    section("⑥ ★★ 良票错误码 → 场景变体（200006/200007/2005/3006）")
    # ★ 依据（良票文档，不是猜的）：
    #   §4 座位图：「预校验在下单接口内同步进行：所有候选供货方的座位图
    #    都判定违规（座位已售报 200006，孤座/情侣座违规报 200007）时，
    #    下单直接同步报错，不占用余额、不产生订单回调。」
    #   §7 识别：noMatchReason 只有 SHOW_EXPIRED / NOT_FOUND ——
    #     ★ 没有座位类原因，所以这些【不是识别失败】，是询价/下单被拒。
    import templates as T
    for code, variant, must in ((200006, "seat_taken", "被别人买走"),
                                (200007, "seat_blocked", "座位被遮挡"),
                                (200005, "show_stopped", "停止购票"),
                                (300006, "price_changed", "票价刚有变动")):
        got = T.render_messages("RECOG_FAIL",
                               {"_variant": variant, "fail_reason": "X"})[0]
        check(f"  错误码 {code} → {variant}", must in got, got[:44])

    section("⑦ ★★ 单笔限购（maxSeatsPerOrder）在询价前就拦")
    # ★ 为什么必须提前拦：文档说「超过限购的订单会在【出票环节】被拒」——
    #   等到那时买家已经付过钱了，得退款。用识别结果里已有的值拦一下成本为零。
    f, api, t = flow(MAP_NONE)
    conv = Conversation(chat_id="c")
    r = result(seats=[{"seatNo": f"{i}排1座"} for i in range(1, 6)])
    r.max_seats_per_order = 4
    out = f._build_quote(conv, r, _out())
    check("★★ 选了 5 张、限购 4 张 → 拦下不发询价", not api.preflight_calls,
          f"preflight {len(api.preflight_calls)} 次")
    check("★★ 文案说清最多买几张",
          any("最多买 4 张" in m for m in (out.messages or [])),
          str(out.messages)[:70])

    f2, api2, t2 = flow(MAP_NONE)
    conv2 = Conversation(chat_id="c")
    r2 = result(seats=[{"seatNo": "1排1座"}])
    r2.max_seats_per_order = 4
    f2._build_quote(conv2, r2, _out())
    check("★ 没超限就正常询价（不是一律拦）", bool(api2.preflight_calls),
          f"preflight {len(api2.preflight_calls)} 次")

    f3, api3, t3 = flow(MAP_NONE)
    conv3 = Conversation(chat_id="c")
    r3 = result(seats=[{"seatNo": f"{i}排1座"} for i in range(1, 6)])
    # r3.max_seats_per_order 保持 None
    f3._build_quote(conv3, r3, _out())
    check("★★ 没有限购值时【不拦】—— 不能拿默认 6 当依据", bool(api3.preflight_calls),
          f"preflight {len(api3.preflight_calls)} 次")

    _unpatch_time(now_ok)
    section("⑧ ★ 识别错误码 → 文案（良票 70xxx 全覆盖）")
    # ★ 依据：良票文档 §3 错误码表的「处理建议」列。
    #   ★ 判据不是文案好听，是【买家知道该做什么】：
    #     700004/700005 是平台侧临时故障 → 说再发一次，不能说你的图不对
    #     （否则买家白白重拍好几遍）。
    import application.ticket_flow as tf
    for code, must in ((700002, "重新发"), (700003, "再发一次"),
                       (700004, "再发一次"), (700005, "重新发一次"),
                       (700006, "不是选座页"), (700008, "重新发截图"),
                       (700009, "重新列")):
        got = tf._recog_fail_reason(code)
        check(f"  错误码 {code} 有专门文案", must in got, got[:40])
    check("★ 未知错误码仍有兜底（不抛异常）",
          tf._recog_fail_reason(999999) == "没能识别出座位信息")
    check("★★ 平台侧故障不许写成「你的截图不对」",
          "截图不对" not in tf._recog_fail_reason(700004)
          and "截图不对" not in tf._recog_fail_reason(700005))

    section("⑨ ★★ 城市码查不到时，万达单不许静默降级到良票")
    # ★★ 这是一个真缺口（2026-10-14 发现）：
    #   _liangpiao_city_code 查不到城市码时会【静默返回空】，
    #   于是 _resolve_channel 返回 None → 走【良票分支】——
    #   而良票 seats 必填 1~6，没座位号就报 seat_missing，
    #   买家收到「未识别到您选择的座位信息」，而他发的明明是万达的图。
    #   修法：识别结果自带的 brandName 里写着「万达」时，
    #     不静默降级，直接报 CITY_CODE_UNAVAILABLE（上层转人工/重试）。
    from integrations.wanda.bridge import WandaBridgeError as _WBE
    from integrations.liangpiao.api import RecognitionResult as _RR2
    import application.ticket_flow as _tf2

    class _NoCityAPI:
        """没有 ch4_city_list —— 模拟城市表接口不可用。"""
        def pick_ticket_mode(self, *a, **kw):
            return "STANDARD"

    def _mk(brand, name):
        return _RR2.from_data({"finalResults": {
            "matchLevel": "EXACT", "cinemaId": 6278, "cinemaName": name,
            "cityName": "娄底", "brandName": brand, "movieName": "X",
            "showId": "1", "startTime": "2026-10-14T20:35:00+08:00"}})

    class _NeverCalled:
        def wanda_lookup(self, **kw):
            raise AssertionError("城市码查不到时不该走到 wanda_lookup")

    ft = _tf2.TicketFlow(_NoCityAPI(), rules(), tel="138",
                         wanda_bridge=_NeverCalled())
    try:
        got = ft._resolve_channel(_mk("万达影城", "万达影城（娄底五江广场XLAND店）"))
        check("★★ 万达品牌 + 城市码查不到 → 必须拦住", False,
              f"★ 竟然返回 {got!r}（会降级到良票）")
    except _WBE as exc:
        check("★★ 万达品牌 + 城市码查不到 → 必须拦住", True,
              f"{exc.code}")
    # ★ 非万达品牌没映射就是正常走良票（既定设计，别误伤）
    try:
        got2 = ft._resolve_channel(_mk("某某院线", "某某影城"))
        check("★ 非万达品牌 → 正常回落良票（不误伤）", got2 is None, repr(got2))
    except Exception as exc:                                            # noqa: BLE001
        check("★ 非万达品牌 → 正常回落良票（不误伤）", False,
              f"★ 不该抛: {type(exc).__name__}: {str(exc)[:80]}")

    section("⑩ ★★ 厅名校验（用万达数据核对识别结果）")
    # ★★ 店主指出的点：「我们用万达的接口去匹配，不就相当于做二次确认了吗」
    #   —— 对。而且我们【已经】用片名在万达排片里定位场次了（pick_show 的
    #   film_name），那本身就是一次交叉验证。
    #   ★ 但【厅名】从来没校验过：WandaShow 有 hall_name，没人用它。
    #   为什么厅名是关键：如果影院识别错了，热门片在【错影院】的相近时刻
    #   很可能也在放 ⇒ 光靠片名定位不出错；厅名是更细的指纹。
    from application.ticket_flow import _hall_mismatch as _hm
    for w, g, want, why in (
        ("7号厅", "7号厅", False, "完全一致"),
        ("8号激光厅", "8号激光厅", False, "完全一致"),
        ("7号厅", "7号激光厅", False, "★ 只是命名习惯不同（多两个字），不算对不上"),
        ("15号-IMAX厅（儿童需购票）", "15号IMAX厅", False, "同上"),
        ("7号厅", "2号厅", True, "★ 真的对不上 → 该提醒"),
        ("8号激光厅", "6号-4DX激光厅（儿童需购票）", True, "★ 真的对不上"),
        ("", "7号厅", False, "识别没给厅名 → 不判（没依据不指认别人错）"),
        ("7号厅", "", False, "万达没给厅名 → 不判"),
    ):
        check(f"  {why}: {w!r} vs {g!r}", _hm(w, g) == want,
              f"得到 {_hm(w, g)}，期望 {want}")

    section("⑪ ★★★ 万达单的档位 / 渠道标记（拆开之后）")
    # ★★★ 这是一个真 bug（2026-10-14 修）：
    #   万达分支原来这么调：
    #       self._stash_quote(conv, result, seat_names, "WANDA", quote, ...)
    #   第 4 个位置参数是 ticket_mode（档位），却传了【渠道名】，
    #   而 _stash_quote 又拿同一个参数当渠道判据：
    #       conv.ticket_mode  = ticket_mode                        ← 档位
    #       conv.quote_channel = "WANDA" if ticket_mode=="WANDA"  ← 渠道
    #   ⇒ 一个参数两种含义。后果：万达单 conv.ticket_mode = "WANDA"，
    #     而时效表只有 STANDARD/FAST/FLASH ⇒ 查不到。
    #     _window() 靠兜底还能拿到 1-60，但模型那边（_ticket_window_of）
    #     查不到就是【没有时效】—— 只能照配置里写死的数答。
    #   ★ 危险：不能只把 "WANDA" 换成档位 ——
    #     那会让 quote_channel 判成 LIANGPIAO，
    #     万达的单被确认后会拿万达的 showtimeId 去调【良票下单】（真下单）。
    #   ⇒ 拆成两个参数：ticket_mode = 档位、channel = 渠道。
    from integrations.liangpiao.api import (TICKET_FAST, TICKET_FLASH,
                                            TICKET_STANDARD)
    from application.conversation_ai import _ticket_window_of

    for tier, want in ((TICKET_STANDARD, "1-60分钟"),
                       (TICKET_FAST, "15分钟内"),
                       (TICKET_FLASH, "1-15分钟")):
        # 用假的 TicketFlow 直接把 _stash_quote 打一遍（不走网络）
        ft2 = TicketFlow.__new__(TicketFlow)
        ft2.quote_store = None
        cq = Conversation(chat_id=f"t-{tier}")
        qq = QuoteResult(
            unit_price_fen=3500, total_price_fen=3500, price_mode="FIXED",
            max_price_fen=None, rule_name="默认", quote_mode="渠道一口价",
            adjust="加法", cost_basis="estimateAmount", cost_unit_fen=3140,
            cost_total_fen=3140, seat_count=1, original_total_fen=3590)
        rr = RecognitionResult.from_data({"finalResults": {"matchLevel": "EXACT"}})
        ft2._stash_quote(cq, rr, [], tier, qq, None, FlowResult(),
                         channel="WANDA")
        check(f"★★ {tier}：档位记的是档位（不是渠道名）",
              cq.ticket_mode == tier, repr(cq.ticket_mode))
        check(f"★★ {tier}：渠道仍判成 WANDA（下单分流靠它）",
              cq.quote_channel == "WANDA", repr(cq.quote_channel))
        check(f"★★ {tier}：文案渠道 = wanda", cq.channel == "wanda",
              repr(cq.channel))
        check(f"★★ {tier}：模型能看到真实时效 {want}",
              _ticket_window_of(cq) == want, repr(_ticket_window_of(cq)))

    # ★ 良票分支的渠道不能被带偏
    ft3 = TicketFlow.__new__(TicketFlow)
    ft3.quote_store = None
    cl = Conversation(chat_id="l-1")
    ft3._stash_quote(cl, RecognitionResult.from_data({"finalResults": {"matchLevel": "EXACT"}}),
                     [], TICKET_STANDARD, qq, None, FlowResult(),
                     channel="LIANGPIAO")
    check("★ 良票单：渠道 = LIANGPIAO",
          cl.quote_channel == "LIANGPIAO", repr(cl.quote_channel))

    section("⑫ ★★★ 时序铁律：金额校验通过 + 已付款，才允许良票建单")
    # ★★★ 店主原话（2026-10-14）：
    #   「我说清楚，必须金额校验正确，且已付款，才可以良票建单」
    #
    #   为什么这条必须钉死（钉的是【代码形状】，不只是行为）：
    #     · 建单早了：买家还没付款就下给上游 —— 一旦金额不符或买家不付，
    #       就是一张【撤不掉】的上游单（良票出了票只能走退票，而退票
    #       可能退不成，还有供应商根本不支持退票）。
    #     · 建单漏了：买家付了钱、我们什么都没下给上游，票永远出不来。
    #   所以两个方向都要断言。
    _tf_src = (Path(__file__).resolve().parent.parent
               / "application" / "ticket_flow.py").read_text(encoding="utf-8")
    _code = "\n".join(ln for ln in _tf_src.splitlines()
                       if not ln.lstrip().startswith("#"))

    # ① 用 AST 把两个方法的【代码体】拆出来分别检查 ——
    #    纯文本搜 "ch5_create" 会把注释也算进去，判不准。
    import ast as _ast
    _tree = _ast.parse(_tf_src)
    _bodies = {}
    for _n in _ast.walk(_tree):
        if isinstance(_n, _ast.FunctionDef) and _n.name in (
                "on_buyer_confirm", "on_payment_confirmed"):
            # ★ 只取【方法体】的源码行，不含 docstring ——
            #   docstring 里正解释着"这里不调 ch5_create"，
            #   连 docstring 一起搜会把它自己判成"调了"（我第一版就踩了）。
            _start = _n.body[0].lineno
            if (isinstance(_n.body[0], _ast.Expr)
                    and isinstance(_n.body[0].value, _ast.Constant)):
                _start = _n.body[0].end_lineno + 1   # 跳过 docstring
            # ★★ 判据要准到"真的调用了" —— 源码文本里 ch5_create 会出现在
            #   docstring、行内注释、以及给运营看的字符串里（我踩了三次）。
            #   ⇒ 直接看 AST 里有没有 Call 节点的函数名是 ch5_create。
            _calls = [x.func.attr if isinstance(x.func, _ast.Attribute)
                      else getattr(x.func, "id", "")
                      for x in _ast.walk(_n) if isinstance(x, _ast.Call)]
            _bodies[_n.name] = "|".join(_calls)
            # 同时留一份源码（去掉注释行与行内注释）给"有没有那道门"用
            _seg = _tf_src.splitlines()[_start - 1:_n.end_lineno]
            _bodies[_n.name + "__src"] = "\n".join(
                _ln.split("#", 1)[0] for _ln in _seg)

    check("★★ on_buyer_confirm【不】调 ch5_create（确认时不建单）",
          "ch5_create" not in _bodies.get("on_buyer_confirm", ""),
          "on_buyer_confirm 里出现了 ch5_create")
    check("★★★ on_payment_confirmed【才】调 ch5_create",
          "ch5_create" in _bodies.get("on_payment_confirmed", ""),
          "on_payment_confirmed 里没有 ch5_create")
    check("★★ 建单前有金额门（实付 vs 报价）",
          "paid_fen" in _bodies.get("on_payment_confirmed__src", "")
          and "AMOUNT_MISMATCH" in _bodies.get(
              "on_payment_confirmed__src", ""))
    check("★★ 建单前有张数门（报价张数 vs 现在座位数）",
          "seat_count" in _bodies.get("on_payment_confirmed__src", ""))

    # ② 付款那条路必须真的调它 —— 否则建单永远不会发生
    _pr = (Path(__file__).resolve().parent.parent
           / "application" / "plugin_runtime.py").read_text(encoding="utf-8")
    check("★★ 付款事件真的会驱动建单（_from_order_paid 调 on_payment_confirmed）",
          "on_payment_confirmed" in _pr)
    check("★★ 付款找不到单时两条渠道都认（不再写死 WANDA）",
          'in ("WANDA", "LIANGPIAO")' in _pr)

    check("★★★ on_payment_confirmed 先过状态机（没确认过不许建单）",
          "TRANSITIONS" in _bodies.get("on_payment_confirmed__src", "")
          and "ORDER_PAID" in _bodies.get("on_payment_confirmed__src", ""),
          "缺状态机闸门 —— 演练里抓到过：状态还是已报价也建了单")

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"{passed}/{total} 项通过")
    return 0 if passed == total else 1


# ---- 让"距开场太近"那条限制不挡我们（样本时间是固定日期） ----
def _patch_time():
    import application.ticket_flow as m
    old = m._far_enough
    m._far_enough = lambda *a, **kw: True
    return old


def _unpatch_time(old):
    import application.ticket_flow as m
    m._far_enough = old


def _out():
    from application.ticket_flow import FlowResult
    return FlowResult()



if __name__ == "__main__":
    sys.exit(main())
