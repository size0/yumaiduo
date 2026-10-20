import templates


def test_wanda_unit_price_copy_differs_by_marked_seat_state():
    values = {
        "city": "荆州", "cinema": "万达影城", "movie": "重生2",
        "show_time": "2026-10-19 21:20", "hall": "5号激光厅",
        "unit_price": "32.96", "marked_count": 2,
    }
    marked = templates.render("WANDA_UNIT_PRICE", {**values, "_variant": "marked"}, channel="wanda")
    unmarked = templates.render("WANDA_UNIT_PRICE", values, channel="wanda")
    assert marked.endswith("需要几张票呢？")
    assert "需要几张票呢？如需指定位置，可以在图上标记后发我。" in unmarked
    assert "如需指定位置" not in marked
