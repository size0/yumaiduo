from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.probe.canonical import (
    ActivityOffersResult,
    CreateOrderResult,
    canonical_activity,
    canonical_create_order,
    canonical_order_status,
)
from app.probe.capture import assert_capture_redacted, redact_capture, write_v3_capture
from app.probe.comparator import ProbeShadowComparator, ShadowClassification
from app.probe.errors import ProbeError
from app.probe.models import ProbeResult, ProbeStatus
from app.probe.account_pool import FixtureProbeAccountPool, ProbeAccount
from app.probe.coordinator import ProbeCoordinator, ProbeRequest
from app.probe.lease_store import DurableAccountLeaseStore
from app.probe.probe_store import DurableProbeStore
from app.probe.release_tracker import FakeClock, ReleaseTracker
from app.probe.seat_selector import LiveSeat, ProbeSeatSelector
from app.probe.wanda_active_probe import FixtureWandaProvider, WandaActiveProbe
from app.probe.official_provider import OfficialWandaProbeProvider
from app.probe.policy import ProbePolicy
from app.probe.replay import V3CaptureReplayProvider


def expected() -> dict[str, object]:
    return {
        "probe_id": "probe-v3",
        "provider": "WANDA",
        "show_id": "show-v3",
        "status": "SUCCESS",
        "seat_type_prices": [{
            "area_code": "A", "zone_type": "W+", "representative_seat_id": "seat-1",
            "original_price_cents": 5000, "member_price_cents": 3800,
        }],
        "cancel_confirmed": True,
        "release_verified": True,
        "release_timing_class": "IMMEDIATE",
        "error_code": None,
    }


def responses() -> dict[str, object]:
    return {
        "create_order.response.json": {"code": 0, "data": {"bizCode": 0, "orderId": "real-order-998"}, "phone": "13800000000"},
        "lock_status.response.json": {"orderStatus": 40, "lockSeatTime": 1, "Cookie": "cookie"},
        "activity_offers.response.json": {"able": True, "name": "W+会员专享", "allotSeat": {"totalPayPrice": 3800}, "token": "secret"},
        "cancel.response.json": {"code": 0, "Authorization": "Bearer secret"},
        "cancel_status.response.json": {"orderStatus": 60, "lockSeatTime": -1, "device_id": "device"},
        "seat_release_0s.response.json": {"available_seat_ids": ["seat-1"]},
        "seat_release_2s.response.json": {"available_seat_ids": ["seat-1"]},
        "seat_release_5s.response.json": {"available_seat_ids": ["seat-1"]},
    }


def test_redaction_removes_credentials_phone_and_replaces_order_id(tmp_path: Path) -> None:
    target = write_v3_capture(
        tmp_path / "fixture",
        manifest={
            "fixture_version": "v3-probe-001", "provider": "WANDA", "cinema_id": "6669",
            "show_id": "show-v3", "seat_type": "W+", "capture_schema_version": "1",
            "source": "V3",
        },
        responses=responses(), expected_probe_result=expected(),
    )
    payload = "\n".join(path.read_text(encoding="utf-8") for path in target.glob("*.json"))
    assert "real-order-998" not in payload
    assert "13800000000" not in payload
    assert "cookie" not in payload.lower()
    assert "authorization" not in payload.lower()
    assert "fixture-order-1" in payload


def test_redaction_validation_fails_closed_for_sensitive_input() -> None:
    redacted = redact_capture({"Token": "secret", "mobile": "13800000000", "orderId": "real"})
    assert redacted == {"orderId": "fixture-order-1"}
    assert_capture_redacted(redacted)
    with pytest.raises(ValueError, match="capture_sensitive_field_present"):
        assert_capture_redacted({"Authorization": "secret"})
    with pytest.raises(ValueError, match="capture_phone_present"):
        assert_capture_redacted({"message": "phone 13800000000"})


def test_canonical_adapter_does_not_expose_v3_object_shape() -> None:
    created = canonical_create_order({"code": 0, "data": {"bizCode": 0, "orderId": "fixture-order-1"}})
    status = canonical_order_status({"orderStatus": 40, "lockSeatTime": 1})
    activity = canonical_activity({"able": True, "name": "W+会员专享", "allotSeat": {"totalPayPrice": 3800}})
    assert isinstance(created, CreateOrderResult)
    assert created.temporary_order_id == "fixture-order-1"
    assert status.order_status == 40
    assert isinstance(activity, ActivityOffersResult)
    assert activity.total_pay_price_cents == 3800
    assert "orderId" not in created.model_dump()


@pytest.mark.asyncio
async def test_v3_capture_replays_through_canonical_provider(tmp_path: Path) -> None:
    directory = write_v3_capture(
        tmp_path / "fixture", manifest={
            "fixture_version": "v3-probe-001", "provider": "WANDA", "cinema_id": "6669",
            "show_id": "show-v3", "seat_type": "W+", "capture_schema_version": "1", "source": "V3",
        }, responses=responses(), expected_probe_result=expected(),
    )
    provider = V3CaptureReplayProvider(directory)
    assert (await provider.create_probe_order(account=None, show_id="show-v3", seat_ids=["seat-1"])).temporary_order_id == "fixture-order-1"  # type: ignore[arg-type]
    assert (await provider.get_order_status(temporary_order_reference="fixture-order-1")).order_status == 40
    assert (await provider.get_activity_offers(temporary_order_reference="fixture-order-1")).able is True
    await provider.cancel_probe_order(temporary_order_reference="fixture-order-1")
    assert (await provider.get_order_status(temporary_order_reference="fixture-order-1")).order_status == 60
    assert (await provider.get_available_seats(show_id="show-v3", seat_ids=["seat-1"])).available_seat_ids == {"seat-1"}
    assert provider.expected_result().release_verified is True


def test_shadow_comparator_has_field_level_diff_and_allowed_class() -> None:
    left = ProbeResult.model_validate(expected())
    right = left.model_copy(update={"probe_id": "probe-v4"})
    assert ProbeShadowComparator().compare(left, right).classification == ShadowClassification.MATCH
    mismatch = left.model_copy(update={"seat_type_prices": []})
    result = ProbeShadowComparator().compare(left, mismatch)
    assert result.classification == ShadowClassification.MISMATCH
    assert result.diffs[0].field == "seat_type_prices"
    acceptable = ProbeShadowComparator().compare(left, left.model_copy(update={"release_timing_class": "AFTER_2S"}), acceptable_fields={"release_timing_class"})
    assert acceptable.classification == ShadowClassification.ACCEPTABLE_DIFFERENCE


@pytest.mark.asyncio
async def test_official_adapter_has_no_real_http_path_in_m25() -> None:
    provider = OfficialWandaProbeProvider()
    with pytest.raises(ProbeError, match="real_provider_disabled"):
        await provider.get_order_status(temporary_order_reference="fixture-order-1")

    operations: list[str] = []

    def mock_transport(operation: str, payload: object) -> dict[str, object]:
        operations.append(operation)
        return {"orderStatus": 40, "lockSeatTime": 1}

    offline_provider = OfficialWandaProbeProvider(offline_transport=mock_transport)
    status = await offline_provider.get_order_status(temporary_order_reference="fixture-order-1")
    assert status.order_status == 40
    assert operations == ["get_order_status"]


@pytest.mark.asyncio
async def test_unknown_create_is_recoverable_and_never_retries_with_another_account(tmp_path: Path) -> None:
    provider = FixtureWandaProvider(create_unknown=True)
    store = DurableProbeStore(tmp_path / "probe.sqlite3")
    policy = ProbePolicy(active_probe_enabled=True, agent_harness_read_only=False)
    active = WandaActiveProbe(
        provider, probe_store=store, release_tracker=ReleaseTracker(store, clock=FakeClock()), policy=policy,
    )
    coordinator = ProbeCoordinator(
        probe_store=store, lease_store=DurableAccountLeaseStore(tmp_path / "probe.sqlite3"),
        account_pool=FixtureProbeAccountPool([ProbeAccount(
            account_ref="a", online=True, is_wplus=True, token_present=True,
            phone_present=True, risk_status="normal", remaining=2,
        )]), seat_selector=ProbeSeatSelector(), active_probe=active, policy=policy,
    )
    result = await coordinator.run(
        ProbeRequest(tenant_id="t", shop_id="s", show_id="show", requested_seat_labels=["1排1座"]),
        [LiveSeat(seat_id="seat-1", label="1排1座", area_code="A", zone_type="W+", available=True, wplus=True)],
    )
    assert result.error_code == "create_unknown"
    assert store.list_all()[0].status == ProbeStatus.CREATE_UNKNOWN
    assert provider.calls == ["create_order"]
    assert store.show_lock("show")["lock_state"] == "PROBE_UNKNOWN_HOLD"


def test_multi_worker_show_gate_and_lease_expiry_recovery(tmp_path: Path) -> None:
    from app.probe.lease_store import DurableAccountLeaseStore
    from app.probe.probe_store import DurableProbeStore

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "workers.sqlite3"
    probes = DurableProbeStore(path)
    leases = DurableAccountLeaseStore(path)
    expiry = (now + timedelta(seconds=180)).isoformat()
    assert probes.try_acquire_show("show-1", "probe-a", now=now, lease_expires_at=expiry)
    assert not probes.try_acquire_show("show-1", "probe-b", now=now, lease_expires_at=expiry)
    assert probes.try_acquire_show("show-2", "probe-c", now=now, lease_expires_at=expiry)
    assert leases.try_acquire("account-a", "probe-a", "show-1", now=now, ttl_seconds=180)
    assert not leases.try_acquire("account-a", "probe-b", "show-2", now=now, ttl_seconds=180)
    assert leases.recover_expired(now=now + timedelta(seconds=181)) == ["account-a"]
    assert leases.try_acquire("account-a", "probe-b", "show-2", now=now + timedelta(seconds=181), ttl_seconds=180)
    probes.mark_show_release_unverified("show-1", "probe-a", lease_expires_at=(now - timedelta(seconds=1)).isoformat())
    assert not probes.try_acquire_show("show-1", "probe-d", now=now + timedelta(days=1), lease_expires_at=expiry)


def test_read_only_wins_over_active_switch_and_liangpiao_flag_is_independent() -> None:
    settings = Settings(agent_harness_read_only=True, wanda_active_probe_enabled=True)
    with pytest.raises(ProbeError, match="agent_harness_read_only"):
        ProbePolicy.from_settings(settings).ensure_allowed()
    assert Settings(agent_harness_read_only=True, wanda_active_probe_enabled=False).liangpiao_selected_seat_quote_enabled is False
