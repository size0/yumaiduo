from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.canonical_conversation_agent import AgentContextBuilder
from app.conversation_fact_patch_parser import ConversationFactPatchParser
from app.conversation_fact_store import ConversationFactStore


FACTS = {
    "city": "广州",
    "cinema": "广州天河万达影城",
    "cinema_address": "天河路",
    "movie": "测试电影",
    "quote_date": "2026-09-11",
    "showtime_start": "08:40",
    "hall": "IMAX厅",
    "dimension": "IMAX",
}


def store(tmp_path: Path) -> ConversationFactStore:
    return ConversationFactStore(tmp_path / "conversation-facts.sqlite3", ttl_seconds=1_800)


def test_parser_supports_time_reference_quantity_and_cinema() -> None:
    parser = ConversationFactPatchParser()
    facts = {**FACTS}

    time_patch = parser.parse("13点10分那场", facts)
    assert time_patch.facts == {"showtime_start": "13:10"}
    assert time_patch.requires_requote is True

    quantity_patch = parser.parse("两张", facts)
    assert quantity_patch.facts == {"ticket_count": 2}

    cinema_patch = parser.parse("还是刚才那个影院", facts)
    assert cinema_patch.facts == {"cinema": FACTS["cinema"]}
    assert cinema_patch.is_reference is True

    assert parser.parse("那场", facts).facts == {}
    assert parser.parse("换成13:10", facts).facts == {"showtime_start": "13:10"}


def test_canonical_and_legacy_writers_share_the_same_fact_schema(tmp_path: Path) -> None:
    facts = store(tmp_path)
    body = {
        "envelope": {
            "id": "image-1", "tenantId": "tenant-a", "event": "im.message.received",
            "payload": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a", "itemId": "purchase-a"},
        },
        "session": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a"},
    }
    saved = facts.record_canonical_event(body, {
        "status": "QUOTED",
        "recognition": {
            "city_text": "广州", "cinema_text": "广州天河万达影城", "movie": "测试电影",
            "show_date": "2026-09-11", "start_time": "08:40", "hall": "IMAX厅",
            "selected_seats": ["6排6座"],
        },
        "quote": {"request_type": "EXACT_SEATS"},
    })
    assert saved is not None
    current = facts.get_current(
        tenant_id="tenant-a", shop_id="shop-a", buyer_id="buyer-a", chat_id="chat-a",
        purchase_context_id="purchase-a",
    )
    assert current is not None
    assert current["facts"]["cinema"] == "广州天河万达影城"
    assert current["facts"]["selected_seats"] == ["6排6座"]
    assert current["facts"]["seat_request_type"] == "EXACT_SEATS"


def test_store_merges_facts_and_isolates_purchase_identity(tmp_path: Path) -> None:
    facts = store(tmp_path)
    saved = facts.save(
        tenant_id="tenant-a", shop_id="shop-a", buyer_id="buyer-a", chat_id="chat-a",
        purchase_context_id="purchase-a", facts=FACTS, source="canonical_image",
        event_id="image-1",
    )
    assert saved["facts"] == FACTS
    assert facts.schema_version() == 2


def test_agent_state_is_durable_revisioned_and_identity_scoped(tmp_path: Path) -> None:
    facts = store(tmp_path)
    identity = dict(tenant_id="tenant-a", shop_id="shop-a", buyer_id="buyer-a", chat_id="chat-a", purchase_context_id="purchase-a")
    saved = facts.save_agent_state(**identity, state={"user_goal": "CHANGE_SHOW", "conversation_phase": "RESOLVE_SHOW"})
    assert saved["revision"] == 1
    loaded = facts.load_agent_state(**identity)
    assert loaded["user_goal"] == "CHANGE_SHOW"
    assert loaded["revision"] == 1
    updated = facts.save_agent_state(**identity, expected_revision=1, state={"user_goal": "SET_TICKET_COUNT", "conversation_phase": "COLLECT_SEAT_OR_COUNT"})
    assert updated["revision"] == 2
    assert facts.load_agent_state(**{**identity, "buyer_id": "buyer-b"}) == {}
    assert facts.journal_mode() == "wal"

    facts.save(
        tenant_id="tenant-a", shop_id="shop-a", buyer_id="buyer-a", chat_id="chat-a",
        purchase_context_id="purchase-a", facts={"showtime_start": "13:10", "ticket_count": 2},
        source="legacy_text_patch", event_id="text-1",
    )
    current = facts.get_current(
        tenant_id="tenant-a", shop_id="shop-a", buyer_id="buyer-a", chat_id="chat-a",
        purchase_context_id="purchase-a",
    )
    assert current is not None
    assert current["facts"]["cinema"] == FACTS["cinema"]
    assert current["facts"]["showtime_start"] == "13:10"
    assert current["facts"]["ticket_count"] == 2
    assert facts.get_current(
        tenant_id="tenant-a", shop_id="shop-a", buyer_id="buyer-other", chat_id="chat-a",
        purchase_context_id="purchase-a",
    ) is None
    assert facts.get_current(
        tenant_id="tenant-a", shop_id="shop-other", buyer_id="buyer-a", chat_id="chat-a",
        purchase_context_id="purchase-a",
    ) is None


def test_expired_facts_are_not_loaded(tmp_path: Path) -> None:
    facts = ConversationFactStore(tmp_path / "conversation-facts.sqlite3", ttl_seconds=60)
    observed = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
    facts.save(
        tenant_id="tenant-a", shop_id="shop-a", buyer_id="buyer-a", chat_id="chat-a",
        purchase_context_id="purchase-a", facts=FACTS, source="canonical_image",
        observed_at=observed,
    )
    assert facts.get_current(
        tenant_id="tenant-a", shop_id="shop-a", buyer_id="buyer-a", chat_id="chat-a",
        purchase_context_id="purchase-a", now=observed + timedelta(seconds=61),
    ) is None
    context = facts.load_context(
        now=observed + timedelta(seconds=61),
        tenant_id="tenant-a", shop_id="shop-a", buyer_id="buyer-a", chat_id="chat-a",
        purchase_context_id="purchase-a",
    )
    # The real clock is intentionally not used for this assertion; expiry is
    # verified through get_current with an injected reference time above.
    assert context["facts"] == {}


@pytest.mark.asyncio
async def test_agent_builder_loads_persistent_facts_without_confirming_them(tmp_path: Path) -> None:
    facts = store(tmp_path)
    facts.save(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        purchase_context_id="purchase-1", facts=FACTS, source="canonical_image",
    )
    body = {
        "envelope": {"id": "text-1", "tenantId": "tenant-1", "event": "im.message.received", "payload": {
            "accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1", "itemId": "purchase-1",
            "remoteMessageId": "message-1", "content": "13点10分那场", "messageType": 1,
        }},
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
    }
    context = await AgentContextBuilder(fact_store=facts).build(body)
    inherited = context.to_dict()["inherited_screenshot_context"]
    assert inherited["source"] == "canonical_image"
    assert inherited["facts"]["cinema"] == FACTS["cinema"]
    assert context.to_dict()["confirmed_facts"] == {}

    body["envelope"]["payload"].pop("itemId")
    continued = await AgentContextBuilder(fact_store=facts).build(body)
    assert continued.purchase_context_id == "purchase-1"
    assert continued.inherited_screenshot_context["facts"]["movie"] == FACTS["movie"]

def test_ordinal_and_dimension_select_persisted_candidates():
    facts = {"candidate_shows": [
        {"show_id": "a", "start_time": "08:40", "dimension": "2D"},
        {"show_id": "b", "start_time": "11:20", "dimension": "IMAX"},
        {"show_id": "c", "start_time": "13:10", "dimension": "2D"},
    ]}
    parser = ConversationFactPatchParser()
    assert parser.parse("第二场", facts).facts["showtime_start"] == "11:20"
    assert parser.parse("IMAX那场", facts).facts["showtime_start"] == "11:20"
