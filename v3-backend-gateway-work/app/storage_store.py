from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from .schemas import StorageSettingsView


DEFAULT_STORAGE_SETTINGS = {
    "bucket_url": "",
    "region": "",
    "secret_id": "",
    "secret_key": "",
    "updated_at": None,
}


class CosSettingsStore:
    """COS 凭据仅供后端 SDK 使用，绝不由 API 回传。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def read(self) -> dict[str, object]:
        with self._lock:
            if not self._path.exists():
                return DEFAULT_STORAGE_SETTINGS.copy()
            loaded = json.loads(self._path.read_text(encoding="utf-8-sig"))
            return {**DEFAULT_STORAGE_SETTINGS, **loaded}

    def view(self) -> StorageSettingsView:
        data = self.read()
        updated_at = data["updated_at"]
        return StorageSettingsView(
            bucket_url=str(data["bucket_url"]),
            region=str(data["region"]),
            has_secret_id=bool(data["secret_id"]),
            has_secret_key=bool(data["secret_key"]),
            updated_at=datetime.fromisoformat(updated_at) if updated_at else None,
        )
