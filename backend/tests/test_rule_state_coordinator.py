from __future__ import annotations

from pathlib import Path

from app.reply_template_store import ReplyTemplates
from app.rule_state_coordinator import RuleStateCoordinator
from app.rules_first_state_store import SqliteTransactionStateStore
from app.transaction_state_store import TransactionStateStore


class Protector:
    def protect(self, value: str) -> str:
        return "sealed:" + value[::-1]

    def unprotect(self, value: str) -> str:
        return value.removeprefix("sealed:")[::-1]


def body(event: str = "order.created") -> dict[str, object]:
    return {
        "envelope": {
            "id": "event-1", "tenantId": "tenant-1", "event": event,
            "timestamp": 1787580000000,
            "payload": {
                "accountUnb": "shop-1", "peerUnb": "buyer-1",
                "chatId": "chat-1", "orderId": "order-1",
            },
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "order": {
            "orderId": "order-1", "accountUnb": "shop-1", "buyerUnb": "buyer-1",
            "chatId": "chat-1", "orderStatus": 1, "quantity": 2,
        },
    }


def identity() -> dict[str, str]:
    return {"tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1", "chat_id": "chat-1"}


def coordinator_store(tmp_path: Path) -> tuple[RuleStateCoordinator, TransactionStateStore]:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    return RuleStateCoordinator(store), store


def change_decision(order_id: str = "order-1") -> dict[str, object]:
    return {"decision": {
        "mode": "auto", "reason": "confirmed_quote_record_bound_to_order",
        "actions": [{
            "id": "event-1:change-order-price", "type": "change_order_price",
            "quote_snapshot": {
                "quote_record_id": "quote-1", "confirmation_version": "confirm-v1",
                "confirmed_ticket_count": 2, "order_id": order_id,
                "target_amount_cents": 8_800,
            },
        }],
    }}


def test_wplus_marker_confirmation_enters_quoted_order_guidance(tmp_path: Path) -> None:
    coordinator, store = coordinator_store(tmp_path)
    event = body("im.message.received")
    event["envelope"]["id"] = "wplus-marker-confirmed"

    reduced = coordinator.record_event_decision(event, {"decision": {
        "mode": "auto", "reason": "wplus_marker_confirmed_order_guidance",
        "actions": [{
            "id": "wplus-marker-confirmed:reply", "type": "send_message",
            "text": "人工会按照标记的位置出票。",
        }],
    }})

    assert reduced["rule_decision"]["state_after"] == "QUOTED"
    state = store.get(**identity())
    assert state is not None
    assert state.flow_state == "QUOTED"
    assert state.quote_status == "ready"
    assert state.confirmation_status == "pending"
    assert state.expected_inputs == ["ticket_count", "order"]


def test_order_amount_match_persists_the_quote_lineage_for_payment_gate(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    values = identity()
    store.transition(
        **values, expected_revision=0, event_id="bootstrap", transition_code="compat.bootstrap.quoted",
        flow_state="CONFIRMED", updates={
            "quote_status": "ready", "confirmation_status": "confirmed",
            "active_quote_record_id": "quote-new", "confirmed_ticket_count": 3,
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store)
    created = body("order.created")
    created["envelope"]["id"] = "order-created"
    created["order"]["payment"] = 16_920
    result = coordinator.record_event_decision(created, {"decision": {
        "mode": "auto", "reason": "authoritative_order_amount_already_matches_quote",
        "actions": [{"id": "order-created:order-amount-confirmation", "type": "send_message", "text": "可付款"}],
        "quote_snapshot": {
            "quote_record_id": "quote-old", "confirmation_version": "confirm-v1",
            "confirmation_source": "buyer_message", "confirmation_event_id": "buyer-confirm",
            "confirmed_ticket_count": 3, "target_amount_cents": 16_920,
        },
    }})

    current = store.get(**values)
    assert current is not None
    assert current.flow_state == "WAITING_PAYMENT"
    assert current.active_quote_record_id == "quote-old"
    assert current.confirmed_quote_record_id == "quote-old"
    assert current.confirmation_version == "confirm-v1"
    assert current.confirmed_ticket_count == 3
    assert current.target_amount_cents == 16_920
    assert result["rule_decision"]["transition_code"] == "order_amount_already_verified"


def test_paid_liangpiao_quote_creates_provider_order_outbox_action(tmp_path: Path) -> None:
    class QuoteStore:
        def get_record(self, *, tenant_id: str, record_id: str):
            return {
                "record_id": record_id, "status": "succeeded", "delivery_state": "delivered",
                "quote_route": "liangpiao_exact", "provider_quote_id": "lpq-1",
                "provider_quote_hash": "a" * 64, "quote_generation": 2,
                "terms_fingerprint": "tf-1",
            }

    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    values = identity()
    store.transition(
        **values, expected_revision=0, event_id="bootstrap", transition_code="compat.bootstrap.paid",
        flow_state="WAITING_PAYMENT", updates={
            "confirmation_status": "confirmed", "active_quote_record_id": "quote-1",
            "confirmed_quote_record_id": "quote-1", "confirmation_event_id": "confirm-1",
            "order_id": "order-1", "target_amount_cents": 8_800,
            "price_change_status": "succeeded", "order_status": "pending_payment",
            "confirmed_ticket_count": 3,
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store, quote_store=QuoteStore(), liangpiao_order_phone="13800138000")
    paid = body("order.paid")
    paid["envelope"]["id"] = "paid-provider-order"
    paid["order"]["orderStatus"] = 2
    paid["order"]["payment"] = 8_800
    result = coordinator.record_event_decision(paid, {"decision": {
        "mode": "auto", "reason": "authoritative_payment_confirmation_ready", "actions": [],
    }})
    order_actions = [item for item in result["decision"]["actions"] if item.get("type") == "create_liangpiao_order"]
    assert len(order_actions) == 1
    assert order_actions[0]["quote_id"] == "lpq-1"
    assert order_actions[0]["buyer_phone"] == "13800138000"
    assert order_actions[0]["generation"] == 2
    assert order_actions[0]["ticket_count"] == 3


def test_fixed_switch_quote_and_confirmation_are_durable_and_state_gated(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    values = identity()
    store.transition(
        **values, expected_revision=0, event_id="bootstrap", transition_code="compat.bootstrap.failed",
        flow_state="MANUAL_HOLD", updates={
            "fixed_switch_status": "pending", "fixed_switch_expires_at": "2099-01-01T00:00:00+00:00",
            "fixed_switch_source_order_no": "lp-failed-1",
            "fixed_switch_source_order_status": "closed",
            "fixed_switch_source_platform_order_id": "order-1",
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store)
    quote_event = body("im.message.received")
    quote_event["envelope"]["id"] = "fixed-quote"
    coordinator.record_event_decision(quote_event, {"decision": {
        "mode": "auto", "reason": "fixed_switch_quote_ready", "actions": [{
            "id": "fixed-quote:fixed-switch-quote", "type": "send_message", "text": "一口价",
            "fixed_switch_quote": {"quote_id": "lpq-fixed", "quote_hash": "a" * 64, "generation": 1, "ticket_count": 2, "amount_fen": 8000},
        }],
    }})
    current = store.get(**values)
    assert current is not None and current.flow_state == "QUOTED"
    assert current.fixed_switch_status == "confirmed"
    assert current.fixed_switch_quote_confirmation_status == "pending"
    assert current.fixed_switch_quote_id == "lpq-fixed"
    confirm_event = body("im.message.received")
    confirm_event["envelope"]["id"] = "fixed-confirm"
    confirmed = coordinator.record_event_decision(confirm_event, {"decision": {
        "mode": "auto", "reason": "fixed_switch_price_confirmed", "actions": [{
            "id": "fixed-confirm:fixed-switch-new-order", "type": "send_message", "text": "请重新拍下",
        }],
    }})
    current = store.get(**values)
    assert current is not None and current.fixed_switch_quote_confirmation_status == "confirmed"
    assert current.flow_state == "CONFIRMED"
    assert current.order_status == "none"
    assert current.payment_status == "unpaid"
    assert not any(action.get("type") == "create_liangpiao_order" for action in confirmed["decision"]["actions"])


def test_fixed_switch_only_creates_provider_order_after_new_order_paid(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    values = identity()
    store.transition(
        **values, expected_revision=0, event_id="bootstrap", transition_code="compat.bootstrap.fixed",
        flow_state="WAITING_PAYMENT", updates={
            "fixed_switch_status": "confirmed",
            "fixed_switch_source_order_status": "closed",
            "fixed_switch_source_platform_order_id": "order-old",
            "fixed_switch_replacement_order_id": "order-fixed",
            "fixed_switch_quote_id": "lpq-fixed",
            "fixed_switch_quote_hash": "a" * 64,
            "fixed_switch_quote_generation": 2,
            "fixed_switch_expires_at": "2099-01-01T00:00:00+00:00",
            "fixed_switch_quote_confirmation_status": "confirmed",
            "confirmation_status": "confirmed", "quote_status": "ready",
            "confirmed_ticket_count": 2, "target_amount_cents": 8000,
            "order_id": "order-fixed", "order_status": "pending_payment",
            "price_change_status": "succeeded", "payment_status": "unpaid",
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store, liangpiao_order_phone="13800138000")
    paid = body("order.paid")
    paid["envelope"]["id"] = "fixed-paid"
    paid["envelope"]["payload"]["orderId"] = "order-fixed"
    paid["order"].update({"orderId": "order-fixed", "orderStatus": 2, "payment": 8000})

    result = coordinator.record_event_decision(paid, {"decision": {
        "mode": "auto", "reason": "authoritative_payment_confirmation_ready", "actions": [],
    }})

    actions = [action for action in result["decision"]["actions"] if action.get("type") == "create_liangpiao_order"]
    assert len(actions) == 1
    assert actions[0]["order_id"] == "order-fixed"
    assert actions[0]["quote_id"] == "lpq-fixed"
    assert actions[0]["quote_hash"] == "a" * 64
    assert actions[0]["generation"] == 2
    assert actions[0]["ticket_count"] == 2


def test_fixed_switch_refund_completion_closes_source_before_fixed_quote(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    values = identity()
    store.transition(
        **values, expected_revision=0, event_id="bootstrap", transition_code="compat.bootstrap.failed",
        flow_state="MANUAL_HOLD", updates={
            "fixed_switch_status": "pending",
            "fixed_switch_source_order_no": "lp-failed",
            "fixed_switch_source_order_status": "failed",
            "order_id": "order-old", "order_status": "failed",
            "fulfillment_status": "failed",
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store)

    applied = body("order.refund.applied")
    applied["envelope"]["id"] = "refund-applied"
    applied["envelope"]["payload"]["orderId"] = "order-old"
    applied["order"]["orderId"] = "order-old"
    coordinator.record_event_decision(applied, {
        "decision": {"mode": "auto", "reason": "liangpiao_event_recorded", "actions": []},
    })
    current = store.get(**values)
    assert current is not None
    assert current.fixed_switch_source_order_status == "refund_pending"
    assert current.fixed_switch_source_platform_order_id == "order-old"

    finished = body("order.refund.finished")
    finished["envelope"]["id"] = "refund-finished"
    finished["envelope"]["payload"]["orderId"] = "order-old"
    finished["order"]["orderId"] = "order-old"
    coordinator.record_event_decision(finished, {
        "decision": {"mode": "auto", "reason": "liangpiao_event_recorded", "actions": []},
    })
    current = store.get(**values)
    assert current is not None and current.flow_state == "REFUNDED"
    assert current.fixed_switch_status == "pending"
    assert current.fixed_switch_source_order_status == "closed"

    quoted = body("im.message.received")
    quoted["envelope"]["id"] = "fixed-quote-after-refund"
    reduced = coordinator.record_event_decision(quoted, {"decision": {
        "mode": "auto", "reason": "fixed_switch_quote_ready", "actions": [{
            "id": "fixed-quote-after-refund:reply", "type": "send_message", "text": "一口价",
            "fixed_switch_quote": {
                "quote_id": "lpq-fixed", "quote_hash": "b" * 64,
                "generation": 1, "ticket_count": 2, "amount_fen": 8200,
            },
        }],
    }})
    assert reduced["rule_decision"]["state_after"] == "QUOTED"
    current = store.get(**values)
    assert current is not None
    assert current.fixed_switch_source_order_status == "closed"
    assert current.fixed_switch_quote_id == "lpq-fixed"


def test_verified_platform_cancel_result_closes_source_then_asks_about_fixed_channel(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    values = identity()
    store.transition(
        **values, expected_revision=0, event_id="bootstrap",
        transition_code="compat.bootstrap.refund-pending", flow_state="MANUAL_HOLD",
        updates={
            "fixed_switch_status": "pending",
            "fixed_switch_expires_at": "2099-01-01T00:00:00+00:00",
            "fixed_switch_source_order_no": "out-limit",
            "fixed_switch_source_order_status": "refund_pending",
            "fixed_switch_source_platform_order_id": "order-1",
            "order_id": "order-1", "order_status": "refund_pending",
            "payment_status": "refund_pending", "fulfillment_status": "failed",
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store)

    rule = coordinator.record_action_result(
        tenant_id="tenant-1",
        body={
            "event_id": "liangpiao-callback-1", "action_id": "liangpiao:out-limit:cancel-source-order",
            "command_type": "cancel_failed_liangpiao_source_order", "order_id": "order-1",
            "result": {
                "status": "succeeded", "order_id": "order-1", "order_closed": True,
                "cancel_attempted": True, "source_out_order_no": "out-limit",
            },
        },
    )

    assert rule is not None
    assert rule.transition_code == "fixed_switch_source_order_closed"
    assert rule.state_after == "MANUAL_HOLD"
    assert len(rule.actions) == 1
    assert rule.actions[0]["type"] == "send_message"
    assert "一口价" in rule.actions[0]["text"]
    current = store.get(**values)
    assert current is not None
    assert current.fixed_switch_source_order_status == "closed"
    assert current.order_status == "closed"
    assert current.payment_status == "refunded"


def test_fixed_switch_paid_event_for_old_order_never_creates_provider_order(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    values = identity()
    store.transition(
        **values, expected_revision=0, event_id="bootstrap", transition_code="compat.bootstrap.fixed",
        flow_state="WAITING_PAYMENT", updates={
            "fixed_switch_status": "confirmed",
            "fixed_switch_source_order_status": "closed",
            "fixed_switch_source_platform_order_id": "order-old",
            "fixed_switch_replacement_order_id": "order-fixed",
            "fixed_switch_quote_id": "lpq-fixed", "fixed_switch_quote_hash": "a" * 64,
            "fixed_switch_quote_generation": 2,
            "fixed_switch_expires_at": "2099-01-01T00:00:00+00:00",
            "fixed_switch_quote_confirmation_status": "confirmed",
            "confirmation_status": "confirmed", "quote_status": "ready",
            "confirmed_ticket_count": 2, "target_amount_cents": 8000,
            "order_id": "order-fixed", "order_status": "pending_payment",
            "price_change_status": "succeeded", "payment_status": "unpaid",
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store)
    paid = body("order.paid")
    paid["envelope"]["id"] = "stale-old-order-paid"
    paid["envelope"]["payload"]["orderId"] = "order-old"
    paid["order"].update({"orderId": "order-old", "orderStatus": 2, "payment": 8000})

    result = coordinator.record_event_decision(paid, {
        "decision": {"mode": "auto", "reason": "authoritative_payment_confirmation_ready", "actions": []},
    })

    assert not any(
        action.get("type") == "create_liangpiao_order"
        for action in result["decision"]["actions"]
    )
    current = store.get(**values)
    assert current is not None
    assert current.flow_state == "WAITING_PAYMENT"
    assert current.order_id == "order-fixed"
    assert current.payment_status == "unpaid"


def test_fixed_switch_duplicate_paid_event_does_not_create_second_provider_order(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    values = identity()
    store.transition(
        **values, expected_revision=0, event_id="bootstrap", transition_code="compat.bootstrap.fixed",
        flow_state="PAID_WAITING_FULFILLMENT", updates={
            "fixed_switch_status": "confirmed",
            "fixed_switch_source_order_status": "closed",
            "fixed_switch_source_platform_order_id": "order-old",
            "fixed_switch_replacement_order_id": "order-fixed",
            "fixed_switch_quote_id": "lpq-fixed", "fixed_switch_quote_hash": "a" * 64,
            "fixed_switch_quote_generation": 2,
            "fixed_switch_expires_at": "2099-01-01T00:00:00+00:00",
            "fixed_switch_quote_confirmation_status": "confirmed",
            "confirmation_status": "confirmed", "quote_status": "ready",
            "confirmed_ticket_count": 2, "target_amount_cents": 8000,
            "order_id": "order-fixed", "order_status": "paid",
            "price_change_status": "succeeded", "payment_status": "verified_paid",
            "fulfillment_status": "pending",
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store)
    paid = body("order.paid")
    paid["envelope"]["id"] = "duplicate-fixed-paid"
    paid["envelope"]["payload"]["orderId"] = "order-fixed"
    paid["order"].update({"orderId": "order-fixed", "orderStatus": 2, "payment": 8000})

    result = coordinator.record_event_decision(paid, {
        "decision": {"mode": "auto", "reason": "authoritative_payment_confirmation_ready", "actions": []},
    })

    assert not any(
        action.get("type") == "create_liangpiao_order"
        for action in result["decision"]["actions"]
    )


def test_same_terms_quote_retry_does_not_start_a_new_transaction_generation(tmp_path: Path) -> None:
    class QuoteStore:
        records = {
            "quote-old": {"record_id": "quote-old", "status": "succeeded", "terms_fingerprint": "tf-same"},
            "quote-retry": {"record_id": "quote-retry", "status": "succeeded", "terms_fingerprint": "tf-same"},
        }

        @classmethod
        def get_record(cls, *, tenant_id: str, record_id: str):
            assert tenant_id == "tenant-1"
            return cls.records.get(record_id)

    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=Protector())
    coordinator = RuleStateCoordinator(store, quote_store=QuoteStore())
    first = body("im.message.received")
    first["envelope"]["id"] = "quote-old"
    first["order"] = None
    second = body("im.message.received")
    second["envelope"]["id"] = "quote-retry"
    second["order"] = None

    coordinator.record_event_decision(first, {
        "decision": {"mode": "auto", "reason": "image_recognition_reply_ready", "actions": []},
    })
    reduced = coordinator.record_event_decision(second, {
        "decision": {"mode": "auto", "reason": "image_recognition_reply_ready", "actions": []},
    })

    current = store.get(**identity())
    assert current is not None and current.generation == 1
    assert current.active_quote_record_id == "quote-retry"
    assert reduced["rule_decision"]["transition_code"] == "authoritative_quote_created"


def test_changed_failed_quote_attempt_invalidates_previous_unbound_quote(tmp_path: Path) -> None:
    class QuoteStore:
        records = {
            "quote-old": {
                "record_id": "quote-old", "status": "succeeded",
                "terms_fingerprint": "tf-12-40", "delivery_state": "delivered",
            },
            "quote-new": {
                "record_id": "quote-new", "status": "failed",
                "terms_fingerprint": "tf-15-55",
            },
        }

        @classmethod
        def get_record(cls, *, tenant_id: str, record_id: str):
            assert tenant_id == "tenant-1"
            return cls.records.get(record_id)

        @classmethod
        def invalidate(cls, *, tenant_id: str, record_id: str, reason: str):
            assert tenant_id == "tenant-1"
            record = cls.records.get(record_id)
            if record is not None:
                record["invalidated_reason"] = reason
            return record

    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=Protector())
    coordinator = RuleStateCoordinator(store, quote_store=QuoteStore())
    first = body("im.message.received")
    first["envelope"]["id"] = "quote-old"
    first["order"] = None
    second = body("im.message.received")
    second["envelope"]["id"] = "quote-new"
    second["order"] = None

    coordinator.record_event_decision(first, {
        "decision": {"mode": "auto", "reason": "image_recognition_reply_ready", "actions": []},
    })
    reduced = coordinator.record_event_decision(second, {
        "decision": {"mode": "auto", "reason": "image_recognition_reply_ready", "actions": []},
    })

    current = store.get(**identity())
    assert current is not None
    assert current.flow_state == "COLLECTING"
    assert current.active_quote_record_id is None
    assert current.confirmed_quote_record_id is None
    assert current.target_amount_cents is None
    assert QuoteStore.records["quote-old"]["invalidated_reason"] == "superseded_by_new_failed_quote_attempt"
    assert reduced["rule_decision"]["transition_code"] == "quote_input_incomplete"


def test_fresh_quote_after_refund_starts_a_new_transaction_generation(tmp_path: Path) -> None:
    class QuoteStore:
        @staticmethod
        def get_record(*, tenant_id: str, record_id: str):
            assert tenant_id == "tenant-1"
            return {"record_id": record_id, "status": "succeeded", "ticket_count": None}

    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=Protector())
    current = store.get_or_create(**identity())
    store.transition(
        **identity(), expected_revision=current.revision, event_id="refund-old",
        flow_state="REFUNDED", transition_code="compat.bootstrap.refunded",
        updates={
            "order_id": "order-old", "order_status": "refunded",
            "payment_status": "refunded",
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store, quote_store=QuoteStore())
    event = body("im.message.received")
    event["envelope"]["id"] = "quote-new"
    event["envelope"]["payload"].pop("orderId", None)
    event["order"] = None

    decorated = coordinator.record_event_decision(event, {
        "decision": {"mode": "auto", "reason": "image_recognition_reply_ready", "actions": []},
    })

    rule = decorated["rule_decision"]
    assert rule["state_after"] == "QUOTED"
    assert rule["transition_code"] == "authoritative_quote_created"
    replacement = store.get(**identity())
    assert replacement is not None
    assert replacement.generation == 2
    assert replacement.order_id is None
    assert replacement.active_quote_record_id == "quote-new"


def test_new_order_after_refund_starts_generation_and_preserves_price_command(tmp_path: Path) -> None:
    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=Protector())
    current = store.get_or_create(**identity())
    store.transition(
        **identity(), expected_revision=current.revision, event_id="refund-old",
        flow_state="REFUNDED", transition_code="compat.bootstrap.refunded",
        updates={
            "order_id": "order-old", "order_status": "refunded",
            "payment_status": "refunded", "price_change_status": "skipped",
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store)
    new_order = body("order.created")
    new_order["envelope"]["id"] = "new-order-event"
    new_order["envelope"]["payload"]["orderId"] = "order-new"
    new_order["order"]["orderId"] = "order-new"
    decision = change_decision(order_id="order-new")
    decision["decision"]["actions"][0]["id"] = "new-order-event:change-order-price"

    reduced = coordinator.record_event_decision(new_order, decision)

    assert reduced["rule_decision"]["state_before"] == "COLLECTING"
    assert reduced["rule_decision"]["state_after"] == "PRICE_CHANGING"
    assert [action["type"] for action in reduced["decision"]["actions"]] == ["change_order_price"]
    replacement = store.get(**identity())
    assert replacement is not None
    assert replacement.generation == 2
    assert replacement.order_id == "order-new"
    assert replacement.price_change_status == "pending"


def test_price_changing_starts_without_an_intermediate_buyer_message(tmp_path: Path) -> None:
    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=Protector())
    coordinator = RuleStateCoordinator(store, reply_template_store=object())

    reduced = coordinator.record_event_decision(body("order.created"), change_decision())

    assert reduced["rule_decision"]["state_after"] == "PRICE_CHANGING"
    assert [action["type"] for action in reduced["decision"]["actions"]] == ["change_order_price"]


def test_pending_order_card_before_created_webhook_also_starts_terminal_replacement(
    tmp_path: Path,
) -> None:
    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=Protector())
    current = store.get_or_create(**identity())
    store.transition(
        **identity(), expected_revision=current.revision, event_id="completed-old",
        flow_state="COMPLETED", transition_code="compat.bootstrap.completed",
        updates={
            "order_id": "order-old", "order_status": "completed",
            "payment_status": "verified_paid", "fulfillment_status": "completed",
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store)
    pending_card = body("im.message.received")
    pending_card["envelope"]["id"] = "pending-card-new-order"
    pending_card["envelope"]["payload"]["orderId"] = "order-new"
    pending_card["order"]["orderId"] = "order-new"

    reduced = coordinator.record_event_decision(
        pending_card, {"decision": {
            "mode": "auto", "reason": "confirmed_quote_unavailable", "actions": [],
        }},
    )

    assert reduced["rule_decision"]["state_before"] == "COLLECTING"
    assert reduced["rule_decision"]["state_after"] == "ORDER_UNVERIFIED"
    replacement = store.get(**identity())
    assert replacement is not None and replacement.generation == 2


def test_refund_finished_clears_manual_hold_and_releases_quote_binding(tmp_path: Path) -> None:
    class Quotes:
        released: list[dict[str, object]] = []

        @classmethod
        def get_record(cls, *, tenant_id: str, record_id: str):
            return {
                "record_id": record_id, "status": "succeeded", "order_id": "order-1",
                "confirmation_version": "confirm-1", "terms_fingerprint": "tf-1",
            } if record_id == "quote-1" else None

        @classmethod
        def release_order_binding(cls, **kwargs):
            cls.released.append(kwargs)
            return {"record_id": kwargs["record_id"], "order_id": None}

    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=Protector())
    current = store.get_or_create(**identity())
    store.transition(
        **identity(), expected_revision=current.revision, event_id="hold",
        flow_state="MANUAL_HOLD", transition_code="compat.bootstrap.manual_hold",
        updates={"order_id": "order-1", "active_quote_record_id": "quote-1"},
        allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store, quote_store=Quotes())
    refunded = body("order.refund.finished")
    refunded["envelope"]["id"] = "refund-finished"
    refunded["envelope"]["payload"]["orderId"] = "order-1"
    refunded["order"].update({"orderId": "order-1", "orderStatus": 5})

    reduced = coordinator.record_event_decision(refunded, {
        "decision": {"mode": "auto", "reason": "automation_hold_active", "actions": []},
    })

    state = store.get(**identity())
    assert state is not None and state.flow_state == "REFUNDED"
    assert reduced["rule_decision"]["transition_code"] == "official_refund_completed"
    assert Quotes.released and Quotes.released[0]["order_id"] == "order-1"


def test_terminal_generation_ignores_stale_confirmation_without_failing_event(tmp_path: Path) -> None:
    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=Protector())
    current = store.get_or_create(**identity())
    store.transition(
        **identity(), expected_revision=current.revision, event_id="refund-old",
        flow_state="REFUNDED", transition_code="compat.bootstrap.refunded",
        updates={
            "order_id": "order-old", "order_status": "refunded", "payment_status": "refunded",
        }, allow_compatible_bootstrap=True,
    )
    coordinator = RuleStateCoordinator(store)
    stale = body("im.message.received")
    stale["envelope"]["id"] = "stale-confirmation"
    stale["envelope"]["payload"]["orderId"] = "order-old"
    stale["order"]["orderId"] = "order-old"

    reduced = coordinator.record_event_decision(stale, {"decision": {
        "mode": "auto", "reason": "order_submission_guidance_ready",
        "actions": [{"id": "unsafe", "type": "send_message", "text": "请下单"}],
    }})

    assert reduced["rule_decision"]["state_after"] == "REFUNDED"
    assert reduced["rule_decision"]["transition_code"] == "terminal_event_ignored"
    assert reduced["decision"]["actions"] == []


def test_quote_confirmation_projects_the_confirmed_quote_lineage_into_state(tmp_path: Path) -> None:
    class Quotes:
        @staticmethod
        def get_record(*, tenant_id: str, record_id: str):
            if tenant_id == "tenant-1" and record_id == "quote-event":
                return {
                    "record_id": "quote-event", "status": "succeeded", "delivery_state": "delivered",
                    "confirmation_version": "v4c-confirmed", "confirmation_source": "buyer_message",
                    "confirmation_event_id": "confirm-event", "confirmed_ticket_count": 2,
                    "ticket_count": 2, "total_quote_cents": 8_800,
                }
            return None

    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=Protector())
    coordinator = RuleStateCoordinator(store, quote_store=Quotes())
    quoted = body("im.message.received")
    quoted["envelope"]["id"] = "quote-event"
    coordinator.record_event_decision(quoted, {"decision": {
        "mode": "auto", "reason": "automatic_reply_ready", "actions": [],
    }})
    confirmed = body("im.message.received")
    confirmed["envelope"]["id"] = "confirm-event"

    reduced = coordinator.record_event_decision(confirmed, {"decision": {
        "mode": "auto", "reason": "order_submission_guidance_ready", "actions": [
            {"id": "primary", "type": "send_message", "text": "完整确认回复"},
            {
                "id": "keyword", "type": "send_message", "text": "点击右上角立即购买",
                "preserve_on_new_buyer_message": True, "rule_governed": True,
            },
            {"id": "guide", "type": "send_image", "image_asset_id": "ki-" + "a" * 40},
        ],
    }})

    assert reduced["rule_decision"]["state_after"] == "CONFIRMED"
    assert [action["type"] for action in reduced["decision"]["actions"]] == [
        "send_message", "send_message", "send_image",
    ]
    assert reduced["decision"]["actions"][1]["text"] == "点击右上角立即购买"
    state = store.get(**identity())
    assert state is not None
    assert state.confirmed_quote_record_id == "quote-event"
    assert state.confirmation_version == "v4c-confirmed"
    assert state.confirmation_source == "buyer_message"
    assert state.confirmation_event_id == "confirm-event"
    assert state.confirmed_ticket_count == 2
    assert state.target_amount_cents == 8_800


def test_confirmed_state_does_not_repeat_submission_reply_for_unrelated_event(tmp_path: Path) -> None:
    coordinator, store = coordinator_store(tmp_path)
    current = store.get_or_create(**identity())
    store.transition(
        **identity(), expected_revision=current.revision, event_id="confirmed",
        transition_code="compat.bootstrap.confirmed", flow_state="CONFIRMED",
        updates={
            "quote_status": "ready", "confirmation_status": "confirmed",
            "confirmed_quote_record_id": "quote-1", "confirmed_ticket_count": 2,
        }, allow_compatible_bootstrap=True,
    )
    later = body("im.message.received")
    later["envelope"]["id"] = "later-event"

    reduced = coordinator.record_event_decision(later, {"decision": {
        "mode": "auto", "reason": "automatic_reply_ready", "actions": [],
    }})

    assert reduced["decision"]["actions"] == []
    assert reduced["rule_decision"]["state_after"] == "CONFIRMED"
    assert store.get(**identity()).confirmation_status == "confirmed"


def test_confirmation_send_blocked_by_human_message_enters_manual_hold(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    coordinator = RuleStateCoordinator(store)
    coordinator.record_event_decision(body(), change_decision())
    coordinator.record_action_result(
        tenant_id="tenant-1",
        body={
            "event_id": "event-1", "action_id": "event-1:change-order-price",
            "result": {
                "status": "succeeded", "order_id": "order-1",
                "target_amount_cents": 8_800, "verified_amount_cents": 8_800,
            },
        },
    )

    rule = coordinator.record_action_result(
        tenant_id="tenant-1",
        body={
            "event_id": "event-1", "action_id": "event-1:confirm-price-change",
            "command_type": "send_price_change_confirmation", "order_id": "order-1",
            "result": {"status": "skipped", "reason": "human_message_arrived_before_send"},
        },
    )

    assert rule is not None
    assert rule.state_after == "MANUAL_HOLD"
    assert rule.handoff_reason == "explicit_human_takeover"
    state = store.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert state is not None and state.automation_control == "human_hold"


def test_confirmation_without_persisted_quote_is_ignored_instead_of_failing_state(tmp_path: Path) -> None:
    coordinator, store = coordinator_store(tmp_path)
    first = body("im.message.received")
    coordinator.record_event_decision(first, {"decision": {
        "mode": "auto", "reason": "automatic_reply_ready", "actions": [],
    }})
    confirmation = body("im.message.received")
    confirmation["envelope"]["id"] = "confirmation-without-quote"

    reduced = coordinator.record_event_decision(confirmation, {"decision": {
        "mode": "auto", "reason": "order_submission_guidance_ready",
        "actions": [{"id": "unsafe-guidance", "type": "send_message", "text": "请下单"}],
    }})

    assert reduced["rule_decision"]["state_after"] == "COLLECTING"
    assert reduced["rule_decision"]["transition_code"] == "confirmation_without_quote_ignored"
    assert reduced["decision"]["actions"] == []
    current = store.get(**identity())
    assert current is not None and current.confirmation_status == "none"


def test_actual_price_action_is_persisted_as_single_authoritative_rule_decision(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    coordinator = RuleStateCoordinator(store)
    result = {"decision": {
        "mode": "auto", "reason": "confirmed_quote_record_bound_to_order",
        "actions": [{
            "id": "event-1:change-order-price", "type": "change_order_price",
            "quote_snapshot": {
                "quote_record_id": "quote-1", "confirmation_version": "confirm-v1",
                "confirmed_ticket_count": 2, "order_id": "order-1",
                "target_amount_cents": 8_800,
            },
        }],
    }}

    decorated = coordinator.record_event_decision(body(), result)
    duplicate = coordinator.record_event_decision(body(), result)

    rule = decorated["rule_decision"]
    assert rule["state_before"] == "NEW"
    assert rule["state_after"] == "PRICE_CHANGING"
    assert rule["transition_code"] == "price_change_command_created"
    assert rule["state_revision"] == 1
    assert duplicate["rule_decision"]["state_revision"] == 1
    state = store.get(tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1")
    assert state is not None
    assert state.confirmation_status == "confirmed"
    assert state.order_status == "bound"
    assert state.price_change_status == "pending"
    assert state.target_amount_cents == 8_800


def test_verified_action_result_advances_only_bound_order_to_waiting_payment(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    coordinator = RuleStateCoordinator(store)
    result = {"decision": {
        "mode": "auto", "reason": "confirmed_quote_record_bound_to_order",
        "actions": [{
            "id": "event-1:change-order-price", "type": "change_order_price",
            "quote_snapshot": {
                "quote_record_id": "quote-1", "confirmation_version": "confirm-v1",
                "confirmed_ticket_count": 2, "order_id": "order-1",
                "target_amount_cents": 8_800,
            },
        }],
    }}
    coordinator.record_event_decision(body(), result)

    rule = coordinator.record_action_result(
        tenant_id="tenant-1",
        body={
            "event_id": "event-1", "action_id": "event-1:change-order-price",
            "result": {
                "status": "succeeded", "order_id": "order-1",
                "target_amount_cents": 8_800, "verified_amount_cents": 8_800,
            },
        },
    )

    assert rule is not None
    assert rule.state_after == "WAITING_PAYMENT"
    state = store.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert state is not None
    assert state.price_change_status == "succeeded"
    assert state.order_status == "pending_payment"


def test_full_authoritative_lifecycle_advances_monotonically(tmp_path: Path) -> None:
    class Quotes:
        def get_record(self, *, tenant_id: str, record_id: str):
            assert tenant_id == "tenant-1"
            return {
                "record_id": record_id, "status": "succeeded", "ticket_count": 2,
                "total_quote_cents": 8_800,
            } if record_id == "quote-event" else None

    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    coordinator = RuleStateCoordinator(store, quote_store=Quotes())
    quote_body = body("im.message.received")
    quote_body["envelope"]["id"] = "quote-event"
    quoted = coordinator.record_event_decision(quote_body, {"decision": {
        "mode": "auto", "reason": "automatic_reply_ready",
        "actions": [{"id": "quote-event:reply", "type": "send_message", "text": "报价"}],
    }})
    assert quoted["rule_decision"]["state_after"] == "QUOTED"
    assert quoted["decision"]["actions"][0]["text"] == "报价"

    confirm_body = body("im.message.received")
    confirm_body["envelope"]["id"] = "confirm-event"
    confirmed = coordinator.record_event_decision(confirm_body, {"decision": {
        "mode": "auto", "reason": "order_submission_guidance_ready", "actions": [],
    }})
    assert confirmed["rule_decision"]["state_after"] == "CONFIRMED"

    price_result = {"decision": {
        "mode": "auto", "reason": "confirmed_quote_record_bound_to_order",
        "actions": [{
            "id": "event-1:change-order-price", "type": "change_order_price",
            "quote_snapshot": {
                "quote_record_id": "quote-event", "confirmation_version": "confirm-v1",
                "confirmed_ticket_count": 2, "order_id": "order-1", "target_amount_cents": 8_800,
            },
        }],
    }}
    changing = coordinator.record_event_decision(body(), price_result)
    assert changing["rule_decision"]["state_after"] == "PRICE_CHANGING"
    coordinator.record_action_result(
        tenant_id="tenant-1", body={
            "event_id": "event-1", "action_id": "event-1:change-order-price",
            "result": {
                "status": "succeeded", "order_id": "order-1",
                "target_amount_cents": 8_800, "verified_amount_cents": 8_800,
            },
        },
    )

    paid_body = body("order.paid")
    paid_body["envelope"]["id"] = "paid-event"
    paid_body["order"]["orderStatus"] = 2
    paid_body["order"]["payment"] = 8_800
    paid = coordinator.record_event_decision(paid_body, {"decision": {
        "mode": "auto", "reason": "authoritative_payment_confirmation_ready", "actions": [],
    }})
    assert paid["rule_decision"]["state_after"] == "PAID_WAITING_FULFILLMENT"
    assert paid["decision"]["actions"][0]["text"] == ReplyTemplates().payment_success_pending_ticket_template

    shipped_body = body("order.shipped")
    shipped_body["envelope"]["id"] = "shipped-event"
    shipped_body["order"]["orderStatus"] = 3
    shipped = coordinator.record_event_decision(shipped_body, {"decision": {
        "mode": "auto", "reason": "automation_inert_for_event", "actions": [],
    }})
    assert shipped["rule_decision"]["state_after"] == "TICKET_SENT"

    finished_body = body("order.finished")
    finished_body["envelope"]["id"] = "finished-event"
    finished_body["order"]["orderStatus"] = 4
    finished = coordinator.record_event_decision(finished_body, {"decision": {
        "mode": "auto", "reason": "automation_inert_for_event", "actions": [],
    }})
    assert finished["rule_decision"]["state_after"] == "COMPLETED"


def test_authoritative_quote_can_advance_collecting_without_bootstrap_error(tmp_path: Path) -> None:
    class Quotes:
        def get_record(self, *, tenant_id: str, record_id: str):
            return {"status": "succeeded", "ticket_count": 1, "total_quote_cents": 4_830} if record_id == "quote-event" else None

    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    coordinator = RuleStateCoordinator(store, quote_store=Quotes())
    first = body("im.message.received")
    coordinator.record_event_decision(first, {"decision": {
        "mode": "auto", "reason": "automatic_reply_ready", "actions": [],
    }})
    quote = body("im.message.received")
    quote["envelope"]["id"] = "quote-event"

    decorated = coordinator.record_event_decision(quote, {"decision": {
        "mode": "auto", "reason": "automatic_reply_ready", "actions": [],
    }})

    assert decorated["rule_decision"]["state_before"] == "COLLECTING"
    assert decorated["rule_decision"]["state_after"] == "QUOTED"


def test_shipment_template_is_not_emitted_twice_when_engine_also_returns_same_reply(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    coordinator = RuleStateCoordinator(store)
    shipped = body("order.shipped")
    shipped["envelope"]["id"] = "shipped-duplicate-check"
    shipped["order"]["orderStatus"] = 3
    template = ReplyTemplates().order_shipped_template

    decorated = coordinator.record_event_decision(shipped, {"decision": {
        "mode": "auto", "reason": "authoritative_shipped_status_reply_ready",
        "actions": [{"id": "engine-shipped-reply", "type": "send_message", "text": template}],
    }})

    actions = decorated["decision"]["actions"]
    assert len(actions) == 1
    assert actions[0]["text"] == template


def test_official_shipment_can_advance_from_collecting_when_intermediate_webhooks_were_absent(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    coordinator = RuleStateCoordinator(store)
    first = body("im.message.received")
    coordinator.record_event_decision(first, {"decision": {
        "mode": "auto", "reason": "automatic_reply_ready", "actions": [],
    }})
    shipped = body("im.message.received")
    shipped["envelope"]["id"] = "shipped-observed"
    shipped["order"]["orderStatus"] = 3

    decorated = coordinator.record_event_decision(shipped, {"decision": {
        "mode": "auto", "reason": "authoritative_shipped_status_reply_ready", "actions": [],
    }})

    assert decorated["rule_decision"]["state_before"] == "COLLECTING"
    assert decorated["rule_decision"]["state_after"] == "TICKET_SENT"
    assert decorated["rule_decision"]["transition_code"] == "official_shipment_verified"


def test_unverified_order_from_confirmed_state_is_recorded_without_projection_error(tmp_path: Path) -> None:
    class Quotes:
        def get_record(self, *, tenant_id: str, record_id: str):
            return (
                {"status": "succeeded", "ticket_count": 7, "unit_quote_cents": 4_830}
                if record_id == "quote-before-order" else None
            )

    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    coordinator = RuleStateCoordinator(store, quote_store=Quotes())
    quoted = body("im.message.received")
    quoted["envelope"]["id"] = "quote-before-order"
    coordinator.record_event_decision(quoted, {"decision": {
        "mode": "auto", "reason": "image_recognition_reply_ready", "actions": [],
    }})
    confirmed = body("im.message.received")
    confirmed["envelope"]["id"] = "confirm-before-order"
    coordinator.record_event_decision(confirmed, {"decision": {
        "mode": "auto", "reason": "order_submission_guidance_ready", "actions": [],
    }})
    order_event = body("order.created")
    order_event["envelope"]["id"] = "order-event"

    decorated = coordinator.record_event_decision(order_event, {"decision": {
        "mode": "auto", "reason": "exact_order_total_unavailable", "actions": [],
    }})

    assert decorated["rule_decision"]["state_before"] == "CONFIRMED"
    assert decorated["rule_decision"]["state_after"] == "ORDER_UNVERIFIED"
    assert decorated["rule_decision"]["transition_code"] == "order_unverified"


def test_late_paid_webhook_with_already_shipped_readback_projects_ticket_sent(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    coordinator = RuleStateCoordinator(store)
    paid = body("order.paid")
    paid["order"]["orderStatus"] = 3
    paid["order"]["payTime"] = "2026-08-26T05:01:45Z"

    decorated = coordinator.record_event_decision(paid, {"decision": {
        "mode": "auto", "reason": "authoritative_shipped_status_reply_ready", "actions": [],
    }})

    assert decorated["rule_decision"]["state_after"] == "TICKET_SENT"
    assert decorated["rule_decision"]["transition_code"] == "official_shipment_verified"
    state = store.get(tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1")
    assert state is not None
    assert state.order_status == "shipped"
    assert state.fulfillment_status == "shipped"


def test_any_current_human_seller_message_enters_persistent_manual_hold(tmp_path: Path) -> None:
    coordinator, store = coordinator_store(tmp_path)
    value = body(event="im.message.received")
    value["envelope"]["payload"]["remoteMessageId"] = "seller-current"
    value["recent_messages"] = [{
        "direction": "seller", "messageId": "seller-current", "content": "需要几张呢",
    }]

    reduced = coordinator.record_event_decision(value, {"decision": {
        "mode": "auto", "reason": "automatic_reply_ready",
        "actions": [{"id": "reply-1", "type": "send_message", "text": "旧回复"}],
    }})

    assert reduced["rule_decision"]["state_after"] == "MANUAL_HOLD"
    assert reduced["decision"]["actions"] == []
    state = store.get(**identity())
    assert state is not None and state.automation_control == "human_hold"


def test_only_explicit_human_takeover_enters_persistent_manual_hold(tmp_path: Path) -> None:
    coordinator, store = coordinator_store(tmp_path)
    value = body(event="im.message.received")
    value["envelope"]["payload"]["remoteMessageId"] = "seller-current"
    value["recent_messages"] = [{
        "direction": "seller", "messageId": "seller-current", "content": "我来处理，停止自动回复",
    }]

    reduced = coordinator.record_event_decision(value, {"decision": {
        "mode": "auto", "reason": "automatic_reply_ready",
        "actions": [{"id": "reply-1", "type": "send_message", "text": "旧回复"}],
    }})

    assert reduced["rule_decision"]["state_after"] == "MANUAL_HOLD"
    assert reduced["decision"]["actions"] == []
    state = store.get(**identity())
    assert state is not None and state.last_transition_code == "explicit_human_takeover"


def test_second_active_order_enters_manual_hold_and_suppresses_price_command(tmp_path: Path) -> None:
    coordinator, store = coordinator_store(tmp_path)
    first_body = body()
    first = coordinator.record_event_decision(first_body, change_decision())
    assert first["rule_decision"]["state_after"] == "PRICE_CHANGING"

    second_body = body()
    second_body["envelope"]["id"] = "event-second-order"
    second_body["envelope"]["payload"]["orderId"] = "order-2"
    second_body["order"]["orderId"] = "order-2"
    second = coordinator.record_event_decision(second_body, change_decision(order_id="order-2"))

    assert second["rule_decision"]["state_after"] == "MANUAL_HOLD"
    assert second["rule_decision"]["handoff_reason"] == "multiple_active_orders"
    assert second["decision"]["actions"] == []
    state = store.get(**identity())
    assert state is not None and state.order_id == "order-1"


def test_manual_hold_does_not_resume_or_send_on_later_shipment_event(tmp_path: Path) -> None:
    coordinator, store = coordinator_store(tmp_path)
    current = store.get_or_create(**identity())
    store.transition(
        **identity(), expected_revision=current.revision, event_id="manual-hold",
        transition_code="compat.bootstrap.manual_hold", flow_state="MANUAL_HOLD",
        updates={"automation_control": "human_hold"}, allow_compatible_bootstrap=True,
    )

    shipped = body("order.shipped")
    shipped["envelope"]["id"] = "shipment-after-human"
    shipped["order"]["orderStatus"] = 3
    reduced = coordinator.record_event_decision(shipped, {"decision": {
        "mode": "auto", "reason": "authoritative_shipped_status_reply_ready",
        "actions": [{"id": "shipped-reply", "type": "send_message", "text": "已发货"}],
    }})

    assert reduced["decision"]["actions"] == []
    assert reduced["rule_decision"]["state_after"] == "MANUAL_HOLD"
    state = store.get(**identity())
    assert state is not None and state.flow_state == "MANUAL_HOLD"


def test_passwordless_payment_before_amount_verification_pauses_fulfillment(tmp_path: Path) -> None:
    coordinator, store = coordinator_store(tmp_path)
    coordinator.record_event_decision(body(), change_decision())
    paid = body("order.paid")
    paid["envelope"]["id"] = "passwordless-paid"
    paid["order"]["orderStatus"] = 2
    paid["order"]["payment"] = 9_900

    reduced = coordinator.record_event_decision(paid, {"decision": {
        "mode": "auto", "reason": "authoritative_payment_confirmation_ready",
        "actions": [{"id": "unsafe-payment-reply", "type": "send_message", "text": "付款成功，开始出票"}],
    }})

    assert reduced["rule_decision"]["state_after"] == "MANUAL_HOLD"
    assert reduced["rule_decision"]["transition_code"] == "paid_amount_mismatch"
    assert reduced["rule_decision"]["handoff_reason"] == "paid_amount_mismatch"
    assert reduced["decision"]["actions"][0]["text"] == (
        "检测到订单已付款，但付款前未完成金额核验，已暂停出票并转人工处理，请稍候。"
    )
    state = store.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert state is not None
    assert state.payment_status == "mismatch"
    assert state.fulfillment_status == "none"

    late_result = coordinator.record_action_result(
        tenant_id="tenant-1", body={
            "event_id": "event-1", "action_id": "event-1:change-order-price",
            "result": {
                "status": "succeeded", "order_id": "order-1",
                "target_amount_cents": 8_800, "verified_amount_cents": 8_800,
            },
        },
    )
    assert late_result is not None and late_result.state_after == "MANUAL_HOLD"
    assert store.find_by_order(tenant_id="tenant-1", order_id="order-1").flow_state == "MANUAL_HOLD"


def test_unknown_price_result_enters_manual_hold_without_claiming_success(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    coordinator = RuleStateCoordinator(store)
    coordinator.record_event_decision(body(), {"decision": {
        "mode": "auto", "reason": "confirmed_quote_record_bound_to_order",
        "actions": [{
            "id": "event-1:change-order-price", "type": "change_order_price",
            "quote_snapshot": {
                "quote_record_id": "quote-1", "confirmation_version": "confirm-v1",
                "confirmed_ticket_count": 2, "order_id": "order-1", "target_amount_cents": 8_800,
            },
        }],
    }})

    rule = coordinator.record_action_result(
        tenant_id="tenant-1",
        body={
            "event_id": "event-1", "action_id": "event-1:change-order-price",
            "result": {"status": "unknown", "order_id": "order-1", "target_amount_cents": 8_800},
        },
    )

    assert rule is not None
    assert rule.state_after == "MANUAL_HOLD"
    assert rule.handoff_reason == "price_change_unknown"
