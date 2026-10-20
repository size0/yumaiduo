"""良票 → 万达 影院身份映射（只读）。

数据来源是 V5 维护的影院身份库，由 ``/usr/local/sbin/ticket-cinema-identity-sync``
定期**原子复制**到出票系统自己的 ``backend-data`` 目录（方案 2：不动 V5 的权限，
与 ``cinema_cache.sqlite`` 的维护方式一致）。

对应插件侧需求文档《给出票系统-影院映射接口需求.md》的约束：

* **只读**：用 ``?mode=ro`` 打开，不建表、不写、不迁移 —— 这份库不是我们的。
  只读 URI 同时避免在同目录尝试创建 ``-wal``/``-shm``（那目录我们写不了）。
* **不做长期缓存**：这张表是人工维护的，改完希望尽快生效；单条主键查询本身极快。
* **库缺失/不可读绝不抛 500**：降级成 ``IDENTITY_STORE_UNAVAILABLE``。
  否则"表坏了"和"这家没映射"会混成同一个结果，插件侧还会把它当网络错误重试。
* 只返回单条查询结果，不整表返回。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .store import DATA_DIR

IDENTITY_DB = DATA_DIR / "cinema-identity.sqlite3"

REASON_NO_MAPPING = "NO_MAPPING"
REASON_STORE_UNAVAILABLE = "IDENTITY_STORE_UNAVAILABLE"
REASON_SOURCE_NOT_FOUND = "SOURCE_CINEMA_NOT_FOUND"
REASON_CITY_MISMATCH = "SOURCE_CITY_MISMATCH"
REASON_AMBIGUOUS = "AMBIGUOUS_SOURCE_CINEMA"

_SOURCE_PROVIDER = "LIANGPIAO"
_TARGET_PROVIDER = "WANDA"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{IDENTITY_DB}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _canonical_name(conn: sqlite3.Connection, provider: str, city_id: str, cinema_id: str) -> str:
    """取名字只用于日志/排查；查不到就返回空串，不影响映射结果。"""
    try:
        row = conn.execute(
            "SELECT canonical_cinema FROM cinema_identity "
            "WHERE provider=? AND provider_city_id=? AND provider_cinema_id=?",
            (provider, city_id, cinema_id),
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row["canonical_cinema"] or "") if row is not None else ""


def lookup(city_id: str, cinema_id: str) -> dict[str, Any]:
    """查一条 良票(cityCode, cinemaId) → 万达(cityId, cinemaId) 映射。

    永远返回可判定的结果：``{"mapped": True, ...}`` 或 ``{"mapped": False, "reason": ...}``，
    不抛异常。
    """
    city = str(city_id or "").strip()
    cinema = str(cinema_id or "").strip()
    try:
        conn = _connect()
    except sqlite3.Error:
        return {"mapped": False, "reason": REASON_STORE_UNAVAILABLE}
    try:
        row = conn.execute(
            "SELECT target_city_id, target_cinema_id FROM cinema_identity_mapping "
            "WHERE source_provider=? AND source_city_id=? AND source_cinema_id=?",
            (_SOURCE_PROVIDER, city, cinema),
        ).fetchone()
        if row is None:
            return {"mapped": False, "reason": REASON_NO_MAPPING}
        wanda_city = str(row["target_city_id"])
        wanda_cinema = str(row["target_cinema_id"])
        return {
            "mapped": True,
            "wandaCityId": wanda_city,
            "wandaCinemaId": wanda_cinema,
            "wandaCinemaName": _canonical_name(conn, _TARGET_PROVIDER, wanda_city, wanda_cinema),
            "sourceCinemaName": _canonical_name(conn, _SOURCE_PROVIDER, city, cinema),
        }
    except sqlite3.Error:
        return {"mapped": False, "reason": REASON_STORE_UNAVAILABLE}
    finally:
        conn.close()


def lookup_recognized(cinema_id: str, city_name: str = "", city_code: str = "") -> dict[str, Any]:
    """Resolve a Liangpiao recognition result to Wanda using provider IDs.

    Recognition may omit cityCode. In that case, infer it only when the source
    cinema ID and (if present) city name identify exactly one stored cinema.
    Never use a cinema-name search to choose an order destination.
    """
    cinema = str(cinema_id or "").strip()
    if not cinema:
        return {"mapped": False, "reason": REASON_SOURCE_NOT_FOUND}
    city = str(city_code or "").strip()
    if not city:
        try:
            conn = _connect()
        except sqlite3.Error:
            return {"mapped": False, "reason": REASON_STORE_UNAVAILABLE}
        try:
            rows = conn.execute(
                "SELECT DISTINCT provider_city_id, canonical_city FROM cinema_identity "
                "WHERE provider=? AND provider_cinema_id=?",
                (_SOURCE_PROVIDER, cinema),
            ).fetchall()
        except sqlite3.Error:
            return {"mapped": False, "reason": REASON_STORE_UNAVAILABLE}
        finally:
            conn.close()
        if not rows:
            return {"mapped": False, "reason": REASON_SOURCE_NOT_FOUND}
        expected_city = re.sub(r"\s+", "", str(city_name or "")).removesuffix("市")
        if expected_city:
            rows = [row for row in rows if re.sub(r"\s+", "", str(row["canonical_city"] or "")).removesuffix("市") == expected_city]
            if not rows:
                return {"mapped": False, "reason": REASON_CITY_MISMATCH}
        cities = {str(row["provider_city_id"]) for row in rows}
        if len(cities) != 1:
            return {"mapped": False, "reason": REASON_AMBIGUOUS}
        city = cities.pop()
    return {"sourceCityId": city, **lookup(city, cinema)}
