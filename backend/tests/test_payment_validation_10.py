from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.order_quote_binding_v2.service import OrderQuoteBindingV2Service
from app.payment_validation import AuthoritativePaymentValidationService, parse_fishmore_fen
from app.quote_record_store import QuoteRecordStore
from app.rules_first_state_store import SqliteTransactionStateStore
from app.wplus_fulfillment import WplusFulfillmentMarkService


IDENTITY = {"tenant_id": "t1", "shop_id": "s1", "buyer_id": "b1", "chat_id": "c1"}
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


def setup(tmp_path: Path, *, source="AUTO_PRICING", request_type="EXACT_SEATS", expires_at=None):
    quotes = QuoteRecordStore(tmp_path / "quotes.json")
    created = NOW.isoformat()
    quotes.save_quote({
        "record_id": "quote-event-1", "request_id": "quote-request-1", **IDENTITY,
        "purchase_context_id": "purchase-1", "source": source,
        "request_type": request_type, "price_basis": "TOTAL" if source == "MANUAL_OPERATOR" else None,
        "total_sell_price_fen": 2900,
        "total_quote_cents": 2900, "ticket_count": 2,
        "terms_fingerprint": "hash-1", "provider_preflight_expires_at": (NOW.replace(year=2027)).isoformat(),
        **({"created_at": expires_at} if expires_at else {"created_at": created}),
    }, now=NOW)
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3")
    states.get_or_create(**IDENTITY)
    binding = OrderQuoteBindingV2Service(quotes, now_provider=lambda: NOW)
    bound = binding.bind_order("order-1", **IDENTITY, order_created_at=NOW, request_id="order-event")
    assert bound.status == "BOUND"
    wplus = WplusFulfillmentMarkService(states, quote_store=quotes)
    service = AuthoritativePaymentValidationService(
        states, binding, quotes, wplus_service=wplus, now_provider=lambda: NOW,
    )
    return service, states, quotes


def body(*, event_id="paid-1", amount="2900", status="2", order_id="order-1", identity=IDENTITY):
    return {
        "envelope": {"id": event_id, "tenantId": identity["tenant_id"], "event": "order.paid",
                     "payload": {"orderId": order_id}},
        "session": {"accountUnb": identity["shop_id"], "peerUnb": identity["buyer_id"], "chatId": identity["chat_id"]},
        "order": {"order_id": order_id, "tenant_id": identity["tenant_id"], "shop_id": identity["shop_id"],
                  "buyer_id": identity["buyer_id"], "chat_id": identity["chat_id"],
                  "order_status": status, "paid_at": NOW.isoformat(), "amount_cents": amount},
    }


def test_money_parser_rejects_yuan_decimal_and_booleans():
    assert parse_fishmore_fen("2900") == 2900
    assert parse_fishmore_fen("29.00") is None
    assert parse_fishmore_fen(True) is None
    assert parse_fishmore_fen("0") == 0


@pytest.mark.asyncio
async def test_valid_equal_and_overpayment_are_verified_without_write(tmp_path: Path):
    service, states, _ = setup(tmp_path)
    result = await service.process_event(body())
    assert result["status"] == "VERIFIED_PAID"
    assert result["validation_status"] == "VERIFIED_PAID"
    assert states.get(**IDENTITY).flow_state == "PAID_WAITING_FULFILLMENT"
    assert states.get(**IDENTITY).payment_validation_evidence["actual_amount_cents"] == 2900

    result = await service.process_event(body(event_id="paid-2", amount="3000"))
    assert result["status"] == "VERIFIED_PAID"
    assert result["payment_validation"]["overpayment_cents"] == 100


@pytest.mark.asyncio
async def test_underpayment_is_refund_required_but_no_refund_is_called(tmp_path: Path):
    service, states, _ = setup(tmp_path)
    result = await service.process_event(body(amount="2800"))
    assert result["status"] == "REFUND_REQUIRED"
    assert result["reason_code"] == "PAID_AMOUNT_LESS_THAN_EXPECTED"
    assert states.get(**IDENTITY).flow_state == "MANUAL_HOLD"
    assert states.get(**IDENTITY).payment_status == "mismatch"


@pytest.mark.asyncio
async def test_duplicate_and_identity_mismatch_fail_closed(tmp_path: Path):
    service, states, _ = setup(tmp_path)
    first = await service.process_event(body())
    duplicate = await service.process_event(body())
    assert first["status"] == "VERIFIED_PAID"
    assert duplicate["status"] == "PAID_WAITING_FULFILLMENT"
    assert duplicate["duplicate"] is True
    assert states.get(**IDENTITY).revision == 1

    other = {**IDENTITY, "tenant_id": "other"}
    result = await service.process_event(body(event_id="paid-other", identity=other))
    assert result["status"] == "MANUAL_HOLD"
    assert states.get(**IDENTITY).revision == 1


@pytest.mark.asyncio
async def test_expired_manual_holds_and_expired_auto_uses_validation_only_refresh(tmp_path: Path):
    service, states, _ = setup(tmp_path, source="MANUAL_OPERATOR")
    service._now_provider = lambda: datetime(2027, 1, 2, tzinfo=timezone.utc)
    result = await service.process_event(body())
    assert result["status"] == "MANUAL_HOLD"
    assert result["reason_code"] == "MANUAL_QUOTE_EXPIRED_AT_PAYMENT"

    service, states, _ = setup(tmp_path / "auto")
    service._now_provider = lambda: datetime(2027, 1, 2, tzinfo=timezone.utc)
    service._expired_auto_refresh = lambda bound, order, event: {"expected_amount_cents": 2900, "record_id": "not-saved"}
    result = await service.process_event(body())
    assert result["status"] == "VERIFIED_PAID"
    assert result["payment_validation"]["refresh_status"] == "validation_only"
    assert states.get(**IDENTITY).payment_validation_evidence["quote_record_id"] == "quote-event-1"


@pytest.mark.asyncio
async def test_wplus_payment_fans_out_to_mark_gate(tmp_path: Path):
    service, states, _ = setup(tmp_path, request_type="WPLUS_AREA")
    result = await service.process_event(body())
    assert result["status"] == "WAITING_WPLUS_MARK"
    assert result["validation_status"] == "VERIFIED_PAID"
    assert states.get(**IDENTITY).payment_status == "verified_paid"