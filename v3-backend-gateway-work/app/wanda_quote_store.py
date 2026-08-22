from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from .schemas import WandaQuoteSettingsUpdate, WandaQuoteSettingsView


DEFAULT_SETTINGS = {
    "account_phone": "",
    "updated_at": None,
}


class WandaQuoteSettingsStore:
    """Local V3 settings. Wanda access tokens remain in the ticket gateway only."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def read(self) -> dict[str, object]:
        with self._lock:
            return self._read_unlocked()

    def view(self) -> WandaQuoteSettingsView:
        data = self.read()
        updated_at = data["updated_at"]
        return WandaQuoteSettingsView(
            account_phone=str(data["account_phone"]),
            gateway_configured=bool(os.getenv("WANDA_QUOTE_GATEWAY_URL", "http://127.0.0.1:8000").strip()),
            account_configured=bool(data["account_phone"]),
            updated_at=datetime.fromisoformat(str(updated_at)) if updated_at else None,
        )

    def save(self, update: WandaQuoteSettingsUpdate) -> WandaQuoteSettingsView:
        with self._lock:
            next_value = {
                "account_phone": update.account_phone.strip(),
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
        return self.view()

    def _read_unlocked(self) -> dict[str, object]:
        if not self._path.exists():
            return DEFAULT_SETTINGS.copy()
        loaded = json.loads(self._path.read_text(encoding="utf-8-sig"))
        return {**DEFAULT_SETTINGS, **loaded}
