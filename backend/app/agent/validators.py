from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .observations import Observation


_PRICE_PATTERN = re.compile(r"(?:¥|￥|人民币)?\s*(\d{1,8}(?:\.\d{1,2})?)\s*元")
_LABELED_PRICE_PATTERN = re.compile(r"(?:每张|单张|一共|合计|总价|共计|共)\s*(\d{1,8}(?:\.\d{1,2})?)\s*(?:元)?")
_UNIT_PRICE_PATTERN = re.compile(r"(?:每张|单张)\s*(\d{1,8}(?:\.\d{1,2})?)\s*(?:元)?")
_QUANTITY_PATTERN = re.compile(r"(?<!\d)(\d{1,2})\s*(?:张|张票|张电影票)")
_CN_QUANTITY = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cents(value: object) -> int | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(round(float(text) * 100))
    except (TypeError, ValueError):
        return None


def validate_quote_reply(reply: str, observations: Iterable[Observation]) -> tuple[bool, str]:
    """Validate prices in a final reply against this turn's quote observation.

    This is deliberately narrow: prose without an amount is not rewritten, but
    any amount presented as RMB must be backed by a successful quote. Unit
    prices are allowed when the reply also contains the authoritative total.
    """
    prices = [_cents(match.group(1)) for match in _PRICE_PATTERN.finditer(reply)]
    prices.extend(_cents(match.group(1)) for match in _LABELED_PRICE_PATTERN.finditer(reply))
    prices = [value for value in prices if value is not None]
    if not prices:
        return True, "no_price_claim"
    quote: Mapping[str, Any] | None = None
    for observation in observations:
        if not observation.ok or observation.code not in {"quote_ready", "OK_QUOTE"}:
            continue
        facts = observation.facts
        if facts.get("total_price_cents") is not None:
            quote = facts
    if quote is None:
        return False, "price_without_successful_quote"
    total = quote.get("total_price_cents")
    try:
        total_cents = int(total)
    except (TypeError, ValueError):
        return False, "quote_total_invalid"
    quantity = quote.get("quantity")
    unit_prices = [_cents(match.group(1)) for match in _UNIT_PRICE_PATTERN.finditer(reply)]
    if unit_prices:
        authoritative_unit = quote.get("unit_price_cents")
        if authoritative_unit is None and quantity is not None and int(quantity) > 0 and total_cents % int(quantity) == 0:
            authoritative_unit = total_cents // int(quantity)
        if authoritative_unit is None or any(value != int(authoritative_unit) for value in unit_prices):
            return False, "quote_unit_mismatch"
    mentioned_quantity = [int(match.group(1)) for match in _QUANTITY_PATTERN.finditer(reply)]
    mentioned_quantity.extend(_CN_QUANTITY[value] for value in re.findall(r"([一二两三四五六七八九十])\s*张", reply))
    if quantity is not None and mentioned_quantity and any(value != int(quantity) for value in mentioned_quantity):
        return False, "quote_quantity_mismatch"
    if total_cents not in prices:
        return False, "quote_total_mismatch"
    return True, "quote_facts_match"
