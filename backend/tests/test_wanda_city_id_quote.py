from __future__ import annotations

import pytest

from app.cinema_route_v2.service import CinemaRouteV2Service
from app.recognition_v2.models import RecognitionResult


class Source:
    async def get_city_list(self):
        return {"data": {"cityList": [{"cityId": "wanda-city-1", "cityName": "Quanzhou"}]}}

    async def get_cinema_list(self, wanda_city_id, longitude="0", latitude="0"):
        return {"data": {"cinemaInfoList": []}}

    async def get_showtimes(self, wanda_store_id, show_date):
        return {"data": {"showList": []}}


class IdentityStore:
    def __init__(self, item):
        self.item = item

    def find_cinema_crosswalk(self, **kwargs):
        return [self.item]


@pytest.mark.asyncio
async def test_verified_crosswalk_propagates_provider_city_id():
    route = await CinemaRouteV2Service(
        Source(),
        identity_store=IdentityStore({
            "liangpiao_cinema_id": "lp-1", "wanda_store_id": "wanda-store-1",
            "city_name": "Quanzhou", "wanda_name": "Quanzhou万达影城",
            "verification_status": "VERIFIED", "verification_level": "VERIFIED_STRONG_IDENTITY",
        }),
    ).resolve(RecognitionResult(city_text="Quanzhou", cinema_text="Quanzhou万达影城", raw_provider_result={"cinemaId": "lp-1"}))
    assert route.route == "WANDA_SELF"
    assert route.wanda_city_id == "wanda-city-1"


@pytest.mark.asyncio
async def test_crosswalk_without_provider_city_does_not_return_authorityless_wanda():
    route = await CinemaRouteV2Service(
        Source(),
        identity_store=IdentityStore({
            "liangpiao_cinema_id": "lp-1", "wanda_store_id": "wanda-store-1",
            "city_name": "MissingCity", "wanda_name": "影城",
            "verification_status": "VERIFIED",
        }),
    ).resolve(RecognitionResult(city_text="Quanzhou", cinema_text="影城"))
    assert route.wanda_city_id in {None, "wanda-city-1"}
    assert not (route.route == "WANDA_SELF" and not route.wanda_city_id)
