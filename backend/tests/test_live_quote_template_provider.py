from copy import deepcopy

from app.canonical_buyer_reply import CanonicalBuyerReplyRenderer
from app.reply_template_store import ReplyTemplateStore


def test_saved_templates_are_read_live_without_mutating_quote(tmp_path):
    store = ReplyTemplateStore(tmp_path / 'templates.json')
    store.save({'recognition_template': '{影院}\n《{影片}》\n{日期} {场次}',
                'area_quote_template': '价格{报价单价}\n{张数提示}'})
    renderer = CanonicalBuyerReplyRenderer(store.current)
    quote = {'request_type': 'WPLUS_AREA', 'cinema': '测试影院', 'movie': '测试影片',
             'quote_date': '2026-09-12', 'showtime_start': '16:10',
             'unit_sell_price_fen': 6120, 'ticket_count': None, 'total_sell_price_fen': None}
    before = deepcopy(quote)
    first = renderer.render({'quote': quote})
    assert first['messages'][0]['text'] == '测试影院\n《测试影片》\n2026-09-12 16:10'
    assert first['messages'][1]['text'] == '价格61.2\n需要几张呀。'
    store.save({'area_quote_template': '更新{报价单价}\n{张数提示}'})
    second = renderer.render({'quote': quote})
    assert second['messages'][1]['text'] == '更新61.2\n需要几张呀。'
    assert quote == before
    ready = renderer.render({'quote': {**quote, 'ticket_count': 2, 'total_sell_price_fen': 12240}})
    assert ready['messages'][1]['text'] == '更新61.2\n共2张，合计122.4元\n请直接提交订单，拍下后先不要付款，我这边改价。'
    assert '{' not in ready['messages'][1]['text']
