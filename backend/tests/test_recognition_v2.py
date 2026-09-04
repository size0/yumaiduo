from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.errors import ProviderError
from app.recognition_v2.liangpiao import LiangpiaoV2Transport
from app.recognition_v2.manual_mark import ManualMarkDetector
from app.recognition_v2.service import RecognitionV2Service, _normalize


def _provider_payload(
    *,
    seats: list[dict[str, Any]] | None = None,
    city: str | None = "深圳",
    cinema_truncated: bool = False,
) -> dict[str, Any]:
    return {
        "code": 0,
        "requestId": "req-v2-1",
        "data": {
            "recognizeId": "rec-v2-1",
            "rawResults": {
                "isSeatSelection": True,
                "platform": "万达",
                "city": city,
                "cinema": "深圳测试影院",
                "cinemaTruncated": cinema_truncated,
                "film": "奥德赛",
                "showtime": "2026-09-03 22:00:00",
                "hall": "IMAX厅",
                "language": "国语",
                "dimension": "IMAX",
                "seat": seats if seats is not None else [{"seatName": "10排15座", "seatPrice": "66.9"}],
                "priceAll": "66.9",
                "confidence": 0.91,
            },
            "finalResults": {
                "cinemaId": 999001,
                "showId": "legacy-show-must-not-promote",
                "prices": [{"price": "1"}],
            },
        },
    }


def _service(
    response_factory: Callable[[httpx.Request], httpx.Response],
) -> tuple[RecognitionV2Service, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(response_factory))
    transport = LiangpiaoV2Transport(
        Settings(liangpiao_app_key="app-key", liangpiao_app_secret="app-secret"),
        http_client=http_client,
    )
    return RecognitionV2Service(transport), http_client


@pytest.mark.asyncio
async def test_success_uses_raw_results_and_keeps_final_ids_raw_only() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_provider_payload())

    service, client = _service(handler)
    try:
        result = await service.recognize(
            "https://img.example/seat.webp",
            trace_id="trace-v2",
            idempotency_key="idem-v2",
        )
    finally:
        await service.aclose()
        await client.aclose()

    assert result.source == "LIANGPIAO"
    assert result.provider_recognize_id == "rec-v2-1"
    assert result.platform_text == "万达"
    assert result.city_text == "深圳"
    assert result.cinema_text == "深圳测试影院"
    assert result.cinema_truncated is False
    assert result.movie == "奥德赛"
    assert result.show_date == "2026-09-03"
    assert result.start_time == "22:00"
    assert result.hall == "IMAX厅"
    assert result.language == "国语"
    assert result.dimension == "IMAX"
    assert result.selected_seats == ["10排15座"]
    assert result.has_selected_seats is True
    assert result.image_total_price_fen == 6690
    assert result.confidence == 0.91
    assert result.has_manual_mark is None
    assert result.raw_provider_result["data"]["finalResults"]["cinemaId"] == 999001
    assert result.raw_provider_result["data"]["finalResults"]["showId"] == "legacy-show-must-not-promote"
    assert "cinema_id" not in result.model_dump()
    assert "show_id" not in result.model_dump()
    assert json.loads(requests[0].content) == {"imageUrl": "https://img.example/seat.webp"}
    assert requests[0].headers["x-trace-id"] == "trace-v2"
    assert requests[0].headers["idempotency-key"] == "idem-v2"


@pytest.mark.asyncio
async def test_nonempty_seat_is_selected_even_when_provider_flag_is_false() -> None:
    service, client = _service(
        lambda _: httpx.Response(200, json=_provider_payload(
            seats=[{"seatName": "10排15座", "seatPrice": "66.9"}],
        )),
    )
    try:
        result = await service.recognize("https://img.example/seat.webp")
    finally:
        await service.aclose()
        await client.aclose()
    assert result.selected_seats == ["10排15座"]
    assert result.has_selected_seats is True


@pytest.mark.asyncio
async def test_empty_seat_is_not_selected_even_when_is_seat_selection_is_true() -> None:
    service, client = _service(
        lambda _: httpx.Response(200, json=_provider_payload(seats=[])),
    )
    try:
        result = await service.recognize("https://img.example/area.webp")
    finally:
        await service.aclose()
        await client.aclose()
    assert result.selected_seats == []
    assert result.has_selected_seats is False
    assert result.raw_provider_result["data"]["rawResults"]["isSeatSelection"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manual_mark", "seats", "expected_mark", "expected_selected"),
    [
        (True, [], True, False),
        (True, [{"seatName": "10排15座"}], True, True),
        (False, [{"seatName": "10排15座"}], False, True),
        (False, [], False, False),
    ],
)
async def test_four_fact_combinations_are_reported_without_request_type(
    manual_mark: bool,
    seats: list[dict[str, Any]],
    expected_mark: bool,
    expected_selected: bool,
) -> None:
    payload = _provider_payload(seats=seats)
    result = _normalize(
        payload["data"],
        raw_provider_result=payload,
        has_manual_mark=manual_mark,
    )
    assert result.has_manual_mark is expected_mark
    assert result.has_selected_seats is expected_selected
    assert not hasattr(result, "seat_request_type")


@pytest.mark.asyncio
async def test_optional_raw_fields_are_normalized_without_final_override() -> None:
    service, client = _service(
        lambda _: httpx.Response(200, json=_provider_payload(city=None, cinema_truncated=True)),
    )
    try:
        result = await service.recognize("https://img.example/case.webp")
    finally:
        await service.aclose()
        await client.aclose()
    assert result.city_text is None
    assert result.cinema_truncated is True
    assert result.movie == "奥德赛"
    assert result.show_date == "2026-09-03"
    assert result.start_time == "22:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_manual_mark", [True, False])
async def test_real_manual_mark_detector_accepts_only_boolean_json(has_manual_mark: bool) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert len(body["messages"]) == 2
        assert "cinema" not in json.dumps(body, ensure_ascii=False).lower()
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"has_manual_mark": has_manual_mark})}}],
        })

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    detector = ManualMarkDetector(
        settings=Settings(api_key="vision-key"),
        http_client=http_client,
    )
    try:
        result = await detector.detect("https://img.example/case.webp")
    finally:
        await detector.aclose()
        await http_client.aclose()
    assert result is has_manual_mark
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500, json={"error": "server"}),
        httpx.Response(200, content=b"not-json"),
    ],
)
async def test_real_manual_mark_detector_failure_returns_null(failure: httpx.Response) -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: failure))
    detector = ManualMarkDetector(
        settings=Settings(api_key="vision-key"),
        http_client=http_client,
    )
    try:
        result = await detector.detect("https://img.example/case.webp")
    finally:
        await detector.aclose()
        await http_client.aclose()
    assert result is None


@pytest.mark.asyncio
async def test_real_manual_mark_detector_timeout_returns_null() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("vision timeout")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    detector = ManualMarkDetector(
        settings=Settings(api_key="vision-key"),
        http_client=http_client,
    )
    try:
        result = await detector.detect("https://img.example/case.webp")
    finally:
        await detector.aclose()
        await http_client.aclose()
    assert result is None


@pytest.mark.asyncio
async def test_base_recognition_does_not_call_manual_mark_detector() -> None:
    service, client = _service(
        lambda _: httpx.Response(200, json=_provider_payload()),
    )
    try:
        result = await service.recognize("https://img.example/case.webp")
    finally:
        await service.aclose()
        await client.aclose()
    assert result.movie == "奥德赛"
    assert result.has_manual_mark is None
    assert result.selected_seats == ["10排15座"]


@pytest.mark.asyncio
async def test_provider_timeout_is_exposed_as_provider_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timeout")

    service, client = _service(handler)
    try:
        with pytest.raises(ProviderError) as error:
            await service.recognize("https://img.example/case.webp")
    finally:
        await service.aclose()
        await client.aclose()
    assert error.value.code == "liangpiao_network_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"code": 1001, "message": "rejected"}),
        httpx.Response(200, content=b"not-json"),
    ],
)
async def test_provider_business_error_and_malformed_json_are_rejected(
    response: httpx.Response,
) -> None:
    service, client = _service(lambda _: response)
    try:
        with pytest.raises(ProviderError):
            await service.recognize("https://img.example/case.webp")
    finally:
        await service.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_authentication_is_not_in_result_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    service, client = _service(lambda _: httpx.Response(200, json=_provider_payload()))
    try:
        result = await service.recognize("https://img.example/case.webp")
    finally:
        await service.aclose()
        await client.aclose()
    serialized = json.dumps(result.model_dump(), ensure_ascii=False)
    assert "app-secret" not in serialized
    assert "Authorization" not in serialized
    assert "Cookie" not in serialized
    assert "Token" not in serialized
    assert "app-secret" not in caplog.text
    assert "x-sign" not in caplog.text


@pytest.mark.asyncio
async def test_price_is_image_fact_not_final_sale_quote() -> None:
    service, client = _service(lambda _: httpx.Response(200, json=_provider_payload()))
    try:
        result = await service.recognize("https://img.example/case.webp")
    finally:
        await service.aclose()
        await client.aclose()
    assert result.image_total_price_fen == 6690
    assert "quote" not in result.model_dump()
    assert "pricing" not in result.model_dump()
