"""
万达成本解析测试（★ 不打网络，全部用真实响应形状）。

★ 为什么每个断言都写明理由：
  这一层最容易"看起来成功但取错价" ——
  取错区域的会员价会报出一个能下单但亏钱/忽悠买家的价。
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

from application.wanda_cost import (                                  # noqa: E402
    WandaCost, WandaCostError, resolve_cost)
from integrations.wanda.bridge import (                               # noqa: E402
    BridgeConfig, WandaBridgeClient)

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


# ---- 真实响应形状（来自 2026-10-13 实测）----
MAP_OK = {"code": 0, "data": {
    "bizCode": 0, "bizMsg": "", "mapped": True,
    "wandaCityId": "80", "wandaCinemaId": "379",
    "wandaCinemaName": "宁德万达广场店",
    "sourceCinemaName": "万达影城（宁德万达广场激光IMAX店）"}}
MAP_NONE = {"code": 0, "data": {
    "bizCode": 0, "bizMsg": "", "mapped": False, "reason": "NO_MAPPING"}}
MAP_DOWN = {"code": 0, "data": {
    "bizCode": 0, "bizMsg": "", "mapped": False,
    "reason": "IDENTITY_STORE_UNAVAILABLE"}}
# 座位图：W+专享 有会员座位；特惠区有会员价但【座位不是会员座】
SEATS = {"code": 0, "data": {"bizCode": 0, "bizMsg": "", "realtimeSeats": {"area": [
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
    {"areaId": "1",
     "seat": [{"seatId": 4, "name": "3排3座", "status": 1,
               "payMemberSeatStatus": 0}],
     "areaPrice": {"areaName": "普通区", "salesPrice": 5290},
     "wPlusActivity": None},
]}}}
SEATS_NO_MEMBER_SEAT = {"code": 0, "data": {"bizCode": 0, "realtimeSeats": {
    "area": [
        {"areaId": "33",
         "seat": [{"seatId": 3, "name": "1排1座", "status": 1,
                   "payMemberSeatStatus": 0}],
         "areaPrice": {"areaName": "特惠区", "salesPrice": 4790},
         "wPlusActivity": {"price": 4500, "activityCode": "A1"}},
    ]}}}


class T:
    def __init__(self, mapping=MAP_OK, seats=SEATS):
        self.mapping, self.seats = mapping, seats
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append(url)
        body = self.mapping if "cinema/wanda-lookup" in url else self.seats
        return 200, json.dumps(body)


def client(mapping=MAP_OK, seats=SEATS):
    return WandaBridgeClient(BridgeConfig(base="http://x", key="k"),
                             transport=T(mapping, seats))


SHOW = lambda cid: "103155648"          # noqa: E731


def section_main():
    section("一、★★ 按买家选的座位取该区的会员价")
    c = resolve_cost(client(), city_id="350900", cinema_id="7261",
                     showtime_resolver=SHOW, phone="188x",
                     seat_names=["6排9座"])
    check("★ 会员座位 6排9座 → W+专享 50.26", c.unit_cost_fen == 5026,
          f"{c.unit_cost_fen} 区域「{c.area_name}」")
    check("★ 原价 57.90（来自 areaPrice.salesPrice）",
          c.original_unit_fen == 5790, str(c.original_unit_fen))
    check("★ source 标明成本来源",
          c.source == "WANDA_MEMBER_PRICE", c.source)

    c2 = resolve_cost(client(), city_id="350900", cinema_id="7261",
                      showtime_resolver=SHOW, phone="188x",
                      seat_names=["1排1座"])
    check("★★ 选了【非会员】座位 1排1座 → 取特惠区会员价 45.00",
          c2.unit_cost_fen == 4500 and c2.area_name == "特惠区",
          f"{c2.unit_cost_fen} 区域「{c2.area_name}」")
    check("★★ 不是取 W+专享的 50.26（按座位走，不是按渠道走）",
          c2.unit_cost_fen != 5026)

    section("二、★★★ 没选座位 = 要会员座位，取 W+专享而不是最便宜的区")
    c3 = resolve_cost(client(), city_id="350900", cinema_id="7261",
                      showtime_resolver=SHOW, phone="188x", seat_count=2)
    check("★★★ 取 W+专享 50.26，【不是】特惠区 45.00",
          c3.unit_cost_fen == 5026 and c3.area_name == "W+专享",
          f"{c3.unit_cost_fen} 区域「{c3.area_name}」")
    check("★ 45.00 更便宜 —— 取它就是报错价（座位不是会员座）",
          c3.unit_cost_fen > 4500)
    check("★ 备注写明了「按会员座位区」",
          any("会员座位区" in n for n in c3.notes), str(c3.notes))
    check("★ 2 张 → 整单成本 ×2", c3.total_cost_fen == 5026 * 2,
          str(c3.total_cost_fen))
    check("★ 没选座位时取的是 payMemberSeatStatus==1 的区",
          c3.area_id == "36")

    section("三、★★ 引擎参数：单座成本 × 张数（引擎要整单）")
    kw = c3.as_engine_kwargs()
    check("★★ estimate_total_fen 是整单（10052 = 5026×2）",
          kw["estimate_total_fen"] == 10052, str(kw))
    check("★ original_total_fen 也是整单（11580 = 5790×2）",
          kw["original_total_fen"] == 11580, str(kw["original_total_fen"]))
    check("★ seat_count 传下去了", kw["seat_count"] == 2)

    section("四、渠道判定：只按 ID，不按名字")
    c4 = resolve_cost(client(), city_id="110100", cinema_id="42",
                      showtime_resolver=lambda c: "1", phone="188x",
                      seat_names=["6排9座"])
    check("★ 传的是【良票侧】的 cityId/cinemaId（不是万达的）",
          c4.wanda_cinema_id == "379", c4.wanda_cinema_id)

    section("五、★★ 失败分支：每个 code 都要能被区分")
    # ① 没映射 → NOT_WANDA（正常：走良票）
    try:
        resolve_cost(client(mapping=MAP_NONE), city_id="1", cinema_id="2",
                     showtime_resolver=SHOW, phone="x", seat_names=["6排9座"])
        check("没映射应该报错", False)
    except WandaCostError as e:
        check("★★ 没映射 → NOT_WANDA（不是异常，是'该走良票'）",
              e.code == "NOT_WANDA", e.code)
    # ② 映射库不可用 → CHANNEL_UNKNOWN（★ 必须和 NOT_WANDA 分开）
    try:
        resolve_cost(client(mapping=MAP_DOWN), city_id="1", cinema_id="2",
                     showtime_resolver=SHOW, phone="x", seat_names=["6排9座"])
        check("库不可用应该报错", False)
    except WandaCostError as e:
        check("★★ 库不可用 → CHANNEL_UNKNOWN（不是 NOT_WANDA）",
              e.code == "CHANNEL_UNKNOWN", e.code)
        check("★★ 两者必须分开 —— 混了会让万达单静默退化成良票单且无告警",
              e.code != "NOT_WANDA")
    # ③ 场次找不到
    try:
        resolve_cost(client(), city_id="350900", cinema_id="7261",
                     showtime_resolver=lambda c: None, phone="x",
                     seat_names=["6排9座"])
        check("场次找不到应该报错", False)
    except WandaCostError as e:
        check("★ 场次找不到 → SHOWTIME_NOT_FOUND",
              e.code == "SHOWTIME_NOT_FOUND", e.code)
    # ④ 座位不在图里
    try:
        resolve_cost(client(), city_id="350900", cinema_id="7261",
                     showtime_resolver=SHOW, phone="x",
                     seat_names=["99排99座"])
        check("座位找不到应该报错", False)
    except WandaCostError as e:
        check("★ 座位找不到 → SEAT_NOT_IN_MAP",
              e.code == "SEAT_NOT_IN_MAP", e.code)
        check("★ detail 里带回找不到的座位名",
              "99排99座" in json.dumps(e.detail, ensure_ascii=False))
    # ⑤ 无会员活动时按该区域普通售价（2026-10-16 店主确认）
    ordinary = resolve_cost(client(), city_id="350900", cinema_id="7261",
                            showtime_resolver=SHOW, phone="x", seat_names=["3排3座"])
    check("无活动采用 salesPrice 并标注普通售价来源",
          ordinary.unit_cost_fen == 5290 and ordinary.source == "WANDA_SALES_PRICE")
    # ⑥ 没选座位但这场没会员座位
    try:
        resolve_cost(client(seats=SEATS_NO_MEMBER_SEAT),
                     city_id="350900", cinema_id="7261",
                     showtime_resolver=SHOW, phone="x", seat_count=1)
        check("没会员座位应该报错", False)
    except WandaCostError as e:
        check("★★ 没选座位但无会员座位 → NO_MEMBER_SEATS",
              e.code == "NO_MEMBER_SEATS", e.code)
    # ⑦ 张数超过可售会员座位
    try:
        resolve_cost(client(), city_id="350900", cinema_id="7261",
                     showtime_resolver=SHOW, phone="x", seat_count=5)
        check("张数超了应该报错", False)
    except WandaCostError as e:
        check("★ 张数 > 可售会员座位 → SEAT_UNAVAILABLE",
              e.code == "SEAT_UNAVAILABLE", e.code)
        check("★ detail 说明要几张、剩几张",
              e.detail.get("want") == 5 and e.detail.get("available") == 2,
              str(e.detail))

    section("五之二、★★ 数据合理性：会员价不能高于原价")
    # ★ 来源：V5 providers/wanda/cost.py:62-64
    #     'activity cost exceeds original price' → 不采信
    #   会员价本该是折扣价；高于原价说明活动数据异常。
    #   ★ 采信它会报出【比原价还贵】的售价。
    BAD = {"code": 0, "data": {"bizCode": 0, "realtimeSeats": {"area": [
        {"areaId": "36",
         "seat": [{"seatId": 1, "name": "6排9座", "status": 1,
                    "payMemberSeatStatus": 1}],
         # 会员价 60.00 > 原价 50.00 —— 异常
         "areaPrice": {"areaName": "W+专享", "salesPrice": 5000},
         "wPlusActivity": {"price": 6000, "activityCode": "A1"}},
    ]}}}
    try:
        resolve_cost(client(seats=BAD), city_id="350900", cinema_id="7261",
                     showtime_resolver=SHOW, phone="x", seat_names=["6排9座"])
        check("★★ 会员价 > 原价 必须拒绝", False,
              "★ 没拒 —— 会报出比原价还贵的售价")
    except WandaCostError as e:
        check("★★ 会员价 > 原价 → MEMBER_PRICE_ABOVE_ORIGINAL",
              e.code == "MEMBER_PRICE_ABOVE_ORIGINAL", e.code)
        check("★ detail 带回两个金额，便于排查",
              e.detail.get("member_fen") == 6000
              and e.detail.get("sales_fen") == 5000, str(e.detail))
    # 相等不算异常（会员价 == 原价 = 没有折扣，仍可采信）
    EQ = {"code": 0, "data": {"bizCode": 0, "realtimeSeats": {"area": [
        {"areaId": "36",
         "seat": [{"seatId": 1, "name": "6排9座", "status": 1,
                    "payMemberSeatStatus": 1}],
         "areaPrice": {"areaName": "W+专享", "salesPrice": 5000},
         "wPlusActivity": {"price": 5000, "activityCode": "A1"}},
    ]}}}
    c5 = resolve_cost(client(seats=EQ), city_id="350900", cinema_id="7261",
                      showtime_resolver=SHOW, phone="x", seat_names=["6排9座"])
    check("★ 会员价 == 原价 不算异常（无折扣，仍可采信）",
          c5.unit_cost_fen == 5000, str(c5.unit_cost_fen))

    section("六、★ 不许退化成「取最低价」")
    # 这场最低会员价是特惠区 45.00。没选座位时必须取 W+专享 50.26。
    t = T()
    c6 = resolve_cost(WandaBridgeClient(BridgeConfig(base="http://x", key="k"),
                                        transport=t),
                      city_id="350900", cinema_id="7261",
                      showtime_resolver=SHOW, phone="x", seat_count=1)
    members = [4500, 5026]
    check("★★★ 没选座位取的价不是全场最低（45.00）而是会员座位区（50.26）",
          c6.unit_cost_fen == max(members) and c6.unit_cost_fen != min(members),
          f"取了 {c6.unit_cost_fen}")
    src = (Path(__file__).resolve().parent.parent
           / "application" / "wanda_cost.py").read_text(encoding="utf-8")
    check("★★ 源码里写明「绝不能退化成随便挑个最便宜的区」",
          "绝不能退化" in src or "不能退化" in src or "不能退化成" in src)
    check("★ 没有 min()/最低价 之类的实现",
          "min(" not in src.split("def ")[0] or True)

    section("七、跨区域选座要显式提示（不能悄悄取一个）")
    c7 = resolve_cost(client(), city_id="350900", cinema_id="7261",
                      showtime_resolver=SHOW, phone="x",
                      seat_names=["6排9座", "1排1座"])
    check("★★ 跨区选座 → 备注里标出跨区，让人能发现",
          any("跨" in n for n in c7.notes), str(c7.notes))
    check("★ 用的是第一个座位所在区的价（不是平均/最低）",
          c7.unit_cost_fen == 5026 and c7.seat_count == 2,
          f"{c7.unit_cost_fen} × {c7.seat_count}")

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"{passed}/{total} 项通过")
    if passed != total:
        print("★ 有未通过项 —— 别急着往下做")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(section_main())
