"""Retired batch module: only the literal quantity grammar remains for compatibility."""
from datetime import datetime
import re


def positive_quantity(text):
    """Full literal quantity grammar, not intent or arbitrary digit extraction."""
    value=text.strip(' \t\r\n。！!，,；;')
    m=re.fullmatch(r'(?:还是|仍然|仍是|仍旧|依旧|就要|就|要|我要|需要|改成|改为)?\s*'
                   r'((?:这|那)?(?:[1-9][0-9]?|[一二两三四五六七八九十俩]+)(?:张(?:票)?|个(?:人)?|人|位)?)',value)
    if m is None:
        return None
    from .semantic_request import number
    return number(m.group(1).removesuffix('票'))

