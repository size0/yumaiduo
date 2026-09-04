from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .rules_first_state_store import SqliteTransactionStateStore
from .rules_first_store import RulesFirstStore
from .settings_store import SecretProtector, default_secret_protector
from .transaction_state_store import TransactionState, TransactionStateStore
from .quote_record_store import QuoteRecordStore


_TERMINAL = frozenset({"COMPLETED", "CANCELLED", "REFUNDED"})
_ORDER_REQUIRED = frozenset({
    "ORDER_BOUND", "PRICE_CHANGING", "WAITING_PAYMENT", "PAID_WAITING_FULFILLMENT",
    "WAITING_WPLUS_MARK", "READY_FOR_MANUAL_TICKETING", "FULFILLMENT_IN_PROGRESS",
    "TICKET_SENT", "REFUND_PENDING",
})


@dataclass(frozen=True)
class MigrationReport:
    examined: int
    importable: int
    ambiguous: int
    imported: int
    manual_tasks_created: int
    dry_run: bool


@dataclass(frozen=True)
class QuoteMigrationReport:
    examined: int
    importable: int
    ambiguous: int
    imported: int
    dry_run: bool


def migrate_legacy_quote_records(
    source: Path, destination: Path, *, apply: bool = False,
    protector: SecretProtector | None = None,
) -> QuoteMigrationReport:
    """Import legacy encrypted quote JSON as read-only snapshots.

    Records without an authoritative provider amount, exact seats, or expiry
    are reported as ambiguous and never promoted to an active quote.
    """
    protection = protector or default_secret_protector()
    legacy = QuoteRecordStore(source, protector=protection)
    records = legacy._read_records()
    candidates: list[dict[str, object]] = []
    ambiguous = 0
    for record in records:
        amount = record.get("provider_amount_fen") or record.get("providerAmountFen")
        seats = record.get("selected_seats") or record.get("selected_seats_json")
        if isinstance(seats, str):
            try:
                seats = json.loads(seats)
            except (TypeError, ValueError):
                seats = None
        if (
            not isinstance(seats, list) or not seats or not isinstance(amount, int) or amount <= 0
            or not str(record.get("quote_id") or record.get("record_id") or "").strip()
            or not str(record.get("tenant_id") or "").strip()
        ):
            ambiguous += 1
            continue
        created = str(record.get("created_at") or datetime.now(timezone.utc).isoformat())
        quote_id = str(record.get("quote_id") or record.get("record_id") or "").strip()
        material = {"record": record, "quote_id": quote_id}
        quote_hash = str(record.get("quote_hash") or hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest())
        try:
            expiry = datetime.fromisoformat(created).astimezone(timezone.utc) + timedelta(minutes=10)
        except ValueError:
            expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
        candidates.append({
            "quote_id": quote_id, "tenant_id": record.get("tenant_id"),
            "conversation_id": record.get("chat_id"), "quote_hash": quote_hash,
            "generation": int(record.get("quote_generation") or 1),
            "expires_at": str(record.get("quote_expires_at") or expiry.isoformat()), "provider_amount_fen": amount, "buyer_amount_fen": record.get("buyer_amount_fen") or amount,
            "seats": seats, "show_id": record.get("provider_show_id") or record.get("show_id"),
            "preflight_verified": False, "legacy_read_only": True,
        })
    imported = 0
    if apply:
        store = RulesFirstStore(destination, protector=protection)
        for snapshot in candidates:
            try:
                store.save_selected_seat_quote(snapshot)
            except (TypeError, ValueError):
                continue
            imported += 1
    return QuoteMigrationReport(
        examined=len(records), importable=len(candidates), ambiguous=ambiguous,
        imported=imported, dry_run=not apply,
    )


def migrate_legacy_transaction_states(
    source: Path, destination: Path, *, apply: bool = False,
    protector: SecretProtector | None = None,
) -> MigrationReport:
    """Read one encrypted JSON snapshot; never replay messages or platform writes."""
    protection = protector or default_secret_protector()
    legacy = TransactionStateStore(source, protector=protection)
    records = [TransactionState.model_validate(item) for item in legacy._read()]
    ambiguous = [
        state for state in records
        if state.flow_state not in _TERMINAL
        and (
            (state.flow_state in _ORDER_REQUIRED and not state.order_id)
            or (state.flow_state in {"CONFIRMED", "ORDER_BOUND", "PRICE_CHANGING"} and not state.confirmed_quote_record_id)
        )
    ]
    imported = 0
    manual_tasks = 0
    if apply:
        states = SqliteTransactionStateStore(destination, protector=protection)
        runtime = RulesFirstStore(destination, protector=protection)
        ambiguous_ids = {state.state_id for state in ambiguous}
        for original in records:
            state = original
            if original.state_id in ambiguous_ids:
                state = original.model_copy(update={
                    "flow_state": "MANUAL_HOLD",
                    "last_transition_code": "legacy_migration_ambiguous",
                })
            if states.import_legacy_state(state):
                imported += 1
            if original.state_id in ambiguous_ids:
                runtime.create_manual_task(
                    tenant_id=state.tenant_id, shop_id=state.shop_id,
                    buyer_id=state.buyer_id, chat_id=state.chat_id,
                    transaction_id=state.state_id, transaction_revision=state.revision,
                    reason="legacy_migration_ambiguous",
                    details={"source": str(source), "replayed": False},
                )
                manual_tasks += 1
    return MigrationReport(
        examined=len(records), importable=len(records) - len(ambiguous), ambiguous=len(ambiguous),
        imported=imported, manual_tasks_created=manual_tasks, dry_run=not apply,
    )
