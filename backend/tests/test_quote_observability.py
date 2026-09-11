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

class _GoldenRecognition:
    provider_recognize_id = "recognition-1"
    city_text = "City"
    cinema_text = "Cinema"
    movie = "Movie"
    show_date = "2026-09-12"
    start_time = "16:00"
    selected_seats = ["4排7座"]
    has_selected_seats = True
    candidate_shows = []

    def model_dump(self, **kwargs):
        return {"city_text": self.city_text, "cinema_text": self.cinema_text,
                "movie": self.movie, "show_date": self.show_date,
                "start_time": self.start_time, "selected_seats": list(self.selected_seats)}


class _GoldenRoute:
    def __init__(self, route):
        self.route = route
        self.resolution_reason = "missing show" if route == "UNRESOLVED" else None
        self.wanda_store_id = "store-1"
        self.wanda_city_id = "city-1"
        self.wanda_cinema_name = "Cinema"
        self.wanda_cinema_address = None
        self.wanda_city_name = "City"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    "ROUTE_UNRESOLVED", "SELECTED_SEATS_REQUIRED", "PROVIDER_UNAVAILABLE",
    "SHOW_RESOLVE_FAILURE", "COST_FAILURE", "PRICING_FAILURE", "QUOTED",
])
async def test_golden_trace_enabled_disabled_business_equivalence(monkeypatch, status):
    """The telemetry hooks are observational: same result and side effects."""
    from app.quote_v2.service import CanonicalQuoteRuntime

    calls = {"route": 0, "quote": 0, "persist": 0, "commands": []}

    class Route:
        async def resolve(self, recognition):
            calls["route"] += 1
            return _GoldenRoute("UNRESOLVED" if status == "ROUTE_UNRESOLVED" else "WANDA_SELF")

    runtime = CanonicalQuoteRuntime.__new__(CanonicalQuoteRuntime)
    runtime._route = Route()
    runtime._fact_store = None
    runtime._reply_renderer = None
    runtime._recognition = None
    runtime._rules = lambda: object()

    async def fake_quote(*args, **kwargs):
        calls["quote"] += 1
        if status == "SELECTED_SEATS_REQUIRED":
            return {"status": status, "reason": "selected seats required"}
        if status == "PROVIDER_UNAVAILABLE":
            return {"status": status, "reason": "provider unavailable"}
        if status == "SHOW_RESOLVE_FAILURE":
            return {"status": "SHOW_UNRESOLVED", "reason": "show unavailable"}
        if status == "COST_FAILURE":
            return {"status": "COST_UNAVAILABLE", "reason": "cost unavailable"}
        if status == "PRICING_FAILURE":
            return {"status": "PRICING_UNAVAILABLE", "reason": "pricing unavailable"}
        calls["persist"] += 1
        calls["commands"].append({"action_type": "send_message", "quote_record_id": "record-1"})
        return {"status": "QUOTED", "quote": {"record_id": "record-1"}, "quote_record_id": "record-1"}

    runtime._quote_wanda = fake_quote
    identity = {"event_id": "event-1", "tenant_id": "t", "shop_id": "s", "buyer_id": "b",
                "chat_id": "c", "purchase_context_id": "p", "message_id": "m"}
    recognition = _GoldenRecognition()
    outputs = []
    for tracing in (True, False):
        if tracing:
            monkeypatch.setattr("app.quote_v2.service._stage_trace", _stage_trace)
        else:
            monkeypatch.setattr("app.quote_v2.service._stage_trace", lambda *a, **k: None)
        before = {key: value.copy() if isinstance(value, list) else value for key, value in calls.items()}
        outputs.append(await runtime.quote_recognition(recognition, identity=identity))
        assert recognition.selected_seats == ["4排7座"]
        assert calls["route"] == before["route"] + 1
    assert outputs[0] == outputs[1]
    assert calls["persist"] == (2 if status == "QUOTED" else 0)
    assert len(calls["commands"]) == (2 if status == "QUOTED" else 0)


def test_delivery_receipt_trace_covers_three_outcomes():
    assert _delivery_receipt_result(succeeded=True, message_id="m", record_id="q", action_type="send_message")[1] == "PENDING"
    assert _delivery_receipt_result(succeeded=True, message_id="m", record_id=None, action_type="send_message")[1] == "SKIPPED_NO_QUOTE_RECORD"
    assert _delivery_receipt_result(succeeded=True, message_id="m", record_id=None, action_type="other")[1] == "NOT_ATTEMPTED"
