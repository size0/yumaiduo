from __future__ import annotations

import inspect
from typing import Any

from .models import GateResult, SafetyClass


def _facts(result: object) -> dict[str, Any]:
    """Expose service facts without changing the service's result object."""
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        return dict(result)
    return {"value": result}


_SUCCESS_STATUSES = {
    "RECOGNIZED", "PARTIAL", "RESOLVED", "WANDA_SELF", "LIANGPIAO",
    "EXACT_SEATS_RESOLVED", "WPLUS_AREA_RESOLVED", "SEATS_NOT_SELECTED",
    "SEAT_AREA_ONLY", "COST_READY", "PRICED", "QUOTED", "SENT",
}


class RecognitionGateMixin:
    async def recognize_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.recognize(*args, **kwargs)
        status = "RECOGNIZED" if getattr(result, "movie", None) or getattr(result, "cinema_text", None) else "PARTIAL"
        facts = _facts(result)
        return GateResult(gate="RECOGNITION", status=status,
                          success=status in _SUCCESS_STATUSES,
                          safety_class=SafetyClass.RECOVERABLE, facts=facts,
                          reason_code=facts.get("reason") or facts.get("resolution_reason"),
                          metadata={"source": "recognition"})


class CinemaRouteGateMixin:
    async def resolve_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.resolve(*args, **kwargs)
        route = getattr(result, "route", "UNRESOLVED")
        status = route if route != "UNRESOLVED" else str(getattr(result, "resolution_reason", None) or route)
        facts = _facts(result)
        gate = GateResult(gate="CINEMA_ROUTE", status=status,
                          success=status in _SUCCESS_STATUSES,
                          safety_class=SafetyClass.RECOVERABLE, facts=facts,
                          reason_code=facts.get("reason") or facts.get("resolution_reason"),
                          metadata={"source": "cinema_route"})
        missing = {"CITY_REQUIRED": ["city"], "CINEMA_REQUIRED": ["cinema"],
                    "CINEMA_TEXT_INSUFFICIENT": ["cinema"]}.get(status, [])
        candidates = list(getattr(result, "candidates", []) or [])
        return gate.model_copy(update={"missing_fields": missing, "candidates": candidates})


class ShowResolveGateMixin:
    async def resolve_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.resolve(*args, **kwargs)
        status = str(getattr(result, "status", "INVALID"))
        facts = _facts(result)
        gate = GateResult(gate="SHOW", status=status,
                          success=status in _SUCCESS_STATUSES,
                          safety_class=SafetyClass.RECOVERABLE, facts=facts,
                          reason_code=facts.get("reason") or facts.get("resolution_reason"),
                          metadata={"source": "show_resolve"})
        candidates = list(getattr(result, "candidates", []) or [])
        missing = ["showtime"] if status == "INPUT_INCOMPLETE" else []
        return gate.model_copy(update={"missing_fields": missing, "candidates": candidates})


class SeatFactsGateMixin:
    async def resolve_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.resolve(*args, **kwargs)
        status = str(getattr(result, "status", "UNAVAILABLE"))
        if status == "WPLUS_AREA_RESOLVED":
            status = "WPLUS_AREA_RESOLVED"
        facts = _facts(result)
        gate = GateResult(gate="SEAT", status=status,
                          success=status in _SUCCESS_STATUSES,
                          safety_class=SafetyClass.RECOVERABLE, facts=facts,
                          reason_code=facts.get("reason") or facts.get("resolution_reason"),
                          metadata={"source": "seat_facts"})
        missing = ["selected_seats"] if status in {"MANUAL_MARK_REQUIRED", "INPUT_INCOMPLETE"} else []
        return gate.model_copy(update={"missing_fields": missing})


class CostResolutionGateMixin:
    def resolve_cost_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = self.resolve(*args, **kwargs)
        if inspect.isawaitable(result):
            raise TypeError("CostResolutionGateMixin expects a synchronous resolver")
        status = str(getattr(result, "status", "INVALID"))
        facts = _facts(result)
        return GateResult(gate="COST", status=status,
                          success=status in _SUCCESS_STATUSES,
                          safety_class=SafetyClass.RECOVERABLE, facts=facts,
                          reason_code=facts.get("reason") or facts.get("resolution_reason"),
                          provider_verified=status == "COST_READY",
                          metadata={"source": "cost_resolution"})


class PricingGateMixin:
    def price_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = self.price(*args, **kwargs)
        if inspect.isawaitable(result):
            raise TypeError("PricingGateMixin expects a synchronous pricing service")
        status = "PRICED" if getattr(result, "status", "") == "PRICED" else "INVALID"
        facts = _facts(result)
        return GateResult(gate="PRICING", status=status,
                          success=status == "PRICED",
                          safety_class=SafetyClass.RECOVERABLE, facts=facts,
                          reason_code=facts.get("reason") or facts.get("resolution_reason"),
                          provider_verified=status == "PRICED",
                          metadata={"source": "pricing"})
