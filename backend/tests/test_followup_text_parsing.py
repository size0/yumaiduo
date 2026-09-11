from datetime import datetime, timedelta

from app.canonical_conversation_agent import _followup_updates


def test_full_text_extracts_date_time_and_count() -> None:
    result = _followup_updates("哈东万达 9月12号 奥德赛 8:30 两张", {})
    assert result == {"quote_date": "2026-09-12", "showtime_start": "08:30", "ticket_count": 2}


def test_relative_date_and_chinese_half_hour() -> None:
    result = _followup_updates("明天 八点半", {})
    assert result["showtime_start"] == "08:30"
    assert result["quote_date"] == (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()


def test_replacement_text_keeps_only_explicit_patch() -> None:
    assert _followup_updates("换成20:30", {}) == {"showtime_start": "20:30"}
