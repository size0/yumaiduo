from __future__ import annotations

from pathlib import Path
from time import sleep

from fastapi.testclient import TestClient

from app.conversation_policy_store import ConversationPolicyStore
from app.keyword_image_store import KeywordImageStore
from app.main import FixedWindowRateLimiter, create_app
from app.models import MovieImageInfo
from app.reply_template_store import ReplyTemplateStore
from app.rules_first_store import RulesFirstStore
from app.shop_automation_store import ShopAutomationStore
from app.transaction_state_store import TransactionStateStore
JPEG = b"\xff\xd8\xff\xe0" + b"test-jpeg-content"


class PlainProtector:
    def protect(self, value: str) -> str:
        return f"enc:{value}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


class StubRecognitionService:
    async def recognize(self, image: bytes, content_type: str, buyer_message: str = "", *, prior_recognitions: list[MovieImageInfo] | None = None) -> MovieImageInfo:
        assert image == JPEG
        assert content_type == "image/jpeg"
        assert isinstance(buyer_message, str)
        assert prior_recognitions is not None
        return MovieImageInfo.model_validate(
            {
                "platform": "猫眼电影",
                "cinema_name": "万达影城（深圳龙岗万达广场IMAX激光店）",
                "city": "深圳",
                "movie_name": "奥德赛",
                "date_text": "今天 8月24日",
                "showtime_start": "22:40",
                "showtime_end": "01:32",
                "hall_name": "IMAX激光厅",
                "language": "英语",
                "format": "2D",
                "selected_seats": [],
                "selected_count_visible": 0,
                "displayed_total": 413.4,
                "price_zones": [],
                "confidence": 0.95,
                "missing_fields": [],
                "warnings": [],
            }
        )


def test_health_does_not_expose_secrets() -> None:
    client = TestClient(create_app(service=StubRecognitionService()))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "wanda-movie-image-recognition"}


def test_ai_reply_switch_can_be_read_and_changed_without_restart(tmp_path: Path) -> None:
    policies = ConversationPolicyStore(tmp_path / "conversation-policy.json")
    client = TestClient(create_app(
        service=StubRecognitionService(), conversation_policy_store=policies,
    ))

    assert client.get("/api/settings/conversation-policy").json()["ai_reply_enabled"] is True
    response = client.put("/api/settings/conversation-policy", json={"ai_reply_enabled": False})

    assert response.status_code == 200
    assert response.json()["ai_reply_enabled"] is False
    assert policies.current().ai_reply_enabled is False


def test_v4_plugin_bridge_persists_before_accepting_and_is_authenticated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test-secret")
    inbox = RulesFirstStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    client = TestClient(create_app(service=StubRecognitionService(), rules_first_store=inbox))
    endpoint = "/api/wanda-ai-v2/plugin/events/process"
    payload = {
        "envelope": {"id": "event-1", "tenantId": "tenant-1", "event": "im.message.received", "payload": {}},
        "session": None,
        "order": None,
        "recent_messages": [],
    }

    assert client.post(endpoint, json=payload).status_code == 401
    response = client.post(endpoint, json=payload, headers={"X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret"})
    assert response.status_code == 202
    assert response.json() == {"event_id": "event-1", "accepted": True, "duplicate": False}
    duplicate = client.post(endpoint, json=payload, headers={"X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret"})
    assert duplicate.json() == {"event_id": "event-1", "accepted": True, "duplicate": True}


def test_plugin_event_and_action_result_persist_authoritative_rule_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test-secret")
    monkeypatch.setenv("WANDA_EXTERNAL_WRITES_ENABLED", "true")

    class Automation:
        async def process_event(self, _: object) -> dict[str, object]:
            return {"decision": {
                "mode": "auto", "reason": "confirmed_quote_record_bound_to_order",
                "actions": [{
                    "id": "event-state:change-order-price", "type": "change_order_price",
                    "quote_snapshot": {
                        "quote_record_id": "quote-1", "confirmation_version": "confirm-1",
                        "confirmed_ticket_count": 2, "order_id": "order-1",
                        "target_amount_cents": 8_800,
                    },
                }],
            }}

        def process_action_result(self, _: object) -> dict[str, object]:
            return {"ok": True, "status": "recorded", "actions": []}

    states = TransactionStateStore(tmp_path / "states.json", protector=PlainProtector())
    inbox = RulesFirstStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    client = TestClient(create_app(
        service=StubRecognitionService(), plugin_automation=Automation(),
        transaction_state_store=states, rules_first_store=inbox,
    ))
    event = {
        "envelope": {
            "id": "event-state", "tenantId": "tenant-1", "event": "order.created",
            "payload": {"orderId": "order-1"},
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "order": {
            "orderId": "order-1", "accountUnb": "shop-1", "buyerUnb": "buyer-1",
            "chatId": "chat-1", "orderStatus": 1, "quantity": 2,
        },
        "recent_messages": [],
    }

    with client:
        planned = client.post(
            "/api/wanda-ai-v2/plugin/events/process", json=event,
            headers={"X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret"},
        )
        commands: list[dict[str, object]] = []
        for _ in range(50):
            claimed = client.post(
                "/api/wanda-ai-v2/plugin/commands/claim", json={"limit": 10},
                headers={"X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret"},
            ).json()
            commands = claimed["commands"]
            if commands:
                break
            sleep(0.01)
        assert {item["command_type"] for item in commands} == {"change_order_price"}
        command = next(item for item in commands if item["command_type"] == "change_order_price")
        reported = client.post(
            f"/api/wanda-ai-v2/plugin/commands/{command['command_id']}/result",
            json={
                "lease_token": command["lease_token"],
                "result": {
                    "status": "succeeded", "order_id": "order-1",
                    "target_amount_cents": 8_800, "verified_amount_cents": 8_800,
                },
            },
            headers={"X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret"},
        )

    assert planned.status_code == 202
    assert planned.json()["accepted"] is True
    assert reported.status_code == 200
    assert reported.json()["status"] == "succeeded"
    state = states.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert state is not None and state.price_change_status == "succeeded"


def test_normal_fulfillment_does_not_require_claim_or_ticket_upload(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test-secret")
    protection = PlainProtector()
    rules = RulesFirstStore(tmp_path / "rules.sqlite3", protector=protection)
    states = TransactionStateStore(tmp_path / "states.json", protector=protection)
    current = states.transition(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        expected_revision=0, event_id="paid-event:decision", transition_code="official_payment_verified",
        flow_state="PAID_WAITING_FULFILLMENT",
        updates={"order_status": "paid", "payment_status": "verified_paid", "fulfillment_status": "pending"},
        allow_compatible_bootstrap=True,
    )
    source = {
        "envelope": {
            "id": "paid-event", "tenantId": "tenant-1", "event": "order.paid",
            "payload": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
    }
    rules.enqueue_event(source)
    task = rules.create_manual_task(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        transaction_id=current.state_id, transaction_revision=current.revision,
        reason="fulfillment_required", details={"event_id": "paid-event"},
    )
    client = TestClient(create_app(
        service=StubRecognitionService(), rules_first_store=rules,
        transaction_state_store=states,
    ))

    claimed = client.post(
        f"/api/rules-first/manual-tasks/{task['task_id']}/claim",
        json={"expected_revision": current.revision, "operator_id": "seller-1"},
        headers={"X-Wanda-Tenant-Id": "tenant-1"},
    )
    assert claimed.status_code == 409
    assert claimed.json()["detail"] == "fulfillment_task_waits_for_official_shipment"
    assert rules.claim_commands() == []
    unchanged = states.get(tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1")
    assert unchanged is not None and unchanged.flow_state == "PAID_WAITING_FULFILLMENT"
    assert rules.list_manual_tasks("tenant-1")[0]["status"] == "pending"


def test_keyword_reply_image_upload_and_plugin_fetch_are_tenant_scoped(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test-secret")
    store = KeywordImageStore(tmp_path / "keyword-images", protector=PlainProtector())
    templates = ReplyTemplateStore(tmp_path / "reply-templates.json")
    client = TestClient(create_app(
        service=StubRecognitionService(), keyword_image_store=store,
        reply_template_store=templates,
    ))
    image = b"\x89PNG\r\n\x1a\n" + b"keyword-reply"

    uploaded = client.post(
        "/api/settings/reply-keyword-images",
        headers={"X-Wanda-Tenant-Id": "tenant-a"},
        files={"image": ("reply.png", image, "image/png")},
    )

    assert uploaded.status_code == 200
    asset_id = uploaded.json()["asset_id"]
    saved = client.put(
        "/api/settings/reply-templates",
        headers={"X-Wanda-Tenant-Id": "tenant-a"},
        json={"keyword_replies": [{
            "id": "rule-image", "keywords": ["教程"], "match_mode": "exact",
            "reply": "请看图片教程。", "image_asset_id": asset_id,
            "enabled": True, "priority": 100,
        }]},
    )
    assert saved.status_code == 200
    assert saved.json()["keyword_replies"][0]["image_asset_id"] == asset_id
    assert saved.json()["keyword_replies"][0]["image_tenant_id"] == "tenant-a"
    denied_save = client.put(
        "/api/settings/reply-templates",
        headers={"X-Wanda-Tenant-Id": "tenant-b"},
        json={"keyword_replies": [{
            "id": "rule-image", "keywords": ["教程"], "match_mode": "exact",
            "reply": "请看图片教程。", "image_asset_id": asset_id,
        }]},
    )
    assert denied_save.status_code == 422
    denied = client.get(
        f"/api/wanda-ai-v2/plugin/keyword-images/{asset_id}",
        headers={
            "X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret",
            "X-Yumaiduo-Tenant-Id": "tenant-b",
        },
    )
    assert denied.status_code == 404
    fetched = client.get(
        f"/api/wanda-ai-v2/plugin/keyword-images/{asset_id}",
        headers={
            "X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret",
            "X-Yumaiduo-Tenant-Id": "tenant-a",
        },
    )
    assert fetched.status_code == 200
    assert fetched.content == image
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.headers["x-keyword-image-sha256"] == uploaded.json()["sha256"]


def test_shop_switches_are_tenant_scoped_and_bridge_authenticated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test-secret")
    store = ShopAutomationStore(tmp_path / "shops.json")
    client = TestClient(create_app(service=StubRecognitionService(), shop_automation_store=store))
    bridge_headers = {"X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret"}

    synced = client.post("/api/wanda-ai-v2/plugin/shops/sync", headers=bridge_headers, json={
        "tenant_id": "tenant-a", "shops": [{"accountUnb": "shop-1", "shopName": "一号店"}],
    })
    listed = client.get("/api/plugin/shops", headers={"X-Wanda-Tenant-Id": "tenant-a"})
    updated = client.put("/api/plugin/shops/shop-1", headers={"X-Wanda-Tenant-Id": "tenant-a"}, json={"enabled": False})
    other = client.get("/api/plugin/shops", headers={"X-Wanda-Tenant-Id": "tenant-b"})

    assert synced.json() == {"accepted": 1}
    assert listed.json() == {"shops": [{"shop_id": "shop-1", "shop_name": "一号店", "enabled": True}]}
    assert updated.json()["shop"]["enabled"] is False
    assert other.json() == {"shops": []}
    assert client.get("/api/plugin/shops").status_code == 401


def test_reply_templates_can_be_loaded_and_saved(tmp_path: Path) -> None:
    client = TestClient(create_app(
        service=StubRecognitionService(),
        reply_template_store=ReplyTemplateStore(tmp_path / "templates.json"),
    ))
    current = client.get("/api/settings/reply-templates").json()
    current["guidance_template"] = "请上传截图，变量使用中文名称。"

    saved = client.put("/api/settings/reply-templates", json=current)

    assert saved.status_code == 200
    assert saved.json()["guidance_template"] == "请上传截图，变量使用中文名称。"
    assert saved.json()["revision"] == 1


def test_upload_movie_screenshot_returns_structured_information() -> None:
    client = TestClient(create_app(service=StubRecognitionService()))
    response = client.post(
        "/api/movie-images/recognize",
        files={"image": ("ticket.jpg", JPEG, "image/jpeg")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["movie_name"] == "奥德赛"
    assert body["data"]["showtime_start"] == "22:40"


def test_chat_image_message_returns_left_side_assistant_reply() -> None:
    client = TestClient(create_app(service=StubRecognitionService()))
    response = client.post(
        "/api/chat/image-messages",
        data={"conversation_id": "preview-chat", "message_text": "帮我看看这个场次"},
        files={"image": ("ticket.jpg", JPEG, "image/jpeg")},
    )

    assert response.status_code == 200
    message = response.json()["message"]
    assert message["conversation_id"] == "preview-chat"
    assert message["role"] == "assistant"
    assert message["message_type"] == "movie_recognition"
    assert "奥德赛" in message["text"]
    assert "截图金额不作为最终报价" in message["text"]
    assert "座位：W+座位" in message["text"]
    assert message["recognition"]["cinema_name"].startswith("万达影城")
    assert message["recognition"]["seat_display"] == "W+座位"


def test_chat_image_messages_reuse_recent_context_in_same_conversation() -> None:
    received_contexts: list[list[MovieImageInfo]] = []

    class ContextAwareService:
        async def recognize(
            self,
            _image: bytes,
            _content_type: str,
            _buyer_message: str = "",
            *,
            prior_recognitions: list[MovieImageInfo] | None = None,
        ) -> MovieImageInfo:
            received_contexts.append(list(prior_recognitions or []))
            return MovieImageInfo(
                movie_name="奥德赛",
                showtime_start="16:20",
                selected_count_visible=0,
                confidence=0.8,
            )

    client = TestClient(create_app(service=ContextAwareService()))
    for name in ("schedule.jpg", "seats.jpg"):
        response = client.post(
            "/api/chat/image-messages",
            data={"conversation_id": "same-order", "message_text": ""},
            files={"image": (name, JPEG, "image/jpeg")},
        )
        assert response.status_code == 200

    assert received_contexts[0] == []
    assert len(received_contexts[1]) == 1
    assert received_contexts[1][0].movie_name == "奥德赛"


def test_chat_image_message_allows_an_image_without_caption() -> None:
    client = TestClient(create_app(service=StubRecognitionService()))
    response = client.post(
        "/api/chat/image-messages",
        data={"conversation_id": "preview-chat", "message_text": ""},
        files={"image": ("ticket.jpg", JPEG, "image/jpeg")},
    )
    assert response.status_code == 200
    assert response.json()["message"]["recognition"]["movie_name"] == "奥德赛"


def test_chat_text_message_reserves_future_ai_customer_service_contract() -> None:
    client = TestClient(create_app(service=StubRecognitionService()))
    response = client.post(
        "/api/chat/text-messages",
        json={"conversation_id": "preview-chat", "text": "你好，怎么买票？"},
    )

    assert response.status_code == 200
    message = response.json()["message"]
    assert message["role"] == "assistant"
    assert message["message_type"] == "guidance"
    assert "上传" in message["text"]


def test_chat_text_message_rejects_empty_or_oversized_input() -> None:
    client = TestClient(create_app(service=StubRecognitionService()))
    assert client.post("/api/chat/text-messages", json={"conversation_id": "chat", "text": "   "}).status_code == 422
    assert client.post("/api/chat/text-messages", json={"conversation_id": "chat", "text": "x" * 2001}).status_code == 422


def test_upload_requires_an_image() -> None:
    client = TestClient(create_app(service=StubRecognitionService()))
    response = client.post("/api/movie-images/recognize")
    assert response.status_code == 422


def test_home_page_contains_upload_ui_and_security_headers() -> None:
    client = TestClient(create_app(service=StubRecognitionService()))
    response = client.get("/")
    assert response.status_code == 200
    assert "拖拽图片到对话区" in response.text
    assert "/api/chat/image-messages" in response.text
    assert "/api/chat/text-messages" in response.text
    assert 'data-role="assistant"' in response.text
    assert 'id="composer"' in response.text
    assert "正在识别图片" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert len(response.headers["x-request-id"]) == 16
    assert response.headers["server-timing"].startswith("total;dur=")
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_expensive_recognition_endpoint_is_rate_limited() -> None:
    client = TestClient(
        create_app(
            service=StubRecognitionService(),
            rate_limiter=FixedWindowRateLimiter(limit=1, window_seconds=60),
        )
    )
    first = client.post("/api/movie-images/recognize", files={"image": ("ticket.jpg", JPEG, "image/jpeg")})
    second = client.post("/api/movie-images/recognize", files={"image": ("ticket.jpg", JPEG, "image/jpeg")})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limit_exceeded"
