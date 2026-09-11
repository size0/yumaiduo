from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any
from uuid import uuid4

from .conversation_fact_patch_parser import merge_conversation_facts
from .settings_store import SecretProtector, default_secret_protector


_SCHEMA_VERSION = 2
_DEFAULT_TTL_SECONDS = 1_800
_ALLOWED_FACTS = frozenset({
    "city", "cinema", "cinema_address", "movie", "quote_date", "showtime_start",
    "showtime_end", "hall", "dimension", "language", "seat_request_type",
    "selected_seats", "ticket_count", "showtime_ordinal",
    "candidate_shows", "show_id", "verified",
})


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _text(value: object, *, limit: int = 300) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized[:limit]


def _event_identity(body: Mapping[str, Any]) -> dict[str, str]:
    envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    session = body.get("session") if isinstance(body.get("session"), Mapping) else {}

    def pick(*values: object) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    chat_id = pick(session.get("chatId"), session.get("chat_id"), payload.get("chatId"), payload.get("chat_id"))
    return {
        "tenant_id": pick(envelope.get("tenantId"), envelope.get("tenant_id"), body.get("tenant_id")),
        "shop_id": pick(session.get("accountUnb"), session.get("account_unb"), payload.get("accountUnb"), payload.get("account_unb")),
        "buyer_id": pick(session.get("peerUnb"), session.get("peer_unb"), payload.get("peerUnb"), payload.get("peer_unb")),
        "chat_id": chat_id,
        # An absent item id means “latest context in this isolated chat”; do
        # not turn it into a new chat:* context and lose the previous image.
        "purchase_context_id": pick(payload.get("itemId"), payload.get("item_id")),
        "event_id": pick(envelope.get("id"), envelope.get("eventId"), envelope.get("event_id")),
        "message_id": pick(payload.get("remoteMessageId"), payload.get("remote_message_id"), payload.get("messageId"), payload.get("message_id")),
    }


class ConversationFactStore:
    """Encrypted, TTL-scoped facts shared by Canonical and Legacy turns.

    This store is intentionally not a quote or transaction authority. It keeps
    only structured recognition/request facts needed to resolve a follow-up;
    prices, provider responses, image bytes and order state are excluded.
    """

    authority_name = "conversation_facts_sqlite"

    def __init__(
        self,
        path: Path,
        *,
        protector: SecretProtector | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 7 * 24 * 60 * 60:
            raise ValueError("conversation_fact_ttl_seconds_invalid")
        self._path = Path(path)
        self._protector = protector or default_secret_protector()
        self._ttl_seconds = ttl_seconds
        self._lock = RLock()
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
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_facts (
                    fact_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    buyer_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    purchase_context_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    fact_tier TEXT NOT NULL,
                    facts_protected TEXT NOT NULL,
                    event_id TEXT,
                    message_id TEXT,
                    observed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, shop_id, buyer_id, chat_id, purchase_context_id)
                );
                CREATE INDEX IF NOT EXISTS conversation_facts_lookup_idx
                    ON conversation_facts(tenant_id, shop_id, buyer_id, chat_id, expires_at, updated_at);
                CREATE TABLE IF NOT EXISTS agent_states (
                    state_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL, buyer_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    purchase_context_id TEXT NOT NULL, state_protected TEXT NOT NULL, revision INTEGER NOT NULL,
                    expires_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, shop_id, buyer_id, chat_id, purchase_context_id)
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (_SCHEMA_VERSION, _utc().isoformat()),
            )

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def journal_mode(self) -> str:
        with self._connect() as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0])

    def save(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        purchase_context_id: str,
        facts: Mapping[str, Any],
        source: str,
        fact_tier: str = "candidate",
        event_id: str | None = None,
        message_id: str | None = None,
        observed_at: datetime | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id, purchase_context_id)
        incoming = self._sanitize_facts(facts)
        if not incoming:
            raise ValueError("conversation_facts_empty")
        source_value = _text(source, limit=80)
        tier_value = _text(fact_tier, limit=40)
        if not source_value or not tier_value:
            raise ValueError("conversation_fact_metadata_invalid")
        ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= 7 * 24 * 60 * 60:
            raise ValueError("conversation_fact_ttl_seconds_invalid")
        observed = _utc(observed_at)
        now = _utc()
        expires = observed + timedelta(seconds=ttl)
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM conversation_facts WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? AND purchase_context_id=?",
                identity,
            ).fetchone()
            previous = self._row_view(existing) if existing is not None else None
            merged = merge_conversation_facts(previous.get("facts") if previous else {}, incoming)
            fact_id = str(previous.get("fact_id")) if previous else f"cf-{uuid4().hex}"
            created_at = str(previous.get("created_at")) if previous else now.isoformat()
            values = (
                fact_id, *identity, source_value, tier_value, self._protect(merged),
                _text(event_id, limit=240), _text(message_id, limit=240), observed.isoformat(),
                expires.isoformat(), created_at, now.isoformat(),
            )
            connection.execute(
                """INSERT INTO conversation_facts(
                    fact_id,tenant_id,shop_id,buyer_id,chat_id,purchase_context_id,
                    source,fact_tier,facts_protected,event_id,message_id,observed_at,
                    expires_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,shop_id,buyer_id,chat_id,purchase_context_id) DO UPDATE SET
                    source=excluded.source, fact_tier=excluded.fact_tier,
                    facts_protected=excluded.facts_protected, event_id=excluded.event_id,
                    message_id=excluded.message_id, observed_at=excluded.observed_at,
                    expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
                values,
            )
            row = connection.execute("SELECT * FROM conversation_facts WHERE fact_id=?", (fact_id,)).fetchone()
        return self._row_view(row) if row is not None else {}

    def get_current(
        self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str,
        purchase_context_id: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        base = tuple(str(value or "").strip() for value in (tenant_id, shop_id, buyer_id, chat_id))
        if any(not value or len(value) > 240 for value in base):
            raise ValueError("conversation_fact_identity_invalid")
        reference = _utc(now)
        with self._lock, self._connect() as connection:
            if purchase_context_id:
                context = str(purchase_context_id).strip()
                if not context or len(context) > 240:
                    raise ValueError("conversation_fact_identity_invalid")
                row = connection.execute(
                    "SELECT * FROM conversation_facts WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? AND purchase_context_id=?",
                    (*base, context),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM conversation_facts WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? ORDER BY updated_at DESC LIMIT 1",
                    base,
                ).fetchone()
        if row is None:
            return None
        view = self._row_view(row)
        expires = _parse_time(view.get("expires_at"))
        if expires is None or expires <= reference:
            return None
        return view

    def load_context(self, *, now: datetime | None = None, **identity: Any) -> dict[str, Any]:
        current = self.get_current(now=now, **identity)
        result = {"available": current is not None, "facts": {}, "fact_tier": None, "source": None, "expired": False}
        if current is not None:
            result.update({
                "facts": dict(current["facts"]), "fact_tier": current["fact_tier"],
                "source": current["source"], "fact_id": current["fact_id"],
                "purchase_context_id": current.get("purchase_context_id"),
                "expires_at": current["expires_at"], "event_id": current.get("event_id"),
                "message_id": current.get("message_id"),
            })
            return result
        # Expose only expiry metadata. Expired values must never become input to
        # a parser, model, quote, or buyer-facing assertion.
        tenant = str(identity.get("tenant_id") or "").strip()
        base = (tenant, str(identity.get("shop_id") or "").strip(), str(identity.get("buyer_id") or "").strip(), str(identity.get("chat_id") or "").strip())
        if all(base):
            context_id = str(identity.get("purchase_context_id") or "").strip()
            with self._connect() as connection:
                if context_id:
                    row = connection.execute(
                        "SELECT expires_at,source FROM conversation_facts WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? AND purchase_context_id=?",
                        (*base, context_id),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT expires_at,source FROM conversation_facts WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? ORDER BY updated_at DESC LIMIT 1",
                        base,
                    ).fetchone()
            reference = _utc(now)
            if row is not None and (_parse_time(row["expires_at"]) or reference) <= reference:
                result.update({"expired": True, "source": row["source"], "expires_at": row["expires_at"]})
        return result

    def load_agent_state(self, *, now: datetime | None = None, **identity: Any) -> dict[str, Any]:
        keys = tuple(str(identity.get(key) or "").strip() for key in ("tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id"))
        if any(not value for value in keys):
            return {}
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM agent_states WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? AND purchase_context_id=?", keys).fetchone()
        if row is None or (_parse_time(row["expires_at"]) or _utc()) <= _utc(now):
            return {}
        return {**json.loads(self._protector.unprotect(row["state_protected"])), "revision": int(row["revision"]), "expires_at": row["expires_at"]}

    def save_agent_state(self, *, state: Mapping[str, Any], ttl_seconds: int | None = None, expected_revision: int | None = None, **identity: Any) -> dict[str, Any]:
        keys = tuple(str(identity.get(key) or "").strip() for key in ("tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id"))
        if any(not value for value in keys):
            raise ValueError("agent_state_identity_invalid")
        now = _utc(); ttl = int(ttl_seconds or self._ttl_seconds); expires = now + timedelta(seconds=ttl)
        with self._lock, self._connect() as connection:
            old = connection.execute("SELECT revision FROM agent_states WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? AND purchase_context_id=?", keys).fetchone()
            revision = int(old[0]) if old else 0
            if expected_revision is not None and revision != expected_revision:
                raise ValueError("agent_state_revision_conflict")
            revision += 1
            state_id = f"as-{uuid4().hex}"
            protected = self._protect(dict(state))
            connection.execute("""INSERT INTO agent_states(state_id,tenant_id,shop_id,buyer_id,chat_id,purchase_context_id,state_protected,revision,expires_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,shop_id,buyer_id,chat_id,purchase_context_id) DO UPDATE SET state_protected=excluded.state_protected,revision=excluded.revision,expires_at=excluded.expires_at,updated_at=excluded.updated_at""", (state_id, *keys, protected, revision, expires.isoformat(), now.isoformat()))
        return {**dict(state), "revision": revision, "expires_at": expires.isoformat()}

    def context_for_event(self, body: Mapping[str, Any]) -> dict[str, Any]:
        identity = _event_identity(body)
        if not all(identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")):
            return {"available": False, "facts": {}, "expired": False}
        return self.load_context(**{key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id")})

    def record_canonical_event(self, body: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any] | None:
        identity = _event_identity(body)
        return self._record_result(identity, result, source="canonical_image")

    def record_legacy_event(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        identity = payload.get("identity") if isinstance(payload.get("identity"), Mapping) else {}
        envelope = payload.get("envelope") if isinstance(payload.get("envelope"), Mapping) else {}
        recognition = payload.get("recognition") if isinstance(payload.get("recognition"), Mapping) else {}
        quote = payload.get("quote") if isinstance(payload.get("quote"), Mapping) else {}
        facts = self._facts_from_values(recognition, quote)
        updates = payload.get("fact_updates") if isinstance(payload.get("fact_updates"), Mapping) else {}
        facts = merge_conversation_facts(facts, updates)
        if not facts:
            return None
        return self.save(
            tenant_id=str(identity.get("tenant_id") or ""), shop_id=str(identity.get("shop_id") or ""),
            buyer_id=str(identity.get("buyer_id") or ""), chat_id=str(identity.get("chat_id") or ""),
            purchase_context_id=str(identity.get("purchase_context_id") or self._context_from_envelope(envelope, identity)),
            facts=facts, source=str(payload.get("source") or "legacy_image"),
            event_id=str(identity.get("event_id") or "") or None,
            message_id=str(identity.get("message_id") or "") or None,
            observed_at=_event_datetime(envelope),
        )

    def save_patch_for_event(
        self, body: Mapping[str, Any], facts: Mapping[str, Any], *, source: str = "legacy_text_patch",
    ) -> dict[str, Any] | None:
        identity = _event_identity(body)
        if not all(identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")):
            return None
        return self.save(
            tenant_id=identity["tenant_id"], shop_id=identity["shop_id"], buyer_id=identity["buyer_id"],
            chat_id=identity["chat_id"],
            purchase_context_id=identity["purchase_context_id"] or f"chat:{identity['chat_id']}",
            facts=facts, source=source, event_id=identity["event_id"] or None,
            message_id=identity["message_id"] or None, observed_at=_event_datetime(body.get("envelope")),
        )

    def _record_result(self, identity: Mapping[str, str], result: Mapping[str, Any], *, source: str) -> dict[str, Any] | None:
        recognition = result.get("recognition") if isinstance(result.get("recognition"), Mapping) else {}
        quote = result.get("quote") if isinstance(result.get("quote"), Mapping) else {}
        facts = self._facts_from_values(recognition, quote)
        if not facts or not all(identity.get(key) for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")):
            return None
        return self.save(
            tenant_id=identity["tenant_id"], shop_id=identity["shop_id"], buyer_id=identity["buyer_id"],
            chat_id=identity["chat_id"], purchase_context_id=identity.get("purchase_context_id") or f"chat:{identity['chat_id']}",
            facts=facts, source=source, event_id=identity.get("event_id") or None,
            message_id=identity.get("message_id") or None,
        )

    @staticmethod
    def _context_from_envelope(envelope: Mapping[str, Any], identity: Mapping[str, Any]) -> str:
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        return str(payload.get("itemId") or payload.get("item_id") or f"chat:{identity.get('chat_id')}").strip()

    @staticmethod
    def _facts_from_values(recognition: Mapping[str, Any], quote: Mapping[str, Any]) -> dict[str, Any]:
        aliases = {
            "city": ("city", "city_text", "matched_city_name"),
            "cinema": ("cinema", "cinema_name", "cinema_text", "matched_cinema_name"),
            "cinema_address": ("cinema_address", "address"),
            "movie": ("movie", "movie_name", "matched_movie_name"),
            "quote_date": ("quote_date", "show_date", "date_text", "date"),
            "showtime_start": ("showtime_start", "start_time", "showtime"),
            "showtime_end": ("showtime_end", "end_time"),
            "hall": ("hall", "hall_name", "matched_hall_name"),
            "dimension": ("dimension", "format"),
            "language": ("language",),
            "seat_request_type": ("seat_request_type", "request_type"),
        }
        result: dict[str, Any] = {}
        for target, names in aliases.items():
            for name in names:
                value = quote.get(name) if name in quote else recognition.get(name)
                if value is not None and str(value).strip():
                    result[target] = value.isoformat() if isinstance(value, datetime) else str(value).strip()
                    break
        seats = quote.get("selected_seats") if isinstance(quote.get("selected_seats"), list) else recognition.get("selected_seats")
        if isinstance(seats, list) and seats:
            labels: list[str] = []
            for item in seats:
                if isinstance(item, Mapping):
                    label = item.get("seat_label") or item.get("seat_number") or item.get("seat_no") or item.get("seatName")
                else:
                    label = item
                if str(label or "").strip():
                    labels.append(str(label).strip())
            if labels:
                result["selected_seats"] = list(dict.fromkeys(labels))
                result.setdefault("seat_request_type", "EXACT_SEATS")
        shows = recognition.get("candidate_shows")
        if isinstance(shows, list):
            normalized_shows = []
            for item in shows[:10]:
                if not isinstance(item, Mapping):
                    continue
                show = {
                    key: str(item[key]).strip()
                    for key in ("show_id", "start_time", "end_time", "hall_name", "dimension", "language")
                    if item.get(key) not in (None, "")
                }
                if show.get("start_time"):
                    normalized_shows.append(show)
            if normalized_shows:
                result["candidate_shows"] = normalized_shows
        return {key: value for key, value in result.items() if key in _ALLOWED_FACTS}

    @staticmethod
    def _identity(*values: str) -> tuple[str, str, str, str, str]:
        normalized = tuple(str(value or "").strip() for value in values)
        if any(not value or len(value) > 240 for value in normalized):
            raise ValueError("conversation_fact_identity_invalid")
        return normalized  # type: ignore[return-value]

    def _sanitize_facts(self, facts: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(facts, Mapping):
            raise ValueError("conversation_facts_invalid")
        result: dict[str, Any] = {}
        for key, value in facts.items():
            name = str(key)
            if name not in _ALLOWED_FACTS or value is None or value == "":
                continue
            if name == "selected_seats":
                if not isinstance(value, list):
                    continue
                labels = []
                for item in value:
                    label = item.get("seat_label") if isinstance(item, Mapping) else item
                    label = label or (item.get("seat_number") if isinstance(item, Mapping) else None)
                    if str(label or "").strip():
                        labels.append(str(label).strip()[:80])
                if labels:
                    result[name] = list(dict.fromkeys(labels))[:30]
                continue
            if name == "ticket_count":
                if type(value) is int and 1 <= value <= 20:
                    result[name] = value
                continue
            if name == "showtime_ordinal":
                if type(value) is int and 1 <= value <= 10:
                    result[name] = value
                continue
            if name == "candidate_shows":
                if isinstance(value, list):
                    fields = ("show_id", "start_time", "end_time", "hall_name", "dimension", "language")
                    result[name] = [
                        {key: _text(item.get(key), limit=120) for key in fields if _text(item.get(key), limit=120)}
                        for item in value[:10] if isinstance(item, Mapping) and _text(item.get("start_time"), limit=120)
                    ]
                continue
            if name == "verified":
                if type(value) is bool:
                    result[name] = value
                continue
            text = _text(value)
            if text:
                result[name] = text
        return result

    def _protect(self, value: Mapping[str, Any]) -> str:
        return self._protector.protect(json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True))

    def _row_view(self, row: Mapping[str, Any] | None) -> dict[str, Any]:
        if row is None:
            return {}
        raw = dict(row)
        try:
            facts = json.loads(self._protector.unprotect(str(raw.get("facts_protected") or "")))
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeError):
            facts = {}
        return {
            "fact_id": raw.get("fact_id"), "tenant_id": raw.get("tenant_id"),
            "shop_id": raw.get("shop_id"), "buyer_id": raw.get("buyer_id"),
            "chat_id": raw.get("chat_id"), "purchase_context_id": raw.get("purchase_context_id"),
            "source": raw.get("source"), "fact_tier": raw.get("fact_tier"),
            "facts": facts if isinstance(facts, dict) else {}, "event_id": raw.get("event_id"),
            "message_id": raw.get("message_id"), "observed_at": raw.get("observed_at"),
            "expires_at": raw.get("expires_at"), "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
        }


def _event_datetime(envelope: object) -> datetime | None:
    if not isinstance(envelope, Mapping):
        return None
    raw = envelope.get("ts") or envelope.get("timestamp")
    if isinstance(raw, (int, float)):
        numeric = float(raw)
        if numeric > 10_000_000_000:
            numeric /= 1000
        if numeric > 0:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
    return None


def reference_now() -> datetime:
    return datetime.now(timezone.utc)
