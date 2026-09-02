from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from app.liangpiao_client import LiangpiaoClient
from app.errors import ProviderError


def _client(handler):
    transport = httpx.MockTransport(handler)
    return LiangpiaoClient(
        base_url="https://liangpiao.example/api/v1",
        app_key="app-key", app_secret="app-secret",
        http_client=httpx.AsyncClient(transport=transport),
    )


@pytest.mark.asyncio
async def test_sync_recognition_uses_provider_documented_five_second_timeout() -> None:
    class RecordingHttpClient:
        timeout: float | None = None

        async def post(self, *_args, **kwargs) -> httpx.Response:
            self.timeout = kwargs.get("timeout")
            return httpx.Response(200, json={"code": 0, "data": {"recognizeId": "1"}})

    transport = RecordingHttpClient()
    client = LiangpiaoClient(
        base_url="https://liangpiao.example/api/v1",
        app_key="app-key",
        app_secret="app-secret",
        http_client=transport,  # type: ignore[arg-type]
    )

    await client.recognize("https://img.example/a.jpg")

    assert transport.timeout == 5.0


@pytest.mark.asyncio
async def test_signed_allowlisted_request_preserves_raw_response_and_meta() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/order/create"
        assert request.headers["x-app-key"] == "app-key"
        assert request.headers["x-trace-id"] == "trace-1"
        assert request.headers["x-idempotency-key"] == "idem-1"
        payload = request.content.decode()
        expected = hmac.new(
            b"app-secret",
            ("app-key" + request.headers["x-timestamp"] + request.headers["x-nonce"] + payload).encode(),
            hashlib.sha256,
        ).hexdigest()
        assert request.headers["x-sign"] == expected
        return httpx.Response(200, json={"code": 0, "data": {"outOrderNo": "server-1"}, "requestId": "r1"})

    client = _client(handler)
    result = await client.order_create(
        {"showId": "show-1"}, idempotency_key="idem-1", trace_id="trace-1",
    )
    assert result["outOrderNo"] == "server-1"
    assert result["trace_id"] == "trace-1"
    assert result["raw_response"]["code"] == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_order_create_moves_internal_trace_id_to_header_not_provider_body() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["body"] = json.loads(request.content)
        observed["trace"] = request.headers.get("x-trace-id")
        return httpx.Response(
            200,
            json={"code": 0, "data": {"orderNo": "LP1", "status": "SUBMITTING"}},
        )

    client = _client(handler)
    await client.order_create(
        {"outOrderNo": "merchant-1", "showId": "889900", "traceId": "trace-order-1"},
    )

    assert observed["trace"] == "trace-order-1"
    assert observed["body"] == {"outOrderNo": "merchant-1", "showId": "889900"}
    await client.aclose()


@pytest.mark.asyncio
async def test_all_allowlisted_endpoints_are_callable_without_arbitrary_paths() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

    client = _client(handler)
    await client.ping()
    await client.city_list(keyword="天津")
    await client.city_locate(longitude=117.201538, latitude=39.085318)
    await client.region_list(parent_code="110100")
    await client.recognize("https://img.example/a.jpg")
    await client.recognize_async("https://img.example/a.jpg", out_trade_no="trade-1")
    await client.task_detail("123")
    await client.confirm("rec-1", 7)
    await client.brand_list(keyword="万达")
    await client.cinema_list(cityCode="120100", brandId=79)
    await client.cinema_detail(cinemaId=9107)
    await client.movie_list(movie_type="HOT", city_code="120100", page=1, page_size=50)
    await client.movie_detail(88)
    await client.show_dates(1486, movie_id=88)
    await client.show_list(cinemaId=1)
    await client.show_detail(showId="show-1")
    await client.seat_list(showId="show-1")
    await client.order_preflight(showId="show-1")
    await client.order_list(page=1, pageSize=20)
    await client.order_create({"showId": "show-1"}, idempotency_key="idem")
    await client.order_detail(orderNo="order-1")
    await client.order_cancel(orderNo="order-1", reason="买家申请拦截")
    await client.order_urge("order-1")
    await client.order_refund("order-1", reason="买家申请")
    await client.order_refund_detail("order-1")
    await client.account_balance()
    await client.account_transaction_list(page=2, page_size=100)
    assert seen == [
        "ping", "list", "locate", "list", "seat-shot", "async", "detail", "confirm",
        "list", "list", "detail", "list", "detail", "dates", "list",
        "detail", "list", "preflight", "list", "create", "detail", "cancel",
        "urge", "refund", "detail", "balance", "list",
    ]
    with pytest.raises(ValueError, match="endpoint_not_allowed"):
        await client.request("https://evil.example/write", {})
    await client.aclose()


@pytest.mark.asyncio
async def test_catalog_and_after_sales_methods_emit_documented_payloads() -> None:
    requests: list[tuple[str, dict[str, object], str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((
            request.url.path,
            json.loads(request.content),
            request.headers.get("x-idempotency-key"),
        ))
        return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

    client = _client(handler)
    await client.ping()
    await client.city_list(keyword="天津")
    await client.city_locate(longitude=117.201538, latitude=39.085318)
    await client.region_list(parent_code="110100")
    await client.region_list()
    await client.movie_list(
        movie_type="COMING", city_code="120100", keyword="奥德赛",
        page=2, page_size=100,
    )
    await client.movie_list()
    await client.movie_detail(88)
    await client.show_dates(1486, movie_id=88)
    await client.order_urge("lp-order-1")
    await client.order_refund("lp-order-1", reason="买家申请")
    await client.order_refund_detail("lp-order-1")
    await client.account_balance()
    await client.account_transaction_list(page=2, page_size=100)

    assert requests == [
        ("/api/v1/ping", {}, None),
        ("/api/v1/city/list", {"keyword": "天津"}, None),
        ("/api/v1/city/locate", {"longitude": 117.201538, "latitude": 39.085318}, None),
        ("/api/v1/region/list", {"parentCode": "110100"}, None),
        ("/api/v1/region/list", {}, None),
        ("/api/v1/movie/list", {
            "type": "COMING", "cityCode": "120100", "keyword": "奥德赛",
            "page": 2, "pageSize": 100,
        }, None),
        ("/api/v1/movie/list", {}, None),
        ("/api/v1/movie/detail", {"movieId": 88}, None),
        ("/api/v1/show/dates", {"cinemaId": 1486, "movieId": 88}, None),
        ("/api/v1/order/urge", {"orderNo": "lp-order-1"}, None),
        ("/api/v1/order/refund", {"orderNo": "lp-order-1", "reason": "买家申请"}, None),
        ("/api/v1/order/refund/detail", {"orderNo": "lp-order-1"}, None),
        ("/api/v1/account/balance", {}, None),
        ("/api/v1/account/transaction/list", {"page": 2, "pageSize": 100}, None),
    ]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda client: client.city_list(keyword="城" * 51), "keyword_invalid"),
        (lambda client: client.city_locate(longitude=181, latitude=39), "longitude_invalid"),
        (lambda client: client.city_locate(longitude=float("nan"), latitude=39), "longitude_invalid"),
        (lambda client: client.city_locate(longitude=117, latitude=-91), "latitude_invalid"),
        (lambda client: client.region_list(parent_code="11010"), "parent_code_invalid"),
        (lambda client: client.movie_list(movie_type="OLD"), "movie_type_invalid"),
        (lambda client: client.movie_list(city_code="12010"), "city_code_invalid"),
        (lambda client: client.movie_list(page=0), "page_invalid"),
        (lambda client: client.movie_list(page=1.5), "page_invalid"),
        (lambda client: client.movie_list(page_size=201), "page_size_invalid"),
        (lambda client: client.movie_detail(0), "movie_id_invalid"),
        (lambda client: client.movie_detail(1.5), "movie_id_invalid"),
        (lambda client: client.show_dates(0), "cinema_id_invalid"),
        (lambda client: client.order_urge(" "), "order_no_invalid"),
        (lambda client: client.order_refund("order-1", reason="理" * 256), "refund_reason_invalid"),
        (lambda client: client.order_refund_detail(""), "order_no_invalid"),
        (lambda client: client.order_detail(outOrderNo="merchant-1"), "order_detail_payload_invalid"),
        (lambda client: client.order_cancel(outOrderNo="merchant-1"), "order_cancel_payload_invalid"),
        (lambda client: client.account_transaction_list(page=0), "page_invalid"),
        (lambda client: client.account_transaction_list(page_size=101), "page_size_invalid"),
    ],
)
async def test_narrow_methods_reject_inputs_outside_documented_contract(call, error: str) -> None:
    client = _client(lambda _: httpx.Response(500))
    try:
        with pytest.raises(ValueError, match=error):
            await call(client)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_confirm_supports_any_candidate_id_and_preserves_trace_metadata() -> None:
    bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "requestId": "request-confirm", "data": {"ok": True}})

    client = _client(handler)
    movie_result = await client.confirm("rec-1", movie_id=88, city_name="厦门", trace_id="trace-confirm")
    await client.confirm("rec-1", show_id="889900")
    await client.confirm("rec-1", cinema_id=1486, movie_id=88, show_id="889900")

    assert bodies == [
        {"recognizeId": "rec-1", "movieId": 88, "cityName": "厦门"},
        {"recognizeId": "rec-1", "showId": "889900"},
        {"recognizeId": "rec-1", "cinemaId": 1486, "movieId": 88, "showId": "889900"},
    ]
    assert movie_result["request_id"] == "request-confirm"
    assert movie_result["trace_id"] == "trace-confirm"
    with pytest.raises(ValueError, match="candidate_id_required"):
        await client.confirm("rec-1")
    with pytest.raises(ValueError, match="movie_id_invalid"):
        await client.confirm("rec-1", movie_id="not-a-number")
    with pytest.raises(ValueError, match="show_id_invalid"):
        await client.confirm("rec-1", show_id="show-1")
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (httpx.Response(503, json={"code": 0}), "liangpiao_http_error"),
        (httpx.Response(200, content=b"not-json"), "liangpiao_invalid_response"),
        (httpx.Response(200, json={"code": 410, "message": "expired"}), "liangpiao_business_410"),
    ],
)
async def test_http_json_and_business_errors_are_classified(response: httpx.Response, error_code: str) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return response

    client = _client(handler)
    with pytest.raises(ProviderError) as caught:
        await client.show_list()
    assert caught.value.code == error_code
    await client.aclose()


def test_https_and_credentials_are_required() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        LiangpiaoClient(base_url="http://liangpiao.example", app_key="a", app_secret="b")
    with pytest.raises(ValueError, match="credentials"):
        LiangpiaoClient(base_url="https://liangpiao.example", app_key="", app_secret="b")
