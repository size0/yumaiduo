from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from .models import MovieImageInfo
CinemaRoute = Literal["WANDA_SELF", "LIANGPIAO_EXACT", "UNKNOWN"]


@dataclass(frozen=True)
class CinemaRouteResult:
    route: CinemaRoute
    recognition: MovieImageInfo
    reason: str = ""
    wanda_cinema_id: str | None = None


class CinemaDetailClient:
    async def cinema_detail(self, *, cinemaId: int) -> Mapping[str, object]:  # pragma: no cover - protocol shape
        raise NotImplementedError


LocalWandaMatcher = Callable[[MovieImageInfo], object | Awaitable[object]]

_WANDA_NAME_MARKERS = ("万达", "寰映", "寰时", "寰時", "方米")


class CinemaRouteResolver:
    """Resolve the quote provider from authoritative cinema identity/capability data."""

    def __init__(
        self,
        liangpiao_client: CinemaDetailClient | None = None,
        *,
        local_wanda_matcher: LocalWandaMatcher | None = None,
    ) -> None:
        self._liangpiao = liangpiao_client
        self._local_wanda_matcher = local_wanda_matcher

    async def resolve(self, recognition: MovieImageInfo) -> CinemaRouteResult:
        enriched = await self._enrich_from_liangpiao(recognition)
        brand = (enriched.brand_name or "").strip()
        cinema_name = (enriched.cinema_name or "").strip()
        known_wanda_brand = any(marker in (brand or cinema_name) for marker in _WANDA_NAME_MARKERS)
        # Always attempt the local catalog lookup, even for a known Wanda brand,
        # so the Liangpiao ID can be paired with the local Wanda ID.
        local_match = await self._local_match(enriched)
        if isinstance(local_match, Mapping):
            wanda_id = str(local_match.get("cinema_id") or "").strip() or None
            return CinemaRouteResult("WANDA_SELF", enriched, wanda_cinema_id=wanda_id)

        if known_wanda_brand:
            # 寰映、寰时、方米都是万达电影旗下品牌；良票可能只返回
            # 品牌短名或把品牌写进影院名称，不能因此降级到第三方院线。
            return CinemaRouteResult("WANDA_SELF", enriched)

        # Once the official Liangpiao record is not a Wanda venue, exact
        # selected seats are sufficient to use Liangpiao's real-time quote.
        # Without seats we deliberately remain UNKNOWN; area prices are not a
        # substitute for an official seat selection.
        if enriched.selected_seats and (brand or enriched.cinema_id is not None):
            return CinemaRouteResult("LIANGPIAO_EXACT", enriched)
        if brand or enriched.cinema_id is not None:
            return CinemaRouteResult(
                "UNKNOWN", enriched,
                reason="非万达影院需要明确座位后才能通过良票精确报价。",
            )
        return CinemaRouteResult("UNKNOWN", enriched, reason="无法确认影院所属报价能力。")

    async def _enrich_from_liangpiao(self, recognition: MovieImageInfo) -> MovieImageInfo:
        if self._liangpiao is None or recognition.cinema_id is None:
            return recognition
        try:
            detail = await self._liangpiao.cinema_detail(cinemaId=recognition.cinema_id)
        except Exception:
            return recognition
        if not isinstance(detail, Mapping):
            return recognition
        updates: dict[str, object] = {}
        for field, key in (
            ("cinema_id", "cinemaId"), ("cinema_name", "name"),
            ("cinema_address", "address"), ("brand_name", "brandName"),
            ("city_code", "cityCode"), ("city", "cityName"),
        ):
            value = detail.get(key)
            if value is not None and str(value).strip():
                updates[field] = value
        return recognition.model_copy(update=updates) if updates else recognition

    async def _local_match(self, recognition: MovieImageInfo) -> Mapping[str, object] | None:
        if self._local_wanda_matcher is None:
            return None
        value = self._local_wanda_matcher(recognition)
        if inspect.isawaitable(value):
            value = await value
        return value if isinstance(value, Mapping) else None
