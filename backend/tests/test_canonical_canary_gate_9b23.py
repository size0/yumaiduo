from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.shop_automation_store import ShopAutomationStore
from app.rules_first_store import RulesFirstStore


class Runtime:
    def __init__(self, status: str = "QUOTED") -> None:
        self.status = status
        self.calls: list[dict[str, object]] = []

    async def process_image_event(self, body):
        self.calls.append(body)
        return {"status": self.status, "route": "WANDA_SELF"}

    async def aclose(self):
        return None


def event(event_id: str, tenant: str, shop: str) -> dict[str, object]:
    return {
        "envelope": {
            "id": event_id, "tenantId": tenant, "event": "im.message.received",
            "payload": {
                "accountUnb": shop, "peerUnb": "buyer-1", "chatId": "chat-1",
                "imageUrls": ["https://img.test/seat.webp"],
            },
        },
        "session": {"accountUnb": shop, "peerUnb": "buyer-1", "chatId": "chat-1"},
        "recent_messages": [], "order": None,
    }


def app(tmp_path: Path, runtime: Runtime):
    return create_app(
        service=object(), canonical_quote_runtime=runtime,
        shop_automation_store=ShopAutomationStore(tmp_path / "shops.json"),
        rules_first_store=RulesFirstStore(tmp_path / "rules.sqlite3"),
    )


def test_unconfigured_shop_defaults_canonical_off(tmp_path: Path) -> None:
    store = ShopAutomationStore(tmp_path / "shops.json")
    store.sync("tenant-a", [{"accountUnb": "shop-a", "shopName": "A"}])
    assert store.is_canonical_quote_enabled("tenant-a", "shop-a") is False


def test_tenant_shop_canary_selects_canonical_or_legacy_exclusively(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test")
    monkeypatch.setenv("CANONICAL_QUOTE_RUNTIME_ENABLED", "true")
    shops = ShopAutomationStore(tmp_path / "shops.json")
    shops.sync("tenant-a", [
        {"accountUnb": "shop-a", "shopName": "A"},
        {"accountUnb": "shop-b", "shopName": "B"},
    ])
    shops.set_canonical_enabled("tenant-a", "shop-a", True)
    runtime = Runtime()
    client = TestClient(create_app(
        service=object(), canonical_quote_runtime=runtime,
        shop_automation_store=shops, rules_first_store=RulesFirstStore(tmp_path / "rules.sqlite3"),
    ))
    headers = {"x-wanda-ai-v2-bridge-key": "bridge-test"}

    canonical = client.post("/api/wanda-ai-v2/plugin/events/process", json=event("e-a", "tenant-a", "shop-a"), headers=headers)
    legacy = client.post("/api/wanda-ai-v2/plugin/events/process", json=event("e-b", "tenant-a", "shop-b"), headers=headers)

    assert canonical.json()["canonical_quote_status"] == "QUOTED"
    assert "canonical_quote_status" not in legacy.json()
    assert len(runtime.calls) == 1


def test_same_shop_id_under_other_tenant_does_not_inherit_canary(tmp_path: Path) -> None:
    store = ShopAutomationStore(tmp_path / "shops.json")
    store.sync("tenant-a", [{"accountUnb": "shop-a", "shopName": "A"}])
    store.sync("tenant-b", [{"accountUnb": "shop-a", "shopName": "A"}])
    store.set_canonical_enabled("tenant-a", "shop-a", True)
    assert store.is_canonical_quote_enabled("tenant-a", "shop-a") is True
    assert store.is_canonical_quote_enabled("tenant-b", "shop-a") is False


def test_global_gate_still_blocks_enabled_shop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test")
    monkeypatch.delenv("CANONICAL_QUOTE_RUNTIME_ENABLED", raising=False)
    shops = ShopAutomationStore(tmp_path / "shops.json")
    shops.sync("tenant-a", [{"accountUnb": "shop-a", "shopName": "A"}])
    shops.set_canonical_enabled("tenant-a", "shop-a", True)
    runtime = Runtime()
    client = TestClient(app(tmp_path, runtime))
    response = client.post(
        "/api/wanda-ai-v2/plugin/events/process", json=event("e-global-off", "tenant-a", "shop-a"),
        headers={"x-wanda-ai-v2-bridge-key": "bridge-test"},
    )
    assert "canonical_quote_status" not in response.json()
    assert runtime.calls == []


def test_canonical_failure_is_terminal_for_canary_event(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test")
    monkeypatch.setenv("CANONICAL_QUOTE_RUNTIME_ENABLED", "true")
    shops = ShopAutomationStore(tmp_path / "shops.json")
    shops.sync("tenant-a", [{"accountUnb": "shop-a", "shopName": "A"}])
    shops.set_canonical_enabled("tenant-a", "shop-a", True)
    runtime = Runtime("CANONICAL_QUOTE_UNAVAILABLE")
    client = TestClient(app(tmp_path, runtime))
    response = client.post(
        "/api/wanda-ai-v2/plugin/events/process", json=event("e-fail", "tenant-a", "shop-a"),
        headers={"x-wanda-ai-v2-bridge-key": "bridge-test"},
    )
    assert response.json()["canonical_quote_status"] == "CANONICAL_QUOTE_UNAVAILABLE"
    assert len(runtime.calls) == 1
