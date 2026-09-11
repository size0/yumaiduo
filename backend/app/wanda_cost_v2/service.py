from __future__ import annotations

from collections import defaultdict

from app.seat_facts_v2.models import SeatFactsResult
from app.show_resolve_v2.models import ShowResolutionResult

from .models import (
    WandaCostFacts,
    WandaCostItem,
    WandaProbeTarget,
)


class WandaCostResolutionService:
    """Resolve Wanda cost facts without Probe, Pricing, or side effects."""

    def resolve(
        self,
        show_facts: ShowResolutionResult,
        seat_facts: SeatFactsResult,
    ) -> WandaCostFacts:
        if not isinstance(show_facts, ShowResolutionResult) or not isinstance(seat_facts, SeatFactsResult):
            return _incomplete("V4_SHOW_AND_SEAT_FACTS_REQUIRED")
        if show_facts.status != "RESOLVED" or not show_facts.wanda_show_id:
            return _incomplete("SHOW_FACTS_NOT_RESOLVED")
        if seat_facts.status == "MANUAL_MARK_REQUIRED" or seat_facts.has_manual_mark is None:
            return _incomplete("MANUAL_MARK_FACT_REQUIRED")

        exact_seats = list(seat_facts.exact_seats)
        if seat_facts.has_manual_mark or not exact_seats:
            return self._resolve_wplus_area(show_facts, seat_facts)
        return self._resolve_exact(exact_seats)

    def _resolve_wplus_area(
        self,
        show_facts: ShowResolutionResult,
        seat_facts: SeatFactsResult,
    ) -> WandaCostFacts:
        # A show-level activity price can be the ordinary member price on
        # providers that flatten the W+ payload.  Prefer the authoritative
        # W+ area member price when seat facts contain one; never let a lower
        # ordinary price masquerade as SHOWTIME_WPLUS cost.
        exclusive = [area for area in seat_facts.wplus_areas if area.has_wplus_exclusive_seats]
        areas = exclusive or seat_facts.wplus_areas
        priced_areas = [area for area in areas if area.wplus_available
                        and area.has_valid_area_member_price and _positive(area.area_member_price_fen)]
        if priced_areas:
            selected = min(priced_areas, key=lambda area: area.area_member_price_fen)
            return _facts(
                status="COST_READY", request_type="WPLUS_AREA",
                cost_items=[WandaCostItem(
                    seat_label=None, area_code=selected.area_code, zone_type="W+",
                    cost_fen=selected.area_member_price_fen, cost_source="REALTIME_AREA_WPLUS",
                )],
            )
        showtime_price = _positive(show_facts.wplus_activity_price_fen)
        if showtime_price is not None and not exclusive:
            return _facts(
                status="COST_READY",
                request_type="WPLUS_AREA",
                cost_items=[WandaCostItem(
                    seat_label=None,
                    area_code=None,
                    zone_type="W+",
                    cost_fen=showtime_price,
                    cost_source="SHOWTIME_WPLUS",
                )],
            )

        for area in areas:
            if not area.wplus_available or not area.available_seat_ids:
                continue
            if not area.area_code or not area.zone_type:
                continue
            return _facts(
                status="PROBE_REQUIRED",
                request_type="WPLUS_AREA",
                probe_required=True,
                probe_targets=[WandaProbeTarget(
                    area_code=area.area_code,
                    zone_type=area.zone_type,
                    seat_id=area.available_seat_ids[0],
                )],
            )
        return _facts(
            status="COST_UNAVAILABLE",
            request_type="WPLUS_AREA",
            probe_required=True,
            reason="AVAILABLE_WPLUS_REPRESENTATIVE_MISSING",
        )

    def _resolve_exact(self, exact_seats: list[object]) -> WandaCostFacts:
        cost_items: list[WandaCostItem] = []
        missing_groups: dict[tuple[str, str], list[object]] = defaultdict(list)
        for seat in exact_seats:
            if seat.status != "AVAILABLE":
                return _facts(
                    status="COST_UNAVAILABLE",
                    request_type="EXACT_SEATS",
                    reason="EXACT_SEAT_NOT_AVAILABLE",
                )
            area_code = _text(seat.area_code)
            zone_type = _text(seat.zone_type)
            label = _text(seat.seat_label or seat.label)
            member_price = _positive(seat.area_member_price_fen)
            if seat.has_valid_area_member_price and member_price is not None and area_code and zone_type:
                cost_items.append(WandaCostItem(
                    seat_label=label,
                    area_code=area_code,
                    zone_type=zone_type,
                    cost_fen=member_price,
                    cost_source="REALTIME_AREA_WPLUS",
                ))
                continue

            cost_items.append(WandaCostItem(
                seat_label=label,
                area_code=area_code,
                zone_type=zone_type,
                cost_fen=None,
                cost_source=None,
            ))
            if area_code and zone_type:
                missing_groups[(area_code, zone_type)].append(seat)

        probe_targets: list[WandaProbeTarget] = []
        missing_target = False
        for (area_code, zone_type), group in missing_groups.items():
            representative = next(
                (
                    seat for seat in group
                    if _text(seat.seat_id or seat.wanda_seat_id)
                    and seat.status == "AVAILABLE"
                ),
                None,
            )
            if representative is None:
                missing_target = True
                continue
            probe_targets.append(WandaProbeTarget(
                area_code=area_code,
                zone_type=zone_type,
                seat_id=_text(representative.seat_id or representative.wanda_seat_id),
            ))

        if missing_groups:
            return _facts(
                status="COST_UNAVAILABLE" if missing_target else "PROBE_REQUIRED",
                request_type="EXACT_SEATS",
                cost_items=cost_items,
                probe_required=True,
                probe_targets=probe_targets,
                reason="AVAILABLE_EXACT_PROBE_REPRESENTATIVE_MISSING" if missing_target else None,
            )
        return _facts(
            status="COST_READY",
            request_type="EXACT_SEATS",
            cost_items=cost_items,
        )


def resolve_wanda_cost(
    show_facts: ShowResolutionResult,
    seat_facts: SeatFactsResult,
) -> WandaCostFacts:
    """Small V4 Phase 5 entry point; does not perform Probe or Pricing."""
    return WandaCostResolutionService().resolve(show_facts, seat_facts)


def _facts(
    *,
    status: str,
    request_type: str,
    cost_items: list[WandaCostItem] | None = None,
    probe_required: bool = False,
    probe_targets: list[WandaProbeTarget] | None = None,
    reason: str | None = None,
) -> WandaCostFacts:
    return WandaCostFacts(
        status=status,
        request_type=request_type,
        cost_items=cost_items or [],
        probe_required=probe_required,
        probe_targets=probe_targets or [],
        reason=reason,
    )


def _incomplete(reason: str) -> WandaCostFacts:
    return WandaCostFacts(status="INPUT_INCOMPLETE", reason=reason)


def _positive(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip()
    return normalized or None
