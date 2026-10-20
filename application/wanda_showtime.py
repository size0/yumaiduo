"""
application/wanda_showtime.py —— 万达场次解析（把买家那一场对到万达的 showtimeId）

════════════════════════════════════════════════════════════════
★ 为什么需要这一层

  两个上游用【两套场次 ID】：
      良票识别结果 → show_id（良票的）
      万达座位图   → 需要 showtimeId（万达的）

  `cinema_identity_mapping` 只映射【影院】，不映射场次。
  ⇒ 必须自己从"万达这家影院这几天的全部场次"里，
     按【影片 + 开映时间】把买家那一场找出来。

════════════════════════════════════════════════════════════════
★★★ 最要紧的一条：`showDate` 参数【被万达忽略】

  实测（2026-10-13，cinemaId=379）：

      传 showDate=2026-10-13 → 返回日期集合
          ['20261013','20261014','20261015','20261016','20261017','20261018']
      传 showDate=2026-10-01 → ★ 同一批
      传 showDate=1999-01-01 → ★ 还是同一批

  ⇒ 这个参数是彻底无效的，接口返回的是【万达当前开放的整个日期范围】。

  ★ 所以必须【自己按 showtimeFilmDateInf[].date 过滤】。
    不滤的后果不是报错，是**静默拿错天的价**：
      同一部影片明天的 8 号厅和今天的 8 号厅，开映时间可能一样，
      不滤日期就会匹配到明天的场次，报出明天的价。
      ——这类错误最难发现，因为一切"看起来都正常"。

════════════════════════════════════════════════════════════════
★ 匹配策略：先按时间戳，不用字符串比时间

  达的场次有 `realtime = 1791872400000`（毫秒时间戳）。
  用时间戳比字符串可靠得多：
    · 不用管 '18:00' 还是 '18:00:00' 还是 '1800'
    · 不用管时区（时间戳是绝对的）

  ★ 但只按时间戳匹配还不够 —— 买家截图上的时间只到【分钟】，
    而场次是精确到秒的（虽然通常都是整分）。
    所以给一个【容差】窗口，默认 ±3 分钟（可调）。

  ★ 多个候选怎么办：不猜，报歧义。
    同一时间同一影片可能有多场（不同厅），这时必须让人/上游决定。
"""
import datetime
from dataclasses import dataclass, field
from typing import Any

from integrations.wanda.bridge import (
    PATH_SHOWTIMES, WandaBridgeClient, WandaBridgeError)

# 匹配容差（分钟）★ 我选的：场次通常整分开映，3 分钟足以吸收
# "截图上的时间被四舍五入/识别偏差"，又不至于把相邻场次混进来。
# （万达排片相邻场次一般隔 10 分钟以上）
DEFAULT_TOLERANCE_MINUTES = 3


class WandaShowtimeError(RuntimeError):
    """
    场次解析失败。

    code：
      SHOWTIME_LIST_UNAVAILABLE  拿不到场次列表
      SHOWTIME_SHAPE_UNRECOGNIZED
                                 拿到了数据但解析不出场次（结构变了）
                                 ★ 和"没排片"是两回事，必须分开报
      NO_SHOWS_FOR_DATE          该影院那天没有排片
      SHOW_NOT_FOUND             找不到匹配的场次
      SHOW_AMBIGUOUS             匹配到多个候选，不敢猜
      BAD_INPUT                  调用方给的日期/时间没法解析
    """
    def __init__(self, code: str, message: str, *, detail: Any = None):
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {message}")


@dataclass
class WandaShow:
    """万达的一个场次（只留报价要用的字段）。"""
    showtime_id: str
    date: str = ""                 # 'YYYYMMDD'
    realtime_ms: int = 0           # ★ 毫秒时间戳
    film_name: str = ""
    hall_name: str = ""
    hall_type: str = ""
    sales_price_fen: int | None = None
    wplus_price_fen: int | None = None
    wplus_activity_code: str = ""
    status: int | None = None
    raw: dict = field(default_factory=dict)

    @property
    def starts_at(self) -> datetime.datetime | None:
        if not self.realtime_ms:
            return None
        return datetime.datetime.fromtimestamp(self.realtime_ms / 1000)

    @property
    def starts_at_text(self) -> str:
        t = self.starts_at
        return t.strftime("%Y-%m-%d %H:%M") if t else ""

    def describe(self) -> str:
        return (f"{self.starts_at_text} {self.film_name or '?'} "
                f"{self.hall_name or '?'} (id={self.showtime_id})")


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def parse_shows(payload: dict) -> list[WandaShow]:
    """
    把 /api/showtime/by-cinema 的原始响应拍平成场次列表。

    ★ 结构（实测 2026-10-13，cinemaId=379；★ 我第一版理解错了）：
        data.showtimeFilmInf[]                 影片
            filmId / extFilmId / effective
            showtimeFilmDateInf[]              日期层
                date                           'YYYYMMDD' 整数
                showtimesInf                   ★★ 是【对象】不是数组！
                    filmId / extFilmId / lastShowtimeDate
                    showtimeCount                  场次数
                    showtimeList[]                 ★ 场次在这里
                        showtimeId / realtime / hallName / hallType
                        filmList[0].filmName
                        salesPrice / wPlusActivityPrice / wPlusActivityCode
                        areaPriceList[] / status

      ★ 我踩的坑：以为 showtimesInf 是 list，直接 `for s in showtimesInf`
        迭代出的是【键名字符串】，然后被 isinstance(s, dict) 全滤掉，
        结果是"0 场"——看起来像"这天没排片"，实际是解析错了。
      ⇒ 这里对两种形状都做兼容（list 或 dict.showtimeList），
        并对认不出的形状显式报出来，不再静默返回空。
    """
    data = (payload or {}).get("data")
    if not isinstance(data, dict):
        return []
    out: list[WandaShow] = []
    for film in (data.get("showtimeFilmInf") or []):
        if not isinstance(film, dict):
            continue
        for day in (film.get("showtimeFilmDateInf") or []):
            if not isinstance(day, dict):
                continue
            date = str(day.get("date") or "").strip()
            for s in _iter_shows(day.get("showtimesInf")):
                # 影片名：优先场次自带的 filmList[0].filmName
                # （影片级节点里没有名字，只有 id）
                fl = s.get("filmList")
                film_name = ""
                if isinstance(fl, list) and fl and isinstance(fl[0], dict):
                    film_name = str(fl[0].get("filmName") or "")
                out.append(WandaShow(
                    showtime_id=str(s.get("showtimeId") or ""),
                    date=date,
                    realtime_ms=_as_int(s.get("realtime")) or 0,
                    film_name=film_name,
                    hall_name=str(s.get("hallName") or ""),
                    hall_type=str(s.get("hallType") or ""),
                    sales_price_fen=_as_int(s.get("salesPrice")),
                    wplus_price_fen=_as_int(s.get("wPlusActivityPrice")),
                    wplus_activity_code=str(s.get("wPlusActivityCode") or ""),
                    status=_as_int(s.get("status")),
                    raw=s,
                ))
    return out


def _iter_shows(node: Any) -> list[dict]:
    """
    从 showtimesInf 里取出场次数组。

    ★ 兼容两种形状（实测是 ②）：
        ① list                     直接就是场次数组
        ② {"showtimeList": [...]}  场次在 showtimeList 里
    认不出来就返回 []（由调用方区分"这天真没场次"和"结构变了"）。
    """
    if isinstance(node, list):
        return [s for s in node if isinstance(s, dict)]
    if isinstance(node, dict):
        for key in ("showtimeList", "showtimes", "list", "showtimeInf"):
            v = node.get(key)
            if isinstance(v, list):
                return [s for s in v if isinstance(s, dict)]
    return []


def normalize_date(value: Any) -> str:
    """
    把各种日期写法归一成 'YYYYMMDD'。

    接受：'2026-10-13' / '20261013' / 20261013 / datetime / date
    ★ 归一不了就抛 —— 不返回一个"看起来像日期"的东西。
      ★ 必须**真校验月/日范围**：
        我第一版只数位数（8 位数字就当日期），
        结果 '2026/13/45' 被归一成 '20261345'（13 月 45 日）——
        这种假日期会让整晚场次都匹配不上，而且看起来像"接口没数据"。
    """
    if value is None:
        raise WandaShowtimeError("BAD_INPUT", "日期为空")
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.strftime("%Y%m%d")
    s = str(value).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) != 8:
        raise WandaShowtimeError(
            "BAD_INPUT", f"日期没法归一成 YYYYMMDD：{value!r}")
    try:
        # ★ 用 datetime 构造来兜底校验（会拒绝 13 月、32 日、2 月 30 日）
        datetime.date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError as exc:
        raise WandaShowtimeError(
            "BAD_INPUT",
            f"日期不合法：{value!r} → {digits}（{exc}）") from exc
    return digits


def normalize_minutes(value: Any) -> int | None:
    """
    把开映时间归一成【当天第几分钟】（0..1439）。

    接受：'14:20' / '14:20:00' / '1420' / 860（已是分钟数）/ 毫秒时间戳
    ★ 归一不了就返回 None（不是抛）—— 让调用方走"时间匹配不上"的分支，
      因为时间缺失时还有别的判断路径（比如只有一场）。
    """
    if value is None or isinstance(value, bool):
        return None
    # ★ 毫秒时间戳：必须【先】判长度再判范围。
    #   我第一版先写了 0<=v<=1439，大整数直接落空返回 None ——
    #   结果是"传毫秒戳时静默匹配不上"，而调用方看不出哪里错。
    if isinstance(value, int):
        if len(str(value)) >= 12:                 # 毫秒戳
            dt = datetime.datetime.fromtimestamp(value / 1000)
            return dt.hour * 60 + dt.minute
        return value if 0 <= value <= 1439 else None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit() and len(s) >= 12:              # 毫秒戳（字符串形式）
        dt = datetime.datetime.fromtimestamp(int(s) / 1000)
        return dt.hour * 60 + dt.minute
    parts = s.replace("：", ":").split(":")
    try:
        if len(parts) >= 2:
            h, m = int(parts[0]), int(parts[1])
        elif s.isdigit() and len(s) == 4:
            h, m = int(s[:2]), int(s[2:])
        elif s.isdigit() and len(s) <= 3:
            h, m = divmod(int(s), 60)
        else:
            return None
    except (ValueError, TypeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h * 60 + m
    return None


def _minutes_of(show: WandaShow) -> int | None:
    t = show.starts_at
    return t.hour * 60 + t.minute if t else None


def pick_show(shows: list[WandaShow], *,
              date: str,
              minutes: int | None = None,
              film_name: str = "",
              tolerance_minutes: int = DEFAULT_TOLERANCE_MINUTES) -> WandaShow:
    """
    从场次列表里挑出买家那一场。

    参数：
      date              'YYYYMMDD'（★ 必须自己滤，showDate 参数无效）
      minutes           开映时间（当天第几分钟）；None = 只按影片滤
      film_name         影片名（可空）
      tolerance_minutes 时间容差

    匹配顺序（都是【先按日期滤】，绝不跨天）：
      ① 日期 + 影片 + 时间（容差内）
      ② 日期 + 时间（容差内）           —— 影片名可能识别不准，退一步
      ③ 日期 + 影片（当天该片只有一场时才敢用）
      ④ 日期（当天只有一场时才敢用）

    ★ 任何时候匹配到多个 → 抛 SHOW_AMBIGUOUS，不猜。
      宁可让人兜底，不可报错价。
    """
    same_day = [s for s in shows if s.date == date]
    if not same_day:
        have = sorted({s.date for s in shows})
        raise WandaShowtimeError(
            "NO_SHOWS_FOR_DATE",
            f"万达这场（{date}）没有排片。"
            f"★ 注意：接口会返回它开放的所有日期，必须自己滤日期",
            detail={"want": date, "have_dates": have})

    def _norm_film(x: str) -> str:
        return "".join(str(x or "").split())

    want_film = _norm_film(film_name)

    def _by_film(items):
        if not want_film:
            return items
        hit = [s for s in items if _norm_film(s.film_name) == want_film]
        # 精确不等就退到"包含"（截图上的影片名可能被截断/加后缀）
        return hit or [s for s in items
                       if want_film in _norm_film(s.film_name)
                       or _norm_film(s.film_name) in want_film]

    def _by_time(items):
        if minutes is None:
            return []
        out = []
        for s in items:
            m = _minutes_of(s)
            if m is None:
                continue
            if abs(m - minutes) <= tolerance_minutes:
                out.append(s)
        return out

    # ① 日期 + 影片 + 时间
    cands = _by_time(_by_film(same_day))
    why = "日期+影片+时间"
    # ② 日期 + 时间（影片名可能不准）
    if not cands:
        cands = _by_time(same_day)
        why = "日期+时间（影片名没对上，放宽）"
    # ③ 日期 + 影片（当天该片只有一场）
    if not cands:
        f = _by_film(same_day)
        if len(f) == 1:
            cands, why = f, "当天该影片只有一场"
    # ④ 日期（当天只有一场）
    if not cands and len(same_day) == 1:
        cands, why = same_day, "当天只有一场"

    if not cands:
        n = len(same_day)
        # ★ 时间缺失 + 多场：不该报"找不到"，该报"无法确定"。
        #   两者对调用方的意义不同 ——
        #     SHOW_NOT_FOUND   这一场可能真的不存在/已过场
        #     SHOW_AMBIGUOUS   场次存在，但我不知道是哪一个
        if minutes is None and n > 1:
            raise WandaShowtimeError(
                "SHOW_AMBIGUOUS",
                f"{date} 有 {n} 场，但没给开映时间 —— 无法确定是哪一场"
                f"（不瞎选）",
                detail={"date": date, "count": n,
                        "sample": [s.describe() for s in same_day[:8]]})
        raise WandaShowtimeError(
            "SHOW_NOT_FOUND",
            f"找不到匹配的场次（{date} 共 {n} 场，"
            f"影片={film_name!r} 时间={minutes}）",
            detail={"date": date, "count": n,
                    "sample": [s.describe() for s in same_day[:6]]})
    if len(cands) > 1:
        raise WandaShowtimeError(
            "SHOW_AMBIGUOUS",
            f"匹配到 {len(cands)} 个候选（{why}）—— 不猜，请人工确认",
            detail={"candidates": [s.describe() for s in cands[:8]]})
    s = cands[0]
    return s


# ============================================================ 桥调用
def fetch_shows(bridge: WandaBridgeClient, *,
                wanda_cinema_id: str,
                date: str = "") -> list[WandaShow]:
    """
    取某万达影院某天的全部场次。

    ★★ 为什么要传 date 却【不能信它】：
      路由签名有 showDate 参数，但实测万达忽略它 ——
      不管传哪天，返回的都是它当前开放的整个日期范围。
      ⇒ 传 date 只是为了少返回点数据（万一以后生效），
        过滤必须由调用方用 pick_show(..., date=...) 自己做。
    """
    cid = str(wanda_cinema_id or "").strip()
    if not cid:
        raise WandaShowtimeError("BAD_INPUT", "没传万达影院 ID")
    params = {"cinemaId": cid}
    if date:
        params["showDate"] = _as_iso_date(date)
    try:
        body = bridge.get_json(PATH_SHOWTIMES, params)
    except WandaBridgeError as exc:
        raise WandaShowtimeError(
            "SHOWTIME_LIST_UNAVAILABLE",
            f"取场次列表失败：{exc}", detail=exc.code) from exc
    shows = parse_shows(body)
    if not shows:
        # ★★ 关键：把"这天真没排片"和"结构变了导致解析不出"分开报。
        #    实测踩过：showtimesInf 是对象不是数组，我的解析静默返回 0 场，
        #    报出来的却是"没有返回任何场次"—— 看起来像影院没排片，
        #    实际是解析错了。这种"错误的原因指向错误的方向"最难查。
        data = body.get("data") or {}
        films = data.get("showtimeFilmInf")
        has_films = isinstance(films, list) and len(films) > 0
        if has_films:
            days = 0
            shape = []
            for f in films:
                for d in (f.get("showtimeFilmDateInf") or []):
                    days += 1
                    node = d.get("showtimesInf")
                    shape.append(type(node).__name__)
            raise WandaShowtimeError(
                "SHOWTIME_SHAPE_UNRECOGNIZED",
                f"影院 {cid} 返回了 {len(films)} 部影片/{days} 个日期，"
                f"但一个场次都没解析出来 —— 结构可能变了"
                f"（showtimesInf 类型：{sorted(set(shape))}）。"
                f"★ 这不是'没排片'，是解析问题",
                detail={"films": len(films), "days": days,
                        "showtimesInf_types": sorted(set(shape))})
        raise WandaShowtimeError(
            "NO_SHOWS_FOR_DATE",
            f"影院 {cid} 没有返回任何场次",
            detail={"bizCode": data.get("bizCode")})
    return shows


def _as_iso_date(value: Any) -> str:
    """'YYYYMMDD' → 'YYYY-MM-DD'（接口要的格式）。"""
    d = normalize_date(value)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"
