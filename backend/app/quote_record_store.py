from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .settings_store import SecretProtector, default_secret_protector


class QuoteRecordStore:
    """Encrypted, tenant-scoped persistence for authoritative quote audit records.

    ``save`` remains the compatibility primitive for Legacy callers. V2 quote
    lifecycle callers must use ``save_quote`` and the query methods below so
    TTL, generation, supersession and eligibility have one authority.
    """

    _CURRENT_QUOTE_STATES = frozenset({"PREVIEW", "TRANSACTION_READY"})
    _V2_QUOTE_SOURCES = frozenset({"AUTO_PRICING", "MANUAL_OPERATOR", "wanda_pricing_v2"})
    _TRANSACTION_READY_STATE = "TRANSACTION_READY"

    def __init__(
        self,
        path: Path,
        *,
        protector: SecretProtector | None = None,
        max_records: int = 5_000,
    ) -> None:
        self._path = path
        self._protector = protector or default_secret_protector()
        self._max_records = max_records
        self._lock = RLock()

    def save(self, record: Mapping[str, Any]) -> dict[str, Any]:
        normalized = self._normalize(record)
        with self._lock:
            records = self._read_records()
            previous_same_id = next(
                (item for item in records if item.get("record_id") == normalized["record_id"]),
                None,
            )
            normalized = self._with_lineage(normalized, previous_same_id)
            records = [item for item in records if item.get("record_id") != normalized["record_id"]]
            # Every recognition attempt remains an independent candidate. A
            # failed retry must not invalidate a previously delivered quote.
            # A successful candidate records its predecessor, but promotion is
            # deferred until the replacement message is actually delivered.
            predecessor = self._latest_lineage_predecessor(
                records, normalized, delivered_only=True,
            )
            if normalized.get("status") in {None, "succeeded"} and predecessor is not None:
                normalized["supersedes_quote_id"] = predecessor["quote_id"]
            records.append(normalized)
            records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            self._write_records(records[: self._max_records])
        return dict(normalized)

    def save_quote(
        self,
        record: Mapping[str, Any],
        *,
        ttl_seconds: int = 1_800,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a V2 quote and own its complete lifecycle.

        This is deliberately an extension of the existing store rather than a
        second quote repository. The service layer supplies facts only; this
        method computes expiry, generation, terms hash and supersession while
        holding the same store lock.
        """
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 24 * 60 * 60:
            raise ValueError("quote_record_ttl_seconds_invalid")
        normalized = self._normalize(record)
        created = self._datetime(normalized.get("created_at")) or self._datetime(now)
        if created is None:
            raise ValueError("quote_record_created_at_invalid")
        created = created.astimezone(timezone.utc)
        normalized["created_at"] = created.isoformat()
        normalized["expires_at"] = (created + timedelta(seconds=ttl_seconds)).isoformat()
        normalized["seller_quote_expires_at"] = normalized["expires_at"]
        normalized["quote_expires_at"] = normalized["expires_at"]
        source = str(normalized.get("source") or "").strip()
        if source in {"wanda_pricing_v2", "AUTO_PRICING", "MANUAL_OPERATOR"}:
            provider_expiry = normalized.get("provider_preflight_expires_at")
            provider_preflight_verified = (
                provider_expiry is None or self._expires_after(provider_expiry, created)
            )
            normalized["provider_preflight_verified"] = provider_preflight_verified
            transaction_ready = bool(
                normalized.get("same_type_reference_only") is not True
                and not normalized.get("needs_ticket_count")
                and normalized.get("ticket_count") is not None
                and normalized.get("total_sell_price_fen") is not None
                and provider_preflight_verified
            )
            normalized["transaction_authorized"] = transaction_ready
            normalized["quote_state"] = "TRANSACTION_READY" if transaction_ready else "PREVIEW"
        if source == "MANUAL_OPERATOR":
            basis = str(normalized.get("price_basis") or "").strip()
            if basis not in {"UNIT", "TOTAL"}:
                raise ValueError("manual_quote_price_basis_invalid")
            count = normalized.get("ticket_count")
            if count is not None and (
                not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20
            ):
                raise ValueError("manual_quote_ticket_count_invalid")
            if basis == "TOTAL":
                total = normalized.get("total_sell_price_fen")
                if count is None or not isinstance(total, int) or isinstance(total, bool) or total <= 0:
                    raise ValueError("manual_quote_total_requires_count_and_total")
            if basis == "UNIT" and (
                not isinstance(normalized.get("unit_sell_price_fen"), int)
                or isinstance(normalized.get("unit_sell_price_fen"), bool)
                or normalized.get("unit_sell_price_fen") <= 0
            ):
                raise ValueError("manual_quote_unit_price_required")
            if basis == "UNIT":
                normalized["needs_ticket_count"] = normalized.get("ticket_count") is None
                if normalized.get("ticket_count") is None:
                    normalized["total_sell_price_fen"] = None
                    normalized["total_quote_cents"] = None
                else:
                    unit = normalized.get("unit_sell_price_fen")
                    if not isinstance(unit, int) or isinstance(unit, bool) or unit <= 0:
                        raise ValueError("manual_quote_unit_price_required")
                    normalized["total_sell_price_fen"] = unit * normalized["ticket_count"]
                    normalized["total_quote_cents"] = normalized["total_sell_price_fen"]
                transaction_ready = normalized["total_sell_price_fen"] is not None
                normalized["transaction_authorized"] = transaction_ready
                normalized["quote_state"] = "TRANSACTION_READY" if transaction_ready else "PREVIEW"
        identity = self._quote_identity(normalized)
        request_id = str(normalized.get("request_id") or "").strip()
        if not request_id:
            raise ValueError("quote_record_request_id_invalid")
        with self._lock:
            records = self._read_records()
            existing = next(
                (
                    item for item in records
                    if self._quote_identity(item) == identity
                    and str(item.get("request_id") or "").strip() == request_id
                ),
                None,
            )
            if existing is not None:
                if self._quote_idempotency_material(existing) != self._quote_idempotency_material(normalized):
                    raise ValueError("idempotency_request_conflict")
                return dict(existing)

            lineage = [
                item for item in records
                if self._quote_identity(item) == identity
                and str(item.get("purchase_context_id") or item.get("item_id") or "").strip()
                == str(normalized.get("purchase_context_id") or normalized.get("item_id") or "").strip()
                and self._is_current_quote(item)
                and not item.get("platform_order_id")
                and not item.get("order_id")
            ]
            normalized["generation"] = max(
                (self._positive_generation(item) for item in lineage), default=0,
            ) + 1
            normalized = self._with_lineage(normalized, None)
            normalized["quote_hash"] = normalized.get("terms_fingerprint")
            predecessor = self._latest_quote_predecessor(
                lineage, normalized,
                allow_same_terms=source == "MANUAL_OPERATOR",
            )
            active_manual = next(
                (
                    item for item in lineage
                    if item.get("source") == "MANUAL_OPERATOR"
                    and self._expires_after(item.get("expires_at"), created)
                ),
                None,
            )
            if source in {"wanda_pricing_v2", "AUTO_PRICING"} and active_manual is not None:
                # Preserve the manual authority as current while retaining the
                # automatic calculation as an audit record.
                normalized.update({
                    "status": "superseded",
                    "quote_state": "SUPERSEDED",
                    "transaction_authorized": False,
                    "invalidated_reason": "active_manual_quote_authority",
                    "superseded_by_quote_id": active_manual.get("quote_id"),
                })
                predecessor = None
            if predecessor is not None:
                normalized["supersedes_quote_id"] = predecessor.get("quote_id")

            records = [item for item in records if item.get("record_id") != normalized["record_id"]]
            records.append(normalized)
            if predecessor is not None:
                predecessor_id = predecessor.get("record_id")
                for item in records:
                    if item.get("record_id") != predecessor_id:
                        continue
                    item.update({
                        "status": "superseded",
                        "quote_state": "SUPERSEDED",
                        "transaction_authorized": False,
                        "superseded_by_quote_id": normalized.get("quote_id"),
                        "superseded_at": created.isoformat(),
                    })
                    break
            records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            self._write_records(records[: self._max_records])
        return dict(normalized)

    def list_current_quotes(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        at: datetime,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Return active V2 quotes; expiry/state decisions live in this store."""
        identity = self._quote_identity({
            "tenant_id": tenant_id, "shop_id": shop_id,
            "buyer_id": buyer_id, "chat_id": chat_id,
        })
        reference = self._datetime(at)
        if reference is None:
            raise ValueError("quote_query_datetime_invalid")
        with self._lock:
            records = [item for item in self._read_records() if self._quote_identity(item) == identity]
        active = [
            dict(item) for item in records
            if self._is_current_quote(item) and self._expires_after(item.get("expires_at"), reference)
        ]
        if active:
            return "ACTIVE_QUOTES_FOUND", active
        expired = any(
            self._is_current_quote(item) and not self._expires_after(item.get("expires_at"), reference)
            for item in records
        )
        return ("QUOTE_EXPIRED" if expired else "NO_QUOTE_EVER"), []

    def list_transaction_candidates(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        at: datetime,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Return only active, unbound, transaction-ready candidates."""
        identity = self._quote_identity({
            "tenant_id": tenant_id, "shop_id": shop_id,
            "buyer_id": buyer_id, "chat_id": chat_id,
        })
        reference = self._datetime(at)
        if reference is None:
            raise ValueError("quote_query_datetime_invalid")
        with self._lock:
            records = [item for item in self._read_records() if self._quote_identity(item) == identity]
        v2_records = [item for item in records if item.get("source") in self._V2_QUOTE_SOURCES]
        candidates = [
            dict(item) for item in v2_records
            if self._is_transaction_candidate(item, reference)
        ]
        if candidates:
            return "ACTIVE_TRANSACTION_QUOTES", candidates
        current = [item for item in v2_records if self._is_current_quote(item)]
        if any(
            item.get("quote_state") == "PREVIEW"
            and self._expires_after(item.get("expires_at"), reference)
            for item in current
        ):
            return "ONLY_PREVIEW_QUOTES", []
        if any(
            item.get("quote_state") == self._TRANSACTION_READY_STATE
            and not self._expires_after(item.get("expires_at"), reference)
            for item in current
        ):
            return "QUOTE_EXPIRED", []
        return ("NO_QUOTE_EVER" if not v2_records else "NO_ACTIVE_TRANSACTION_QUOTE"), []

    def is_quote_active(self, record: Mapping[str, Any], *, at: datetime) -> bool:
        """Evaluate current/expiry eligibility from the Quote authority."""
        reference = self._datetime(at)
        return reference is not None and self._is_current_quote(record) and self._expires_after(
            record.get("expires_at"), reference,
        )

    def get_bound_order_quote(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        platform_order_id: str,
    ) -> dict[str, Any] | None:
        identity = self._quote_identity({
            "tenant_id": tenant_id, "shop_id": shop_id,
            "buyer_id": buyer_id, "chat_id": chat_id,
        })
        wanted = str(platform_order_id or "").strip()
        if not wanted:
            return None
        with self._lock:
            for item in self._read_records():
                if self._quote_identity(item) != identity:
                    continue
                stored = str(item.get("platform_order_id") or item.get("order_id") or "").strip()
                if stored == wanted and item.get("quote_id"):
                    return dict(item)
        return None

    def find_by_request(
        self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        identity = self._quote_identity({
            "tenant_id": tenant_id, "shop_id": shop_id,
            "buyer_id": buyer_id, "chat_id": chat_id,
        })
        wanted = str(request_id or "").strip()
        if not wanted:
            return None
        with self._lock:
            selected = next(
                (
                    item for item in self._read_records()
                    if self._quote_identity(item) == identity
                    and str(item.get("request_id") or "").strip() == wanted
                ),
                None,
            )
        return dict(selected) if selected is not None else None

    def list(self, tenant_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            return []
        bounded_limit = max(1, min(int(limit), 500))
        with self._lock:
            return [
                dict(item)
                for item in self._read_records()
                if item.get("tenant_id") == tenant
            ][:bounded_limit]

    def get_record(self, *, tenant_id: str, record_id: str) -> dict[str, Any] | None:
        tenant = str(tenant_id or "").strip()
        wanted = str(record_id or "").strip()
        if not tenant or not wanted:
            return None
        with self._lock:
            selected = next((
                item for item in self._read_records()
                if item.get("tenant_id") == tenant and item.get("record_id") == wanted
            ), None)
        return dict(selected) if selected is not None else None

    def find_by_order(
        self, *, tenant_id: str, order_id: str, shop_id: str | None = None,
        buyer_id: str | None = None, chat_id: str | None = None,
    ) -> dict[str, Any] | None:
        wanted = {
            "tenant_id": str(tenant_id or "").strip(), "order_id": str(order_id or "").strip(),
            "shop_id": str(shop_id or "").strip(), "buyer_id": str(buyer_id or "").strip(),
            "chat_id": str(chat_id or "").strip(),
        }
        if not wanted["tenant_id"] or not wanted["order_id"]:
            return None
        with self._lock:
            for item in self._read_records():
                if item.get("status") not in {None, "succeeded"}:
                    continue
                if str(item.get("tenant_id") or "") != wanted["tenant_id"] or str(item.get("order_id") or "") != wanted["order_id"]:
                    continue
                if any(wanted[field] and str(item.get(field) or "") != wanted[field] for field in ("shop_id", "buyer_id", "chat_id")):
                    continue
                return dict(item)
        return None

    def find_recent(
        self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str,
        item_id: str | None, at: datetime, max_age_seconds: int = 900,
    ) -> dict[str, Any] | None:
        with self._lock:
            return self._find_recent_record(
                self._read_records(), tenant_id=tenant_id, shop_id=shop_id,
                buyer_id=buyer_id, chat_id=chat_id, item_id=item_id, at=at,
                max_age_seconds=max_age_seconds,
            )

    def confirm_latest(
        self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str,
        item_id: str | None, confirmation_id: str, ticket_count: int | None,
        confirmed_at: datetime, max_age_seconds: int = 900,
    ) -> dict[str, Any] | None:
        if ticket_count is not None and not 1 <= int(ticket_count) <= 20:
            return None
        with self._lock:
            records = self._read_records()
            recent = self._find_recent_record(
                records, tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id,
                chat_id=chat_id, item_id=item_id, at=confirmed_at,
                max_age_seconds=max_age_seconds,
            )
            if recent is None or recent.get("delivery_state") != "delivered":
                return None
            created_at = self._datetime(recent.get("created_at"))
            if created_at is None:
                return None
            previous_count = recent.get("confirmed_ticket_count") or recent.get("ticket_count")
            effective_count = int(ticket_count) if ticket_count is not None else (
                int(previous_count)
                if isinstance(previous_count, int) and not isinstance(previous_count, bool) and 1 <= previous_count <= 20
                else None
            )
            material = f'{recent["record_id"]}:{confirmation_id}:{effective_count or ""}'
            recent.update({
                "confirmation_id": str(confirmation_id),
                "confirmation_version": f"v4c-{hashlib.sha256(material.encode()).hexdigest()[:20]}",
                "confirmation_source": "buyer_message",
                "confirmation_event_id": str(confirmation_id),
                "confirmed_at": confirmed_at.astimezone(timezone.utc).isoformat(),
                "confirmed_ticket_count": effective_count,
                "quote_expires_at": (created_at + timedelta(seconds=max_age_seconds)).isoformat(),
            })
            normalized = self._normalize(recent)
            updated = [item for item in records if item.get("record_id") != normalized["record_id"]]
            updated.append(normalized)
            updated.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            self._write_records(updated[: self._max_records])
            return dict(normalized)

    def mark_delivered(
        self, *, tenant_id: str, record_id: str, delivered_at: datetime, message_id: str,
    ) -> dict[str, Any] | None:
        tenant = str(tenant_id or "").strip()
        wanted_record = str(record_id or "").strip()
        normalized_message = str(message_id or "").strip()
        if not tenant or not wanted_record or not normalized_message:
            return None
        with self._lock:
            records = self._read_records()
            selected = next((item for item in records if item.get("record_id") == wanted_record), None)
            if (
                selected is None
                or selected.get("tenant_id") != tenant
                or selected.get("status") not in {None, "succeeded"}
            ):
                return None
            selected.update({
                "delivery_state": "delivered",
                "delivered_at": delivered_at.astimezone(timezone.utc).isoformat(),
                "delivery_message_id": normalized_message,
            })
            normalized = self._normalize(selected)
            predecessor_id = str(normalized.get("supersedes_quote_id") or "").strip()
            if predecessor_id:
                predecessor = next(
                    (item for item in records if item.get("quote_id") == predecessor_id),
                    None,
                )
                if (
                    predecessor is not None
                    and predecessor.get("status") in {None, "succeeded"}
                    and predecessor.get("delivery_state") == "delivered"
                    # A confirmed/bound quote is immutable for its order.
                    and not predecessor.get("confirmation_version")
                    and not predecessor.get("order_id")
                ):
                    predecessor["invalidated_reason"] = "superseded_by_new_quote"
            self._write_records(records[: self._max_records])
            return dict(normalized)

    def confirm_for_order(
        self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str,
        item_id: str | None, order_id: str, order_created_at: datetime,
        ticket_count: int, confirmation_id: str, max_age_seconds: int = 900,
    ) -> dict[str, Any] | None:
        identity = tuple(str(value or "").strip() for value in (tenant_id, shop_id, buyer_id, chat_id))
        wanted_item = str(item_id or "").strip()
        wanted_order = str(order_id or "").strip()
        confirmation = str(confirmation_id or "").strip()
        if any(not value for value in identity) or not wanted_order or not confirmation:
            return None
        if not isinstance(ticket_count, int) or isinstance(ticket_count, bool) or not 1 <= ticket_count <= 20:
            return None
        order_time = order_created_at.astimezone(timezone.utc)
        with self._lock:
            records = self._read_records()
            duplicate = next((
                item for item in records
                if item.get("tenant_id") == identity[0]
                and item.get("order_id") == wanted_order
                and item.get("confirmation_source") == "order_created"
                and item.get("confirmation_event_id") == confirmation
            ), None)
            if duplicate is not None:
                return dict(duplicate)
            candidates: list[tuple[datetime, dict[str, Any]]] = []
            for item in records:
                actual = tuple(str(item.get(field) or "").strip() for field in (
                    "tenant_id", "shop_id", "buyer_id", "chat_id",
                ))
                if actual != identity:
                    continue
                record_item = str(item.get("item_id") or "").strip()
                if wanted_item and record_item != wanted_item:
                    continue
                created = self._datetime(item.get("created_at"))
                if created is None or not 0 < (order_time - created).total_seconds() <= max_age_seconds:
                    continue
                if item.get("status") not in {None, "succeeded"}:
                    # Failed recognition attempts remain audit records and are
                    # never candidates for order confirmation.
                    continue
                if item.get("delivery_state") != "delivered":
                    # A successful but undelivered replacement must not hide an
                    # older delivered quote.
                    continue
                if item.get("invalidated_reason"):
                    continue
                candidates.append((created, item))
            if not candidates:
                return None
            candidates.sort(key=lambda pair: pair[0], reverse=True)
            created, selected = candidates[0]
            delivered = self._datetime(selected.get("delivered_at"))
            count = selected.get("ticket_count")
            explicit_count_pending = bool(
                selected.get("confirmation_source") == "buyer_message"
                and selected.get("confirmation_version")
                and selected.get("confirmed_ticket_count") is None
            )
            count_matches = (
                isinstance(count, int) and not isinstance(count, bool) and count == ticket_count
            ) or explicit_count_pending
            existing_order = str(selected.get("order_id") or "").strip()
            if (
                selected.get("status") not in {None, "succeeded"}
                or selected.get("delivery_state") != "delivered"
                or delivered is None or delivered > order_time
                or not count_matches
                or selected.get("quote_scope") not in {"exact_seats", "area_probe", "area_preview"}
                or (existing_order and existing_order != wanted_order)
            ):
                return None
            expires_at = created + timedelta(seconds=max_age_seconds)
            if order_time >= expires_at:
                return None
            confirmation_source = "buyer_message" if explicit_count_pending else "order_created"
            confirmation_event_id = (
                str(selected.get("confirmation_event_id") or "").strip()
                if explicit_count_pending else confirmation
            )
            material = (
                f'{selected["record_id"]}:{confirmation_event_id}:{ticket_count}:'
                f'{confirmation_source}:{wanted_order}'
            )
            selected.update({
                "confirmation_id": confirmation_event_id,
                "confirmation_version": f"v4c-{hashlib.sha256(material.encode()).hexdigest()[:20]}",
                "confirmation_source": confirmation_source,
                "confirmation_event_id": confirmation_event_id,
                "confirmed_at": order_time.isoformat(),
                "confirmed_ticket_count": ticket_count,
                "quote_expires_at": expires_at.isoformat(),
                "order_id": wanted_order,
                "binding_state": "order_bound",
                "order_bound_at": order_time.isoformat(),
            })
            normalized = self._normalize(selected)
            self._write_records(records[: self._max_records])
            return dict(normalized)

    def bind_order(
        self, *, record_id: str, tenant_id: str, shop_id: str, buyer_id: str,
        chat_id: str, order_id: str, bound_at: datetime,
    ) -> dict[str, Any] | None:
        expected = tuple(str(value or "").strip() for value in (tenant_id, shop_id, buyer_id, chat_id))
        normalized_order_id = str(order_id or "").strip()
        if not str(record_id or "").strip() or not normalized_order_id or any(not value for value in expected):
            return None
        with self._lock:
            records = self._read_records()
            selected = next((item for item in records if item.get("record_id") == record_id), None)
            if selected is None:
                return None
            actual = tuple(str(selected.get(field) or "").strip() for field in ("tenant_id", "shop_id", "buyer_id", "chat_id"))
            existing_order_id = str(selected.get("order_id") or "").strip()
            if actual != expected or (existing_order_id and existing_order_id != normalized_order_id):
                return None
            selected.update({
                "order_id": normalized_order_id,
                "binding_state": "order_bound",
                "order_bound_at": bound_at.astimezone(timezone.utc).isoformat(),
            })
            normalized = self._normalize(selected)
            records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            self._write_records(records[: self._max_records])
            return dict(normalized)

    def find_confirmed(
        self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str,
        item_id: str | None, at: datetime,
    ) -> dict[str, Any] | None:
        recent = self.find_recent(
            tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id, chat_id=chat_id,
            item_id=item_id, at=at,
        )
        if (
            recent is None
            or recent.get("delivery_state") != "delivered"
            or not recent.get("confirmation_version")
        ):
            return None
        expires_at = self._datetime(recent.get("quote_expires_at"))
        count = recent.get("confirmed_ticket_count")
        if expires_at is None or at.astimezone(timezone.utc) >= expires_at:
            return None
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20:
            return None
        return recent

    @classmethod
    def _find_recent_record(
        cls, records: list[dict[str, Any]], *, tenant_id: str, shop_id: str,
        buyer_id: str, chat_id: str, item_id: str | None, at: datetime,
        max_age_seconds: int,
    ) -> dict[str, Any] | None:
        wanted = tuple(str(value or "").strip() for value in (tenant_id, shop_id, buyer_id, chat_id))
        reference = at.astimezone(timezone.utc)
        for item in records:
            actual = tuple(str(item.get(field) or "").strip() for field in ("tenant_id", "shop_id", "buyer_id", "chat_id"))
            if actual != wanted:
                continue
            record_item = str(item.get("item_id") or "").strip()
            wanted_item = str(item_id or "").strip()
            if wanted_item and record_item != wanted_item:
                continue
            created = cls._datetime(item.get("created_at"))
            if created is None or not 0 <= (reference - created).total_seconds() <= max_age_seconds:
                continue
            if item.get("status") == "failed":
                # A failed recognition is audit-only and cannot hide an older
                # delivered quote.
                continue
            if item.get("status") not in {None, "succeeded"}:
                continue
            if item.get("delivery_state") != "delivered":
                # An undelivered success is not usable yet; fall back to the
                # previous delivered candidate when one exists.
                continue
            if item.get("quote_scope") not in {"exact_seats", "area_probe", "area_preview"}:
                continue
            if item.get("invalidated_reason"):
                continue
            return dict(item)
        return None

    @classmethod
    def _quote_identity(cls, record: Mapping[str, Any]) -> tuple[str, str, str, str]:
        return tuple(
            str(record.get(field) or "").strip()
            for field in ("tenant_id", "shop_id", "buyer_id", "chat_id")
        )  # type: ignore[return-value]

    @classmethod
    def _is_current_quote(cls, record: Mapping[str, Any]) -> bool:
        return (
            record.get("quote_state") in cls._CURRENT_QUOTE_STATES
            and record.get("status") in {None, "succeeded"}
            and not record.get("invalidated_reason")
        )

    @classmethod
    def _is_transaction_candidate(cls, record: Mapping[str, Any], reference: datetime) -> bool:
        return (
            record.get("source") in cls._V2_QUOTE_SOURCES
            and cls._is_current_quote(record)
            and record.get("quote_state") == cls._TRANSACTION_READY_STATE
            and record.get("transaction_authorized") is True
            and record.get("total_sell_price_fen") is not None
            and (
                record.get("provider_preflight_expires_at") is None
                or cls._expires_after(record.get("provider_preflight_expires_at"), reference)
            )
            and not record.get("platform_order_id")
            and not record.get("order_id")
            and cls._expires_after(record.get("expires_at"), reference)
        )

    @classmethod
    def _latest_quote_predecessor(
        cls, records: list[dict[str, Any]], record: Mapping[str, Any],
        *, allow_same_terms: bool = False,
    ) -> dict[str, Any] | None:
        candidates = [
            item for item in records
            if item.get("quote_id") != record.get("quote_id")
            and (
                allow_same_terms
                or item.get("terms_fingerprint") != record.get("terms_fingerprint")
            )
        ]
        candidates.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return candidates[0] if candidates else None

    @staticmethod
    def _positive_generation(record: Mapping[str, Any]) -> int:
        value = record.get("generation")
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    @staticmethod
    def _quote_idempotency_material(record: Mapping[str, Any]) -> str:
        excluded = {
            "record_id", "quote_id", "quote_version", "quote_hash", "terms_fingerprint",
            "event_id", "recognition_id", "message_id", "created_at", "expires_at",
            "quote_expires_at", "seller_quote_expires_at", "generation", "supersedes_quote_id", "superseded_by_quote_id",
            "superseded_at", "invalidated_reason", "delivery_state", "delivered_at",
            "delivery_message_id", "binding_state", "order_bound_at", "order_id",
            "platform_order_id", "binding_request_id", "bound_at", "binding_revision",
            "binding_reason", "binding_history",
        }
        material = {key: value for key, value in record.items() if key not in excluded}
        return json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _expires_after(cls, value: object, reference: datetime) -> bool:
        expires = cls._datetime(value)
        return expires is not None and reference.astimezone(timezone.utc) < expires

    @staticmethod
    def _datetime(value: object) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _with_lineage(
        cls, record: dict[str, Any], previous_same_id: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if previous_same_id:
            for field in ("quote_id", "quote_version", "terms_fingerprint"):
                if not record.get(field) and previous_same_id.get(field):
                    record[field] = previous_same_id[field]
        record["quote_id"] = str(record.get("quote_id") or record["record_id"]).strip()
        fingerprint = cls._terms_fingerprint(record)
        if fingerprint:
            record["terms_fingerprint"] = fingerprint
        elif "terms_fingerprint" not in record:
            record["terms_fingerprint"] = None
        version_material = f'{record["quote_id"]}:{record.get("terms_fingerprint") or "unknown"}'
        record["quote_version"] = str(record.get("quote_version") or (
            f'qv-{hashlib.sha256(version_material.encode()).hexdigest()[:20]}'
        ))
        record.setdefault("supersedes_quote_id", None)
        record.setdefault("invalidated_reason", None)
        return record

    @classmethod
    def _latest_lineage_predecessor(
        cls, records: list[dict[str, Any]], record: Mapping[str, Any],
        *, delivered_only: bool = False,
    ) -> dict[str, Any] | None:
        identity_fields = ("tenant_id", "shop_id", "buyer_id", "chat_id", "item_id")
        wanted_identity = tuple(str(record.get(field) or "").strip() for field in identity_fields)
        created = cls._datetime(record.get("created_at"))
        if created is None:
            return None
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for item in records:
            if tuple(str(item.get(field) or "").strip() for field in identity_fields) != wanted_identity:
                continue
            item_created = cls._datetime(item.get("created_at"))
            if item_created is None or item_created >= created:
                continue
            if item.get("status") not in {None, "succeeded"}:
                continue
            if delivered_only and item.get("delivery_state") != "delivered":
                continue
            if item.get("invalidated_reason"):
                continue
            candidates.append((item_created, item))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        predecessor = candidates[0][1]
        if record.get("terms_fingerprint") and record.get("terms_fingerprint") == predecessor.get("terms_fingerprint"):
            return None
        return predecessor

    @staticmethod
    def _terms_fingerprint(record: Mapping[str, Any]) -> str | None:
        date_value = record.get("quote_date") or record.get("date_text")
        required = (
            record.get("city"), record.get("cinema"), record.get("movie"), date_value,
            record.get("showtime_start"), record.get("hall"), record.get("seat_display"),
        )
        if any(not str(value or "").strip() for value in required):
            return None
        def canonical(value: object) -> str:
            text = unicodedata.normalize("NFKC", str(value or "")).casefold()
            return re.sub(r"\\s+", " ", text).strip()
        material = {
            "item_id": canonical(record.get("item_id")),
            "city": canonical(record.get("city")),
            "cinema": canonical(record.get("cinema")),
            "movie": canonical(record.get("movie")),
            "date": canonical(date_value),
            "showtime_start": canonical(record.get("showtime_start")),
            "showtime_end": canonical(record.get("showtime_end")),
            "hall": canonical(record.get("hall")),
            "seat_display": canonical(record.get("seat_display")),
            "quote_scope": canonical(record.get("quote_scope")),
            "seat_zone_type": canonical(record.get("seat_zone_type")),
            "provider": canonical(record.get("provider")),
            "provider_route": canonical(record.get("provider_route")),
            "canonical_quote_route": canonical(record.get("canonical_quote_route")),
            "wanda_store_id": canonical(record.get("wanda_store_id")),
            "wanda_show_id": canonical(record.get("wanda_show_id")),
            "liangpiao_cinema_id": canonical(record.get("liangpiao_cinema_id")),
            "liangpiao_movie_id": canonical(record.get("liangpiao_movie_id")),
            "liangpiao_show_id": canonical(record.get("liangpiao_show_id")),
            "ticket_count": record.get("ticket_count"),
        }
        if record.get("source") == "MANUAL_OPERATOR":
            material.update({
                "source": "MANUAL_OPERATOR",
                "price_basis": canonical(record.get("price_basis")),
                "unit_sell_price_fen": record.get("unit_sell_price_fen"),
                "total_sell_price_fen": record.get("total_sell_price_fen"),
                "showtime_end": canonical(record.get("showtime_end")),
                "seat_request_type": canonical(record.get("seat_request_type")),
                "format": canonical(record.get("format")),
                "language": canonical(record.get("language")),
                "selected_seats": record.get("selected_seats") or [],
            })
            # A UNIT commitment remains valid when only the buyer's count is
            # supplied later. TOTAL commitments retain count as a term.
            if record.get("price_basis") == "UNIT":
                material["ticket_count"] = None
                material["total_sell_price_fen"] = None
        # V2 records opt into the extended existing terms fingerprint. Keep
        # legacy records compatible while ensuring V2 money and pricing-policy
        # changes cannot reuse the old quote hash.
        if record.get("pricing_rule_revision") is not None or record.get("pricing_rule_version"):
            material.update({
                "request_type": canonical(record.get("request_type")),
                "wanda_show_id": canonical(record.get("wanda_show_id")),
                "unit_sell_price_fen": record.get("unit_sell_price_fen"),
                "total_sell_price_fen": record.get("total_sell_price_fen"),
                "pricing_rule_revision": record.get("pricing_rule_revision"),
                "pricing_rule_version": canonical(record.get("pricing_rule_version")),
            })
        return f'tf-{hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]}'

    @staticmethod
    def _normalize(record: Mapping[str, Any]) -> dict[str, Any]:
        required = ("record_id", "tenant_id", "shop_id", "buyer_id", "chat_id", "created_at")
        normalized = dict(record)
        for field in required:
            value = str(normalized.get(field) or "").strip()
            if not value or len(value) > 200:
                raise ValueError(f"quote_record_{field}_invalid")
            normalized[field] = value
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 20_000:
            raise ValueError("quote_record_too_large")
        return json.loads(encoded)

    def _read_records(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            protected = payload.get("records_protected") if isinstance(payload, dict) else None
            records = json.loads(self._protector.unprotect(protected)) if isinstance(protected, str) and protected else []
        except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
            return []
        if not isinstance(records, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            try:
                normalized.append(self._with_lineage(dict(item), None))
            except (TypeError, ValueError):
                continue
        return normalized

    def _write_records(self, records: list[dict[str, Any]]) -> None:
        protected = self._protector.protect(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
        payload = {"version": 1, "records_protected": protected}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self._path)
