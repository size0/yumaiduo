from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.order_quote_binding_v2.service import (
    OrderQuoteBindingV2Service,
    OrderRepriceAuthorizationService,
    build_canonical_reprice_command,
)
from app.rule_state_coordinator import RuleStateCoordinator
from app.rules_first_runtime import RulesFirstRuntime
from app.rules_first_state_store import SqliteTransactionStateStore
from app.rules_first_store import RulesFirstStore
from app.transaction_state_store import TransactionStateStore
from app.pricing.models import PricingRulesSnapshot
from app.quote_record_store import QuoteRecordStore
from app.quote_v2.service import QuoteV2Service
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


def identity(**overrides: str) -> dict[str, str]:
    return {
        "tenant_id": overrides.get("tenant_id", "tenant-1"),
        "shop_id": overrides.get("shop_id", "shop-1"),
        "buyer_id": overrides.get("buyer_id", "buyer-1"),
        "chat_id": overrides.get("chat_id", "chat-1"),
    }


def show() -> ShowResolutionResult:
    return ShowResolutionResult(
        status="RESOLVED", wanda_store_id="store-1", wanda_show_id="show-1",
        wanda_film_id="film-1", movie_name="奥德赛", show_date="2026-08-26",
        start_time="19:30", hall_name="IMAX厅", sales_price_fen=6200,
    )


def pricing(*, ticket_count: int = 2):
    return price_wanda_cost(
        WandaCostFacts(
            status="COST_READY", request_type="WPLUS_AREA",
            cost_items=[WandaCostItem(zone_type="W+", cost_fen=4490, cost_source="SHOWTIME_WPLUS")],
        ), show(), SeatFactsResult(
            status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA", wanda_show_id="show-1",
        ), PricingRulesSnapshot(enabled=True, revision=12, rule_version="pricing-r12"),
        ticket_count=ticket_count,
    )


def setup(tmp_path: Path) -> tuple[QuoteV2Service, OrderQuoteBindingV2Service, OrderRepriceAuthorizationService]:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    quotes = QuoteV2Service(store, ttl_seconds=1800, now_provider=now)
    binding = OrderQuoteBindingV2Service(store, now_provider=now)
    return quotes, binding, OrderRepriceAuthorizationService(store, now_provider=now)


def create_quote(
    quotes: QuoteV2Service,
    *,
    context: str = "purchase-1",
    request: str = "request-1",
    created_at: datetime | None = None,
    tenant_id: str = "tenant-1",
    shop_id: str = "shop-1",
    buyer_id: str = "buyer-1",
    chat_id: str = "chat-1",
) -> dict[str, object]:
    return quotes.persist(
        pricing(), show(), tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id,
        chat_id=chat_id, wanda_city_id="city-1", cinema_name="测试万达影城",
        purchase_context_id=context, request_id=request, event_id=f"event-{request}",
        created_at=created_at,
    )


def order(*, status: str = "UNPAID", amount: int = 200, **overrides: object) -> dict[str, object]:
    return {
        "platform_order_id": "order-1", "order_status": status,
        "current_amount_fen": amount, "paid_amount_fen": None,
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "quantity": 1,
        **overrides,
    }


def bind_one(quotes: QuoteV2Service, binding: OrderQuoteBindingV2Service) -> dict[str, object]:
    created = create_quote(quotes)
    result = binding.bind_order("order-1", **identity())
    assert result.status == "BOUND"
    assert created is not None
    return created


def test_unpaid_bound_quote_is_reprice_ready_using_quote_total(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    created = bind_one(quotes, binding)

    result = auth.authorize("order-1", order(), **identity())

    assert result.status == "REPRICE_READY"
    assert result.target_amount_fen == created["total_sell_price_fen"] == 11820
    assert result.quote_id == created["quote_id"]
    assert result.quote_hash == created["quote_hash"]
    assert result.quote_generation == created["generation"]
    assert result.binding_revision == 1
    assert result.idempotency_key.startswith("price_change:v1:")


def test_normalized_plugin_order_fields_are_accepted_without_using_quantity(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    bind_one(quotes, binding)

    result = auth.authorize("order-1", {
        "order_id": "order-1", "order_status": "1", "amount_cents": 200,
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1", "chat_id": "chat-1",
        "quantity": 1,
    }, **identity())

    assert result.status == "REPRICE_READY"
    assert result.current_amount_fen == 200
    assert result.target_amount_fen == 11820


def test_xianyu_quantity_one_does_not_override_ticket_count_two(tmp_path: Path) -> None:
    _, binding, auth = setup(tmp_path)
    quotes = binding.store and QuoteV2Service(binding.store, ttl_seconds=1800, now_provider=now)
    bind_one(quotes, binding)

    result = auth.authorize("order-1", order(quantity=1), **identity())

    assert result.status == "REPRICE_READY"
    assert result.target_amount_fen == 11820


def test_current_amount_equal_target_is_already_priced(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    bind_one(quotes, binding)

    result = auth.authorize("order-1", order(amount=11820), **identity())

    assert result.status == "ALREADY_PRICED"
    assert result.target_amount_fen == 11820
    assert result.idempotency_key is None


def test_missing_binding_does_not_search_quotes_again(tmp_path: Path) -> None:
    quotes, _, auth = setup(tmp_path)
    create_quote(quotes)

    result = auth.authorize("order-1", order(), **identity())

    assert result.status == "NO_BOUND_QUOTE"
    assert result.quote_id is None


def test_expired_bound_quote_is_not_authorized(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    create_quote(quotes, created_at=now() - timedelta(seconds=1801))
    # The binding phase must also reject an expired quote, so create an audit
    # binding-shaped record only through the existing store API for this case.
    record = quotes.store.list("tenant-1")[0]
    quotes.store.save({**record, "quote_state": "TRANSACTION_READY", "transaction_authorized": True, "platform_order_id": "order-1", "order_id": "order-1", "binding_revision": 1})

    result = auth.authorize("order-1", order(), **identity())

    assert result.status == "QUOTE_EXPIRED"


def test_superseded_bound_quote_is_invalid(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    first = bind_one(quotes, binding)
    quotes.store.save({**first, "status": "superseded", "quote_state": "SUPERSEDED", "platform_order_id": "order-1", "order_id": "order-1"})

    result = auth.authorize("order-1", order(), **identity())

    assert result.status == "QUOTE_INVALID"


def test_unauthorized_quote_is_invalid(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    created = bind_one(quotes, binding)
    quotes.store.save({**created, "transaction_authorized": False, "platform_order_id": "order-1", "order_id": "order-1"})

    result = auth.authorize("order-1", order(), **identity())

    assert result.status == "QUOTE_INVALID"


def test_missing_quote_total_is_invalid(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    created = bind_one(quotes, binding)
    quotes.store.save({**created, "total_sell_price_fen": None, "total_quote_cents": None, "platform_order_id": "order-1", "order_id": "order-1"})

    result = auth.authorize("order-1", order(), **identity())

    assert result.status == "QUOTE_INVALID"


@pytest.mark.parametrize("status", ["PAID", "CLOSED", "REFUNDED", "UNKNOWN"])
def test_non_unpaid_order_is_rejected(status: str, tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    bind_one(quotes, binding)

    result = auth.authorize("order-1", order(status=status), **identity())

    assert result.status == "ORDER_NOT_UNPAID"


def test_buyer_mismatch_is_identity_mismatch(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    bind_one(quotes, binding)

    result = auth.authorize("order-1", order(buyer_id="buyer-2"), **identity())

    assert result.status == "IDENTITY_MISMATCH"


def test_shop_mismatch_is_identity_mismatch(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    bind_one(quotes, binding)

    result = auth.authorize("order-1", order(shop_id="shop-2"), **identity())

    assert result.status == "IDENTITY_MISMATCH"


def test_tenant_mismatch_is_identity_mismatch(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    bind_one(quotes, binding)

    result = auth.authorize("order-1", order(tenant_id="tenant-2"), **identity())

    assert result.status == "IDENTITY_MISMATCH"


def test_chat_mismatch_is_identity_mismatch(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    bind_one(quotes, binding)

    result = auth.authorize("order-1", order(chat_id="chat-2"), **identity())

    assert result.status == "IDENTITY_MISMATCH"


def test_manual_takeover_blocks_reprice_from_rules_first_state(tmp_path: Path) -> None:
    quotes, binding, _ = setup(tmp_path)
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    states.transition(
        **identity(), expected_revision=0, event_id="manual-hold-1",
        transition_code="explicit_human_takeover", flow_state="MANUAL_HOLD",
        updates={"automation_control": "human_hold"},
    )
    auth = OrderRepriceAuthorizationService(
        quotes.store, now_provider=now, transaction_state_store=states,
    )
    bind_one(quotes, binding)

    result = auth.authorize("order-1", order(automation_control="active"), **identity())

    assert result.status == "MANUAL_TAKEOVER"


def test_repeated_authorization_has_stable_idempotency_key(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    bind_one(quotes, binding)

    first = auth.authorize("order-1", order(), **identity(), request_id="request-a")
    second = auth.authorize("order-1", order(), **identity(), request_id="request-b")

    assert first.status == second.status == "REPRICE_READY"
    assert first.idempotency_key == second.idempotency_key


def test_newer_unbound_quote_does_not_replace_current_order_binding(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    first = bind_one(quotes, binding)
    second = create_quote(quotes, context="purchase-1", request="request-2")

    result = auth.authorize("order-1", order(), **identity())

    assert second["quote_id"] != first["quote_id"]
    assert result.status == "REPRICE_READY"
    assert result.quote_id == first["quote_id"]
    assert result.target_amount_fen == first["total_sell_price_fen"]


def test_binding_revision_changes_the_reprice_idempotency_key(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    created = bind_one(quotes, binding)
    first = auth.authorize("order-1", order(), **identity())
    record = quotes.get_record("tenant-1", created["record_id"])
    quotes.store.save({**record, "binding_revision": 2, "platform_order_id": "order-1", "order_id": "order-1"})

    second = auth.authorize("order-1", order(), **identity())

    assert first.status == second.status == "REPRICE_READY"
    assert first.idempotency_key != second.idempotency_key
    assert second.binding_revision == 2


def test_expected_binding_snapshot_mismatch_is_invalid(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    created = bind_one(quotes, binding)

    result = auth.authorize(
        "order-1", order(), **identity(), expected_quote_id="quote-other",
        expected_quote_hash=created["quote_hash"], expected_generation=created["generation"],
        expected_binding_revision=1,
    )

    assert result.status == "QUOTE_INVALID"


def test_provider_cost_is_not_target_source(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    created = bind_one(quotes, binding)
    record = quotes.get_record("tenant-1", created["record_id"])
    quotes.store.save({**record, "cost_fen": 100, "provider_cost_fen": 100, "platform_order_id": "order-1", "order_id": "order-1"})

    result = auth.authorize("order-1", order(), **identity())

    assert result.target_amount_fen == 11820
    assert result.target_amount_fen != 100


def test_new_flow_uses_rules_first_sqlite_transaction_authority(tmp_path: Path) -> None:
    quotes, binding, _ = setup(tmp_path)
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    states.get_or_create(**identity())
    auth = OrderRepriceAuthorizationService(
        quotes.store, now_provider=now, transaction_state_store=states,
    )
    bind_one(quotes, binding)

    result = auth.authorize("order-1", order(), **identity())

    assert result.status == "REPRICE_READY"
    assert result.transaction_revision == 0


def test_new_flow_rejects_legacy_transaction_state_authority(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    legacy = TransactionStateStore(tmp_path / "legacy-state.json", protector=PlainProtector())

    with pytest.raises(ValueError, match="transaction_state_authority"):
        OrderRepriceAuthorizationService(store, transaction_state_store=legacy)


def test_canonical_reprice_command_contains_plugin_compatible_identity(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    created = bind_one(quotes, binding)
    result = auth.authorize("order-1", order(), **identity())
    # A production command also carries the authoritative RulesFirst revision.
    result = result.__class__(**{**result.__dict__, "transaction_revision": 0})

    command = build_canonical_reprice_command(
        result, **identity(), action_id="event-1:change-order-price",
    )

    assert command["flow_version"] == "V4_NEW_FLOW_V2"
    assert command["source"] == "phase_9a_authorization"
    assert command["idempotency_key"] == command["quote_snapshot"]["idempotency_key"]
    assert command["quote_snapshot"]["quote_id"] == created["quote_id"]
    assert command["quote_snapshot"]["target_amount_cents"] == created["total_sell_price_fen"]
    assert command["quote_snapshot"]["quote_hash"] == created["quote_hash"]


def reprice_event() -> dict[str, object]:
    return {
        "envelope": {
            "id": "reprice-event-1", "tenantId": "tenant-1", "event": "order.created",
            "payload": {"orderId": "order-1", "accountUnb": "shop-1", "orderStatus": 1},
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "order": order(), "recent_messages": [],
    }


def test_ready_authorization_enqueues_one_existing_rules_first_command(tmp_path: Path) -> None:
    quotes, binding, _ = setup(tmp_path)
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    states.get_or_create(**identity())
    auth = OrderRepriceAuthorizationService(
        quotes.store, now_provider=now, transaction_state_store=states,
    )
    bind_one(quotes, binding)
    authorized = auth.authorize("order-1", order(), **identity())
    outbox = RulesFirstStore(tmp_path / "rules.sqlite3", protector=PlainProtector())

    first = auth.enqueue_authorized_reprice(
        authorized, rules_first_store=outbox, event=reprice_event(), **identity(),
    )
    second = auth.enqueue_authorized_reprice(
        authorized, rules_first_store=outbox, event=reprice_event(), **identity(),
    )

    assert first["command"]["command_type"] == "change_order_price"
    assert first["command"]["action"]["flow_version"] == "V4_NEW_FLOW_V2"
    assert first["command"]["action"]["quote_snapshot"]["target_amount_cents"] == 11820
    assert first["command"]["command_id"] == second["command"]["command_id"]
    assert outbox.claim_commands(limit=10)[0]["command_id"] == first["command"]["command_id"]


@pytest.mark.asyncio
async def test_authorized_command_reports_through_runtime_to_waiting_payment(tmp_path: Path) -> None:
    class NewFlowEngine:
        async def process_event(self, _: object) -> dict[str, object]:
            return {"decision": {"mode": "auto", "actions": [], "reason": "new_flow_reprice_external"}}

        def process_action_result(self, _: object) -> dict[str, object]:
            return {"actions": []}

    quotes, binding, _ = setup(tmp_path)
    database = tmp_path / "rules.sqlite3"
    states = SqliteTransactionStateStore(database, protector=PlainProtector())
    states.get_or_create(**identity())
    auth = OrderRepriceAuthorizationService(
        quotes.store, now_provider=now, transaction_state_store=states,
    )
    bind_one(quotes, binding)
    authorized = auth.authorize("order-1", order(), **identity())
    outbox = RulesFirstStore(database, protector=PlainProtector())
    auth.enqueue_authorized_reprice(
        authorized, rules_first_store=outbox, event=reprice_event(), **identity(),
    )
    runtime = RulesFirstRuntime(outbox, NewFlowEngine(), RuleStateCoordinator(states), states)

    assert await runtime.drain_once() is True
    command = runtime.claim_commands(limit=1)[0]
    recorded = runtime.record_command_result(
        command_id=command["command_id"], lease_token=command["lease_token"],
        result={
            "status": "succeeded", "reason_code": "price_change_verified",
            "flow_version": "V4_NEW_FLOW_V2", "reprice_status": "REPRICE_CONFIRMED",
            "order_id": "order-1", "target_amount_cents": 11820,
            "verified_amount_cents": 11820,
        },
    )

    assert recorded["status"] == "succeeded"
    state = states.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert state is not None and state.flow_state == "WAITING_PAYMENT"
    assert state.price_change_status == "succeeded"


def test_non_ready_authorization_never_creates_an_outbox_command(tmp_path: Path) -> None:
    quotes, binding, _ = setup(tmp_path)
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    states.get_or_create(**identity())
    auth = OrderRepriceAuthorizationService(
        quotes.store, now_provider=now, transaction_state_store=states,
    )
    bind_one(quotes, binding)
    result = auth.authorize("order-1", order(amount=11820), **identity())
    outbox = RulesFirstStore(tmp_path / "rules.sqlite3", protector=PlainProtector())

    with pytest.raises(ValueError, match="reprice_command_requires_ready_authorization"):
        auth.enqueue_authorized_reprice(
            result, rules_first_store=outbox, event=reprice_event(), **identity(),
        )

    assert outbox.claim_commands(limit=10) == []


def test_authorization_result_does_not_create_change_price_command(tmp_path: Path) -> None:
    quotes, binding, auth = setup(tmp_path)
    bind_one(quotes, binding)

    result = auth.authorize("order-1", order(), **identity())
    records = quotes.store.list("tenant-1")

    assert result.status == "REPRICE_READY"
    assert result.command_created is False
    assert all("change_order_price" not in record for record in records)
