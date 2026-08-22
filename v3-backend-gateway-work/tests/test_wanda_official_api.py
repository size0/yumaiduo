from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from app.wanda_direct_gateway import DirectGatewayError, build_wanda_direct_gateway_from_env
from app.wanda_official_api import JsonWandaAccountSource, WandaOfficialApiClient


ACCOUNT = {
    "phone": "13800000000",
    "token": "secret-account-token",
    "status": "online",
    "risk_status": "",
    "is_wplus": True,
    "user_info": {"userIdentifier": "official-user"},
    "shumei_box_id": "device-box",
}
APP_AES_KEY = b"6f34faeefba8fd39"


def _encrypted_hex(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return AES.new(APP_AES_KEY, AES.MODE_ECB).encrypt(pad(raw, AES.block_size)).hex()


def test_account_source_reads_only_eligible_wplus_accounts_without_mutating_file(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    records = [
        ACCOUNT,
        {**ACCOUNT, "phone": "2", "status": "offline"},
        {**ACCOUNT, "phone": "3", "is_wplus": False},
        {**ACCOUNT, "phone": "4", "token": ""},
        {**ACCOUNT, "phone": "5", "risk_status": "blocked"},
        {**ACCOUNT, "phone": "6", "remaining": 0},
        {**ACCOUNT, "phone": "7", "remaining": "1"},
    ]
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    selected = asyncio.run(JsonWandaAccountSource(path).list_accounts())

    assert len(selected) == 1
    assert selected[0]["phone"] == ACCOUNT["phone"]
    assert selected[0]["token"] == ACCOUNT["token"]
    assert json.loads(path.read_text(encoding="utf-8")) == records


def test_create_order_uses_verified_android_form_contract_not_ticket_gateway() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "front-gateway-c.wandafilm.com"
        assert request.url.path == "/order/create_order.api"
        assert request.headers["x-ry-token"] == ACCOUNT["token"]
        assert "authorization" not in request.headers
        mx = json.loads(request.headers["mx-api"])
        assert mx["cCode"] == "1_2" and mx["_mi_"] == ACCOUNT["token"]
        body = request.content.decode()
        assert body == "retailerCode=MX&mobile=13800000000&seatId=s-1%2c8000%2c0%2c0&totalPrice=8000&dId=show-1"
        return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0, "orderId": "order-1"}})

    client = WandaOfficialApiClient(ACCOUNT, transport=httpx.MockTransport(handler), timestamp_factory=lambda: 123)
    result = asyncio.run(client.create_order({
        "showtime_id": "show-1", "seat_ids": ["s-1"], "seat_payloads": ["s-1,8000,0,0"],
        "total_price_cents": 8000, "cinema_id": "cinema-1", "partition": "a-s-1",
    }))

    assert result == {"order_id": "order-1", "create_verified": True}
    assert len(requests) == 1


def test_create_order_does_not_treat_order_id_as_success_when_biz_code_is_nonzero() -> None:
    client = WandaOfficialApiClient(
        ACCOUNT,
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"code": 0, "data": {"bizCode": 17, "orderId": "order-uncertain"}}
        )),
        timestamp_factory=lambda: 123,
    )

    result = asyncio.run(client.create_order({
        "showtime_id": "show-1", "seat_ids": ["s-1"], "seat_payloads": ["s-1,8000,0,0"],
        "total_price_cents": 8000, "cinema_id": "cinema-1", "partition": "a-s-1",
    }))

    assert result == {"order_id": "order-uncertain", "create_verified": False}


def test_definitive_create_auth_rejection_is_retryable_before_any_order_exists() -> None:
    client = WandaOfficialApiClient(
        ACCOUNT,
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, json={"code": 401})),
        timestamp_factory=lambda: 123,
    )
    try:
        asyncio.run(client.create_order({
            "showtime_id": "show-1", "seat_ids": ["s-1"], "seat_payloads": ["s-1,8000,0,0"],
            "total_price_cents": 8000, "cinema_id": "cinema-1", "partition": "a-s-1",
        }))
    except DirectGatewayError as error:
        assert error.code == "pre_create_account_unavailable"
        assert error.retryable_before_create is True
    else:
        raise AssertionError("definitive account rejection was not classified")


def test_locked_activity_offer_decrypts_official_aes_ecb_response() -> None:
    groups = [{"groupName": "会员优惠", "groupType": 1, "groupItems": [{
        "name": "W+会员专享优惠", "able": True, "code": "wplus",
        "allotSeat": json.dumps({"totalPayPrice": 6190}),
    }]}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/order/order_status.api":
            return httpx.Response(200, json={"code": 0, "data": {"orderStatus": "40", "lockSeatTime": 120}})
        assert request.url.host == "mkt-activity-api-prd-mx.wandafilm.com"
        assert request.url.path == "/mkt/activity/secret/list.api"
        query = parse_qs(request.url.query.decode())
        assert query == {"partition": ["a-s-1"], "orderId": ["order-1"], "did": ["show-1"]}
        return httpx.Response(200, json={"code": 0, "data": _encrypted_hex(groups)})

    client = WandaOfficialApiClient(ACCOUNT, transport=httpx.MockTransport(handler), timestamp_factory=lambda: 123)
    offers = asyncio.run(client.activity_offers(order_id="order-1", cinema_id="cinema-1", showtime_id="show-1", partition="a-s-1"))

    assert offers == {"activities": [{
        "group": "会员优惠", "group_type": 1, "code": "wplus", "name": "W+会员专享优惠",
        "able": True, "recommend": False, "price": 0, "allot_seat": {"totalPayPrice": 6190},
    }]}


def test_activity_offer_is_blocked_unless_order_status_is_locked_and_lock_time_is_nonnegative() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"code": 0, "data": {"orderStatus": "20", "lockSeatTime": -1}})

    client = WandaOfficialApiClient(ACCOUNT, transport=httpx.MockTransport(handler), timestamp_factory=lambda: 123)
    try:
        asyncio.run(client.activity_offers(order_id="order-1", cinema_id="cinema-1", showtime_id="show-1", partition="a-s-1"))
    except DirectGatewayError as error:
        assert error.code == "temporary_lock_state_unknown"
    else:
        raise AssertionError("unconfirmed lock was allowed to query W+ offers")
    assert paths == ["/order/order_status.api"] * 3


def test_cancel_status_and_realtime_seats_use_only_reviewed_official_paths() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/order/cancel.api":
            return httpx.Response(200, json={"code": 0, "success": True})
        if request.url.path == "/order/order_status.api":
            return httpx.Response(200, json={"code": 0, "data": {"orderStatus": "60", "lockSeatTime": -1}})
        if request.url.path == "/order/real_time_seat.api":
            return httpx.Response(200, json={"code": 0, "data": {"area": [{"seat": [{"seatId": "s-1", "status": 1}]}]}})
        raise AssertionError(request.url)

    client = WandaOfficialApiClient(ACCOUNT, transport=httpx.MockTransport(handler), timestamp_factory=lambda: 123)
    assert asyncio.run(client.cancel_order("order-1")) is True
    seats = asyncio.run(client.realtime_seats("show-1"))
    assert seats["available_seat_ids"] == ["s-1"]
    assert paths == ["/order/cancel.api", "/order/order_status.api", "/order/real_time_seat.api"]
    assert all("/api/order" not in path for path in paths)


def test_cancel_is_not_verified_when_lock_time_has_not_cleared() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/order/cancel.api":
            return httpx.Response(200, json={"code": 0, "success": True})
        return httpx.Response(200, json={"code": 0, "data": {"orderStatus": "60", "lockSeatTime": 0}})

    client = WandaOfficialApiClient(ACCOUNT, transport=httpx.MockTransport(handler), timestamp_factory=lambda: 123)
    assert asyncio.run(client.cancel_order("order-1")) is False


def test_direct_gateway_builder_is_default_off_and_requires_explicit_opt_in(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WANDA_DIRECT_GATEWAY_ENABLED", raising=False)
    assert build_wanda_direct_gateway_from_env() is None

    path = tmp_path / "accounts.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("WANDA_DIRECT_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("WANDA_DIRECT_ACCOUNT_POOL_PATH", str(path))
    monkeypatch.delenv("WANDA_PRICING_ACCOUNT_REF_KEY", raising=False)
    with pytest.raises(RuntimeError, match="WANDA_PRICING_ACCOUNT_REF_KEY"):
        build_wanda_direct_gateway_from_env()
    monkeypatch.setenv("WANDA_PRICING_ACCOUNT_REF_KEY", "test-pricing-reference-key-0000001")
    assert build_wanda_direct_gateway_from_env() is not None


def test_non_official_origin_is_rejected_before_network() -> None:
    try:
        WandaOfficialApiClient(ACCOUNT, front_origin="http://127.0.0.1:8000")
    except DirectGatewayError as error:
        assert error.code == "official_origin_forbidden"
    else:
        raise AssertionError("unsafe origin accepted")
