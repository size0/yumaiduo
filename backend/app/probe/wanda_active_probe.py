from __future__ import annotations

import asyncio
from typing import Any, Mapping, Protocol

from .account_pool import ProbeAccount
from .audit import ProbeAuditStore
from .errors import ProbeError
from .models import ProbeOrder, ProbeResult, ProbeSeatTypePrice, ProbeStatus
from .policy import ProbePolicy
from .probe_store import DurableProbeStore
from .release_tracker import ReleaseTracker
from .seat_selector import LiveSeat


class WandaProbeProvider(Protocol):
    fixture: bool

    async def create_order(self, *, account: ProbeAccount, show_id: str, seat_ids: list[str]) -> Mapping[str, Any]: ...
    async def order_status(self, *, temporary_order_reference: str) -> Mapping[str, Any]: ...
    async def activity(self, *, temporary_order_reference: str) -> Mapping[str, Any]: ...
    async def read_member_prices(self, *, activity: Mapping[str, Any], seats: list[LiveSeat]) -> list[Mapping[str, Any]]: ...
    async def cancel_order(self, *, temporary_order_reference: str) -> Mapping[str, Any]: ...
    async def available_seat_ids(self, *, show_id: str, seat_ids: list[str]) -> set[str]: ...


class WandaActiveProbe:
    """Fixture-only implementation of the frozen V3 lifecycle."""

    def __init__(
        self,
        provider: WandaProbeProvider,
        *,
        probe_store: DurableProbeStore,
        release_tracker: ReleaseTracker,
        policy: ProbePolicy,
        audit: ProbeAuditStore | None = None,
    ) -> None:
        if not getattr(provider, "fixture", False):
            raise ValueError("real_provider_forbidden_in_m2")
        self._provider = provider
        self._store = probe_store
        self._release_tracker = release_tracker
        self._policy = policy
        self._audit = audit

    def _record(self, probe_id: str, event: str, **payload: object) -> None:
        if self._audit is not None:
            self._audit.record(probe_id, event, payload)

    async def recover_pending(self, provider: WandaProbeProvider | None = None) -> list[str]:
        """Resume cleanup from durable state after a Backend restart."""
        provider = provider or self._provider
        recovered: list[str] = []
        for order in self._store.recoverable():
            if not order.temporary_order_reference:
                continue
            if order.status == ProbeStatus.RELEASE_VERIFIED:
                recovered.append(order.probe_id)
                continue
            release_verified, _ = await self._cleanup(order, order.temporary_order_reference, provider=provider)
            if release_verified:
                recovered.append(order.probe_id)
        return recovered

    async def run(self, order: ProbeOrder, seats: list[LiveSeat], account: ProbeAccount) -> ProbeResult:
        self._policy.ensure_allowed()
        self._record(order.probe_id, "probe_started", show_id=order.show_id, account_ref=account.account_ref)
        temporary_ref: str | None = None
        facts: list[ProbeSeatTypePrice] = []
        error_code: str | None = None
        try:
            response = await self._provider.create_order(
                account=account, show_id=order.show_id, seat_ids=[seat.seat_id for seat in seats],
            )
            temporary_ref = _order_reference(response)
            if temporary_ref:
                self._record(order.probe_id, "temporary_order_observed")
                current = self._store.get(order.probe_id)
                if current:
                    order = self._store.update(
                        order.probe_id, expected_revision=current.revision,
                        temporary_order_reference=temporary_ref,
                    )
            _verify_create(response)
            status = await self._provider.order_status(temporary_order_reference=temporary_ref or "")
            _verify_locked(status)
            self._store.transition(order.probe_id, ProbeStatus.LOCKED, locked_at=_now())
            activity = await self._provider.activity(temporary_order_reference=temporary_ref or "")
            _verify_activity(activity)
            raw_facts = await self._provider.read_member_prices(activity=activity, seats=seats)
            facts = _facts(raw_facts, seats)
            self._store.transition(order.probe_id, ProbeStatus.PRICE_READ, price_read_at=_now())
        except asyncio.CancelledError:
            # The finally block starts cleanup in a child task and shields it;
            # the caller's cancellation remains the result of this operation.
            raise
        except ProbeError as error:
            error_code = error.code
        except Exception:
            error_code = "probe_provider_failed"
        finally:
            if temporary_ref:
                cleanup = asyncio.create_task(self._cleanup(order, temporary_ref, provider=self._provider))
                try:
                    release_verified, cleanup_error = await asyncio.shield(cleanup)
                    if cleanup_error:
                        error_code = cleanup_error
                except Exception:
                    release_verified = False
                    error_code = "temporary_lock_release_unverified"
                latest = self._store.get(order.probe_id)
                if latest and latest.status not in {ProbeStatus.RELEASE_VERIFIED, ProbeStatus.RELEASE_UNVERIFIED}:
                    self._store.transition(order.probe_id, ProbeStatus.RELEASE_UNVERIFIED, error_code="temporary_lock_release_unverified")
            else:
                release_verified = False
                latest = self._store.get(order.probe_id)
                if latest and latest.status != ProbeStatus.FAILED:
                    self._store.transition(order.probe_id, ProbeStatus.FAILED, error_code=error_code or "probe_failed")
        latest = self._store.get(order.probe_id)
        release_verified = bool(latest and latest.status == ProbeStatus.RELEASE_VERIFIED)
        if error_code is None and not release_verified:
            error_code = "temporary_lock_release_unverified"
        return ProbeResult(
            probe_id=order.probe_id, show_id=order.show_id,
            status="SUCCESS" if error_code is None else "FAILED",
            seat_type_prices=facts, release_verified=release_verified, error_code=error_code,
        )

    async def _cleanup(
        self, order: ProbeOrder, temporary_ref: str, *, provider: WandaProbeProvider,
    ) -> tuple[bool, str | None]:
        current = self._store.get(order.probe_id)
        if current and current.status not in {ProbeStatus.CANCEL_REQUESTED, ProbeStatus.CANCEL_CONFIRMED, ProbeStatus.RELEASE_CHECKING, ProbeStatus.RELEASE_VERIFIED, ProbeStatus.RELEASE_UNVERIFIED}:
            self._store.transition(order.probe_id, ProbeStatus.CANCEL_REQUESTED, cancel_requested_at=_now())
        cancel_call_ok = True
        try:
            await provider.cancel_order(temporary_order_reference=temporary_ref)
        except Exception:
            cancel_call_ok = False
        status_confirmed = False
        try:
            status = await provider.order_status(temporary_order_reference=temporary_ref)
            status_confirmed = _cancel_confirmed(status)
        except Exception:
            status_confirmed = False
        current = self._store.get(order.probe_id)
        if status_confirmed and current and current.status == ProbeStatus.CANCEL_REQUESTED:
            self._store.transition(order.probe_id, ProbeStatus.CANCEL_CONFIRMED, cancel_confirmed_at=_now())
        self._record(order.probe_id, "cancel_requested")
        release_verified = await self._release_tracker.verify(
            order, provider, cancel_confirmed=cancel_call_ok and status_confirmed,
        )
        if release_verified:
            self._record(order.probe_id, "release_verified")
            self._store.release_show(order.show_id, order.probe_id)
        else:
            self._record(order.probe_id, "release_unverified")
            from datetime import datetime, timedelta, timezone
            expiry = (datetime.now(timezone.utc) + timedelta(seconds=self._policy.account_lease_ttl_seconds)).isoformat()
            self._store.mark_show_release_unverified(order.show_id, order.probe_id, lease_expires_at=expiry)
            self._release_tracker.schedule_reconciliation(order, provider)
        return release_verified, None if cancel_call_ok and status_confirmed and release_verified else "temporary_lock_release_unverified"


def _order_reference(response: Mapping[str, Any]) -> str | None:
    data = response.get("data") if isinstance(response.get("data"), Mapping) else response
    value = data.get("orderId") if isinstance(data, Mapping) else None
    return str(value).strip() if value is not None and str(value).strip() else None


def _verify_create(response: Mapping[str, Any]) -> None:
    data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
    if response.get("code") != 0 or data.get("bizCode") != 0 or not _order_reference(response):
        raise ProbeError("temporary_lock_state_unknown")


def _verify_locked(response: Mapping[str, Any]) -> None:
    if response.get("orderStatus") != 40 or int(response.get("lockSeatTime", -1)) < 0:
        raise ProbeError("temporary_lock_state_unknown")


def _verify_activity(response: Mapping[str, Any]) -> None:
    if response.get("able") is not True or "W+会员专享" not in str(response.get("name") or ""):
        raise ProbeError("activity_offers_failed")
    allot = response.get("allotSeat")
    if not isinstance(allot, Mapping) or not isinstance(allot.get("totalPayPrice"), int) or allot["totalPayPrice"] <= 0:
        raise ProbeError("wplus_price_unavailable")


def _facts(raw: list[Mapping[str, Any]], seats: list[LiveSeat]) -> list[ProbeSeatTypePrice]:
    if not raw:
        raise ProbeError("wplus_price_unavailable")
    result: list[ProbeSeatTypePrice] = []
    for item in raw:
        result.append(ProbeSeatTypePrice.model_validate(item))
    return result


def _cancel_confirmed(response: Mapping[str, Any]) -> bool:
    return response.get("orderStatus") == 60 and response.get("lockSeatTime") == -1


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class FixtureWandaProvider:
    fixture = True

    def __init__(
        self,
        *,
        member_price_cents: int | None = 3800,
        cancel_delay_seconds: float = 0,
        create_delay_seconds: float = 0,
        available_after_seconds: int = 0,
        create_error: str | None = None,
        create_order_id: bool = True,
        lock_status: int = 40,
        activity_available: bool = True,
        cancel_error: str | None = None,
        cancel_status: int = 60,
        cancel_lock_seat_time: int = -1,
        token_present: bool = True,
        is_wplus: bool = True,
        remaining: int = 3,
    ) -> None:
        self.member_price_cents = member_price_cents
        self.cancel_delay_seconds = cancel_delay_seconds
        self.create_delay_seconds = create_delay_seconds
        self.available_after_seconds = available_after_seconds
        self.create_error = create_error
        self.create_order_id = create_order_id
        self.lock_status = lock_status
        self.activity_available = activity_available
        self.cancel_error = cancel_error
        self.cancel_status = cancel_status
        self.cancel_lock_seat_time = cancel_lock_seat_time
        self.token_present = token_present
        self.is_wplus = is_wplus
        self.remaining = remaining
        self.calls: list[str] = []
        self.created = asyncio.Event()
        self._availability_calls = 0
        self._cancelled = False

    @classmethod
    def from_scenario(cls, scenario: Mapping[str, Any]) -> "FixtureWandaProvider":
        return cls(**{key: value for key, value in scenario.items() if key in {
            "member_price_cents", "create_delay_seconds", "available_after_seconds", "create_error", "create_order_id",
            "lock_status", "activity_available", "cancel_error", "cancel_status",
            "cancel_lock_seat_time", "token_present", "is_wplus", "remaining",
        }})

    async def create_order(self, *, account: ProbeAccount, show_id: str, seat_ids: list[str]) -> Mapping[str, Any]:
        self.calls.append("create_order")
        self.created.set()
        if self.create_delay_seconds:
            await asyncio.sleep(self.create_delay_seconds)
        if self.create_error:
            raise ProbeError(self.create_error)
        data: dict[str, Any] = {"bizCode": 0}
        if self.create_order_id:
            data["orderId"] = "fixture-order-1"
        return {"code": 0, "data": data}

    async def order_status(self, *, temporary_order_reference: str) -> Mapping[str, Any]:
        self.calls.append("order_status")
        if not self._cancelled:
            return {"orderStatus": self.lock_status, "lockSeatTime": 1}
        return {"orderStatus": self.cancel_status, "lockSeatTime": self.cancel_lock_seat_time}

    async def activity(self, *, temporary_order_reference: str) -> Mapping[str, Any]:
        self.calls.append("activity")
        return {
            "able": self.activity_available, "name": "W+会员专享",
            "allotSeat": {"totalPayPrice": self.member_price_cents or 0},
        }

    async def read_member_prices(self, *, activity: Mapping[str, Any], seats: list[LiveSeat]) -> list[Mapping[str, Any]]:
        self.calls.append("read_member_prices")
        if self.member_price_cents is None:
            raise ProbeError("wplus_price_unavailable")
        return [{
            "area_code": seat.area_code, "zone_type": seat.zone_type,
            "representative_seat_id": seat.seat_id,
            "original_price_cents": seat.original_price_cents or 5000,
            "member_price_cents": self.member_price_cents,
        } for seat in seats]

    async def cancel_order(self, *, temporary_order_reference: str) -> Mapping[str, Any]:
        self.calls.append("cancel_order")
        if self.cancel_delay_seconds:
            await asyncio.sleep(self.cancel_delay_seconds)
        if self.cancel_error:
            raise ProbeError(self.cancel_error)
        self._cancelled = True
        return {"ok": True}

    async def available_seat_ids(self, *, show_id: str, seat_ids: list[str]) -> set[str]:
        self.calls.append("available_seat_ids")
        self._availability_calls += 1
        elapsed = (0, 2, 5)[min(self._availability_calls - 1, 2)]
        return set(seat_ids) if elapsed >= self.available_after_seconds else set()
