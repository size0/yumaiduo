from __future__ import annotations
from dataclasses import asdict

from app.pricing.engine import V4PricingEngine
from app.pricing.errors import PricingError
from app.pricing.models import PricingFacts, PricingRulesSnapshot, PricingSeatFact
from app.seat_facts_v2.models import SeatFactsResult
from app.show_resolve_v2.models import ShowResolutionResult
from app.wanda_cost_v2.models import WandaCostFacts

from .models import WandaPricingCostItem, WandaPricingResult, WandaSeatQuote
from ..recovery.models import GateResult, SafetyClass

_ALLOWED_COST_SOURCES = frozenset({
    "SHOWTIME_WPLUS",
    "REALTIME_AREA_WPLUS",
    "LOCKED_ALLOT_SEAT",
})


class WandaPricingV2Service:
    """Adapt Wanda Cost V2 facts into the existing V4 pricing engine."""

    def __init__(self, engine: V4PricingEngine | object | None = None) -> None:
        self._engine = engine or V4PricingEngine()

    def price(
        self,
        cost_facts: WandaCostFacts,
        show_facts: ShowResolutionResult,
        seat_facts: SeatFactsResult,
        rules: PricingRulesSnapshot,
        *,
        ticket_count: int | None = None,
    ) -> WandaPricingResult:
        if not all(isinstance(value, (WandaCostFacts, ShowResolutionResult, SeatFactsResult, PricingRulesSnapshot))
                   for value in (cost_facts, show_facts, seat_facts, rules)):
            return _incomplete("V4_COST_SHOW_SEAT_RULE_FACTS_REQUIRED")
        if cost_facts.status != "COST_READY":
            return _requires_cost(cost_facts)
        if show_facts.status != "RESOLVED" or not show_facts.wanda_show_id:
            return _incomplete("SHOW_FACTS_NOT_RESOLVED", cost_facts)
        if cost_facts.request_type == "WPLUS_AREA":
            return self._price_area(cost_facts, show_facts, seat_facts, rules, ticket_count)
        if cost_facts.request_type == "EXACT_SEATS":
            return self._price_exact(cost_facts, show_facts, seat_facts, rules)
        return _incomplete("COST_REQUEST_TYPE_REQUIRED", cost_facts)

    def price_gate(self, cost: WandaCostFacts, show: ShowResolutionResult, seat: SeatFactsResult,
                   rules: PricingRulesSnapshot, **kwargs: object) -> GateResult:
        result = self.price(cost, show, seat, rules, ticket_count=kwargs.get("ticket_count"))
        facts = result.model_dump(mode="json")
        return GateResult(gate="PRICING", status=result.status, success=result.status == "PRICED",
                          safety_class=SafetyClass.RECOVERABLE, facts=facts,
                          provider_verified=result.status == "PRICED",
                          reason_code=facts.get("reason"), metadata={"source": "pricing"})

    def _price_area(
        self,
        cost_facts: WandaCostFacts,
        show_facts: ShowResolutionResult,
        seat_facts: SeatFactsResult,
        rules: PricingRulesSnapshot,
        ticket_count: int | None,
    ) -> WandaPricingResult:
        item = cost_facts.cost_items[0] if len(cost_facts.cost_items) == 1 else None
        cost = item.cost_fen if item is not None else None
        source = item.cost_source if item is not None else None
        original = show_facts.sales_price_fen
        if source == "REALTIME_AREA_WPLUS":
            area = next((area for area in seat_facts.wplus_areas
                         if item.area_code and area.area_code == item.area_code
                         and area.area_member_price_fen == cost and area.wplus_available), None)
            if area is None or not _positive(area.area_original_price_fen):
                return _incomplete("MATCHED_AREA_ORIGINAL_PRICE_REQUIRED", cost_facts)
            original = area.area_original_price_fen
        if cost is None or source not in _ALLOWED_COST_SOURCES:
            return _requires_cost(cost_facts, "AREA_COST_REQUIRED")
        if not _positive(original):
            return _incomplete("SHOWTIME_ORIGINAL_PRICE_REQUIRED", cost_facts)
        if ticket_count is not None and (isinstance(ticket_count, bool) or not 1 <= ticket_count <= 20):
            return _incomplete("TICKET_COUNT_INVALID", cost_facts)
        reference = PricingSeatFact(
            seat_id=f"area:{show_facts.wanda_show_id}:wplus",
            seat_label="W+区域",
            area_code="",
            area_name="W+",
            zone_type="W+",
            physical_wplus=True,
            original_price_cents=original,
            member_cost_cents=cost,
            cost_source=source,
        )
        facts = PricingFacts(
            provider="WANDA",
            show_id=show_facts.wanda_show_id,
            quote_route="WANDA_SELF",
            quantity=ticket_count,
            quote_scope="area_preview",
            area_reference=reference,
            seats=(),
        )
        return self._run_engine(facts, rules, cost_facts, request_type="WPLUS_AREA")

    def _price_exact(
        self,
        cost_facts: WandaCostFacts,
        show_facts: ShowResolutionResult,
        seat_facts: SeatFactsResult,
        rules: PricingRulesSnapshot,
    ) -> WandaPricingResult:
        if len(cost_facts.cost_items) != len(seat_facts.exact_seats) or not cost_facts.cost_items:
            return _incomplete("EXACT_COST_SEAT_COUNT_MISMATCH", cost_facts)
        seat_by_label = {seat.seat_label: seat for seat in seat_facts.exact_seats}
        pricing_seats: list[PricingSeatFact] = []
        for item in cost_facts.cost_items:
            if item.cost_fen is None or item.cost_source not in _ALLOWED_COST_SOURCES:
                return _requires_cost(cost_facts, "EXACT_COST_REQUIRED")
            seat = seat_by_label.get(item.seat_label)
            if seat is None or not _positive(seat.area_original_price_fen):
                return _incomplete("EXACT_SEAT_ORIGINAL_PRICE_REQUIRED", cost_facts)
            seat_id = seat.seat_id or seat.wanda_seat_id
            if not seat_id:
                return _incomplete("EXACT_SEAT_ID_REQUIRED", cost_facts)
            pricing_seats.append(PricingSeatFact(
                seat_id=seat_id,
                seat_label=seat.seat_label,
                area_code=seat.area_code or "",
                area_name=seat.zone_type or "",
                zone_type=seat.zone_type or "未知",
                physical_wplus=seat.is_wplus_exclusive,
                original_price_cents=seat.area_original_price_fen,
                member_cost_cents=item.cost_fen,
                cost_source=item.cost_source,
            ))
        facts = PricingFacts(
            provider="WANDA",
            show_id=show_facts.wanda_show_id,
            quote_route="WANDA_SELF",
            quantity=len(pricing_seats),
            quote_scope="exact_seats",
            seats=tuple(pricing_seats),
        )
        return self._run_engine(facts, rules, cost_facts, request_type="EXACT_SEATS")

    def _run_engine(
        self,
        facts: PricingFacts,
        rules: PricingRulesSnapshot,
        cost_facts: WandaCostFacts,
        *,
        request_type: str,
    ) -> WandaPricingResult:
        try:
            priced = self._engine.quote(facts, rules)
        except PricingError as error:
            return _incomplete(error.code, cost_facts)
        seat_quotes: list[WandaSeatQuote] = []
        if request_type == "EXACT_SEATS":
            seat_quotes = [
                WandaSeatQuote(
                    seat_id=priced_item.seat_id,
                    seat_label=cost_item.seat_label,
                    cost_fen=cost_item.cost_fen,
                    sell_price_fen=priced_item.unit_quote_cents,
                    cost_source=cost_item.cost_source,
                )
                for cost_item, priced_item in zip(cost_facts.cost_items, priced.seat_quotes, strict=True)
            ]
        return WandaPricingResult(
            status="PRICED",
            request_type=request_type,
            unit_sell_price_fen=priced.unit_quote_cents,
            total_sell_price_fen=priced.total_quote_cents,
            ticket_count=priced.ticket_count,
            needs_ticket_count=priced.needs_ticket_count,
            seat_quotes=seat_quotes,
            cost_items=[WandaPricingCostItem.model_validate(item.model_dump()) for item in cost_facts.cost_items],
            cost_sources=[item.cost_source for item in cost_facts.cost_items],
            pricing_rule_revision=rules.revision,
            pricing_rule_version=rules.rule_version,
            pricing_engine_applied=True,
            calculation_evidence={
                "facts": asdict(facts), "rules": asdict(rules),
                "result": asdict(priced),
            },
        )


def price_wanda_cost(
    cost_facts: WandaCostFacts,
    show_facts: ShowResolutionResult,
    seat_facts: SeatFactsResult,
    rules: PricingRulesSnapshot,
    *,
    ticket_count: int | None = None,
) -> WandaPricingResult:
    return WandaPricingV2Service().price(
        cost_facts, show_facts, seat_facts, rules, ticket_count=ticket_count,
    )


def _requires_cost(cost_facts: WandaCostFacts, reason: str = "COST_FACTS_NOT_READY") -> WandaPricingResult:
    return WandaPricingResult(
        status="PRICING_REQUIRES_COST",
        request_type=cost_facts.request_type,
        probe_targets=[target.model_dump() for target in cost_facts.probe_targets],
        pricing_rule_revision=None,
        pricing_rule_version=None,
        reason=reason,
    )


def _incomplete(reason: str, cost_facts: WandaCostFacts | None = None) -> WandaPricingResult:
    return WandaPricingResult(
        status="INPUT_INCOMPLETE",
        request_type=cost_facts.request_type if cost_facts is not None else None,
        reason=reason,
    )


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
