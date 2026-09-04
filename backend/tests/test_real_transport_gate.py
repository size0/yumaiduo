from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.probe.capture_reliability import DurableProbeCapture
from app.probe.errors import ProbeError
from app.probe.capture_runner import (
    CAPTURE_HARNESS_VERSION,
    OneTimeConfirmationStore,
    ProbeWriteAllowlist,
    RealProbeBinding,
    RealProbeTransport,
    RealTransportConfig,
    SingleCreateFuse,
)


TARGET = RealProbeBinding(
    run_id="run-real-1", show_id="show-1", seat_id="seat-1", account_ref="account-1",
    expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    area_code="36", zone_type="W+", original_price_cents=3300,
)


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def create_order(self, request):
        self.calls.append(("create_order", request))
        return {"order_id": "order-1", "create_verified": True}

    async def order_status(self, order_id):
        self.calls.append(("order_status", order_id))
        return {"data": {"orderStatus": 40, "lockSeatTime": 1}}

    async def activity_offers(self, **kwargs):
        self.calls.append(("activity_offers", kwargs))
        return {"activities": [{"able": True, "name": "W+会员专享", "allot_seat": {"totalPayPrice": 3200}}]}

    async def cancel_order(self, order_id):
        self.calls.append(("cancel_order", order_id))
        return True

    async def realtime_seats(self, showtime_id):
        self.calls.append(("realtime_seats", showtime_id))
        return {"available_seat_ids": ["seat-1"]}


@pytest.mark.asyncio
async def test_real_transport_defaults_fail_closed_before_client_call(tmp_path: Path) -> None:
    client = Client()
    capture = DurableProbeCapture(tmp_path / "capture", probe_id="probe-1", show_id="show-1", seat_id="seat-1", account_ref_hash="hash")
    transport = RealProbeTransport(
        client=client, binding=TARGET, capture=capture,
        config=RealTransportConfig(), fuse=SingleCreateFuse(),
    )
    with pytest.raises(ProbeError, match="active_probe_disabled"):
        await transport.create_probe_order(account="account-1", show_id="show-1", seat_ids=["seat-1"])
    assert client.calls == []


def test_one_time_confirmation_is_short_lived_and_single_use(tmp_path: Path) -> None:
    store = OneTimeConfirmationStore(tmp_path / "confirmation.json")
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    token = store.prepare(run_id="run-real-1", expires_at=expires)
    assert token.value
    assert token.report_metadata["token_value"] == "REDACTED"
    assert store.consume(run_id="run-real-1", token=token.value) is True
    assert store.consume(run_id="run-real-1", token=token.value) is False


def test_confirmation_cannot_be_used_for_another_run_or_after_expiry(tmp_path: Path) -> None:
    store = OneTimeConfirmationStore(tmp_path / "confirmation.json")
    token = store.prepare(run_id="run-real-1", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert store.consume(run_id="run-other", token=token.value) is False
    assert store.consume(run_id="run-real-1", token=token.value) is False


@pytest.mark.asyncio
async def test_real_transport_requires_frozen_binding_and_hard_single_create(tmp_path: Path) -> None:
    client = Client()
    capture = DurableProbeCapture(tmp_path / "capture", probe_id="probe-1", show_id="show-1", seat_id="seat-1", account_ref_hash="hash")
    transport = RealProbeTransport(
        client=client, binding=TARGET, capture=capture,
        config=RealTransportConfig(
            enabled=True, confirmation_consumed=True,
            active_probe_enabled=True, external_writes_enabled=True, flow_allows_probe=True,
        ), fuse=SingleCreateFuse(),
    )
    with pytest.raises(ValueError, match="target_binding_mismatch"):
        await transport.create_probe_order(account="account-1", show_id="other-show", seat_ids=["seat-1"])
    created = await transport.create_probe_order(account="account-1", show_id="show-1", seat_ids=["seat-1"])
    capture.record_order_reference(created.temporary_order_id or "")
    with pytest.raises(RuntimeError, match="single_create_fuse_tripped"):
        await transport.create_probe_order(account="account-1", show_id="show-1", seat_ids=["seat-1"])
    assert client.calls == [("create_order", {
        "showtime_id": "show-1", "seat_ids": ["seat-1,3300,0,0"],
        "seat_payloads": ["seat-1,3300,0,0"], "total_price": 3300,
        "cinema_id": "", "account_ref": "account-1",
    })]


@pytest.mark.asyncio
async def test_cancel_accepts_only_current_durable_order_reference(tmp_path: Path) -> None:
    client = Client()
    capture = DurableProbeCapture(tmp_path / "capture", probe_id="probe-1", show_id="show-1", seat_id="seat-1", account_ref_hash="hash")
    transport = RealProbeTransport(
        client=client, binding=TARGET, capture=capture,
        config=RealTransportConfig(
            enabled=True, confirmation_consumed=True,
            active_probe_enabled=True, external_writes_enabled=True, flow_allows_probe=True,
        ), fuse=SingleCreateFuse(),
    )
    with pytest.raises(PermissionError, match="order_reference_not_durable"):
        await transport.cancel_probe_order(temporary_order_reference="arbitrary-order")
    capture.record_order_reference("order-1")
    result = await transport.cancel_probe_order(temporary_order_reference="order-1")
    assert result.accepted is True
    with pytest.raises(PermissionError, match="order_reference_not_durable"):
        await transport.cancel_probe_order(temporary_order_reference="order-2")


def test_endpoint_allowlist_has_no_other_write_path() -> None:
    allowlist = ProbeWriteAllowlist()
    assert allowlist.allowed_write_endpoints == {
        ("POST", "/order/create_order.api"), ("POST", "/order/cancel.api"),
    }
    for method, endpoint in (("POST", "/order/confirm.api"), ("POST", "/activity/pay.api"), ("PUT", "/order/cancel.api")):
        assert allowlist.check(method, endpoint) is False
    assert CAPTURE_HARNESS_VERSION == "M2.7+"
