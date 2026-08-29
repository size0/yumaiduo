from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path

from app.rules_first_store import RulesFirstStore


class PlainProtector:
    def protect(self, value: str) -> str:
        return f"enc:{value}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


def event(index: int) -> dict[str, object]:
    return {
        "envelope": {
            "id": f"event-{index}", "tenantId": "tenant-1", "event": "order.created",
            "payload": {
                "accountUnb": "shop-1", "peerUnb": f"buyer-{index}",
                "chatId": f"chat-{index}", "orderId": f"order-{index}",
            },
        },
        "session": {"accountUnb": "shop-1", "peerUnb": f"buyer-{index}", "chatId": f"chat-{index}"},
    }


def price_action(index: int) -> dict[str, object]:
    return {
        "id": f"event-{index}:change-order-price", "type": "change_order_price",
        "quote_snapshot": {
            "order_id": f"order-{index}", "quote_record_id": f"quote-{index}",
            "confirmation_version": "confirm-1", "target_amount_cents": 8_800 + index,
        },
    }


def test_receive_order_causality_covers_all_three_event_permutations_for_50_rounds(tmp_path: Path) -> None:
    store = RulesFirstStore(tmp_path / "ordered.sqlite3", protector=PlainProtector())
    kinds = ("buyer.message", "quote.confirmed", "order.created")
    variants = list(permutations(kinds))
    expected: list[str] = []
    for round_index in range(50):
        for position, kind in enumerate(variants[round_index % len(variants)]):
            item = event(round_index * 10 + position)
            item["envelope"]["event"] = kind
            item["envelope"]["id"] = f"ordered-{round_index}-{position}"
            expected.append(item["envelope"]["id"])
            store.enqueue_event(item)
    claimed: list[str] = []
    while item := store.claim_event():
        claimed.append(item["event_id"])
        store.complete_event(item["inbox_id"], item["lease_token"], commands=[], state_revision=0)
    assert claimed == expected


def test_same_conversation_concurrent_ingress_loses_no_event_for_50_rounds(tmp_path: Path) -> None:
    store = RulesFirstStore(tmp_path / "concurrent.sqlite3", protector=PlainProtector())
    for round_index in range(50):
        values = []
        for position, kind in enumerate(("buyer.message", "quote.confirmed", "order.created")):
            item = event(round_index * 10 + position)
            item["envelope"]["event"] = kind
            item["envelope"]["id"] = f"concurrent-{round_index}-{position}"
            values.append(item)
        with ThreadPoolExecutor(max_workers=3) as executor:
            receipts = list(executor.map(store.enqueue_event, values))
        assert all(receipt["accepted"] and not receipt["duplicate"] for receipt in receipts)
    observed: set[str] = set()
    while item := store.claim_event():
        observed.add(item["event_id"])
        store.complete_event(item["inbox_id"], item["lease_token"], commands=[], state_revision=0)
    assert observed == {f"concurrent-{round_index}-{position}" for round_index in range(50) for position in range(3)}


def test_four_durable_crash_boundaries_repeat_50_rounds_without_duplicate_commands(tmp_path: Path) -> None:
    path = tmp_path / "rules.sqlite3"
    protection = PlainProtector()
    start = datetime(2026, 8, 26, tzinfo=timezone.utc)

    for index in range(50):
        runtime = RulesFirstStore(path, protector=protection)
        assert runtime.enqueue_event(event(index))["accepted"] is True  # crash after inbox commit
        runtime = RulesFirstStore(path, protector=protection)
        claimed_event = runtime.claim_event(now=start)
        assert claimed_event is not None
        commands = runtime.complete_event(
            claimed_event["inbox_id"], claimed_event["lease_token"],
            commands=[price_action(index)], state_revision=1,
        )
        assert len(commands) == 1

        claimed = runtime.claim_commands(now=start, lease_seconds=60)[0]  # crash after command claim
        runtime = RulesFirstStore(path, protector=protection)
        reclaimed = runtime.claim_commands(now=start + timedelta(seconds=61), lease_seconds=60)[0]
        assert reclaimed["command_id"] == claimed["command_id"]

        # Simulate platform-write result being unknown before durable result commit.
        unknown = runtime.record_command_result(
            reclaimed["command_id"], reclaimed["lease_token"],
            {"status": "unknown", "order_id": f"order-{index}"}, now=start,
        )
        assert unknown["status"] == "reconciling"
        read_only = runtime.claim_commands(now=start + timedelta(seconds=5))[0]
        assert read_only["reconciliation_only"] is True

        # A verified result committed before reply-result acknowledgement is idempotent.
        result = {
            "status": "succeeded", "order_id": f"order-{index}",
            "target_amount_cents": 8_800 + index, "verified_amount_cents": 8_800 + index,
        }
        finished = runtime.record_command_result(
            read_only["command_id"], read_only["lease_token"], result,
            now=start + timedelta(seconds=5),
        )
        assert finished["status"] == "succeeded"
        assert runtime.record_command_result(
            read_only["command_id"], read_only["lease_token"], result,
            now=start + timedelta(seconds=6),
        )["status"] == "succeeded"
