from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.rules_first_runtime import RulesFirstRuntime
from app.rules_first_state_store import SqliteTransactionStateStore
from app.rules_first_store import RulesFirstStore
from app.wplus_fulfillment import WplusFulfillmentMarkService

from test_wplus_fulfillment_mark_9b3 import Detector, IDENTITY, QuoteBook, seed


def body(event_id: str = "mark-event") -> dict[str, object]:
    return {
        "envelope": {
            "id": event_id, "tenantId": "tenant-a", "event": "im.message.received",
            "payload": {
                "accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a",
                "itemId": "purchase-1", "messageId": "message-1",
                "imageUrls": ["https://img.alicdn.com/mark.png"],
            },
        },
        "session": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a"},
    }


class Engine:
    calls = 0

    async def process_event(self, _body):
        self.calls += 1
        raise AssertionError("waiting mark must not enter quote engine")

    def process_action_result(self, _body):
        return {}


class Coordinator:
    calls = 0

    def record_event_decision(self, _body, _result):
        self.calls += 1
        raise AssertionError("waiting mark result is already reduced")

    def record_action_result(self, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_rules_first_routes_waiting_mark_before_legacy_engine_and_persists_command(tmp_path: Path):
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3")
    seed(states, flow_state="WAITING_WPLUS_MARK")
    marks = WplusFulfillmentMarkService(states, quote_store=QuoteBook(), mark_detector=Detector(False))
    rules = RulesFirstStore(tmp_path / "rules.sqlite3")
    runtime = RulesFirstRuntime(
        rules, Engine(), Coordinator(), states, fulfillment_mark_handler=marks.process_event,
    )
    rules.enqueue_event(body())
    assert await runtime.drain_once() is True
    current = states.get(**IDENTITY)
    assert current is not None and current.flow_state == "WAITING_WPLUS_MARK"
    commands = rules.claim_commands(limit=10)
    assert len(commands) == 1
    assert commands[0]["action"]["text"] == "辛苦标记一下位置截图发我哈"


def test_main_waiting_mark_entry_is_terminal_before_canonical_or_legacy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test")
    monkeypatch.setenv("CANONICAL_QUOTE_RUNTIME_ENABLED", "true")
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3")
    seed(states, flow_state="WAITING_WPLUS_MARK")
    detector = Detector(True)
    marks = WplusFulfillmentMarkService(states, quote_store=QuoteBook(), mark_detector=detector)
    rules = RulesFirstStore(tmp_path / "rules.sqlite3")

    class Canonical:
        calls = 0

        async def process_image_event(self, _body):
            self.calls += 1
            return {"status": "QUOTED"}

    canonical = Canonical()
    client = TestClient(create_app(
        service=object(), canonical_quote_runtime=canonical,
        transaction_state_store=states, rules_first_store=rules,
        wplus_fulfillment_mark_service=marks,
    ))
    response = client.post(
        "/api/wanda-ai-v2/plugin/events/process", json=body("entry-event"),
        headers={"x-wanda-ai-v2-bridge-key": "bridge-test"},
    )
    assert response.status_code == 202
    assert response.json()["fulfillment_mark_routed"] is True
    assert canonical.calls == 0
    assert detector.calls == 1
