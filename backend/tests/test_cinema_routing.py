from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.cinema_routing import CinemaRouteResolver
from app.models import MovieImageInfo


@dataclass
class FakeCinemaClient:
    details: dict[int, dict]

    async def cinema_detail(self, *, cinemaId: int):
        return self.details[cinemaId]


def recognition(**updates) -> MovieImageInfo:
    value = {
        "cinema_id": 9107,
        "cinema_name": "寰映影城（河东店）",
        "cinema_address": "津滨大道53号",
        "city": "天津",
        "city_code": "120100",
        "movie_name": "测试片",
        "date": "2026-08-30",
        "showtime_start": "17:00",
        "selected_count_visible": 2,
        "selected_seats": [
            {"seat_number": "5排6座", "row_no": 5, "col_no": 6},
            {"seat_number": "5排7座", "row_no": 5, "col_no": 7},
        ],
    }
    value.update(updates)
    value["selected_count_visible"] = len(value["selected_seats"])
    return MovieImageInfo.model_validate(value)


@pytest.mark.asyncio
async def test_wanda_brand_routes_every_seat_shape_to_wanda_quote() -> None:
    resolver = CinemaRouteResolver(
        FakeCinemaClient({9107: {"cinemaId": 9107, "brandName": "万达影城"}}),
    )

    result = await resolver.resolve(recognition())

    assert result.route == "WANDA_SELF"
    assert result.recognition.brand_name == "万达影城"


@pytest.mark.asyncio
async def test_wanda_huanying_brand_routes_to_wanda_self() -> None:
    resolver = CinemaRouteResolver(
        FakeCinemaClient({9107: {"cinemaId": 9107, "brandName": "万达寰映影城"}}),
    )

    result = await resolver.resolve(recognition())

    assert result.route == "WANDA_SELF"
    assert result.recognition.brand_name == "万达寰映影城"


@pytest.mark.asyncio
async def test_wanda_huanshi_brand_routes_to_wanda_self() -> None:
    resolver = CinemaRouteResolver(
        FakeCinemaClient({9107: {"cinemaId": 9107, "brandName": "寰時影城"}}),
    )

    result = await resolver.resolve(recognition())

    assert result.route == "WANDA_SELF"
    assert result.recognition.brand_name == "寰時影城"


@pytest.mark.asyncio
async def test_non_wanda_brand_routes_exact_seats_to_liangpiao() -> None:
    resolver = CinemaRouteResolver(
        FakeCinemaClient({9107: {"cinemaId": 9107, "brandName": "CGV影城"}}),
    )

    result = await resolver.resolve(recognition())

    assert result.route == "LIANGPIAO_EXACT"
    assert result.recognition.brand_name == "CGV影城"


@pytest.mark.asyncio
async def test_wanda_cache_match_overrides_inconsistent_other_brand_for_huanying() -> None:
    resolver = CinemaRouteResolver(
        FakeCinemaClient({9107: {"cinemaId": 9107, "brandName": "其他"}}),
        local_wanda_matcher=lambda _: {"cinema_id": "590", "match_score": 90},
    )

    result = await resolver.resolve(recognition())

    assert result.route == "WANDA_SELF"
    assert result.recognition.cinema_id == 9107  # Liangpiao namespace
    assert result.wanda_cinema_id == "590"  # local Wanda namespace


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_provider_name_identifies_non_wanda_when_brand_field_is_empty() -> None:
    resolver = CinemaRouteResolver(
        FakeCinemaClient({9107: {"cinemaId": 9107, "brandName": "", "name": "CGV影城(大卫城IMAX店)"}}),
    )

    result = await resolver.resolve(recognition(
        cinema_name="CGV影城(大卫城IMAX店)", brand_name=None,
    ))

    assert result.route == "LIANGPIAO_EXACT"


@pytest.mark.asyncio
async def test_any_official_non_wanda_cinema_with_selected_seats_uses_liangpiao() -> None:
    resolver = CinemaRouteResolver(
        FakeCinemaClient({9107: {"cinemaId": 9107, "brandName": "", "name": "大卫城影城"}}),
    )

    result = await resolver.resolve(recognition(
        cinema_name="大卫城影城", brand_name=None,
    ))

    assert result.route == "LIANGPIAO_EXACT"


@pytest.mark.asyncio
async def test_wanda_name_routes_to_wanda_even_when_brand_field_is_empty() -> None:
    resolver = CinemaRouteResolver(
        FakeCinemaClient({9107: {"cinemaId": 9107, "brandName": "", "name": "万达影城（河东店）"}}),
    )

    result = await resolver.resolve(recognition(
        cinema_name="万达影城（河东店）", brand_name=None,
    ))

    assert result.route == "WANDA_SELF"


@pytest.mark.asyncio
async def test_non_wanda_without_explicit_seats_is_unknown_not_area_quoted() -> None:
    resolver = CinemaRouteResolver(
        FakeCinemaClient({9107: {"cinemaId": 9107, "brandName": "CGV影城"}}),
    )

    result = await resolver.resolve(recognition(selected_seats=[]))

    assert result.route == "UNKNOWN"
    assert "明确座位" in result.reason
