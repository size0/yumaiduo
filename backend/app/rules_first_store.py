from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .settings_store import SecretProtector, default_secret_protector


_RECONCILIATION_DELAYS_SECONDS = (5, 30, 120)
_FINAL_COMMAND_STATUSES = frozenset({"succeeded", "failed", "cancelled", "unknown"})
_SCHEMA_VERSION = 3


def backup_sqlite_database(path: Path, backup_path: Path | None = None) -> Path:
    """Create a consistent SQLite backup using the online backup API."""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)
    destination = backup_path or source.with_name(f"{source.name}.pre-migration-v{_SCHEMA_VERSION}.bak")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_connection, sqlite3.connect(destination) as destination_connection:
        source_connection.backup(destination_connection)
    return destination


def check_sqlite_integrity(path: Path) -> bool:
    """Return true only when SQLite's full integrity check reports ``ok``."""
    with sqlite3.connect(Path(path)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    return bool(result and str(result[0]).lower() == "ok")


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def _text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _pick(source: object, *fields: str) -> str | None:
    if not isinstance(source, Mapping):
        return None
    for field in fields:
        value = _text(source.get(field))
        if value:
            return value
    return None


class RulesFirstStore:
    """SQLite WAL inbox/outbox for the single rules-first transaction runtime.

    Searchable ownership and lifecycle fields remain normalized. Message bodies,
    command payloads, results and manual-task details are protected with the
    configured SecretProtector (AES-256-GCM in Linux production).
    """

    def __init__(self, path: Path, *, protector: SecretProtector | None = None) -> None:
        self._path = path
        self._protector = protector or default_secret_protector()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        had_database = self._path.exists() and self._path.stat().st_size > 0
        migration_backup_ready = False
        if had_database:
            # Inspect before CREATE/ALTER so the rollback copy reflects the
            # true pre-migration database, including legacy databases without
            # a schema_migrations table.
            try:
                with sqlite3.connect(self._path) as probe:
                    row = probe.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()
                    current_version = int(row[0] or 0)
            except sqlite3.Error:
                current_version = 0
            if current_version < _SCHEMA_VERSION:
                if not check_sqlite_integrity(self._path):
                    raise RuntimeError("sqlite_integrity_check_failed")
                backup_sqlite_database(self._path)
                migration_backup_ready = True
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_inbox (
                    inbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    platform_message_id TEXT,
                    event_type TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    lease_until TEXT,
                    payload_protected TEXT NOT NULL,
                    result_protected TEXT,
                    last_error TEXT,
                    received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, event_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS event_inbox_platform_message_unique
                    ON event_inbox(tenant_id, platform_message_id)
                    WHERE platform_message_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS event_inbox_claim_idx
                    ON event_inbox(status, inbox_id);

                CREATE TABLE IF NOT EXISTS transactions (
                    transaction_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    buyer_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 0,
                    flow_state TEXT NOT NULL,
                    order_id TEXT,
                    state_protected TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    closed_at TEXT,
                    close_reason TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, shop_id, buyer_id, chat_id, generation)
                );
                CREATE INDEX IF NOT EXISTS transactions_order_idx
                    ON transactions(tenant_id, order_id);

                CREATE TABLE IF NOT EXISTS state_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
                    tenant_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    revision_before INTEGER NOT NULL,
                    revision_after INTEGER NOT NULL,
                    state_before TEXT NOT NULL,
                    state_after TEXT NOT NULL,
                    transition_code TEXT NOT NULL,
                    evidence_protected TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(transaction_id, event_id)
                );

                CREATE TABLE IF NOT EXISTS command_outbox (
                    command_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    inbox_id INTEGER NOT NULL REFERENCES event_inbox(inbox_id),
                    command_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    state_revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    reconciliation_attempts INTEGER NOT NULL DEFAULT 0,
                    reconciliation_only INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    lease_until TEXT,
                    next_attempt_at TEXT,
                    payload_protected TEXT NOT NULL,
                    result_protected TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, command_type, dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS command_outbox_claim_idx
                    ON command_outbox(status, next_attempt_at, created_at);

                CREATE TABLE IF NOT EXISTS manual_tasks (
                    task_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    buyer_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    transaction_revision INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    lease_token TEXT,
                    lease_until TEXT,
                    claimed_by TEXT,
                    details_protected TEXT NOT NULL,
                    resolution_protected TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, transaction_id, transaction_revision, reason)
                );
                CREATE INDEX IF NOT EXISTS manual_tasks_tenant_status_idx
                    ON manual_tasks(tenant_id, status, created_at);

                CREATE TABLE IF NOT EXISTS liangpiao_quotes (
                    quote_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    quote_hash TEXT NOT NULL UNIQUE,
                    quote_generation INTEGER NOT NULL,
                    quote_expires_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    snapshot_protected TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS liangpiao_quotes_tenant_idx
                    ON liangpiao_quotes(tenant_id, conversation_id, created_at);

                CREATE TABLE IF NOT EXISTS liangpiao_orders (
                    out_order_no TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    quote_id TEXT NOT NULL,
                    quote_hash TEXT NOT NULL,
                    provider_order_no TEXT UNIQUE,
                    provider_status TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    snapshot_protected TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS liangpiao_callback_records (
                    callback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT,
                    out_order_no TEXT,
                    provider_order_no TEXT,
                    event_id TEXT,
                    provider_status TEXT,
                    verification_status TEXT NOT NULL DEFAULT 'received',
                    processing_status TEXT NOT NULL DEFAULT 'received',
                    result_code TEXT,
                    reason TEXT,
                    payload_hash TEXT NOT NULL,
                    payload_protected TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS liangpiao_callback_tenant_idx
                    ON liangpiao_callback_records(tenant_id, received_at DESC);
                CREATE INDEX IF NOT EXISTS liangpiao_callback_order_idx
                    ON liangpiao_callback_records(provider_order_no, out_order_no, received_at DESC);
                """
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(transactions)")}
            for name, declaration in (
                ("is_active", "INTEGER NOT NULL DEFAULT 1"),
                ("closed_at", "TEXT"),
                ("close_reason", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE transactions ADD COLUMN {name} {declaration}")
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS transactions_one_active_generation
                   ON transactions(tenant_id,shop_id,buyer_id,chat_id) WHERE is_active=1"""
            )
            manual_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(manual_tasks)")}
            for name, declaration in (("lease_until", "TEXT"), ("claimed_by", "TEXT")):
                if name not in manual_columns:
                    connection.execute(f"ALTER TABLE manual_tasks ADD COLUMN {name} {declaration}")

            versions = {
                int(row[0]) for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            }
            if not versions:
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                    (1, _now().isoformat()),
                )
                versions.add(1)
            if _SCHEMA_VERSION not in versions:
                # Existing databases are copied before any destructive-looking
                # schema operation.  The migration itself only adds nullable
                # columns and therefore preserves all legacy data.
                if had_database and not migration_backup_ready:
                    if not check_sqlite_integrity(self._path):
                        raise RuntimeError("sqlite_integrity_check_failed")
                    backup_sqlite_database(self._path)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    transaction_columns = {
                        str(row[1]) for row in connection.execute("PRAGMA table_info(transactions)")
                    }
                    additions = (
                        ("provider", "TEXT"),
                        ("provider_show_id", "TEXT"),
                        ("selected_seats_json", "TEXT"),
                        ("preflight_request_json", "TEXT"),
                        ("preflight_response_json", "TEXT"),
                        ("provider_amount_fen", "INTEGER"),
                        ("buyer_amount_fen", "INTEGER"),
                        ("quote_hash", "TEXT"),
                        ("quote_generation", "INTEGER"),
                        ("quote_expires_at", "TEXT"),
                        ("confirmation_id", "TEXT"),
                        ("out_order_no", "TEXT"),
                        ("provider_order_no", "TEXT"),
                        ("provider_payload_hash", "TEXT"),
                        ("provider_status", "TEXT"),
                    )
                    for name, declaration in additions:
                        if name not in transaction_columns:
                            connection.execute(
                                f"ALTER TABLE transactions ADD COLUMN {name} {declaration}"
                            )
                    if 2 not in versions:
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                            (2, _now().isoformat()),
                        )
                    if _SCHEMA_VERSION not in versions:
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                            (_SCHEMA_VERSION, _now().isoformat()),
                        )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise

    def journal_mode(self) -> str:
        with self._connect() as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0])

    def schema_version(self) -> int:
        """Return the highest applied incremental schema version."""
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def _protect(self, value: object) -> str:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return self._protector.protect(raw)

    def _unprotect(self, value: str | None) -> Any:
        if not value:
            return None
        return json.loads(self._protector.unprotect(value))

    def save_selected_seat_quote(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Persist an immutable quote snapshot; repeated hashes are idempotent."""
        required = ("quote_id", "tenant_id", "conversation_id", "quote_hash", "generation", "expires_at")
        if any(not _text(snapshot.get(key)) for key in required):
            raise ValueError("liangpiao_quote_snapshot_invalid")
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO liangpiao_quotes(
                   quote_id,tenant_id,conversation_id,quote_hash,quote_generation,
                   quote_expires_at,snapshot_protected,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    str(snapshot["quote_id"]), str(snapshot["tenant_id"]), str(snapshot["conversation_id"]),
                    str(snapshot["quote_hash"]), int(snapshot.get("generation") or 1), str(snapshot["expires_at"]),
                    self._protect(dict(snapshot)), now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM liangpiao_quotes WHERE quote_id=?", (str(snapshot["quote_id"]),)
            ).fetchone()
        return self._liangpiao_quote_view(row)

    def get_selected_seat_quote(self, quote_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM liangpiao_quotes WHERE quote_id=?", (str(quote_id),)).fetchone()
        return self._liangpiao_quote_view(row) if row else None

    def save_liangpiao_order(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        required = ("out_order_no", "tenant_id", "quote_id", "quote_hash", "payload_hash")
        if any(not _text(snapshot.get(key)) for key in required):
            raise ValueError("liangpiao_order_snapshot_invalid")
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO liangpiao_orders(
                   out_order_no,tenant_id,quote_id,quote_hash,provider_order_no,
                   provider_status,payload_hash,snapshot_protected,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    str(snapshot["out_order_no"]), str(snapshot["tenant_id"]), str(snapshot["quote_id"]),
                    str(snapshot["quote_hash"]), _text(snapshot.get("provider_order_no")),
                    str(snapshot.get("provider_status") or "unknown"), str(snapshot["payload_hash"]),
                    self._protect(dict(snapshot)), now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM liangpiao_orders WHERE out_order_no=?", (str(snapshot["out_order_no"]),)
            ).fetchone()
        return self._liangpiao_order_view(row)

    def record_liangpiao_callback(
        self, raw_body: bytes, *, signature: str = "", timestamp: str = "", nonce: str = "",
    ) -> dict[str, Any]:
        raw = bytes(raw_body)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        body = dict(body) if isinstance(body, Mapping) else {}
        out_order_no = _pick(body, "outOrderNo", "out_order_no")
        provider_order_no = _pick(body, "providerOrderNo", "provider_order_no", "orderNo", "orderId", "order_id")
        event_id = _pick(body, "eventId", "event_id")
        provider_status = _pick(body, "status", "orderStatus", "order_status", "event")
        tenant_id = None
        if out_order_no or provider_order_no:
            linked = self.find_liangpiao_order(
                out_order_no=out_order_no, provider_order_no=provider_order_no,
            )
            tenant_id = _text(linked.get("tenant_id")) if linked else None
        payload = {
            "raw_body_b64": base64.b64encode(raw).decode("ascii"),
            "signature": str(signature or ""), "timestamp": str(timestamp or ""), "nonce": str(nonce or ""),
        }
        now = _now().isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO liangpiao_callback_records(
                   tenant_id,out_order_no,provider_order_no,event_id,provider_status,
                   payload_hash,payload_protected,received_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    tenant_id, out_order_no, provider_order_no, event_id, provider_status,
                    hashlib.sha256(raw).hexdigest(), self._protect(payload), now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM liangpiao_callback_records WHERE callback_id=?", (cursor.lastrowid,)
            ).fetchone()
        return self._liangpiao_callback_view(row)

    def update_liangpiao_callback(self, callback_id: int, **updates: object) -> dict[str, Any]:
        allowed = {
            "tenant_id", "provider_status", "verification_status", "processing_status", "result_code", "reason",
        }
        values = {key: updates[key] for key in allowed if key in updates}
        values["updated_at"] = _now().isoformat()
        if not values:
            raise ValueError("liangpiao_callback_update_empty")
        assignments = ",".join(f"{key}=?" for key in values)
        params = [values[key] for key in values]
        params.append(int(callback_id))
        with self._connect() as connection:
            connection.execute(
                f"UPDATE liangpiao_callback_records SET {assignments} WHERE callback_id=?", tuple(params),
            )
            row = connection.execute(
                "SELECT * FROM liangpiao_callback_records WHERE callback_id=?", (int(callback_id),)
            ).fetchone()
        if row is None:
            raise ValueError("liangpiao_callback_not_found")
        return self._liangpiao_callback_view(row)

    def list_liangpiao_callbacks(self, tenant_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            return []
        bounded_limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM liangpiao_callback_records WHERE tenant_id=? ORDER BY received_at DESC LIMIT ?",
                (tenant, bounded_limit),
            ).fetchall()
        return [self._liangpiao_callback_view(row) for row in rows]

    def list_liangpiao_orders(self, tenant_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            return []
        bounded_limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM liangpiao_orders WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant, bounded_limit),
            ).fetchall()
        return [self._liangpiao_order_view(row) for row in rows]

    def find_liangpiao_order(self, *, out_order_no: str | None = None,
                             provider_order_no: str | None = None,
                             tenant_id: str | None = None) -> dict[str, Any] | None:
        clauses = []
        params: list[str] = []
        if out_order_no:
            clauses.append("out_order_no=?")
            params.append(str(out_order_no))
        if provider_order_no:
            clauses.append("provider_order_no=?")
            params.append(str(provider_order_no))
        if not clauses or (not out_order_no and not provider_order_no):
            return None
        order_clause = " OR ".join(clauses)
        if tenant_id:
            order_clause = f"({order_clause}) AND tenant_id=?"
            params.append(str(tenant_id))
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM liangpiao_orders WHERE {order_clause} LIMIT 1",
                tuple(params),
            ).fetchone()
        return self._liangpiao_order_view(row) if row else None

    def _liangpiao_quote_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(row)
        data = self._unprotect(row.get("snapshot_protected")) or {}
        data.update({"quote_id": str(row["quote_id"]), "quote_hash": str(row["quote_hash"]),
                     "tenant_id": str(row["tenant_id"]), "conversation_id": str(row["conversation_id"]),
                     "generation": int(row["quote_generation"]), "expires_at": str(row["quote_expires_at"])})
        return data

    def _liangpiao_callback_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(row)
        return {
            "callback_id": int(row["callback_id"]), "tenant_id": row.get("tenant_id"),
            "out_order_no": row.get("out_order_no"), "provider_order_no": row.get("provider_order_no"),
            "event_id": row.get("event_id"), "provider_status": row.get("provider_status"),
            "verification_status": row.get("verification_status"), "processing_status": row.get("processing_status"),
            "result_code": row.get("result_code"), "reason": row.get("reason"),
            "payload_hash": row.get("payload_hash"), "received_at": row.get("received_at"),
            "updated_at": row.get("updated_at"),
        }

    def _liangpiao_order_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(row)
        data = self._unprotect(row.get("snapshot_protected")) or {}
        data.update({"out_order_no": str(row["out_order_no"]), "tenant_id": str(row["tenant_id"]),
                     "quote_id": str(row["quote_id"]), "quote_hash": str(row["quote_hash"]),
                     "provider_order_no": row.get("provider_order_no"), "provider_status": str(row["provider_status"]),
                     "payload_hash": str(row["payload_hash"])})
        return data

    @staticmethod
    def _event_identity(body: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
        envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        tenant_id = _pick(envelope, "tenantId", "tenant_id")
        event_id = _pick(envelope, "id", "eventId", "event_id")
        event_type = _pick(envelope, "event")
        if not tenant_id or not event_id or not event_type:
            raise ValueError("rules_event_identity_invalid")
        platform_message_id = _pick(payload, "remoteMessageId", "remote_message_id", "messageId", "message_id") or ""
        session_key = "\0".join(
            _pick(payload, left, right) or ""
            for left, right in (("accountUnb", "account_unb"), ("chatId", "chat_id"), ("peerUnb", "peer_unb"))
        )
        return tenant_id, event_id, event_type, platform_message_id, session_key

    def enqueue_event(self, body: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, object]:
        tenant_id, event_id, event_type, platform_message_id, session_key = self._event_identity(body)
        timestamp = _now(now).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_id FROM event_inbox WHERE tenant_id=? AND (event_id=? OR platform_message_id=?) ORDER BY inbox_id LIMIT 1",
                (tenant_id, event_id, platform_message_id or None),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return {"event_id": str(existing["event_id"]), "accepted": True, "duplicate": True}
            connection.execute(
                """INSERT INTO event_inbox(
                    tenant_id,event_id,platform_message_id,event_type,session_key,payload_protected,received_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    tenant_id, event_id, platform_message_id or None, event_type, session_key,
                    self._protect(dict(body)), timestamp, timestamp,
                ),
            )
            connection.commit()
        return {"event_id": event_id, "accepted": True, "duplicate": False}

    def claim_event(
        self, *, now: datetime | None = None, lease_seconds: int = 60,
    ) -> dict[str, Any] | None:
        current = _now(now)
        token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM event_inbox
                   WHERE status='pending' OR (status='processing' AND lease_until<=?)
                   ORDER BY inbox_id LIMIT 1""",
                (current.isoformat(),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            until = (current + timedelta(seconds=max(1, lease_seconds))).isoformat()
            connection.execute(
                """UPDATE event_inbox SET status='processing', attempts=attempts+1,
                   lease_token=?,lease_until=?,updated_at=? WHERE inbox_id=?""",
                (token, until, current.isoformat(), row["inbox_id"]),
            )
            connection.commit()
        return {
            "inbox_id": int(row["inbox_id"]), "tenant_id": str(row["tenant_id"]),
            "event_id": str(row["event_id"]), "lease_token": token,
            "body": self._unprotect(row["payload_protected"]),
        }

    @staticmethod
    def _command_dedupe(action: Mapping[str, Any], state_revision: int) -> str:
        action_type = _pick(action, "type") or "unknown"
        explicit = _pick(action, "dedupe_key")
        if explicit:
            if len(explicit) > 500:
                raise ValueError("command_dedupe_key_invalid")
            return explicit
        if action_type == "change_order_price":
            quote = action.get("quote_snapshot") if isinstance(action.get("quote_snapshot"), Mapping) else {}
            values = (
                _pick(quote, "order_id"), _pick(quote, "quote_record_id"),
                _pick(quote, "confirmation_version"), str(quote.get("target_amount_cents") or ""),
            )
            if any(not value for value in values):
                raise ValueError("price_change_command_identity_invalid")
            return "\0".join(values)
        action_id = _pick(action, "id")
        if not action_id:
            raise ValueError("command_action_id_missing")
        return f"{action_id}\0revision:{state_revision}"

    def complete_event(
        self, inbox_id: int, lease_token: str, *, commands: Sequence[Mapping[str, Any]],
        state_revision: int, result: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current = _now(now).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute("SELECT * FROM event_inbox WHERE inbox_id=?", (inbox_id,)).fetchone()
            if event is None:
                connection.rollback()
                raise KeyError("event_inbox_missing")
            if event["status"] == "completed":
                rows = connection.execute(
                    "SELECT * FROM command_outbox WHERE inbox_id=? ORDER BY created_at,command_id", (inbox_id,),
                ).fetchall()
                connection.commit()
                return [self._command_view(row) for row in rows]
            if event["status"] != "processing" or event["lease_token"] != lease_token:
                connection.rollback()
                raise ValueError("event_inbox_lease_conflict")
            created: list[sqlite3.Row] = []
            for action in commands:
                action_type = _pick(action, "type") or ""
                dedupe_key = self._command_dedupe(action, state_revision)
                command_id = "cmd-" + hashlib.sha256(
                    f"{event['tenant_id']}\0{action_type}\0{dedupe_key}".encode()
                ).hexdigest()[:40]
                payload = {
                    "action": dict(action),
                    "context": self._unprotect(event["payload_protected"]),
                }
                connection.execute(
                    """INSERT OR IGNORE INTO command_outbox(
                        command_id,tenant_id,event_id,inbox_id,command_type,dedupe_key,state_revision,
                        payload_protected,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        command_id, event["tenant_id"], event["event_id"], inbox_id, action_type,
                        dedupe_key, state_revision, self._protect(payload), current, current,
                    ),
                )
                row = connection.execute("SELECT * FROM command_outbox WHERE command_id=?", (command_id,)).fetchone()
                if row is not None:
                    created.append(row)
            connection.execute(
                """UPDATE event_inbox SET status='completed',lease_token=NULL,lease_until=NULL,
                   result_protected=?,updated_at=? WHERE inbox_id=?""",
                (self._protect(dict(result or {})), current, inbox_id),
            )
            connection.commit()
        return [self._command_view(row) for row in created]

    def append_commands(
        self, *, tenant_id: str, event_id: str, commands: Sequence[Mapping[str, Any]],
        state_revision: int, now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        timestamp = _now(now).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT * FROM event_inbox WHERE tenant_id=? AND event_id=?", (tenant_id, event_id),
            ).fetchone()
            if event is None:
                connection.rollback()
                raise KeyError("event_inbox_missing")
            created: list[sqlite3.Row] = []
            for action in commands:
                action_type = _pick(action, "type") or ""
                dedupe_key = self._command_dedupe(action, state_revision)
                command_id = "cmd-" + hashlib.sha256(
                    f"{tenant_id}\0{action_type}\0{dedupe_key}".encode()
                ).hexdigest()[:40]
                payload = {"action": dict(action), "context": self._unprotect(event["payload_protected"])}
                connection.execute(
                    """INSERT OR IGNORE INTO command_outbox(
                        command_id,tenant_id,event_id,inbox_id,command_type,dedupe_key,state_revision,
                        payload_protected,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        command_id, tenant_id, event_id, event["inbox_id"], action_type, dedupe_key,
                        state_revision, self._protect(payload), timestamp, timestamp,
                    ),
                )
                row = connection.execute("SELECT * FROM command_outbox WHERE command_id=?", (command_id,)).fetchone()
                if row is not None:
                    created.append(row)
            connection.commit()
        return [self._command_view(row) for row in created]

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM command_outbox WHERE command_id=?", (command_id,)).fetchone()
        return self._command_view(row) if row else None

    def fail_event(self, inbox_id: int, lease_token: str, reason: str, *, now: datetime | None = None) -> None:
        current = _now(now).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts FROM event_inbox WHERE inbox_id=? AND status='processing' AND lease_token=?",
                (inbox_id, lease_token),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ValueError("event_inbox_lease_conflict")
            status = "failed" if int(row["attempts"]) >= 5 else "pending"
            connection.execute(
                """UPDATE event_inbox SET status=?,lease_token=NULL,lease_until=NULL,
                   last_error=?,updated_at=? WHERE inbox_id=?""",
                (status, str(reason)[:240], current, inbox_id),
            )
            connection.commit()

    def cancel_pending_commands(self, reason: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
        timestamp = _now(now).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM command_outbox WHERE status IN ('pending','reconciling') ORDER BY created_at",
            ).fetchall()
            connection.execute(
                """UPDATE command_outbox SET status='cancelled',next_attempt_at=NULL,
                   last_error=?,updated_at=? WHERE status IN ('pending','reconciling')""",
                (str(reason)[:240], timestamp),
            )
            connection.commit()
        return [self._command_view(row) for row in rows]

    def claim_commands(
        self, *, now: datetime | None = None, lease_seconds: int = 60, limit: int = 10,
    ) -> list[dict[str, Any]]:
        current = _now(now)
        claimed: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT * FROM command_outbox
                   WHERE ((status IN ('pending','reconciling') AND (next_attempt_at IS NULL OR next_attempt_at<=?))
                      OR (status='claimed' AND lease_until<=?))
                   ORDER BY created_at,command_id LIMIT ?""",
                (current.isoformat(), current.isoformat(), max(1, min(int(limit), 50))),
            ).fetchall()
            for row in rows:
                token = uuid4().hex
                until = (current + timedelta(seconds=max(1, lease_seconds))).isoformat()
                connection.execute(
                    """UPDATE command_outbox SET status='claimed',attempts=attempts+1,
                       lease_token=?,lease_until=?,updated_at=? WHERE command_id=?""",
                    (token, until, current.isoformat(), row["command_id"]),
                )
                current_row = dict(row)
                current_row.update({"status": "claimed", "lease_token": token, "lease_until": until})
                claimed.append(self._command_view(current_row))
            connection.commit()
        return claimed

    def _command_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(row)
        payload = self._unprotect(row.get("payload_protected"))
        return {
            "command_id": str(row["command_id"]), "tenant_id": str(row["tenant_id"]),
            "event_id": str(row["event_id"]), "command_type": str(row["command_type"]),
            "dedupe_key": str(row["dedupe_key"]), "state_revision": int(row["state_revision"]),
            "status": str(row["status"]), "lease_token": row.get("lease_token"),
            "lease_until": row.get("lease_until"), "next_attempt_at": row.get("next_attempt_at"),
            "reconciliation_only": bool(row.get("reconciliation_only")),
            "action": payload["action"], "context": payload["context"],
        }

    def record_command_result(
        self, command_id: str, lease_token: str, result: Mapping[str, Any],
        *, now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _now(now)
        requested_status = _pick(result, "status") or "unknown"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM command_outbox WHERE command_id=?", (command_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError("command_missing")
            existing_result = self._unprotect(row["result_protected"])
            if row["status"] in _FINAL_COMMAND_STATUSES:
                if existing_result != dict(result):
                    connection.rollback()
                    raise ValueError("command_result_conflict")
                connection.commit()
                return self._command_result_view(row)
            if row["status"] != "claimed" or row["lease_token"] != lease_token:
                connection.rollback()
                raise ValueError("command_lease_conflict")
            if requested_status == "succeeded":
                status = "succeeded"
            elif requested_status == "cancelled":
                status = "cancelled"
            elif requested_status in {"failed", "skipped", "rejected"}:
                status = "failed"
            else:
                status = "unknown"
            next_attempt_at = None
            reconciliation_only = int(row["reconciliation_only"])
            reconciliation_attempts = int(row["reconciliation_attempts"])
            if requested_status == "unknown" and row["command_type"] == "change_order_price":
                if reconciliation_attempts < len(_RECONCILIATION_DELAYS_SECONDS):
                    delay = _RECONCILIATION_DELAYS_SECONDS[reconciliation_attempts]
                    reconciliation_attempts += 1
                    status = "reconciling"
                    reconciliation_only = 1
                    next_attempt_at = (current + timedelta(seconds=delay)).isoformat()
            connection.execute(
                """UPDATE command_outbox SET status=?,reconciliation_attempts=?,reconciliation_only=?,
                   lease_token=NULL,lease_until=NULL,next_attempt_at=?,result_protected=?,updated_at=?
                   WHERE command_id=?""",
                (
                    status, reconciliation_attempts, reconciliation_only, next_attempt_at,
                    self._protect(dict(result)), current.isoformat(), command_id,
                ),
            )
            updated = connection.execute("SELECT * FROM command_outbox WHERE command_id=?", (command_id,)).fetchone()
            connection.commit()
        return self._command_result_view(updated)

    @staticmethod
    def _command_result_view(row: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(row)
        return {
            "command_id": str(row["command_id"]), "status": str(row["status"]),
            "next_attempt_at": row.get("next_attempt_at"),
            "reconciliation_only": bool(row.get("reconciliation_only")),
        }

    def create_manual_task(
        self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str,
        transaction_id: str, transaction_revision: int, reason: str,
        details: Mapping[str, Any] | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        values = [tenant_id, shop_id, buyer_id, chat_id, transaction_id, reason]
        if any(not _text(value) for value in values) or transaction_revision < 0:
            raise ValueError("manual_task_identity_invalid")
        task_id = "manual-" + hashlib.sha256(
            "\0".join([tenant_id, transaction_id, str(transaction_revision), reason]).encode()
        ).hexdigest()[:40]
        timestamp = _now(now).isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO manual_tasks(
                    task_id,tenant_id,shop_id,buyer_id,chat_id,transaction_id,transaction_revision,
                    reason,details_protected,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, tenant_id, shop_id, buyer_id, chat_id, transaction_id,
                    transaction_revision, reason, self._protect(dict(details or {})), timestamp, timestamp,
                ),
            )
            row = connection.execute("SELECT * FROM manual_tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._manual_view(row)

    def list_manual_tasks(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM manual_tasks WHERE tenant_id=? ORDER BY created_at", (tenant_id,),
            ).fetchall()
        return [self._manual_view(row) for row in rows]

    def claim_manual_task(
        self, tenant_id: str, task_id: str, *, expected_revision: int,
        operator_id: str, lease_seconds: int = 900, now: datetime | None = None,
    ) -> dict[str, Any]:
        operator = str(operator_id or "").strip()
        if not operator or len(operator) > 200:
            raise ValueError("manual_task_operator_invalid")
        current = _now(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM manual_tasks WHERE tenant_id=? AND task_id=?", (tenant_id, task_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError("manual_task_missing")
            if int(row["transaction_revision"]) != expected_revision:
                connection.rollback()
                raise ValueError("manual_task_revision_conflict")
            lease_active = bool(
                row["status"] == "claimed" and row["lease_until"]
                and str(row["lease_until"]) > current.isoformat()
            )
            if row["status"] == "claimed" and lease_active and row["claimed_by"] != operator:
                connection.rollback()
                raise ValueError("manual_task_already_claimed")
            if row["status"] not in {"pending", "claimed"}:
                connection.rollback()
                raise ValueError("manual_task_not_claimable")
            token = row["lease_token"] if lease_active and row["claimed_by"] == operator else uuid4().hex
            until = (current + timedelta(seconds=max(60, min(int(lease_seconds), 3600)))).isoformat()
            connection.execute(
                """UPDATE manual_tasks SET status='claimed',lease_token=?,lease_until=?,claimed_by=?,updated_at=?
                   WHERE task_id=?""",
                (token, until, operator, current.isoformat(), task_id),
            )
            updated = connection.execute("SELECT * FROM manual_tasks WHERE task_id=?", (task_id,)).fetchone()
            connection.commit()
        return self._manual_view(updated)

    def update_manual_task_revision(
        self, tenant_id: str, task_id: str, *, previous_revision: int, new_revision: int,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE manual_tasks SET transaction_revision=?,updated_at=?
                   WHERE tenant_id=? AND task_id=? AND transaction_revision=? AND status='claimed'""",
                (new_revision, _now().isoformat(), tenant_id, task_id, previous_revision),
            ).rowcount
            if changed != 1:
                raise ValueError("manual_task_revision_conflict")
            row = connection.execute("SELECT * FROM manual_tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._manual_view(row)

    def complete_manual_task(
        self, tenant_id: str, task_id: str, *, expected_revision: int,
        lease_token: str, resolution: str, now: datetime | None = None,
    ) -> dict[str, Any]:
        if resolution not in {"resume", "cancel", "resolved"}:
            raise ValueError("manual_task_resolution_invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM manual_tasks WHERE tenant_id=? AND task_id=?", (tenant_id, task_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError("manual_task_missing")
            if int(row["transaction_revision"]) != expected_revision:
                connection.rollback()
                raise ValueError("manual_task_revision_conflict")
            current = _now(now)
            if (
                row["status"] != "claimed" or row["lease_token"] != str(lease_token or "")
                or not row["lease_until"] or str(row["lease_until"]) <= current.isoformat()
            ):
                connection.rollback()
                raise ValueError("manual_task_lease_conflict")
            connection.execute(
                """UPDATE manual_tasks SET status='completed',lease_token=NULL,lease_until=NULL,
                   resolution_protected=?,updated_at=? WHERE task_id=?""",
                (self._protect({"resolution": resolution}), current.isoformat(), task_id),
            )
            updated = connection.execute("SELECT * FROM manual_tasks WHERE task_id=?", (task_id,)).fetchone()
            connection.commit()
        return self._manual_view(updated)

    def _manual_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(row)
        return {
            "task_id": str(row["task_id"]), "tenant_id": str(row["tenant_id"]),
            "shop_id": str(row["shop_id"]), "buyer_id": str(row["buyer_id"]),
            "chat_id": str(row["chat_id"]), "transaction_id": str(row["transaction_id"]),
            "transaction_revision": int(row["transaction_revision"]),
            "reason": str(row["reason"]), "status": str(row["status"]),
            "lease_token": row.get("lease_token"), "lease_until": row.get("lease_until"),
            "claimed_by": row.get("claimed_by"),
            "details": self._unprotect(row.get("details_protected")) or {},
        }
