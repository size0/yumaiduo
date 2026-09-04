from __future__ import annotations

from typing import Any

import pytest

from app.show_resolve_v2.service import ShowResolveV2Service


class FakeWandaShowSource:
    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response or {"code": 0, "data": {"showtimeFilmInf": []}}
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def get_showtimes(self, wanda_store_id: str, show_date: str) -> dict[str, Any]:
        self.calls.append((wanda_store_id, show_date))
        if self.error:
            raise self.error
        return self.response


def show(
    show_id: str,
    movie: str = "奥德赛",
    start: str = "22:00",
    hall: str = "1号IMAX激光-COLA厅",
    language: str = "英文",
    dimension: str = "IMAX2D",
) -> dict[str, Any]:
    return {
        "showtimeId": show_id,
        "cinemaId": "324",
        "filmId": "film-56",
        "realtime": f"2026-09-03T{start}:00+08:00",
        "hallId": "hall-1",
        "hallName": hall,
        "salesPrice": 6200,
        "minAreaPrice": 5800,
        "wPlusActivityPrice": 5816,
        "wPlusActivityCode": "display-hint-code",
        "versionLanguage": language,
        "dimension": dimension,
        "filmName": movie,
    }


def response(*shows: dict[str, Any], date: str = "20260903") -> dict[str, Any]:
    return {"code": 0, "data": {"showtimeFilmInf": [{
        "filmName": shows[0].get("filmName", "奥德赛") if shows else "奥德赛",
        "showtimeFilmDateInf": [{
            "date": date,
            "showtimesInf": {"showtimeList": list(shows)},
        }],
    }]}}


def request(**updates: Any) -> dict[str, Any]:
    return {
        "wanda_store_id": "315",
        "movie": "奥德赛",
        "show_date": "2026-09-03",
        "start_time": "22:00",
        "hall": "1号IMAX激光COLA厅",
        "language": "英语",
        "dimension": "IMAX2D",
        **updates,
    }


@pytest.mark.asyncio
async def test_unique_movie_date_time_resolves_wanda_show() -> None:
    source = FakeWandaShowSource(response(show("show-22")))
    result = await ShowResolveV2Service(source).resolve(request())
    assert result.status == "RESOLVED"
    assert result.wanda_show_id == "show-22"
    assert result.wanda_store_id == "315"
    assert result.wanda_film_id == "film-56"
    assert result.sales_price_fen == 6200
    assert result.min_area_price_fen == 5800
    assert result.wplus_activity_price_fen == 5816
    assert result.wplus_activity_code_hint == "display-hint-code"
    assert source.calls == [("315", "20260903")]


@pytest.mark.asyncio
async def test_hall_is_not_required_for_unique_core_match() -> None:
    source = FakeWandaShowSource(response(show("show-22")))
    result = await ShowResolveV2Service(source).resolve(request(hall=None))
    assert result.status == "RESOLVED"
    assert result.wanda_show_id == "show-22"


@pytest.mark.asyncio
async def test_hall_format_noise_does_not_break_unique_match() -> None:
    source = FakeWandaShowSource(response(show(
        "show-22", hall="1号IMAX激光-COLA厅（儿童需购票）",
    )))
    result = await ShowResolveV2Service(source).resolve(request())
    assert result.status == "RESOLVED"


@pytest.mark.asyncio
async def test_duplicate_same_time_can_be_resolved_by_hall() -> None:
    source = FakeWandaShowSource(response(
        show("show-imax", hall="1号IMAX激光-COLA厅"),
        show("show-normal", hall="2号普通厅", dimension="2D"),
    ))
    result = await ShowResolveV2Service(source).resolve(request())
    assert result.status == "RESOLVED"
    assert result.wanda_show_id == "show-imax"


@pytest.mark.asyncio
async def test_duplicate_same_time_without_hall_requires_candidate() -> None:
    source = FakeWandaShowSource(response(
        show("show-imax"),
        show("show-normal", hall="2号普通厅", dimension="2D"),
    ))
    result = await ShowResolveV2Service(source).resolve(request(hall=None, dimension=None, language=None))
    assert result.status == "CANDIDATE_REQUIRED"
    assert result.wanda_show_id is None
    assert result.candidate_count == 2
    assert {item.wanda_show_id for item in result.candidates} == {"show-imax", "show-normal"}


@pytest.mark.asyncio
async def test_duplicate_remains_candidate_when_auxiliary_fields_do_not_disambiguate() -> None:
    source = FakeWandaShowSource(response(
        show("show-1", hall="1号普通厅", dimension="2D"),
        show("show-2", hall="1号普通厅", dimension="2D"),
    ))
    result = await ShowResolveV2Service(source).resolve(request(hall="1号普通厅", dimension="2D", language="国语"))
    assert result.status == "CANDIDATE_REQUIRED"
    assert result.candidate_count == 2


@pytest.mark.asyncio
async def test_movie_or_time_mismatch_is_not_found() -> None:
    source = FakeWandaShowSource(response(show("show-1", movie="另一部电影", start="21:55")))
    movie_mismatch = await ShowResolveV2Service(source).resolve(request())
    time_mismatch = await ShowResolveV2Service(source).resolve(request(movie="另一部电影", start_time="22:00"))
    assert movie_mismatch.status == "NOT_FOUND"
    assert time_mismatch.status == "NOT_FOUND"


@pytest.mark.asyncio
async def test_provider_failure_is_not_not_found() -> None:
    source = FakeWandaShowSource(error=TimeoutError("timeout"))
    result = await ShowResolveV2Service(source).resolve(request())
    assert result.status == "PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_store_id_is_required_and_no_cinema_route_is_called() -> None:
    source = FakeWandaShowSource(response(show("show-1")))
    result = await ShowResolveV2Service(source).resolve(request(wanda_store_id=None))
    assert result.status == "INPUT_INCOMPLETE"
    assert source.calls == []


@pytest.mark.asyncio
async def test_liangpiao_final_show_id_is_ignored() -> None:
    source = FakeWandaShowSource(response(show("wanda-show-1")))
    value = request()
    value["raw_provider_result"] = {"data": {"finalResults": {"showId": "liangpiao-show"}}}
    result = await ShowResolveV2Service(source).resolve(value)
    assert result.status == "RESOLVED"
    assert result.wanda_show_id == "wanda-show-1"


@pytest.mark.asyncio
async def test_first_provider_candidate_is_not_selected_when_core_is_ambiguous() -> None:
    source = FakeWandaShowSource(response(
        show("first", hall="3号普通厅"),
        show("second", hall="4号普通厅"),
    ))
    result = await ShowResolveV2Service(source).resolve(request(hall=None, dimension=None, language=None))
    assert result.status == "CANDIDATE_REQUIRED"
    assert result.wanda_show_id is None


@pytest.mark.asyncio
async def test_time_matching_is_exact_to_the_minute() -> None:
    source = FakeWandaShowSource(response(show("show-1", start="21:55"), show("show-2", start="22:05")))
    result = await ShowResolveV2Service(source).resolve(request())
    assert result.status == "NOT_FOUND"


@pytest.mark.asyncio
async def test_bracketed_movie_title_normalizes_without_fuzzy_matching() -> None:
    source = FakeWandaShowSource(response(show("show-1", movie="《奥德赛》")))
    result = await ShowResolveV2Service(source).resolve(request())
    assert result.status == "RESOLVED"
    assert result.wanda_show_id == "show-1"


@pytest.mark.asyncio
async def test_real_wanda_nested_film_list_shape_is_supported() -> None:
    source = FakeWandaShowSource({"code": 0, "data": {"showtimeFilmInf": [{
        "filmId": 56,
        "showtimeFilmDateInf": [{
            "date": 20260903,
            "showtimesInf": {"showtimeList": [{
                "showtimeId": 101277935,
                "realtime": 1788444000000,
                "hallName": "1号IMAX激光-COLA厅（儿童需购票）",
                "filmList": [{
                    "filmName": "奥德赛", "language": "英文",
                    "version": "IMAX2D", "versionLanguage": "IMAX2D/英文",
                }],
            }]},
        }],
    }]}})
    result = await ShowResolveV2Service(source).resolve(request())
    assert result.status == "RESOLVED"
    assert result.wanda_show_id == "101277935"
    assert result.movie_name == "奥德赛"
    assert result.language == "英文"
    assert result.dimension == "IMAX2D"
    assert result.wanda_film_id == "56"
    assert result.sales_price_fen is None
    assert result.min_area_price_fen is None
    assert result.wplus_activity_price_fen is None
    assert result.wplus_activity_code_hint is None


@pytest.mark.asyncio
async def test_different_movies_at_same_time_are_not_cross_selected() -> None:
    source = FakeWandaShowSource(response(
        show("show-1", movie="另一部电影"),
        show("show-2", movie="奥德赛"),
    ))
    result = await ShowResolveV2Service(source).resolve(request(hall=None, dimension=None, language=None))
    assert result.status == "RESOLVED"
    assert result.wanda_show_id == "show-2"
