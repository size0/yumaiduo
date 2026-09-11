from app.canonical_buyer_reply import CanonicalBuyerReplyRenderer
from app.quote_v2.service import _recognition_seats


def test_liangpiao_accepts_column_seat_labels_from_recognition() -> None:
    seats = _recognition_seats(["4排3列", "4排2列"])
    assert [(seat.row_no, seat.col_no) for seat in seats] == [(4, 3), (4, 2)]


def test_existing_exact_quote_followup_uses_configured_template() -> None:
    renderer = CanonicalBuyerReplyRenderer(lambda: type("Templates", (), {
        "exact_quote_template": "{影院}｜{影片}｜{报价单价}元/张｜合计{报价合计}元",
    })())
    result = renderer.render({"status": "QUOTED", "quote": {
        "request_type": "EXACT_SEATS", "cinema": "合肥天鹅湖万达广场店",
        "movie": "奥德赛", "quote_date": "2026-09-11", "showtime_start": "20:15",
        "hall": "IMAX激光厅", "selected_seats": [{"seat_label": "3排10座"}],
        "unit_sell_price_fen": 4800, "total_sell_price_fen": 4800,
    }})
    assert result["text"] == "合肥天鹅湖万达广场店｜奥德赛｜48元/张｜合计48元"
