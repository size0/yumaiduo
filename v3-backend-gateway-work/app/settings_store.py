from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from .schemas import ModelSettingsUpdate, ModelSettingsView


DEFAULT_SETTINGS = {
    "base_url": "https://api.openai.com",
    "model": "",
    "api_key": "",
    "temperature": 0,
    "max_tokens": 1200,
    "updated_at": None,
}


class ModelSettingsStore:
    """本地单机配置存储。API 密钥不出现在任何读取接口或日志里。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def read(self) -> dict[str, object]:
        with self._lock:
            if not self._path.exists():
                return DEFAULT_SETTINGS.copy()
            loaded = json.loads(self._path.read_text(encoding="utf-8-sig"))
            return {**DEFAULT_SETTINGS, **loaded}

    def view(self) -> ModelSettingsView:
        data = self.read()
        updated_at = data["updated_at"]
        return ModelSettingsView(
            base_url=self._display_base_url(str(data["base_url"])),
            model=str(data["model"]),
            temperature=float(data["temperature"]),
            max_tokens=int(data["max_tokens"]),
            has_api_key=bool(data["api_key"]),
            updated_at=datetime.fromisoformat(updated_at) if updated_at else None,
        )

    def save(self, update: ModelSettingsUpdate) -> ModelSettingsView:
        with self._lock:
            current = self.read_unlocked()
            api_key = update.api_key if update.api_key is not None else current["api_key"]
            next_value = {
                "base_url": self._display_base_url(str(update.base_url)),
                "model": update.model.strip(),
                "api_key": api_key,
                "temperature": update.temperature,
                "max_tokens": update.max_tokens,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self._path.with_suffix(".tmp")
            temporary_path.write_text(json.dumps(next_value, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary_path, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
            return ModelSettingsView(
                base_url=str(next_value["base_url"]),
                model=str(next_value["model"]),
                temperature=float(next_value["temperature"]),
                max_tokens=int(next_value["max_tokens"]),
                has_api_key=bool(next_value["api_key"]),
                updated_at=datetime.fromisoformat(str(next_value["updated_at"])),
            )

    def clear_api_key(self) -> ModelSettingsView:
        with self._lock:
            next_value = self.read_unlocked()
            next_value["api_key"] = ""
            next_value["updated_at"] = datetime.now(UTC).isoformat()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self._path.with_suffix(".tmp")
            temporary_path.write_text(json.dumps(next_value, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary_path, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
            return ModelSettingsView(
                base_url=str(next_value["base_url"]),
                model=str(next_value["model"]),
                temperature=float(next_value["temperature"]),
                max_tokens=int(next_value["max_tokens"]),
                has_api_key=False,
                updated_at=datetime.fromisoformat(str(next_value["updated_at"])),
            )

    def read_unlocked(self) -> dict[str, object]:
        if not self._path.exists():
            return DEFAULT_SETTINGS.copy()
        loaded = json.loads(self._path.read_text(encoding="utf-8-sig"))
        return {**DEFAULT_SETTINGS, **loaded}

    @staticmethod
    def _display_base_url(value: str) -> str:
        base_url = value.rstrip("/")
        return base_url[:-3] if base_url.endswith("/v1") else base_url
