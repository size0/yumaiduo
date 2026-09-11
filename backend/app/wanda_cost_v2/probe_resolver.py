from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from app.probe.coordinator import ProbeRequest
from app.probe.models import ProbeResult
from app.probe.seat_selector import LiveSeat
from app.show_resolve_v2.models import ShowResolutionResult
from app.seat_facts_v2.models import SeatFactsResult

from .models import WandaCostFacts, WandaCostItem


class ProbeCoordinatorLike(Protocol):
    async def run(self, request: ProbeRequest, live_seats: list[LiveSeat]) -> ProbeResult: ...


class ProbeLiveSeatSource(Protocol):
    async def get_probe_live_seats(self, show: ShowResolutionResult) -> list[LiveSeat]: ...


class CanonicalWandaProbeCostResolver:
    """Bridge verified Canonical cost gaps to the durable Active Probe flow.

    The resolver probes one representative seat per missing W+ area/type. It
    never probes ordinary seats, never uses a provider price as a sale price,
    and only returns COST_READY after the coordinator confirms cancellation and
    seat release.
    """

    def __init__(
        self,
        coordinator: ProbeCoordinatorLike,
        live_seat_source: ProbeLiveSeatSource,
        *,
        provider: Any | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._live_seat_source = live_seat_source
        self._provider = provider

    async def resolve(
        self,
        show: ShowResolutionResult,
        seat_facts: SeatFactsResult,
        cost_facts: WandaCostFacts,
        identity: Mapping[str, str],
    ) -> WandaCostFacts:
        if cost_facts.status != "PROBE_REQUIRED" or not cost_facts.probe_targets:
            return cost_facts
        if any(str(target.zone_type).strip().upper() != "W+" for target in cost_facts.probe_targets):
            return cost_facts.model_copy(update={
                "status": "COST_UNAVAILABLE",
                "probe_required": True,
                "reason": "ACTIVE_PROBE_ONLY_SUPPORTS_WPLUS",
            })
        try:
            live_seats = await self._live_seat_source.get_probe_live_seats(show)
        except Exception:
            return cost_facts.model_copy(update={
                "status": "PROBE_REQUIRED",
                "reason": "PROBE_LIVE_SEAT_READ_FAILED",
            })
        by_id = {seat.seat_id: seat for seat in live_seats}
        prices: dict[tuple[str, str], int] = {}
        for target in cost_facts.probe_targets:
            representative = by_id.get(target.seat_id)
            if representative is None or not representative.available or not representative.wplus:
                return cost_facts.model_copy(update={
                    "status": "COST_UNAVAILABLE",
                    "probe_required": True,
                    "reason": "PROBE_REPRESENTATIVE_NOT_AVAILABLE",
                })
            if representative.area_code != target.area_code or representative.zone_type != target.zone_type:
                return cost_facts.model_copy(update={
                    "status": "COST_UNAVAILABLE",
                    "probe_required": True,
                    "reason": "PROBE_REPRESENTATIVE_TYPE_CHANGED",
                })
            if self._provider is not None:
                bind = getattr(self._provider, "bind_live_seats", None)
                if callable(bind):
                    bind(live_seats)
            result = await self._run_probe(
                show=show,
                representative=representative,
                identity=identity,
                live_seats=live_seats,
            )
            if result.status != "SUCCESS" or not result.release_verified:
                return cost_facts.model_copy(update={
                    "status": "PROBE_REQUIRED",
                    "reason": result.error_code or "PROBE_FAILED_OR_RELEASE_UNVERIFIED",
                })
            matches = [
                item for item in result.seat_type_prices
                if item.area_code == target.area_code and item.zone_type == target.zone_type
            ]
            unique_prices = {item.member_price_cents for item in matches}
            if len(unique_prices) != 1:
                return cost_facts.model_copy(update={
                    "status": "PROBE_REQUIRED",
                    "reason": "PROBE_PRICE_TYPE_MISMATCH",
                })
            prices[(target.area_code, target.zone_type)] = next(iter(unique_prices))
        return self._apply_prices(cost_facts, prices)

    async def _run_probe(
        self,
        *,
        show: ShowResolutionResult,
        representative: LiveSeat,
        identity: Mapping[str, str],
        live_seats: list[LiveSeat],
    ) -> ProbeResult:
        request = ProbeRequest(
            tenant_id=str(identity.get("tenant_id") or ""),
            shop_id=str(identity.get("shop_id") or ""),
            show_id=show.wanda_show_id or "",
            requested_seat_labels=[representative.label],
            area_probe=False,
        )
        return await self._coordinator.run(request, live_seats)

    @staticmethod
    def _apply_prices(cost_facts: WandaCostFacts, prices: Mapping[tuple[str, str], int]) -> WandaCostFacts:
        items: list[WandaCostItem] = []
        if cost_facts.request_type == "WPLUS_AREA":
            target = cost_facts.probe_targets[0]
            price = prices.get((target.area_code, target.zone_type))
            if price is None:
                return cost_facts.model_copy(update={"status": "PROBE_REQUIRED", "reason": "PROBE_PRICE_MISSING"})
            items.append(WandaCostItem(
                area_code=target.area_code, zone_type=target.zone_type,
                cost_fen=price, cost_source="LOCKED_ALLOT_SEAT",
            ))
        else:
            for item in cost_facts.cost_items:
                if item.cost_fen is not None:
                    items.append(item)
                    continue
                price = prices.get((item.area_code or "", item.zone_type or ""))
                if price is None:
                    return cost_facts.model_copy(update={
                        "status": "PROBE_REQUIRED", "reason": "PROBE_PRICE_MISSING",
                    })
                items.append(item.model_copy(update={
                    "cost_fen": price, "cost_source": "LOCKED_ALLOT_SEAT",
                }))
        return cost_facts.model_copy(update={
            "status": "COST_READY",
            "cost_items": items,
            "probe_targets": [],
            "probe_required": False,
            "probe_executed": True,
            "reason": None,
        })
