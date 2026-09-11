from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4


class ShopAutomationStore:
    """Atomic tenant-scoped shop switches; no credentials or conversation data are stored."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()

    @staticmethod
    def _text(value: object, *, maximum: int) -> str | None:
        normalized = str(value or "").strip()
        return normalized[:maximum] if normalized else None

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": 1, "tenants": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "tenants": {}}
        if data.get("version") != 1 or not isinstance(data.get("tenants"), dict):
            return {"version": 1, "tenants": {}}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def sync(self, tenant_id: str, shops: list[object]) -> int:
        tenant = self._text(tenant_id, maximum=160)
        if not tenant:
            return 0
        with self._lock:
            data = self._read()
            tenant_data = data["tenants"].setdefault(tenant, {"shops": {}})
            stored = tenant_data.setdefault("shops", {})
            accepted = 0
            for item in shops[:500]:
                if not isinstance(item, dict):
                    continue
                shop_id = self._text(
                    item.get("accountUnb", item.get("account_unb", item.get("shopId", item.get("unb", item.get("id"))))),
                    maximum=200,
                )
                if not shop_id:
                    continue
                shop_name = self._text(
                    item.get("shopName", item.get("shop_name", item.get("name", item.get("nick")))),
                    maximum=200,
                ) or shop_id
                previous = stored.get(shop_id) if isinstance(stored.get(shop_id), dict) else {}
                stored[shop_id] = {
                    "shop_name": shop_name,
                    "enabled": previous.get("enabled", True) is True,
                    # Canonical quoting is an independent, opt-in canary.  Old
                    # shop records therefore remain automation-compatible while
                    # defaulting the new path to OFF.
                    "canonical_quote_enabled": previous.get("canonical_quote_enabled", False) is True,
                    "canonical_conversation_enabled": previous.get("canonical_conversation_enabled", False) is True,
                }
                accepted += 1
            self._write(data)
            return accepted

    def list_shops(self, tenant_id: str, *, include_canonical: bool = False) -> list[dict[str, object]]:
        tenant = self._text(tenant_id, maximum=160)
        if not tenant:
            return []
        with self._lock:
            shops = self._read().get("tenants", {}).get(tenant, {}).get("shops", {})
            if not isinstance(shops, dict):
                return []
            result: list[dict[str, object]] = []
            for shop_id, value in shops.items():
                if not isinstance(value, dict):
                    continue
                item: dict[str, object] = {
                    "shop_id": shop_id,
                    "shop_name": str(value.get("shop_name") or shop_id),
                    "enabled": value.get("enabled") is True,
                }
                if include_canonical:
                    item["canonical_quote_enabled"] = value.get("canonical_quote_enabled") is True
                    item["canonical_conversation_enabled"] = value.get("canonical_conversation_enabled") is True
                result.append(item)
            return sorted(
                result,
                key=lambda item: (str(item["shop_name"]), str(item["shop_id"])),
            )

    def is_enabled(self, tenant_id: str, shop_id: str) -> bool:
        return any(item["shop_id"] == shop_id and item["enabled"] is True for item in self.list_shops(tenant_id))

    def is_canonical_quote_enabled(self, tenant_id: str, shop_id: str) -> bool:
        tenant = self._text(tenant_id, maximum=160)
        shop = self._text(shop_id, maximum=200)
        if not tenant or not shop:
            return False
        with self._lock:
            shops = self._read().get("tenants", {}).get(tenant, {}).get("shops", {})
            value = shops.get(shop) if isinstance(shops, dict) else None
            return isinstance(value, dict) and value.get("canonical_quote_enabled") is True

    def is_canonical_conversation_enabled(self, tenant_id: str, shop_id: str) -> bool:
        tenant = self._text(tenant_id, maximum=160)
        shop = self._text(shop_id, maximum=200)
        if not tenant or not shop:
            return False
        with self._lock:
            shops = self._read().get("tenants", {}).get(tenant, {}).get("shops", {})
            value = shops.get(shop) if isinstance(shops, dict) else None
            return isinstance(value, dict) and value.get("canonical_conversation_enabled") is True

    def set_settings(
        self,
        tenant_id: str,
        shop_id: str,
        *,
        enabled: bool | None = None,
        canonical_quote_enabled: bool | None = None,
        canonical_conversation_enabled: bool | None = None,
    ) -> dict[str, object]:
        tenant = self._text(tenant_id, maximum=160)
        shop = self._text(shop_id, maximum=200)
        if (
            not tenant or not shop
            or (enabled is not None and not isinstance(enabled, bool))
            or (
                canonical_quote_enabled is not None
                and not isinstance(canonical_quote_enabled, bool)
            )
            or (
                canonical_conversation_enabled is not None
                and not isinstance(canonical_conversation_enabled, bool)
            )
        ):
            raise KeyError("shop_not_found")
        with self._lock:
            data = self._read()
            shops = data.get("tenants", {}).get(tenant, {}).get("shops", {})
            value = shops.get(shop) if isinstance(shops, dict) else None
            if not isinstance(value, dict):
                raise KeyError("shop_not_found")
            if enabled is not None:
                value["enabled"] = enabled
            if canonical_quote_enabled is not None:
                value["canonical_quote_enabled"] = canonical_quote_enabled
            if canonical_conversation_enabled is not None:
                value["canonical_conversation_enabled"] = canonical_conversation_enabled
            self._write(data)
            result: dict[str, object] = {
                "shop_id": shop,
                "shop_name": str(value.get("shop_name") or shop),
                "enabled": value.get("enabled") is True,
            }
            if canonical_quote_enabled is not None:
                result["canonical_quote_enabled"] = value.get("canonical_quote_enabled") is True
            if canonical_conversation_enabled is not None:
                result["canonical_conversation_enabled"] = value.get("canonical_conversation_enabled") is True
            return result

    def set_canonical_enabled(self, tenant_id: str, shop_id: str, enabled: bool) -> dict[str, object]:
        return self.set_settings(
            tenant_id, shop_id, canonical_quote_enabled=enabled,
        )

    def set_canonical_conversation_enabled(self, tenant_id: str, shop_id: str, enabled: bool) -> dict[str, object]:
        return self.set_settings(
            tenant_id, shop_id, canonical_conversation_enabled=enabled,
        )

    def set_enabled(self, tenant_id: str, shop_id: str, enabled: bool) -> dict[str, object]:
        return self.set_settings(tenant_id, shop_id, enabled=enabled)
