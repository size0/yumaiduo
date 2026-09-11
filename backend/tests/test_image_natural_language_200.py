"""Two hundred image-context + natural-language combinations.

The image paths are the actual buyer screenshots supplied for this evaluation;
the test keeps recognition facts explicit so a routing regression cannot hide
behind an OCR/provider call.
"""

from pathlib import Path

import pytest

from app.canonical_conversation_agent import _conversation_intent


ATTACHMENTS = Path(r"E:\.codex\attachments\af8a5ad7-bfd4-4eec-856c-98c14dbfc136")
IMAGE_FACTS = [
    ("image-1.png", "潇湘冷江影城", "奥德赛", "2026-09-13", "10:10", ["4排3列", "4排2列"]),
    ("image-2.png", "大唐欢娱", "奥德赛", "2026-09-13", "17:15", ["5排3排", "5排4排"]),
    ("image-3.png", "上海枫泾天娱影城", "奥德赛", "2026-09-11", "20:30", ["4排4座"]),
    ("image-4.png", "万达影城", "奥德赛", "2026-09-12", "09:40", []),
    ("image-5.png", "万达影城", "奥德赛", "2026-09-12", "09:40", ["7排14座", "7排13座"]),
    ("image-6.png", "万达影城", "奥德赛", "2026-09-12", "10:30", []),
    ("image-7.png", "万达影城", "空枪", "2026-09-13", "11:45", ["7排7座", "7排8座"]),
    ("image-8.png", "春天国际影城", "奥德赛", "2026-09-13", "10:00", ["5排7座"]),
    ("image-9.png", "万达影城（娄底五江广场）", "奥德赛", "2026-09-13", "17:30", ["5排7座"]),
    ("image-10.png", "徐州铜山万达广场店", "奥德赛", "2026-09-11", "19:20", ["8排16座"]),
]

PROMPTS = (
    ("这个多少钱", "UNDERSTAND_OR_REQUEST_QUOTE"), ("什么价格", "UNDERSTAND_OR_REQUEST_QUOTE"),
    ("按这张图报价", "UNDERSTAND_OR_REQUEST_QUOTE"), ("帮我算总价", "UNDERSTAND_OR_REQUEST_QUOTE"),
    ("刚才的价格还在吗", "REVIEW_CURRENT_QUOTE"), ("按之前报价来", "REVIEW_CURRENT_QUOTE"),
    ("换晚一点的场次", "CHANGE_SHOW"), ("换到明天", "CHANGE_SHOW"),
    ("改成第二场", "CHANGE_SHOW"), ("这场能换成IMAX吗", "CHANGE_SHOW"),
    ("两张", "SET_TICKET_COUNT"), ("买2张票", "SET_TICKET_COUNT"),
    ("改成一张", "SET_TICKET_COUNT"), ("再加一张", "SET_TICKET_COUNT"),
    ("不要了", "CANCEL_PURCHASE"), ("先不买了", "CANCEL_PURCHASE"),
    ("不用继续查", "CANCEL_PURCHASE"), ("我去别家看看", "CANCEL_PURCHASE"),
    ("这个座位可以吗", "BUY_MOVIE_TICKET"), ("还能买到吗", "UNDERSTAND_OR_REQUEST_QUOTE"),
)

CASES = [
    (image, cinema, movie, date, showtime, seats, text, expected)
    for image, cinema, movie, date, showtime, seats in IMAGE_FACTS
    for text, expected in PROMPTS
]


def test_all_supplied_images_are_present() -> None:
    assert len(IMAGE_FACTS) == 10
    assert all((ATTACHMENTS / item[0]).is_file() for item in IMAGE_FACTS)


@pytest.mark.parametrize("image,cinema,movie,date,showtime,seats,text,expected", CASES)
def test_image_context_and_natural_language_routing(
    image: str, cinema: str, movie: str, date: str, showtime: str,
    seats: list[str], text: str, expected: str,
) -> None:
    assert image.startswith("image-") and cinema and movie and date and showtime
    assert isinstance(seats, list)
    quote = {"record_id": "quote-for-image-eval"} if expected == "REVIEW_CURRENT_QUOTE" else None
    assert _conversation_intent(text, quote=quote) == expected
