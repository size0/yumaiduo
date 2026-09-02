from __future__ import annotations

from app.models import CinemaCandidate, MovieImageInfo, SelectedSeat, ShowCandidate
from app.plugin_automation import (
    _build_image_quote_targets,
    _recognition_ready_for_preflight,
    validate_image_url,
)


SEAT_IMAGE_URL = (
    "https://img.alicdn.com/imgextra/i1/2464035965/"
    "O1CN0133v5VBg1lvC0sG0H_!!2464035965-2-xy_chat.png"
)
SHOW_IMAGE_URL = (
    "https://img.alicdn.com/imgextra/i1/2313315754/"
    "O1CN01T4hCpHVyl7L0sG0H_!!2313315754-0-xy_chat.jpg"
)


def _show_image() -> MovieImageInfo:
    return MovieImageInfo(
        is_seat_selection=False,
        city="上海",
        cinema_id=1486,
        cinema_name="时代国际影城（金山方圆荟店）",
        movie_id=123,
        movie_name="奥德赛",
        date_text="2026-09-01",
        show_id="889900",
        showtime_start="15:10",
        hall_name="1号厅",
        match_level="EXACT",
        seat_matched=True,
        price_mismatch=False,
    )


def _seat_image(seat_number: str, row_no: int, col_no: int) -> MovieImageInfo:
    return _show_image().model_copy(update={
        "is_seat_selection": True,
        "selected_seats": [SelectedSeat(
            seat_number=seat_number,
            row_no=row_no,
            col_no=col_no,
            seat_no=seat_number,
            status="AVAILABLE",
        )],
        "selected_count_visible": 1,
    })


def test_supplied_alicdn_images_are_accepted_by_the_runtime_url_gate() -> None:
    assert validate_image_url(SEAT_IMAGE_URL) == SEAT_IMAGE_URL
    assert validate_image_url(SHOW_IMAGE_URL) == SHOW_IMAGE_URL


def test_supplied_seat_and_show_images_form_one_enriched_quote_target() -> None:
    targets = _build_image_quote_targets([
        _seat_image("8排7座", 8, 7),
        _show_image(),
    ])

    assert len(targets) == 1
    assert targets[0]["image_indexes"] == [0, 1]
    assert targets[0]["conflict_fields"] == []
    recognition = targets[0]["recognition"]
    assert recognition.is_seat_selection is True
    assert recognition.selected_seats[0].seat_number == "8排7座"
    assert recognition.show_id == "889900"
    assert _recognition_ready_for_preflight(recognition) is True


def test_two_seat_images_form_two_independent_quote_targets() -> None:
    targets = _build_image_quote_targets([
        _seat_image("8排7座", 8, 7),
        _seat_image("9排13座", 9, 13),
    ])

    assert len(targets) == 2
    assert [target["image_indexes"] for target in targets] == [[0], [1]]
    assert [
        target["recognition"].selected_seats[0].seat_number
        for target in targets
    ] == ["8排7座", "9排13座"]
    assert all(
        _recognition_ready_for_preflight(target["recognition"])
        for target in targets
    )


def test_show_image_without_selected_seats_cannot_reach_preflight() -> None:
    assert _recognition_ready_for_preflight(_show_image()) is False


def test_exact_match_ignores_diagnostic_candidate_arrays_after_provider_converged() -> None:
    recognition = _seat_image("8排7座", 8, 7).model_copy(update={
        "candidate_cinemas": [
            CinemaCandidate(cinema_id=1486, name="时代国际影城（金山方圆荟店）"),
            CinemaCandidate(cinema_id=1487, name="历史候选影院"),
        ],
        "candidate_shows": [
            ShowCandidate(show_id="889900", start_time="2026-09-01T15:10:00+08:00"),
            ShowCandidate(show_id="889901", start_time="2026-09-01T18:10:00+08:00"),
        ],
        "match_level": "EXACT",
    })

    assert _recognition_ready_for_preflight(recognition) is True
