"""Read-only canonicalization from the ticket backend's Wanda SQLite cache."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .schemas import Recognition

_DEFAULT_PATH = Path("/var/lib/ticket-system/backend-data/cinema_cache.sqlite")


@dataclass(frozen=True)
class CatalogResolution:
    recognition: Recognition
    matched: bool
    cinema_id: str | None


def _key(value: str | None) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", value or "").replace("影城", "").replace("电影院", "")


def _has_distinctive_common_fragment(left: str, right: str, minimum: int = 4) -> bool:
    """Match ticket-search-style branch keywords while still requiring uniqueness."""
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) < minimum:
        return False
    return any(shorter[index:index + minimum] in longer for index in range(len(shorter) - minimum + 1))


def _wanda_branch_key(value: str | None, city: str | None = None) -> str:
    """Extract a Wanda branch token while leaving uniqueness to the caller."""
    raw_key = _key(value)
    if "万达" not in raw_key:
        return ""
    key = _cinema_alias_key(value, city)
    key = key.replace("万达", "").replace("广场", "")
    key = re.sub(r"(?:激光|xland|prime|旗舰)", "", key, flags=re.IGNORECASE)
    return re.sub(r"店$", "", key)


def _cinema_alias_key(value: str | None, city: str | None = None) -> str:
    """Remove bounded brand and auditorium-format noise; uniqueness remains mandatory."""
    key = _key(value)
    city_key = _key(city)
    if city_key and key.startswith(city_key):
        key = key[len(city_key):]
    key = re.sub(r"(?:i-?max|巨幕|杜比(?:影院|影厅)?|cinity|4dx)", "", key, flags=re.IGNORECASE)
    # Narrow headers are often visually truncated as “张家港I…/IM…/IMA…”.
    # Strip only a trailing partial IMAX token; never truncate Chinese branch text.
    key = re.sub(r"(?:ima|im|i)$", "", key, flags=re.IGNORECASE)
    key = key.replace("电影院", "").replace("电影城", "").replace("影城", "").replace("影院", "")
    # Official names move the branch suffix around format words, for example
    # “云龙万达广场激光店” vs “云龙万达广场店激光”. Treat only that bounded
    # store suffix as formatting noise so a generic “泉州万达广场激光店” cannot
    # win by suffix containment after its city prefix is removed.
    key = key.replace("店激光", "激光")
    key = re.sub(r"店$", "", key)
    # OCR often emits “万达影城（新北万达广场…）”, leaving a redundant
    # leading brand after suffix stripping. Keep the branch's “万达广场”.
    if key.startswith("万达") and key.count("万达") > 1:
        key = key[len("万达"):]
    return key


class LocalWandaCatalog:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else Path(os.getenv("WANDA_CINEMA_CACHE_PATH", _DEFAULT_PATH))

    def canonicalize(self, recognition: Recognition) -> Recognition:
        return self.resolve(recognition).recognition

    def contains_cinema_id(self, cinema_id: str) -> bool:
        """Confirm a gateway match still belongs to the authoritative local catalog."""
        normalized = str(cinema_id or "").strip()
        if not normalized or not self._path.is_file():
            return False
        try:
            with sqlite3.connect(f"file:{self._path}?mode=ro&immutable=1", uri=True) as connection:
                return connection.execute(
                    "SELECT 1 FROM cinemas WHERE CAST(cinema_id AS TEXT) = ? LIMIT 1",
                    (normalized,),
                ).fetchone() is not None
        except (sqlite3.Error, OSError):
            return False

    def resolve(self, recognition: Recognition) -> CatalogResolution:
        if not self._path.is_file() or not recognition.cinema:
            return CatalogResolution(recognition=recognition, matched=False, cinema_id=None)
        try:
            # The ticket backend owns SQLite writes; immutable avoids journal/WAL writes
            # from this read-only V3 process. A fresh connection is opened for each quote.
            with sqlite3.connect(f"file:{self._path}?mode=ro&immutable=1", uri=True) as connection:
                cinema = self._unique_cinema(connection, recognition.cinema, recognition.city, recognition.cinema_address_hint)
                if cinema is None:
                    return CatalogResolution(recognition=recognition, matched=False, cinema_id=None)
                cinema_id, cinema_name, city_id = cinema
                movie = self._unique_embedded_movie(connection, city_id, recognition.movie)
        except (sqlite3.Error, OSError):
            return CatalogResolution(recognition=recognition, matched=False, cinema_id=None)
        updates = {}
        if cinema_name != recognition.cinema:
            updates["cinema"] = cinema_name
        if movie and movie != recognition.movie:
            updates["movie"] = movie
        resolved = recognition.model_copy(update=updates) if updates else recognition
        return CatalogResolution(recognition=resolved, matched=True, cinema_id=cinema_id)

    @staticmethod
    def _unique_cinema(
        connection: sqlite3.Connection,
        raw: str,
        city_hint: str | None = None,
        address_hint: str | None = None,
    ) -> tuple[str, str, str] | None:
        key = _key(raw)
        if len(key) < 2:
            return None
        city_key = _key(city_hint)
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(cinemas)")}
        if {"address", "search_text"}.issubset(columns):
            rows = connection.execute("SELECT cinema_id, cinema_name, city_id, city_name, address, search_text FROM cinemas").fetchall()
        else:
            rows = [(*row, "", "") for row in connection.execute("SELECT cinema_id, cinema_name, city_id, city_name FROM cinemas").fetchall()]
        if city_key:
            rows = [row for row in rows if city_key in _key(str(row[3])) or _key(str(row[3])) in city_key]
        address_key = _key(address_hint)
        if len(address_key) >= 8:
            address_matches = [
                row for row in rows
                if address_key in _key(f"{row[4]}{row[5]}")
                or (len(_key(str(row[4]))) >= 8 and _key(str(row[4])) in address_key)
            ]
            if len(address_matches) == 1:
                row = address_matches[0]
                return str(row[0]), str(row[1]), str(row[2])
            if address_matches:
                rows = address_matches
        matches = [(str(cinema_id), str(name), str(city_id)) for cinema_id, name, city_id, _, _, _ in rows if key in _key(name) or _key(name) in key]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
        branch_key = _wanda_branch_key(raw, city_hint)
        # A header containing only brand/plaza/auditorium-format words has no
        # branch identity. Never let a coincidental unique alias turn it into
        # a specific city (for example generic “万达广场IMAX激光店” → 泉州).
        if "万达" in key and not branch_key:
            return None
        alias_key = _cinema_alias_key(raw, city_hint)
        alias_rows = [
            ((str(cinema_id), str(name), str(city_id)), _cinema_alias_key(str(name), str(city_name)))
            for cinema_id, name, city_id, city_name, _, _ in rows
        ]
        exact_alias_matches = [candidate for candidate, candidate_key in alias_rows if alias_key and candidate_key == alias_key]
        if len(exact_alias_matches) == 1:
            return exact_alias_matches[0]
        if exact_alias_matches:
            return None
        alias_matches = [
            candidate for candidate, candidate_key in alias_rows
            if alias_key and len(alias_key) >= 4 and len(candidate_key) >= 4
            and (alias_key in candidate_key or candidate_key in alias_key)
        ]
        if len(alias_matches) == 1:
            return alias_matches[0]
        if alias_matches:
            return None
        branch_matches = [
            candidate for (candidate, _), row in zip(alias_rows, rows)
            if len(branch_key) >= 2 and _wanda_branch_key(str(row[1]), str(row[3])) == branch_key
        ]
        if len(branch_matches) == 1:
            return branch_matches[0]
        if branch_matches:
            return None
        keyword_matches = [
            candidate for candidate, candidate_key in alias_rows
            if _has_distinctive_common_fragment(alias_key, candidate_key)
        ]
        return keyword_matches[0] if len(keyword_matches) == 1 else None

    @staticmethod
    def _unique_embedded_movie(connection: sqlite3.Connection, city_id: str, raw: str | None) -> str | None:
        key = _key(raw)
        if len(key) < 2:
            return None
        names = set()
        for (value,) in connection.execute("SELECT raw_json FROM city_movies WHERE city_id = ?", (city_id,)):
            try:
                name = str(json.loads(value).get("name") or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            candidate = _key(name)
            if len(candidate) >= 2 and candidate in key:
                names.add(name)
        return next(iter(names)) if len(names) == 1 else None
