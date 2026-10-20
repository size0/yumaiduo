"""Directory matching is independent of model wording and never picks a tie."""
from unittest.mock import Mock

from application.cinema_matching import match_cinema, name_key, title_key


def row(id_, name, city='哈尔滨'):
    return dict(cinemaId=id_, name=name, cityName=city, cityCode='230100', address='哈尔滨市哈东万达广场')


def test_brand_change_and_marketing_suffix_preserve_branch():
    assert name_key('儒意影城哈东万达广场店') == name_key('万达影城（哈东IMAXGT双激光店地下停车首赠1H）') == '哈东'


def test_unique_directory_branch_accepts_alias_and_uses_directory_id():
    api=Mock()
    api.ch4_cinema_list.return_value={'list':[row(4019,'万达影城（哈东IMAXGT双激光店地下停车首赠1H）')]}
    result=match_cinema(api,'儒意影城哈东万达广场店',city_code='230100',city_name='哈尔滨')
    assert result['status']=='MATCHED' and result['cinema']['cinemaId']==4019
    assert api.ch4_cinema_list.call_args.kwargs['city_code']=='230100'


def test_close_siblings_require_confirmation_instead_of_highest_score():
    api=Mock()
    api.ch4_cinema_list.return_value={'list':[row(1,'万达影城（哈东店）'),row(2,'儒意影城（哈东店）')]}
    result=match_cinema(api,'哈东万达广场店',city_code='230100',city_name='哈尔滨')
    assert result['status']=='AMBIGUOUS' and 'cinema' not in result


def test_wrong_city_and_generic_name_cannot_auto_select():
    api=Mock()
    api.ch4_cinema_list.return_value={'list':[row(1,'万达影城（哈东店）',city='上海')]}
    assert match_cinema(api,'哈东万达店',city_code='230100',city_name='哈尔滨')['status']!='MATCHED'
    assert match_cinema(api,'万达影城',city_code='230100',city_name='哈尔滨')['status']!='MATCHED'


def test_full_city_directory_finds_different_branch_without_keyword_config():
    api=Mock()
    api.ch4_cinema_list.return_value={'list':[row(1,'万达影城（哈东店）'),row(2,'万达影城（哈西店）')]}
    assert match_cinema(api,'儒意影城哈西万达广场店',city_code='230100',city_name='哈尔滨')['cinema']['cinemaId']==2


def test_pagination_incomplete_never_claims_unique():
    api=Mock()
    api.ch4_cinema_list.return_value={'list':[row(1,'万达影城（哈东店）')], 'total':2000}
    assert match_cinema(api,'哈东万达店',city_code='230100',city_name='哈尔滨')['status']=='DIRECTORY_INCOMPLETE'


def test_title_punctuation_is_not_a_new_movie():
    assert title_key('《生化危机:爆发夜》')==title_key('生化危机：爆发夜')


def test_generic_imax_hall_alias_needs_unique_exact_film_date_and_time():
    from datetime import datetime,timezone,timedelta
    from types import SimpleNamespace
    from dataclasses import replace
    from application.ticket_flow import _verified_hall_alias
    from application.wanda_showtime import WandaShow
    stamp=datetime(2026,10,18,11,35,tzinfo=timezone(timedelta(hours=8)))
    show=WandaShow(showtime_id='one',realtime_ms=int(stamp.timestamp()*1000),
        hall_name='3号厅IMAX影厅【儿童观影需购票】',film_name='生化危机：爆发夜')
    result=SimpleNamespace(hall_name='IMAX厅',movie_name='生化危机:爆发夜',start_time=stamp.isoformat())
    assert _verified_hall_alias(result,show,[show])
    assert not _verified_hall_alias(result,show,[show,replace(show,showtime_id='two')])
    assert not _verified_hall_alias(result,replace(show,realtime_ms=show.realtime_ms+300000),[show])
    assert not _verified_hall_alias(result,replace(show,film_name='其他影片'),[replace(show,film_name='其他影片')])
    result.hall_name='2号IMAX厅'
    assert not _verified_hall_alias(result,show,[show])
    for hall in ('二号IMAX厅','２号IMAX厅','第二IMAX厅'):
        result.hall_name=hall
        assert not _verified_hall_alias(result,show,[show])


def test_generic_imax_alias_continues_costing_without_cinema_candidate_menu():
    from application.ticket_flow import FlowResult
    from domain.conversation import Conversation,ConvState
    from tests._check_channel_route import MAP_WANDA,flow,result
    f,_,_=flow(MAP_WANDA)
    r=result(seats=[{'seatNo':'6排9座'}]);r.hall_name='IMAX厅'
    out=f._build_quote(Conversation(chat_id='c',state=ConvState.QUOTED),r,FlowResult())
    assert 'QUOTE_CARD' in out.template_keys and 'DISAMBIG' not in out.template_keys

