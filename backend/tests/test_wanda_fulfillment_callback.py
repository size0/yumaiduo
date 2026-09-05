from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import MovieImageInfo
from app.quote_record_store import QuoteRecordStore
from app.rules_first_store import RulesFirstStore
from app.settings_store import PersistentSettingsStore
from app.shop_automation_store import ShopAutomationStore
from app.transaction_state_store import TransactionStateStore
from app.wanda_fulfillment_callbacks import CallbackVerifier


class PlainProtector:
    def protect(self, value: str) -> str:
        return "protected:" + value.encode("utf-8").hex()

    def unprotect(self, value: str) -> str:
        assert value.startswith("protected:")
        return bytes.fromhex(value.removeprefix("protected:")).decode("utf-8")


class StubRecognitionService:
    async def recognize(self, image: bytes, content_type: str, buyer_message: str = "", *, prior_recognitions: list[MovieImageInfo] | None = None) -> MovieImageInfo:
        raise AssertionError("recognition should not run")


class LegacyChatStub:
    async def reply(self, text: str, conversation_id: str) -> str:
        raise AssertionError("legacy ai_chat_service.reply should not run for canonical scope")


class CanonicalAgentStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def process(self, body: dict[str, object]) -> dict[str, object]:
        self.calls.append(body)
        return {
            "status": "AGENT_REPLY_READY",
            "reply": "canonical reply",
            "actions": [{"type": "send_message", "text": "canonical reply"}],
            "agent_run_id": "run-1",
        }


class DurableRuntimeStub:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def accept_agent_result(self, body: dict[str, object], result: dict[str, object]) -> dict[str, object]:
        self.calls.append((body, result))
        return {"commands": [{"command_id": "cmd-1"}]}


def _make_app(tmp_path: Path, *, settings: Settings, agent: CanonicalAgentStub | None = None, runtime: DurableRuntimeStub | None = None) -> TestClient:
    rules_store = RulesFirstStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    quote_store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    transaction_store = TransactionStateStore(tmp_path / "transactions.json", protector=PlainProtector())
    settings_store = PersistentSettingsStore(
        tmp_path / "settings.json",
        protector=PlainProtector(),
        environment=settings,
    )
    shops = ShopAutomationStore(tmp_path / "shops.json")
    shops.sync("tenant-1", [{
        "accountUnb": "2313315754",
        "shopName": "万达影城",
    }])
    shops.set_settings("tenant-1", "2313315754", enabled=True, canonical_conversation_enabled=True)
    return TestClient(create_app(
        service=StubRecognitionService(),
        chat_reply_service=LegacyChatStub(),
        settings_store=settings_store,
        quote_record_store=quote_store,
        transaction_state_store=transaction_store,
        rules_first_store=rules_store,
        shop_automation_store=shops,
        canonical_conversation_agent=agent,
        rules_first_runtime=runtime,
    ))


def _bootstrap_transaction(store: TransactionStateStore) -> tuple[str, str]:
    state = store.transition(
        tenant_id="tenant-1",
        shop_id="2313315754",
        buyer_id="buyer-1",
        chat_id="chat-1",
        expected_revision=0,
        event_id="bootstrap-order-paid",
        transition_code="compat.bootstrap.order_paid",
        flow_state="PAID_WAITING_FULFILLMENT",
        updates={
            "order_id": "order-1",
            "active_quote_record_id": "quote-1",
            "confirmed_quote_record_id": "quote-1",
            "payment_status": "verified_paid",
            "fulfillment_status": "pending",
            "order_status": "paid",
        },
        allow_compatible_bootstrap=True,
    )
    return state.state_id, state.order_id or ""


def _seed_quote(store: QuoteRecordStore) -> None:
    store.save({
        "record_id": "quote-1",
        "tenant_id": "tenant-1",
        "shop_id": "2313315754",
        "buyer_id": "buyer-1",
        "chat_id": "chat-1",
        "order_id": "order-1",
        "quote_id": "quote-1",
        "created_at": "2026-09-05T10:00:00+00:00",
        "status": "succeeded",
        "delivery_state": "delivered",
        "cinema": "万达影城",
        "original_unit_price_cents": 7_290,
        "member_unit_price_cents": 6_190,
        "unit_quote_cents": 7_150,
    })


def test_canonical_chat_text_route_uses_canonical_agent_when_shop_scope_is_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test-secret")
    agent = CanonicalAgentStub()
    runtime = DurableRuntimeStub()
    client = _make_app(
        tmp_path,
        settings=Settings(chat_api_key="legacy-key"),
        agent=agent,
        runtime=runtime,
    )

    response = client.post(
        "/api/chat/text-messages",
        json={"conversation_id": "chat-1", "text": "老板，刚好想买票"},
        headers={
            "x-wanda-tenant-id": "tenant-1",
            "x-wanda-shop-id": "2313315754",
            "x-wanda-buyer-id": "buyer-1",
            "x-wanda-chat-id": "chat-1",
            "x-wanda-ai-v2-bridge-key": "bridge-test-secret",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"]["message_type"] == "ai_reply"
    assert payload["message"]["text"] == "canonical reply"
    assert len(agent.calls) == 1
    assert runtime.calls and runtime.calls[0][1]["status"] == "AGENT_REPLY_READY"


def test_wanda_fulfillment_callback_is_idempotent_and_supports_send_reconciliation(tmp_path: Path) -> None:
    settings = Settings(
        wanda_fulfillment_callback_enabled=True,
        wanda_fulfillment_callback_secret="fulfillment-secret",
    )
    rules_store = RulesFirstStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    quote_store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    transaction_store = TransactionStateStore(tmp_path / "transactions.json", protector=PlainProtector())
    _seed_quote(quote_store)
    transaction_id, _ = _bootstrap_transaction(transaction_store)

    client = TestClient(create_app(
        service=StubRecognitionService(),
        settings_store=PersistentSettingsStore(
            tmp_path / "settings.json",
            protector=PlainProtector(),
            environment=settings,
        ),
        quote_record_store=quote_store,
        transaction_state_store=transaction_store,
        rules_first_store=rules_store,
    ))

    verifier = CallbackVerifier("fulfillment-secret")
    base_body = {
        "tenant_id": "tenant-1",
        "shop_id": "2313315754",
        "buyer_id": "buyer-1",
        "chat_id": "chat-1",
        "order_id": "order-1",
        "quote_id": "quote-1",
        "transaction_id": transaction_id,
        "event_id": "wanda-callback-1",
        "idempotency_key": "wanda-callback-1",
        "ticket_code": "123456",
        "ticket_code_version": "v1",
        "status": "COMPLETED",
    }

    def post(body: dict[str, object], *, nonce: str | None = None) -> dict[str, object]:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce_value = nonce or str(body["event_id"])
        signature = verifier.sign(raw, timestamp, nonce_value)
        response = client.post(
            "/api/wanda-fulfillment/callback",
            content=raw,
            headers={
                "content-type": "application/json",
                "x-wanda-fulfillment-sign": signature,
                "x-wanda-fulfillment-timestamp": timestamp,
                "x-wanda-fulfillment-nonce": nonce_value,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    first = post(base_body)
    duplicate = post(base_body, nonce="wanda-callback-1-retry")
    reconciled = post({
        **base_body,
        "event_id": "wanda-callback-2",
        "idempotency_key": "wanda-callback-2",
        "delivery_status": "SENT",
        "sent_message_id": "msg-001",
        "send_reconciliation": True,
    })

    assert first["code"] == "WANDA_FULFILLMENT_COMMAND_QUEUED"
    assert first["state_after"] == "READY_FOR_MANUAL_TICKETING"
    assert duplicate["state_after"] == "READY_FOR_MANUAL_TICKETING"
    assert reconciled["code"] == "WANDA_FULFILLMENT_SEND_RECONCILED"
    assert reconciled["state_after"] == "TICKET_SENT"

    with sqlite3.connect(rules_store._path) as connection:
        command_count = connection.execute("SELECT COUNT(*) FROM command_outbox").fetchone()[0]
        callback_count = connection.execute(
            "SELECT COUNT(*) FROM wanda_fulfillment_callback_records WHERE tenant_id=?",
            ("tenant-1",),
        ).fetchone()[0]

    state = transaction_store.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert state is not None and state.flow_state == "TICKET_SENT"
    assert state.fulfillment_status == "shipped"
    assert command_count == 1
    assert callback_count == 2
