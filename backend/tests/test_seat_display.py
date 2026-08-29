from __future__ import annotations

from datetime import date

from app.chat import build_recognition_reply
import pytest
from pydantic import ValidationError

from app.models import MovieImageInfo, PriceZone, RealQuote, SelectedSeat
from app.reply_template_store import ReplyTemplates


def test_specific_bottom_seat_cards_take_priority() -> None:
    recognition = MovieImageInfo.model_validate({
        "movie_name": "奥德赛",
        "selected_seats": [
            {"seat_number": "11排16座", "displayed_price": 68.9},
            {"seat_number": "11排14座", "displayed_price": 68.9},
        ],
        "selected_count_visible": 2,
        "confidence": 0.95,
    })

    assert recognition.seat_display == "11排16座、11排14座"
    assert recognition.seat_display_mode == "specific"
    reply = build_recognition_reply(recognition)
    assert "可见已选座：11排16座、11排14座" in reply
    assert "座位：W+座位" not in reply


def test_reply_hides_language_format_and_screenshot_amount() -> None:
    recognition = MovieImageInfo.model_validate({
        "movie_name": "奥德赛", "cinema_name": "成都蜀都万达广场店",
        "language": "英语", "format": "IMAX2D", "displayed_total": 45.9,
        "selected_count_visible": 0,
    })

    reply = build_recognition_reply(recognition)

    assert "成都蜀都万达广场店" in reply
    assert "语言" not in reply
    assert "IMAX2D" not in reply
    assert "45.9" not in reply
    assert "截图显示" not in reply


def test_missing_field_reply_never_exposes_optional_internal_schema_names() -> None:
    recognition = MovieImageInfo(
        selected_count_visible=0,
        missing_fields=[
            "platform", "cinema_name", "city", "movie_name", "date_text", "date",
            "showtime_start", "showtime_end", "hall_name", "language", "format", "displayed_total",
        ],
    )

    reply = build_recognition_reply(recognition)

    assert "城市" in reply
    assert "影院全名" in reply
    assert "影片" in reply
    assert "日期" in reply
    assert "开场时间" in reply
    assert reply.count("日期") == 1
    for internal in ("platform", "date_text", "showtime_end", "language", "format", "displayed_total"):
        assert internal not in reply
    assert "影厅" not in reply


def test_reply_supports_editable_templates_with_chinese_variables() -> None:
    recognition = MovieImageInfo(movie_name="奥德赛", city="成都", cinema_name="成都蜀都万达广场店", selected_count_visible=0)
    templates = ReplyTemplates(recognition_template="电影：{影片}\n城市：{城市}\n门店：{影院}\n{座位}\n{报价内容}")

    reply = build_recognition_reply(recognition, templates=templates)

    assert "电影：奥德赛" in reply
    assert "城市：成都" in reply
    assert "门店：成都蜀都万达广场店" in reply
    assert "实时报价暂未取得" not in reply
    assert "临时锁座失败" not in reply
    assert "请核对影片、影院和场次" in reply


def test_safe_supplement_templates_classify_cinema_and_showtime_failures() -> None:
    cinema = MovieImageInfo(
        movie_name="八仙！", cinema_name="万达影城（贵港万...", showtime_start="14:05",
        selected_count_visible=0, missing_fields=["city"],
    )
    other = MovieImageInfo(movie_name="八仙！", cinema_name="其他影院", selected_count_visible=0)

    assert "城市＋影院全名" in build_recognition_reply(cinema, quote_error="影院无法唯一匹配")
    assert build_recognition_reply(cinema, quote_error="场次未匹配") == ReplyTemplates().showtime_changed_template
    assert build_recognition_reply(other, quote_error="影院无法匹配") == ReplyTemplates().unsupported_cinema_template


def test_recognition_template_can_use_safe_global_quote_total() -> None:
    recognition = MovieImageInfo(
        movie_name="八仙！", city="贵港", cinema_name="贵港万达广场店",
        date_text="明天", showtime_start="14:05", selected_count_visible=0,
    )
    quote = RealQuote(
        quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=4270,
        total_quote_cents=4270, ticket_count=1, needs_ticket_count=False,
        pricing_source="official", matched_city_name="贵港", matched_cinema_name="贵港万达广场店",
    )
    templates = ReplyTemplates(recognition_template="{城市} | {影院}\n合计：{报价合计}")

    assert build_recognition_reply(recognition, quote=quote, templates=templates).endswith("合计：42.70")


def test_official_showtime_movie_fills_missing_vision_movie_in_customer_reply() -> None:
    recognition = MovieImageInfo(
        movie_name=None, city=None, cinema_name="万达影城（华都汇广场CINITY店）",
        date_text="今天 8月27日", showtime_start="20:40", hall_name="6号激光厅",
        missing_fields=["city", "movie_name"], confidence=0.95,
    )
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", unit_quote_cents=4_200,
        needs_ticket_count=True, pricing_source="official",
        matched_city_name="湛江", matched_cinema_name="湛江万达影城华都汇广场店",
        matched_movie_name="空枪", matched_showtime_start="20:40",
    )
    templates = ReplyTemplates(area_quote_template="{城市}|{影院}\n影片：{影片}\n{报价单价}/张")

    reply = build_recognition_reply(recognition, quote=quote, templates=templates)

    assert reply == "湛江|湛江万达影城华都汇广场店\n影片：空枪\n42.00/张"


def test_quote_failure_is_not_exposed_when_custom_recognition_template_omits_quote_content() -> None:
    recognition = MovieImageInfo(
        movie_name="奥德赛", city="广州", cinema_name="广州番禺万达广场店",
        date_text="今天", showtime_start="19:40", selected_count_visible=0,
    )
    templates = ReplyTemplates(
        recognition_template="※{城市} | {影院}\n影片：{影片}\n场次：{场次}\n{报价单价}一张",
    )

    reply = build_recognition_reply(
        recognition,
        quote_error="临时锁座已取消，但座位尚未确认恢复；本次不返回报价。",
        templates=templates,
    )

    assert reply == "实时报价暂未取得：当前场次暂未取得可核验价格\n请刷新场次截图后再发我核价。"
    assert "座位尚未确认恢复" not in reply


def test_unavailable_selected_seat_exposes_only_safe_same_type_reference_price() -> None:
    recognition = MovieImageInfo(
        movie_name="奥德赛", city="合肥", cinema_name="万达影城（天鹅湖激光IMAX店）",
        date_text="明天 8月28日", showtime_start="15:50",
        selected_seats=[{"seat_number": "10排16座"}, {"seat_number": "10排15座"}],
        selected_count_visible=2,
    )

    reply = build_recognition_reply(
        recognition,
        quote_error=(
            "10排16座、10排15座当前不可选，不能按原座位下单；"
            "同座位类型当前参考价：42.00一张，按2张参考合计84.00元；"
            "请重新选择同类型可售座位并发送最新截图。"
        ),
    )

    assert "10排16座、10排15座当前不可选" in reply
    assert "同座位类型当前参考价：42.00一张" in reply
    assert "按2张参考合计84.00元" in reply
    assert "不能按原座位下单" in reply


def test_recognition_template_can_show_authoritative_quote_unit_directly() -> None:
    recognition = MovieImageInfo(
        movie_name="奥德赛", city="青岛", cinema_name="青岛城阳万达广场店",
        date_text="8月26日", showtime_start="16:20", selected_count_visible=0,
    )
    templates = ReplyTemplates(
        recognition_template="※{城市} | {影院}\n影片：{影片}\n日期：{日期}\n场次：{场次}\n{报价单价}一张",
    )
    quote = RealQuote(
        quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=5830,
        needs_ticket_count=True, pricing_source="万达官方会员价",
    )

    assert build_recognition_reply(recognition, quote=quote, templates=templates) == (
        "※青岛 | 青岛城阳万达广场店\n影片：奥德赛\n日期：8月26日\n场次：16:20\n58.30一张"
    )
    assert "一张" not in build_recognition_reply(recognition, templates=templates)


def test_outer_quote_template_keeps_unit_price_when_area_count_is_unknown() -> None:
    recognition = MovieImageInfo(
        movie_name="欢迎来龙餐馆", city="廊坊", cinema_name="廊坊万达广场店",
        date_text="今天", showtime_start="18:30", showtime_end="20:50",
        selected_count_visible=0,
    )
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", unit_quote_cents=4000,
        needs_ticket_count=True, pricing_source="realtime_wplus_area",
    )
    templates = ReplyTemplates(
        recognition_template="{报价单价}一张合计 {报价合计}元",
    )

    assert build_recognition_reply(recognition, quote=quote, templates=templates) == (
        "40.00一张合计 40.00元"
    )


def test_authoritative_quote_date_expands_relative_today_for_buyer_reply() -> None:
    recognition = MovieImageInfo(
        movie_name="奥德赛", city="重庆", cinema_name="重庆南坪万达广场店",
        date_text="今天 (周二)", showtime_start="19:30", showtime_end="22:23", selected_count_visible=0,
    )
    quote = RealQuote(
        quote_scope="area_probe", quote_date=date(2026, 8, 25), seat_zone_type="W+",
        unit_quote_cents=6670, needs_ticket_count=True, pricing_source="万达官方会员价",
        matched_city_name="曲靖", matched_cinema_name="曲靖经开万达广场店",
    )
    templates = ReplyTemplates(recognition_template="{城市}|{影院}\n日期：{日期}\n场次：{场次}\n{报价单价}一张")

    assert build_recognition_reply(recognition, quote=quote, templates=templates) == (
        "曲靖|曲靖经开万达广场店\n日期：08月25日（周二，今天）\n场次：19:30–22:23\n66.70一张"
    )


def test_official_showtime_overrides_a_nearby_vision_time_error_in_reply() -> None:
    recognition = MovieImageInfo(
        movie_name="奥德赛", city="昆明", cinema_name="昆明西山万达广场店",
        date="2026-08-27", date_text="今天 (周四)",
        showtime_start="16:33", showtime_end="19:22", hall_name="16号IMAX厅",
    )
    quote = RealQuote(
        quote_scope="area_preview", quote_date=date(2026, 8, 27), seat_zone_type="W+",
        unit_quote_cents=6970, needs_ticket_count=True, pricing_source="万达官方会员价",
        matched_cinema_name="昆明西山万达广场店", matched_city_name="昆明",
        matched_showtime_start="16:20", matched_showtime_end="19:13",
        matched_hall_name="16号-激光IMAX-COLA厅(儿童须购票)",
    )
    templates = ReplyTemplates(
        recognition_template="{影院}\n场次：{场次}\n影厅：{影厅}\n{报价单价}一张",
    )

    assert build_recognition_reply(recognition, quote=quote, templates=templates) == (
        "昆明西山万达广场店\n场次：16:20–19:13\n"
        "影厅：16号-激光IMAX-COLA厅(儿童须购票)\n69.70一张"
    )


def test_exact_quote_template_can_use_recognition_identity_variables() -> None:
    recognition = MovieImageInfo.model_validate({
        "movie_name": "空枪", "city": "石河子", "cinema_name": "石河子万达广场店",
        "date_text": "今天", "showtime_start": "14:05", "showtime_end": "16:45",
        "hall_name": "5号激光厅", "selected_seats": [{"seat_number": "5排6座"}],
        "selected_count_visible": 1,
    })
    quote = RealQuote.model_validate({
        "quote_scope": "exact_seats", "seat_zone_type": "W+", "total_quote_cents": 4200,
        "ticket_count": 1, "pricing_source": "万达官方会员价", "matched_cinema_name": "石河子万达广场店",
        "seat_quotes": [{
            "seat_number": "5排6座", "seat_zone_type": "W+", "original_price_cents": 4490,
            "member_price_cents": 4190, "unit_quote_cents": 4200,
        }],
    })
    templates = ReplyTemplates(
        recognition_template="{报价内容}",
        exact_quote_template="{城市}|{影院}|{影片}|{日期}|{场次}|{影厅}|{座位}\n{逐座报价}",
    )

    reply = build_recognition_reply(recognition, quote=quote, templates=templates)

    assert "石河子|石河子万达广场店|空枪|今天|14:05–16:45|5号激光厅" in reply
    assert "可见已选座：5排6座" in reply
    assert "5排6座 42.00" in reply


def test_full_exact_quote_template_is_used_directly_and_shows_selected_seat() -> None:
    recognition = MovieImageInfo.model_validate({
        "movie_name": "奥德赛", "city": "昆明", "cinema_name": "昆明西山万达广场店",
        "date_text": "今天", "showtime_start": "16:20", "showtime_end": "19:13",
        "selected_seats": [{"seat_number": "1排14座"}], "selected_count_visible": 1,
    })
    quote = RealQuote.model_validate({
        "quote_scope": "exact_seats", "seat_zone_type": "特惠区",
        "unit_quote_cents": 6590, "total_quote_cents": 6590, "ticket_count": 1,
        "pricing_source": "realtime_regular_area", "matched_cinema_name": "昆明西山万达广场店",
        "seat_quotes": [{
            "seat_number": "1排14座", "seat_zone_type": "特惠区",
            "original_price_cents": 7090, "member_price_cents": 6490,
            "unit_quote_cents": 6590,
        }],
    })
    templates = ReplyTemplates(
        recognition_template=(
            "※{城市} | {影院}\n影片：{影片}\n日期：{日期}\n场次：{场次}\n\n"
            "W+会员价：{报价单价}/张"
        ),
        exact_quote_template=(
            "※{城市} | {影院}\n影片：{影片}\n日期：{日期}\n场次：{场次}\n"
            "座位：{座位}\n\n{报价单价}/张 合计：{报价合计}元"
        ),
    )

    reply = build_recognition_reply(recognition, quote=quote, templates=templates)

    assert reply == (
        "※昆明 | 昆明西山万达广场店\n影片：奥德赛\n日期：今天\n"
        "场次：16:20–19:13\n座位：1排14座\n\n65.90/张 合计：65.90元"
    )
    assert "W+会员价" not in reply


def test_missing_bottom_seat_cards_use_wplus_business_fallback() -> None:
    recognition = MovieImageInfo.model_validate({
        "movie_name": "奥德赛",
        "selected_seats": [],
        "selected_count_visible": 0,
        "confidence": 0.9,
    })

    assert recognition.seat_display == "W+座位"
    assert recognition.seat_display_mode == "wplus_fallback"
    assert "座位：W+座位" in build_recognition_reply(recognition)


def test_visible_currency_markers_are_normalized_without_calculation() -> None:
    recognition = MovieImageInfo.model_validate({
        "displayed_total": "¥72",
        "selected_count_visible": 1,
        "selected_seats": [{"seat_number": "6排16座", "displayed_price": "￥ 72.00 元"}],
        "price_zones": [{"name": "W+会员", "displayed_price": "CNY 62.70"}],
    })
    assert recognition.displayed_total == 72
    assert recognition.selected_seats[0].displayed_price == 72
    assert recognition.price_zones[0].displayed_price == 62.7
    assert MovieImageInfo.model_validate({"displayed_total": "", "selected_count_visible": 0}).displayed_total is None


def test_qualified_or_ambiguous_money_text_is_not_silently_accepted() -> None:
    with pytest.raises(ValidationError):
        MovieImageInfo.model_validate({"displayed_total": "¥69.9起", "selected_count_visible": 0})
    with pytest.raises(ValidationError):
        SelectedSeat.model_validate({"seat_number": "6排16座", "displayed_price": "大约72元"})


def test_unknown_price_zone_names_from_gpt_are_normalized_instead_of_failing() -> None:
    assert PriceZone.model_validate({"name": None, "displayed_price": 28}).name == "未知"
    assert PriceZone.model_validate({"name": "", "displayed_price": 28}).name == "未知"
    recognition = MovieImageInfo.model_validate({
        "price_zones": [
            {"name": "W+", "displayed_price": 28},
            {"name": None, "displayed_price": 28},
        ],
        "selected_count_visible": 0,
    })
    assert [zone.name for zone in recognition.price_zones] == ["W+", "未知"]


def test_computed_seat_display_is_in_api_serialization_but_not_accepted_from_model() -> None:
    recognition = MovieImageInfo(selected_count_visible=0)
    serialized = recognition.model_dump(mode="json")
    assert serialized["seat_display"] == "W+座位"
    assert serialized["seat_display_mode"] == "wplus_fallback"
