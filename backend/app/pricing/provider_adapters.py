from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import PricingError
from .models import PricingFacts, PricingSeatFact


_AMOUNT_FIELDS = re.compile(r"^\d+$")


def _value(source: object, *names: str) -> Any:
    if isinstance(source, Mapping):
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
    else:
        for name in names:
            value = getattr(source, name, None)
            if value is not None:
                return value
    return None


def _text(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PricingError("pricing_input_invalid", f"{field}不能为空。")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _cents(value: object, *, field: str, required: bool = False) -> int | None:
    if value is None or value == "":
        if required:
            raise PricingError("provider_amount_missing", f"{field}缺少分金额。")
        return None
    if isinstance(value, bool):
        raise PricingError("provider_amount_invalid", f"{field}必须是分字符串或整数分。")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _AMOUNT_FIELDS.fullmatch(value.strip()):
        parsed = int(value.strip())
    else:
        raise PricingError("provider_amount_invalid", f"{field}必须是分字符串或整数分。")
    if parsed <= 0:
        if required:
            raise PricingError("provider_amount_invalid", f"{field}必须为正整数分。")
        return None
    return parsed


def _items(source: object, *names: str) -> list[Any]:
    value = _value(source, *names)
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        nested = _value(value, "items", "seats", "list", "data")
        return nested if isinstance(nested, list) else []
    return []


def _bool(source: object, *names: str, default: bool = False) -> bool:
    value = _value(source, *names)
    return default if value is None else value is True


def _seat_identity(raw: object, index: int) -> tuple[str, str]:
    seat_id = _optional_text(_value(raw, "seatId", "seat_id", "id", "code"))
    label = _optional_text(_value(raw, "seatNumber", "seat_number", "seatNo", "seat_no", "name"))
    row = _value(raw, "rowNo", "row_no", "row")
    col = _value(raw, "colNo", "col_no", "column", "col")
    if not label and row is not None and col is not None:
        label = f"{row}排{col}座"
    label = label or f"座位{index + 1}"
    return seat_id or f"{row or ''}:{col or ''}:{index + 1}", label


def _wanda_seat(raw: object, index: int) -> PricingSeatFact:
    seat_id, label = _seat_identity(raw, index)
    physical_wplus = _bool(raw, "physicalWplus", "physical_wplus", "wplus", default=False)
    zone_type = str(_value(raw, "zoneType", "zone_type") or ("WPLUS" if physical_wplus else "REGULAR")).upper()
    # A probe may be the authoritative source for both original and member
    # cost; leave original absent here so with_probe_cost can fill it.
    original = _cents(_value(raw, "originalPriceCents", "original_price_cents", "salesPriceCents", "sales_price_cents"), field="original_price_cents")
    member = _cents(
        _value(raw, "wplusMemberPrice", "wplus_member_price") if physical_wplus
        else _value(raw, "regularMemberPrice", "regular_member_price"),
        field="member_cost_cents",
    )
    return PricingSeatFact(
        seat_id=seat_id, seat_label=label,
        area_id=str(_value(raw, "areaId", "area_id") or ""),
        area_code=str(_value(raw, "areaCode", "area_code", "code") or ""),
        area_name=str(_value(raw, "areaName", "area_name") or ""),
        zone_type=zone_type, physical_wplus=physical_wplus,
        original_price_cents=original, member_cost_cents=member,
        channel_fee_cents=_cents(_value(raw, "channelFeeCents", "channel_fee_cents"), field="channel_fee_cents") or 0,
        availability_verified=_value(raw, "available", "isAvailable", "availabilityVerified") is not False,
        cost_source=str(_value(raw, "costSource", "cost_source") or "wanda_official"),
    )


def _probe_value(source: object, *names: str) -> Any:
    return _value(source, *names)


class WandaPricingFactsAdapter:
    """Convert already-read Wanda seat facts and optional Probe facts only."""

    def adapt(self, payload: Mapping[str, Any], *, probe_results: Sequence[object] | Mapping[str, object] | object | None = None) -> PricingFacts:
        raw_seats = _items(payload, "seats", "selectedSeats", "selected_seats", "officialSeats", "official_seats")
        seats = tuple(_wanda_seat(item, index) for index, item in enumerate(raw_seats))
        reference_raw = _value(payload, "areaReference", "area_reference")
        reference = _wanda_seat(reference_raw, 0) if reference_raw is not None else None
        facts = PricingFacts(
            provider="WANDA", show_id=_text(_value(payload, "showId", "show_id"), field="show_id"),
            quote_route="WANDA_SELF",
            cinema_id=_value(payload, "cinemaId", "cinema_id"),
            hall_name=str(_value(payload, "hallName", "hall_name") or ""),
            is_vip=_bool(payload, "isVip", "is_vip", "vip"),
            quantity=_value(payload, "quantity", "ticketCount", "ticket_count") or (len(seats) if seats else None),
            seats=seats, quote_scope=str(_value(payload, "quoteScope", "quote_scope") or "exact_seats"),
            area_reference=reference,
        )
        for probe in self._probes(probe_results):
            release_verified = _probe_value(probe, "releaseVerified", "release_verified")
            if release_verified is not True:
                raise PricingError("probe_release_not_verified", "Probe释放未验证，不能进入报价。")
            if str(_probe_value(probe, "status") or "SUCCESS").upper() != "SUCCESS":
                raise PricingError("probe_result_invalid", "ProbeResult未成功读取成本。")
            probe_id = _text(_probe_value(probe, "probeResultId", "probe_result_id", "probe_id", "id"), field="probe_result_id")
            prices = _items(probe, "seatTypePrices", "seat_type_prices")
            if prices:
                for price in prices:
                    targets = self._probe_targets(facts, price)
                    for target in targets:
                        facts = facts.with_probe_cost(
                            probe_result_id=probe_id, seat_id=target.seat_id,
                            original_price_cents=_cents(_value(price, "originalPriceCents", "original_price_cents"), field="original_price_cents", required=True),
                            member_cost_cents=_cents(_value(price, "memberPriceCents", "member_price_cents"), field="member_cost_cents", required=True),
                            release_verified=True,
                        )
            else:
                facts = facts.with_probe_cost(
                    probe_result_id=probe_id,
                    seat_id=_text(_probe_value(probe, "seatId", "seat_id"), field="seat_id"),
                    original_price_cents=_cents(_probe_value(probe, "originalPriceCents", "original_price_cents"), field="original_price_cents", required=True),
                    member_cost_cents=_cents(_probe_value(probe, "memberCostCents", "member_cost_cents"), field="member_cost_cents", required=True),
                    release_verified=True,
                )
        return facts

    from_provider = adapt

    @staticmethod
    def _probe_targets(facts: PricingFacts, price: object) -> list[PricingSeatFact]:
        all_facts = list(facts.seats) + ([facts.area_reference] if facts.area_reference is not None else [])
        representative = _optional_text(_value(price, "representativeSeatId", "representative_seat_id"))
        candidates = [item for item in all_facts if representative and item.seat_id == representative]
        if not candidates:
            area_code = _optional_text(_value(price, "areaCode", "area_code"))
            zone_type = str(_value(price, "zoneType", "zone_type") or "").upper()
            candidates = [item for item in all_facts if area_code and item.area_code == area_code and item.zone_type.upper() == zone_type]
        if not candidates:
            raise PricingError("pricing_seat_not_found", "Probe座位类型无法映射到PricingFacts。")
        return candidates

    @staticmethod
    def _probes(value: Sequence[object] | Mapping[str, object] | object | None) -> list[object]:
        if value is None:
            return []
        if isinstance(value, Mapping):
            return list(value.values()) if "seatId" not in value and "seat_id" not in value else [value]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return list(value)
        return [value]


class LiangpiaoPricingFactsAdapter:
    """Convert one successful Liangpiao order/preflight response to facts."""

    def from_preflight(self, payload: Mapping[str, Any], *, request: Mapping[str, Any] | None = None) -> PricingFacts:
        request = request or {}
        available = _value(payload, "available")
        if available is not True:
            raise PricingError("LIANGPIAO_PREFLIGHT_UNAVAILABLE", "良票预检未确认座位可售。")
        if _value(payload, "ok", "success") is False or str(_value(payload, "status") or "").lower() in {"failed", "error"}:
            raise PricingError("LIANGPIAO_PREFLIGHT_FAILED", "良票预检未通过。")
        show_id = _text(_value(payload, "showId", "show_id") or _value(request, "showId", "show_id"), field="show_id")
        mode = str(_value(payload, "priceMode", "price_mode") or _value(request, "priceMode", "price_mode") or "FIXED").upper()
        if mode not in {"LIMIT", "FIXED"}:
            raise PricingError("pricing_input_invalid", "良票price_mode无效。")
        estimated = _value(payload, "estimated") is True
        if mode == "FIXED" and estimated:
            raise PricingError("LIANGPIAO_PREFLIGHT_INVALID", "良票FIXED预检只返回预估金额。")
        raw_seats = _items(payload, "seats", "seatList", "selectedSeats", "selected_seats") or _items(request, "seats", "seatList")
        if not raw_seats:
            raise PricingError("LIANGPIAO_PREFLIGHT_INVALID", "良票预检缺少座位事实。")
        seats = tuple(self._seat(item, index) for index, item in enumerate(raw_seats))
        estimate = _cents(_value(payload, "estimateAmount", "estimate_amount_fen", "estimateAmountFen"), field="estimateAmount", required=mode == "LIMIT")
        total = _cents(_value(payload, "totalAmount", "total_amount_fen", "totalAmountFen"), field="totalAmount", required=mode == "FIXED")
        market = _cents(_value(payload, "marketAmount", "market_amount_fen", "marketAmountFen"), field="marketAmount")
        base = estimate if mode == "LIMIT" else total
        provider_amount = _cents(_value(payload, "providerAmount", "providerAmountFen", "provider_amount_fen", "supplierAmountFen"), field="providerAmount") or base
        return PricingFacts(
            provider="LIANGPIAO", show_id=show_id,
            quote_route=f"LIANGPIAO_{mode}", quantity=len(seats), seats=seats,
            price_mode=mode, ticket_mode=str(_value(payload, "ticketMode", "ticket_mode") or _value(request, "ticketMode", "ticket_mode") or "STANDARD"),
            area_quote_strategy=_value(payload, "areaQuoteStrategy", "area_quote_strategy") or _value(request, "areaQuoteStrategy", "area_quote_strategy"),
            provider_total_amount_cents=total, provider_estimate_amount_cents=estimate,
            provider_buyer_amount_cents=total if mode == "FIXED" else None,
            provider_amount_cents=provider_amount, provider_market_amount_cents=market,
            provider_max_amount_cents=total if mode == "LIMIT" else total,
            provider_quote_id=_optional_text(_value(payload, "quoteId", "quote_id")),
            provider_quote_hash=_optional_text(_value(payload, "quoteHash", "quote_hash")),
            provider_pricing_rule_version=_optional_text(_value(payload, "pricingRuleVersion", "pricing_rule_version")),
            preflight_verified=True, estimated=estimated,
        )

    adapt = from_preflight

    @staticmethod
    def _seat(raw: object, index: int) -> PricingSeatFact:
        if _value(raw, "available", "isAvailable") is False:
            raise PricingError("LIANGPIAO_SEAT_UNAVAILABLE", "良票预检座位事实不可售。")
        seat_id, label = _seat_identity(raw, index)
        return PricingSeatFact(
            seat_id=seat_id, seat_label=label,
            area_id=str(_value(raw, "areaId", "area_id") or ""),
            area_code=str(_value(raw, "areaCode", "area_code") or ""),
            area_name=str(_value(raw, "areaName", "area_name") or ""),
            zone_type=str(_value(raw, "zoneType", "zone_type") or "REGULAR").upper(),
            cost_source="liangpiao_preflight", availability_verified=True,
        )
