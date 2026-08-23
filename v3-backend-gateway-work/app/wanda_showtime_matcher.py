from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from fastapi import HTTPException

from .local_catalog import LocalWandaCatalog
from .schemas import Recognition
from .wanda_quote_diagnostics import _diagnostic_failure, _match_diagnostics
from .wanda_quote_domain import _data, _identifier, _text
from .wanda_quote_gateway import TicketGateway, _showtime_start


def _match_has_cinema(match: Mapping[str, Any]) -> bool:
    data = _data(match)
    cinema = data.get("cinema")
    cinema = cinema if isinstance(cinema, Mapping) else {}
    return bool(_identifier(cinema.get("cinemaId") or cinema.get("id") or data.get("cinema_id")))


def _showtime_and_cinema(match: Mapping[str, Any]) -> tuple[str, str, str | None]:
    data = _data(match)
    showtime = data.get("showtime")
    showtime = showtime if isinstance(showtime, Mapping) else {}
    showtime_id = _identifier(showtime.get("showtimeId") or showtime.get("id") or data.get("showtime_id"))
    cinema = data.get("cinema")
    cinema = cinema if isinstance(cinema, Mapping) else {}
    cinema_id = _identifier(showtime.get("cinemaId") or cinema.get("cinemaId") or cinema.get("id") or data.get("cinema_id"))
    cinema_name = _text(cinema.get("cinemaName") or cinema.get("name") or showtime.get("cinemaName") or data.get("cinemaName")) or None
    if not showtime_id or not cinema_id:
        raise HTTPException(status_code=422, detail="截图信息无法唯一匹配万达场次")
    return showtime_id, cinema_id, cinema_name


def _city_identity(value: Any) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", _text(value)).removesuffix("市")


def _matched_city_identity(match: Mapping[str, Any]) -> tuple[str, str]:
    root = _data(match)
    city = root.get("city") if isinstance(root.get("city"), Mapping) else {}
    cinema = root.get("cinema") if isinstance(root.get("cinema"), Mapping) else {}
    city_name = _text(city.get("name") or city.get("cityName") or cinema.get("cityName") or cinema.get("_cityName") or root.get("cityName"))
    cinema_name = _text(cinema.get("cinemaName") or cinema.get("name") or root.get("cinemaName"))
    return _city_identity(city_name), _city_identity(cinema_name)


WANDA_CINEMA_ALIASES = frozenset({"万达"})


def is_wanda_cinema_name(cinema: str | None) -> bool:
    return bool(cinema and any(alias in cinema for alias in WANDA_CINEMA_ALIASES))


class ShowtimeMatcher:
    def __init__(self, cinema_catalog: LocalWandaCatalog | None = None) -> None:
        self._cinema_catalog = cinema_catalog
        self._match_cache: dict[str, tuple[float, Mapping[str, Any]]] = {}
        self._match_inflight: dict[str, asyncio.Task[Mapping[str, Any]]] = {}

    @staticmethod
    def _has_complete_joint_identity(recognition: Recognition) -> bool:
        return bool(
            _text(recognition.cinema)
            and _text(recognition.movie)
            and recognition.date is not None
            and _showtime_start(recognition.showtime)
        )

    def _catalog_error(self, recognition: Recognition) -> HTTPException:
        explicit_cinema = _text(recognition.cinema)
        supported_brand = any(brand in explicit_cinema for brand in ("万达", "寰映", "儒意"))
        detail = "仅支持万达影院实时核价" if explicit_cinema and not supported_brand else "影院无法在官方影院库唯一匹配"
        return _diagnostic_failure(
            HTTPException(status_code=422, detail=detail),
            step="validate_cinema_catalog",
            recognition=recognition,
        )

    def _catalog_match_input(self, recognition: Recognition) -> tuple[Recognition, bool]:
        """Return a strict catalog match or defer bounded ambiguity to the full identity matcher."""
        if self._cinema_catalog is None:
            return recognition, False
        resolution = self._cinema_catalog.resolve(recognition)
        if resolution.matched:
            return resolution.recognition, False
        explicit_cinema = _text(recognition.cinema)
        supported_brand = any(brand in explicit_cinema for brand in ("万达", "寰映", "儒意"))
        if supported_brand and self._has_complete_joint_identity(recognition):
            # The ticket gateway searches the same official SQLite catalog and
            # resolves bounded cinema-name ambiguity with authoritative movie,
            # absolute date and start time.  The result is verified below.
            return recognition, True
        raise self._catalog_error(recognition)

    def _catalog_recognition(self, recognition: Recognition) -> Recognition:
        resolved, deferred = self._catalog_match_input(recognition)
        if deferred:
            raise self._catalog_error(recognition)
        return resolved

    def _verify_joint_match(self, match: Mapping[str, Any], cinema_id: str, recognition: Recognition) -> None:
        root = _data(match)
        requested_city = _city_identity(recognition.city)
        if requested_city:
            matched_city, matched_cinema = _matched_city_identity(match)
            if (matched_city and matched_city != requested_city) or (not matched_city and requested_city not in matched_cinema):
                raise HTTPException(status_code=422, detail="官方场次城市与买家明确城市不一致")
        evidence = root.get("showtime_match")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        components = evidence.get("components")
        components = components if isinstance(components, Mapping) else {}

        def status_of(name: str) -> str:
            value = components.get(name)
            return _text(value.get("status")) if isinstance(value, Mapping) else ""

        confidence = evidence.get("confidence")
        confidence = float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else 0.0
        candidate_count = evidence.get("candidate_count")
        candidate_count = candidate_count if isinstance(candidate_count, int) and not isinstance(candidate_count, bool) else 0
        verified = (
            confidence >= 0.85
            and candidate_count == 1
            and status_of("cinema") in {"exact", "normalized_partial", "address_fragment"}
            and status_of("movie") in {"exact", "short_unique"}
            and status_of("date") == "exact"
            and status_of("time") == "exact"
        )
        if not verified:
            raise HTTPException(status_code=422, detail="影院、影片和场次无法唯一匹配")
        contains = getattr(self._cinema_catalog, "contains_cinema_id", None)
        if not callable(contains) or not contains(cinema_id):
            raise HTTPException(status_code=422, detail="影院无法在官方影院库唯一匹配")

    @staticmethod
    def _match_cache_key(recognition: Recognition) -> str:
        selected = ",".join(recognition.official_selection.selected_seat_numbers)
        date_text = recognition.date.isoformat() if recognition.date is not None else ""
        return "\u001f".join((
            _text(recognition.city), _text(recognition.cinema), _text(recognition.movie),
            date_text, _showtime_start(recognition.showtime) or "", _text(recognition.hall), selected,
        ))

    async def _read_only_match(self, gateway: TicketGateway, recognition: Recognition) -> Mapping[str, Any]:
        key = self._match_cache_key(recognition)
        now = time.monotonic()
        cached = self._match_cache.get(key)
        if cached and cached[0] > now:
            return deepcopy(cached[1])
        if cached:
            self._match_cache.pop(key, None)
        pending = self._match_inflight.get(key)
        if pending is None:
            pending = asyncio.create_task(gateway.match(recognition))
            self._match_inflight[key] = pending
        try:
            result = await asyncio.shield(pending)
        finally:
            if self._match_inflight.get(key) is pending and pending.done():
                self._match_inflight.pop(key, None)
        # Cache only a complete, uniquely identified showtime. Negative and
        # partial matches are coalesced while in flight but never retained.
        try:
            _showtime_and_cinema(result)
        except HTTPException:
            return result
        self._match_cache[key] = (time.monotonic() + 15.0, deepcopy(result))
        while len(self._match_cache) > 200:
            self._match_cache.pop(next(iter(self._match_cache)))
        return deepcopy(result)

    async def _match_joint_identity(
        self,
        gateway: TicketGateway,
        recognition: Recognition,
        joint_match_required: bool,
    ) -> tuple[Mapping[str, Any], Recognition, str, str, str | None]:
        initial_match = await self._read_only_match(gateway, recognition)
        try:
            showtime_id, cinema_id, cinema_name = _showtime_and_cinema(initial_match)
            if joint_match_required:
                self._verify_joint_match(initial_match, cinema_id, recognition)
            return initial_match, recognition, showtime_id, cinema_id, cinema_name
        except HTTPException as initial_error:
            # Wanda's seat-map title often truncates a long branch name with an
            # ellipsis. If the official matcher still identifies exactly one
            # canonical cinema, retry only that canonical identity. All movie,
            # absolute date, start time, hall and seat facts remain unchanged.
            raw_cinema = _text(recognition.cinema)
            canonical_cinema = _text(_match_diagnostics(initial_match).get("cinema"))
            truncated = "..." in raw_cinema or "…" in raw_cinema
            if not truncated or not canonical_cinema or canonical_cinema == raw_cinema:
                initial_error.match_result = initial_match
                raise initial_error
            retried_recognition = recognition.model_copy(update={"cinema": canonical_cinema})
            retried_match = await self._read_only_match(gateway, retried_recognition)
            try:
                showtime_id, cinema_id, cinema_name = _showtime_and_cinema(retried_match)
                if joint_match_required:
                    self._verify_joint_match(retried_match, cinema_id, retried_recognition)
            except HTTPException as retried_error:
                retried_error.match_result = retried_match
                raise
            return retried_match, retried_recognition, showtime_id, cinema_id, cinema_name
    def catalog_match_input(self, recognition: Recognition) -> tuple[Recognition, bool]:
        return self._catalog_match_input(recognition)

    def catalog_recognition(self, recognition: Recognition) -> Recognition:
        return self._catalog_recognition(recognition)

    def verify_joint_match(self, match: Mapping[str, Any], cinema_id: str, recognition: Recognition) -> None:
        self._verify_joint_match(match, cinema_id, recognition)

    async def read_only_match(self, gateway: TicketGateway, recognition: Recognition) -> Mapping[str, Any]:
        return await self._read_only_match(gateway, recognition)

    async def match_joint_identity(
        self, gateway: TicketGateway, recognition: Recognition, joint_match_required: bool,
    ) -> tuple[Mapping[str, Any], Recognition, str, str, str | None]:
        return await self._match_joint_identity(gateway, recognition, joint_match_required)
