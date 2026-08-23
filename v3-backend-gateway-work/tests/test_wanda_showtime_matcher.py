from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.schemas import Recognition
from app.wanda_showtime_matcher import ShowtimeMatcher


class Catalog:
    def contains_cinema_id(self, cinema_id: str) -> bool:
        return cinema_id == "cinema-jinan"


def recognition() -> Recognition:
    return Recognition.model_validate({
        "image_type": "SEAT_MAP", "city": "济南", "cinema": "济南万达影城世茂广场店",
        "movie": "奥德赛", "date": "2026-08-23", "showtime": "12:35", "hall": "8号厅",
        "official_selection": {"is_selected": False, "selected_seat_numbers": [], "selected_count": 0},
    })


def matched(city: str = "济南") -> dict[str, object]:
    return {"data": {
        "cityName": city,
        "cinema": {"cinemaId": "cinema-jinan", "cinemaName": f"{city}万达影城世茂广场店"},
        "showtime": {"showtimeId": "showtime-1235", "cinemaId": "cinema-jinan"},
        "showtime_match": {
            "confidence": 0.99, "candidate_count": 1,
            "components": {
                "cinema": {"status": "exact"}, "movie": {"status": "exact"},
                "date": {"status": "exact"}, "time": {"status": "exact"},
            },
        },
    }}


def test_showtime_matcher_enforces_explicit_city_as_hard_boundary() -> None:
    matcher = ShowtimeMatcher(Catalog())
    matcher.verify_joint_match(matched("济南"), "cinema-jinan", recognition())

    with pytest.raises(HTTPException, match="城市.*不一致"):
        matcher.verify_joint_match(matched("青岛"), "cinema-jinan", recognition())


def test_showtime_matcher_coalesces_identical_read_only_requests() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def match(self, _recognition: Recognition) -> dict[str, object]:
            self.calls += 1
            await asyncio.sleep(0.01)
            return matched()

    async def scenario() -> None:
        gateway = Gateway()
        matcher = ShowtimeMatcher(Catalog())
        first, second = await asyncio.gather(
            matcher.read_only_match(gateway, recognition()),
            matcher.read_only_match(gateway, recognition()),
        )
        assert first == second
        assert gateway.calls == 1
        first["data"]["cityName"] = "tampered"
        cached = await matcher.read_only_match(gateway, recognition())
        assert cached["data"]["cityName"] == "济南"

    asyncio.run(scenario())
