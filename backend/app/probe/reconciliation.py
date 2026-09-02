from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .audit import ProbeAuditStore
from .errors import CREATE_UNKNOWN_UNRECOVERABLE_AUTOMATICALLY
from .canonical import OrderLookupResult
from .models import ProbeOrder, ProbeStatus
from .policy import ProbePolicy
from .probe_store import DurableProbeStore


class ProbeOrderLookupProvider(Protocol):
    async def lookup_probe_order(self, *, show_id: str, seat_ids: list[str], account_ref: str, since: str) -> OrderLookupResult: ...


class CreateUnknownReconciler:
    """Explicit lookup seam; unsupported providers remain in permanent safe hold."""

    def __init__(
        self,
        probe_store: DurableProbeStore,
        *,
        policy: ProbePolicy,
        audit: ProbeAuditStore | None = None,
    ) -> None:
        self._store = probe_store
        self._policy = policy
        self._audit = audit
        self.last_error_code: str | None = None

    async def reconcile(self, order: ProbeOrder, provider: Any) -> OrderLookupResult:
        lookup = getattr(provider, "lookup_probe_order", None)
        if not callable(lookup):
            self.last_error_code = CREATE_UNKNOWN_UNRECOVERABLE_AUTOMATICALLY
            result = OrderLookupResult(outcome="UNSUPPORTED")
        else:
            self.last_error_code = None
            try:
                result = await lookup(
                    show_id=order.show_id, seat_ids=list(order.seat_ids),
                    account_ref=order.account_ref, since=order.created_at,
                )
                if not isinstance(result, OrderLookupResult):
                    result = OrderLookupResult(outcome="LOOKUP_UNKNOWN")
            except Exception:
                result = OrderLookupResult(outcome="LOOKUP_UNKNOWN")
        self._record(order, result)
        if result.outcome == "ORDER_FOUND_LOCKED" and result.temporary_order_id:
            current = self._store.get(order.probe_id)
            if current and current.status == ProbeStatus.CREATE_UNKNOWN:
                self._store.update(
                    order.probe_id, expected_revision=current.revision,
                    temporary_order_reference=result.temporary_order_id,
                )
                self._store.transition(order.probe_id, ProbeStatus.LOCKED, locked_at=datetime.now(timezone.utc).isoformat())
        elif result.outcome == "ORDER_NOT_FOUND_CONFIRMED":
            # The show/account hold is intentionally not cleared here. A
            # separate reviewed resolver must confirm the provider's absence
            # semantics before releasing CREATE_UNKNOWN.
            self._record(order, "order_not_found_requires_review")
        return result

    def _record(self, order: ProbeOrder, result: OrderLookupResult | str) -> None:
        if self._audit is not None:
            payload = result.model_dump(mode="json") if isinstance(result, OrderLookupResult) else {}
            self._audit.record(order.probe_id, str(result), payload)


def unknown_hold_expires_at(policy: ProbePolicy, *, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return (current + timedelta(seconds=policy.unknown_create_hold_seconds)).isoformat()
