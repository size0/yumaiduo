from __future__ import annotations

from pathlib import Path

import pytest

from app.probe.account_pool import FixtureProbeAccountPool, ProbeAccount
from app.probe.canonical import OrderLookupResult
from app.probe.capture import write_v3_capture
from app.probe.comparator import ProbeShadowComparator, ShadowClassification
from app.probe.coordinator import ProbeCoordinator, ProbeRequest
from app.probe.lease_store import DurableAccountLeaseStore
from app.probe.models import ProbeOrder, ProbeStatus
from app.probe.policy import ProbePolicy
from app.probe.probe_store import DurableProbeStore
from app.probe.reconciliation import CREATE_UNKNOWN_UNRECOVERABLE_AUTOMATICALLY, CreateUnknownReconciler
from app.probe.release_tracker import FakeClock, ReleaseTracker
from app.probe.replay import V3CaptureReplayProvider
from app.probe.seat_selector import LiveSeat, ProbeSeatSelector
from app.probe.wanda_active_probe import WandaActiveProbe


RESPONSES = {
    "create_order.response.json": {"code": 0, "data": {"bizCode": 0, "orderId": "real-order"}},
    "lock_status.response.json": {"orderStatus": 40, "lockSeatTime": 1},
    "activity_offers.response.json": {"able": True, "name": "W+会员专享", "allotSeat": {"totalPayPrice": 3800}},
    "cancel.response.json": {"code": 0},
    "cancel_status.response.json": {"orderStatus": 60, "lockSeatTime": -1},
    "seat_release_0s.response.json": {"available_seat_ids": ["seat-1"]},
    "seat_release_2s.response.json": {"available_seat_ids": ["seat-1"]},
    "seat_release_5s.response.json": {"available_seat_ids": ["seat-1"]},
}


def _fixture(tmp_path: Path) -> Path:
    return write_v3_capture(
        tmp_path / "replay",
        manifest={
            "fixture_version": "v3-probe-001", "provider": "WANDA", "cinema_id": "6669",
            "show_id": "show-1", "seat_type": "W+", "capture_schema_version": "1",
            "source": "V3", "captured_at": "2026-09-02T00:00:00Z",
        },
        responses=RESPONSES,
        expected_probe_result={
            "probe_id": "v3-probe", "provider": "WANDA", "show_id": "show-1", "status": "SUCCESS",
            "seat_type_prices": [{
                "area_code": "A", "zone_type": "W+", "representative_seat_id": "seat-1",
                "original_price_cents": 5000, "member_price_cents": 3800,
            }],
            "cancel_confirmed": True, "release_verified": True,
            "release_timing_class": "IMMEDIATE", "error_code": None,
        },
    )


@pytest.mark.asyncio
async def test_realistic_fixture_replay_runs_active_probe_and_comparator_without_http(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    provider = V3CaptureReplayProvider(fixture)
    store = DurableProbeStore(tmp_path / "probe.sqlite3")
    policy = ProbePolicy(active_probe_enabled=True, agent_harness_read_only=False)
    active = WandaActiveProbe(
        provider, probe_store=store, release_tracker=ReleaseTracker(store, clock=FakeClock()), policy=policy,
    )
    coordinator = ProbeCoordinator(
        probe_store=store, lease_store=DurableAccountLeaseStore(tmp_path / "probe.sqlite3"),
        account_pool=FixtureProbeAccountPool([ProbeAccount(
            account_ref="fixture-account", online=True, is_wplus=True,
            token_present=True, phone_present=True, risk_status="normal", remaining=1,
        )]),
        seat_selector=ProbeSeatSelector(), active_probe=active, policy=policy,
    )
    actual = await coordinator.run(
        ProbeRequest(tenant_id="t", shop_id="s", show_id="show-1", requested_seat_labels=["1排1座"]),
        [LiveSeat(
            seat_id="seat-1", label="1排1座", area_code="A", zone_type="W+",
            available=True, wplus=True, original_price_cents=5000,
        )],
    )
    expected = provider.expected_result()
    comparison = ProbeShadowComparator().compare(expected, actual)
    assert comparison.classification == ShadowClassification.MATCH


@pytest.mark.asyncio
async def test_create_unknown_reconciler_is_explicitly_unsupported_without_lookup(tmp_path: Path) -> None:
    store = DurableProbeStore(tmp_path / "probe.sqlite3")
    policy = ProbePolicy(active_probe_enabled=False, agent_harness_read_only=True)
    order = ProbeOrder(
        probe_id="unknown", tenant_id="t", shop_id="s", show_id="show",
        status=ProbeStatus.CREATE_UNKNOWN, account_ref="account", seat_ids=["seat-1"],
    )
    store.create(order)
    reconciler = CreateUnknownReconciler(store, policy=policy)
    result = await reconciler.reconcile(order, object())
    assert result.outcome == "UNSUPPORTED"
    assert CREATE_UNKNOWN_UNRECOVERABLE_AUTOMATICALLY == "CREATE_UNKNOWN_UNRECOVERABLE_AUTOMATICALLY"
    assert store.get("unknown").status == ProbeStatus.CREATE_UNKNOWN


@pytest.mark.asyncio
async def test_lookup_result_contract_can_identify_locked_order_without_raw_v3_object() -> None:
    class LookupProvider:
        async def lookup_probe_order(self, **_kwargs: object) -> OrderLookupResult:
            return OrderLookupResult(
                outcome="ORDER_FOUND_LOCKED", temporary_order_id="fixture-order-1",
                order_status=40, lock_seat_time=1,
            )

    assert (await LookupProvider().lookup_probe_order()).outcome == "ORDER_FOUND_LOCKED"
