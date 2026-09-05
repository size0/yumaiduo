from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.pricing.models import PricingRulesSnapshot
from app.quote_record_store import QuoteRecordStore
from app.seat_facts_v2.models import ExactSeatFact, SeatFactsResult
from app.show_resolve_v2.models import ShowResolutionResult
from app.wanda_cost_v2.models import WandaCostFacts, WandaCostItem
from app.wanda_pricing_v2.service import price_wanda_cost
from app.quote_v2.service import (
    AUTO_QUOTE_TTL_SECONDS,
    MANUAL_QUOTE_TTL_SECONDS,
    ManualQuoteInput,
    QuoteV2Service,
)


class PlainProtector:
    def protect(self, value: str) -> str:
        return "protected:" + value

    def unprotect(self, value: str) -> str:
        return value.removeprefix("protected:")


def now() -> datetime:
    return datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)


def rules() -> PricingRulesSnapshot:
    return PricingRulesSnapshot(enabled=True, revision=12, rule_version="pricing-r12")


def show(*, original: int = 6200) -> ShowResolutionResult:
    return ShowResolutionResult(
        status="RESOLVED", wanda_store_id="store-1", wanda_show_id="show-1",
        wanda_film_id="film-1", movie_name="奥德赛", show_date="2026-08-26",
        start_time="19:30", hall_name="IMAX厅", sales_price_fen=original,
    )


def area_cost(cost: int = 4490) -> WandaCostFacts:
    return WandaCostFacts(
        status="COST_READY", request_type="WPLUS_AREA",
        cost_items=[WandaCostItem(
            zone_type="W+", cost_fen=cost, cost_source="SHOWTIME_WPLUS",
        )],
    )


def exact_facts() -> SeatFactsResult:
    return SeatFactsResult(
        status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
        wanda_store_id="store-1", wanda_show_id="show-1", has_manual_mark=False,
        exact_seats=[
            ExactSeatFact(
                label="8排9座", seat_label="8排9座", seat_id="seat-1",
                wanda_seat_id="seat-1", area_code="36", zone_type="W+",
                status="AVAILABLE", is_wplus_exclusive=True,
                area_original_price_fen=6200,
            ),
            ExactSeatFact(
                label="8排10座", seat_label="8排10座", seat_id="seat-2",
                wanda_seat_id="seat-2", area_code="10", zone_type="普通",
                status="AVAILABLE", area_original_price_fen=6200,
            ),
        ],
    )


def exact_cost() -> WandaCostFacts:
    return WandaCostFacts(
        status="COST_READY", request_type="EXACT_SEATS",
        cost_items=[
            WandaCostItem(seat_label="8排9座", area_code="36", zone_type="W+", cost_fen=4500, cost_source="REALTIME_AREA_WPLUS"),
            WandaCostItem(seat_label="8排10座", area_code="10", zone_type="普通", cost_fen=4800, cost_source="REALTIME_AREA_WPLUS"),
        ],
    )


def priced_area(*, ticket_count: int | None = 1):
    return price_wanda_cost(
        area_cost(), show(), SeatFactsResult(
            status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA",
            wanda_show_id="show-1", has_manual_mark=False,
        ), rules(), ticket_count=ticket_count,
    )


def priced_exact():
    return price_wanda_cost(exact_cost(), show(), exact_facts(), rules())


def service(tmp_path: Path, *, ttl_seconds: int = 1800) -> QuoteV2Service:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    return QuoteV2Service(store, ttl_seconds=ttl_seconds, now_provider=now)


def persist(service: QuoteV2Service, result, *, buyer: str = "buyer-1", shop: str = "shop-1", chat: str = "chat-1", context: str = "purchase-1", request: str = "request-1", event: str = "event-1"):
    return service.persist(
        result, show(),
        tenant_id="tenant-1", shop_id=shop, buyer_id=buyer, chat_id=chat,
        wanda_city_id="city-1", cinema_name="测试万达影城",
        purchase_context_id=context, request_id=request, event_id=event,
    )


def test_wplus_single_transaction_ready_quote_is_persisted(tmp_path: Path) -> None:
    result = persist(service(tmp_path), priced_area())

    assert result is not None
    assert result["quote_state"] == "TRANSACTION_READY"
    assert result["transaction_authorized"] is True
    assert result["unit_sell_price_fen"] == 5910
    assert result["total_sell_price_fen"] == 5910


def test_quote_record_store_owns_expiry_and_generation(tmp_path: Path) -> None:
    svc = service(tmp_path, ttl_seconds=600)
    first = persist(svc, priced_area(ticket_count=None), request="store-q1", event="store-e1")
    second = persist(svc, priced_area(ticket_count=2), request="store-q2", event="store-e2")

    assert first["expires_at"] == (now() + timedelta(seconds=600)).isoformat()
    assert second["generation"] == 2
    assert second["supersedes_quote_id"] == first["quote_id"]
    stored_first = svc.store.get_record(tenant_id="tenant-1", record_id=first["record_id"])
    assert stored_first["quote_state"] == "SUPERSEDED"
    assert stored_first["transaction_authorized"] is False


def test_quote_v2_does_not_accept_caller_expiry_as_authority(tmp_path: Path) -> None:
    svc = service(tmp_path, ttl_seconds=600)
    created = persist(svc, priced_area(ticket_count=2), request="store-q1")
    assert created["expires_at"] == (now() + timedelta(seconds=600)).isoformat()


def test_wplus_unknown_count_is_preview_and_not_authorized(tmp_path: Path) -> None:
    result = persist(service(tmp_path), priced_area(ticket_count=None))

    assert result["quote_state"] == "PREVIEW"
    assert result["has_selected_seats"] is False
    assert result["unit_sell_price_fen"] == 5910
    assert result["total_sell_price_fen"] is None
    assert result["needs_ticket_count"] is True
    assert result["transaction_authorized"] is False


def test_filling_count_creates_q2_and_supersedes_q1(tmp_path: Path) -> None:
    svc = service(tmp_path)
    q1 = persist(svc, priced_area(ticket_count=None), request="request-1", event="event-1")
    q2 = persist(svc, priced_area(ticket_count=2), request="request-2", event="event-2")

    assert q1["quote_id"] != q2["quote_id"]
    assert q1["quote_state"] == "PREVIEW"
    assert q2["quote_state"] == "TRANSACTION_READY"
    assert q2["transaction_authorized"] is True
    assert q2["total_sell_price_fen"] == 11820
    assert q2["supersedes_quote_id"] == q1["quote_id"]
    assert svc.get_record("tenant-1", q1["record_id"])["quote_state"] == "SUPERSEDED"


def test_exact_seats_persist_concrete_seat_identity(tmp_path: Path) -> None:
    result = persist(service(tmp_path), priced_exact())

    assert result["request_type"] == "EXACT_SEATS"
    assert result["has_selected_seats"] is True
    assert [seat["seat_label"] for seat in result["selected_seats"]] == ["8排9座", "8排10座"]
    assert [seat["seat_id"] for seat in result["selected_seats"]] == ["seat-1", "seat-2"]


def test_mixed_seats_persist_per_seat_pricing_and_cost_lineage(tmp_path: Path) -> None:
    result = persist(service(tmp_path), priced_exact())

    assert len(result["seat_quotes"]) == 2
    assert [seat["cost_fen"] for seat in result["seat_quotes"]] == [4500, 4800]
    assert [seat["sell_price_fen"] for seat in result["seat_quotes"]] == [5910, 4900]
    assert [item["cost_source"] for item in result["cost_items"]] == [
        "REALTIME_AREA_WPLUS", "REALTIME_AREA_WPLUS",
    ]


def test_provider_cost_and_sell_price_are_separate_fields(tmp_path: Path) -> None:
    result = persist(service(tmp_path), priced_area())

    assert result["cost_items"][0]["cost_fen"] == 4490
    assert result["unit_sell_price_fen"] == 5910
    assert result["cost_items"][0]["cost_fen"] != result["unit_sell_price_fen"]


def test_pricing_revision_and_version_are_persisted(tmp_path: Path) -> None:
    result = persist(service(tmp_path), priced_area())

    assert result["pricing_rule_revision"] == 12
    assert result["pricing_rule_version"] == "pricing-r12"


def test_ttl_uses_configured_value(tmp_path: Path) -> None:
    result = persist(service(tmp_path, ttl_seconds=1800), priced_area())

    assert result["created_at"] == now().isoformat()
    assert result["expires_at"] == (now() + timedelta(seconds=1800)).isoformat()


def test_expired_quote_is_not_active(tmp_path: Path) -> None:
    svc = service(tmp_path)
    persist(svc, priced_area(), request="request-old")

    query = svc.list_active_quotes("tenant-1", "shop-1", "buyer-1", "chat-1", at=now() + timedelta(seconds=1800))

    assert query.status == "QUOTE_EXPIRED"
    assert query.quotes == []


def test_never_quoted_is_distinguished_from_expired(tmp_path: Path) -> None:
    query = service(tmp_path).list_active_quotes("tenant-1", "shop-1", "buyer-1", "chat-1", at=now())

    assert query.status == "NO_QUOTE_EVER"
    assert query.quotes == []


def test_superseded_quote_is_not_active(tmp_path: Path) -> None:
    svc = service(tmp_path)
    persist(svc, priced_area(ticket_count=None), request="request-1")
    q2 = persist(svc, priced_area(ticket_count=2), request="request-2")

    query = svc.list_active_quotes("tenant-1", "shop-1", "buyer-1", "chat-1", at=now())

    assert query.status == "ACTIVE_QUOTES_FOUND"
    assert [quote["quote_id"] for quote in query.quotes] == [q2["quote_id"]]


def test_different_buyer_does_not_share_quote(tmp_path: Path) -> None:
    svc = service(tmp_path)
    persist(svc, priced_area(), buyer="buyer-1")

    query = svc.list_active_quotes("tenant-1", "shop-1", "buyer-2", "chat-1", at=now())

    assert query.status == "NO_QUOTE_EVER"


def test_different_shop_does_not_share_quote(tmp_path: Path) -> None:
    svc = service(tmp_path)
    persist(svc, priced_area(), shop="shop-1")

    query = svc.list_active_quotes("tenant-1", "shop-2", "buyer-1", "chat-1", at=now())

    assert query.status == "NO_QUOTE_EVER"


def test_different_chat_does_not_share_quote(tmp_path: Path) -> None:
    svc = service(tmp_path)
    persist(svc, priced_area(), chat="chat-1")

    query = svc.list_active_quotes("tenant-1", "shop-1", "buyer-1", "chat-2", at=now())

    assert query.status == "NO_QUOTE_EVER"


def test_quote_id_is_not_event_id_or_record_id(tmp_path: Path) -> None:
    result = persist(service(tmp_path), priced_area(), event="event-123")

    assert result["quote_id"] != "event-123"
    assert result["quote_id"] != result["record_id"]
    assert result["event_id"] == "event-123"


def test_recognition_and_request_ids_are_not_quote_id(tmp_path: Path) -> None:
    svc = service(tmp_path)
    result = svc.persist(
        priced_area(), show(), tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1",
        chat_id="chat-1", wanda_city_id="city-1", cinema_name="测试万达影城",
        purchase_context_id="purchase-1", request_id="request-123", event_id="event-123",
        recognition_id="recognize-123",
    )

    assert result["quote_id"] not in {"request-123", "event-123", "recognize-123"}
    assert result["request_id"] == "request-123"
    assert result["recognition_id"] == "recognize-123"


def test_duplicate_same_request_is_idempotent(tmp_path: Path) -> None:
    svc = service(tmp_path)
    first = persist(svc, priced_area(), request="same-request")
    second = persist(svc, priced_area(), request="same-request", event="event-retry")

    assert second == first
    assert len(svc.store.list("tenant-1")) == 1


def test_quote_hash_and_generation_are_preserved(tmp_path: Path) -> None:
    svc = service(tmp_path)
    first = persist(svc, priced_area(ticket_count=None), request="request-1")
    second = persist(svc, priced_area(ticket_count=2), request="request-2")

    assert first["quote_hash"] == first["terms_fingerprint"]
    assert second["quote_hash"] == second["terms_fingerprint"]
    assert first["generation"] == 1
    assert second["generation"] == 2
    assert first["quote_version"].startswith("qv-")


def test_restart_keeps_quote_persisted(tmp_path: Path) -> None:
    svc = service(tmp_path)
    created = persist(svc, priced_area())
    restarted_store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    restarted = QuoteV2Service(restarted_store, ttl_seconds=1800, now_provider=now)

    query = restarted.list_active_quotes("tenant-1", "shop-1", "buyer-1", "chat-1", at=now())

    assert query.status == "ACTIVE_QUOTES_FOUND"
    assert query.quotes[0]["quote_id"] == created["quote_id"]


def test_pricing_requires_cost_creates_no_quote(tmp_path: Path) -> None:
    from app.wanda_cost_v2.models import WandaProbeTarget

    cost = WandaCostFacts(
        status="PROBE_REQUIRED", request_type="WPLUS_AREA", probe_required=True,
        probe_targets=[WandaProbeTarget(area_code="36", zone_type="W+", seat_id="seat-1")],
    )
    pricing = price_wanda_cost(cost, show(), SeatFactsResult(
        status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA", wanda_show_id="show-1",
    ), rules())
    svc = service(tmp_path)

    assert pricing.status == "PRICING_REQUIRES_COST"
    assert svc.persist(pricing, show(), tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1", wanda_city_id="city-1", cinema_name="测试万达影城", purchase_context_id="purchase-1", request_id="request-1") is None
    assert svc.store.list("tenant-1") == []


def test_image_price_is_not_persisted_as_sell_price(tmp_path: Path) -> None:
    pricing = priced_area()
    svc = service(tmp_path)
    result = persist(svc, pricing)

    assert "image_total_price_fen" not in result
    assert result["unit_sell_price_fen"] == pricing.unit_sell_price_fen
    assert result["total_sell_price_fen"] == pricing.total_sell_price_fen


def test_multiple_independent_purchase_contexts_remain_queryable(tmp_path: Path) -> None:
    svc = service(tmp_path)
    first = persist(svc, priced_area(), context="show-1-context", request="request-1")
    second = persist(svc, priced_area(), context="show-2-context", request="request-2")

    query = svc.list_active_quotes("tenant-1", "shop-1", "buyer-1", "chat-1", at=now())

    assert query.status == "ACTIVE_QUOTES_FOUND"
    assert {quote["quote_id"] for quote in query.quotes} == {first["quote_id"], second["quote_id"]}
    assert query.count == 2


def test_wanda_business_facts_are_persisted(tmp_path: Path) -> None:
    result = persist(service(tmp_path), priced_area())

    assert result["provider_route"] == "WANDA_SELF"
    assert result["wanda_city_id"] == "city-1"
    assert result["wanda_store_id"] == "store-1"
    assert result["wanda_show_id"] == "show-1"
    assert (result["movie"], result["quote_date"], result["showtime_start"], result["hall"]) == (
        "奥德赛", "2026-08-26", "19:30", "IMAX厅",
    )


def test_same_request_id_with_changed_terms_fails_closed(tmp_path: Path) -> None:
    svc = service(tmp_path)
    persist(svc, priced_area(ticket_count=1), request="same-request")

    with pytest.raises(ValueError, match="idempotency_request_conflict"):
        persist(svc, priced_area(ticket_count=2), request="same-request")


def test_existing_terms_hash_changes_with_pricing_revision(tmp_path: Path) -> None:
    svc = service(tmp_path)
    first_pricing = priced_area()
    second_pricing = first_pricing.model_copy(update={
        "pricing_rule_revision": 13,
        "pricing_rule_version": "pricing-r13",
    })
    first = persist(svc, first_pricing, request="request-1")
    second = persist(svc, second_pricing, request="request-2")

    assert first["quote_hash"] != second["quote_hash"]
    assert first["quote_version"] != second["quote_version"]


def manual_input(
    *,
    request: str = "manual-1",
    context: str = "purchase-1",
    price_basis: str = "UNIT",
    unit_price: int | None = 4800,
    ticket_count: int | None = None,
    total_price: int | None = None,
) -> ManualQuoteInput:
    return ManualQuoteInput(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        purchase_context_id=context, request_id=request,
        city="哈尔滨", cinema="测试万达影城", movie="奥德赛",
        quote_date="2026-08-26", showtime_start="19:30", hall="IMAX厅",
        seat_display="W+区域", quote_scope="area_preview", seat_zone_type="W+",
        price_basis=price_basis, unit_sell_price_fen=unit_price,
        ticket_count=ticket_count, total_sell_price_fen=total_price,
    )


def test_auto_quote_uses_the_canonical_thirty_minute_ttl(tmp_path: Path) -> None:
    result = persist(service(tmp_path, ttl_seconds=AUTO_QUOTE_TTL_SECONDS), priced_area())

    assert AUTO_QUOTE_TTL_SECONDS == 1800
    assert result["expires_at"] == (now() + timedelta(seconds=1800)).isoformat()
    assert result["source"] == "AUTO_PRICING"


def test_manual_unit_quote_without_count_is_preview(tmp_path: Path) -> None:
    svc = service(tmp_path)

    result = svc.persist_manual(manual_input())

    assert result["source"] == "MANUAL_OPERATOR"
    assert result["price_basis"] == "UNIT"
    assert result["unit_sell_price_fen"] == 4800
    assert result["ticket_count"] is None
    assert result["total_sell_price_fen"] is None
    assert result["quote_state"] == "PREVIEW"
    assert result["transaction_authorized"] is False
    assert result["expires_at"] == (now() + timedelta(seconds=MANUAL_QUOTE_TTL_SECONDS)).isoformat()


def test_manual_unit_quote_reuses_price_when_count_is_structured(tmp_path: Path) -> None:
    svc = service(tmp_path)

    result = svc.persist_manual(manual_input(ticket_count=2))

    assert result["total_sell_price_fen"] == 9600
    assert result["quote_state"] == "TRANSACTION_READY"
    assert result["transaction_authorized"] is True


def test_manual_unit_quote_supports_three_tickets_without_provider_pricing(tmp_path: Path) -> None:
    svc = service(tmp_path)

    result = svc.persist_manual(manual_input(ticket_count=3))

    assert result["total_sell_price_fen"] == 14400
    assert result["cost_items"] == []
    assert result["pricing_rule_revision"] is None


def test_manual_unit_quote_count_update_reuses_unit_lineage(tmp_path: Path) -> None:
    svc = service(tmp_path)
    preview = svc.persist_manual(manual_input(request="manual-preview"))
    priced = svc.persist_manual(manual_input(request="manual-two", ticket_count=2))

    assert priced["total_sell_price_fen"] == 9600
    assert priced["supersedes_quote_id"] == preview["quote_id"]
    assert svc.store.get_record(tenant_id="tenant-1", record_id=preview["record_id"])["quote_state"] == "SUPERSEDED"


def test_manual_total_quote_locks_ticket_count_and_requires_requote(tmp_path: Path) -> None:
    svc = service(tmp_path)
    result = svc.persist_manual(manual_input(
        price_basis="TOTAL", unit_price=None, ticket_count=2, total_price=9500,
    ))

    assert result["unit_sell_price_fen"] is None
    assert result["total_sell_price_fen"] == 9500
    assert svc.manual_quote_applicability(result, ticket_count=2, at=now()) == "APPLICABLE"
    assert svc.manual_quote_applicability(result, ticket_count=3, at=now()) == "REQUOTE_REQUIRED"


def test_manual_quote_supersedes_auto_in_the_same_purchase_context(tmp_path: Path) -> None:
    svc = service(tmp_path)
    auto = persist(svc, priced_area(), context="purchase-1")
    manual = svc.persist_manual(manual_input(request="manual-1"))

    assert manual["supersedes_quote_id"] == auto["quote_id"]
    assert svc.store.get_record(tenant_id="tenant-1", record_id=auto["record_id"])["quote_state"] == "SUPERSEDED"


def test_active_manual_quote_cannot_be_overwritten_by_new_auto_quote(tmp_path: Path) -> None:
    svc = service(tmp_path)
    manual = svc.persist_manual(manual_input(request="manual-1"))
    auto = persist(svc, priced_area(), context="purchase-1", request="auto-after-manual")

    assert manual["quote_state"] == "PREVIEW"
    assert svc.store.get_record(tenant_id="tenant-1", record_id=manual["record_id"])["quote_state"] == "PREVIEW"
    assert auto["quote_state"] == "SUPERSEDED"
    assert auto["invalidated_reason"] == "active_manual_quote_authority"
    assert svc.store.list_current_quotes(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1", at=now(),
    )[1][0]["quote_id"] == manual["quote_id"]


def test_expired_manual_quote_is_historical_only(tmp_path: Path) -> None:
    svc = service(tmp_path)
    manual = svc.persist_manual(manual_input(), created_at=now() - timedelta(seconds=MANUAL_QUOTE_TTL_SECONDS + 1))

    assert svc.get_record("tenant-1", manual["record_id"])["quote_id"] == manual["quote_id"]
    active = svc.list_active_quotes("tenant-1", "shop-1", "buyer-1", "chat-1", at=now())
    assert active.status == "QUOTE_EXPIRED"
    assert active.quotes == []


def test_manual_quote_requires_structured_price_basis(tmp_path: Path) -> None:
    svc = service(tmp_path)

    with pytest.raises(ValueError, match="manual_quote_price_basis_invalid"):
        svc.persist_manual(manual_input(price_basis="NATURAL_LANGUAGE"))


def test_service_uses_settings_ttl_when_not_overridden(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    svc = QuoteV2Service(
        store, settings=Settings(quote_record_ttl_seconds=600), now_provider=now,
    )

    result = persist(svc, priced_area())

    assert result["expires_at"] == (now() + timedelta(seconds=600)).isoformat()
