from types import SimpleNamespace

import pytest

from app.main import _delivery_receipt_result
from app.quote_v2.service import _recognition_trace, _safe_reason, _stage_trace


@pytest.mark.parametrize(
    ("succeeded", "message_id", "record_id", "action_type", "expected"),
    [
        (True, "m1", "q1", "send_message", (True, "PENDING", "")),
        (True, "m1", None, "send_message", (False, "SKIPPED_NO_QUOTE_RECORD", "")),
        (True, "m1", None, "other", (False, "NOT_ATTEMPTED", "COMMAND_RESULT_NOT_SUCCEEDED_OR_MESSAGE_ID_MISSING")),
        (False, "m1", "q1", "send_message", (False, "NOT_ATTEMPTED", "COMMAND_RESULT_NOT_SUCCEEDED_OR_MESSAGE_ID_MISSING")),
    ],
)
def test_delivery_receipt_trace_classification_preserves_eligibility(
    succeeded, message_id, record_id, action_type, expected,
):
    assert _delivery_receipt_result(
        succeeded=succeeded, message_id=message_id, record_id=record_id,
        action_type=action_type,
    ) == expected


@pytest.mark.parametrize("status", [
    "ROUTE_UNRESOLVED", "SELECTED_SEATS_REQUIRED", "PROVIDER_UNAVAILABLE",
    "SHOW_RESOLVE_FAILURE", "COST_FAILURE", "PRICING_FAILURE", "QUOTED",
])
def test_observability_golden_statuses_are_data_only(status):
    # Golden contract: tracing receives status metadata and does not mutate it.
    payload = {"status": status, "quote_record_id": "q1" if status == "QUOTED" else None}
    before = dict(payload)
    _stage_trace("golden", "ROUTE", status=payload["status"])
    assert payload == before


def test_recognition_trace_is_presence_only_and_counts_collections():
    recognition = SimpleNamespace(
        city_text="鄂尔多斯", cinema_text="万达", movie="奥德赛",
        show_date="2026-09-12", start_time="16:00",
        selected_seats=["4排7座"], has_selected_seats=True,
        candidate_shows=[{"id": "show-1"}],
    )
    trace = _recognition_trace(recognition)
    assert trace == {
        "city": True, "cinema": True, "movie": True, "date": True,
        "showtime_start": True, "selected_seats_count": 1,
        "has_selected_seats": True, "candidate_shows_count": 1,
    }
    assert "鄂尔多斯" not in str(trace)


def test_safe_reason_hashes_and_bounds_reason():
    reason = "secret customer text " + "x" * 200
    observed = _safe_reason(reason)
    assert observed["reason_class"] == "UNCLASSIFIED"
    assert len(observed["reason_hash"]) == 16
    assert "secret customer text" not in str(observed)


def test_stage_trace_isolated_when_logger_fails(monkeypatch):
    class BrokenLogger:
        def info(self, *args, **kwargs):
            raise RuntimeError("logging unavailable")

    monkeypatch.setattr("app.quote_v2.service.LOGGER", BrokenLogger())
    _stage_trace("event-1", "ROUTE", status="UNRESOLVED", failure="reason")


def test_stage_trace_does_not_mutate_quote_inputs(caplog):
    payload = {"status": "ROUTE_UNRESOLVED", "quote": None}
    before = dict(payload)
    _stage_trace("event-1", "ROUTE", status=payload["status"], failure="missing_show")
    assert payload == before
    assert "event-1" not in caplog.text


@pytest.mark.parametrize("stage", ["RECOGNITION", "ROUTE", "SHOW", "SEAT", "COST", "PRICING", "QUOTE_PERSIST"])
def test_stage_names_are_explicit(stage, caplog):
    caplog.set_level("INFO")
    _stage_trace("event-1", stage, status="STARTED")
    assert f"stage={stage}" in caplog.text
