from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.cinema_route_v2.service import CinemaRouteV2Service
from app.recognition_v2.models import RecognitionResult
from app.rules_first_store import RulesFirstStore


class FakeSource:
    def __init__(self, cinemas: list[dict[str, Any]], showtimes: dict[str, list[dict[str, Any]]]) -> None:
        self.cinemas = cinemas
        self.showtimes = showtimes
        self.cinema_calls = 0
        self.showtime_calls: list[tuple[str, str]] = []

    async def get_city_list(self) -> dict[str, Any]:
        return {"data": {"cities": [{"id": "1501", "name": "呼和浩特"}]}}

    async def get_cinema_list(self, city_id: str, longitude: str = "0", latitude: str = "0") -> dict[str, Any]:
        self.cinema_calls += 1
        return {"data": {"cinemaList": self.cinemas}}

    async def get_showtimes(self, store_id: str, show_date: str) -> dict[str, Any]:
        self.showtime_calls.append((store_id, show_date))
        return {"data": {"showtimeList": self.showtimes.get(store_id, [])}}


def recognition(**updates: Any) -> RecognitionResult:
    values = {
        "city_text": "呼和浩特", "cinema_text": "万达影城",
        "movie": "奥德赛", "show_date": "2026-09-06", "start_time": "18:50",
        "raw_provider_result": {"data": {"finalResults": {"cinemaId": 4748}}},
    }
    values.update(updates)
    return RecognitionResult.model_validate(values)


def cinema(store: str, name: str, address: str) -> dict[str, Any]:
    return {"storeId": store, "cinemaName": name, "cityName": "呼和浩特", "address": address}


def show(movie: str = "奥德赛", date: str = "2026-09-06", start: str = "18:50", **extra: Any) -> dict[str, Any]:
    return {"showId": f"show-{start}", "movieName": movie, "showDate": date,
            "startTime": start, "hallName": "1号厅", **extra}


@pytest.mark.asyncio
async def test_verified_crosswalk_is_first_and_does_not_use_liangpiao_id_as_wanda_id(tmp_path: Path) -> None:
    source = FakeSource([cinema("625", "呼和浩特回民区万达广场店", "回民区")], {})
    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    store.save_cinema_crosswalk({
        "canonical_cinema_identity_id": "cin-crosswalk-1", "liangpiao_cinema_id": "4748",
        "wanda_store_id": "625", "city_name": "呼和浩特", "liangpiao_name": "万达影城",
        "wanda_name": "呼和浩特回民区万达广场店", "verification_status": "VERIFIED",
        "verification_level": "NAME_ADDRESS", "evidence": {"source": "operator_audit"},
    })

    result = await CinemaRouteV2Service(source, identity_store=store).resolve(recognition())

    assert result.route == "WANDA_SELF"
    assert result.wanda_store_id == "625"
    assert result.wanda_store_id != "4748"
    assert result.canonical_cinema_identity_id == "cin-crosswalk-1"
    assert result.resolution_reason == "VERIFIED_PROVIDER_CROSSWALK"
    assert source.cinema_calls == 0


@pytest.mark.asyncio
async def test_unique_show_fingerprint_is_cached_and_first_observation_is_not_verified(tmp_path: Path) -> None:
    source = FakeSource(
        [cinema("625", "呼和浩特回民区万达广场店", "回民区"), cinema("338", "呼和浩特万达广场店", "赛罕区")],
        {"625": [show()], "338": [show(movie="别的电影")]},
    )
    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    service = CinemaRouteV2Service(source, identity_store=store)

    first = await service.resolve(recognition(cinema_text="万达影城（万达广场店）"))
    second = await service.resolve(recognition(cinema_text="万达影城（万达广场店）"))

    assert first.route == second.route == "WANDA_SELF"
    assert first.wanda_store_id == second.wanda_store_id == "625"
    assert first.verification_level == "FINGERPRINT_CANDIDATE"
    assert second.verification_level == "FINGERPRINT_CANDIDATE"
    stored = store.list_cinema_crosswalks(city_name="呼和浩特")[0]
    assert stored["verification_status"] == "CANDIDATE"
    assert len(stored["evidence"]["show_fingerprints"]) == 1
    assert len(source.showtime_calls) == 2
    assert len(store.list_cinema_crosswalks(city_name="呼和浩特")) == 1
    assert source.showtime_calls == [("625", "20260906"), ("338", "20260906")]


@pytest.mark.asyncio
async def test_different_show_fingerprint_can_upgrade_same_crosswalk(tmp_path: Path) -> None:
    source = FakeSource(
        [cinema("625", "呼和浩特回民区万达广场店", "回民区"), cinema("338", "呼和浩特万达广场店", "赛罕区")],
        {"625": [show(), show(date="2026-09-07")], "338": [show(movie="别的电影"), show(movie="别的电影", date="2026-09-07")]},
    )
    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    service = CinemaRouteV2Service(source, identity_store=store)

    first = await service.resolve(recognition(cinema_text="万达影城"))
    second = await service.resolve(recognition(cinema_text="万达影城", show_date="2026-09-07"))

    assert first.wanda_store_id == second.wanda_store_id == "625"
    assert first.verification_status == "CANDIDATE"
    assert second.verification_status == "VERIFIED"
    assert second.verification_level == "VERIFIED_REPEATED_UNIQUE"
    evidence = store.list_cinema_crosswalks(city_name="呼和浩特")[0]["evidence"]
    assert len(evidence["show_fingerprints"]) == 2


@pytest.mark.asyncio
async def test_independent_address_evidence_can_verify_crosswalk(tmp_path: Path) -> None:
    source = FakeSource(
        [cinema("625", "呼和浩特回民区万达广场店", "回民区"), cinema("338", "呼和浩特万达广场店", "赛罕区")],
        {},
    )
    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    result = await CinemaRouteV2Service(source, identity_store=store).resolve(
        recognition(
            cinema_text="万达影城", raw_provider_result={
                "data": {"finalResults": {"cinemaId": 4748, "districtName": "回民区"}},
            },
        ),
    )

    assert result.route == "WANDA_SELF"
    assert result.wanda_store_id == "625"
    assert result.verification_status == "VERIFIED"
    assert result.verification_level == "VERIFIED_STRONG_IDENTITY"


@pytest.mark.asyncio
async def test_multiple_show_fingerprint_matches_fail_closed_without_first_candidate(tmp_path: Path) -> None:
    source = FakeSource(
        [cinema("625", "呼和浩特回民区万达广场店", "回民区"), cinema("338", "呼和浩特万达广场店", "赛罕区")],
        {"625": [show()], "338": [show()]},
    )

    result = await CinemaRouteV2Service(source).resolve(recognition(cinema_text="万达影城"))

    assert result.route == "UNRESOLVED"
    assert result.wanda_store_id is None
    assert result.resolution_reason == "SHOW_FINGERPRINT_NOT_UNIQUE"
    assert {item.wanda_store_id for item in result.candidates} == {"625", "338"}


@pytest.mark.asyncio
async def test_hall_dimension_and_language_can_disambiguate_show_fingerprint(tmp_path: Path) -> None:
    source = FakeSource(
        [cinema("625", "呼和浩特回民区万达广场店", "回民区"), cinema("338", "呼和浩特万达广场店", "赛罕区")],
        {"625": [show(hallName="IMAX厅", dimension="IMAX", language="国语")],
         "338": [show(hallName="普通厅", dimension="2D", language="国语")]},
    )

    result = await CinemaRouteV2Service(source).resolve(
        recognition(cinema_text="万达影城", hall="IMAX厅", dimension="IMAX", language="国语"),
    )

    assert result.route == "WANDA_SELF"
    assert result.wanda_store_id == "625"
    assert result.resolution_reason == "UNIQUE_SHOW_FINGERPRINT_MATCH"
