from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from ..probe.seat_selector import LiveSeat
from ..show_resolve_v2.models import ShowResolutionResult
from ..wanda_direct_quote import (
    APP_CHANNEL,
    CINEMA_ORIGIN,
    FRONT_ORIGIN,
    H5_CHANNEL,
)


class WandaV2ReadService(Protocol):
    async def get_city_list(self) -> Mapping[str, Any]: ...
    async def get_cinema_list(self, city_id: str, district_id: str, brand_id: str) -> Mapping[str, Any]: ...
    async def get_showtimes(self, store_id: str, show_date: str) -> Mapping[str, Any]: ...
    async def get_realtime_seats(self, show_id: str) -> Mapping[str, Any]: ...


class WandaDirectQuoteV2ReadSource:
    """Thin read-only bridge from the existing Wanda adapter to V2 facts.

    The old service remains the HTTP/signature owner. This bridge only exposes
    its already-authorized read endpoints and the local cinema capability
    cache; it does not call the old quote method or run a second price formula.
    """

    def __init__(self, wanda_service: Any) -> None:
        self._wanda = wanda_service
        self._settings_provider = wanda_service._settings_provider

    async def get_city_list(self) -> Mapping[str, Any]:
        rows = self._cache_rows()
        cities: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            city = str(row["city_name"] or "").strip()
            if not city or city in seen:
                continue
            seen.add(city)
            cities.append({"id": city, "name": city})
        return {"code": 0, "data": {"cities": cities}}

    async def get_cinema_list(self, city_id: str, district_id: str, brand_id: str) -> Mapping[str, Any]:
        wanted = str(city_id or "").strip()
        cinemas: list[dict[str, str]] = []
        for row in self._cache_rows():
            if wanted and str(row["city_name"] or "").strip() != wanted:
                continue
            cinemas.append({
                "storeId": str(row["cinema_id"] or ""),
                "cinemaName": str(row["cinema_name"] or ""),
                "cityName": str(row["city_name"] or ""),
                "address": str(row["address"] or ""),
            })
        return {"code": 0, "data": {"cinemaList": cinemas}}

    async def get_showtimes(self, store_id: str, show_date: str) -> Mapping[str, Any]:
        settings = self._settings_provider()
        account = self._wanda._fixed_account(settings)
        return await self._wanda._official_get(
            account, CINEMA_ORIGIN, "/showtime/by_cinema.api",
            [("cinemaId", str(store_id)), ("showDate", str(show_date)), ("json", "true")],
            channel=H5_CHANNEL, event="wanda_v2_showtimes_response",
        )

    async def get_realtime_seats(self, show_id: str) -> Mapping[str, Any]:
        settings = self._settings_provider()
        account = self._wanda._fixed_account(settings)
        return await self._wanda._official_get(
            account, FRONT_ORIGIN, "/order/real_time_seat.api", [("dId", str(show_id))],
            channel=APP_CHANNEL, event="wanda_v2_realtime_seats_response",
        )

    async def get_probe_live_seats(self, show: ShowResolutionResult) -> list[LiveSeat]:
        """Read the current seat map with official area prices for Probe."""
        if not show.wanda_store_id or not show.wanda_show_id or not show.show_date:
            raise ValueError("probe_show_facts_incomplete")
        showtimes = await self.get_showtimes(show.wanda_store_id, show.show_date.replace("-", ""))
        showtime = _find_showtime(showtimes, show.wanda_show_id)
        if showtime is None:
            raise ValueError("probe_showtime_not_found")
        payload = await self.get_realtime_seats(show.wanda_show_id)
        facts = self._wanda._seat_facts(payload, self._wanda._area_prices(showtime))
        return [LiveSeat(
            seat_id=str(item.get("seat_id") or ""),
            label=str(item.get("label") or ""),
            area_code=str(item.get("area_id") or ""),
            zone_type=str(item.get("area_name") or "未知"),
            available=item.get("available") is True,
            wplus=item.get("wplus") is True,
            original_price_cents=item.get("price"),
        ) for item in facts if item.get("seat_id") and item.get("label") and item.get("area_id")]

    def _cache_rows(self) -> list[sqlite3.Row]:
        settings = self._settings_provider()
        path = Path(settings.wanda_cinema_cache_path)
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3) as connection:
                connection.row_factory = sqlite3.Row
                return list(connection.execute(
                    "SELECT cinema_id, city_name, cinema_name, address FROM cinemas",
                ).fetchall())
        except sqlite3.Error:
            return []


def _find_showtime(payload: Mapping[str, Any], show_id: str) -> Mapping[str, Any] | None:
    wanted = str(show_id).strip()

    def visit(value: Any) -> Mapping[str, Any] | None:
        if isinstance(value, Mapping):
            if str(value.get("showtimeId") or "").strip() == wanted:
                return value
            for nested in value.values():
                found = visit(nested)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = visit(nested)
                if found is not None:
                    return found
        return None

    return visit(payload)
