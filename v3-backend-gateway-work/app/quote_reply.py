from __future__ import annotations

import re

from fastapi import HTTPException

from .plugin_bridge_store import DEFAULT_REPLY_TEMPLATES
from .schemas import QuoteRealtimeResponse, Recognition


QUOTE_REPLY_VARIABLES = frozenset({"城市", "城市标记", "影院", "影片", "日期", "场次", "影厅", "座位", "张数", "单价", "合计", "座位类型", "价格来源", "缺失信息", "排数", "可选座位"})


def validate_quote_reply_template(value: object) -> str:
    template = value.strip() if isinstance(value, str) else ""
    if not template or len(template) > 500:
        raise HTTPException(status_code=422, detail="invalid quote reply template")
    variables = re.findall(r"\{([^{}]+)\}", template)
    remaining = re.sub(r"\{[^{}]+\}", "", template)
    if "{" in remaining or "}" in remaining or any(variable not in QUOTE_REPLY_VARIABLES for variable in variables):
        raise HTTPException(status_code=422, detail="invalid quote reply template")
    # Reject concrete fabricated facts and affirmative inventory promises, but
    # permit safe wording such as “不为买家保留座位” and “余票以出票时为准”.
    prohibited = r"(?:[¥￥]\s*\d|\d+(?:\.\d{1,2})?\s*(?:元|块)|\d+\s*张|\d+排\s*\d+座|库存充足|余票充足|保证有票|已锁座|已保留座位|承诺出票|出票成功)"
    if re.search(prohibited, remaining):
        raise HTTPException(status_code=422, detail="unsafe quote reply template")
    return template


def quote_reply_template(runtime: dict[str, object], quote: QuoteRealtimeResponse) -> str:
    templates = runtime.get("reply_templates")
    templates = templates if isinstance(templates, dict) else {}
    if quote.buyer_app_purchase_recommended:
        key = "quote_buyer_app_better_price"
    elif quote.needs_ticket_count:
        key = "quote_need_count"
    elif quote.quote_scope.value == "exact_seats":
        key = "quote_exact"
    else:
        key = "quote_area"
    return str(templates.get(key, DEFAULT_REPLY_TEMPLATES[key]))


def static_reply_text(template: str) -> str:
    """Buyer-facing failure copy is fully managed by backend reply templates."""
    return template.strip()


def quote_reply_text(quote: QuoteRealtimeResponse, recognition: Recognition, template: str) -> str:
    """Render merchant text from verified facts without hidden buyer-facing suffixes."""
    if quote.buyer_app_purchase_recommended:
        return static_reply_text(template)
    if quote.seat_quotes and quote.unit_quote_cents is None:
        parts = [f"{item.seat_number[:24]} {item.unit_quote_cents / 100:.2f}元" for item in quote.seat_quotes]
        detail = "、".join(parts)
        # Standard Wanda labels fit comfortably. For pathological upstream
        # labels, keep the authoritative per-seat facts in seat_quotes and use
        # a bounded grouped summary so response validation cannot drop a quote.
        if len(detail) > 360:
            counts: dict[int, int] = {}
            for item in quote.seat_quotes:
                counts[item.unit_quote_cents] = counts.get(item.unit_quote_cents, 0) + 1
            detail = "、".join(f"{price / 100:.2f}元×{count}" for price, count in sorted(counts.items()))
        total = quote.total_quote_cents or sum(item.unit_quote_cents for item in quote.seat_quotes)
        seats = "、".join(item.seat_number[:24] for item in quote.seat_quotes)
        city = recognition.city or ""
        city_label = f"【{city}的】" if city else ""
        header = (
            f"※{city_label}| {quote.matched_cinema_name or recognition.cinema or ''}\n"
            f"电影：{recognition.movie or ''}\n影厅：{recognition.hall or ''}\n"
            f"场次：{recognition.date.isoformat() if recognition.date else ''} {recognition.showtime or ''}\n座位：{seats}"
        )
        return f"{header}\n\n按官方选座逐座实时核验：{detail}；{len(quote.seat_quotes)}张合计{total / 100:.2f}元。"
    values = {
        "城市": recognition.city or "", "城市标记": f"【{recognition.city}的】" if recognition.city else "",
        "影院": quote.matched_cinema_name or recognition.cinema or "", "影片": recognition.movie or "",
        "日期": recognition.date.isoformat() if recognition.date else "", "场次": recognition.showtime or "",
        "影厅": recognition.hall or "",
        "座位": "、".join(item.seat_number[:24] for item in quote.seat_quotes),
        "张数": str(quote.ticket_count or recognition.official_selection.selected_count or ""),
        # Quotes are executable order amounts. Preserve cents so an operator
        # can enter exactly the same amount during a manual price change.
        "单价": f"{quote.unit_quote_cents / 100:.2f}" if quote.unit_quote_cents is not None else "",
        "合计": f"{quote.total_quote_cents / 100:.2f}" if quote.total_quote_cents is not None else "",
        "座位类型": quote.seat_zone_type.value, "价格来源": quote.pricing_source,
    }
    return re.sub(r"\{([^{}]+)\}", lambda match: values.get(match.group(1), ""), template).strip()
