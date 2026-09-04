from __future__ import annotations

from typing import Any

import pytest

from app.recognition_v2.models import RecognitionResult
from app.cinema_route_v2.service import CinemaRouteV2Service


class FakeWandaCatalog:
    def __init__(self, *, fail: bool = False, cinemas: list[dict[str, Any]] | None = None) -> None:
        self.fail = fail
        self.cinemas = cinemas or []
        self.city_calls: list[tuple[()]] = []
        self.cinema_calls: list[tuple[str, str, str]] = []

    async def get_city_list(self) -> dict[str, Any]:
        self.city_calls.append(())
        if self.fail:
            raise TimeoutError("Wanda unavailable")
        return {"data": {"city": [
            {"id": "324", "name": "哈尔滨"},
            {"id": "1", "name": "北京市"},
        ]}}

    async def get_cinema_list(self, city_id: str, lon: str = "0", lat: str = "0") -> dict[str, Any]:
        self.cinema_calls.append((city_id, lon, lat))
        if self.fail:
            raise TimeoutError("Wanda unavailable")
        return {"data": {"cinemaInfoList": self.cinemas}}


def recognition(**updates: Any) -> RecognitionResult:
    value: dict[str, Any] = {
        "source": "LIANGPIAO",
        "city_text": "哈尔滨",
        "cinema_text": "哈西万达广场店",
        "cinema_truncated": False,
        "movie": "奥德赛",
        "show_date": "2026-09-03",
        "start_time": "22:00",
        "selected_seats": [],
        "has_selected_seats": False,
        **updates,
    }
    return RecognitionResult.model_validate(value)


def cinema(
    store_id: str,
    name: str,
    address: str = "哈尔滨测试地址",
    city_name: str | None = None,
) -> dict[str, str]:
    value = {"storeId": store_id, "cinemaName": name, "address": address}
    if city_name is not None:
        value["cityName"] = city_name
    return value


@pytest.mark.asyncio
async def test_haxi_wanda_resolves_in_city_scope() -> None:
    source = FakeWandaCatalog(cinemas=[cinema("315", "哈尔滨哈西万达广场店")])
    result = await CinemaRouteV2Service(source).resolve(recognition())
    assert result.route == "WANDA_SELF"
    assert result.wanda_city_id == "324"
    assert result.wanda_store_id == "315"
    assert result.wanda_city_name == "哈尔滨"
    assert source.cinema_calls == [("324", "0", "0")]


@pytest.mark.asyncio
async def test_hadong_ruyi_resolves_without_brand_rule() -> None:
    source = FakeWandaCatalog(cinemas=[cinema(
        "1822", "儒意影城(哈东万达广场店)原万达影城",
    )])
    result = await CinemaRouteV2Service(source).resolve(recognition(cinema_text="哈东儒意影城"))
    assert result.route == "WANDA_SELF"
    assert result.wanda_store_id == "1822"


@pytest.mark.asyncio
async def test_huanying_fuli_resolves_by_landmark_tokens() -> None:
    source = FakeWandaCatalog(cinemas=[cinema("6957", "万达寰映影城（富力广场店）")])
    result = await CinemaRouteV2Service(source).resolve(recognition(cinema_text="寰映富力广场"))
    assert result.route == "WANDA_SELF"
    assert result.wanda_store_id == "6957"


@pytest.mark.asyncio
async def test_complete_non_wanda_cinema_is_liangpiao_only_after_exhaustive_wanda_lookup() -> None:
    source = FakeWandaCatalog(cinemas=[cinema("315", "哈尔滨哈西万达广场店")])
    result = await CinemaRouteV2Service(source).resolve(
        recognition(cinema_text="哈尔滨中央大街独立影城")
    )
    assert result.route == "LIANGPIAO"
    assert result.wanda_city_id == "324"
    assert result.wanda_store_id is None


@pytest.mark.asyncio
async def test_missing_city_and_truncated_cinema_is_unresolved() -> None:
    source = FakeWandaCatalog(cinemas=[cinema("315", "哈尔滨哈西万达广场店")])
    result = await CinemaRouteV2Service(source).resolve(
        recognition(city_text=None, cinema_text="万达影城 (万达广场IM...", cinema_truncated=True)
    )
    assert result.route == "UNRESOLVED"
    assert result.resolution_reason == "CITY_REQUIRED_FOR_TRUNCATED_CINEMA"
    assert source.cinema_calls == []


@pytest.mark.asyncio
async def test_truncated_name_with_unique_same_city_candidate_resolves() -> None:
    source = FakeWandaCatalog(cinemas=[cinema("315", "哈尔滨哈西万达广场店")])
    result = await CinemaRouteV2Service(source).resolve(
        recognition(cinema_text="哈西万达广...", cinema_truncated=True)
    )
    assert result.route == "WANDA_SELF"
    assert result.wanda_store_id == "315"


@pytest.mark.asyncio
async def test_truncated_name_with_multiple_candidates_returns_candidates() -> None:
    source = FakeWandaCatalog(cinemas=[
        cinema("315", "哈尔滨哈西万达广场店"),
        cinema("316", "哈尔滨哈西万达广场IMAX店"),
    ])
    result = await CinemaRouteV2Service(source).resolve(
        recognition(cinema_text="哈西万达广...", cinema_truncated=True)
    )
    assert result.route == "UNRESOLVED"
    assert result.wanda_store_id is None
    assert {candidate.wanda_store_id for candidate in result.candidates} == {"315", "316"}


@pytest.mark.asyncio
async def test_wanda_api_failure_is_not_liangpiao() -> None:
    source = FakeWandaCatalog(fail=True)
    result = await CinemaRouteV2Service(source).resolve(recognition())
    assert result.route == "UNRESOLVED"
    assert result.resolution_reason == "WANDA_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_final_provider_hints_are_ignored() -> None:
    source = FakeWandaCatalog(cinemas=[cinema("315", "哈尔滨哈西万达广场店")])
    base = recognition(city_text=None, cinema_text="万达影城 (万达广场IM...", cinema_truncated=True)
    value = base.model_copy(update={
        "raw_provider_result": {
            "data": {"finalResults": {
                "city": "呼和浩特", "cinemaId": 4748, "showId": "16293909",
            }},
        },
    })
    result = await CinemaRouteV2Service(source).resolve(value)
    assert result.route == "UNRESOLVED"
    assert result.wanda_city_id is None
    assert result.wanda_store_id is None


@pytest.mark.asyncio
async def test_city_suffix_is_conservatively_normalized() -> None:
    source = FakeWandaCatalog(cinemas=[cinema("315", "哈尔滨哈西万达广场店")])
    result = await CinemaRouteV2Service(source).resolve(recognition(city_text="哈尔滨市"))
    assert result.route == "WANDA_SELF"
    assert result.wanda_city_id == "324"


@pytest.mark.asyncio
async def test_same_name_in_other_city_is_not_considered() -> None:
    source = FakeWandaCatalog(cinemas=[cinema(
        "315", "哈尔滨哈西万达广场店", city_name="哈尔滨",
    )])
    result = await CinemaRouteV2Service(source).resolve(
        recognition(city_text="北京市", cinema_text="哈西万达广场店")
    )
    assert result.route == "LIANGPIAO"
    assert result.wanda_city_id == "1"
    assert result.wanda_store_id is None
    assert source.cinema_calls == [("1", "0", "0")]


@pytest.mark.asyncio
async def test_equal_same_city_candidates_are_not_first_wins() -> None:
    source = FakeWandaCatalog(cinemas=[
        cinema("315", "哈尔滨万达影城（中央大街店）"),
        cinema("316", "哈尔滨万达影城（中央大街IMAX店）"),
    ])
    result = await CinemaRouteV2Service(source).resolve(
        recognition(cinema_text="中央大街万达影城")
    )
    assert result.route == "UNRESOLVED"
    assert result.wanda_store_id is None
    assert result.candidate_count == 2


@pytest.mark.asyncio
async def test_brand_name_difference_does_not_block_unique_landmark_match() -> None:
    source = FakeWandaCatalog(cinemas=[cinema("6957", "万达寰映影城（富力广场店）")])
    result = await CinemaRouteV2Service(source).resolve(
        recognition(cinema_text="富力广场寰映影城")
    )
    assert result.route == "WANDA_SELF"
    assert result.wanda_store_id == "6957"


@pytest.mark.asyncio
async def test_city_lookup_failure_and_cinema_lookup_failure_share_unresolved_safety() -> None:
    class CityWorksCinemaFails(FakeWandaCatalog):
        async def get_cinema_list(self, city_id: str, lon: str = "0", lat: str = "0") -> dict[str, Any]:
            raise ConnectionError("cinema endpoint unavailable")

    city_failure = await CinemaRouteV2Service(FakeWandaCatalog(fail=True)).resolve(recognition())
    cinema_failure = await CinemaRouteV2Service(CityWorksCinemaFails()).resolve(recognition())
    assert city_failure.route == cinema_failure.route == "UNRESOLVED"
    assert city_failure.resolution_reason == cinema_failure.resolution_reason == "WANDA_PROVIDER_UNAVAILABLE"
