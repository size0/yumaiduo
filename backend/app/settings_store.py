from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Protocol

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
            payload = self._read_payload()
            if payload is None:
                return self._environment
            has_persisted_key_setting = "api_key_protected" in payload
            api_key = "" if has_persisted_key_setting else self._environment.api_key
            protected_key = payload.get("api_key_protected")
            if isinstance(protected_key, str) and protected_key:
                try:
                    api_key = self._protector.unprotect(protected_key)
                except (OSError, ValueError, UnicodeError):
                    api_key = self._environment.api_key
            has_persisted_chat_key = "chat_api_key_protected" in payload
            chat_api_key = "" if has_persisted_chat_key else api_key
            protected_chat_key = payload.get("chat_api_key_protected")
            if isinstance(protected_chat_key, str) and protected_chat_key:
                try:
                    chat_api_key = self._protector.unprotect(protected_chat_key)
                except (OSError, ValueError, UnicodeError):
                    chat_api_key = self._environment.chat_api_key
            return Settings.model_validate({
                **self._environment.model_dump(),
                "api_key": api_key,
                "base_url": payload.get("base_url", self._environment.base_url),
                "model": payload.get("model", self._environment.model),
                "chat_api_key": chat_api_key,
                "chat_base_url": payload.get("chat_base_url", payload.get("base_url", self._environment.chat_base_url)),
                "chat_model": payload.get("chat_model", payload.get("model", self._environment.chat_model)),
                "enable_thinking": payload.get("enable_thinking", self._environment.enable_thinking),
                "reasoning_effort": payload.get("reasoning_effort", self._environment.reasoning_effort),
                "vision_prompt": payload.get("vision_prompt", self._environment.vision_prompt),
                "chat_prompt": payload.get("chat_prompt", self._environment.chat_prompt),
            })

    def view(self) -> VisionSettingsView:
        current = self.current()
        payload = self._read_payload() or {}
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
            updated_at=payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else None,
        )

    def save(self, update: VisionSettingsUpdate) -> VisionSettingsView:
        with self._lock:
            existing = self.current()
            api_key = "" if update.clear_api_key else (update.api_key if update.api_key is not None else existing.api_key)
            chat_api_key = (
                "" if update.clear_chat_api_key
                else (update.chat_api_key if update.chat_api_key is not None else existing.chat_api_key)
            )
            validated = Settings(
                api_key=api_key,
                base_url=update.base_url,
                model=update.model,
                chat_api_key=chat_api_key,
                chat_base_url=update.chat_base_url or existing.chat_base_url,
                chat_model=update.chat_model or existing.chat_model,
                enable_thinking=update.enable_thinking,
                reasoning_effort=update.reasoning_effort,
                vision_prompt=update.vision_prompt,
                chat_prompt=update.chat_prompt or existing.chat_prompt,
                max_image_bytes=existing.max_image_bytes,
                request_timeout_seconds=existing.request_timeout_seconds,
                wanda_account_pool_path=existing.wanda_account_pool_path,
                wanda_cinema_cache_path=existing.wanda_cinema_cache_path,
                wanda_fixed_account_phone=existing.wanda_fixed_account_phone,
                wanda_request_timeout_seconds=existing.wanda_request_timeout_seconds,
            )
            payload = {
                "version": 5,
                "base_url": validated.base_url,
                "model": validated.model,
                "chat_base_url": validated.chat_base_url,
                "chat_model": validated.chat_model,
                "enable_thinking": validated.enable_thinking,
                "reasoning_effort": validated.reasoning_effort,
                "vision_prompt": validated.vision_prompt,
                "chat_prompt": validated.chat_prompt,
                "api_key_protected": self._protector.protect(api_key) if api_key else "",
                "chat_api_key_protected": self._protector.protect(chat_api_key) if chat_api_key else "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_payload(payload)
            return self.view()

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
