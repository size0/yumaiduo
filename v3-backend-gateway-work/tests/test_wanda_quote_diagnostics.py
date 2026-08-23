from __future__ import annotations

import json

from fastapi import HTTPException

from app.schemas import Recognition
from app.wanda_quote_diagnostics import diagnostic_failure, quote_failure_code


def test_quote_diagnostics_map_release_failure_without_copying_sensitive_payloads() -> None:
    recognition = Recognition.model_validate({
        "image_type": "SEAT_MAP",
        "city": "牡丹江",
        "cinema": "牡丹江万达广场店",
        "movie": "奥德赛",
        "date": "2026-08-23",
        "showtime": "17:15-20:08",
        "hall": "6号IMAX厅",
        "official_selection": {"is_selected": False, "selected_seat_numbers": [], "selected_count": 0},
    })
    realtime = {"data": {
        "token": "forbidden-token",
        "area": [{
            "areaCode": "36", "areaName": "W+专享",
            "areaPrice": {"salesPrice": 6490, "wPlusActivity": {"price": 5540}},
            "seat": [{"seatId": "secret-seat-id", "status": 1}],
        }],
    }}

    result = diagnostic_failure(
        HTTPException(status_code=502, detail="临时锁座未确认释放，已停止报价"),
        step="locked_offer",
        recognition=recognition,
        match={"data": {"cinema": {"cinemaName": "牡丹江万达广场店"}, "showtime": {"showtimeId": "secret-showtime"}}},
        realtime=realtime,
    )

    assert result.detail["code"] == "temporary_lock_release_unverified"
    assert result.detail["diagnostics"]["realtime_areas"][0]["available_seat_count"] == 1
    serialized = json.dumps(result.detail, ensure_ascii=False)
    assert "forbidden-token" not in serialized
    assert "secret-seat-id" not in serialized
    assert "secret-showtime" not in serialized


def test_quote_failure_code_distinguishes_business_rejections_from_gateway_failures() -> None:
    assert quote_failure_code(HTTPException(status_code=422, detail="当前场次没有可用的 W+座位"), "select_seats") == "wplus_seats_unavailable"
    assert quote_failure_code(HTTPException(status_code=502, detail="upstream unavailable"), "realtime_seats") == "wanda_gateway_unavailable"
