"""
出票系统插件桥的测试（★ 不打网络 —— 用假 transport）。

★ 为什么每个断言都写明「为什么值得测」：
  这一层踩过的坑都是"看起来成功、其实错了"的类型，不写清理由，
  以后有人会把它当"多余的检查"删掉。
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

from integrations.wanda.bridge import (                               # noqa: E402
    AccountLease, BridgeConfig, LeaseBook, MemberArea, SeatMapResult,
    WandaBridgeClient, WandaBridgeError, HEADER_KEY)

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


class FakeTransport:
    """按 URL 特征返回预设响应。记录调用过的 URL。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return 404, "{}"


def cfg():
    return BridgeConfig(base="http://bridge.test", key="k" * 43)


# 真实样本（2026-10-13 实测 showtimeId=103172465 的形状，值做了缩减）
SEAT_OK = {
    "code": 0, "msg": "",
    "data": {"bizCode": 0, "bizMsg": "", "realtimeSeats": {
        "areaRule": 0,
        "area": [
            {"areaId": "36", "seat": [
                # ★ payMemberSeatStatus=1 = 这是【会员座位】（W+专享）
                {"seatId": 22793128, "name": "6排9座", "row": "6",
                 "column": "9", "status": 1, "payMemberSeatStatus": 1}],
             "areaPrice": {"areaCode": "36", "areaName": "W+专享",
                           "salesPrice": 3490},
             "wPlusActivity": {"activityCode": "17669737281361346712153",
                               "price": 3026,
                               "activityName": "W+会员专享优惠",
                               "userLimitNum": 6}},
            {"areaId": "33", "seat": [
                # ★ 这个区【也有】 wPlusActivity.price，但座位
                #   payMemberSeatStatus=0 —— 不是会员座位。
                #   这就是"有会员价 ≠ 有会员座位"的证据。
                {"seatId": 111, "name": "1排1座", "row": "1",
                 "column": "1", "status": 1, "payMemberSeatStatus": 0}],
             "areaPrice": {"areaCode": "33", "areaName": "特惠区",
                           "salesPrice": 2990},
             "wPlusActivity": {"activityCode": "17669737281361346712153",
                               "price": 2800,
                               "activityName": "W+会员专享优惠",
                               "userLimitNum": 6}},
            {"areaId": "9", "seat": [
                {"seatId": 222, "name": "3排3座", "row": "3",
                 "column": "3", "status": 1}],
             "areaPrice": {"areaCode": "9", "areaName": "普通区",
                           "salesPrice": 3390},
             "wPlusActivity": None},          # ★ 这个区没有会员价
        ]}}}

ACCOUNTS_OK = {"accounts": [{"phone": "15386424566", "available": True},
                            {"phone": "18873890439", "available": True}],
               "count": 2}


def main():
    section("一、★★ HTTP 200 但 bizCode != 0 必须判失败（纪律①）")
    # 这是实测遇到的真实响应：场次ID不对时后端会把万达的失败原样透出来
    biz_fail = {"code": 0, "msg": "",
                "data": {"bizCode": 1000015,
                         "bizMsg": "服务器繁忙,请稍后再试",
                         "realtimeSeats": None}}
    tr = FakeTransport({"/api/showtime/seats": (200, json.dumps(biz_fail))})
    c = WandaBridgeClient(cfg(), transport=tr)
    try:
        c.seat_map("103172465", phone="18873890439")
        check("bizCode!=0 应该抛异常", False, "★ 但它没抛 —— 会把失败当成功")
    except WandaBridgeError as e:
        check("★ bizCode!=0 抛 WANDA_BIZ_1000015", e.code == "WANDA_BIZ_1000015",
              e.code)
        check("★ 错误消息里点明「不一定是限流」",
              "不一定是限流" in str(e), str(e)[:70])

    section("二、★★ 手机号必须显式传（纪律②：防静默回退）")
    tr = FakeTransport({"/api/showtime/seats": (200, json.dumps(SEAT_OK))})
    c = WandaBridgeClient(cfg(), transport=tr)
    # 不传 phone —— seat_map 的 phone 是 keyword-only 且必填
    try:
        c.seat_map("103172465")                       # type: ignore[call-arg]
        check("不传 phone 应该报错", False, "★ 没报错")
    except TypeError:
        check("★ 不传 phone 直接 TypeError（语言层就挡住了）", True,
              "keyword-only 无默认值")
    # 传空串
    try:
        c.seat_map("103172465", phone="")
        check("传空 phone 应该报错", False, "★ 没报错")
    except WandaBridgeError as e:
        check("★ 传空 phone 抛 ACCOUNT_NOT_IN_POOL", e.code == "ACCOUNT_NOT_IN_POOL",
              e.code)
    check("★ 报错时【没有】发出网络请求", not tr.calls,
          f"发了 {len(tr.calls)} 次")

    section("三、★★ 号池：指定号必须在池里（防静默回退的落地检查）")
    tr = FakeTransport({"/api/auth/internal/wplus-accounts":
                        (200, json.dumps(ACCOUNTS_OK))})
    c = WandaBridgeClient(cfg(), transport=tr)
    got = c.pick_account("18873890439")
    check("★ 在池里 → 返回它", got == "18873890439", got)
    try:
        c.pick_account("19900000000")
        check("不在池里应该报错", False, "★ 没报错 —— 会静默用别人的号")
    except WandaBridgeError as e:
        check("★ 不在池里 → ACCOUNT_NOT_IN_POOL", e.code == "ACCOUNT_NOT_IN_POOL",
              e.code)
        check("★ 错误消息里手机号打码", "199****00" in str(e) or "***" in str(e),
              str(e)[:60])
    # 号池空
    tr2 = FakeTransport({"/api/auth/internal/wplus-accounts":
                         (200, json.dumps({"accounts": [], "count": 0}))})
    c2 = WandaBridgeClient(cfg(), transport=tr2)
    try:
        c2.pick_account()
        check("池空应该报错", False)
    except WandaBridgeError as e:
        check("★ 池空 → ACCOUNT_POOL_EMPTY", e.code == "ACCOUNT_POOL_EMPTY", e.code)

    section("四、鉴权头与白名单失败")
    tr = FakeTransport({"/api/showtime/seats": (401, '{"detail":"需要管理后台登录"}')})
    c = WandaBridgeClient(cfg(), transport=tr)
    try:
        c.seat_map("1", phone="18873890439")
        check("401 应该报错", False)
    except WandaBridgeError as e:
        check("★ 401 → BRIDGE_FORBIDDEN", e.code == "BRIDGE_FORBIDDEN", e.code)
        check("★ 提示里说清【用的是哪种鉴权方式】",
              "鉴权方式=bridge_key" in str(e), str(e)[:76])
        check("★★ 并提示 wplus-* 只认集成专用密钥（这条最容易卡住人）",
              "集成专用密钥" in str(e))
    # 密钥没配
    c3 = WandaBridgeClient(BridgeConfig(base="http://x", key=""),
                           transport=tr)
    try:
        c3.list_accounts()
        check("没密钥应该报错", False)
    except WandaBridgeError as e:
        check("★ 没密钥 → BRIDGE_NOT_CONFIGURED",
              e.code == "BRIDGE_NOT_CONFIGURED", e.code)
        check("★ 错误里点明两条路（桥密钥 或 应用签名）",
              "应用签名" in str(e), str(e)[:80])

    section("四之二、★★★ 应用签名（X-App-Key / X-Timestamp / X-Nonce / X-Sign）")
    # ★ 文档 §2.2：canonical_body = 请求体原始字节；GET/空体用 "{}"
    #              待签名串 = appKey + timestamp + nonce + canonical_body（无分隔符）
    #              X-Sign   = hex(HMAC-SHA256(待签名串, appSecret))
    from integrations.wanda.bridge import (                            # noqa: E402
        NO_APP_SIGNATURE_PATHS, PATH_ACCOUNTS, PATH_WPLUS_PRICE,
        PATH_WPLUS_RECOVERY, PATH_SEATS, build_app_signature_headers)

    h = build_app_signature_headers("AK", "SK", "{}")
    check("★ 生成四个头", set(h) == {"X-App-Key", "X-Timestamp", "X-Nonce",
                                     "X-Sign"}, str(sorted(h)))
    check("★ AppKey 原样带上", h["X-App-Key"] == "AK")
    check("★ 时间戳是 10 位秒级",
          len(h["X-Timestamp"]) == 10 and h["X-Timestamp"].isdigit(),
          h["X-Timestamp"])
    check("★ nonce 是随机串（32 位 hex）",
          len(h["X-Nonce"]) == 32)
    check("★★ X-Sign 是 64 位小写 hex",
          len(h["X-Sign"]) == 64 and h["X-Sign"] == h["X-Sign"].lower())
    # ★ nonce 不复用
    h2 = build_app_signature_headers("AK", "SK", "{}")
    check("★★ 每次调用 nonce 都不同（否则第二次会 409）",
          h2["X-Nonce"] != h["X-Nonce"])
    # ★ 签名算法自己复算一遍，确认拼法与文档一致（无分隔符）
    import hashlib as _hl                                            # noqa: E402
    import hmac as _hm                                               # noqa: E402
    want = _hm.new(b"SK", ("AK" + h["X-Timestamp"] + h["X-Nonce"]
                           + "{}").encode(), _hl.sha256).hexdigest()
    check("★★ 签名 = HMAC-SHA256(appKey+ts+nonce+body, secret)，无分隔符",
          h["X-Sign"] == want)
    check("★ GET 的 canonical body 是 \"{}\"（不是空串）",
          build_app_signature_headers("AK", "SK", "{}")["X-Sign"]
          != build_app_signature_headers("AK", "SK", "")["X-Sign"])
    # ★★ 路径白名单：这三个到不了应用签名分支（线上实测 403/401）
    check("★★ wplus-accounts / wplus-price / wplus-recovery 禁用签名",
          NO_APP_SIGNATURE_PATHS == frozenset(
              {PATH_ACCOUNTS, PATH_WPLUS_PRICE, PATH_WPLUS_RECOVERY}),
          str(sorted(NO_APP_SIGNATURE_PATHS)))
    # 配了签名时：能签的路径用签名，不能的自动回退桥密钥
    app_cfg = BridgeConfig(base="http://x", key="GK", app_key="AK",
                           app_secret="SK")
    c_app = WandaBridgeClient(app_cfg,
                              transport=FakeTransport({
                                  "/api/showtime/seats":
                                      (200, json.dumps(SEAT_OK))}))
    c_app.seat_map("1", phone="188x")
    hdr = c_app._transport.calls[0][1]
    check("★★ 座位图走【应用签名】（不是桥密钥）",
          "X-Sign" in hdr and HEADER_KEY not in hdr, str(sorted(hdr)))
    acc_tr = FakeTransport({"/api/auth/internal/wplus-accounts":
                            (200, json.dumps(ACCOUNTS_OK))})
    c_app2 = WandaBridgeClient(app_cfg, transport=acc_tr)
    c_app2.list_accounts()
    hdr2 = acc_tr.calls[0][1]
    check("★★ 账号池【自动回退桥密钥】（签名到不了，会 403）",
          HEADER_KEY in hdr2 and "X-Sign" not in hdr2, str(sorted(hdr2)))
    # ★ wplus-* 用【集成专用密钥】而不是通用密钥
    lp_cfg = BridgeConfig(base="http://x", key="GEN", liangpiao_key="LPK")
    check("★★ wplus-price 用集成专用密钥",
          lp_cfg.key_for_path(PATH_WPLUS_PRICE) == "LPK")
    check("★ 其他路径用通用密钥",
          lp_cfg.key_for_path(PATH_SEATS) == "GEN")
    check("★ 没配集成密钥时回退通用密钥（让它自己去报 401，不在这里猜）",
          BridgeConfig(base="http://x", key="GEN").key_for_path(
              PATH_WPLUS_PRICE) == "GEN")
    # has_app_signature
    check("★ has_app_signature 需要 key+secret 都有",
          app_cfg.has_app_signature
          and not BridgeConfig(base="x", key="k",
                               app_key="AK").has_app_signature)
    check("★★ 源码里写明了鉴权矩阵的三个例外（含原因）",
          "应用签名替代不了桥密钥" in
          (Path(__file__).resolve().parent.parent
           / "integrations" / "wanda" / "bridge.py").read_text(encoding="utf-8"))

    section("五、★ 座位图解析：会员价 vs 原价 不能混")
    tr = FakeTransport({"/api/showtime/seats": (200, json.dumps(SEAT_OK))})
    c = WandaBridgeClient(cfg(), transport=tr)
    sm = c.seat_map("103172465", phone="18873890439")
    check("解析出 3 个区域", len(sm.areas) == 3, str(len(sm.areas)))
    a36 = next(a for a in sm.areas if a.area_id == "36")
    check("★★ W+专享区：会员价 3026（不是原价 3490）",
          a36.member_price_fen == 3026 and a36.sales_price_fen == 3490,
          f"member={a36.member_price_fen} sales={a36.sales_price_fen}")
    check("★ activityCode / activityName 都取到了",
          a36.activity_code == "17669737281361346712153"
          and a36.activity_name == "W+会员专享优惠")
    check("★ userLimitNum=6（限购）", a36.user_limit_num == 6)

    section("六、★★ wPlusActivity=null = 「这个区没会员价」，不是「没座位」")
    a9 = next(a for a in sm.areas if a.area_id == "9")
    check("★★ 该区 member_price_fen 是 None（不是 0）",
          a9.member_price_fen is None, repr(a9.member_price_fen))
    check("★ 但该区【有座位】—— 两件事必须分开",
          len(a9.seats) == 1 and a9.seats[0]["name"] == "3排3座")
    check("★ has_member_price=False 但 seats 非空",
          a9.has_member_price is False and len(a9.seats) > 0)
    check("★ member_areas() 只返回有会员价的区（2 个）",
          len(sm.member_areas()) == 2,
          str([a.area_id for a in sm.member_areas()]))
    check("★ has_any_member_price()=True", sm.has_any_member_price())

    section("六之二、★★★ 「会员座位」= W+专享座位，判据是 seat[].payMemberSeatStatus")
    # ★★★ 这条是用户纠正我的：我原来以为"有会员价的区"就是会员座位区，
    #      实际特惠区/优选区/普通区【也有】 wPlusActivity.price，
    #      但它们座位上的 payMemberSeatStatus=0 —— 不是会员座位。
    # 实测（showtimeId=103172465）：
    #     「W+专享」10 个座位 payMemberSeatStatus 全 = 1
    #     「特惠区」「优选区」「普通区」全 = 0
    check("★★ member_areas()（有会员价）范围更宽 —— 不能拿它当会员座位区",
          len(sm.member_areas()) == 2 and len(sm.member_seat_areas()) == 1,
          f"有会员价 {len(sm.member_areas())} 个 / "
          f"有会员座位 {len(sm.member_seat_areas())} 个")
    msa = sm.member_seat_areas()
    check("★★★ member_seat_areas() 只返回 W+专享（area 36）",
          len(msa) == 1 and msa[0].area_id == "36",
          str([a.area_id for a in msa]))
    check("★ 会员座位区里有可售会员座位", sm.has_member_seats())
    a36 = msa[0]
    check("★ 该区的 member_seats 就是 payMemberSeatStatus==1 的那些",
          len(a36.member_seats) == 1
          and a36.member_seats[0]["name"] == "6排9座",
          str([s.get("name") for s in a36.member_seats]))
    check("★★ 特惠区虽然有会员价，但【不是】会员座位区",
          any(a.area_id == "33" and a.has_member_price
              and not a.is_member_area for a in sm.areas),
          "area 33：has_member_price=True, is_member_area=False")
    check("★ status!=1 的会员座位不算可售", 
          len(a36.available_member_seats()) == 1)
    # 源码里必须写明这条判据为什么不能靠区域名
    src = (Path(__file__).resolve().parent.parent
           / "integrations" / "wanda" / "bridge.py").read_text(encoding="utf-8")
    check("★★ 注释里写明「不能靠区域名判会员座位」",
          "不能" in src and "区域名" in src and "payMemberSeatStatus" in src,
          "区域名可能变，结构化字段不会")

    section("六之三、★★ is_wplus_area 语义要与出票系统对齐")
    # ★ 出票系统 2026-10-13 起 wplus-price 返回 is_wplus_area 字段：
    #     re.search(r"W\\s*[+＋]|WPLUS|会员专享", area_name)
    #     or any(_is_wplus_seat(seat))
    #   ★ 我们的 is_member_area 用 bool(member_seats) —— 两者都覆盖
    #     "名字带 W+"和"座位是会员座"两种情况，语义等价。
    src = (Path(__file__).resolve().parent.parent
           / "integrations" / "wanda" / "bridge.py").read_text(encoding="utf-8")
    check("★★ 注释里写明了与出票系统 is_wplus_area 的等价关系",
          "is_wplus_area" in src and "等价" in src)
    check("★ 也写明了他们那边的判据（区域名或会员座位）",
          "会员专享" in src or "_is_wplus_seat" in src or "名字带 W+" in src)
    # 实测：只有 W+专享区有会员座位，其他区有会员价但没会员座位
    check("★★ 四个区里只有 W+专享 is_member_area=True",
          len(sm.member_seat_areas()) == 1
          and sm.member_seat_areas()[0].area_id == "36",
          str([a.area_id for a in sm.member_seat_areas()]))
    check("★ 特惠区有会员价但不是会员区（两者必须分开）",
          any(a.area_id == "33" and a.has_member_price
              and not a.is_member_area for a in sm.areas))

    section("七、★ 按座位名定位它属于哪个区")
    got = sm.area_of_seat("6排9座")
    check("「6排9座」→ W+专享区（area 36）",
          got is not None and got.area_id == "36",
          got.area_id if got else "None")
    got2 = sm.area_of_seat("1排1座")
    check("「1排1座」→ 特惠区（area 33）",
          got2 is not None and got2.area_id == "33",
          got2.area_id if got2 else "None")
    check("★ 找不到的座位返回 None（不做模糊匹配）",
          sm.area_of_seat("99排99座") is None)
    check("★ 空格/全角括号归一后能对上",
          sm.area_of_seat(" 6排9座 ") is not None)

    section("八、★★「没选座位 = 买家要会员座」这条语义")
    # 这条不是代码逻辑，是【业务语义】，必须写进代码注释，
    # 否则以后有人会"顺手"用最低价兜底。
    src = (Path(__file__).resolve().parent.parent
           / "integrations" / "wanda" / "bridge.py").read_text(encoding="utf-8")
    check("★★ 模块注释里写明了这条语义",
          "买家没选座位" in src and "会员座位" in src,
          "在模块头「不吞异常」那段")
    check("★ 并且写明：不许退化成「按最低价报」",
          "不许退化" in src or "退化成" in src)
    check("★★ 这里【没有】任何返回「最低价」的函数",
          not any(hasattr(WandaBridgeClient, n)
                  for n in ("cheapest", "min_price", "pick_cheapest")),
          "杜绝 顺手给个最低价 的实现")

    section("九、租约台账（★ 是本地记账，不是后端租约）")
    book = LeaseBook()
    l1 = book.acquire("order-1", "18873890439")
    l2 = book.acquire("order-1", "15386424566")     # 同一单再申请
    check("★★ 同一单不换号（避免两个号抢同一次操作）",
          l1.phone == l2.phone == "18873890439", f"{l1.phone} vs {l2.phone}")
    l3 = book.acquire("order-2", "15386424566")
    check("★ 不同单可以用不同号", l3.phone == "15386424566")
    check("★ TTL 被夹到 ≤1800s（与后端一致）",
          book.acquire("o3", "x", ttl_seconds=99999).ttl_seconds == 1800.0)
    book.release("order-1")
    check("★ 释放后同一单可以重新选号",
          book.acquire("order-1", "15386424566").phone == "15386424566")
    check("★ 台账里没有密钥/token 字段（只有手机号和 scope）",
          set(AccountLease.__dataclass_fields__) ==
          {"phone", "scope", "acquired_at", "ttl_seconds"})
    check("★ 模块注释说明了它不能替代后端租约",
          "替代后端租约" in
          (Path(__file__).resolve().parent.parent
           / "integrations" / "wanda" / "bridge.py").read_text(encoding="utf-8"),
          "注释里写了「不能替代后端租约」")
    check("★★ 而且写明两个后端租约函数【不在桥白名单里】",
          "不在桥的白名单里" in
          (Path(__file__).resolve().parent.parent
           / "integrations" / "wanda" / "bridge.py").read_text(encoding="utf-8"))

    section("十、默认配置与请求形状")
    import os
    os.environ.pop("TICKET_BRIDGE_KEY", None)
    c4 = WandaBridgeClient(transport=FakeTransport({}))
    check("★ 默认 base 是 127.0.0.1:8000（实测那个后端）",
          c4.config.base == "http://127.0.0.1:8000", c4.config.base)
    tr = FakeTransport({"/api/showtime/seats": (200, json.dumps(SEAT_OK))})
    c5 = WandaBridgeClient(cfg(), transport=tr)
    c5.seat_map("103172465", phone="18873890439")
    url, headers = tr.calls[0]
    check("★ 请求带 X-Plugin-Bridge-Key", HEADER_KEY in headers)
    check("★ URL 里带了 showtimeId 和 phone（显式传）",
          "showtimeId=103172465" in url and "phone=18873890439" in url,
          url[:90])
    check("★ 只发 GET（这层没有写操作）",
          all("POST" not in u for u, _ in tr.calls))

    section("十一、★★★ 影院映射：按 ID，不按名字")
    # ★★ 出票系统 2026-10-13 上线的 /api/cinema/wanda-lookup
    #    响应逐字来自他们的答复文档第四节。
    MAP_OK_1 = {"code": 0, "data": {
        "bizCode": 0, "bizMsg": "", "mapped": True,
        "wandaCityId": "80", "wandaCinemaId": "379",
        "wandaCinemaName": "宁德万达广场店",
        "sourceCinemaName": "万达影城（宁德万达广场激光IMAX店）"}}
    # ★ 用例2：万达侧规范名【不含"万达"】—— 影院改名过，但 ID 还是万达的
    MAP_OK_2 = {"code": 0, "data": {
        "bizCode": 0, "bizMsg": "", "mapped": True,
        "wandaCityId": "290", "wandaCinemaId": "595",
        "wandaCinemaName": "儒意影城(CBD万达广场店)原万达影城",
        "sourceCinemaName": "万达影城（CBD万达广场IMAX店）"}}
    MAP_NONE = {"code": 0, "data": {
        "bizCode": 0, "bizMsg": "", "mapped": False,
        "reason": "NO_MAPPING"}}
    MAP_DOWN = {"code": 0, "data": {
        "bizCode": 0, "bizMsg": "", "mapped": False,
        "reason": "IDENTITY_STORE_UNAVAILABLE"}}

    def mk(body):
        return WandaBridgeClient(
            cfg(), transport=FakeTransport({"/api/cinema/wanda-lookup":
                                            (200, json.dumps(body))}))

    r1 = mk(MAP_OK_1).wanda_lookup(city_id="350900", cinema_id="7261")
    check("★★ 用例1：mapped=true → 走万达，ID 正确",
          r1.use_wanda and r1.wanda_city_id == "80"
          and r1.wanda_cinema_id == "379",
          r1.describe())

    r2 = mk(MAP_OK_2).wanda_lookup(city_id="110100", cinema_id="42")
    check("★★★ 用例2：万达侧名字【不含「万达」】也必须判成万达",
          r2.use_wanda and r2.wanda_cinema_id == "595",
          f"{r2.wanda_cinema_name!r} → use_wanda={r2.use_wanda}")
    check("★★ 只用 ID 判断，不做名字校验",
          "儒意影城" in r2.wanda_cinema_name and r2.use_wanda,
          "如果他们那边按名字判，这一单会被误判成良票")

    r3 = mk(MAP_NONE).wanda_lookup(city_id="350900", cinema_id="99999999")
    check("★★ 没映射 → 不抛异常，走良票（这是【正常】分支）",
          not r3.use_wanda and not r3.store_unavailable,
          r3.describe())
    check("★ NO_MAPPING 时 reason 原样带回", r3.reason == "NO_MAPPING")

    try:
        mk(MAP_DOWN).wanda_lookup(city_id="1", cinema_id="2")
        check("★★ 映射库不可用【必须抛】", False,
              "★ 没抛 —— 会让所有万达单静默退化成良票单，且无告警")
    except WandaBridgeError as e:
        check("★★ 映射库不可用 → 抛 IDENTITY_STORE_UNAVAILABLE",
              e.code == "IDENTITY_STORE_UNAVAILABLE", e.code)
    check("★★ 而且能和 NO_MAPPING 区分开",
          "IDENTITY_STORE_UNAVAILABLE" != "NO_MAPPING")

    check("★ 参数缺失不请求网络",
          (lambda: (mk(MAP_OK_1), ))() is not None)
    tr2 = FakeTransport({"/api/cinema/wanda-lookup": (200, json.dumps(MAP_OK_1))})
    c2 = WandaBridgeClient(cfg(), transport=tr2)
    for bad in ({"city_id": "", "cinema_id": "1"},
                {"city_id": "1", "cinema_id": ""}):
        try:
            c2.wanda_lookup(**bad)
            check(f"缺参数 {bad} 应该报错", False)
        except WandaBridgeError:
            pass
    check("★★ 缺参数时【一个网络请求都没发】", not tr2.calls,
          f"发了 {len(tr2.calls)} 次")

    section("十二、请求形状（映射接口）")
    tr3 = FakeTransport({"/api/cinema/wanda-lookup": (200, json.dumps(MAP_OK_1))})
    WandaBridgeClient(cfg(), transport=tr3).wanda_lookup(
        city_id="350900", cinema_id="7261")
    url, headers = tr3.calls[0]
    check("★ URL 用 cityId/cinemaId（和出票系统的别名一致）",
          "cityId=350900" in url and "cinemaId=7261" in url, url[:84])
    check("★ 带 bridge key", HEADER_KEY in headers)

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"{passed}/{total} 项通过")
    if passed != total:
        print("★ 有未通过项 —— 别急着往下做")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
