from pydantic import ValidationError

from app.schemas import Recognition
from app.vision import SYSTEM_PROMPT, _normalize_recognition_payload, _safe_schema_diagnostics


def test_schema_diagnostics_expose_only_paths_types_and_retry_counts():
    try:
        Recognition.model_validate({"showtime": "not-a-time"})
    except ValidationError as error:
        diagnostics = _safe_schema_diagnostics(error, request_attempts=3, format_retries=2)
    assert diagnostics["failure_phase"] == "schema_validate"
    assert diagnostics["model_attempt_count"] == 3
    assert diagnostics["format_retry_count"] == 2
    assert all(set(item) == {"loc", "type"} for item in diagnostics["validation_paths"])


def test_visible_cinema_address_hint_is_schema_checked_and_prompt_bounded():
    recognition = _normalize_recognition_payload('{"cinema":"万达影城（南万达广场IMAX店）","cinema_address_hint":"谯城区希夷大道与杜仲路交叉口万达广场"}')
    assert recognition.cinema_address_hint == "谯城区希夷大道与杜仲路交叉口万达广场"
    assert "cinema_address_hint" in SYSTEM_PROMPT
    assert "只抄录截图中清晰可见的影院地址" in SYSTEM_PROMPT


def test_full_datetime_showtime_is_split_into_schema_safe_visible_facts():
    recognition = _normalize_recognition_payload(
        '{"image_type":"ORDER_CONFIRM","showtime":"2026-08-21 17:15:00","date":null}'
    )

    assert recognition.date.isoformat() == "2026-08-21"
    assert recognition.showtime == "17:15"


def test_json_extraction_diagnostics_never_include_model_content():
    diagnostics = _safe_schema_diagnostics(ValueError("model response has no unique JSON object"), request_attempts=1, format_retries=0)
    assert diagnostics == {"failure_phase": "json_extract", "validation_paths": [], "model_attempt_count": 1, "format_retry_count": 0}
