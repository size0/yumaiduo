from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.liangpiao_recognition import LiangpiaoRecognitionClient


@pytest.mark.asyncio
async def test_liangpiao_recognizes_candidates_and_maps_structured_facts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1/recognize/seat-shot"
        return httpx.Response(200, json={
            "code": 0,
            "requestId": "req-1",
            "data": {
                "recognizeId": "13576",
                "rawResults": {
                    "platform": "万达",
                    "confidence": 0.95,
                    "seat": [{"seatName": "6排11座", "seatPrice": "45.9"}],
                },
                "finalResults": {
                    "matchLevel": "CANDIDATE",
                    "city": "厦门",
                    "cinema": "万达影城（鹭港广场CINITY店）",
                    "film": "欢迎来龙餐馆",
                    "showtime": "2026-08-28 22:40:00",
                    "hall": "1号CINITY厅",
                    "seat": [{"seatName": "6排11座", "seatPriceFen": "4590"}],
                    "candidates": {"cinemas": [
                        {"cinemaId": 10027, "name": "万达影城（鹭港广场CINITY店）", "cityName": "厦门", "address": "地址一", "score": 1},
                        {"cinemaId": 1900, "name": "万达影城（正翔广场CINITY店）", "cityName": "包头", "address": "地址二", "score": 0.79},
                    ]},
                },
            },
        })

    client = LiangpiaoRecognitionClient(
        Settings(liangpiao_app_key="app-key", liangpiao_app_secret="app-secret"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        result = await client.recognize_url("https://img.alicdn.com/seat.webp")
    finally:
        await client.aclose()

    assert result.recognition_id == "13576"
    assert result.match_level == "CANDIDATE"
    assert result.city == "厦门"
    assert result.movie_name == "欢迎来龙餐馆"
    assert [item.name for item in result.candidate_cinemas] == [
        "万达影城（鹭港广场CINITY店）", "万达影城（正翔广场CINITY店）"
    ]
    assert result.selected_seats[0].seat_number == "6排11座"
    body = requests[0].content.decode()
    assert json.loads(body) == {"imageUrl": "https://img.alicdn.com/seat.webp"}
    assert requests[0].headers["x-app-key"] == "app-key"
    assert requests[0].headers["x-sign"]


def test_liangpiao_recognition_routes_from_final_seats_even_when_show_is_unmatched() -> None:
    result = LiangpiaoRecognitionClient._map_result({
        "recognizeId": "14837",
        "rawResults": {
            "isSeatSelection": True,
            "city": "锦州",
            "cinema": "万达影城(IMAX锦州万...",
            "showtime": "2026-08-30 18:50:00",
            "seat": [{"seatName": "10排17座", "seatPrice": "67.9"}],
        },
        "finalResults": {
            "matchLevel": "NONE",
            "city": "锦州",
            "cinema": "万达影城（IMAX锦州万达广场店）",
            "film": None,
            "showtime": "2026-08-30 18:50:00",
            "hall": "5号IMAX厅",
            "seat": [{"seatName": "10排17座", "seatPriceFen": "6790", "rowNo": 10, "colNo": 17}],
            "showId": None,
        },
    })

    assert result.match_level == "NONE"
    assert result.movie_name is None
    assert result.selected_count_visible == 1
    assert result.selected_seats[0].seat_number == "10排17座"
    assert result.selected_seats[0].displayed_price == 67.9
    assert result.fulfillment_route == "LIANGPIAO_AUTO"


@pytest.mark.asyncio
async def test_liangpiao_confirmation_returns_new_recognition_for_repricing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/recognize/confirm"
        assert json.loads(request.content) == {"recognizeId": "13576", "cinemaId": 10027}
        return httpx.Response(200, json={
            "code": 0,
            "requestId": "req-2",
            "data": {"recognizeId": "13576", "finalResults": {
                "matchLevel": "EXACT", "city": "厦门", "cinema": "万达影城（鹭港广场CINITY店）",
                "film": "欢迎来龙餐馆", "showtime": "2026-08-28 22:40:00", "hall": "1号CINITY厅",
                "showId": "13927898", "seat": [{"seatName": "6排11座", "seatPriceFen": "4590"}],
            }},
        })

    client = LiangpiaoRecognitionClient(
        Settings(liangpiao_app_key="app-key", liangpiao_app_secret="app-secret"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        result = await client.confirm("13576", 10027)
    finally:
        await client.aclose()

    assert result.match_level == "EXACT"
    assert result.show_id == "13927898"
    assert result.cinema_name == "万达影城（鹭港广场CINITY店）"
