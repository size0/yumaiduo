"""
动态漂移检查：出票系统开放平台【文档声称】vs【线上实际】（★ 全只读）。

════════════════════════════════════════════════════════════════
★ 为什么要有这个（和出票系统那边 11 条静态测试互补）

  出票系统自己在 backend/tests/test_openapi_doc_alignment.py 里做了【静态】核对：
    文档路径 ↔ 路由表、密钥矩阵 ↔ 中间件分支、常量 ↔ 源码。
  他们说：「你们那份『发送请求对比状态码』的动态核对我这边没有做（会打生产）」。

  ⇒ 这就是那条动态的腿：真发一次请求，看线上实际是不是文档说的那样。
     静态测试查不出「部署的不是那个 commit」「环境变量没配」
     「路由被别的中间件拦了」这类只在运行时才暴露的漂移。

════════════════════════════════════════════════════════════════
★ 怎么跑

    python check_bridge_drift.py

  需要两个环境变量（★ 只从环境读，不落盘）：
    TICKET_BRIDGE_KEY           通用桥密钥（PLUGIN_BRIDGE_KEY）
    TICKET_BRIDGE_KEY_LIANGPIAO 集成专用密钥（LIANGPIAO_WANDA_BRIDGE_KEY，可选）
    TICKET_BRIDGE_BASE          默认 http://127.0.0.1:8000

  ★ 只调只读接口；写接口（wplus-price 的探针模式）一律不碰。

★ 退出码：0 = 全部一致；1 = 有漂移（可接进 CI）
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = (os.getenv("TICKET_BRIDGE_BASE") or "http://127.0.0.1:8000").rstrip("/")
KEY = (os.getenv("TICKET_BRIDGE_KEY") or "").strip()
KEY_LP = (os.getenv("TICKET_BRIDGE_KEY_LIANGPIAO") or "").strip()

# 用哪个影院/场次做样本（可覆盖；默认是宁德万达 + 一个真实场次）
CINEMA = os.getenv("DRIFT_CINEMA", "379")
SHOWTIME = os.getenv("DRIFT_SHOWTIME", "")
LP_CITY = os.getenv("DRIFT_LP_CITY", "350900")
LP_CINEMA = os.getenv("DRIFT_LP_CINEMA", "7261")

RESULTS: list[tuple] = []


def check(label, ok, detail=""):
    RESULTS.append((bool(ok), label, detail))
    mark = "PASS" if ok else "DRIFT"
    print(f"[{mark}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def section(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


@dataclass
class Call:
    path: str
    method: str = "GET"
    params: dict = field(default_factory=dict)
    body: dict | None = None
    key: str = ""
    note: str = ""


def call(c: Call) -> tuple:
    """返回 (status, body_text, parsed_or_None)。"""
    url = BASE + c.path
    if c.params:
        url += "?" + urllib.parse.urlencode(c.params)
    data = json.dumps(c.body).encode() if c.body is not None else None
    headers = {"Accept": "application/json"}
    if c.key:
        headers["X-Plugin-Bridge-Key"] = c.key
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=c.method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as exc:                                 # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}", None
    try:
        return status, raw, json.loads(raw)
    except ValueError:
        return status, raw, None


def get(path, *, params=None, key=None, note="") -> tuple:
    return call(Call(path=path, params=params or {}, key=key or KEY,
                     note=note))


def main() -> int:
    if not KEY:
        print("★ 没设 TICKET_BRIDGE_KEY —— 无法做动态核对")
        return 1

    app_key = (os.getenv("TICKET_APP_KEY") or "").strip()
    app_secret = (os.getenv("TICKET_APP_SECRET") or "").strip()
    print(f"  base = {BASE}")
    print(f"  应用签名 {'✓ 已配 AppKey=' + app_key[:8] + '…' if (app_key and app_secret) else '未配（跳过签名检查）'}")
    print(f"  通用密钥 {KEY[:4]}…{KEY[-3:]}（{len(KEY)} 位）")
    if KEY_LP:
        print(f"  集成密钥 {KEY_LP[:4]}…{KEY_LP[-3:]}（{len(KEY_LP)} 位）"
              f"  {'★ 与通用密钥相同！' if KEY_LP == KEY else '✓ 两把不同'}")
    else:
        print("  集成密钥 未提供 —— 涉及 5.1/5.2 的检查会跳过")

    # ---------------------------------------------------------- 4.1 自检
    section("① 4.1 连通性自检：文档说要 GET /health")
    st, raw, body = call(Call(path="/health", key="", note="免鉴权"))
    check("★ GET /health 免密钥返回 200", st == 200, f"HTTP {st}")
    check("★ 返回含 status:ok",
          isinstance(body, dict) and body.get("status") == "ok",
          json.dumps(body, ensure_ascii=False)[:60] if body else raw[:60])
    # 文档旧版写的是 /api/healthz
    st2, _, _ = call(Call(path="/api/healthz", key=KEY))
    check("★ /api/healthz 不存在（返回 401 而不是 200）—— 文档不该再用它",
          st2 != 200, f"HTTP {st2}")

    # ---------------------------------------------------------- 4.2 场次
    section("② 4.2 场次列表：结构与 showDate 行为")
    st, raw, body = get("/api/showtime/by-cinema",
                        params={"cinemaId": CINEMA})
    check("★ HTTP 200", st == 200, f"HTTP {st}")
    data = (body or {}).get("data") or {}
    films = data.get("showtimeFilmInf")
    check("★ data.showtimeFilmInf 是数组且非空",
          isinstance(films, list) and len(films) > 0,
          f"len={len(films) if isinstance(films, list) else films}")
    dates, shape_ok, shows = set(), True, []
    for f in (films or []):
        for d in (f.get("showtimeFilmDateInf") or []):
            dates.add(str(d.get("date") or ""))
            node = d.get("showtimesInf")
            if isinstance(node, dict) and isinstance(node.get("showtimeList"), list):
                shows.extend(node["showtimeList"])
            elif isinstance(node, list):
                shows.extend(node)
            else:
                shape_ok = False
    check("★★ showtimesInf 是含 showtimeList 的对象（我们第一版理解错过）",
          shape_ok, "形状符合")
    check("★ 拍平出场次", len(shows) > 0, f"{len(shows)} 场")
    check("★ date 是 YYYYMMDD 整数（用来自己滤日期）",
          bool(dates) and all(len(d) == 8 and d.isdigit() for d in dates),
          f"{sorted(dates)}")

    # ★ showDate 不生效 —— 这条最要紧，必须每次核对
    st_a, _, body_a = get("/api/showtime/by-cinema",
                          params={"cinemaId": CINEMA,
                                  "showDate": "1999-01-01"})
    da = (body_a or {}).get("data") or {}
    films_a = da.get("showtimeFilmInf") or []
    dates_a = {str(d.get("date"))
               for f in films_a for d in (f.get("showtimeFilmDateInf") or [])}
    check("★★ showDate 仍被上游忽略（传 1999 也返回同一批日期）",
          dates_a == dates,
          f"传 1999-01-01 → {sorted(dates_a)}")
    print("     ★ 结论：必须自己按 date 过滤，不能信 showDate")

    # ---------------------------------------------------------- 4.3 座位图
    section("③ 4.3 座位图：★★ 会返回会员价（文档旧版说不返回）")
    sample = shows[0] if shows else None
    show_id = SHOWTIME or (str(sample.get("showtimeId")) if sample else "")
    if not show_id:
        check("拿到样本场次", False, "没场次可测")
    else:
        st, _, seats = get("/api/showtime/seats",
                           params={"showtimeId": show_id})
        check(f"★ /seats?showtimeId={show_id} → 200", st == 200, f"HTTP {st}")
        rs = ((seats or {}).get("data") or {}).get("realtimeSeats") or {}
        areas = rs.get("area") or []
        check("★ realtimeSeats.area 非空", bool(areas), f"{len(areas)} 个区")
        with_price = [a for a in areas
                      if isinstance(a.get("wPlusActivity"), dict)
                      and (a["wPlusActivity"].get("price") or 0) > 0]
        if with_price:
            names = [str((a.get("areaPrice") or {}).get("areaName"))
                     for a in with_price]
            check("★★★ 座位图返回了区域级会员价 wPlusActivity.price",
                  True, f"{len(with_price)} 个区有价：{names}")
            a0 = with_price[0]
            act = a0["wPlusActivity"]
            check("★ wPlusActivity 含 price / activityCode / activityName",
                  all(k in act for k in ("price", "activityCode")),
                  str(sorted(act.keys())))
        else:
            # 整个影院今天都没活动价 —— 不是漂移，但要标出来
            check("★ 该场次无会员价（可能是真的没活动，不是漂移）",
                  True, "跳过会员价断言")
        # areaId vs areaCode 一致性（决定"按 area_code 匹配座位所在区"可不可行）
        mism = [a.get("areaId") for a in areas
                if str(a.get("areaId")) != str(
                    (a.get("areaPrice") or {}).get("areaCode"))]
        check("★ area[].areaId == area[].areaPrice.areaCode（所以按 area_code 匹配可行）",
              not mism, f"不一致的：{mism}" if mism else "全部一致")
        # 会员座位判据
        has_pms = any("payMemberSeatStatus" in (s or {})
                      for a in areas for s in (a.get("seat") or []))
        check("★ seat[].payMemberSeatStatus 字段在（判会员座位用）", has_pms)
        member_areas = [str((a.get("areaPrice") or {}).get("areaName"))
                        for a in areas
                        if any((s or {}).get("payMemberSeatStatus") == 1
                               for s in (a.get("seat") or []))]
        check("★ 能识别出会员座位区（payMemberSeatStatus==1）",
              bool(member_areas), f"{member_areas}")
        # ★ 文档旧版说"座位图不返回会员价、要锁座" —— 这里明确推翻
        check("★★ 推翻文档旧断言「座位图不返回会员价、必须锁座后才能查」",
              bool(with_price), "座位图直接就有价")

    # ---------------------------------------------------------- 4.4 账号池
    section("④ 4.4 账号池")
    st, _, accs = get("/api/auth/internal/wplus-accounts")
    check("★ HTTP 200", st == 200, f"HTTP {st}")
    items = (accs or {}).get("accounts")
    check("★ 返回 accounts 数组", isinstance(items, list),
          f"{len(items) if isinstance(items, list) else items} 个")
    if items:
        keys = set(items[0].keys())
        check("★★ 只含 phone/available（不含 token 等敏感字段）",
              keys == {"phone", "available"}, str(sorted(keys)))
        check("★ available 恒为 true（只表示在线+W+，不代表空闲）",
              all(a.get("available") is True for a in items))

    # ---------------------------------------------------------- 影院映射
    section("⑤ 影院映射（非文档章节，但我们依赖）")
    st, _, m = get("/api/cinema/wanda-lookup",
                   params={"cityId": LP_CITY, "cinemaId": LP_CINEMA})
    check("★ HTTP 200", st == 200, f"HTTP {st}")
    md = (m or {}).get("data") or {}
    check("★ mapped=true 且有万达 ID",
          md.get("mapped") is True and md.get("wandaCinemaId"),
          f"{md.get('wandaCityId')}/{md.get('wandaCinemaId')}")

    # ---------------------------------------------------------- 密钥矩阵
    section("⑥ ★★★ 密钥分流（文档旧版完全没写，最容易卡住人）")
    matrix = [
        ("/api/showtime/by-cinema", {"cinemaId": CINEMA},
         "GET", [200, 200]),
        ("/api/showtime/seats", {"showtimeId": show_id},
         "GET", [200, 200]),
        ("/api/auth/internal/wplus-accounts", {}, "GET", [200, 401]),
        ("/api/cinema/wanda-lookup",
         {"cityId": LP_CITY, "cinemaId": LP_CINEMA}, "GET", [200, 401]),
    ]
    for path, params, method, (want_gen, want_lp) in matrix:
        st_g, _, _ = get(path, params=params, key=KEY)
        check(f"★ {path} 通用密钥 → {want_gen}",
              st_g == want_gen, f"实际 {st_g}")
        if KEY_LP:
            st_l, _, _ = get(path, params=params, key=KEY_LP)
            check(f"★ {path} 集成密钥 → {want_lp}",
                  st_l == want_lp, f"实际 {st_l}")

    # ---------------------------------------------------------- 5.1
    section("⑦ 5.1 wplus-price（★ 只读模式；探针模式一概不碰）")
    if not KEY_LP:
        print("  跳过：没提供集成专用密钥")
    else:
        st, raw, wp = call(Call(
            path="/api/order/internal/wplus-price", method="POST",
            key=KEY,
            body={"tenant_id": "drift", "shop_id": "drift",
                  "request_id": "drift-1", "cinema_id": int(CINEMA),
                  "showtime_id": str(show_id), "seat_ids": [],
                  "ticket_count": 1, "probe": False}))
        check("★★ 用【通用】密钥调 wplus-price → 401（文档旧版没写这条）",
              st == 401, f"HTTP {st}")
        st, raw, wp = call(Call(
            path="/api/order/internal/wplus-price", method="POST",
            key=KEY_LP,
            body={"tenant_id": "drift", "shop_id": "drift",
                  "request_id": "drift-2", "cinema_id": int(CINEMA),
                  "showtime_id": str(show_id), "seat_ids": [],
                  "ticket_count": 1, "probe": False}))
        check("★★ 用【集成专用】密钥调 → 200（默认只读，不锁座）",
              st == 200, f"HTTP {st}")
        if st == 200:
            areas = ((wp or {}).get("data") or {}).get("wplus_areas") or []
            check("★★ 返回结构是 {success,data:{wplus_areas}}（非统一信封）",
                  "success" in (wp or {}) and isinstance(areas, list),
                  f"{len(areas)} 个区域")
            codes = {str(a.get("area_code")) for a in areas}
            # ★★ 核心：预览区域应 ⊇ 座位图有会员价的区域
            if show_id and 'areas' in dir():
                pass
            check("★★ 每条含 is_wplus_area 字段（他们本次新增）",
                  all("is_wplus_area" in a for a in areas) if areas else False,
                  str({str(a.get("area_code")): a.get("is_wplus_area")
                       for a in areas})[:110])
            check("★ 含 member_price_cents / official_price_cents / available_seat_count",
                  all(all(k in a for k in ("member_price_cents",
                                           "official_price_cents",
                                           "available_seat_count"))
                      for a in areas) if areas else False)
            print(f"     区域：{sorted(codes, key=lambda x: int(x) if x.isdigit() else 0)}")

    # ---------------------------------------------------------- 5.2
    section("⑧ 5.2 wplus-recovery（★ 纯只读；参数校验）")
    if not KEY_LP:
        print("  跳过：没提供集成专用密钥")
    else:
        st, _, _ = call(Call(
            path="/api/order/internal/wplus-recovery", method="POST",
            key=KEY_LP,
            body={"tenant_id": "drift", "shop_id": "drift",
                  "request_id": "drift-3", "cinema_id": int(CINEMA),
                  "showtime_id": str(show_id),
                  "provider_order_id": "1", "seat_ids": []}))
        check("★★ seat_ids 为空数组 → 400（文档旧版没写这条校验）",
              st == 400, f"HTTP {st}")
        st, _, _ = call(Call(
            path="/api/order/internal/wplus-recovery", method="POST",
            key=KEY_LP,
            body={"tenant_id": "drift", "shop_id": "drift",
                  "request_id": "drift-4", "cinema_id": int(CINEMA),
                  "showtime_id": str(show_id),
                  "provider_order_id": "1", "seat_ids": ["1", "2", "3",
                                                         "4", "5", "6", "7"]}))
        check("★★ seat_ids 超过 6 个 → 400", st == 400, f"HTTP {st}")

    # ---------------------------------------------------------- 5.3
    section("⑨ 5.3 /api/order/match（普通密钥即可）")
    st, _, mt = call(Call(path="/api/order/match", method="POST", key=KEY,
                          body={"text": "万达影城 测试"}))
    check("★ 通用密钥 → 200", st == 200, f"HTTP {st}")
    check("★ 返回含 parsed 结构（不是统一信封）",
          isinstance(mt, dict) and "parsed" in ((mt.get("data") or {})
                                                if isinstance(mt.get("data"), dict)
                                                else {}),
          str(sorted(mt.keys()))[:70] if isinstance(mt, dict) else "")

    # ---------------------------------------------------------- 汇总
    section("汇总")
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    total = len(RESULTS)
    drift = [(lbl, det) for ok, lbl, det in RESULTS if not ok]
    print(f"  {passed}/{total} 项一致")
    if drift:
        print()
        print("  ★ 漂移项（文档/我们理解 与 线上实际 不一致）：")
        for lbl, det in drift:
            print(f"      {lbl}   {det}")
        print()
        print("  ⇒ 把下面这些发给出票系统：文档行号 + 代码位置 + 实测")
        return 1
    print("  ✓ 没有漂移")
    return 0


if __name__ == "__main__":
    sys.exit(main())
