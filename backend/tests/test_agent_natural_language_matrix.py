"""Regression matrix for natural-language turn routing (read-only)."""

import pytest

from app.canonical_conversation_agent import _conversation_intent, _task_phase


GROUPS = {
    "CHANGE_SHOW": "13点10分那场|第二场|IMAX那场|换晚一点|改成11:20|下午那场|有没有更晚的|换个场次|明天还有吗|换到明天|后天那场|今天晚上可以吗|换日期|周末有场吗|早一点的场次|最晚几点|有没有普通厅|换成2D|之后那场|这场可以吗",
    "SET_TICKET_COUNT": "两张|买2张|我要三张|一张票|4张可以吗|改成两张|再加一张|只要一张|两位成人|票要三张|几张合适|多要一张|减成一张|三位|两张成人票|数量改四张|我要5张|一张就好|再来两张|票数是三张",
    "UNDERSTAND_OR_REQUEST_QUOTE": "多少钱|价格多少|一张票多少钱|这个报价包含什么|贵不贵|能买么|现在还能买到吗|帮我算一下|报价有效多久|总价多少|服务费怎么算|会员价是什么|儿童票怎么算|学生优惠吗|普通区多少钱|有优惠吗|最低多少钱|费用怎么收|能不能买到|这场什么价格",
    "CANCEL_PURCHASE": "不要了|先不买了|取消吧|不用继续|我不想买了|算了|先放着|不用报价了|停止帮我查|我改天再看|不用回复了|我去别家看看|暂时不需要|先这样吧|晚点再说|不用帮我下单|我自己买|停止查询|不用继续跟进|谢谢先不用",
    "BUY_MOVIE_TICKET": "你好|在吗|我想看电影|帮我买电影票|想订一张电影票|有人工吗|可以代订吗|我想买票|看个电影|怎么购票|想看八仙|帮忙看看电影|我准备看电影|能帮我查票吗|想了解购票|电影票怎么订|第一次买票|请问怎么操作|我要买票|开始看电影",
    "REVIEW_CURRENT_QUOTE": "刚才的报价还在吗|还是刚才那个价格吗|我再看看报价|刚才那单呢|这个报价能保留吗|前面的价格有效吗|报价变了吗|还能按刚才买么|继续刚才的|刚才那场多少钱|刚才的场次还在吗|之前说的价格|刚才那个还能买|前面报价呢|刚才的票价|原来的报价有效吗|按之前的来|继续上一单|刚才说的那场|之前那个价格",
}

CASES = [(text, intent) for intent, values in GROUPS.items() for text in values.split("|")]


@pytest.mark.parametrize("text,expected", CASES, ids=lambda item: item)
def test_natural_language_intent_matrix(text: str, expected: str) -> None:
    quote = {"record_id": "quote-1"} if expected == "REVIEW_CURRENT_QUOTE" else None
    assert _conversation_intent(text, quote=quote) == expected


def test_task_phase_uses_intent_precedence_for_followups() -> None:
    assert _task_phase(current_text="改成两张", candidate={"movie": "八仙"}, quote=None)[0] == "COLLECT_SEAT_OR_COUNT"
    assert _task_phase(current_text="不要座位，普通区域", candidate={"movie": "八仙"}, quote=None)[0] == "COLLECT_SEAT_OR_COUNT"
    assert _task_phase(current_text="刚才的报价还在吗", candidate={"movie": "八仙"}, quote={"record_id": "q1"})[0] == "QUOTE_REVIEW"
