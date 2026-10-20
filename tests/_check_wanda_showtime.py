"""
万达场次解析测试（★ 不打网络，用真实响应结构）。

★ 最要紧的一条：`showDate` 参数被万达【忽略】，
  必须自己按 showtimeFilmDateInf[].date 过滤。
  不滤的后果不是报错，是【静默拿错天的价】—— 最难发现的一类错。
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

from application.wanda_showtime import (                              # noqa: E402
    DEFAULT_TOLERANCE_MINUTES, WandaShowtimeError, fetch_shows,
    normalize_date, normalize_minutes, parse_shows, pick_show)
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


# ---- 真实响应结构（cinemaId=379 实测，字段名逐字照抄）----
PAYLOAD = {"code": 0, "msg": "API Success.",
           "data": {"bizCode": 0, "bizMsg": "", "showtimeFilmInf": [
               {"filmId": "900002313", "extFilmId": "34211", "effective": 440,
                "showtimeFilmDateInf": [
                    # ★ showtimesInf 是【对象】（含 showtimeList），不是数组
                    #   —— 我第一版当成数组，结果静默解析出 0 场
                    {"date": 20261013, "showtimesInf": {
                        "filmId": 900002313, "extFilmId": 34211,
                        "showtimeCount": 2,
                        "showtimeList": [
                            {"showtimeId": 103155648,
                             "realtime": 1791872400000,     # 2026-10-13 14:20
                             "hallName": "8号激光IMAX厅-全新升级",
                             "hallType": "IMAX厅", "hallId": 2380,
                             "salesPrice": 4790, "wPlusActivityPrice": 4500,
                             "wPlusActivityCode": "17669737281361346712153",
                             "status": 1,
                             "filmList": [{"filmName": "生化危机：爆发夜",
                                           "duration": 94}]},
                            {"showtimeId": 103155700,
                             "realtime": 1791876000000,     # 15:20
                             "hallName": "3号厅", "salesPrice": 3990,
                             "wPlusActivityPrice": 3800, "status": 1,
                             "filmList": [{"filmName": "生化危机：爆发夜"}]},
                        ]}},
                    {"date": 20261014, "showtimesInf": {
                        "showtimeCount": 1,
                        "showtimeList": [
                            {"showtimeId": 999999999,
                             "realtime": 1791958800000,     # 次日 14:20
                             "hallName": "8号激光IMAX厅-全新升级",
                             "salesPrice": 4790, "wPlusActivityPrice": 4500,
                             "status": 1,
                             "filmList": [{"filmName": "生化危机：爆发夜"}]},
                        ]}}]}]}}


def main():
    shows = parse_shows(PAYLOAD)

    section("一、★ 拍平嵌套结构（影片 → 日期 → 场次）")
    check("★ 拍平出 3 场（2 场今天 + 1 场明天）", len(shows) == 3, str(len(shows)))
    check("★ 日期来自 showtimeFilmDateInf[].date（整数 → 字符串）",
          {s.date for s in shows} == {"20261013", "20261014"},
          str(sorted({s.date for s in shows})))
    check("★ realtime 毫秒时间戳解成 14:20",
          shows[0].starts_at_text == "2026-10-13 14:20", shows[0].starts_at_text)
    check("★ 影片名来自场次自带的 filmList[0].filmName",
          shows[0].film_name == "生化危机：爆发夜", shows[0].film_name)
    check("★ 会员价/原价都取到了",
          shows[0].wplus_price_fen == 4500 and shows[0].sales_price_fen == 4790)
    check("★ 厅名/厅型/ID 都取到了",
          shows[0].hall_name == "8号激光IMAX厅-全新升级"
          and shows[0].hall_type == "IMAX厅"
          and shows[0].showtime_id == "103155648")
    check("★ 保留原始 dict（排查时要看全文）",
          isinstance(shows[0].raw, dict) and "showtimeId" in shows[0].raw)

    section("二、★★★ 最要紧：必须自己滤日期（showDate 参数无效）")
    # ★ 实测：传 showDate=2026-10-13 / 2026-10-01 / 1999-01-01，
    #   万达返回的都是同一批日期（它当前开放的整个范围）。
    a = pick_show(shows, date="20261013", minutes=860,
                  film_name="生化危机：爆发夜")
    b = pick_show(shows, date="20261014", minutes=860,
                  film_name="生化危机：爆发夜")
    check("★★★ 同影片同厅、只差一天 → 两个不同的 showtimeId",
          a.showtime_id != b.showtime_id,
          f"10-13={a.showtime_id}  10-14={b.showtime_id}")
    check("★★ 所以不滤日期会【静默拿错天的价】",
          a.showtime_id == "103155648" and b.showtime_id == "999999999")
    check("★ 滤不到的日期 → NO_SHOWS_FOR_DATE（不是返回一个错的）", True)
    try:
        pick_show(shows, date="20261020", minutes=860)
        check("不存在的日期应该报错", False, "★ 没报错 —— 会拿错天的场次")
    except WandaShowtimeError as e:
        check("★ 不存在的日期 → NO_SHOWS_FOR_DATE",
              e.code == "NO_SHOWS_FOR_DATE", e.code)
        check("★★ detail 里列出接口实际返回了哪些日期（便于排查）",
              e.detail.get("have_dates") == ["20261013", "20261014"],
              str(e.detail.get("have_dates")))

    section("三、★ 匹配用时间戳，不用字符串比时间")
    check("★ starts_at 是从 realtime 毫秒戳解出来的",
          shows[0].realtime_ms == 1791872400000)
    # 各种时间写法都要能归一
    for raw, want in (("14:20", 860), ("14:20:00", 860), ("1420", 860),
                      (860, 860), ("14：20", 860),
                      (1791872400000, 860)):
        got = normalize_minutes(raw)
        check(f"★ {raw!r} → {want} 分", got == want, str(got))
    check("★ 归不了一律返回 None（不返回一个像时间的数）",
          normalize_minutes("下午两点") is None
          and normalize_minutes("") is None
          and normalize_minutes("99:99") is None)

    section("四、★ 容差窗口（默认 ±3 分钟）")
    check(f"★ 默认容差 = {DEFAULT_TOLERANCE_MINUTES} 分钟",
          DEFAULT_TOLERANCE_MINUTES == 3)
    for delta, should in ((0, True), (2, True), (-2, True),
                          (3, True), (-3, True), (4, False), (-4, False)):
        try:
            pick_show(shows, date="20261013", minutes=860 + delta,
                      film_name="生化危机：爆发夜")
            got = True
        except WandaShowtimeError:
            got = False
        check(f"★ {delta:+d} 分 → {'匹配' if should else '不匹配'}",
              got == should)
    check("★ 容差可调（下面单独验 0 的情况）", True)
    try:
        pick_show(shows, date="20261013", minutes=862,
                  film_name="生化危机：爆发夜", tolerance_minutes=0)
        check("容差 0 时 +2 分应该不匹配", False)
    except WandaShowtimeError:
        check("★ 容差 0 时 +2 分不匹配（可调）", True)

    section("五、★★ 歧义保护：多候选不猜")
    AMB = {"data": {"showtimeFilmInf": [{"showtimeFilmDateInf": [
        {"date": 20261013, "showtimesInf": {
            "showtimeCount": 2,
            "showtimeList": [
                {"showtimeId": 1, "realtime": 1791872400000,
                 "filmList": [{"filmName": "X"}]},
                {"showtimeId": 2, "realtime": 1791872400000,
                 "filmList": [{"filmName": "X"}]},
            ]}}]}]}}
    try:
        pick_show(parse_shows(AMB), date="20261013", minutes=860,
                  film_name="X")
        check("同时间两场应该报歧义", False, "★ 没报 —— 会随机报一个价")
    except WandaShowtimeError as e:
        check("★★ 同时间多场 → SHOW_AMBIGUOUS（不猜）",
              e.code == "SHOW_AMBIGUOUS", e.code)
        check("★ detail 列出候选让人能看",
              len(e.detail.get("candidates") or []) == 2,
              str(e.detail.get("candidates")))

    section("六、★ 放宽顺序（严格 → 宽松），且都先滤日期")
    # ① 影片名对不上时退到"日期+时间"
    s = pick_show(shows, date="20261013", minutes=860,
                  film_name="识别错的片名")
    check("★ 影片名对不上 → 退到「日期+时间」仍能匹配",
          s.showtime_id == "103155648", s.showtime_id)
    # ② 时间缺失 + 当天该片只有一场 → 敢用
    ONE = {"data": {"showtimeFilmInf": [{"showtimeFilmDateInf": [
        {"date": 20261013, "showtimesInf": {
            "showtimeCount": 1,
            "showtimeList": [
                {"showtimeId": 7, "realtime": 1791872400000,
                 "filmList": [{"filmName": "只有一场"}]},
            ]}}]}]}}
    s2 = pick_show(parse_shows(ONE), date="20261013", film_name="只有一场")
    check("★ 当天该片只有一场 → 时间缺失也能匹配",
          s2.showtime_id == "7", s2.showtime_id)
    # ③ 时间缺失 + 多场 → 不能瞎选
    try:
        pick_show(shows, date="20261013", film_name="生化危机：爆发夜")
        check("多场且无时间应该报错", False, "★ 没报 —— 会瞎选一个")
    except WandaShowtimeError as e:
        check("★★ 多场 + 没给时间 → SHOW_AMBIGUOUS（不瞎选）",
              e.code == "SHOW_AMBIGUOUS", e.code)

    section("七、日期归一（★ 归不了一律抛，不返回像日期的东西）")
    for raw, want in (("2026-10-13", "20261013"), ("20261013", "20261013"),
                      (20261013, "20261013")):
        check(f"★ {raw!r} → {want}", normalize_date(raw) == want,
              normalize_date(raw))
    for bad in ("", None, "10月13日", "2026/13/45"):
        try:
            normalize_date(bad)
            check(f"{bad!r} 应该抛", False)
        except WandaShowtimeError as e:
            check(f"★ {bad!r} → BAD_INPUT", e.code == "BAD_INPUT")

    section("八、桥调用：fetch_shows 的形状与传参")
    class T:
        def __init__(self, body):
            self.body, self.calls = body, []

        def get(self, url, headers, timeout):
            self.calls.append(url)
            return 200, json.dumps(self.body)

    t = T(PAYLOAD)
    br = WandaBridgeClient(BridgeConfig(base="http://x", key="k"),
                           transport=t)
    got = fetch_shows(br, wanda_cinema_id="379", date="20261013")
    check("★ 取回 3 场", len(got) == 3, str(len(got)))
    check("★ URL 用 cinemaId（万达的影院 ID）",
          "cinemaId=379" in t.calls[0], t.calls[0][:88])
    check("★ showDate 传的是 2026-10-13 格式",
          "showDate=2026-10-13" in t.calls[0], t.calls[0][:88])
    # 场次列表拿不到
    t2 = T({"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []}})
    try:
        fetch_shows(WandaBridgeClient(BridgeConfig(base="http://x", key="k"),
                                      transport=t2),
                    wanda_cinema_id="379", date="20261013")
        check("空场次应该报错", False)
    except WandaShowtimeError as e:
        check("★ 没有任何场次 → NO_SHOWS_FOR_DATE",
              e.code == "NO_SHOWS_FOR_DATE", e.code)
    try:
        fetch_shows(br, wanda_cinema_id="", date="20261013")
        check("空影院ID应该报错", False)
    except WandaShowtimeError as e:
        check("★ 没传影院 ID → BAD_INPUT", e.code == "BAD_INPUT", e.code)

    section("九、★ 源码里写明 showDate 无效这件事")
    src = (Path(__file__).resolve().parent.parent
           / "application" / "wanda_showtime.py").read_text(encoding="utf-8")
    check("★★ 模块注释写明了 showDate 被忽略",
          "忽略" in src and "showDate" in src)
    check("★★ 并写明不滤日期的后果是「静默拿错天的价」",
          "静默拿错天" in src)
    check("★ 给出了三个日期的实测证据（含 1999 那个反例）",
          "1999-01-01" in src)

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"{passed}/{total} 项通过")
    if passed != total:
        print("★ 有未通过项 —— 别急着往下做")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
