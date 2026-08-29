from __future__ import annotations

from pathlib import Path

import pytest

from app.shop_automation_store import ShopAutomationStore


def test_sync_creates_tenant_isolated_enabled_shops_and_preserves_switch(tmp_path: Path) -> None:
    path = tmp_path / "shops.json"
    store = ShopAutomationStore(path)

    store.sync("tenant-a", [{"accountUnb": "shop-1", "shopName": "一号店"}])
    store.set_enabled("tenant-a", "shop-1", False)
    store.sync("tenant-a", [{"accountUnb": "shop-1", "shopName": "一号店新名称"}, {"accountUnb": "shop-2", "shopName": "二号店"}])
    store.sync("tenant-b", [{"accountUnb": "shop-1", "shopName": "其他租户店"}])

    assert store.list_shops("tenant-a") == [
        {"shop_id": "shop-1", "shop_name": "一号店新名称", "enabled": False},
        {"shop_id": "shop-2", "shop_name": "二号店", "enabled": True},
    ]
    assert store.is_enabled("tenant-a", "shop-1") is False
    assert store.is_enabled("tenant-b", "shop-1") is True
    assert ShopAutomationStore(path).is_enabled("tenant-a", "shop-1") is False


def test_unknown_shop_fails_closed(tmp_path: Path) -> None:
    store = ShopAutomationStore(tmp_path / "shops.json")

    assert store.is_enabled("tenant-a", "missing") is False
    with pytest.raises(KeyError):
        store.set_enabled("tenant-a", "missing", True)


def test_sync_ignores_malformed_shop_records(tmp_path: Path) -> None:
    store = ShopAutomationStore(tmp_path / "shops.json")

    accepted = store.sync("tenant-a", [None, {}, {"name": "missing id"}, {"unb": "shop-1", "shopName": "可用店铺"}])

    assert accepted == 1
    assert store.list_shops("tenant-a") == [{"shop_id": "shop-1", "shop_name": "可用店铺", "enabled": True}]
