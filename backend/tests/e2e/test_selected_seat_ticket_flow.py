from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.liangpiao_callbacks import CallbackVerifier, LiangpiaoCallbackHandler
from app.liangpiao_order_service import LiangpiaoOrderRequest, LiangpiaoOrderService
from app.rules_first_state_store import SqliteTransactionStateStore
from app.rules_first_store import RulesFirstStore
from app.selected_seat_quote_service import SelectedSeatQuoteService


class Sandbox:
    async def show_list(self, **_: object) -> dict[str, object]:
        return {"items": [{"showId": "show-1", "movieName": "电影", "showDate": "2026-08-29", "startTime": "20:00"}]}

    async def seat_list(self, **_: object) -> dict[str, object]:
        return {"items": [{"rowNo": 5, "colNo": 8, "seatNo": "5排8座", "areaId": "A", "status": "AVAILABLE"}]}

    async def order_preflight(self, **_: object) -> dict[str, object]:
        return {"providerAmountFen": 8000, "buyerAmountFen": 8800, "pricingRuleVersion": "sandbox", "ok": True}

    async def order_create(self, **_: object) -> dict[str, object]:
        return {"providerOrderNo": "provider-1", "status": "created"}

    async def order_detail(self, **_: object) -> dict[str, object]:
        return {}


class PlainProtector:
    def protect(self, value: str) -> str:
        return "enc:" + value

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


@pytest.mark.asyncio
async def test_mock_sandbox_selected_seat_flow_stops_before_real_external_write() -> None:
    sandbox = Sandbox()
    quote = await SelectedSeatQuoteService(sandbox).quote({
        "tenant_id": "tenant", "conversation_id": "chat", "cinema_id": 1,
        "movie_name": "电影", "show_date": "2026-08-29", "showtime_start": "20:00",
        "seats": [{"row_no": 5, "col_no": 8, "seat_no": "5排8座", "area_id": "A"}],
    })
    assert quote.preflight_verified is True
    order_service = LiangpiaoOrderService(sandbox, order_create_enabled=False, external_writes_enabled=False)
    assert quote.expires_at > datetime.now(timezone.utc) + timedelta(minutes=4)
    # The sandbox contract intentionally keeps the real order write disabled.
    with pytest.raises(Exception):
        await order_service.create(LiangpiaoOrderRequest(
            tenant_id="tenant", conversation_id="chat", confirmation_id="confirm",
            quote_id=quote.quote_id, quote_hash=quote.quote_hash, latest_buyer_message="确认下单",
            buyer_phone="13800138000", generation=1, trace_id="trace", buyer_confirmed=True,
        ), quote)


@pytest.mark.asyncio
async def test_full_mock_quote_confirm_order_callback_ticket_flow(tmp_path: Path) -> None:
    sandbox = Sandbox()
    database = tmp_path / "rules.sqlite3"
    protector = PlainProtector()
    store = RulesFirstStore(database, protector=protector)
    states = SqliteTransactionStateStore(database, protector=protector)
    quote = await SelectedSeatQuoteService(sandbox, quote_store=store).quote({
        "tenant_id": "tenant", "conversation_id": "chat", "cinema_id": 1,
        "movie_name": "电影", "show_date": "2026-08-29", "showtime_start": "20:00",
        "seats": [{"row_no": 5, "col_no": 8, "seat_no": "5排8座", "area_id": "A"}],
    })
    identity = {"tenant_id": "tenant", "shop_id": "shop", "buyer_id": "buyer", "chat_id": "chat"}
    current = states.get_or_create(**identity)
    states.transition(
        **identity, expected_revision=current.revision, event_id="quote-confirmed",
        transition_code="quote_confirmed", flow_state="ORDER_BOUND",
        updates={"order_status": "bound", "quote_status": "ready"}, allow_compatible_bootstrap=True,
    )
    orders = LiangpiaoOrderService(
        sandbox, quote_store=store, order_store=store,
        order_create_enabled=True, external_writes_enabled=True,
    )
    order = await orders.create(LiangpiaoOrderRequest(
        tenant_id="tenant", conversation_id="chat", shop_id="shop", buyer_id="buyer", chat_id="chat",
        confirmation_id="confirm-1", quote_id=quote.quote_id, quote_hash=quote.quote_hash,
        latest_buyer_message="确认下单", buyer_phone="13800138000",
        generation=1, trace_id="trace", buyer_confirmed=True,
    ))
    callback_body = json.dumps({
        "outOrderNo": order.out_order_no, "providerOrderNo": order.provider_order_no,
        "status": "ticket_sent", "ticketCode": "TK-1", "eventId": "callback-1",
    }).encode()
    verifier = CallbackVerifier("secret")
    timestamp = str(int(time.time()))
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=states, mapping_store=store, client=sandbox, enabled=True,
    )
    applied = await handler.handle(
        callback_body, signature=verifier.sign(callback_body, timestamp, "nonce"),
        timestamp=timestamp, nonce="nonce",
    )
    assert applied["state_after"] == "TICKET_SENT"
    assert applied["reply_plan"]["protected_facts"]["out_order_no"] == order.out_order_no
    assert states.get(**identity).flow_state == "TICKET_SENT"
