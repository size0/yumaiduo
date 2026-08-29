from __future__ import annotations

import hashlib
import hmac

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
async def test_all_allowlisted_endpoints_are_callable_without_arbitrary_paths() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

    client = _client(handler)
    await client.recognize("https://img.example/a.jpg")
    await client.confirm("rec-1", 7)
    await client.show_list(cinemaId=1)
    await client.show_detail(showId="show-1")
    await client.seat_list(showId="show-1")
    await client.order_preflight(showId="show-1")
    await client.order_create({"showId": "show-1"}, idempotency_key="idem")
    await client.order_detail(outOrderNo="order-1")
    await client.order_cancel(outOrderNo="order-1")
    assert seen == ["seat-shot", "confirm", "list", "detail", "list", "preflight", "create", "detail", "cancel"]
    with pytest.raises(ValueError, match="endpoint_not_allowed"):
        await client.request("https://evil.example/write", {})
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
