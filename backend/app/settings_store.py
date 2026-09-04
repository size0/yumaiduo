from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from dataclasses import dataclass
from typing import Any, Protocol

from Crypto.Cipher import AES

from .config import Settings
from .models import VisionSettingsUpdate, VisionSettingsView


class SecretProtector(Protocol):
    def protect(self, value: str) -> str: ...
    def unprotect(self, value: str) -> str: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class WindowsDpapiProtector:
    """Encrypt secrets for the current Windows user via Credential DPAPI."""

    _UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("persistent API keys require Windows DPAPI")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    @staticmethod
    def _input_blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(value)
        blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        return blob, buffer

    def protect(self, value: str) -> str:
        source, source_buffer = self._input_blob(value.encode("utf-8"))
        output = _DataBlob()
        if not self._crypt32.CryptProtectData(
            ctypes.byref(source),
            "wanda-vision-api-key",
            None,
            None,
            None,
            self._UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            encrypted = ctypes.string_at(output.pbData, output.cbData)
            return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
        finally:
            self._kernel32.LocalFree(output.pbData)

    def unprotect(self, value: str) -> str:
        if not value.startswith("dpapi:"):
            raise ValueError("unsupported protected secret format")
        encrypted = base64.b64decode(value.removeprefix("dpapi:"), validate=True)
        source, source_buffer = self._input_blob(encrypted)
        output = _DataBlob()
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            None,
            None,
            None,
            self._UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        finally:
            self._kernel32.LocalFree(output.pbData)


class AesGcmSecretProtector:
    """Encrypt persisted secrets on non-Windows hosts with an operator-managed key."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("WANDA_SETTINGS_ENCRYPTION_KEY must decode to exactly 32 bytes")
        self._key = key

    @classmethod
    def from_environment(cls) -> "AesGcmSecretProtector":
        encoded = os.getenv("WANDA_SETTINGS_ENCRYPTION_KEY", "").strip()
        if not encoded:
            raise RuntimeError("persistent API keys require WANDA_SETTINGS_ENCRYPTION_KEY on non-Windows hosts")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeError) as error:
            raise RuntimeError("WANDA_SETTINGS_ENCRYPTION_KEY must be valid Base64") from error
        return cls(key)

    def protect(self, value: str) -> str:
        cipher = AES.new(self._key, AES.MODE_GCM)
        ciphertext, tag = cipher.encrypt_and_digest(value.encode("utf-8"))
        payload = cipher.nonce + tag + ciphertext
        return "aesgcm:" + base64.b64encode(payload).decode("ascii")

    def unprotect(self, value: str) -> str:
        if not value.startswith("aesgcm:"):
            raise ValueError("unsupported protected secret format")
        payload = base64.b64decode(value.removeprefix("aesgcm:"), validate=True)
        if len(payload) < 32:
            raise ValueError("invalid protected secret payload")
        nonce, tag, ciphertext = payload[:16], payload[16:32], payload[32:]
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")


def default_secret_protector() -> SecretProtector:
    return WindowsDpapiProtector() if os.name == "nt" else AesGcmSecretProtector.from_environment()


@dataclass(frozen=True)
class ResolvedModelConfig:
    """One redacted model authority plus its private key for the HTTP client."""

    config_id: str
    revision: int
    scope: str
    tenant_id: str | None
    shop_id: str | None
    purpose: str
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    temperature: float
    max_tokens: int | None
    supported_capabilities: tuple[str, ...]

    def audit_view(self) -> dict[str, Any]:
        """Return metadata safe for audit/UI; deliberately omits the API key."""
        return {
            "config_id": self.config_id,
            "config_revision": self.revision,
            "scope": self.scope,
            "tenant_id": self.tenant_id,
            "shop_id": self.shop_id,
            "purpose": self.purpose,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "supported_capabilities": list(self.supported_capabilities),
        }


class PersistentSettingsStore:
    def __init__(
        self,
        path: Path,
        *,
        protector: SecretProtector | None = None,
        environment: Settings | None = None,
    ) -> None:
        self._path = path
        self._protector = protector or default_secret_protector()
        self._environment = environment or Settings.from_env()
        self._lock = RLock()

    def current(self) -> Settings:
        with self._lock:
            return self._settings_from_payload(self._read_payload(), None)

    def current_for_scope(self, tenant_id: str | None = None, shop_id: str | None = None) -> Settings:
        """Read the existing settings authority with optional scoped override."""
        tenant, shop = self._normalize_scope(tenant_id, shop_id)
        with self._lock:
            payload = self._read_payload()
            return self._settings_from_payload(payload, self._profile_for_scope(payload, tenant, shop))

    def resolve_model_config(
        self, tenant_id: str | None, shop_id: str | None,
        *, purpose: str = "conversation_agent",
    ) -> ResolvedModelConfig:
        """Resolve one model from shop > tenant > persisted global > environment."""
        tenant, shop = self._normalize_scope(tenant_id, shop_id)
        normalized_purpose = str(purpose or "conversation_agent").strip() or "conversation_agent"
        with self._lock:
            payload = self._read_payload()
            profile = self._profile_for_scope(payload, tenant, shop)
            if profile is not None:
                settings = self._settings_from_payload(payload, profile)
                scope = "SHOP" if shop and tenant and self._shop_profile(payload, tenant, shop) is profile else "TENANT"
                config_id = self._profile_id(profile, scope, tenant, shop)
                revision = self._profile_revision(profile)
            elif payload is not None and self._has_global_config(payload):
                settings = self._settings_from_payload(payload, None)
                scope, config_id, revision = "GLOBAL", self._global_config_id(payload), self._global_revision(payload)
            else:
                settings = self._environment
                scope, config_id, revision = "ENVIRONMENT", f"environment-{self._purpose_kind(normalized_purpose)}", 0
            use_vision = normalized_purpose in {"vision", "recognition", "manual_mark", "image"}
            return ResolvedModelConfig(
                config_id=config_id,
                revision=revision,
                scope=scope,
                tenant_id=tenant,
                shop_id=shop if scope == "SHOP" else None,
                purpose=normalized_purpose,
                provider="OpenAI-compatible",
                base_url=settings.base_url if use_vision else settings.chat_base_url,
                api_key=settings.api_key if use_vision else settings.chat_api_key,
                model=settings.model if use_vision else settings.chat_model,
                timeout_seconds=settings.request_timeout_seconds,
                temperature=0,
                max_tokens=None,
                supported_capabilities=("chat", "tools") if not use_vision else ("vision", "chat"),
            )

    def view(self, tenant_id: str | None = None, shop_id: str | None = None) -> VisionSettingsView:
        tenant, shop = self._normalize_scope(tenant_id, shop_id)
        with self._lock:
            payload = self._read_payload()
            profile = self._profile_for_scope(payload, tenant, shop)
            current = self._settings_from_payload(payload, profile)
            metadata = self._view_metadata(payload, profile, tenant, shop)
            return VisionSettingsView(
                base_url=current.base_url,
                model=current.model,
                chat_base_url=current.chat_base_url,
                chat_model=current.chat_model,
                enable_thinking=current.enable_thinking,
                reasoning_effort=current.reasoning_effort,
                vision_prompt=current.vision_prompt,
                chat_prompt=current.chat_prompt,
                has_api_key=bool(current.api_key),
                masked_api_key=f"***{current.api_key[-4:]}" if current.api_key else "",
                has_chat_api_key=bool(current.chat_api_key),
                masked_chat_api_key=f"***{current.chat_api_key[-4:]}" if current.chat_api_key else "",
                **metadata,
            )

    def save(
        self, update: VisionSettingsUpdate, *,
        tenant_id: str | None = None, shop_id: str | None = None,
    ) -> VisionSettingsView:
        tenant, shop = self._normalize_scope(tenant_id, shop_id)
        if shop and not tenant:
            raise ValueError("shop_scope_requires_tenant")
        with self._lock:
            payload = self._read_payload() or {"version": 5}
            profile = self._profile_for_scope(payload, tenant, shop)
            existing = self._settings_from_payload(payload, profile)
            api_key = "" if update.clear_api_key else (update.api_key if update.api_key is not None else existing.api_key)
            chat_api_key = "" if update.clear_chat_api_key else (
                update.chat_api_key if update.chat_api_key is not None else existing.chat_api_key
            )
            validated = Settings(
                api_key=api_key, base_url=update.base_url, model=update.model,
                chat_api_key=chat_api_key,
                chat_base_url=update.chat_base_url or existing.chat_base_url,
                chat_model=update.chat_model or existing.chat_model,
                enable_thinking=update.enable_thinking, reasoning_effort=update.reasoning_effort,
                vision_prompt=update.vision_prompt, chat_prompt=update.chat_prompt or existing.chat_prompt,
                max_image_bytes=existing.max_image_bytes, request_timeout_seconds=existing.request_timeout_seconds,
                wanda_account_pool_path=existing.wanda_account_pool_path,
                wanda_cinema_cache_path=existing.wanda_cinema_cache_path,
                wanda_fixed_account_phone=existing.wanda_fixed_account_phone,
                wanda_request_timeout_seconds=existing.wanda_request_timeout_seconds,
            )
            now = datetime.now(timezone.utc).isoformat()
            if shop or tenant:
                scope = "SHOP" if shop else "TENANT"
                old_revision = self._profile_revision(profile)
                new_profile = self._settings_payload(validated, revision=old_revision + 1, now=now)
                new_profile["config_id"] = self._profile_id(new_profile, scope, tenant, shop)
                new_profile["api_key"] = api_key
                new_profile["chat_api_key"] = chat_api_key
                self._store_profile(payload, tenant, shop, new_profile)
            else:
                old_revision = self._global_revision(payload)
                payload.update(self._settings_payload(validated, revision=old_revision + 1, now=now))
                payload["api_key_protected"] = self._protector.protect(api_key) if api_key else ""
                payload["chat_api_key_protected"] = self._protector.protect(chat_api_key) if chat_api_key else ""
                payload["version"] = max(int(payload.get("version") or 0), 6)
                payload["config_id"] = self._global_config_id(payload)
            self._write_payload(payload)
            return self.view(tenant, shop)

    def _settings_from_payload(self, payload: dict[str, object] | None, profile: dict[str, object] | None) -> Settings:
        if profile is None and (payload is None or not self._has_global_config(payload)):
            return self._environment
        source = profile or payload or {}
        environment = self._environment
        api_key = self._decrypt_key(source, "api_key_protected", environment.api_key if profile is None and payload is None else "")
        chat_fallback = (
            environment.chat_api_key if profile is None and payload is None
            else api_key if "chat_api_key_protected" not in source and "api_key_protected" in source
            else ""
        )
        chat_api_key = self._decrypt_key(source, "chat_api_key_protected", chat_fallback)
        return Settings.model_validate({
            **environment.model_dump(),
            "api_key": api_key,
            "base_url": source.get("base_url", environment.base_url),
            "model": source.get("model", environment.model),
            "chat_api_key": chat_api_key,
            "chat_base_url": source.get("chat_base_url", source.get("base_url", environment.chat_base_url)),
            "chat_model": source.get("chat_model", source.get("model", environment.chat_model)),
            "enable_thinking": source.get("enable_thinking", environment.enable_thinking),
            "reasoning_effort": source.get("reasoning_effort", environment.reasoning_effort),
            "vision_prompt": source.get("vision_prompt", environment.vision_prompt),
            "chat_prompt": source.get("chat_prompt", environment.chat_prompt),
        })

    def _decrypt_key(self, source: dict[str, object], field: str, fallback: str) -> str:
        if field not in source:
            return fallback
        protected = source.get(field)
        if not isinstance(protected, str) or not protected:
            return ""
        try:
            return self._protector.unprotect(protected)
        except (OSError, ValueError, UnicodeError):
            return fallback

    @staticmethod
    def _normalize_scope(tenant_id: str | None, shop_id: str | None) -> tuple[str | None, str | None]:
        tenant = str(tenant_id or "").strip() or None
        shop = str(shop_id or "").strip() or None
        if tenant and len(tenant) > 160:
            raise ValueError("tenant_scope_invalid")
        if shop and len(shop) > 200:
            raise ValueError("shop_scope_invalid")
        return tenant, shop

    @staticmethod
    def _purpose_kind(purpose: str) -> str:
        return "vision" if purpose in {"vision", "recognition", "manual_mark", "image"} else "chat"

    @staticmethod
    def _has_global_config(payload: dict[str, object] | None) -> bool:
        return bool(payload) and any(
            key in payload for key in ("base_url", "model", "chat_base_url", "chat_model")
        )

    @staticmethod
    def _global_revision(payload: dict[str, object]) -> int:
        value = payload.get("revision")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return 1 if any(key in payload for key in ("base_url", "model", "chat_base_url", "chat_model")) else 0

    @staticmethod
    def _profile_revision(profile: dict[str, object] | None) -> int:
        if not profile:
            return 0
        value = profile.get("revision")
        return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @staticmethod
    def _global_config_id(payload: dict[str, object]) -> str:
        value = payload.get("config_id")
        return str(value) if isinstance(value, str) and value.strip() else "global-chat"

    @staticmethod
    def _profile_id(profile: dict[str, object], scope: str, tenant: str | None, shop: str | None) -> str:
        value = profile.get("config_id")
        if isinstance(value, str) and value.strip():
            return value
        identity = f"{scope}:{tenant or ''}:{shop or ''}"
        return "model-config-" + hashlib.sha256(identity.encode()).hexdigest()[:16]

    @staticmethod
    def _settings_payload(settings: Settings, *, revision: int, now: str) -> dict[str, object]:
        return {
            "base_url": settings.base_url, "model": settings.model,
            "chat_base_url": settings.chat_base_url, "chat_model": settings.chat_model,
            "enable_thinking": settings.enable_thinking, "reasoning_effort": settings.reasoning_effort,
            "vision_prompt": settings.vision_prompt, "chat_prompt": settings.chat_prompt,
            "api_key_protected": "", "chat_api_key_protected": "",
            "revision": revision, "updated_at": now,
        }

    def _store_profile(self, payload: dict[str, object], tenant: str, shop: str | None, profile: dict[str, object]) -> None:
        configs = payload.setdefault("model_configs", {})
        if not isinstance(configs, dict):
            configs = {}
            payload["model_configs"] = configs
        if shop:
            shops = configs.setdefault("shops", {})
            if not isinstance(shops, dict):
                shops = {}
                configs["shops"] = shops
            tenant_shops = shops.setdefault(tenant, {})
            if not isinstance(tenant_shops, dict):
                tenant_shops = {}
                shops[tenant] = tenant_shops
            tenant_shops[shop] = profile
        else:
            tenants = configs.setdefault("tenants", {})
            if not isinstance(tenants, dict):
                tenants = {}
                configs["tenants"] = tenants
            tenants[tenant] = profile
        # Protect only after constructing the profile so plaintext never enters the payload.
        api_key = profile.pop("api_key", "")
        chat_api_key = profile.pop("chat_api_key", "")
        profile["api_key_protected"] = self._protector.protect(api_key) if api_key else ""
        profile["chat_api_key_protected"] = self._protector.protect(chat_api_key) if chat_api_key else ""

    def _profile_for_scope(self, payload: dict[str, object] | None, tenant: str | None, shop: str | None) -> dict[str, object] | None:
        if not payload or not tenant:
            return None
        configs = payload.get("model_configs")
        if not isinstance(configs, dict):
            return None
        if shop:
            profile = self._shop_profile(payload, tenant, shop)
            if profile is not None:
                return profile
        tenants = configs.get("tenants")
        profile = tenants.get(tenant) if isinstance(tenants, dict) else None
        return profile if isinstance(profile, dict) else None

    @staticmethod
    def _shop_profile(payload: dict[str, object] | None, tenant: str, shop: str) -> dict[str, object] | None:
        configs = payload.get("model_configs") if isinstance(payload, dict) else None
        shops = configs.get("shops") if isinstance(configs, dict) else None
        tenant_shops = shops.get(tenant) if isinstance(shops, dict) else None
        profile = tenant_shops.get(shop) if isinstance(tenant_shops, dict) else None
        return profile if isinstance(profile, dict) else None

    def _view_metadata(self, payload: dict[str, object] | None, profile: dict[str, object] | None, tenant: str | None, shop: str | None) -> dict[str, object]:
        if profile is not None:
            is_shop = shop is not None and self._shop_profile(payload, tenant or "", shop) is profile
            scope = "SHOP" if is_shop else "TENANT"
            config_id = self._profile_id(profile, scope, tenant, shop if is_shop else None)
            revision = self._profile_revision(profile)
            updated_at = profile.get("updated_at")
        elif payload is not None and self._has_global_config(payload):
            scope, config_id, revision, updated_at = "GLOBAL", self._global_config_id(payload), self._global_revision(payload), payload.get("updated_at")
        else:
            scope, config_id, revision, updated_at = "ENVIRONMENT", "environment-chat", 0, None
        return {
            "provider": "OpenAI-compatible", "config_id": config_id, "config_revision": revision,
            "scope": scope, "scope_tenant_id": tenant, "scope_shop_id": shop if scope == "SHOP" else None,
            "supported_capabilities": ["vision", "chat", "tools"],
            "updated_at": updated_at if isinstance(updated_at, str) else None,
        }

    def _read_payload(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_payload(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self._path)
