from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rules_first_store import RulesFirstStore
from .settings_store import SecretProtector, default_secret_protector
from .transaction_state_store import (
    _ALLOWED_TRANSITIONS,
    _MUTABLE_FIELDS,
    StateRevisionConflict,
    TransactionState,
    TransactionStateStore,
)


class SqliteTransactionStateStore:
    """Production New-flow transaction state in the RulesFirst DB."""

    authority_name = "rules_first_sqlite"

    def __init__(
        self, path: Path, *, protector: SecretProtector | None = None,
        max_event_ids: int = 200,
    ) -> None:
        self._path = path
        self._protector = protector or default_secret_protector()
        self._max_event_ids = max(1, min(int(max_event_ids), 1_000))
        RulesFirstStore(path, protector=self._protector)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _protect(self, value: dict[str, Any]) -> str:
        return self._protector.protect(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def _unprotect(self, value: str) -> dict[str, Any]:
        payload = json.loads(self._protector.unprotect(value))
        if not isinstance(payload, dict):
            raise ValueError("transaction_state_payload_invalid")
        return payload

    @staticmethod
    def _identity(tenant_id: str, shop_id: str, buyer_id: str, chat_id: str) -> tuple[str, str, str, str]:
        return TransactionStateStore._identity(tenant_id, shop_id, buyer_id, chat_id)

    @staticmethod
    def _state_id(identity: tuple[str, str, str, str], generation: int = 1) -> str:
        if generation == 1:
            return TransactionStateStore._state_id(identity)
        material = "\0".join((*identity, f"generation:{generation}"))
        return "ts-" + hashlib.sha256(material.encode()).hexdigest()[:40]

    def get(self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str) -> TransactionState | None:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        with self._connect() as connection:
            row = connection.execute(
                """SELECT state_protected FROM transactions
                   WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? AND is_active=1
                   ORDER BY generation DESC LIMIT 1""",
                identity,
            ).fetchone()
        return TransactionState.model_validate(self._unprotect(row["state_protected"])) if row else None

    def find_by_order(self, *, tenant_id: str, order_id: str) -> TransactionState | None:
        tenant = str(tenant_id or "").strip()
        order = str(order_id or "").strip()
        if not tenant or not order:
            return None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state_protected FROM transactions WHERE tenant_id=? AND order_id=? ORDER BY generation DESC,updated_at DESC",
                (tenant, order),
            ).fetchall()
        return TransactionState.model_validate(self._unprotect(rows[0]["state_protected"])) if len(rows) == 1 else None

    def get_or_create(self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str) -> TransactionState:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT state_protected FROM transactions
                   WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? AND is_active=1
                   ORDER BY generation DESC LIMIT 1""",
                identity,
            ).fetchone()
            if row is not None:
                connection.commit()
                return TransactionState.model_validate(self._unprotect(row["state_protected"]))
            transaction_id = self._state_id(identity)
            now = datetime.now(timezone.utc).isoformat()
            state = TransactionState(
                state_id=transaction_id, tenant_id=identity[0], shop_id=identity[1],
                buyer_id=identity[2], chat_id=identity[3], generation=1, updated_at=now,
            )
            connection.execute(
                """INSERT INTO transactions(
                    transaction_id,tenant_id,shop_id,buyer_id,chat_id,generation,revision,
                    flow_state,order_id,state_protected,updated_at
                ) VALUES(?,?,?,?,?,1,?,?,?,?,?)""",
                (
                    transaction_id, *identity, state.revision, state.flow_state, state.order_id,
                    self._protect(state.model_dump()), now,
                ),
            )
            connection.commit()
        return state

    def import_legacy_state(self, state: TransactionState) -> bool:
        """Import one current-state snapshot without replaying any historical event."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM transactions WHERE transaction_id=?", (state.state_id,),
            ).fetchone()
            if exists is not None:
                connection.commit()
                return False
            connection.execute(
                """INSERT INTO transactions(
                    transaction_id,tenant_id,shop_id,buyer_id,chat_id,generation,revision,
                    flow_state,order_id,state_protected,updated_at
                ) VALUES(?,?,?,?,?,1,?,?,?,?,?)""",
                (
                    state.state_id, state.tenant_id, state.shop_id, state.buyer_id, state.chat_id,
                    state.revision, state.flow_state, state.order_id,
                    self._protect(state.model_dump()), state.updated_at,
                ),
            )
            connection.execute(
                """INSERT INTO state_transitions(
                    transaction_id,tenant_id,event_id,revision_before,revision_after,state_before,state_after,
                    transition_code,evidence_protected,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    state.state_id, state.tenant_id, f"migration:{state.state_id}", state.revision,
                    state.revision, state.flow_state, state.flow_state, "legacy_snapshot_imported",
                    self._protect({"source": "encrypted_json_snapshot", "replayed": False}), state.updated_at,
                ),
            )
            connection.commit()
        return True

    def start_new_generation(
        self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str,
        expected_revision: int, event_id: str, reason: str,
    ) -> TransactionState:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        normalized_event_id = str(event_id or "").strip()
        normalized_reason = str(reason or "").strip()
        if not normalized_event_id or not normalized_reason:
            raise ValueError("transaction_generation_evidence_invalid")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM transactions
                   WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? AND is_active=1
                   ORDER BY generation DESC LIMIT 1""",
                identity,
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ValueError("transaction_generation_current_missing")
            current = TransactionState.model_validate(self._unprotect(row["state_protected"]))
            if current.revision != expected_revision:
                connection.rollback()
                raise StateRevisionConflict("transaction_state_revision_conflict")
            terminal_order = current.flow_state in {"COMPLETED", "CANCELLED", "REFUNDED"}
            if (
                (current.order_id and not terminal_order)
                or current.price_change_status in {"pending", "submitted", "unknown"}
            ):
                connection.rollback()
                raise ValueError("transaction_generation_has_active_order")
            generation = int(row["generation"]) + 1
            transaction_id = self._state_id(identity, generation)
            fresh = TransactionState(
                state_id=transaction_id, tenant_id=identity[0], shop_id=identity[1],
                buyer_id=identity[2], chat_id=identity[3], generation=generation,
                flow_state="COLLECTING", quote_status="collecting",
                last_transition_code="generation_started", processed_event_ids=[normalized_event_id],
                updated_at=now,
            )
            connection.execute(
                """UPDATE transactions SET is_active=0,closed_at=?,close_reason=?,updated_at=?
                   WHERE transaction_id=? AND is_active=1""",
                (now, normalized_reason[:120], now, current.state_id),
            )
            connection.execute(
                """INSERT INTO transactions(
                    transaction_id,tenant_id,shop_id,buyer_id,chat_id,generation,revision,
                    flow_state,order_id,state_protected,is_active,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)""",
                (
                    transaction_id, *identity, generation, fresh.revision, fresh.flow_state,
                    fresh.order_id, self._protect(fresh.model_dump()), now,
                ),
            )
            connection.execute(
                """INSERT INTO state_transitions(
                    transaction_id,tenant_id,event_id,revision_before,revision_after,state_before,state_after,
                    transition_code,evidence_protected,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    transaction_id, identity[0], normalized_event_id, 0, 0, "NEW", "COLLECTING",
                    "generation_started", self._protect({
                        "reason": normalized_reason, "superseded_transaction_id": current.state_id,
                        "superseded_generation": current.generation,
                    }), now,
                ),
            )
            connection.commit()
        return fresh

    def transition(
        self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str,
        expected_revision: int, event_id: str, transition_code: str,
        flow_state: str, updates: dict[str, Any], allow_compatible_bootstrap: bool = False,
    ) -> TransactionState:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        normalized_event_id = str(event_id or "").strip()
        normalized_code = str(transition_code or "").strip()
        if not normalized_event_id or len(normalized_event_id) > 240:
            raise ValueError("transaction_state_event_id_invalid")
        if not normalized_code or len(normalized_code) > 120:
            raise ValueError("transaction_state_transition_code_invalid")
        if set(updates) - _MUTABLE_FIELDS:
            raise ValueError("transaction_state_update_field_invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT state_protected FROM transactions
                   WHERE tenant_id=? AND shop_id=? AND buyer_id=? AND chat_id=? AND is_active=1
                   ORDER BY generation DESC LIMIT 1""",
                identity,
            ).fetchone()
            current = (
                TransactionState.model_validate(self._unprotect(row["state_protected"]))
                if row else TransactionState(
                    state_id=self._state_id(identity), tenant_id=identity[0], shop_id=identity[1],
                    buyer_id=identity[2], chat_id=identity[3], generation=1,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            transaction_id = current.state_id
            if normalized_event_id in current.processed_event_ids:
                connection.commit()
                return current
            if current.revision != expected_revision:
                connection.rollback()
                raise StateRevisionConflict("transaction_state_revision_conflict")
            compatible_bootstrap = (
                current.revision == 0 and current.flow_state == "NEW"
                and (allow_compatible_bootstrap or normalized_code.startswith("compat.bootstrap."))
            )
            if flow_state not in _ALLOWED_TRANSITIONS[current.flow_state] and not compatible_bootstrap:
                connection.rollback()
                raise ValueError("transaction_state_transition_invalid")
            payload = current.model_dump()
            payload.update(updates)
            payload.update({
                "revision": current.revision + 1,
                "flow_state": flow_state,
                "last_transition_code": normalized_code,
                "processed_event_ids": [*current.processed_event_ids, normalized_event_id][-self._max_event_ids :],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            updated = TransactionState.model_validate(payload)
            protected = self._protect(updated.model_dump())
            if row:
                connection.execute(
                    """UPDATE transactions SET revision=?,flow_state=?,order_id=?,state_protected=?,updated_at=?
                       WHERE transaction_id=?""",
                    (
                        updated.revision, updated.flow_state, updated.order_id, protected,
                        updated.updated_at, transaction_id,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO transactions(
                        transaction_id,tenant_id,shop_id,buyer_id,chat_id,generation,revision,
                        flow_state,order_id,state_protected,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        transaction_id, *identity, updated.generation, updated.revision,
                        updated.flow_state, updated.order_id, protected, updated.updated_at,
                    ),
                )
            connection.execute(
                """INSERT INTO state_transitions(
                    transaction_id,tenant_id,event_id,revision_before,revision_after,state_before,state_after,
                    transition_code,evidence_protected,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    transaction_id, identity[0], normalized_event_id, current.revision, updated.revision,
                    current.flow_state, updated.flow_state, normalized_code,
                    self._protect({"updates": updates}), updated.updated_at,
                ),
            )
            connection.commit()
        return updated
