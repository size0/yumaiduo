from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .account_pool import ProbeAccount
from .errors import ProbeError
from .lease_store import DurableAccountLeaseStore
from .models import ProbeOrder, ProbeResult, ProbeStatus
from .policy import ProbePolicy, allow_active_probe
from .probe_store import DurableProbeStore
from .seat_selector import LiveSeat, ProbeSeatSelector
from .wanda_active_probe import WandaActiveProbe


class ProbeAccountPool(Protocol):
    def select(self, *, now: datetime | None = None) -> ProbeAccount: ...


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=200)
    shop_id: str = Field(min_length=1, max_length=200)
    show_id: str = Field(min_length=1, max_length=240)
    requested_seat_labels: list[str] = Field(default_factory=list, max_length=100)
    area_probe: bool = False


class ProbeCoordinator:
    """Deterministic owner of Probe admission, locks, leases and cleanup gates."""

    def __init__(
        self,
        *,
        probe_store: DurableProbeStore,
        lease_store: DurableAccountLeaseStore,
        account_pool: ProbeAccountPool,
        seat_selector: ProbeSeatSelector,
        active_probe: WandaActiveProbe,
        policy: ProbePolicy,
        lease_ttl_seconds: float | None = None,
    ) -> None:
        self._probe_store = probe_store
        self._lease_store = lease_store
        self._account_pool = account_pool
        self._seat_selector = seat_selector
        self._active_probe = active_probe
        self._policy = policy
        self._lease_ttl = float(lease_ttl_seconds or policy.account_lease_ttl_seconds)

    async def run(self, request: ProbeRequest, live_seats: list[LiveSeat]) -> ProbeResult:
        # Admission is checked before any durable Probe state or provider
        # operation is created. The context is set again only after the
        # account lease and risk/eligibility checks have succeeded.
        self._policy.ensure_enabled()
        now = datetime.now(timezone.utc)
        probe_id = f"probe-{uuid4().hex}"
        order = ProbeOrder(
            probe_id=probe_id, tenant_id=request.tenant_id, shop_id=request.shop_id,
            show_id=request.show_id, created_at=now.isoformat(), updated_at=now.isoformat(),
        )
        self._probe_store.create(order)
        lease_expiry = datetime.fromtimestamp(now.timestamp() + self._lease_ttl, timezone.utc).isoformat()
        if not self._probe_store.try_acquire_show(request.show_id, probe_id, now=now, lease_expires_at=lease_expiry):
            self._probe_store.transition(probe_id, ProbeStatus.FAILED, error_code="show_probe_unavailable")
            raise ProbeError("show_probe_unavailable", "该场次已有 Probe 或等待释放复核。")
        account: ProbeAccount | None = None
        lease_acquired = False
        try:
            try:
                account = self._account_pool.select(now=now)
            except ProbeError as error:
                self._probe_store.transition(probe_id, ProbeStatus.FAILED, error_code=error.code)
                self._probe_store.release_show(request.show_id, probe_id)
                return ProbeResult(
                    probe_id=probe_id, show_id=request.show_id, status="FAILED",
                    release_verified=False, error_code=error.code,
                )
            lease_acquired = self._lease_store.try_acquire(
                account.account_ref, probe_id, request.show_id, now=now, ttl_seconds=self._lease_ttl,
            )
            if not lease_acquired:
                raise ProbeError("account_lease_unavailable", "Probe 账号正在被其他任务使用。")
            selected = (
                self._seat_selector.select_exact(request.requested_seat_labels, live_seats)
                if request.requested_seat_labels and not request.area_probe
                else self._seat_selector.select_area(live_seats)
            )
            order = self._probe_store.update(
                probe_id, expected_revision=order.revision,
                account_ref=account.account_ref, seat_ids=[seat.seat_id for seat in selected],
            )
            with allow_active_probe(lease_acquired=True, risk_approved=True):
                result = await self._active_probe.run(order, selected, account)
            latest = self._probe_store.get(probe_id)
            if latest and latest.status == ProbeStatus.RELEASE_VERIFIED:
                self._probe_store.release_show(request.show_id, probe_id)
                self._lease_store.release(account.account_ref, probe_id)
                lease_acquired = False
            elif latest and latest.status == ProbeStatus.CREATE_UNKNOWN:
                unknown_expiry = (now + timedelta(seconds=self._policy.unknown_create_hold_seconds)).isoformat()
                self._probe_store.mark_show_unknown_hold(
                    request.show_id, probe_id, lease_expires_at=unknown_expiry,
                )
                self._lease_store.renew(
                    account.account_ref, probe_id, now=now,
                    ttl_seconds=self._policy.unknown_create_hold_seconds,
                )
            else:
                self._probe_store.mark_show_release_unverified(
                    request.show_id, probe_id, lease_expires_at=lease_expiry,
                )
            return result
        except ProbeError as error:
            current = self._probe_store.get(probe_id)
            if current and current.status == ProbeStatus.CREATED:
                self._probe_store.transition(probe_id, ProbeStatus.FAILED, error_code=error.code)
            if account is not None and lease_acquired:
                self._lease_store.release(account.account_ref, probe_id)
            self._probe_store.release_show(request.show_id, probe_id)
            raise
        finally:
            # A release-pending show lock is deliberately not removed here.
            latest = self._probe_store.get(probe_id)
            if account is not None and lease_acquired and latest and latest.status == ProbeStatus.RELEASE_VERIFIED:
                self._lease_store.release(account.account_ref, probe_id)

    async def recover(self, provider: Any) -> list[str]:
        """Restart hook: resume cancel/release from SQLite, not memory."""
        # Cleanup recovery is not a new Probe admission and must continue even
        # while the Active Probe kill switch is false.
        recovered = await self._active_probe.recover_pending(provider)
        for probe_id in recovered:
            order = self._probe_store.get(probe_id)
            if order is None:
                continue
            self._probe_store.release_show(order.show_id, order.probe_id)
            if order.account_ref:
                self._lease_store.release(order.account_ref, order.probe_id)
        return recovered
