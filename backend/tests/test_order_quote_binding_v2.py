from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.pricing.models import PricingRulesSnapshot
from app.quote_record_store import QuoteRecordStore
from app.quote_v2.service import QuoteV2Service
from app.order_quote_binding_v2.service import OrderQuoteBindingV2Service
from app.seat_facts_v2.models import SeatFactsResult
from app.show_resolve_v2.models import ShowResolutionResult
from app.wanda_cost_v2.models import WandaCostFacts, WandaCostItem
from app.wanda_pricing_v2.service import price_wanda_cost


class PlainProtector:
    def protect(self, value: str) -> str:
        return "protected:" + value

    def unprotect(self, value: str) -> str:
        return value.removeprefix("protected:")


def now() -> datetime:
    return datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)


def show(show_id: str = "show-1") -> ShowResolutionResult:
    return ShowResolutionResult(
        status="RESOLVED", wanda_store_id="store-1", wanda_show_id=show_id,
        wanda_film_id="film-1", movie_name="奥德赛", show_date="2026-08-26",
        start_time="19:30", hall_name="IMAX厅", sales_price_fen=6200,
    )


def pricing(*, ticket_count: int | None = 1, show_id: str = "show-1"):
    return price_wanda_cost(
        WandaCostFacts(
            status="COST_READY", request_type="WPLUS_AREA",
            cost_items=[WandaCostItem(zone_type="W+", cost_fen=4490, cost_source="SHOWTIME_WPLUS")],
        ), show(show_id), SeatFactsResult(
            status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA", wanda_show_id=show_id,
        ), PricingRulesSnapshot(enabled=True, revision=12, rule_version="pricing-r12"),
        ticket_count=ticket_count,
    )


def setup(tmp_path: Path, *, ttl_seconds: int = 1800) -> tuple[QuoteV2Service, OrderQuoteBindingV2Service]:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    quotes = QuoteV2Service(store, ttl_seconds=ttl_seconds, now_provider=now)
    return quotes, OrderQuoteBindingV2Service(store, now_provider=now)


def create_quote(
    quotes: QuoteV2Service,
    *,
    buyer: str = "buyer-1",
    shop: str = "shop-1",
    tenant: str = "tenant-1",
    chat: str = "chat-1",
    context: str = "purchase-1",
    request: str = "request-1",
    ticket_count: int | None = 1,
    created_at: datetime | None = None,
    show_id: str = "show-1",
) -> dict[str, object]:
    return quotes.persist(
        pricing(ticket_count=ticket_count, show_id=show_id), show(show_id),
        tenant_id=tenant, shop_id=shop, buyer_id=buyer, chat_id=chat,
        wanda_city_id="city-1", cinema_name="测试万达影城",
        purchase_context_id=context, request_id=request,
        event_id=f"event-{request}", created_at=created_at,
    )


def identity(*, tenant: str = "tenant-1", shop: str = "shop-1", buyer: str = "buyer-1", chat: str = "chat-1") -> dict[str, str]:
    return {"tenant_id": tenant, "shop_id": shop, "buyer_id": buyer, "chat_id": chat}


def test_no_quote_returns_no_quote_ever(tmp_path: Path) -> None:
    _, binding = setup(tmp_path)

    result = binding.bind_order("order-1", **identity())

    assert result.status == "NO_QUOTE_EVER"
    assert result.quote_id is None


def test_only_expired_quote_returns_quote_expired(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes, created_at=now() - timedelta(seconds=1801))

    result = binding.bind_order("order-1", **identity())

    assert result.status == "QUOTE_EXPIRED"


def test_only_preview_quote_returns_only_preview_quotes(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes, ticket_count=None)

    result = binding.bind_order("order-1", **identity())

    assert result.status == "ONLY_PREVIEW_QUOTES"


def test_unique_transaction_ready_quote_is_bound_and_persisted(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    created = create_quote(quotes)

    result = binding.bind_order("platform-order-1", **identity(), request_id="bind-request-1")
    stored = quotes.get_record("tenant-1", created["record_id"])

    assert result.status == "BOUND"
    assert result.quote_id == created["quote_id"]
    assert result.would_reprice_to_fen == 5910
    assert stored["platform_order_id"] == "platform-order-1"
    assert stored["binding_revision"] == 1
    assert stored["binding_reason"] == "UNIQUE_ACTIVE_QUOTE"
    assert stored["quote_hash"] == stored["terms_fingerprint"]
    assert stored["tenant_id"] == "tenant-1"
    assert stored["shop_id"] == "shop-1"
    assert stored["buyer_id"] == "buyer-1"
    assert stored["chat_id"] == "chat-1"


def test_two_active_quotes_return_multiple_without_default_selection(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    first = create_quote(quotes, context="purchase-1", request="request-1")
    second = create_quote(quotes, context="purchase-2", request="request-2")

    result = binding.bind_order("order-1", **identity())

    assert result.status == "MULTIPLE_ACTIVE_QUOTES"
    assert result.quote_id is None
    assert {item["quote_id"] for item in result.candidates} == {first["quote_id"], second["quote_id"]}
    assert [item["quote_id"] for item in result.candidates] != [second["quote_id"]]


def test_multiple_candidates_are_not_sorted_into_automatic_latest_choice(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    first = create_quote(quotes, context="purchase-1", request="request-1")
    second = create_quote(quotes, context="purchase-2", request="request-2")

    result = binding.bind_order("order-1", **identity())

    assert result.status == "MULTIPLE_ACTIVE_QUOTES"
    assert result.candidates[0]["quote_id"] in {first["quote_id"], second["quote_id"]}
    assert len(result.candidates) == 2


def test_explicit_selection_binds_selected_q2(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes, context="purchase-1", request="request-1")
    second = create_quote(quotes, context="purchase-2", request="request-2")

    result = binding.bind_selected_quote("order-1", second["quote_id"], identity())

    assert result.status == "BOUND"
    assert result.quote_id == second["quote_id"]


def test_selection_outside_candidate_set_is_rejected(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes)

    result = binding.bind_selected_quote("order-1", "quote-not-in-candidates", identity())

    assert result.status == "REJECTED"
    assert result.reason == "QUOTE_NOT_ELIGIBLE"


def test_explicit_selection_of_expired_quote_is_rejected(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    expired = create_quote(quotes, created_at=now() - timedelta(seconds=1801))

    result = binding.bind_selected_quote("order-1", expired["quote_id"], identity())

    assert result.status == "REJECTED"
    assert result.reason == "QUOTE_NOT_ELIGIBLE"


def test_explicit_selection_of_superseded_quote_is_rejected(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    first = create_quote(quotes, context="purchase-1", request="request-1")
    create_quote(quotes, context="purchase-1", request="request-2", ticket_count=2)

    result = binding.bind_selected_quote("order-1", first["quote_id"], identity())

    assert result.status == "REJECTED"
    assert result.reason == "QUOTE_NOT_ELIGIBLE"


def test_different_buyer_cannot_bind(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes, buyer="buyer-1")

    assert binding.bind_order("order-1", **identity(buyer="buyer-2")).status == "NO_QUOTE_EVER"


def test_different_shop_cannot_bind(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes, shop="shop-1")

    assert binding.bind_order("order-1", **identity(shop="shop-2")).status == "NO_QUOTE_EVER"


def test_different_tenant_cannot_bind(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes, tenant="tenant-1")

    assert binding.bind_order("order-1", **identity(tenant="tenant-2")).status == "NO_QUOTE_EVER"


def test_different_chat_cannot_bind(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes, chat="chat-1")

    assert binding.bind_order("order-1", **identity(chat="chat-2")).status == "NO_QUOTE_EVER"


def test_order_quantity_is_not_an_input_and_does_not_match_ticket_count(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    created = create_quote(quotes, ticket_count=2)

    result = binding.bind_order("order-quantity-one", **identity())

    assert result.status == "BOUND"
    assert result.quote_id == created["quote_id"]


def test_quote_event_record_and_request_ids_remain_distinct(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    created = create_quote(quotes, request="request-1")
    result = binding.bind_order("order-1", **identity(), request_id="bind-request-1")
    stored = quotes.get_record("tenant-1", created["record_id"])

    assert stored["quote_id"] != stored["event_id"]
    assert stored["quote_id"] != stored["record_id"]
    assert stored["quote_id"] != stored["request_id"]
    assert result.quote_id == stored["quote_id"]


def test_duplicate_order_created_is_idempotent(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes)
    first = binding.bind_order("order-1", **identity(), request_id="bind-1")
    duplicate = binding.bind_order("order-1", **identity(), request_id="bind-2")

    assert first.status == "BOUND"
    assert duplicate.status == "ALREADY_BOUND"
    assert duplicate.quote_id == first.quote_id
    assert duplicate.binding_revision == first.binding_revision == 1


def test_restart_keeps_order_quote_binding(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    created = create_quote(quotes)
    bound = binding.bind_order("order-1", **identity())
    restarted_store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    restarted = OrderQuoteBindingV2Service(restarted_store, now_provider=now)

    result = restarted.bind_order("order-1", **identity())
    stored = restarted_store.get_record(tenant_id="tenant-1", record_id=created["record_id"])

    assert result.status == "ALREADY_BOUND"
    assert result.quote_id == bound.quote_id
    assert stored["platform_order_id"] == "order-1"


def test_new_quote_does_not_rebind_existing_order(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    first = create_quote(quotes, context="purchase-1", request="request-1")
    first_binding = binding.bind_order("order-1", **identity())
    second = create_quote(quotes, context="purchase-2", request="request-2")

    repeated = binding.bind_order("order-1", **identity())
    first_stored = quotes.get_record("tenant-1", first["record_id"])

    assert first_binding.quote_id == first["quote_id"]
    assert second["quote_id"] != first["quote_id"]
    assert repeated.status == "ALREADY_BOUND"
    assert repeated.quote_id == first["quote_id"]
    assert first_stored["platform_order_id"] == "order-1"


def test_binding_revision_and_history_are_saved(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    created = create_quote(quotes)
    binding.bind_order("order-1", **identity())
    stored = quotes.get_record("tenant-1", created["record_id"])

    assert stored["binding_revision"] == 1
    assert stored["binding_history"] == [{
        "revision": 1, "platform_order_id": "order-1",
        "quote_id": created["quote_id"], "binding_reason": "UNIQUE_ACTIVE_QUOTE",
    }]


def test_same_amount_does_not_resolve_multiple_quotes(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes, context="purchase-1", request="request-1")
    create_quote(quotes, context="purchase-2", request="request-2")

    result = binding.bind_order("order-1", **identity())

    assert result.status == "MULTIPLE_ACTIVE_QUOTES"
    assert all(item["total_sell_price_fen"] == 5910 for item in result.candidates)


def test_binding_candidate_projection_contains_required_facts(tmp_path: Path) -> None:
    quotes, binding = setup(tmp_path)
    create_quote(quotes)
    # Add another context so the projection path is exercised.
    create_quote(quotes, context="purchase-2", request="request-2", ticket_count=2)

    result = binding.bind_order("order-1", **identity())
    candidate = result.candidates[0]

    assert set(("quote_id", "movie", "show_date", "start_time", "hall", "selected_seats", "ticket_count", "unit_sell_price_fen", "total_sell_price_fen", "expires_at")).issubset(candidate)
