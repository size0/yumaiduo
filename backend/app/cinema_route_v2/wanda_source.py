from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class WandaCatalogSource(Protocol):
    """Read-only subset required by the isolated city/store resolver."""

    async def get_city_list(self) -> Mapping[str, Any]: ...

    async def get_cinema_list(
        self, wanda_city_id: str, longitude: str = "0", latitude: str = "0",
    ) -> Mapping[str, Any]: ...

    async def get_showtimes(self, wanda_store_id: str, show_date: str) -> Mapping[str, Any]: ...


def city_items(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = response.get("data")
    if not isinstance(data, Mapping):
        return []
    value = data.get("city") or data.get("cities") or data.get("cityList")
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def cinema_items(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = response.get("data")
    if not isinstance(data, Mapping):
        return []
    value = data.get("cinemaInfoList") or data.get("cinemaList")
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []
