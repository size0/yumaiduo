from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from ..recognition_v2.models import RecognitionResult

from .models import CinemaRouteResult, WandaCinemaCandidate
from ..recovery.service_contracts import CinemaRouteGateMixin
from .wanda_source import WandaCatalogSource, cinema_items, city_items


_MARKETING_SUFFIXES = (
    "赠停车2小时", "赠停车1小时", "儿童需购票", "原万达影城",
)
_DECORATION_TERMS = ("imax", "dolby", "激光", "vip")
_GENERIC_TOKENS = {
    "万达", "影城", "影院", "电影", "广场", "店", "国际", "中心", "厅",
    "影院店", "影城店",
}
_GENERIC_CHARS = frozenset("万达影城影院电影广场店国际中心厅")


class CinemaRouteV2Service(CinemaRouteGateMixin):
    """Resolve only Wanda city/store identity from an isolated recognition fact."""

    def __init__(
        self, source: WandaCatalogSource, *, identity_store: object | None = None,
        show_index_ttl_seconds: int = 900,
    ) -> None:
        self._source = source
        self._identity_store = identity_store
        self._show_index_ttl_seconds = max(60, min(int(show_index_ttl_seconds), 86_400))
        self._show_index_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}

    async def resolve(self, recognition: RecognitionResult) -> CinemaRouteResult:
        city_text = _text(recognition.city_text)
        cinema_text = _text(recognition.cinema_text)
        if not city_text:
            reason = (
                "CITY_REQUIRED_FOR_TRUNCATED_CINEMA"
                if recognition.cinema_truncated
                else "CITY_REQUIRED"
            )
            return _unresolved(reason)
        liangpiao_cinema_id = _liangpiao_cinema_id(recognition)
        if self._identity_store is not None and liangpiao_cinema_id:
            lookup = getattr(self._identity_store, "find_cinema_crosswalk", None)
            if callable(lookup):
                verified = lookup(
                    liangpiao_cinema_id=liangpiao_cinema_id, city_name=city_text, verified_only=True,
                )
                if len(verified) == 1:
                    item = verified[0]
                    return _crosswalk_result(item, reason="VERIFIED_PROVIDER_CROSSWALK")
        try:
            city_response = await self._source.get_city_list()
            city = _match_city(city_text, city_items(city_response))
            if city is None:
                return _unresolved("WANDA_CITY_NOT_FOUND")
            wanda_city_id, wanda_city_name = city
            cinema_response = await self._source.get_cinema_list(wanda_city_id, "0", "0")
            cinemas = cinema_items(cinema_response)
        except Exception:
            return _unresolved("WANDA_PROVIDER_UNAVAILABLE")

        if not cinema_text:
            return _unresolved(
                "CINEMA_REQUIRED",
                wanda_city_id=wanda_city_id,
                wanda_city_name=wanda_city_name,
            )
        scored = _score_cinemas(cinema_text, cinemas, wanda_city_name)
        if not scored and not _distinctive_tokens(cinema_text, wanda_city_name):
            # A generic Wanda label intentionally expands to the finite city
            # candidate set; show reverse lookup must decide it, never list
            # order or a brand keyword.
            scored = [(0, item) for item in cinemas if _store_id(item)]
        metadata_candidates = _metadata_candidates(recognition, scored, wanda_city_name)
        if metadata_candidates:
            scored = metadata_candidates
        if scored:
            highest = max(score for score, _ in scored)
            winners = [item for score, item in scored if score == highest]
            unique_ids = {_store_id(item) for item in winners}
            unique_ids.discard(None)
            if len(unique_ids) == 1:
                winner = next(item for item in winners if _store_id(item) is not None)
                result = _route_result(
                    winner, route="WANDA_SELF", wanda_city_id=wanda_city_id,
                    wanda_city_name=wanda_city_name, liangpiao_cinema_id=liangpiao_cinema_id,
                    resolution_reason="UNIQUE_WANDA_CINEMA_MATCH", verification_level="CANDIDATE",
                )
                self._persist_candidate(
                    result, recognition=recognition,
                    evidence_type="ADDRESS_GEO" if _strong_identity_match(recognition, winner, wanda_city_name) else "NAME",
                )
                persisted = self._existing_crosswalk(result)
                if persisted is not None:
                    result = result.model_copy(update={
                        "canonical_cinema_identity_id": persisted.get("canonical_cinema_identity_id"),
                        "verification_status": persisted.get("verification_status") or "CANDIDATE",
                        "verification_level": persisted.get("verification_level") or "FINGERPRINT_CANDIDATE",
                    })
                return result
            # A city-wide show index is only used when the candidate set is
            # finite and a showtime source is available. No candidate is ever
            # selected by list order.
            reverse_candidates = (
                cinemas if not _distinctive_tokens(cinema_text, wanda_city_name) else winners
            )
            reverse = await self._resolve_by_show_fingerprint(
                recognition, wanda_city_id, wanda_city_name, cinemas, reverse_candidates,
                liangpiao_cinema_id=liangpiao_cinema_id,
            )
            if reverse is not None:
                return reverse
            candidates = _candidate_models(winners, liangpiao_cinema_id=liangpiao_cinema_id)
            return CinemaRouteResult(
                route="UNRESOLVED", wanda_city_id=wanda_city_id,
                liangpiao_cinema_id=liangpiao_cinema_id, wanda_city_name=wanda_city_name,
                resolution_reason=(
                    "TRUNCATED_CINEMA_MULTIPLE_CANDIDATES"
                    if recognition.cinema_truncated else "WANDA_CINEMA_NOT_UNIQUE"
                ), candidate_count=len(candidates), candidates=candidates,
            )

        if (
            not recognition.cinema_truncated
            and _is_sufficient_cinema_text(cinema_text)
        ):
            return CinemaRouteResult(
                route="LIANGPIAO",
                wanda_city_id=wanda_city_id,
                wanda_city_name=wanda_city_name,
                resolution_reason="NO_TRUSTED_WANDA_CINEMA_MATCH",
            )
        return CinemaRouteResult(
            route="UNRESOLVED",
            wanda_city_id=wanda_city_id,
            wanda_city_name=wanda_city_name,
            resolution_reason=(
                "TRUNCATED_CINEMA_NO_MATCH"
                if recognition.cinema_truncated
                else "CINEMA_TEXT_INSUFFICIENT"
            ),
        )

    async def _resolve_by_show_fingerprint(
        self, recognition: RecognitionResult, wanda_city_id: str, wanda_city_name: str,
        cinemas: list[Mapping[str, Any]], winners: list[Mapping[str, Any]], *,
        liangpiao_cinema_id: str | None,
    ) -> CinemaRouteResult | None:
        get_showtimes = getattr(self._source, "get_showtimes", None)
        if not callable(get_showtimes) or not recognition.movie or not recognition.show_date or not recognition.start_time:
            return None
        candidates = winners or cinemas
        if not candidates:
            return None
        fingerprint = _show_fingerprint(recognition)
        cache_key = _show_index_key(wanda_city_id, fingerprint)
        cached = self._show_index_get(cache_key)
        if cached is None:
            stores: dict[str, dict[str, Any]] = {}
            for item in candidates:
                store_id = _store_id(item)
                if not store_id:
                    continue
                try:
                    payload = await get_showtimes(store_id, recognition.show_date.replace("-", ""))
                except Exception:
                    continue
                stores[store_id] = {
                    "wanda_store_id": store_id, "cinema_name": _cinema_name(item),
                    "address": _address(item), "shows": _extract_showtimes(payload),
                }
            cached = {
                "city_id": wanda_city_id, "city_name": wanda_city_name,
                "show_date": recognition.show_date,
                "movie_key": _comparison_key(recognition.movie or ""), "stores": stores,
            }
            self._show_index_put(cache_key, cached)
        stores = cached.get("stores") if isinstance(cached, Mapping) else {}
        matches: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
        for store_id, store in stores.items() if isinstance(stores, Mapping) else []:
            if not isinstance(store, Mapping):
                continue
            matching = [show_item for show_item in store.get("shows", []) if isinstance(show_item, Mapping) and _show_matches(show_item, fingerprint)]
            if len(matching) == 1:
                matches[str(store_id)] = (store, matching[0])
        if len(matches) != 1:
            if len(matches) > 1:
                matched_candidates = [_candidate_from_store(store, liangpiao_cinema_id, "CANDIDATE") for store, _ in matches.values()]
                return CinemaRouteResult(
                    route="UNRESOLVED", wanda_city_id=wanda_city_id,
                    liangpiao_cinema_id=liangpiao_cinema_id, wanda_city_name=wanda_city_name,
                    resolution_reason="SHOW_FINGERPRINT_NOT_UNIQUE", candidate_count=len(matched_candidates),
                    candidates=matched_candidates, show_fingerprint=fingerprint,
                )
            return None
        store, matched_show = next(iter(matches.values()))
        result = _route_result(
            store, route="WANDA_SELF", wanda_city_id=wanda_city_id,
            wanda_city_name=wanda_city_name, liangpiao_cinema_id=liangpiao_cinema_id,
            resolution_reason="UNIQUE_SHOW_FINGERPRINT_MATCH", verification_level="CANDIDATE",
        )
        result = result.model_copy(update={"show_fingerprint": {**fingerprint, "show_id": matched_show.get("show_id")}})
        self._persist_candidate(result, recognition=recognition, evidence_type="SHOW_FINGERPRINT")
        persisted = self._existing_crosswalk(result)
        if persisted is not None:
            result = result.model_copy(update={
                "canonical_cinema_identity_id": persisted.get("canonical_cinema_identity_id"),
                "verification_status": persisted.get("verification_status") or "CANDIDATE",
                "verification_level": persisted.get("verification_level") or "FINGERPRINT_CANDIDATE",
            })
        return result

    def _show_index_get(self, cache_key: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        memory = self._show_index_cache.get(cache_key)
        if memory is not None and memory[0] > now:
            return memory[1]
        getter = getattr(self._identity_store, "get_wanda_show_index", None)
        if callable(getter):
            persisted = getter(cache_key, now=now)
            if isinstance(persisted, Mapping) and isinstance(persisted.get("snapshot"), Mapping):
                snapshot = dict(persisted["snapshot"])
                self._show_index_cache[cache_key] = (datetime.fromisoformat(str(persisted["expires_at"])), snapshot)
                return snapshot
        return None

    def _show_index_put(self, cache_key: str, snapshot: dict[str, Any]) -> None:
        expires = datetime.now(timezone.utc) + timedelta(seconds=self._show_index_ttl_seconds)
        self._show_index_cache[cache_key] = (expires, snapshot)
        saver = getattr(self._identity_store, "save_wanda_show_index", None)
        if callable(saver):
            saver(
                cache_key=cache_key, city_id=str(snapshot["city_id"]), city_name=str(snapshot["city_name"]),
                show_date=str(snapshot.get("show_date") or ""), movie_key=str(snapshot.get("movie_key") or ""),
                snapshot=snapshot, expires_at=expires.isoformat(),
            )

    def _existing_crosswalk(self, result: CinemaRouteResult) -> Mapping[str, Any] | None:
        lookup = getattr(self._identity_store, "find_cinema_crosswalk", None)
        if not callable(lookup) or not result.liangpiao_cinema_id or not result.wanda_store_id:
            return None
        values = lookup(liangpiao_cinema_id=result.liangpiao_cinema_id, wanda_store_id=result.wanda_store_id)
        return values[0] if values else None

    def _persist_candidate(self, result: CinemaRouteResult, *, recognition: RecognitionResult, evidence_type: str) -> None:
        saver = getattr(self._identity_store, "save_cinema_crosswalk", None)
        if not callable(saver) or not result.liangpiao_cinema_id or not result.wanda_store_id:
            return
        existing = self._existing_crosswalk(result)
        existing_evidence = existing.get("evidence") if isinstance(existing, Mapping) else {}
        existing_evidence = existing_evidence if isinstance(existing_evidence, Mapping) else {}
        fingerprint = result.show_fingerprint or _show_fingerprint(recognition)
        fingerprints = existing_evidence.get("show_fingerprints")
        if not isinstance(fingerprints, list):
            fingerprints = []
        prior_fingerprint = existing_evidence.get("show_fingerprint")
        if isinstance(prior_fingerprint, Mapping) and not fingerprints:
            fingerprints = [dict(prior_fingerprint)]
        fingerprint_key = _independent_fingerprint_key(fingerprint)
        known_keys = {_independent_fingerprint_key(item) for item in fingerprints if isinstance(item, Mapping)}
        if evidence_type == "SHOW_FINGERPRINT" and fingerprint_key not in known_keys:
            fingerprints = [*fingerprints, dict(fingerprint)]
        independent_show_evidence = len({
            _independent_fingerprint_key(item) for item in fingerprints if isinstance(item, Mapping)
        }) >= 2
        strong_evidence = evidence_type in {"ADDRESS", "DISTRICT", "GEO", "ADDRESS_GEO"}
        verification_status = "VERIFIED" if independent_show_evidence or strong_evidence else "CANDIDATE"
        verification_level = (
            "VERIFIED_REPEATED_UNIQUE" if independent_show_evidence
            else "VERIFIED_STRONG_IDENTITY" if strong_evidence
            else "FINGERPRINT_CANDIDATE"
        )
        canonical_id = str(existing.get("canonical_cinema_identity_id") if existing else "") or _canonical_cinema_id(
            result.liangpiao_cinema_id, result.wanda_store_id, result.wanda_city_name,
        )
        saver({
            "canonical_cinema_identity_id": canonical_id, "liangpiao_cinema_id": result.liangpiao_cinema_id,
            "wanda_store_id": result.wanda_store_id, "city_name": result.wanda_city_name or "",
            "liangpiao_name": recognition.cinema_text, "wanda_name": result.wanda_cinema_name,
            "address": result.wanda_cinema_address, "verification_status": verification_status,
            "verification_level": verification_level,
            "evidence": {
                **dict(existing_evidence), "evidence_type": evidence_type,
                **({"show_fingerprint": fingerprint} if evidence_type == "SHOW_FINGERPRINT" else {}),
                "show_fingerprints": fingerprints,
            },
        })


def _match_city(value: str, cities: list[Mapping[str, Any]]) -> tuple[str, str] | None:
    wanted = _city_key(value)
    matches: list[tuple[str, str]] = []
    for item in cities:
        city_id = _first(item, "id", "locationId", "cityId")
        name = _first(item, "name", "cityName", "city")
        if not city_id or not name:
            continue
        aliases = {_city_key(name)}
        alias = _first(item, "alias", "shortName")
        if alias:
            aliases.add(_city_key(alias))
        if wanted in aliases:
            matches.append((city_id, name))
    return matches[0] if len(matches) == 1 else None


def _score_cinemas(
    query: str,
    cinemas: list[Mapping[str, Any]],
    city_name: str,
) -> list[tuple[int, Mapping[str, Any]]]:
    query_key = _comparison_key(query)
    query_tokens = _distinctive_tokens(query, city_name)
    scored: list[tuple[int, Mapping[str, Any]]] = []
    expected_city = _city_key(city_name)
    for item in cinemas:
        item_city = _first(item, "cityName", "city")
        if item_city and _city_key(item_city) != expected_city:
            continue
        name = _cinema_name(item)
        if not name or not _store_id(item):
            continue
        actual_key = _comparison_key(name)
        actual_tokens = _distinctive_tokens(name, city_name)
        score = 0
        if query_key == actual_key:
            score = 100
        elif len(query_key) >= 4 and (query_key in actual_key or actual_key in query_key):
            score = 90
        else:
            overlap = query_tokens & actual_tokens
            if overlap:
                score = min(90, 60 + len(overlap) * 10)
            address_tokens = _distinctive_tokens(_address(item) or "", city_name)
            if query_tokens & address_tokens:
                score = max(score, 70)
        if score >= 70:
            scored.append((score, item))
    return scored


def _candidate_models(
    items: list[Mapping[str, Any]], *, liangpiao_cinema_id: str | None = None,
) -> list[WandaCinemaCandidate]:
    seen: set[str] = set()
    result: list[WandaCinemaCandidate] = []
    for item in items:
        store_id = _store_id(item)
        name = _cinema_name(item)
        if not store_id or not name or store_id in seen:
            continue
        seen.add(store_id)
        result.append(WandaCinemaCandidate(
            wanda_store_id=store_id, cinema_name=name, address=_address(item),
            liangpiao_cinema_id=liangpiao_cinema_id,
        ))
    return result


def _route_result(
    item: Mapping[str, Any], *, route: str, wanda_city_id: str, wanda_city_name: str,
    liangpiao_cinema_id: str | None, resolution_reason: str, verification_level: str,
) -> CinemaRouteResult:
    return CinemaRouteResult(
        route=route, wanda_city_id=wanda_city_id, liangpiao_cinema_id=liangpiao_cinema_id,
        wanda_store_id=_store_id(item), wanda_city_name=wanda_city_name,
        wanda_cinema_name=_cinema_name(item), wanda_cinema_address=_address(item),
        resolution_reason=resolution_reason, verification_status="CANDIDATE", verification_level=verification_level,
    )


def _crosswalk_result(item: Mapping[str, Any], *, reason: str) -> CinemaRouteResult:
    return CinemaRouteResult(
        route="WANDA_SELF", liangpiao_cinema_id=_text(item.get("liangpiao_cinema_id")),
        canonical_cinema_identity_id=_text(item.get("canonical_cinema_identity_id")),
        wanda_store_id=_text(item.get("wanda_store_id")), wanda_city_name=_text(item.get("city_name")),
        wanda_cinema_name=_text(item.get("wanda_name")), wanda_cinema_address=_text(item.get("address")),
        resolution_reason=reason, verification_status="VERIFIED", verification_level=_text(item.get("verification_level")) or "VERIFIED",
    )


def _canonical_cinema_id(liangpiao_cinema_id: str, wanda_store_id: str, city_name: str | None) -> str:
    material = json.dumps({"liangpiao_cinema_id": liangpiao_cinema_id, "wanda_store_id": wanda_store_id, "city": city_name or ""}, sort_keys=True, ensure_ascii=False)
    return "cin-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _candidate_from_store(
    item: Mapping[str, Any], liangpiao_cinema_id: str | None, verification_level: str,
) -> WandaCinemaCandidate:
    return WandaCinemaCandidate(
        wanda_store_id=str(item.get("wanda_store_id") or ""),
        cinema_name=str(item.get("cinema_name") or ""), address=_text(item.get("address")),
        liangpiao_cinema_id=liangpiao_cinema_id, verification_level=verification_level,
    )


def _liangpiao_cinema_id(recognition: RecognitionResult) -> str | None:
    raw = recognition.raw_provider_result
    containers: list[Mapping[str, Any]] = []
    if isinstance(raw, Mapping):
        containers.append(raw)
        for key in ("data", "finalResults", "final_results", "result"):
            value = raw.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
                for nested_key in ("data", "finalResults", "final_results", "result"):
                    nested = value.get(nested_key)
                    if isinstance(nested, Mapping):
                        containers.append(nested)
    for container in containers:
        for key in ("liangpiao_cinema_id", "cinemaId", "cinema_id"):
            value = _text(container.get(key))
            if value:
                return value
    return None


def _strong_identity_match(
    recognition: RecognitionResult, item: Mapping[str, Any], city_name: str,
) -> bool:
    address = _recognition_address(recognition)
    if address:
        wanted = _distinctive_tokens(address, city_name)
        actual = _distinctive_tokens(_address(item) or "", city_name)
        if wanted & actual:
            return True
    latitude, longitude = _recognition_geo(recognition)
    item_latitude = _number(item.get("latitude", item.get("lat")))
    item_longitude = _number(item.get("longitude", item.get("lon", item.get("lng"))))
    return (
        latitude is not None and longitude is not None
        and item_latitude is not None and item_longitude is not None
        and (latitude - item_latitude) ** 2 + (longitude - item_longitude) ** 2 <= 0.005 ** 2
    )


def _metadata_candidates(
    recognition: RecognitionResult, scored: list[tuple[int, Mapping[str, Any]]], city_name: str,
) -> list[tuple[int, Mapping[str, Any]]]:
    if len({_store_id(item) for _, item in scored if _store_id(item)}) <= 1:
        return scored
    address = _recognition_address(recognition)
    if address:
        tokens = _distinctive_tokens(address, city_name)
        filtered = [(score, item) for score, item in scored if tokens & _distinctive_tokens(_address(item) or "", city_name)]
        if filtered:
            return filtered
    latitude, longitude = _recognition_geo(recognition)
    if latitude is not None and longitude is not None:
        distances: list[tuple[float, tuple[int, Mapping[str, Any]]]] = []
        for item in scored:
            item_latitude = _number(item.get("latitude", item.get("lat")))
            item_longitude = _number(item.get("longitude", item.get("lon", item.get("lng"))))
            if item_latitude is not None and item_longitude is not None:
                distance = (item_latitude - latitude) ** 2 + (item_longitude - longitude) ** 2
                distances.append((distance, item))
        if distances:
            nearest = min(distance for distance, _ in distances)
            nearest_items = [item for distance, item in distances if abs(distance - nearest) < 1e-12]
            if len(nearest_items) == 1 and nearest <= 0.005 ** 2:
                return [(100, nearest_items[0])]
    return scored


def _recognition_address(recognition: RecognitionResult) -> str | None:
    for container in _raw_containers(recognition.raw_provider_result):
        value = _first(container, "address", "cinemaAddress", "cinema_address", "district", "districtName", "area")
        if value:
            return value
    return None


def _recognition_geo(recognition: RecognitionResult) -> tuple[float | None, float | None]:
    for container in _raw_containers(recognition.raw_provider_result):
        latitude = _number(container.get("latitude", container.get("lat")))
        longitude = _number(container.get("longitude", container.get("lon", container.get("lng"))))
        if latitude is not None and longitude is not None:
            return latitude, longitude
    return None, None


def _raw_containers(raw: object) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    pending = [raw]
    for _ in range(3):
        next_pending: list[object] = []
        for value in pending:
            if not isinstance(value, Mapping):
                continue
            result.append(value)
            next_pending.extend(value.values())
        pending = next_pending
    return result


def _number(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _show_fingerprint(recognition: RecognitionResult) -> dict[str, Any]:
    return {
        "city": _text(recognition.city_text), "movie": _text(recognition.movie),
        "date": _text(recognition.show_date), "start_time": _text(recognition.start_time),
        "dimension": _text(recognition.dimension), "language": _text(recognition.language),
        "hall": _text(recognition.hall),
    }


def _independent_fingerprint_key(fingerprint: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        _comparison_key(str(fingerprint.get("movie") or "")),
        _normalize_date(fingerprint.get("date")),
        _normalize_time(fingerprint.get("start_time")),
    )


def _show_index_key(city_id: str, fingerprint: Mapping[str, Any]) -> str:
    material = json.dumps({"city_id": city_id, "date": fingerprint.get("date"), "movie": _comparison_key(str(fingerprint.get("movie") or ""))}, sort_keys=True, ensure_ascii=False)
    return "wanda-show-index-" + hashlib.sha256(material.encode()).hexdigest()


def _extract_showtimes(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    result: list[dict[str, Any]] = []
    films = data.get("showtimeFilmInf") if isinstance(data, Mapping) else None
    if isinstance(films, list):
        for film in films:
            if not isinstance(film, Mapping):
                continue
            film_name = _first(film, "filmName", "nameCN", "name", "movieName")
            dates = film.get("showtimeFilmDateInf") or []
            for date_info in dates if isinstance(dates, list) else []:
                if not isinstance(date_info, Mapping):
                    continue
                date_value = _first(date_info, "date", "showDate", "show_date")
                nested = date_info.get("showtimesInf")
                values = nested.get("showtimeList") if isinstance(nested, Mapping) else []
                for value in values if isinstance(values, list) else []:
                    if isinstance(value, Mapping):
                        result.append(_normalize_show(value, film_name=film_name, date_value=date_value))
        return result
    for key in ("showtimeList", "showtimes", "shows", "items", "list"):
        values = data.get(key) if isinstance(data, Mapping) else None
        if isinstance(values, list):
            return [_normalize_show(value) for value in values if isinstance(value, Mapping)]
    return result


def _normalize_show(value: Mapping[str, Any], *, film_name: str | None = None, date_value: str | None = None) -> dict[str, Any]:
    film_list = value.get("filmList") if isinstance(value.get("filmList"), list) else []
    nested_film = film_list[0] if film_list and isinstance(film_list[0], Mapping) else {}
    raw_time = _first(value, "startTime", "showtimeStart", "showtime_start", "time", "realtime", "showtime") or ""
    timestamp_date, timestamp_time = _timestamp_parts(raw_time)
    text = str(raw_time)
    date_match = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", text)
    time_match = re.search(r"(\d{1,2}):(\d{2})", text)
    raw_date = date_value or ("-".join(date_match.groups()) if date_match else _first(value, "showDate", "show_date", "date")) or timestamp_date
    date_text = _normalize_date(raw_date)
    start_time = timestamp_time or (f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else _normalize_time(_first(value, "startTime", "showtimeStart", "showtime_start", "time")))
    version_language = _first(nested_film, "versionLanguage", "version_language")
    language = _first(value, "language", "languageName", "lang", "language_type") or _first(nested_film, "language", "languageName", "lang")
    dimension = _first(value, "dimension", "dimensional", "version") or _first(nested_film, "version", "dimension") or _first(value, "hallType")
    if not dimension and version_language:
        dimension = version_language.split("/", 1)[0]
    return {
        "show_id": _first(value, "showId", "showtimeId", "show_id", "id"),
        "movie": film_name or _first(value, "movieName", "filmName", "nameCN", "movie") or _first(nested_film, "filmName", "movieName", "nameCN"),
        "date": date_text, "start_time": start_time,
        "hall": _first(value, "hallName", "hall", "hall_name"),
        "dimension": dimension, "language": language,
    }


def _timestamp_parts(value: object) -> tuple[str | None, str | None]:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None, None
    if timestamp < 1_000_000_000:
        return None, None
    moment = datetime.fromtimestamp(timestamp / 1000 if timestamp > 10_000_000_000 else timestamp, timezone(timedelta(hours=8)))
    return moment.date().isoformat(), moment.strftime("%H:%M")


def _show_matches(show: Mapping[str, Any], fingerprint: Mapping[str, Any]) -> bool:
    if _comparison_key(str(show.get("movie") or "")) != _comparison_key(str(fingerprint.get("movie") or "")):
        return False
    if _normalize_date(show.get("date")) != _normalize_date(fingerprint.get("date")):
        return False
    if _normalize_time(show.get("start_time")) != _normalize_time(fingerprint.get("start_time")):
        return False
    for field in ("dimension", "language", "hall"):
        wanted = _text(fingerprint.get(field))
        actual = _text(show.get(field))
        wanted_key = _attribute_key(field, wanted or "")
        actual_key = _attribute_key(field, actual or "")
        if wanted and actual and wanted_key not in actual_key and actual_key not in wanted_key:
            return False
        if wanted and not actual:
            return False
    return True


def _attribute_key(field: str, value: str) -> str:
    key = _base_key(value)
    if field == "language":
        for alias in (("英语", "英文"), ("国语", "中文"), ("普通话", "中文")):
            if key in {_base_key(item) for item in alias}:
                return _base_key(alias[0])
    return key


def _normalize_date(value: object) -> str:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", text)
    return "-".join(match.groups()) if match else text


def _normalize_time(value: object) -> str:
    match = re.search(r"(\d{1,2}):(\d{2})", str(value or ""))
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else str(value or "").strip()


def _comparison_key(value: str) -> str:
    normalized = _base_key(value)
    normalized = normalized.replace("影院", "影城")
    for term in _DECORATION_TERMS:
        normalized = normalized.replace(term, "")
    return normalized


def _distinctive_tokens(value: str, city_name: str) -> set[str]:
    key = _comparison_key(value)
    city_key = _city_key(city_name)
    if city_key:
        key = key.replace(city_key, "")
    tokens = {key[index:index + 2] for index in range(len(key) - 1)}
    return {
        token for token in tokens
        if token not in _GENERIC_TOKENS
        and not any(char in _GENERIC_CHARS for char in token)
        and not token.isdigit()
        and any("\u4e00" <= char <= "\u9fff" for char in token)
    }


def _base_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    for suffix in _MARKETING_SUFFIXES:
        normalized = normalized.replace(suffix, "")
    normalized = normalized.replace("（", "").replace("）", "")
    normalized = normalized.replace("(", "").replace(")", "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", normalized)


def _city_key(value: str) -> str:
    return _base_key(value).removesuffix("市")


def _is_sufficient_cinema_text(value: str) -> bool:
    key = _comparison_key(value)
    return len(key) >= 4 and key not in _GENERIC_TOKENS


def _first(item: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = str(item.get(name) or "").strip()
        if value:
            return value
    return None


def _store_id(item: Mapping[str, Any]) -> str | None:
    return _first(item, "storeId", "wanda_store_id", "cinema_id")


def _cinema_name(item: Mapping[str, Any]) -> str | None:
    return _first(item, "cinemaName", "cinema_name", "name")


def _address(item: Mapping[str, Any]) -> str | None:
    return _first(item, "address", "cinemaAddress")


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _unresolved(reason: str, **values: Any) -> CinemaRouteResult:
    return CinemaRouteResult(route="UNRESOLVED", resolution_reason=reason, **values)
