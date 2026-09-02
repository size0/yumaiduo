from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .models import MovieImageInfo
from .settings_store import SecretProtector, default_secret_protector


_SCHEMA_VERSION = 1


class RecognitionSnapshotError(RuntimeError):
    """Base error for durable recognition snapshots."""


class RecognitionSnapshotConflict(RecognitionSnapshotError):
    """The requested mutation conflicts with the durable snapshot state."""


class RecognitionSnapshotAccessDenied(RecognitionSnapshotError):
    """A snapshot exists but does not belong to the supplied identity."""


class RecognitionSnapshotPayloadTooLarge(RecognitionSnapshotError):
    def __init__(self, *, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"recognition_snapshot_payload_too_large: actual={actual_bytes}, max={max_bytes}"
        )


class RecognitionSnapshotConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=200)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class RecognitionSnapshot(BaseModel):
    """Public, decrypted view of one provider recognition observation."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1, max_length=100)
    tenant_id: str = Field(min_length=1, max_length=200)
    shop_id: str = Field(min_length=1, max_length=200)
    buyer_id: str = Field(min_length=1, max_length=200)
    chat_id: str = Field(min_length=1, max_length=200)
    event_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=160)
    recognize_id: str | None = Field(default=None, max_length=160)
    provider_request_id: str | None = Field(default=None, max_length=200)
    trace_id: str | None = Field(default=None, max_length=160)
    raw_results: dict[str, Any] = Field(default_factory=dict)
    final_results: dict[str, Any] = Field(default_factory=dict)
    raw_response: dict[str, Any] = Field(default_factory=dict)
    normalized: MovieImageInfo
    revision: int = Field(ge=1)
    confirmation_history: list[RecognitionSnapshotConfirmation] = Field(default_factory=list)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    is_current: bool = True


class RecognitionSnapshotStore:
    """Encrypted SQLite store scoped by identity and recognition target.

    The raw provider response is retained inside one protected JSON payload.
    Search and ownership columns stay normalized so the runtime can select a
    target without decrypting every snapshot. Mutations use revision CAS and
    event IDs to make retries safe.
    """

    def __init__(
        self,
        path: Path,
        *,
        protector: SecretProtector | None = None,
        ttl_seconds: int = 24 * 60 * 60,
        max_payload_bytes: int = 2 * 1024 * 1024,
        max_confirmation_history: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("recognition_snapshot_ttl_invalid")
        if max_payload_bytes < 1:
            raise ValueError("recognition_snapshot_max_payload_bytes_invalid")
        if max_confirmation_history < 1:
            raise ValueError("recognition_snapshot_confirmation_history_limit_invalid")
        self._path = Path(path)
        self._protector = protector or default_secret_protector()
        self._ttl_seconds = ttl_seconds
        self._max_payload_bytes = max_payload_bytes
        self._max_confirmation_history = max_confirmation_history
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS recognition_snapshot_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recognition_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    buyer_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    recognize_id TEXT,
                    provider_request_id TEXT,
                    trace_id TEXT,
                    revision INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    source_payload_hash TEXT NOT NULL,
                    payload_protected TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(tenant_id, shop_id, buyer_id, chat_id, target_id, event_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS recognition_snapshots_current_slot_unique
                    ON recognition_snapshots(tenant_id, shop_id, buyer_id, chat_id, target_id)
                    WHERE is_current = 1;
                CREATE INDEX IF NOT EXISTS recognition_snapshots_expiry_idx
                    ON recognition_snapshots(is_current, expires_at);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO recognition_snapshot_schema(version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, self._now().isoformat()),
            )

    def create(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        event_id: str,
        target_id: str,
        recognition: MovieImageInfo,
        raw_results: Mapping[str, Any] | None = None,
        final_results: Mapping[str, Any] | None = None,
        raw_response: Mapping[str, Any] | None = None,
        recognize_id: str | None = None,
        provider_request_id: str | None = None,
        trace_id: str | None = None,
    ) -> RecognitionSnapshot:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        event = self._required_text(event_id, "event_id", 200)
        target = self._required_text(target_id, "target_id", 160)
        normalized = MovieImageInfo.model_validate(recognition)
        payload = self._new_payload(
            recognition=normalized,
            raw_results=raw_results,
            final_results=final_results,
            raw_response=raw_response,
        )
        encoded, payload_hash = self._encode_payload(payload)
        now = self._now()
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        snapshot_id = "rs-" + uuid4().hex
        resolved_recognize_id = self._optional_text(
            recognize_id if recognize_id is not None else normalized.recognition_id, 160
        )
        resolved_request_id = self._optional_text(
            provider_request_id if provider_request_id is not None else normalized.provider_request_id, 200
        )
        resolved_trace_id = self._optional_text(
            trace_id if trace_id is not None else normalized.trace_id, 160
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM recognition_snapshots
                    WHERE tenant_id = ? AND shop_id = ? AND buyer_id = ? AND chat_id = ?
                      AND target_id = ? AND event_id = ?
                    """,
                    (*identity, target, event),
                ).fetchone()
                if existing is not None:
                    if str(existing["source_payload_hash"]) != payload_hash:
                        raise RecognitionSnapshotConflict(
                            "recognition_snapshot_event_payload_conflict"
                        )
                    result = self._hydrate(existing)
                    connection.commit()
                    return result
                connection.execute(
                    """
                    UPDATE recognition_snapshots SET is_current = 0, updated_at = ?
                    WHERE tenant_id = ? AND shop_id = ? AND buyer_id = ? AND chat_id = ?
                      AND target_id = ? AND is_current = 1
                    """,
                    (now.isoformat(), *identity, target),
                )
                connection.execute(
                    """
                    INSERT INTO recognition_snapshots(
                        snapshot_id, tenant_id, shop_id, buyer_id, chat_id, event_id, target_id,
                        recognize_id, provider_request_id, trace_id, revision, payload_hash,
                        source_payload_hash, payload_protected, created_at, updated_at, expires_at,
                        is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        snapshot_id, *identity, event, target, resolved_recognize_id,
                        resolved_request_id, resolved_trace_id, payload_hash, payload_hash,
                        self._protector.protect(encoded), now.isoformat(), now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
                row = self._select_by_id(connection, snapshot_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._hydrate(row)

    def get_current(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        target_id: str,
    ) -> RecognitionSnapshot | None:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        target = self._required_text(target_id, "target_id", 160)
        now = self._now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM recognition_snapshots
                WHERE tenant_id = ? AND shop_id = ? AND buyer_id = ? AND chat_id = ?
                  AND target_id = ? AND is_current = 1
                """,
                (*identity, target),
            ).fetchone()
            if row is None:
                return None
            if self._parse_datetime(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE recognition_snapshots SET is_current = 0, updated_at = ? WHERE snapshot_id = ?",
                    (now.isoformat(), row["snapshot_id"]),
                )
                return None
        return self._hydrate(row)

    def get_by_id(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        snapshot_id: str,
    ) -> RecognitionSnapshot | None:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        identifier = self._required_text(snapshot_id, "snapshot_id", 100)
        with self._connect() as connection:
            row = self._select_by_id(connection, identifier, required=False)
        if row is None:
            return None
        self._require_identity(row, identity)
        return self._hydrate(row)

    def list_current(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
    ) -> list[RecognitionSnapshot]:
        """Return every unexpired current target for one buyer conversation."""
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        now = self._now()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recognition_snapshots
                WHERE tenant_id = ? AND shop_id = ? AND buyer_id = ? AND chat_id = ?
                  AND is_current = 1
                ORDER BY created_at ASC, target_id ASC
                """,
                identity,
            ).fetchall()
            expired_ids = [
                str(row["snapshot_id"])
                for row in rows
                if self._parse_datetime(row["expires_at"]) <= now
            ]
            if expired_ids:
                connection.executemany(
                    "UPDATE recognition_snapshots SET is_current = 0, updated_at = ? WHERE snapshot_id = ?",
                    [(now.isoformat(), snapshot_id) for snapshot_id in expired_ids],
                )
        return [
            self._hydrate(row)
            for row in rows
            if str(row["snapshot_id"]) not in set(expired_ids)
        ]

    def append_confirmation(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        snapshot_id: str,
        target_id: str,
        expected_revision: int,
        event_id: str,
        confirmation: Mapping[str, Any],
    ) -> RecognitionSnapshot:
        event = self._required_text(event_id, "event_id", 200)
        details = self._json_object(confirmation, "confirmation")

        def mutate(payload: dict[str, Any], now: datetime) -> None:
            history = list(payload.get("confirmation_history") or [])
            history.append(
                RecognitionSnapshotConfirmation(
                    event_id=event,
                    details=details,
                    created_at=now,
                ).model_dump(mode="json")
            )
            payload["confirmation_history"] = history[-self._max_confirmation_history :]

        return self._mutate(
            tenant_id=tenant_id,
            shop_id=shop_id,
            buyer_id=buyer_id,
            chat_id=chat_id,
            snapshot_id=snapshot_id,
            target_id=target_id,
            expected_revision=expected_revision,
            event_id=event,
            mutate=mutate,
        )

    def compare_and_swap(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        snapshot_id: str,
        target_id: str,
        expected_revision: int,
        event_id: str,
        recognition: MovieImageInfo,
        raw_results: Mapping[str, Any] | None = None,
        final_results: Mapping[str, Any] | None = None,
        raw_response: Mapping[str, Any] | None = None,
        recognize_id: str | None = None,
        provider_request_id: str | None = None,
        trace_id: str | None = None,
        confirmation: Mapping[str, Any] | None = None,
    ) -> RecognitionSnapshot:
        normalized = MovieImageInfo.model_validate(recognition)
        confirmation_details = (
            self._json_object(confirmation, "confirmation")
            if confirmation is not None
            else None
        )

        def mutate(payload: dict[str, Any], now: datetime) -> None:
            payload.update(
                self._observation_payload(
                    recognition=normalized,
                    raw_results=raw_results,
                    final_results=final_results,
                    raw_response=raw_response,
                )
            )
            if confirmation_details is not None:
                history = list(payload.get("confirmation_history") or [])
                history.append(
                    RecognitionSnapshotConfirmation(
                        event_id=event_id,
                        details=confirmation_details,
                        created_at=now,
                    ).model_dump(mode="json")
                )
                payload["confirmation_history"] = history[-self._max_confirmation_history :]

        return self._mutate(
            tenant_id=tenant_id,
            shop_id=shop_id,
            buyer_id=buyer_id,
            chat_id=chat_id,
            snapshot_id=snapshot_id,
            target_id=target_id,
            expected_revision=expected_revision,
            event_id=event_id,
            mutate=mutate,
            recognize_id=recognize_id if recognize_id is not None else normalized.recognition_id,
            provider_request_id=(
                provider_request_id
                if provider_request_id is not None
                else normalized.provider_request_id
            ),
            trace_id=trace_id if trace_id is not None else normalized.trace_id,
        )

    def _mutate(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        snapshot_id: str,
        target_id: str,
        expected_revision: int,
        event_id: str,
        mutate: Callable[[dict[str, Any], datetime], None],
        recognize_id: str | None = None,
        provider_request_id: str | None = None,
        trace_id: str | None = None,
    ) -> RecognitionSnapshot:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        identifier = self._required_text(snapshot_id, "snapshot_id", 100)
        target = self._required_text(target_id, "target_id", 160)
        event = self._required_text(event_id, "event_id", 200)
        if expected_revision < 1:
            raise ValueError("recognition_snapshot_expected_revision_invalid")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._select_by_id(connection, identifier)
                self._require_identity(row, identity)
                if str(row["target_id"]) != target:
                    raise RecognitionSnapshotAccessDenied(
                        "recognition_snapshot_target_mismatch"
                    )
                payload = self._decode_payload(row)
                mutation_event_ids = list(payload.get("mutation_event_ids") or [])
                if event in mutation_event_ids:
                    result = self._hydrate(row, payload=payload)
                    connection.commit()
                    return result
                if self._parse_datetime(row["expires_at"]) <= now:
                    connection.execute(
                        "UPDATE recognition_snapshots SET is_current = 0, updated_at = ? WHERE snapshot_id = ?",
                        (now.isoformat(), identifier),
                    )
                    raise RecognitionSnapshotConflict("recognition_snapshot_expired")
                if not bool(row["is_current"]):
                    raise RecognitionSnapshotConflict("recognition_snapshot_not_current")
                if int(row["revision"]) != expected_revision:
                    raise RecognitionSnapshotConflict(
                        "recognition_snapshot_revision_conflict"
                    )
                mutate(payload, now)
                mutation_event_ids.append(event)
                payload["mutation_event_ids"] = mutation_event_ids[-100:]
                encoded, payload_hash = self._encode_payload(payload)
                new_revision = expected_revision + 1
                cursor = connection.execute(
                    """
                    UPDATE recognition_snapshots
                    SET revision = ?, payload_hash = ?, payload_protected = ?,
                        recognize_id = COALESCE(?, recognize_id),
                        provider_request_id = COALESCE(?, provider_request_id),
                        trace_id = COALESCE(?, trace_id), updated_at = ?
                    WHERE snapshot_id = ? AND revision = ? AND is_current = 1
                    """,
                    (
                        new_revision,
                        payload_hash,
                        self._protector.protect(encoded),
                        self._optional_text(recognize_id, 160),
                        self._optional_text(provider_request_id, 200),
                        self._optional_text(trace_id, 160),
                        now.isoformat(),
                        identifier,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RecognitionSnapshotConflict(
                        "recognition_snapshot_revision_conflict"
                    )
                updated = self._select_by_id(connection, identifier)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._hydrate(updated)

    def _new_payload(
        self,
        *,
        recognition: MovieImageInfo,
        raw_results: Mapping[str, Any] | None,
        final_results: Mapping[str, Any] | None,
        raw_response: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "version": _SCHEMA_VERSION,
            **self._observation_payload(
                recognition=recognition,
                raw_results=raw_results,
                final_results=final_results,
                raw_response=raw_response,
            ),
            "confirmation_history": [],
            "mutation_event_ids": [],
        }

    def _observation_payload(
        self,
        *,
        recognition: MovieImageInfo,
        raw_results: Mapping[str, Any] | None,
        final_results: Mapping[str, Any] | None,
        raw_response: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "raw_results": self._json_object(
                recognition.raw_results if raw_results is None else raw_results,
                "raw_results",
            ),
            "final_results": self._json_object(
                recognition.final_results if final_results is None else final_results,
                "final_results",
            ),
            "raw_response": self._json_object(
                recognition.raw_response if raw_response is None else raw_response,
                "raw_response",
            ),
            # Computed presentation fields are deliberately not persisted as
            # inputs. They are recalculated by MovieImageInfo after restart.
            "normalized": recognition.model_dump(
                mode="json",
                exclude={"seat_display", "seat_display_mode", "fulfillment_route"},
            ),
        }

    def _encode_payload(self, payload: Mapping[str, Any]) -> tuple[str, str]:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("recognition_snapshot_payload_not_json") from error
        actual_bytes = len(encoded.encode("utf-8"))
        if actual_bytes > self._max_payload_bytes:
            raise RecognitionSnapshotPayloadTooLarge(
                actual_bytes=actual_bytes,
                max_bytes=self._max_payload_bytes,
            )
        return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _decode_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            encoded = self._protector.unprotect(str(row["payload_protected"]))
            payload = json.loads(encoded)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise RecognitionSnapshotError(
                "recognition_snapshot_payload_unreadable"
            ) from error
        if not isinstance(payload, dict):
            raise RecognitionSnapshotError("recognition_snapshot_payload_invalid")
        actual_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if actual_hash != str(row["payload_hash"]):
            raise RecognitionSnapshotError("recognition_snapshot_payload_hash_mismatch")
        return payload

    def _hydrate(
        self,
        row: sqlite3.Row,
        *,
        payload: dict[str, Any] | None = None,
    ) -> RecognitionSnapshot:
        body = payload or self._decode_payload(row)
        return RecognitionSnapshot.model_validate(
            {
                "snapshot_id": row["snapshot_id"],
                "tenant_id": row["tenant_id"],
                "shop_id": row["shop_id"],
                "buyer_id": row["buyer_id"],
                "chat_id": row["chat_id"],
                "event_id": row["event_id"],
                "target_id": row["target_id"],
                "recognize_id": row["recognize_id"],
                "provider_request_id": row["provider_request_id"],
                "trace_id": row["trace_id"],
                "raw_results": body.get("raw_results") or {},
                "final_results": body.get("final_results") or {},
                "raw_response": body.get("raw_response") or {},
                "normalized": body.get("normalized") or {},
                "revision": row["revision"],
                "confirmation_history": body.get("confirmation_history") or [],
                "payload_hash": row["payload_hash"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "expires_at": row["expires_at"],
                "is_current": bool(row["is_current"]),
            }
        )

    @staticmethod
    def _select_by_id(
        connection: sqlite3.Connection,
        snapshot_id: str,
        *,
        required: bool = True,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM recognition_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None and required:
            raise RecognitionSnapshotConflict("recognition_snapshot_not_found")
        return row

    @staticmethod
    def _require_identity(row: sqlite3.Row, identity: tuple[str, str, str, str]) -> None:
        stored = tuple(str(row[field]) for field in ("tenant_id", "shop_id", "buyer_id", "chat_id"))
        if stored != identity:
            raise RecognitionSnapshotAccessDenied(
                "recognition_snapshot_identity_mismatch"
            )

    @classmethod
    def _identity(
        cls,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
    ) -> tuple[str, str, str, str]:
        return (
            cls._required_text(tenant_id, "tenant_id", 200),
            cls._required_text(shop_id, "shop_id", 200),
            cls._required_text(buyer_id, "buyer_id", 200),
            cls._required_text(chat_id, "chat_id", 200),
        )

    @staticmethod
    def _required_text(value: object, field: str, max_length: int) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > max_length:
            raise ValueError(f"recognition_snapshot_{field}_invalid")
        return normalized

    @staticmethod
    def _optional_text(value: object, max_length: int) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if len(normalized) > max_length:
            raise ValueError("recognition_snapshot_metadata_too_long")
        return normalized

    @staticmethod
    def _json_object(value: Mapping[str, Any], field: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"recognition_snapshot_{field}_invalid")
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError) as error:
            raise ValueError(f"recognition_snapshot_{field}_not_json") from error

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    @staticmethod
    def _parse_datetime(value: object) -> datetime:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


__all__ = [
    "RecognitionSnapshot",
    "RecognitionSnapshotAccessDenied",
    "RecognitionSnapshotConfirmation",
    "RecognitionSnapshotConflict",
    "RecognitionSnapshotError",
    "RecognitionSnapshotPayloadTooLarge",
    "RecognitionSnapshotStore",
]
