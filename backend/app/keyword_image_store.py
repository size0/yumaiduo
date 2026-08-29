from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from threading import RLock
from typing import Any

from .settings_store import SecretProtector, default_secret_protector


MAX_KEYWORD_IMAGE_BYTES = 5 * 1024 * 1024
_ALLOWED_MIME_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}
_ASSET_ID = re.compile(r"^ki-[0-9a-f]{40}$")


def _detected_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


class KeywordImageStore:
    """Encrypted tenant-scoped storage for fixed keyword-reply images."""

    def __init__(self, root: Path, *, protector: SecretProtector | None = None) -> None:
        self._root = root
        self._protector = protector or default_secret_protector()
        self._lock = RLock()

    def save(self, tenant_id: str, data: bytes, content_type: str, filename: str = "") -> dict[str, Any]:
        tenant = self._tenant(tenant_id)
        if not data or len(data) > MAX_KEYWORD_IMAGE_BYTES:
            raise ValueError("keyword_image_size_invalid")
        detected = _detected_mime(data)
        claimed = str(content_type or "").split(";", 1)[0].strip().lower()
        if detected is None or claimed not in _ALLOWED_MIME_TYPES or claimed != detected:
            raise ValueError("keyword_image_type_invalid")
        digest = hashlib.sha256(data).hexdigest()
        asset_id = f"ki-{hashlib.sha256(f'{tenant}:{digest}'.encode()).hexdigest()[:40]}"
        extension = _ALLOWED_MIME_TYPES[detected]
        safe_filename = f"keyword-{digest[:12]}.{extension}"
        payload = {
            "version": 1,
            "asset_id": asset_id,
            "tenant_id": tenant,
            "content_type": detected,
            "filename": safe_filename,
            "source_filename": Path(filename or "").name[:120],
            "size": len(data),
            "sha256": digest,
            "data_protected": self._protector.protect(base64.b64encode(data).decode("ascii")),
        }
        with self._lock:
            path = self._path(tenant, asset_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(path)
            os.chmod(path, 0o600)
        return self._metadata(payload)

    def get(self, tenant_id: str, asset_id: str) -> dict[str, Any]:
        tenant = self._tenant(tenant_id)
        asset = self._asset(asset_id)
        with self._lock:
            try:
                payload = json.loads(self._path(tenant, asset).read_text(encoding="utf-8"))
                if payload.get("tenant_id") != tenant or payload.get("asset_id") != asset:
                    raise ValueError("keyword_image_binding_invalid")
                data = base64.b64decode(
                    self._protector.unprotect(str(payload["data_protected"])), validate=True,
                )
            except FileNotFoundError:
                raise KeyError("keyword_image_not_found") from None
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                raise ValueError("keyword_image_storage_invalid") from None
        if hashlib.sha256(data).hexdigest() != payload.get("sha256") or _detected_mime(data) != payload.get("content_type"):
            raise ValueError("keyword_image_integrity_invalid")
        return {**self._metadata(payload), "data": data}

    def exists(self, tenant_id: str, asset_id: str) -> bool:
        try:
            self.get(tenant_id, asset_id)
        except (KeyError, ValueError):
            return False
        return True

    def _path(self, tenant: str, asset_id: str) -> Path:
        tenant_key = hashlib.sha256(tenant.encode()).hexdigest()[:24]
        return self._root / tenant_key / f"{asset_id}.json"

    @staticmethod
    def _tenant(value: str) -> str:
        tenant = str(value or "").strip()
        if not tenant or len(tenant) > 100:
            raise ValueError("keyword_image_tenant_invalid")
        return tenant

    @staticmethod
    def _asset(value: str) -> str:
        asset = str(value or "").strip()
        if not _ASSET_ID.fullmatch(asset):
            raise ValueError("keyword_image_asset_id_invalid")
        return asset

    @staticmethod
    def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "asset_id": payload["asset_id"],
            "content_type": payload["content_type"],
            "filename": payload["filename"],
            "size": payload["size"],
            "sha256": payload["sha256"],
        }
