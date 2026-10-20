"""First text, then image: resolve identities before consistency checks, offline."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from application.ticket_flow import TicketFlow, FlowResult
from domain.conversation import Conversation, ConvState
from integrations.liangpiao.api import RecognitionResult


@pytest.fixture
def case():
    api = Mock()
    raw = dict(
        city="无锡",
        cinema="无锡滨湖万达...",
        cinemaTruncated=True,
        film="生化危机：爆发夜",
        hall="IMAX2D 原音",
        showtime="2026-10-18 13:45:00",
        isSeatSelection=True,
        seat=[
            {"seatName": "12排17座", "seatPrice": "47.9"},
            {"seatName": "12排16座", "seatPrice": "47.9"},
        ],
    )
    image = RecognitionResult(
        match_level="NONE",
        movie_name=raw["film"],
        raw_results=raw,
        seats=[
            dict(seatName="12排17座", seatNo=None),
            dict(seatName="12排16座", seatNo=None),
        ],
        price_mismatch=False,
        seat_confirm_required=False,
    )
    api.ch7_recognize.return_value = image
    api.ch4_city_list.return_value = [dict(code="320200", name="无锡")]
    api.ch4_cinema_list.return_value = {
        "list": [
            dict(
                cinemaId=100, name="万达影城（无锡滨湖万达广场IMAX店）", cityName="无锡"
            )
        ]
    }
    show = dict(
        showId="show",
        cinemaId=100,
        movieId=865,
        movieName=raw["film"],
        hallName="IMAX厅",
        startTime="2026-10-18T13:45:00+08:00",
    )
    api.ch4_show_list.return_value = [show]
    api.ch4_show_detail.return_value = show
    f = TicketFlow(api, Mock())
    f._after_recognition = Mock(
        return_value=FlowResult(
            ok=True, messages=["真实接口核价"], template_keys=["QUOTE_CARD"]
        )
    )
    conv = Conversation(chat_id="c", state=ConvState.IDLE)
    conv.demand_id = ""
    conv.demand_rev = 0
    request = dict(cinema="无锡滨湖万达", demand_id="", demand_rev=0)
    return SimpleNamespace(
        flow=f, api=api, conv=conv, request=request, raw=raw, image=image, show=show
    )


def run(c):
    return c.flow.on_screenshot(
        c.conv, "https://example.test/image", buyer_request=c.request
    )


def test_actual_first_cinema_then_image_reaches_quote_without_reasking_known_fields(
    case,
):
    out = run(case)
    assert out.ok and "QUOTE_CARD" in out.template_keys
    resolved = case.flow._after_recognition.call_args.args[1]
    assert resolved.cinema_id == 100 and resolved.show_id == "show"
    assert resolved.start_time == "2026-10-18T13:45:00+08:00"
    assert [s["seatNo"] for s in resolved.seats] == ["12排17座", "12排16座"]
    assert resolved.expected_seat_count == 2
    case.api.ch4_show_list.assert_called_once_with(100, "2026-10-18")


def test_ambiguous_directory_asks_only_cinema_not_movie_time_or_seats(case):
    cinema = case.api.ch4_cinema_list.return_value["list"][0]
    case.api.ch4_cinema_list.return_value["list"].append({**cinema, "cinemaId": 101})
    out = run(case)
    assert not out.ok and "TEXT_IMAGE_PENDING" in out.template_keys
    assert "门店" in "\n".join(out.messages)
    assert "日期、开场时间" not in "\n".join(out.messages)
    case.flow._after_recognition.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [{"movie": "其他影片"}, {"time": "14:00"}, {"ticket_count": 3}, {"demand_rev": 9}],
)
def test_real_text_conflicts_never_reach_quote(case, change):
    case.request.update(change)
    out = run(case)
    assert not out.ok
    case.flow._after_recognition.assert_not_called()


def test_wrong_cinema_in_picture_never_silently_overwritten(case):
    case.image.raw_results["cinema"] = "无锡新吴万达..."
    out = run(case)
    assert not out.ok
    case.flow._after_recognition.assert_not_called()


def test_multiple_exact_shows_require_show_confirmation(case):
    case.api.ch4_show_list.return_value = [
        case.show,
        {**case.show, "showId": "another"},
    ]
    out = run(case)
    assert not out.ok and "场次" in "\n".join(out.messages)
    case.flow._after_recognition.assert_not_called()


def test_detail_mismatch_never_reaches_quote(case):
    case.api.ch4_show_detail.return_value = {**case.show, "cinemaId": 999}
    assert not run(case).ok
    case.flow._after_recognition.assert_not_called()


def test_price_and_seat_confirmation_warnings_survive_resolution(case):
    case.image.price_mismatch = True
    case.image.seat_confirm_required = True
    run(case)
    resolved = case.flow._after_recognition.call_args.args[1]
    assert resolved.price_mismatch and resolved.seat_confirm_required
    assert not resolved.seat_matched


def test_directory_outage_is_not_called_a_text_image_conflict(case):
    case.api.ch4_cinema_list.side_effect = TimeoutError()
    out = run(case)
    assert not out.ok and "目录" in "\n".join(out.messages)
    assert "不一致" not in "\n".join(out.messages)
    case.flow._after_recognition.assert_not_called()


@pytest.mark.parametrize("hall", ["普通厅", "1号厅", "4DX厅", "CINITY厅"])
def test_special_image_format_cannot_be_overwritten_by_other_hall(case, hall):
    case.api.ch4_show_detail.return_value = {**case.show, "hallName": hall}
    out = run(case)
    assert not out.ok and "影厅类型" in "\n".join(out.messages)
    case.flow._after_recognition.assert_not_called()


def test_real_downstream_flow_receives_resolved_facts_before_quote(case):
    case.flow._after_recognition = TicketFlow._after_recognition.__get__(
        case.flow, TicketFlow
    )
    case.flow._build_quote = Mock(
        return_value=FlowResult(ok=True, template_keys=["QUOTE_CARD"])
    )
    out = run(case)
    assert out.ok and "QUOTE_CARD" in out.template_keys
    resolved = case.flow._build_quote.call_args.args[1]
    assert resolved.cinema_id == 100 and resolved.expected_seat_count == 2
    assert resolved.raw_results["hall"] == "IMAX2D 原音"


@pytest.mark.parametrize(
    "supplement",
    [{"ticket_count": 2}, {"movie": "生化危机：爆发夜"}, {"time": "13:45"}],
)
def test_non_cinema_supplement_can_resolve_missing_image_identity(case, supplement):
    case.request.pop("cinema")
    case.request.update(supplement)
    assert run(case).ok
    resolved = case.flow._after_recognition.call_args.args[1]
    assert resolved.cinema_id == 100 and len(resolved.seats) == 2


def test_non_cinema_supplement_still_blocks_stale_demand_before_query(case):
    case.request = {"ticket_count": 2, "demand_id": "old", "demand_rev": 9}
    assert not run(case).ok
    case.api.ch4_cinema_list.assert_not_called()


def test_missing_cinema_asks_only_cinema_instead_of_generic_conflict(case):
    case.request.pop("cinema")
    case.request["ticket_count"] = 2
    case.image.raw_results["cinema"] = ""
    out = run(case)
    assert not out.ok and "影院" in "\n".join(out.messages)
    assert "文字购票需求与这张截图" not in "\n".join(out.messages)
