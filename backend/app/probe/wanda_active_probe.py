from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

from .account_pool import ProbeAccount
from .audit import ProbeAuditStore
from .canonical import (
    ActivityOffersResult,
    CancelResult,
    CreateOrderResult,
    OrderStatusResult,
    SeatAvailabilityResult,
)
from .errors import ProbeError
from .models import ProbeOrder, ProbeResult, ProbeSeatTypePrice, ProbeStatus
from .policy import ProbePolicy
from .probe_store import DurableProbeStore
from .release_tracker import ReleaseTracker
from .seat_selector import LiveSeat


class WandaProbeProvider(Protocol):
    fixture: bool

    async def create_probe_order(self, *, account: ProbeAccount, show_id: str, seat_ids: list[str]) -> CreateOrderResult: ...
    async def get_order_status(self, *, temporary_order_reference: str) -> OrderStatusResult: ...
    async def get_activity_offers(self, *, temporary_order_reference: str) -> ActivityOffersResult: ...
    async def cancel_probe_order(self, *, temporary_order_reference: str) -> CancelResult: ...
    async def get_available_seats(self, *, show_id: str, seat_ids: list[str]) -> SeatAvailabilityResult: ...


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
            if order.status == ProbeStatus.CREATE_UNKNOWN:
                # No safe order reference exists. A future provider may add
                # lookup_probe_order; until then this remains a manual/M3 gate.
                continue
            if not order.temporary_order_reference:
                continue
            release_verified, _, _ = await self._cleanup(order, order.temporary_order_reference, provider=provider)
            if release_verified:
                recovered.append(order.probe_id)
        return recovered

    async def run(self, order: ProbeOrder, seats: list[LiveSeat], account: ProbeAccount) -> ProbeResult:
        self._policy.ensure_allowed()
        self._record(order.probe_id, "probe_started", show_id=order.show_id, account_ref=account.account_ref)
        temporary_ref: str | None = None
        facts: list[ProbeSeatTypePrice] = []
        error_code: str | None = None
        cancel_confirmed = False
        try:
            created = await self._provider.create_probe_order(
                account=account, show_id=order.show_id, seat_ids=[seat.seat_id for seat in seats],
            )
            temporary_ref = created.temporary_order_id
            if temporary_ref:
                self._record(order.probe_id, "temporary_order_observed")
                current = self._store.get(order.probe_id)
                if current:
                    order = self._store.update(
                        order.probe_id, expected_revision=current.revision,
                        temporary_order_reference=temporary_ref,
                    )
            if created.outcome == "UNKNOWN":
                error_code = "create_unknown"
                self._mark_create_unknown(order.probe_id)
            else:
                _verify_create(created)
                status = await self._provider.get_order_status(temporary_order_reference=temporary_ref or "")
                _verify_locked(status)
                self._store.transition(order.probe_id, ProbeStatus.LOCKED, locked_at=_now())
                activity = await self._provider.get_activity_offers(temporary_order_reference=temporary_ref or "")
                _verify_activity(activity)
                facts = _facts_from_activity(activity, seats)
                self._store.transition(order.probe_id, ProbeStatus.PRICE_READ, price_read_at=_now())
        except asyncio.TimeoutError:
            error_code = "create_unknown"
            self._mark_create_unknown(order.probe_id)
        except asyncio.CancelledError:
            # finally starts cleanup in a child task; cancellation remains the
            # result of this operation while cleanup continues independently.
            raise
        except ProbeError as error:
            error_code = error.code
        except Exception:
            error_code = "probe_provider_failed"
        finally:
            if temporary_ref:
                cleanup = asyncio.create_task(self._cleanup(order, temporary_ref, provider=self._provider))
                try:
                    release_verified, cleanup_error, cancel_confirmed = await asyncio.shield(cleanup)
                    if cleanup_error:
                        error_code = cleanup_error
                except Exception:
                    release_verified = False
                    error_code = "temporary_lock_release_unverified"
                latest = self._store.get(order.probe_id)
                if latest and latest.status not in {ProbeStatus.RELEASE_VERIFIED, ProbeStatus.RELEASE_UNVERIFIED}:
                    self._store.transition(
                        order.probe_id, ProbeStatus.RELEASE_UNVERIFIED,
                        error_code="temporary_lock_release_unverified",
                    )
            elif error_code != "create_unknown":
                release_verified = False
                latest = self._store.get(order.probe_id)
                if latest and latest.status != ProbeStatus.FAILED:
                    self._store.transition(order.probe_id, ProbeStatus.FAILED, error_code=error_code or "probe_failed")
            else:
                release_verified = False
        latest = self._store.get(order.probe_id)
        release_verified = bool(latest and latest.status == ProbeStatus.RELEASE_VERIFIED)
        if error_code is None and not release_verified:
            error_code = "temporary_lock_release_unverified"
        return ProbeResult(
            probe_id=order.probe_id, show_id=order.show_id,
            status="SUCCESS" if error_code is None else "FAILED",
            seat_type_prices=facts, cancel_confirmed=cancel_confirmed,
            release_verified=release_verified,
            release_timing_class=self._release_tracker.last_timing_class if release_verified else "UNVERIFIED",
            error_code=error_code,
        )

    def _mark_create_unknown(self, probe_id: str) -> None:
        current = self._store.get(probe_id)
        if current and current.status == ProbeStatus.CREATED:
            self._store.transition(probe_id, ProbeStatus.CREATE_UNKNOWN, error_code="create_unknown")

    async def _cleanup(
        self, order: ProbeOrder, temporary_ref: str, *, provider: WandaProbeProvider,
    ) -> tuple[bool, str | None, bool]:
        current = self._store.get(order.probe_id)
        if current and current.status not in {
            ProbeStatus.CANCEL_REQUESTED, ProbeStatus.CANCEL_CONFIRMED,
            ProbeStatus.RELEASE_CHECKING, ProbeStatus.RELEASE_VERIFIED,
            ProbeStatus.RELEASE_UNVERIFIED,
        }:
            self._store.transition(order.probe_id, ProbeStatus.CANCEL_REQUESTED, cancel_requested_at=_now())
        cancel_call_ok = True
        try:
            cancel_response = await provider.cancel_probe_order(temporary_order_reference=temporary_ref)
            cancel_call_ok = cancel_response.accepted
        except Exception:
            cancel_call_ok = False
        status_confirmed = False
        try:
            status = await provider.get_order_status(temporary_order_reference=temporary_ref)
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
            expiry = (datetime.now(timezone.utc) + timedelta(seconds=self._policy.account_lease_ttl_seconds)).isoformat()
            self._store.mark_show_release_unverified(order.show_id, order.probe_id, lease_expires_at=expiry)
            self._release_tracker.schedule_reconciliation(order, provider)
        return (
            release_verified,
            None if cancel_call_ok and status_confirmed and release_verified else "temporary_lock_release_unverified",
            cancel_call_ok and status_confirmed,
        )


def _verify_create(response: CreateOrderResult) -> None:
    if response.outcome != "CONFIRMED" or response.code not in (0, "0") or response.biz_code not in (0, "0") or not response.temporary_order_id:
        raise ProbeError("temporary_lock_state_unknown")


def _verify_locked(response: OrderStatusResult) -> None:
    if response.order_status not in (40, "40") or response.lock_seat_time is None or response.lock_seat_time < 0:
        raise ProbeError("temporary_lock_state_unknown")


def _verify_activity(response: ActivityOffersResult) -> None:
    if not response.able or "W+会员专享" not in response.name:
        raise ProbeError("activity_offers_failed")
    if response.total_pay_price_cents is None and not response.seat_type_prices:
        raise ProbeError("wplus_price_unavailable")


def _facts_from_activity(response: ActivityOffersResult, seats: list[LiveSeat]) -> list[ProbeSeatTypePrice]:
    if response.seat_type_prices:
        return list(response.seat_type_prices)
    if response.member_price_cents is None and response.total_pay_price_cents is None:
        raise ProbeError("wplus_price_unavailable")
    member = response.member_price_cents or response.total_pay_price_cents
    return [ProbeSeatTypePrice(
        area_code=seat.area_code, zone_type=seat.zone_type,
        representative_seat_id=seat.seat_id,
        original_price_cents=seat.original_price_cents or _raise_original_price(),
        member_price_cents=member,
    ) for seat in seats]


def _raise_original_price() -> int:
    raise ProbeError("official_original_price_unavailable")


def _cancel_confirmed(response: OrderStatusResult) -> bool:
    return response.order_status in (60, "60") and response.lock_seat_time == -1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FixtureWandaProvider:
    """In-memory canonical provider used by M2/M2.5 tests only."""

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
        create_unknown: bool = False,
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
        self.create_unknown = create_unknown
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
        self._seats: list[str] = []

    @classmethod
    def from_scenario(cls, scenario: Mapping[str, Any]) -> "FixtureWandaProvider":
        return cls(**{key: value for key, value in scenario.items() if key in {
            "member_price_cents", "create_delay_seconds", "available_after_seconds", "create_error",
            "create_order_id", "create_unknown", "lock_status", "activity_available", "cancel_error",
            "cancel_status", "cancel_lock_seat_time", "token_present", "is_wplus", "remaining",
        }})

    async def create_probe_order(self, *, account: ProbeAccount, show_id: str, seat_ids: list[str]) -> CreateOrderResult:
        self.calls.append("create_order")
        self.created.set()
        self._seats = list(seat_ids)
        if self.create_delay_seconds:
            await asyncio.sleep(self.create_delay_seconds)
        if self.create_error:
            raise ProbeError(self.create_error)
        if self.create_unknown:
            return CreateOrderResult(outcome="UNKNOWN")
        return CreateOrderResult(
            code=0, biz_code=0,
            temporary_order_id="fixture-order-1" if self.create_order_id else None,
        )

    async def get_order_status(self, *, temporary_order_reference: str) -> OrderStatusResult:
        self.calls.append("order_status")
        if not self._cancelled:
            return OrderStatusResult(order_status=self.lock_status, lock_seat_time=1)
        return OrderStatusResult(order_status=self.cancel_status, lock_seat_time=self.cancel_lock_seat_time)

    async def get_activity_offers(self, *, temporary_order_reference: str) -> ActivityOffersResult:
        self.calls.append("activity")
        return ActivityOffersResult(
            able=self.activity_available, name="W+会员专享",
            total_pay_price_cents=self.member_price_cents,
            member_price_cents=self.member_price_cents,
        )

    async def cancel_probe_order(self, *, temporary_order_reference: str) -> CancelResult:
        self.calls.append("cancel_order")
        if self.cancel_delay_seconds:
            await asyncio.sleep(self.cancel_delay_seconds)
        if self.cancel_error:
            raise ProbeError(self.cancel_error)
        self._cancelled = True
        return CancelResult(accepted=True)

    async def get_available_seats(self, *, show_id: str, seat_ids: list[str]) -> SeatAvailabilityResult:
        self.calls.append("available_seat_ids")
        self._availability_calls += 1
        elapsed = (0, 2, 5)[min(self._availability_calls - 1, 2)]
        return SeatAvailabilityResult(
            available_seat_ids=set(seat_ids) if elapsed >= self.available_after_seconds else set(),
        )
