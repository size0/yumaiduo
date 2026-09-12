from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .reply_template_store import render_template


def _amount(value: object) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return f"{value / 100:.2f}".rstrip("0").rstrip(".")


def _text(value: object) -> str:
    return str(value or "").strip()

def _template(provider: Any | None, name: str, fallback: str, values: Mapping[str, object]) -> str:
    provider = provider() if callable(provider) else provider
    raw = _text(getattr(provider, name, None)) if provider is not None else ""
    # These two settings are shared with the legacy image-reply renderer. Do
    # not let its old "报价内容"/seat-confirmation contract collapse the
    # canonical two-message contract; a clean user-saved template still wins.
    if name == "recognition_template" and any(marker in raw for marker in ("{报价内容}", "{报价单价}", "请在图片", "分隔符")):
        raw = ""
    if name == "area_quote_template" and "座位是否" in raw:
        raw = ""
    return render_template(raw or fallback, dict(values))


def _date_label(value: object) -> str:
    text = _text(value)
    match = re.fullmatch(r"\d{4}-(\d{1,2})-(\d{1,2})", text)
    return f"{int(match.group(1))}月{int(match.group(2))}日" if match else text


def _purchase_summary(quote: Mapping[str, Any]) -> str | None:
    cinema = _text(quote.get("cinema") or quote.get("cinema_name"))
    movie = _text(quote.get("movie") or quote.get("movie_name"))
    show_date = _date_label(quote.get("quote_date") or quote.get("show_date"))
    start_time = _text(quote.get("showtime_start") or quote.get("start_time"))
    if not all((cinema, movie, show_date, start_time)):
        return None
    return f"{cinema}《{movie}》{show_date}{start_time}这场"


def _purchase_summary_message(quote: Mapping[str, Any]) -> str | None:
    summary = _purchase_summary(quote)
    if summary is None:
        return None
    cinema = _text(quote.get("cinema") or quote.get("cinema_name"))
    movie = _text(quote.get("movie") or quote.get("movie_name"))
    start_time = _text(quote.get("showtime_start") or quote.get("start_time"))
    # Keep the buyer-facing context scannable and stable: one fact per line.
    return f"{cinema}\n{movie}\n{quote.get('quote_date') or quote.get('show_date')} {start_time}"


def _seat_labels(quote: Mapping[str, Any]) -> list[str]:
    items = quote.get("seat_quotes")
    if not isinstance(items, list):
        items = quote.get("selected_seats")
    if not isinstance(items, list):
        return []
    labels = []
    for item in items:
        if isinstance(item, Mapping):
            label = _text(item.get("seat_label") or item.get("seat_number") or item.get("seat_no"))
        else:
            label = _text(item)
        if label:
            labels.append(label)
    return labels


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
    ) -> dict[str, Any]:
        quote = result.get("quote") if isinstance(result.get("quote"), Mapping) else None
        # Keep compatibility with callers that pass a quote record directly.
        if quote is None and any(key in result for key in ("request_type", "unit_sell_price_fen", "total_sell_price_fen")):
            quote = result
        status = _text(result.get("status"))
        reason = _text(result.get("reason"))
        if quote is not None:
            # Resolve once per reply, so both segments share one saved revision.
            templates = self._template_provider() if callable(self._template_provider) else self._template_provider
            values = {
                "影院": quote.get("cinema") or quote.get("cinema_name"),
                "影片": quote.get("movie") or quote.get("movie_name"),
                "城市": quote.get("city"),
                "日期": quote.get("quote_date") or quote.get("show_date"),
                "场次": quote.get("showtime_start") or quote.get("start_time"),
                "影厅": quote.get("hall") or quote.get("hall_name"), "座位": "、".join(_seat_labels(quote)),
                "报价名称": "W+", "报价说明": "",
                "规则版本": quote.get("pricing_rule_version"),
            }
            provider_route = _text(quote.get("provider_route"))
            request_type = _text(quote.get("request_type"))
            summary = _purchase_summary(quote)
            if quote.get("same_type_reference_only") is True:
                unit = _amount(quote.get("unit_sell_price_fen") or quote.get("unit_quote_cents"))
                if unit:
                    return {
                        "kind": "SAME_TYPE_REFERENCE_PREVIEW",
                        "text": f"你刚选的这几个座位现在没了，同类型座位{unit}一张，可以重新选一下座位发我哈",
                    }
            if request_type == "WPLUS_AREA":
                unit = _amount(quote.get("unit_sell_price_fen") or quote.get("unit_quote_cents"))
                count = quote.get("ticket_count")
                total = _amount(quote.get("total_sell_price_fen") or quote.get("total_quote_cents"))
                if isinstance(count, int) and not isinstance(count, bool) and count >= 1 and total and unit:
                    summary_message = _template(templates, "recognition_template", "{影院}\n《{影片}》\n{日期} {场次}", values)
                    price_message = _template(templates, "area_quote_template", "{城市}{影院}《{影片}》{日期} {场次}，{报价单价}/张，共{报价合计}", {
                        **values, "报价单价": unit, "张数": count, "报价合计": total,
                        "张数提示": f"共{count}张，合计{total}元",
                    })
                    order_guidance = _template(
                        templates, "order_submit_unpaid_template",
                        "请直接提交订单，拍下后先不要付款，我这边改价。", {"张数": count},
                    )
                    if order_guidance and order_guidance not in price_message:
                        price_message = f"{price_message}\n{order_guidance}"
                    if summary_message is not None:
                        return {
                            "kind": "QUOTE_READY_WPLUS",
                            "text": summary_message,
                            "messages": [
                                {"kind": "purchase_summary", "text": summary_message},
                                {"kind": "price", "text": price_message},
                            ],
                        }
                    prefix = f"{summary}，" if summary else ""
                    return {
                        "kind": "QUOTE_READY_WPLUS",
                        "text": f"{prefix}W+ {unit}/张，共{count}张{total}。\n{order_guidance}",
                    }
                if unit:
                    summary_message = _template(templates, "recognition_template", "{影院}\n《{影片}》\n{日期} {场次}", values)
                    if summary_message is not None:
                        price_message = _template(templates, "area_quote_template", "W+ {报价单价}一张，需要几张呀。", {**values, "报价单价": unit, "张数提示": "需要几张呀。"})
                        return {
                            "kind": "QUOTE_PREVIEW_WPLUS",
                            # ``text`` remains the first message for old
                            # synchronous callers; durable callers use the
                            # explicit ordered ``messages`` list below.
                            "text": summary_message,
                            "messages": [
                                {"kind": "purchase_summary", "text": summary_message},
                                {"kind": "price", "text": price_message},
                            ],
                        }
            elif request_type == "EXACT_SEATS":
                labels = _seat_labels(quote)
                unit = _amount(quote.get("unit_sell_price_fen") or quote.get("unit_quote_cents"))
                total = _amount(quote.get("total_sell_price_fen") or quote.get("total_quote_cents"))
                if labels and unit and total:
                    prefix = f"{summary}，" if summary else ""
                    return {
                        "kind": "QUOTE_READY_EXACT",
                        "text": _template(templates, "exact_seat_quote_template",
                                        "{城市}{影院}《{影片}》{日期} {场次}，{座位}，{逐座报价}/张，共{报价合计}",
                                        {**values, "逐座报价": unit, "报价合计": total}),
                    }
            if provider_route == "LIANGPIAO" and not quote.get("selected_seats"):
                return {
                    "kind": "SELECTED_SEATS_REQUIRED",
                    "text": "这场需要先选好座位，把选座截图发我就可以哈",
                }
        # These are deterministic business categories, not inferred intent.
        if status == "RECOGNITION_QUALITY_UNAVAILABLE" and "PRICE_MISMATCH" in reason:
            return {
                "kind": "RECOGNITION_CONFIRMATION_REQUIRED",
                "text": "图里的座位和价格信息对不上，我先不乱报价哈。请确认后重新发当前选座图。",
            }
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
        if status == "LIANGPIAO_FACTS_INCOMPLETE" and "SELECTED_SEATS" in reason:
            return {
                "kind": "SELECTED_SEATS_REQUIRED",
                "text": "这场需要先选好座位，把选座截图发我就可以哈",
            }
        if status == "SEAT_FACTS_UNAVAILABLE":
            return {
                "kind": "SEAT_FACTS_UNAVAILABLE",
                "text": "这几个座位现在已经没有了，可以重新选一下座位发我哈",
            }
        if status in {"SHOW_UNRESOLVED", "PRICING_UNAVAILABLE"}:
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
