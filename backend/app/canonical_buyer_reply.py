from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _amount(value: object) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return f"{value / 100:.2f}".rstrip("0").rstrip(".")


def _text(value: object) -> str:
    return str(value or "").strip()


class CanonicalBuyerReplyRenderer:
    """Render buyer text from canonical structured outcomes only.

    This renderer deliberately has no message parsing, keyword matching, model
    call, provider call, or transaction side effect.  It is an expression layer
    for an already completed canonical fact/quote decision.
    """

    def __init__(self, template_provider: Any | None = None) -> None:
        self._template_provider = template_provider

    def render(
        self,
        result: Mapping[str, Any],
        *,
        transaction_state: object | None = None,
    ) -> dict[str, str]:
        quote = result.get("quote") if isinstance(result.get("quote"), Mapping) else None
        status = _text(result.get("status"))
        reason = _text(result.get("reason"))
        if quote is not None:
            provider_route = _text(quote.get("provider_route"))
            request_type = _text(quote.get("request_type"))
            if request_type == "WPLUS_AREA":
                unit = _amount(quote.get("unit_sell_price_fen") or quote.get("unit_quote_cents"))
                count = quote.get("ticket_count")
                total = _amount(quote.get("total_sell_price_fen") or quote.get("total_quote_cents"))
                if isinstance(count, int) and not isinstance(count, bool) and count >= 1 and total and unit:
                    return {
                        "kind": "QUOTE_READY_WPLUS",
                        "text": f"W+这场{unit}/张，共{count}张{total}，直接拍就行哈",
                    }
                if unit:
                    return {
                        "kind": "QUOTE_PREVIEW_WPLUS",
                        "text": f"W+这场{unit}/张，需要几张呀",
                    }
            elif request_type == "EXACT_SEATS":
                seat_quotes = quote.get("seat_quotes")
                entries = []
                if isinstance(seat_quotes, list):
                    for item in seat_quotes:
                        if not isinstance(item, Mapping):
                            continue
                        label = _text(item.get("seat_label") or item.get("seat_number"))
                        price = _amount(item.get("sell_price_fen") or item.get("unit_quote_cents"))
                        if label and price:
                            entries.append(f"{label} {price}")
                if not entries and isinstance(quote.get("selected_seats"), list):
                    unit = _amount(quote.get("unit_sell_price_fen") or quote.get("unit_quote_cents"))
                    if unit:
                        entries = [
                            f"{_text(item.get('seat_no') or item.get('seat_number') or item.get('seat_label'))} {unit}"
                            for item in quote["selected_seats"]
                            if isinstance(item, Mapping)
                            and _text(item.get("seat_no") or item.get("seat_number") or item.get("seat_label"))
                        ]
                total = _amount(quote.get("total_sell_price_fen") or quote.get("total_quote_cents"))
                if entries and total:
                    text = f"{'、'.join(entries)}，直接拍就行哈"
                    if len(entries) > 1:
                        text = f"{'、'.join(entries)}，合计{total}，直接拍就行哈"
                    return {"kind": "QUOTE_READY_EXACT", "text": text}
            if provider_route == "LIANGPIAO" and not quote.get("selected_seats"):
                return {
                    "kind": "SELECTED_SEATS_REQUIRED",
                    "text": "这场需要先选好座位，把选座截图发我就可以哈",
                }
        # These are deterministic business categories, not inferred intent.
        if status == "ROUTE_UNRESOLVED" and "CITY_REQUIRED" in reason:
            return {
                "kind": "UNRESOLVED_REQUIRED_FIELD",
                "text": "麻烦补充一下城市，我再帮你确认影院、场次和价格哈。",
            }
        if status in {"SELECTED_SEATS_REQUIRED", "MANUAL_MARK_REQUIRED"} or "MANUAL_MARK" in reason:
            return {
                "kind": "MANUAL_MARK_UNKNOWN",
                "text": "暂时无法判断图片中的手绘标记，请重新发送清晰的座位图后我再核价。",
            }
        if status == "PROBE_REQUIRED" or "PROBE" in reason or "COST_REQUIRED" in reason:
            return {
                "kind": "PROBE_REQUIRED",
                "text": "当前还无法取得可核验的实时价格，我先不乱报价哈。",
            }
        if status == "LIANGPIAO_FACTS_INCOMPLETE" or "SELECTED_SEATS" in reason:
            return {
                "kind": "SELECTED_SEATS_REQUIRED",
                "text": "这场需要先选好座位，把选座截图发我就可以哈",
            }
        if status in {"SHOW_UNRESOLVED", "SEAT_FACTS_UNAVAILABLE", "PRICING_UNAVAILABLE"}:
            return {
                "kind": "UNRESOLVED_REQUIRED_FIELD",
                "text": "当前场次暂时无法取得可核验价格，请发送最新场次截图后再核价哈。",
            }
        return {
            "kind": "UNRESOLVED_REQUIRED_FIELD",
            "text": "这张图暂时无法确认报价，我先不乱报价哈。",
        }

    def render_text(self, result: Mapping[str, Any], *, transaction_state: object | None = None) -> str:
        return self.render(result, transaction_state=transaction_state)["text"]
