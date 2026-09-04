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
