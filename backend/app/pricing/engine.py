from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

from .errors import PricingError
from .models import LiangpiaoPricingBand, PricingFacts, PricingRulesSnapshot, PricingSeatFact, PricedSeat, QuoteResult, WandaPricingBand


class V4PricingEngine:
    """Pure V4 calculations over verified facts and an explicit rules snapshot."""

    def quote(self, facts: PricingFacts, rules: PricingRulesSnapshot) -> QuoteResult:
        if not isinstance(facts, PricingFacts) or not isinstance(rules, PricingRulesSnapshot):
            raise PricingError("pricing_input_invalid", "quote需要PricingFacts和PricingRulesSnapshot。")
        return self._quote_wanda(facts, rules) if facts.provider == "WANDA" else self._quote_liangpiao(facts, rules)

    def _quote_wanda(self, facts: PricingFacts, rules: PricingRulesSnapshot) -> QuoteResult:
        if facts.quote_scope != "exact_seats":
            reference = facts.area_reference or (facts.seats[0] if facts.seats else None)
            if reference is None:
                raise PricingError("pricing_input_invalid", "区域预览缺少代表座位事实。")
            unit = self._price_wanda_seat(reference, rules, facts.is_vip)
            quantity = facts.quantity
            base = reference.member_cost_cents or reference.original_price_cents
            if base is None:
                raise PricingError("authoritative_original_price_required", "区域参考缺少官方原价。")
            return QuoteResult(
                provider="WANDA", quote_scope=facts.quote_scope, quote_route=facts.quote_route,
                seat_zone_type="W+" if reference.physical_wplus else reference.area_name or "普通",
                seat_type="wplus" if reference.physical_wplus else "regular",
                member_unit_price_cents=reference.member_cost_cents,
                original_unit_price_cents=reference.original_price_cents,
                base_unit_cents=base, base_total_cents=base * quantity if quantity else None,
                unit_quote_cents=unit, total_quote_cents=unit * quantity if quantity else None,
                channel_fee_total_cents=reference.channel_fee_cents * quantity if quantity else None,
                ticket_count=quantity, needs_ticket_count=quantity is None, seat_quotes=(),
                pricing_rule_version=rules.rule_version if rules.enabled else None,
                price_source="realtime_vip_area" if facts.is_vip else "realtime_wplus_area" if reference.physical_wplus else "realtime_regular_area",
                pricing_source="万达官方实时座位原价（W+区域优先）+ 后台报价规则（只读）" if rules.enabled else "万达官方实时W+区域原价（只读）",
            )
        if not facts.seats:
            raise PricingError("pricing_input_invalid", "Wanda精确报价至少需要一个座位事实。")
        priced: list[PricedSeat] = []
        for item in facts.seats:
            if not facts.is_vip and item.member_cost_cents is None:
                raise PricingError("wanda_regular_member_price_unavailable", "座位缺少官方实时会员成本。")
            unit = self._price_wanda_seat(item, rules, facts.is_vip)
            if item.original_price_cents is None:
                raise PricingError("authoritative_original_price_required", "座位缺少官方原价。")
            priced.append(PricedSeat(item.seat_id, item.seat_label, "W+" if item.physical_wplus else item.area_name or "普通", item.original_price_cents, None if facts.is_vip else item.member_cost_cents, item.channel_fee_cents, unit))
        members = {item.member_cost_cents for item in priced}
        originals = {item.original_price_cents for item in priced}
        types = {"wplus" if item.physical_wplus else "regular" for item in facts.seats}
        seat_type = next(iter(types)) if len(types) == 1 else "mixed"
        zones = {item.zone_type for item in priced}
        return QuoteResult(
            provider="WANDA", quote_scope="exact_seats", quote_route=facts.quote_route,
            seat_zone_type=next(iter(zones)) if len(zones) == 1 else "混合区域",
            seat_type=next(iter(types)) if len(types) == 1 else "mixed",
            member_unit_price_cents=next(iter(members)) if len(members) == 1 else None,
            original_unit_price_cents=next(iter(originals)) if len(originals) == 1 else None,
            base_unit_cents=next(iter(originals)) if len(originals) == 1 else None,
            base_total_cents=sum(item.original_price_cents for item in priced),
            unit_quote_cents=priced[0].unit_quote_cents if len({item.unit_quote_cents for item in priced}) == 1 else None,
            total_quote_cents=sum(item.unit_quote_cents for item in priced),
            channel_fee_total_cents=sum(item.channel_fee_cents for item in priced),
            ticket_count=len(priced), needs_ticket_count=False, seat_quotes=tuple(priced),
            pricing_rule_version=rules.rule_version if rules.enabled else None,
            price_source=("realtime_vip_area" if facts.is_vip else "realtime_regular_area" if seat_type == "regular" else "realtime_wplus_area" if seat_type == "wplus" else "realtime_mixed_area"),
            pricing_source="万达官方实时座位原价（W+区域优先）+ 后台报价规则（只读）" if rules.enabled else "万达官方实时座位原价（W+区域优先，只读）",
        )

    def _price_wanda_seat(self, seat: PricingSeatFact, rules: PricingRulesSnapshot, is_vip: bool) -> int:
        original = seat.original_price_cents
        if original is None:
            raise PricingError("authoritative_original_price_required", "座位缺少官方原价。")
        if is_vip:
            return self._vip_priced_unit(original, rules)
        member = seat.member_cost_cents
        if not rules.enabled:
            return member if member is not None and member > 0 else original
        if member is None:
            raise PricingError("authoritative_member_price_required", "座位缺少官方会员成本。")
        if rules.wanda_rules:
            raw = member + self._dynamic_wanda_adjustment(original, member, rules.wanda_rules)
        elif seat.physical_wplus:
            raw = max(original + rules.wplus_adjustment_cents, member) if member <= rules.wplus_member_price_threshold_cents else member
        else:
            raw = member + rules.regular_adjustment_cents
        increment = rules.rounding_increment_cents
        lower = ((member + increment - 1) // increment) * increment
        upper = (original // increment) * increment
        if lower > upper:
            raise PricingError("pricing_member_floor_exceeds_original_cap", "会员成本下限高于官方原价上限。")
        rounded = max(increment, ((raw + increment // 2) // increment) * increment)
        return min(max(rounded, lower), upper)

    @staticmethod
    def _vip_priced_unit(original: int, rules: PricingRulesSnapshot) -> int:
        if original <= 0:
            raise PricingError("authoritative_original_price_required", "官方原价必须为正数。")
        raw = original - rules.vip_low_price_discount_cents if original <= rules.vip_discount_threshold_cents else (original * rules.vip_high_price_discount_percent + 50) // 100
        increment = rules.rounding_increment_cents
        rounded = ((raw + increment // 2) // increment) * increment
        return min(max(rounded, increment), original)

    @staticmethod
    def _dynamic_wanda_adjustment(original: int, member: int, bands: Sequence[WandaPricingBand]) -> int:
        ratio = Decimal(member * 100) / Decimal(original)
        for index, band in enumerate(bands):
            maximum = Decimal(str(band.max_discount_percent))
            if Decimal(str(band.min_discount_percent)) <= ratio < maximum or (index == len(bands) - 1 and ratio <= maximum):
                return band.fixed_adjustment_cents
        raise PricingError("pricing_discount_band_not_covered", "会员成本比例未匹配Wanda报价区间。")

    def _quote_liangpiao(self, facts: PricingFacts, rules: PricingRulesSnapshot) -> QuoteResult:
        mode = facts.price_mode or rules.liangpiao_price_mode
        if mode == "FIXED":
            base = facts.provider_total_amount_cents or facts.provider_buyer_amount_cents
            maximum = base
            bands = rules.liangpiao_fixed_rules if rules.liangpiao_fixed_rules_explicit else rules.liangpiao_rules
            if facts.estimated:
                raise PricingError("LIANGPIAO_PREFLIGHT_INVALID", "FIXED预检只返回预估金额。")
        else:
            base = facts.provider_estimate_amount_cents
            maximum = facts.provider_total_amount_cents or facts.provider_estimate_amount_cents
            bands = rules.liangpiao_rules
        if base is None:
            raise PricingError("LIANGPIAO_PREFLIGHT_INVALID", "预检未返回有效基础金额。")
        provider_amount = facts.provider_amount_cents or base
        buyer_amount = base
        markup = None
        applied = False
        if rules.enabled and bands:
            market = facts.provider_market_amount_cents
            if market is None or market <= 0:
                raise PricingError("LIANGPIAO_PRICING_BASE_MISSING", "预检缺少市场原价。")
            selected = self._liangpiao_band(Decimal(base * 100) / Decimal(market), bands)
            raw = Decimal(base) * (Decimal(100) + Decimal(str(selected.markup_percent))) / Decimal(100)
            increment = Decimal(rules.rounding_increment_cents)
            buyer_amount = int((raw / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * increment)
            markup, applied = float(selected.markup_percent), True
        if buyer_amount <= 0:
            raise PricingError("LIANGPIAO_PRICING_RESULT_INVALID", "良票报价结果无效。")
        quantity = facts.quantity
        unit = buyer_amount // quantity if quantity and buyer_amount % quantity == 0 else None
        return QuoteResult(
            provider="LIANGPIAO", quote_scope="exact_seats", quote_route=f"LIANGPIAO_{mode}", seat_zone_type="LIANGPIAO", seat_type="mixed",
            member_unit_price_cents=None, original_unit_price_cents=None,
            base_unit_cents=base // quantity if quantity and base % quantity == 0 else None,
            base_total_cents=base, unit_quote_cents=unit, total_quote_cents=buyer_amount,
            channel_fee_total_cents=0, ticket_count=quantity, needs_ticket_count=False, seat_quotes=(),
            price_mode=mode, max_price_cents=maximum, provider_amount_cents=provider_amount,
            operator_pricing_applied=applied, operator_markup_percent=markup,
            pricing_rule_version=facts.provider_pricing_rule_version or rules.rule_version,
            price_source="liangpiao_realtime_preflight",
            provider_quote_id=facts.provider_quote_id, provider_quote_hash=facts.provider_quote_hash,
            pricing_source="良票实时选座预检 + 后台良票报价规则" if applied else "良票实时选座预检",
            semantic_flags=("PRICING_SEMANTIC_REVIEW_REQUIRED",) if maximum != buyer_amount else (),
        )

    @staticmethod
    def _liangpiao_band(ratio: Decimal, bands: Sequence[LiangpiaoPricingBand]) -> LiangpiaoPricingBand:
        for index, band in enumerate(bands):
            maximum = Decimal(str(band.max_discount_percent))
            if Decimal(str(band.min_discount_percent)) <= ratio < maximum or (index == len(bands) - 1 and ratio <= maximum):
                return band
        raise PricingError("LIANGPIAO_PRICING_BAND_MISSING", "良票金额比例未匹配报价区间。")
