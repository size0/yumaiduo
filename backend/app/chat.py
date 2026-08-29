from __future__ import annotations

import re

from .models import MovieImageInfo, RealQuote
from .reply_template_store import ReplyTemplates, render_template


def _cents(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{value / 100:.2f}"


def _direct_unit_quote(quote: RealQuote | None) -> str | None:
    if quote is None:
        return None
    if quote.unit_quote_cents is not None:
        return _cents(quote.unit_quote_cents)
    unit_prices = {item.unit_quote_cents for item in quote.seat_quotes}
    return _cents(next(iter(unit_prices))) if len(unit_prices) == 1 else None


def _quote_explanation(quote: RealQuote) -> str:
    notes: list[str] = []
    if quote.same_type_probe_used:
        notes.append("原座位不可锁，已用相同官方区域和座位类型的可售座位核价；未跨座位类型。")
    if "W+会员专享" in quote.pricing_source:
        notes.append("价格基准来自临时锁座后的W+会员专享优惠；临时订单已取消并确认座位恢复可售。")
    if quote.pricing_rule_version:
        notes.append(f"最终展示金额已应用后台确定性报价规则（{quote.pricing_rule_version}）。")
    return "\n".join(notes)


def _display_date(recognition: MovieImageInfo, quote: RealQuote | None) -> str | None:
    resolved = quote.quote_date if quote and quote.quote_date else recognition.date
    if resolved is None:
        return recognition.date_text
    weekday = "一二三四五六日"[resolved.weekday()]
    relative = "，今天" if "今天" in (recognition.date_text or "") else ""
    return f"{resolved:%m月%d日}（周{weekday}{relative}）"


def _recognition_variables(recognition: MovieImageInfo, quote: RealQuote | None) -> dict[str, object]:
    cinema = quote.matched_cinema_name if quote and quote.matched_cinema_name else recognition.cinema_name
    city = quote.matched_city_name if quote and quote.matched_city_name else recognition.city
    start = quote.matched_showtime_start if quote and quote.matched_showtime_start else recognition.showtime_start
    end = quote.matched_showtime_end if quote and quote.matched_showtime_end else recognition.showtime_end
    hall = quote.matched_hall_name if quote and quote.matched_hall_name else recognition.hall_name
    return {
        "影片": quote.matched_movie_name if quote and quote.matched_movie_name else recognition.movie_name,
        "城市": city,
        "影院": cinema,
        "日期": _display_date(recognition, quote),
        "场次": "–".join(filter(None, (start, end))) or None,
        "影厅": hall,
        "座位": f"可见已选座：{recognition.seat_display}" if recognition.selected_seats else "座位：W+座位",
    }


def _recognition_quote_variables(quote: RealQuote | None) -> dict[str, object]:
    if quote is None:
        return {"报价名称": None, "逐座报价": None, "报价合计": None, "报价单价": None, "报价金额": None}
    seat_prices = "、".join(
        f"{item.seat_number} {_cents(item.unit_quote_cents)}" for item in quote.seat_quotes
    ) or None
    label = "后台实时报价" if quote.pricing_rule_version else "万达官方实时区域参考价"
    unit_quote = _direct_unit_quote(quote)
    total_quote = _cents(quote.total_quote_cents)
    # Area previews know the authoritative per-seat quote before the buyer gives
    # a quantity.  Outer templates that put unit and total on the same line must
    # still render that known one-seat amount instead of dropping the whole line.
    one_seat_total = total_quote or (unit_quote if quote.needs_ticket_count else None)
    return {
        "报价名称": label,
        "逐座报价": seat_prices,
        "报价合计": one_seat_total,
        "报价单价": unit_quote,
        "报价金额": total_quote,
    }


def _seat_value_for_template(template: str, recognition: MovieImageInfo, fallback: object) -> object:
    if re.search(r"座位\s*[：:]\s*\{座位\}", template):
        return recognition.seat_display
    return fallback


def _standalone_quote_template(template: str) -> bool:
    return any(
        f"{{{placeholder}}}" in template
        for placeholder in ("影片", "城市", "影院", "日期", "场次", "影厅", "座位")
    )


def _quote_block(recognition: MovieImageInfo, quote: RealQuote, templates: ReplyTemplates) -> str:
    explanation = _quote_explanation(quote)
    recognition_values = _recognition_variables(recognition, quote)
    if quote.quote_scope == "exact_seats":
        seat_prices = "、".join(
            f"{item.seat_number} {_cents(item.unit_quote_cents)}"
            for item in quote.seat_quotes
        )
        label = "后台实时报价" if quote.pricing_rule_version else "万达官方实时区域参考价"
        return render_template(templates.exact_quote_template, {
            **recognition_values,
            **_recognition_quote_variables(quote),
            "座位": _seat_value_for_template(
                templates.exact_quote_template, recognition, recognition_values["座位"],
            ),
            "报价名称": label,
            "逐座报价": seat_prices,
            "报价合计": _cents(quote.total_quote_cents),
            "报价说明": explanation,
            "规则版本": quote.pricing_rule_version,
        })
    return render_template(templates.area_quote_template, {
        **recognition_values,
        "报价名称": "W+专享区后台报价单价" if quote.pricing_rule_version else "W+专享区官方参考价",
        "报价单价": _cents(quote.unit_quote_cents),
        "张数提示": "请告诉我需要几张。" if quote.needs_ticket_count else "",
        "报价说明": explanation,
        "规则版本": quote.pricing_rule_version,
    })


def _safe_quote_failure_reply(
    recognition: MovieImageInfo, quote_error: str | None, templates: ReplyTemplates,
) -> str | None:
    cinema = (recognition.cinema_name or "").strip()
    error = (quote_error or "").strip()
    # “寰映影城” is Wanda's official cinema brand and is present in the
    # Wanda cinema directory. Do not reject it as a third-party cinema before
    # the authoritative cache/showtime resolver gets a chance to match it.
    if cinema and not any(brand in cinema for brand in ("万达", "寰映")):
        return templates.unsupported_cinema_template
    if error and "影院" in error:
        return render_template(templates.cinema_match_failure_template, {"影院": cinema or "截图中的影院"})
    if error and "场次" in error:
        return templates.showtime_changed_template
    if recognition.missing_fields and (not error or any(marker in error for marker in ("缺少", "缺失", "完整", "需要"))):
        labels = {
            "city": "城市", "cinema_name": "影院全名", "movie_name": "影片",
            "date_text": "日期", "date": "日期", "showtime_start": "开场时间",
        }
        missing_labels: list[str] = []
        for field in recognition.missing_fields:
            label = labels.get(field)
            if label and label not in missing_labels:
                missing_labels.append(label)
        if missing_labels:
            return render_template(
                templates.missing_fields_template,
                {"缺失信息": "、".join(missing_labels)},
            )
    if error:
        safe_failure_reason = (
            error
            if "当前不可选" in error and "同座位类型当前参考价" in error
            else "当前场次暂未取得可核验价格"
        )
        return render_template(
            templates.quote_unavailable_template,
            {"失败原因": safe_failure_reason},
        )
    return None


def build_recognition_reply(
    recognition: MovieImageInfo,
    *,
    quote: RealQuote | None = None,
    quote_error: str | None = None,
    templates: ReplyTemplates | None = None,
) -> str:
    """Render structured facts without exposing screenshot prices or language/format."""
    configured = templates or ReplyTemplates()
    if recognition.match_level == "CANDIDATE" and recognition.candidate_cinemas:
        lines = [
            f"{index}、{candidate.city_name + ' ' if candidate.city_name else ''}{candidate.name}"
            + (f"（{candidate.address}）" if candidate.address else "")
            for index, candidate in enumerate(recognition.candidate_cinemas, start=1)
        ]
        return "识别到多个可能的影院，请回复序号确认：\n" + "\n".join(lines) + "\n请回复对应序号（如“1”），确认后我再重新获取当前场次和座位报价。"
    if quote is not None:
        quote_content = _quote_block(recognition, quote, configured)
        quote_template = (
            configured.exact_quote_template
            if quote.quote_scope == "exact_seats" else configured.area_quote_template
        )
        if _standalone_quote_template(quote_template):
            return quote_content
    else:
        # Only deterministic, buyer-safe business categories may be shown. Raw
        # provider, matching, temporary-lock, and release errors remain audit-only.
        safe_failure = _safe_quote_failure_reply(recognition, quote_error, configured)
        if safe_failure is not None:
            return safe_failure
        quote_content = render_template(configured.no_quote_template, {})
    return render_template(configured.recognition_template, {
        **_recognition_variables(recognition, quote),
        **_recognition_quote_variables(quote),
        "报价内容": quote_content,
    })


def build_guidance_reply(_: str, *, templates: ReplyTemplates | None = None) -> str:
    return (templates or ReplyTemplates()).guidance_template
