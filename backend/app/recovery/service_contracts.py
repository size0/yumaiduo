from __future__ import annotations

import inspect
from typing import Any

from .models import GateResult, SafetyClass


def _native_gate(gate: str, result: object, status: str) -> GateResult:
    values = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result) if isinstance(result, dict) else {"value": result}
    success = status in {"RECOGNIZED", "PARTIAL", "RESOLVED", "WANDA_SELF", "LIANGPIAO", "EXACT_SEATS_RESOLVED", "WPLUS_AREA_RESOLVED", "SEATS_NOT_SELECTED", "SEAT_AREA_ONLY", "COST_READY", "PRICED", "QUOTED", "SENT"}
    return GateResult(gate=gate, status=status, success=success, safety_class=SafetyClass.RECOVERABLE,
                      facts=values, reason_code=values.get("reason") or values.get("resolution_reason"),
                      provider_verified=status in {"COST_READY", "PRICED", "QUOTED"}, metadata={"native": True})


class RecognitionGateMixin:
    async def recognize_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.recognize(*args, **kwargs)
        status = "RECOGNIZED" if getattr(result, "movie", None) or getattr(result, "cinema_text", None) else "PARTIAL"
        return _native_gate("RECOGNITION", result, status)


class CinemaRouteGateMixin:
    async def resolve_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.resolve(*args, **kwargs)
        route = getattr(result, "route", "UNRESOLVED")
        status = route if route != "UNRESOLVED" else str(getattr(result, "resolution_reason", None) or route)
        gate = _native_gate("CINEMA_ROUTE", result, status)
        missing = {"CITY_REQUIRED": ["city"], "CINEMA_REQUIRED": ["cinema"],
                    "CINEMA_TEXT_INSUFFICIENT": ["cinema"]}.get(status, [])
        candidates = list(getattr(result, "candidates", []) or [])
        return gate.model_copy(update={"missing_fields": missing, "candidates": candidates})


class ShowResolveGateMixin:
    async def resolve_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.resolve(*args, **kwargs)
        status = str(getattr(result, "status", "INVALID"))
        gate = _native_gate("SHOW", result, status)
        candidates = list(getattr(result, "candidates", []) or [])
        missing = ["showtime"] if status == "INPUT_INCOMPLETE" else []
        return gate.model_copy(update={"missing_fields": missing, "candidates": candidates})


class SeatFactsGateMixin:
    async def resolve_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.resolve(*args, **kwargs)
        status = str(getattr(result, "status", "UNAVAILABLE"))
        if status == "WPLUS_AREA_RESOLVED":
            status = "WPLUS_AREA_RESOLVED"
        gate = _native_gate("SEAT", result, status)
        missing = ["selected_seats"] if status in {"MANUAL_MARK_REQUIRED", "INPUT_INCOMPLETE"} else []
        return gate.model_copy(update={"missing_fields": missing})


class CostResolutionGateMixin:
    def resolve_cost_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = self.resolve(*args, **kwargs)
        if inspect.isawaitable(result):
            raise TypeError("CostResolutionGateMixin expects a synchronous resolver")
        return _native_gate("COST", result, str(getattr(result, "status", "INVALID")))


class PricingGateMixin:
    def price_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = self.price(*args, **kwargs)
        if inspect.isawaitable(result):
            raise TypeError("PricingGateMixin expects a synchronous pricing service")
        return _native_gate("PRICING", result, status="PRICED" if getattr(result, "status", "") == "PRICED" else "INVALID")
