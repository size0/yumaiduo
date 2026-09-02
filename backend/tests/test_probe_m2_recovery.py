from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.probe.account_pool import FixtureProbeAccountPool, ProbeAccount
from app.probe.coordinator import ProbeCoordinator, ProbeRequest
from app.probe.errors import ProbeError
from app.probe.lease_store import DurableAccountLeaseStore
from app.probe.models import ProbeOrder, ProbeStatus
from app.probe.policy import ProbePolicy
from app.probe.probe_store import DurableProbeStore
from app.probe.release_tracker import FakeClock, ReleaseTracker
from app.probe.seat_selector import LiveSeat, ProbeSeatSelector
from app.probe.wanda_active_probe import FixtureWandaProvider, WandaActiveProbe


async def _coordinator(path: Path, provider: FixtureWandaProvider) -> ProbeCoordinator:
    store = DurableProbeStore(path)
    active = WandaActiveProbe(
        provider,
        probe_store=store,
        release_tracker=ReleaseTracker(store, clock=FakeClock()),
        policy=ProbePolicy(active_probe_enabled=True, agent_harness_read_only=False),
    )
    return ProbeCoordinator(
        probe_store=store,
        lease_store=DurableAccountLeaseStore(path),
        account_pool=FixtureProbeAccountPool([ProbeAccount(
            account_ref="acct", online=True, is_wplus=True,
            token_present=True, phone_present=True, risk_status="normal", remaining=2,
        )]),
        seat_selector=ProbeSeatSelector(), active_probe=active,
        policy=ProbePolicy(active_probe_enabled=True, agent_harness_read_only=False),
    )


def _request(show_id: str) -> ProbeRequest:
    return ProbeRequest(tenant_id="t", shop_id="s", show_id=show_id, requested_seat_labels=["1排1座"])


def _seats() -> list[LiveSeat]:
    return [LiveSeat(seat_id="seat-1", label="1排1座", area_code="A", zone_type="W+", available=True, wplus=True)]


@pytest.mark.asyncio
async def test_same_show_is_serialized_durably(tmp_path: Path) -> None:
    path = tmp_path / "probe.sqlite3"
    provider = FixtureWandaProvider(create_delay_seconds=0.05)
    first = await _coordinator(path, provider)
    second = await _coordinator(path, FixtureWandaProvider())
    first_task = asyncio.create_task(first.run(_request("show"), _seats()))
    await provider.created.wait()
    with pytest.raises(ProbeError, match="show_probe_unavailable"):
        await second.run(_request("show"), _seats())
    provider.create_delay_seconds = 0
    await first_task


@pytest.mark.asyncio
async def test_same_account_is_serialized_durably(tmp_path: Path) -> None:
    path = tmp_path / "probe.sqlite3"
    first_provider = FixtureWandaProvider(create_delay_seconds=0.05)
    first = await _coordinator(path, first_provider)
    second = await _coordinator(path, FixtureWandaProvider())
    first_task = asyncio.create_task(first.run(_request("show-1"), _seats()))
    await first_provider.created.wait()
    with pytest.raises(ProbeError, match="account_lease_unavailable"):
        await second.run(_request("show-2"), _seats())
    first_provider.create_delay_seconds = 0
    await first_task


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ProbeStatus.CREATED, ProbeStatus.LOCKED, ProbeStatus.PRICE_READ, ProbeStatus.CANCEL_REQUESTED, ProbeStatus.CANCEL_CONFIRMED, ProbeStatus.RELEASE_CHECKING])
async def test_restart_resumes_cancel_and_release_for_each_nonterminal_state(tmp_path: Path, status: ProbeStatus) -> None:
    path = tmp_path / f"{status.value}.sqlite3"
    store = DurableProbeStore(path)
    order = ProbeOrder(
        probe_id="probe-restart", tenant_id="t", shop_id="s", show_id="show",
        seat_ids=["seat-1"], temporary_order_reference="fixture-order-1", status=status,
    )
    store.create(order)
    provider = FixtureWandaProvider()
    restarted = await _coordinator(path, provider)

    recovered = await restarted.recover(provider)

    assert recovered == ["probe-restart"]
    assert store.get("probe-restart").status == ProbeStatus.RELEASE_VERIFIED
    assert "cancel_order" in provider.calls
