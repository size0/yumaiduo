from __future__ import annotations

from pathlib import Path

import pytest

from app.conversation_fact_store import ConversationFactStore
from app.models import RealQuote
from app.plugin_automation import RulesFirstDecisionEngine


class ShopStub:
    def is_enabled(self, _tenant: str, _shop: str) -> bool:
        return True

    def is_canonical_quote_enabled(self, _tenant: str, _shop: str) -> bool:
        return False

    def is_canonical_conversation_enabled(self, _tenant: str, _shop: str) -> bool:
        return False


class QuoteStub:
    def __init__(self) -> None:
        self.requests = []

    async def quote(self, recognition):
        self.requests.append(recognition)
        return RealQuote(
            quote_scope="area_preview", quote_date=recognition.date,
            seat_zone_type="W+", member_unit_price_cents=3590,
            unit_quote_cents=3590, needs_ticket_count=True,
            matched_city_name=recognition.city,
            matched_cinema_name=recognition.cinema_name,
            matched_movie_name=recognition.movie_name,
            matched_showtime_start=recognition.showtime_start,
            matched_hall_name=recognition.hall_name,
        )


class RecognitionStub:
    pass


@pytest.mark.asyncio
async def test_canonical_facts_are_loaded_by_legacy_text_and_requoted(tmp_path: Path) -> None:
    facts = ConversationFactStore(tmp_path / "facts.sqlite3")
    facts.save(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        purchase_context_id="purchase-1",
        facts={
            "city": "广州", "cinema": "广州天河万达影城", "movie": "测试电影",
            "quote_date": "2026-09-11", "showtime_start": "08:40", "hall": "IMAX厅",
        }, source="canonical_image", event_id="image-1",
    )
    quote_service = QuoteStub()
    body = {
        "envelope": {
            "id": "text-1", "tenantId": "tenant-1", "event": "im.message.received",
            "payload": {
                "accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1",
                "itemId": "purchase-1", "remoteMessageId": "message-1",
                "content": "13点10分那场", "messageType": 1,
            },
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "recent_messages": [{
            "direction": "buyer", "messageType": 1, "content": "13点10分那场",
            "messageId": "message-1",
        }],
    }
    engine = RulesFirstDecisionEngine(
        RecognitionStub(), quote_service, shop_store=ShopStub(),
        conversation_fact_store=facts,
    )

    result = await engine.process_event(body)

    assert result["decision"]["reason"] == "conversation_fact_patch_requoted"
    assert len(quote_service.requests) == 1
    request = quote_service.requests[0]
    assert request.city == "广州"
    assert request.cinema_name == "广州天河万达影城"
    assert request.movie_name == "测试电影"
    assert request.date_text == "2026-09-11"
    assert request.showtime_start == "13:10"
    assert request.hall_name == "IMAX厅"
