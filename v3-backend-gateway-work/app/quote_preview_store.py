from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .schemas import PendingQuoteRecord


class QuotePreviewStore:
    """Persist only sanitized quote-preview records; this store has no send state."""

    _retention = timedelta(hours=24)
    _maximum_items = 500

    def __init__(self, path: Path, *, now=None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._now = now or (lambda: datetime.now(UTC))

    def claim(self, event_id: str, tenant_id: str, record: PendingQuoteRecord) -> tuple[dict[str, Any], bool]:
        event_digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        with self._lock:
            data = self._read_unlocked()
            for item in data["items"]:
                if item.get("event_digest") == event_digest:
                    return item, False
            item = {
                "event_digest": event_digest,
                "tenant_id": tenant_id,
                "ingest_status": "failed",
                "record": record.model_dump(mode="json"),
                "recognition": None,
                "quote": None,
            }
            data["items"].append(item)
            self._write_unlocked(data)
            return item, True

    def complete(
        self,
        event_digest: str,
        *,
        ingest_status: str,
        record: PendingQuoteRecord,
        recognition: dict[str, Any] | None = None,
        quote: dict[str, Any] | None = None,
        failure_stage: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        with self._lock:
            data = self._read_unlocked()
            for item in data["items"]:
                if item.get("event_digest") != event_digest:
                    continue
                item["ingest_status"] = ingest_status
                item["record"] = record.model_dump(mode="json")
                item["recognition"] = recognition
                item["quote"] = quote
                item["failure_stage"] = failure_stage
                item["failure_code"] = failure_code
                self._write_unlocked(data)
                return
            raise KeyError("quote preview event was not claimed")

    def pending(self, tenant_id: str | None = None) -> list[PendingQuoteRecord]:
        with self._lock:
            data = self._read_unlocked()
        records: list[PendingQuoteRecord] = []
        for item in data["items"]:
            if tenant_id is not None and item.get("tenant_id") != tenant_id:
                continue
            raw_record = item.get("record")
            if not isinstance(raw_record, dict):
                continue
            record = PendingQuoteRecord.model_validate(raw_record)
            # A message without a ticket image is persisted for audit only.
            # It is not a quote task and must not occupy the human quote queue.
            if item.get("ingest_status") == "preview_ready" and record.status == "UNSENT_PREVIEW":
                records.append(record)
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    @staticmethod
    def event_digest(event_id: str) -> str:
        return hashlib.sha256(event_id.encode("utf-8")).hexdigest()

    @staticmethod
    def masked_buyer_label(value: str) -> str:
        label = " ".join(value.split())
        if len(label) <= 1:
            return "*"
        return f"{label[:1]}***{label[-1:]}"

    @staticmethod
    def message_summary(value: str) -> str:
        summary = " ".join(value.split())
        summary = re.sub(r"(?<!\d)1\d{10}(?!\d)", "[phone]", summary)
        summary = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "[email]", summary)
        return summary[:160]

    def _read_unlocked(self) -> dict[str, list[dict[str, Any]]]:
        if not self._path.exists():
            return {"items": []}
        loaded = json.loads(self._path.read_text(encoding="utf-8-sig"))
        items = loaded.get("items") if isinstance(loaded, dict) else None
        if not isinstance(items, list):
            return {"items": []}
        cutoff = self._now() - self._retention
        retained = [item for item in items if _record_created_at(item) >= cutoff]
        retained.sort(key=_record_created_at)
        return {"items": retained[-self._maximum_items:]}

    def _write_unlocked(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary_path, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass


def _record_created_at(item: Any) -> datetime:
    raw = item.get("record", {}).get("created_at") if isinstance(item, dict) else None
    if not isinstance(raw, str):
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def empty_pending_record(*, event_id: str, buyer_label: str, message_summary: str) -> PendingQuoteRecord:
    return PendingQuoteRecord(
        id=QuotePreviewStore.event_digest(event_id),
        buyer_label=buyer_label,
        message_summary=message_summary,
        status="UNSENT_PREVIEW",
        created_at=datetime.now(UTC),
    )
