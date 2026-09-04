from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.models import RealQuote, RealSeatQuote
from app.selected_seat_quote_service import SelectedSeat, SelectedSeatQuoteResult

from .errors import PricingError
from .models import PricingFacts, QuoteResult


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PricingError("compatibility_input_invalid", f"{field}不能为空。")
    return text


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _required(value, "datetime")


def _legacy_quote_route(result: QuoteResult) -> str:
    return "liangpiao_exact" if result.provider == "LIANGPIAO" else "wanda_self"


def _legacy_price_source(result: QuoteResult) -> str:
    if result.price_source:
        return result.price_source
    if result.provider == "LIANGPIAO":
        return "liangpiao_realtime_preflight"
    if result.seat_type == "wplus":
        return "realtime_wplus_area"
    if result.seat_type == "regular":
        return "realtime_regular_area"
    if result.seat_type == "mixed":
        return "realtime_mixed_area"
    return "realtime_vip_area"


def to_real_quote(
    result: QuoteResult,
    *,
    quote_date: date | str | None = None,
    matched_cinema_name: str | None = None,
    matched_city_name: str | None = None,
    matched_movie_name: str | None = None,
    matched_showtime_start: str | None = None,
    matched_showtime_end: str | None = None,
    matched_hall_name: str | None = None,
    quote_generation: int | None = None,
    detail: str = "",
) -> RealQuote:
    """Adapt a result to RealQuote; this function never derives a new amount."""
    parsed_date = date.fromisoformat(quote_date) if isinstance(quote_date, str) and quote_date else quote_date
    return RealQuote(
        quote_id=result.quote_id, record_id=result.record_id, event_id=result.event_id,
        recognition_snapshot_id=result.recognition_snapshot_id, provider=result.provider,
        quote_route=result.quote_route, generation=quote_generation or result.generation,
        quote_expires_at=result.quote_expires_at,
        provider_max_amount_cents=result.provider_max_amount_cents,
        buyer_quote_cents=result.buyer_quote_cents,
        order_max_price_cents=result.order_max_price_cents,
        calculation_evidence=dict(result.calculation_evidence),
        quote_scope=result.quote_scope, quote_date=parsed_date,
        seat_zone_type=result.seat_zone_type,
        member_unit_price_cents=result.member_unit_price_cents,
        original_unit_price_cents=result.original_unit_price_cents,
        seat_type=result.seat_type, base_unit_cents=result.base_unit_cents,
        base_total_cents=result.base_total_cents, price_source=_legacy_price_source(result),
        price_mode=result.price_mode or "FIXED", max_price_cents=result.max_price_cents,
        unit_quote_cents=result.unit_quote_cents, total_quote_cents=result.total_quote_cents,
        channel_fee_total_cents=result.channel_fee_total_cents,
        seat_quotes=[RealSeatQuote(
            seat_number=seat.seat_label, seat_zone_type=seat.zone_type,
            original_price_cents=seat.original_price_cents,
            member_price_cents=seat.member_cost_cents,
            channel_fee_cents=seat.channel_fee_cents,
            unit_quote_cents=seat.unit_quote_cents,
        ) for seat in result.seat_quotes],
        ticket_count=result.ticket_count, needs_ticket_count=result.needs_ticket_count,
        pricing_source=result.pricing_source, pricing_rule_version=result.pricing_rule_version,
        detail=detail, matched_cinema_name=matched_cinema_name,
        matched_city_name=matched_city_name, matched_movie_name=matched_movie_name,
        matched_showtime_start=matched_showtime_start, matched_showtime_end=matched_showtime_end,
        matched_hall_name=matched_hall_name, provider_quote_id=result.provider_quote_id,
        provider_quote_hash=result.provider_quote_hash, quote_generation=quote_generation,
    )


def _selected_seats(facts: PricingFacts) -> list[SelectedSeat]:
    seats: list[SelectedSeat] = []
    for item in facts.seats:
        row, col = _coordinates(item.seat_label)
        if row is None or col is None:
            raise PricingError("compatibility_seat_identity_missing", "良票兼容映射缺少座位坐标。")
        seats.append(SelectedSeat(row_no=row, col_no=col, seat_no=item.seat_label, area_id=item.area_id or None))
    return seats


def _coordinates(label: str) -> tuple[int | None, int | None]:
    import re
    match = re.search(r"(\d+)\s*[排排行]\s*(\d+)\s*[座号號]", label)
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def to_selected_seat_quote_result(
    result: QuoteResult,
    *,
    facts: PricingFacts,
    quote_id: str,
    quote_hash: str,
    expires_at: datetime,
    generation: int,
    trace_id: str,
) -> SelectedSeatQuoteResult:
    """Adapt a Liangpiao result using preflight seat identity and lineage facts."""
    if result.provider != "LIANGPIAO":
        raise PricingError("compatibility_route_invalid", "只有良票结果可以映射为SelectedSeatQuoteResult。")
    wanted_id = _required(quote_id, "quote_id")
    wanted_hash = _required(quote_hash, "quote_hash")
    if len(wanted_hash) != 64:
        raise PricingError("compatibility_input_invalid", "quote_hash长度无效。")
    return SelectedSeatQuoteResult(
        quote_id=wanted_id, quote_hash=wanted_hash, show_id=facts.show_id,
        price_mode=result.price_mode or "FIXED", seats=_selected_seats(facts),
        provider_amount_fen=result.provider_amount_cents or result.base_total_cents or result.total_quote_cents or 0,
        buyer_amount_fen=result.total_quote_cents or 0, max_price_fen=result.max_price_cents,
        operator_pricing_applied=result.operator_pricing_applied,
        operator_markup_percent=result.operator_markup_percent,
        pricing_rule_version=result.pricing_rule_version or "pricing-unversioned",
        expires_at=expires_at, preflight_verified=facts.preflight_verified,
        generation=generation, trace_id=_required(trace_id, "trace_id"),
        snapshot={
            "quote_route": result.quote_route, "ticket_mode": facts.ticket_mode,
            "area_quote_strategy": facts.area_quote_strategy,
            "estimate_amount_fen": facts.provider_estimate_amount_cents,
            "total_amount_fen": facts.provider_total_amount_cents,
            "market_amount_fen": facts.provider_market_amount_cents,
        },
    )


def to_quote_record(
    result: QuoteResult,
    *,
    record_id: str,
    quote_id: str,
    tenant_id: str,
    shop_id: str,
    buyer_id: str,
    chat_id: str,
    created_at: datetime | str,
    quote_expires_at: datetime | str,
    item_id: str | None = None,
    order_id: str | None = None,
    event_id: str | None = None,
    recognition_snapshot_id: str | None = None,
    generation: int | None = None,
    source: str = "pricing_engine",
) -> dict[str, Any]:
    """Build a QuoteRecord-shaped snapshot without persistence or ID generation."""
    record = {
        "record_id": _required(record_id, "record_id"),
        "quote_id": _required(quote_id, "quote_id"),
        "tenant_id": _required(tenant_id, "tenant_id"), "shop_id": _required(shop_id, "shop_id"),
        "buyer_id": _required(buyer_id, "buyer_id"), "chat_id": _required(chat_id, "chat_id"),
        "created_at": _iso(created_at), "quote_expires_at": _iso(quote_expires_at),
        "item_id": item_id, "order_id": order_id, "event_id": event_id,
        "recognition_snapshot_id": recognition_snapshot_id,
        "generation": generation if generation is not None else result.generation,
        "source": source,
        "status": "succeeded", "delivery_state": "pending",
        # Existing transaction code branches on these lowercase route values;
        # retain them while preserving the canonical route in explicit fields.
        "route": _legacy_quote_route(result), "quote_route": _legacy_quote_route(result),
        "legacy_quote_route": _legacy_quote_route(result),
        "canonical_quote_route": result.quote_route,
        "provider_route": result.quote_route, "pricing_quote_route": result.quote_route,
        "provider": result.provider,
        "quote_scope": result.quote_scope, "ticket_count": result.ticket_count,
        "member_unit_price_cents": result.member_unit_price_cents,
        "original_unit_price_cents": result.original_unit_price_cents,
        "base_unit_cents": result.base_unit_cents, "base_total_cents": result.base_total_cents,
        "unit_quote_cents": result.unit_quote_cents, "total_quote_cents": result.total_quote_cents,
        "max_price_cents": result.max_price_cents, "price_mode": result.price_mode,
        "price_source": _legacy_price_source(result), "pricing_source": result.pricing_source,
        "pricing_rule_version": result.pricing_rule_version,
        "provider_amount_cents": result.provider_amount_cents,
        "provider_max_amount_cents": result.provider_max_amount_cents,
        "buyer_quote_cents": result.buyer_quote_cents,
        "order_max_price_cents": result.order_max_price_cents,
        "provider_quote_id": result.provider_quote_id, "provider_quote_hash": result.provider_quote_hash,
        "supersedes_quote_id": result.supersedes_quote_id,
        "calculation_evidence": dict(result.calculation_evidence),
        "semantic_flags": list(result.semantic_flags),
        "seat_quotes": [
            {"seat_id": seat.seat_id, "seat_number": seat.seat_label,
             "seat_zone_type": seat.zone_type, "original_price_cents": seat.original_price_cents,
             "member_price_cents": seat.member_cost_cents, "channel_fee_cents": seat.channel_fee_cents,
             "unit_quote_cents": seat.unit_quote_cents}
            for seat in result.seat_quotes
        ],
    }
    return record
