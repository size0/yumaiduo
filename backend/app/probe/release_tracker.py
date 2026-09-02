from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

from .models import ProbeOrder, ProbeStatus
from .probe_store import DurableProbeStore


class ReleaseProvider(Protocol):
    async def available_seat_ids(self, *, show_id: str, seat_ids: list[str]) -> set[str]: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    def __init__(self) -> None:
        self.elapsed_seconds = 0.0

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.elapsed_seconds, timezone.utc)

    async def sleep(self, seconds: float) -> None:
        self.elapsed_seconds += float(seconds)


class ReleaseTracker:
    def __init__(
        self,
        probe_store: DurableProbeStore,
        *,
        clock: Clock | None = None,
        delays_seconds: tuple[float, ...] = (0.0, 2.0, 5.0),
        scheduler: Callable[[float, Callable[[], Awaitable[object]]], object] | None = None,
    ) -> None:
        if tuple(delays_seconds) != (0.0, 2.0, 5.0):
            raise ValueError("probe_release_delays_must_be_0_2_5")
        self._store = probe_store
        self._clock = clock or SystemClock()
        self._delays = delays_seconds
        self._scheduler = scheduler

    async def verify(self, order: ProbeOrder, provider: ReleaseProvider, *, cancel_confirmed: bool = True) -> bool:
        if not order.seat_ids:
            return False
        current = self._store.get(order.probe_id)
        if current and current.status not in {ProbeStatus.RELEASE_CHECKING, ProbeStatus.CANCEL_CONFIRMED, ProbeStatus.CANCEL_REQUESTED}:
            if current.status == ProbeStatus.RELEASE_UNVERIFIED:
                self._store.transition(order.probe_id, ProbeStatus.RELEASE_CHECKING)
        elif current and current.status != ProbeStatus.RELEASE_CHECKING:
            self._store.transition(order.probe_id, ProbeStatus.RELEASE_CHECKING)
        previous_delay = 0.0
        for delay in self._delays:
            interval = float(delay) - previous_delay
            if interval > 0:
                await self._clock.sleep(interval)
            previous_delay = float(delay)
            available = await provider.available_seat_ids(show_id=order.show_id, seat_ids=list(order.seat_ids))
            if set(order.seat_ids).issubset(available) and cancel_confirmed:
                latest = self._store.get(order.probe_id)
                if latest and latest.status != ProbeStatus.RELEASE_VERIFIED:
                    self._store.transition(
                        order.probe_id, ProbeStatus.RELEASE_VERIFIED,
                        release_verified_at=self._clock.now().isoformat(),
                    )
                return True
        latest = self._store.get(order.probe_id)
        if latest and latest.status != ProbeStatus.RELEASE_UNVERIFIED:
            self._store.transition(order.probe_id, ProbeStatus.RELEASE_UNVERIFIED, error_code="temporary_lock_release_unverified")
        return False

    def schedule_reconciliation(self, order: ProbeOrder, provider: ReleaseProvider) -> None:
        if self._scheduler is None:
            return
        for delay in (15.0, 15.0):
            self._scheduler(delay, lambda order=order, provider=provider: self.reconcile(order, provider))

    async def reconcile(self, order: ProbeOrder, provider: ReleaseProvider) -> bool:
        current = self._store.get(order.probe_id)
        if current is None or current.status == ProbeStatus.RELEASE_VERIFIED:
            return True
        return await self.verify(current, provider)

    async def reconcile_pending(self, provider: ReleaseProvider) -> list[str]:
        verified: list[str] = []
        for order in self._store.recoverable():
            if order.status in {ProbeStatus.RELEASE_UNVERIFIED, ProbeStatus.RELEASE_CHECKING, ProbeStatus.CANCEL_REQUESTED, ProbeStatus.CANCEL_CONFIRMED}:
                if await self.reconcile(order, provider):
                    verified.append(order.probe_id)
        return verified
