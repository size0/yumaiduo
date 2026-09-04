from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.probe.capture_reliability import (
    CaptureStep,
    DurableProbeCapture,
    ProbeRequestAudit,
    RequestDisposition,
    ReliableProbeLifecycle,
    close_provider_client,
)
from app.probe.canonical import (
    ActivityOffersResult,
    CancelResult,
    CreateOrderResult,
    OrderStatusResult,
    SeatAvailabilityResult,
)
from app.probe.incident_close import (
    IncidentObservation,
    ManualIncidentCloseGate,
    ManualIncidentCloseStore,
)


class FakeProvider:
    fixture = True

    def __init__(self, *, close_error: bool = False, writer=None) -> None:
        self.calls: list[str] = []
        self.close_error = close_error
        self.writer = writer

    async def create_probe_order(self, *, account, show_id: str, seat_ids: list[str]) -> CreateOrderResult:
        self.calls.append("create_order")
        return CreateOrderResult(code=0, biz_code=0, temporary_order_id="real-order-1")

    async def get_order_status(self, *, temporary_order_reference: str) -> OrderStatusResult:
        self.calls.append("order_status")
        return OrderStatusResult(order_status=60 if "cancel_order" in self.calls else 40, lock_seat_time=-1 if "cancel_order" in self.calls else 1)

    async def get_activity_offers(self, *, temporary_order_reference: str) -> ActivityOffersResult:
        self.calls.append("activity")
        return ActivityOffersResult(able=True, name="W+会员专享", total_pay_price_cents=3800)

    async def cancel_probe_order(self, *, temporary_order_reference: str) -> CancelResult:
        self.calls.append("cancel_order")
        return CancelResult(accepted=True)

    async def get_available_seats(self, *, show_id: str, seat_ids: list[str]) -> SeatAvailabilityResult:
        self.calls.append("available_seat_ids")
        return SeatAvailabilityResult(available_seat_ids=set(seat_ids))

    def close(self) -> None:
        self.calls.append("close")
        if self.close_error:
            raise AttributeError("legacy client close failure")


class FailingCapture(DurableProbeCapture):
    def record_step(self, step, payload):
        if step == CaptureStep.ACTIVITY_RESPONSE:
            raise OSError("capture writer failure")
        return super().record_step(step, payload)


@pytest.mark.asyncio
async def test_close_provider_client_supports_real_sync_close_and_isolates_failure() -> None:
    closed: list[str] = []

    class SyncClient:
        def close(self) -> None:
            closed.append("close")

    result = await close_provider_client(SyncClient())
    assert result.method == "close"
    assert result.failed is False
    assert closed == ["close"]

    errors: list[str] = []
    failed = await close_provider_client(FakeProvider(close_error=True), on_error=errors.append)
    assert failed.failed is True
    assert failed.error_code == "CLIENT_CLOSE_FAILED"
    assert errors == ["CLIENT_CLOSE_FAILED"]


@pytest.mark.asyncio
async def test_async_close_is_supported_and_missing_close_is_not_failure() -> None:
    class AsyncClient:
        async def aclose(self) -> None:
            self.closed = True

    client = AsyncClient()
    result = await close_provider_client(client)
    assert result.method == "aclose"
    assert client.closed is True
    missing = await close_provider_client(object())
    assert missing.failed is False
    assert missing.error_code == "CLIENT_CLOSE_NOT_REQUIRED"


def test_probe_journal_is_write_ahead_and_order_reference_is_independent(tmp_path: Path) -> None:
    capture = DurableProbeCapture(tmp_path / "capture", probe_id="probe-1", show_id="show-1", seat_id="seat-1", account_ref_hash="account-hash")
    capture.record_intent(started_at="2026-01-01T00:00:00Z")
    capture.record_step(CaptureStep.CREATE_REQUEST_STARTED, {"endpoint": "/order/create_order.api"})
    capture.record_step(CaptureStep.CREATE_RESPONSE_RECEIVED, {"code": 0, "data": {"orderId": "real-order-1"}})
    capture.record_order_reference("real-order-1")

    journal = json.loads((tmp_path / "capture" / "probe_journal.json").read_text(encoding="utf-8"))
    assert journal["state"] == CaptureStep.ORDER_ID_EXTRACTED
    assert journal["temporary_order_reference"] == "real-order-1"
    assert [event["state"] for event in journal["history"]] == [
        "CREATE_INTENT", "PRE_CREATE", "CREATE_REQUEST_STARTED", "CREATE_RESPONSE_RECEIVED", "ORDER_ID_EXTRACTED",
    ]
    assert (tmp_path / "capture" / "raw" / "steps").is_dir()


def test_redaction_handles_camel_case_temporary_order_reference() -> None:
    from app.probe.capture import assert_capture_redacted, redact_capture

    redacted = redact_capture({"temporaryOrderReference": "real-order-1"})
    assert redacted == {"temporaryOrderReference": "fixture-order-1"}
    assert_capture_redacted(redacted)


def test_probe_crash_points_leave_recoverable_journal_evidence(tmp_path: Path) -> None:
    capture = DurableProbeCapture(tmp_path / "capture", probe_id="probe-crash", show_id="show-1", seat_id="seat-1", account_ref_hash="account-hash")
    capture.record_intent(started_at="2026-01-01T00:00:00Z")  # A: crash before HTTP
    assert capture.journal()["state"] == "PRE_CREATE"
    capture.record_request_audit(step="CREATE", endpoint="/order/create_order.api", method="POST", disposition=RequestDisposition.REQUEST_SENT_RESPONSE_UNKNOWN)
    assert "REQUEST_SENT_RESPONSE_UNKNOWN" in (tmp_path / "capture" / "raw" / "provider_audit.json").read_text(encoding="utf-8")  # B
    capture.record_step(CaptureStep.CREATE_RESPONSE_RECEIVED, {"code": 0, "data": {"orderId": "real-order-1"}})  # C
    capture.record_order_reference("real-order-1")  # D: independent WAL record
    restarted = DurableProbeCapture(tmp_path / "capture", probe_id="probe-crash", show_id="show-1", seat_id="seat-1", account_ref_hash="account-hash")
    assert restarted.journal()["temporary_order_reference"] == "real-order-1"


def test_provider_request_audit_has_three_dispositions_and_no_credentials(tmp_path: Path) -> None:
    capture = DurableProbeCapture(tmp_path / "capture", probe_id="probe-1", show_id="show-1", seat_id="seat-1", account_ref_hash="account-hash")
    audit = ProbeRequestAudit(
        probe_id="probe-1", step="CREATE", endpoint="/order/create_order.api", method="POST",
        started_at="2026-01-01T00:00:00Z", finished_at=None, http_status=None,
        provider_request_id=None, disposition=RequestDisposition.REQUEST_SENT_RESPONSE_UNKNOWN,
    )
    capture.record_provider_audit(audit)
    payload = (tmp_path / "capture" / "raw" / "provider_audit.json").read_text(encoding="utf-8")
    assert "token" not in payload.lower()
    assert "authorization" not in payload.lower()
    assert "phone" not in payload.lower()
    assert "REQUEST_SENT_RESPONSE_UNKNOWN" in payload


@pytest.mark.asyncio
async def test_cleanup_runs_when_capture_writer_fails_and_close_does_not_replace_result(tmp_path: Path) -> None:
    capture = FailingCapture(tmp_path / "capture", probe_id="probe-1", show_id="show-1", seat_id="seat-1", account_ref_hash="account-hash")
    provider = FakeProvider(close_error=True)
    lifecycle = ReliableProbeLifecycle(capture, sleep=lambda _seconds: asyncio.sleep(0))
    report = await lifecycle.run(provider, account=object(), show_id="show-1", seat_ids=["seat-1"])

    assert "cancel_order" in provider.calls
    assert "available_seat_ids" in provider.calls
    assert report.capture_error == "capture writer failure"
    assert report.cleanup_attempted is True
    assert report.client_close_failed is True
    assert report.probe_result.error_code != "CLIENT_CLOSE_FAILED"


@pytest.mark.asyncio
async def test_success_persists_each_lifecycle_step_before_final_close(tmp_path: Path) -> None:
    capture = DurableProbeCapture(tmp_path / "capture", probe_id="probe-1", show_id="show-1", seat_id="seat-1", account_ref_hash="account-hash")
    provider = FakeProvider()
    lifecycle = ReliableProbeLifecycle(capture, sleep=lambda _seconds: asyncio.sleep(0))
    report = await lifecycle.run(provider, account=object(), show_id="show-1", seat_ids=["seat-1"])

    assert report.probe_result.status == "SUCCESS"
    assert report.probe_result.cancel_confirmed is True
    assert report.probe_result.release_verified is True
    assert report.capture_error is None
    from app.probe.replay import V3CaptureReplayProvider

    replay = V3CaptureReplayProvider(tmp_path / "capture" / "sanitized")
    assert (await replay.get_activity_offers(temporary_order_reference="fixture-order-1")).total_pay_price_cents == 3800
    journal = json.loads((tmp_path / "capture" / "probe_journal.json").read_text(encoding="utf-8"))
    states = [event["state"] for event in journal["history"]]
    assert states[:5] == ["CREATE_INTENT", "PRE_CREATE", "CREATE_REQUEST_STARTED", "CREATE_RESPONSE_RECEIVED", "ORDER_ID_EXTRACTED"]
    assert "CANCEL_REQUEST_STARTED" in states
    assert "CANCEL_RESPONSE" in states
    assert "CANCEL_STATUS" in states
    assert "RELEASE_0" in states
    assert "FINAL" in states
    assert journal["temporary_order_reference"] == "real-order-1"


def test_manual_close_gate_requires_stable_read_only_observation(tmp_path: Path) -> None:
    store = ManualIncidentCloseStore(tmp_path / "incident.json")
    gate = ManualIncidentCloseGate(store)
    observation = IncidentObservation(
        incident_id="incident-1", show_id="show-1", seat_id="seat-1", account_ref_hash="account-hash",
        target_seat_available=True, show_available=150, show_total=151,
        expected_probe_seat_ids=["seat-1"],
        probe_seat_availability_samples=[{"seat-1": True}, {"seat-1": True}, {"seat-1": True}],
        target_show_readable=True, target_provider_anomaly=False,
        target_seat_abnormal_occupancy=False,
        observation_window_elapsed=True, observation_sample_count=3, observation_window_seconds=900,
        no_matching_probe_order=True,
        no_provider_anomalies=False,
    )
    decision = gate.evaluate(observation)
    assert decision.eligible is True
    assert decision.result_state == "CLOSED_UNVERIFIED_SAFE"
    assert gate.close(observation, approved=False).closed is False
    closed = gate.close(observation, approved=True)
    assert closed.closed is True
    assert store.read()["state"] == "CLOSED_UNVERIFIED_SAFE"
    assert store.read()["create_commit"] == "UNKNOWN"
    assert store.read()["cleanup"] == "UNVERIFIED"


def _observation(**updates: object) -> IncidentObservation:
    values: dict[str, object] = {
        "incident_id": "incident-1", "show_id": "show-1", "seat_id": "seat-1", "account_ref_hash": "account-hash",
        "target_seat_available": True, "show_available": 151, "show_total": 151,
        "expected_probe_seat_ids": ["seat-1"],
        "probe_seat_availability_samples": [{"seat-1": True}, {"seat-1": True}, {"seat-1": True}],
        "target_show_readable": True, "target_provider_anomaly": False,
        "target_seat_abnormal_occupancy": False,
        "observation_window_elapsed": True, "observation_sample_count": 3,
        "observation_window_seconds": 900, "no_matching_probe_order": True,
        "no_provider_anomalies": True,
    }
    values.update(updates)
    return IncidentObservation(**values)


def test_show_count_change_and_unrelated_provider_anomaly_do_not_block_close(tmp_path: Path) -> None:
    gate = ManualIncidentCloseGate(ManualIncidentCloseStore(tmp_path / "incident.json"))
    decision = gate.evaluate(_observation(show_available=150, no_provider_anomalies=False))
    assert decision.eligible is True


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"target_seat_available": False, "probe_seat_availability_samples": [{"seat-1": True}, {"seat-1": False}, {"seat-1": True}]}, "expected_probe_seat_not_stable"),
        ({"target_provider_anomaly": True}, "target_provider_anomaly_present"),
        ({"no_matching_probe_order": False}, "matching_probe_order_present"),
        ({"target_show_readable": False}, "target_show_unreadable"),
        ({"target_seat_abnormal_occupancy": True}, "target_seat_abnormal_occupancy"),
        ({"observation_window_seconds": 899}, "observation_window_incomplete"),
        ({"observation_sample_count": 2}, "observation_window_incomplete"),
    ],
)
def test_target_scoped_manual_close_blockers(tmp_path: Path, updates: dict[str, object], reason: str) -> None:
    gate = ManualIncidentCloseGate(ManualIncidentCloseStore(tmp_path / "incident.json"))
    decision = gate.evaluate(_observation(**updates))
    assert decision.eligible is False
    assert reason in decision.reasons


def test_manual_close_gate_rejects_unstable_state(tmp_path: Path) -> None:
    gate = ManualIncidentCloseGate(ManualIncidentCloseStore(tmp_path / "incident.json"))
    observation = IncidentObservation(
        incident_id="incident-1", show_id="show-1", seat_id="seat-1", account_ref_hash="account-hash",
        target_seat_available=False, show_available=150, show_total=151,
        expected_probe_seat_ids=["seat-1"],
        probe_seat_availability_samples=[{"seat-1": False}],
        target_show_readable=False, target_provider_anomaly=True,
        target_seat_abnormal_occupancy=True,
        observation_window_elapsed=False, no_matching_probe_order=True,
        no_provider_anomalies=True,
    )
    decision = gate.evaluate(observation)
    assert decision.eligible is False
    assert decision.result_state == "CREATE_UNKNOWN_HOLD"
    assert set(decision.reasons) == {"expected_probe_seat_not_stable", "target_show_unreadable", "observation_window_incomplete", "target_provider_anomaly_present", "target_seat_abnormal_occupancy"}
