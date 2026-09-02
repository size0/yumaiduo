from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.liangpiao_recognition import LiangpiaoRecognitionClient
from app.errors import ProviderError


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
                "cached": False,
                "rawResults": {
                    "isSeatSelection": True,
                    "platform": "万达",
                    "cinemaTruncated": True,
                    "priceAll": "45.9",
                    "confidence": 0.95,
                    "seat": [{"seatName": "6排11座", "seatPrice": "45.9"}],
                },
                "finalResults": {
                    "matchLevel": "CANDIDATE",
                    "noMatchReason": "NOT_FOUND",
                    "city": "厦门",
                    "cinema": "万达影城（鹭港广场CINITY店）",
                    "cinemaAddress": "地址一",
                    "cinemaId": 10027,
                    "cityCode": "350200",
                    "brandName": "万达影城",
                    "film": "欢迎来龙餐馆",
                    "movieId": 88,
                    "showtime": "2026-08-28 22:40:00",
                    "hall": "1号CINITY厅",
                    "cinemaHitNums": 2,
                    "priceAllFen": "4590",
                    "priceMismatch": False,
                    "seatMatched": True,
                    "seat": [{"seatName": "6排11座", "seatPriceFen": "4590", "rowNo": 6, "colNo": 11, "seatNo": "6排11座", "areaId": "wplus", "status": "AVAILABLE"}],
                    "prices": [{
                        "ticketMode": "STANDARD", "priceMode": "FIXED", "price": "3510",
                        "maxPrice": "5200", "originalPrice": "3890",
                        "stopSaleTime": "2026-08-28T22:20:00+08:00", "available": True,
                    }],
                    "candidates": {"cinemas": [
                        {"cinemaId": 10027, "name": "万达影城（鹭港广场CINITY店）", "cityName": "厦门", "address": "地址一", "score": 1},
                        {"cinemaId": 1900, "name": "万达影城（正翔广场CINITY店）", "cityName": "包头", "address": "地址二", "score": 0.79},
                    ], "movies": [
                        {"movieId": 88, "name": "欢迎来龙餐馆", "score": 0.98},
                    ], "shows": [
                        {"showId": "13927898", "cinemaId": 10027, "movieId": 88,
                         "movieName": "欢迎来龙餐馆", "hallName": "1号CINITY厅",
                         "startTime": "2026-08-28T22:40:00+08:00", "endTime": "2026-08-29T00:20:00+08:00",
                         "dimension": "CINITY", "language": "国语", "score": 0.96},
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
    assert result.recognition_cached is False
    assert result.provider_request_id == "req-1"
    assert result.trace_id == requests[0].headers["x-trace-id"]
    assert result.raw_results["priceAll"] == "45.9"
    assert result.final_results["candidates"]["shows"][0]["showId"] == "13927898"
    assert result.raw_response["requestId"] == "req-1"
    assert result.match_level == "CANDIDATE"
    assert result.is_seat_selection is True
    assert result.cinema_truncated is True
    assert result.no_match_reason == "NOT_FOUND"
    assert result.city == "厦门"
    assert result.city_code == "350200"
    assert result.cinema_id == 10027
    assert result.cinema_address == "地址一"
    assert result.brand_name == "万达影城"
    assert result.movie_name == "欢迎来龙餐馆"
    assert result.movie_id == 88
    assert result.cinema_hit_count == 2
    assert result.price_mismatch is False
    assert result.seat_matched is True
    assert result.displayed_total == 45.9
    assert [item.name for item in result.candidate_cinemas] == [
        "万达影城（鹭港广场CINITY店）", "万达影城（正翔广场CINITY店）"
    ]
    assert result.selected_seats[0].seat_number == "6排11座"
    assert result.selected_seats[0].row_no == 6
    assert result.selected_seats[0].col_no == 11
    assert result.selected_seats[0].area_id == "wplus"
    assert result.selected_seats[0].seat_no == "6排11座"
    assert result.selected_seats[0].status == "AVAILABLE"
    assert result.provider_prices[0].ticket_mode == "STANDARD"
    assert result.provider_prices[0].price_mode == "FIXED"
    assert result.provider_prices[0].price_cents == 3510
    assert result.candidate_movies[0].movie_id == 88
    assert result.candidate_shows[0].show_id == "13927898"
    assert result.candidate_shows[0].hall_name == "1号CINITY厅"
    body = requests[0].content.decode()
    assert json.loads(body) == {"imageUrl": "https://img.alicdn.com/seat.webp"}
    assert requests[0].headers["x-app-key"] == "app-key"
    assert requests[0].headers["x-sign"]


@pytest.mark.asyncio
async def test_liangpiao_async_recognition_polls_task_and_maps_result() -> None:
    paths: list[str] = []
    traces: list[str] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        paths.append(request.url.path)
        traces.append(request.headers["x-trace-id"])
        if request.url.path.endswith("/async"):
            assert json.loads(request.content) == {
                "imageUrl": "https://img.alicdn.com/seat.webp", "outTradeNo": "event-1",
            }
            return httpx.Response(200, json={"code": 0, "data": {"taskId": "123", "status": "PENDING"}})
        assert request.url.path.endswith("/task/detail")
        assert json.loads(request.content) == {"taskId": "123"}
        return httpx.Response(200, json={"code": 0, "data": {
            "taskId": "123", "status": "SUCCESS", "result": {
                "recognizeId": "async-rec-1", "finalResults": {
                    "matchLevel": "EXACT", "city": "深圳", "cinema": "万达影城（测试店）",
                    "cinemaId": 99, "film": "测试影片", "showtime": "2026-08-30 18:50:00",
                    "showId": "show-1", "seat": [{"seatName": "5排6座", "seatPriceFen": 5000}],
                },
            },
        }})

    client = LiangpiaoRecognitionClient(
        Settings(
            liangpiao_app_key="app-key", liangpiao_app_secret="app-secret",
            liangpiao_recognition_async_enabled=True,
            liangpiao_recognition_poll_interval_seconds=0.2,
            liangpiao_recognition_poll_timeout_seconds=10,
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        result = await client.recognize_url(
            "https://img.alicdn.com/seat.webp", out_trade_no="event-1",
        )
    finally:
        await client.aclose()
    assert calls == 2
    assert paths == ["/api/v1/recognize/seat-shot/async", "/api/v1/recognize/task/detail"]
    assert len(set(traces)) == 1
    assert result.trace_id == traces[0]
    assert result.recognition_id == "async-rec-1"
    assert result.show_id == "show-1"


@pytest.mark.asyncio
async def test_sync_timeout_switches_once_to_idempotent_async_recognition() -> None:
    client = LiangpiaoRecognitionClient(
        Settings(liangpiao_app_key="app-key", liangpiao_app_secret="app-secret"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )
    client._api.recognize = AsyncMock(side_effect=ProviderError("liangpiao_network_error", "timeout"))  # type: ignore[method-assign]
    client._api.recognize_async = AsyncMock(return_value={
        "taskId": "task-1", "status": "SUCCESS", "result": {
            "recognizeId": "rec-1", "finalResults": {
                "matchLevel": "EXACT", "city": "深圳", "cinema": "影院",
                "cinemaId": 1, "film": "影片", "showtime": "2026-09-01 18:00:00",
                "showId": "show-1", "seat": [{"seatName": "5排8座", "seatPriceFen": 5000}],
            },
        },
    })
    try:
        result = await client.recognize_url(
            "https://img.alicdn.com/seat.webp", out_trade_no="stable-event-1",
        )
    finally:
        await client.aclose()

    client._api.recognize_async.assert_awaited_once()  # type: ignore[attr-defined]
    assert client._api.recognize_async.await_args.kwargs["out_trade_no"] == "stable-event-1"  # type: ignore[attr-defined]
    assert result.recognition_id == "rec-1"


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


def test_liangpiao_exact_match_trusts_authoritative_ids_despite_raw_truncation() -> None:
    result = LiangpiaoRecognitionClient._map_result({
        "recognizeId": "exact-1",
        "rawResults": {"isSeatSelection": True, "cinemaTruncated": True},
        "finalResults": {
            "matchLevel": "EXACT", "city": "上海", "cinema": "时代国际影城（金山店）",
            "cinemaId": 10027, "film": "奥德赛", "movieId": 88,
            "showtime": "2026-09-01 15:10:00", "hall": "1号厅", "showId": "show-1",
            "candidates": {"cinemas": [
                {"cinemaId": 10027, "name": "时代国际影城（金山店）"},
                {"cinemaId": 10028, "name": "时代国际影城（其他店）"},
            ]},
        },
    })

    assert result.match_level == "EXACT"
    assert result.cinema_truncated is True
    assert len(result.candidate_cinemas) == 2
    assert "cinema_name" not in result.missing_fields


def test_liangpiao_expired_show_is_preserved_as_authoritative_terminal_match() -> None:
    result = LiangpiaoRecognitionClient._map_result({
        "recognizeId": "expired-1",
        "rawResults": {"isSeatSelection": True, "cinemaTruncated": False},
        "finalResults": {
            "matchLevel": "NONE", "noMatchReason": "SHOW_EXPIRED",
            "cinemaId": 10027, "cinema": "万达影城", "movieId": 88,
            "film": "奥德赛", "showtime": "2026-09-01 15:10:00",
        },
    })

    assert result.match_level == "NONE"
    assert result.provider_match_level == "NONE"
    assert result.no_match_reason == "SHOW_EXPIRED"
    assert result.provider_no_match_reason == "SHOW_EXPIRED"
    assert result.recognition_blocker == "SHOW_EXPIRED"
    assert result.is_seat_selection is True


def test_liangpiao_preserves_complete_two_layer_contract_and_unknown_fields() -> None:
    raw_results = {
        "isSeatSelection": True,
        "cinema": "时代影城",
        "futureRawField": {"nested": [1, {"new": "value"}]},
    }
    final_results = {
        "matchLevel": "FUTURE_MATCH_LEVEL",
        "noMatchReason": "FUTURE_NO_MATCH_REASON",
        "cinema": "时代国际影城（金山店）",
        "candidates": {
            "cinemas": [{
                "cinemaId": 10027,
                "name": "时代国际影城（金山店）",
                "districtName": "金山区",
                "futureCandidateField": {"rankSignals": ["alias", "distance"]},
            }],
            "movies": [{"movieId": 88, "name": "奥德赛", "poster": "https://img.example/poster"}],
            "shows": [{"showId": "show-1", "startTime": "2026-09-01T15:10:00+08:00", "futureShowField": 7}],
            "futureCandidateGroup": [{"id": "future-1", "metadata": {"x": 1}}],
        },
        "futureFinalField": {"providerVersion": "next"},
    }
    raw_response = {
        "code": 0,
        "message": "success",
        "requestId": "provider-request-1",
        "data": {
            "recognizeId": "recognize-1",
            "cached": False,
            "rawResults": raw_results,
            "finalResults": final_results,
        },
    }

    result = LiangpiaoRecognitionClient._map_result({
        **raw_response["data"],
        "raw_response": raw_response,
        "request_id": "provider-request-1",
        "trace_id": "trace-1",
    })

    assert result.raw_results == raw_results
    assert result.final_results == final_results
    assert result.final_results["candidates"] == final_results["candidates"]
    assert result.raw_response == raw_response
    assert result.provider_request_id == "provider-request-1"
    assert result.trace_id == "trace-1"
    assert result.match_level == "FUTURE_MATCH_LEVEL"
    assert result.provider_match_level == "FUTURE_MATCH_LEVEL"
    assert result.no_match_reason == "FUTURE_NO_MATCH_REASON"
    assert result.provider_no_match_reason == "FUTURE_NO_MATCH_REASON"
    assert result.recognition_blocker is None
    assert result.cinema_name == "时代国际影城（金山店）"
    assert result.candidate_cinemas[0].cinema_id == 10027


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


@pytest.mark.asyncio
async def test_liangpiao_confirmation_accepts_movie_show_and_city_selection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/recognize/confirm"
        assert json.loads(request.content) == {
            "recognizeId": "13576",
            "movieId": 88,
            "showId": "13927898",
            "cityName": "厦门",
        }
        return httpx.Response(200, json={
            "code": 0,
            "requestId": "req-confirm",
            "data": {"recognizeId": "13576", "finalResults": {"matchLevel": "EXACT"}},
        })

    client = LiangpiaoRecognitionClient(
        Settings(liangpiao_app_key="app-key", liangpiao_app_secret="app-secret"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        result = await client.confirm(
            "13576", movie_id=88, show_id="13927898", city_name="厦门",
        )
    finally:
        await client.aclose()

    assert result.match_level == "EXACT"
    assert result.provider_request_id == "req-confirm"


@pytest.mark.asyncio
async def test_liangpiao_confirmation_requires_at_least_one_candidate_id() -> None:
    client = LiangpiaoRecognitionClient(
        Settings(liangpiao_app_key="app-key", liangpiao_app_secret="app-secret"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )
    try:
        with pytest.raises(ValueError, match="candidate_id_required"):
            await client.confirm("13576", city_name="厦门")
    finally:
        await client.aclose()
