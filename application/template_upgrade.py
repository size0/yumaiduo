"""Explicit migration of known legacy copy; leaves unrelated shop wording intact."""
from copy import deepcopy


def upgrade(data):
    result = deepcopy(data)
    rows = result.get('templates', result)
    if not isinstance(rows, dict):
        return result
    for key in ('QUOTE_CARD', 'QUOTE_CARD_MULTI', 'RECOGNIZING_TIP'):
        row = rows.get(key)
        def clean(text):
            if not isinstance(text, str):
                return text
            if key in ('QUOTE_CARD', 'QUOTE_CARD_MULTI'):
                return (text.replace('座位是否在图片中标记？需要几张票？', '张数：{count}张，请核对以上场次、座位和金额。')
                    .replace('[ggx1z82tg84]', '').replace('[kdpdte8g6cg]', ''))
            if key == 'RECOGNIZING_TIP':
                return text.replace('系统正在识别，', '')
            return text
        if isinstance(row, str):
            rows[key] = clean(row)
        elif isinstance(row, dict):
            if 'text' in row:
                row['text'] = clean(row['text'])
            for channel, value in (row.get('channel_text') or {}).items():
                row['channel_text'][channel] = clean(value)
    return result
