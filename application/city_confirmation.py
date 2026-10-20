"""Recognize an unchanged, already quoted city from scoped supplier facts."""
import re


def unchanged_quote(tool, city):
    ctx = tool.session.image_context or {}
    from .quote_confirmation import current_quote
    if current_quote(tool) is None:
        return None
    facts = ctx.get('recognition_facts') or {}
    name = facts.get('city_name') or ''
    if not name or facts.get('match_level') != 'EXACT':
        return None
    # Province variants come from the same supplier response, never a guessed
    # city substring (e.g. do not equate 吉林 and 吉林市).
    province = (facts.get('final_results') or {}).get('provinceName') or ''
    short = re.sub(r'(?:回族自治区|维吾尔自治区|壮族自治区|自治区|省)$','',province)
    names = {name, name if name.endswith('市') else name+'市'}
    variants = {prefix+n for prefix in ('',province,short) for n in names}
    if city not in variants or tool.text.strip(' \t\r\n。！!，,；;') not in variants:
        return None
    return name
