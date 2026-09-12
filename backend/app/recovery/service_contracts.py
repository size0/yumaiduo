from __future__ import annotations

import inspect
from typing import Any

from .adapters import from_service_result
from .models import GateResult


class RecognitionGateMixin:
    async def recognize_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.recognize(*args, **kwargs)
        status = "RECOGNIZED" if getattr(result, "movie", None) or getattr(result, "cinema_text", None) else "PARTIAL"
        return from_service_result("RECOGNITION", result, status=status)


class CinemaRouteGateMixin:
    async def resolve_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.resolve(*args, **kwargs)
        route = getattr(result, "route", "UNRESOLVED")
        status = route if route != "UNRESOLVED" else str(getattr(result, "resolution_reason", None) or route)
        gate = from_service_result("CINEMA_ROUTE", result, status=status)
        missing = {"CITY_REQUIRED": ["city"], "CINEMA_REQUIRED": ["cinema"],
                    "CINEMA_TEXT_INSUFFICIENT": ["cinema"]}.get(status, [])
        candidates = list(getattr(result, "candidates", []) or [])
        return gate.model_copy(update={"missing_fields": missing, "candidates": candidates})


class ShowResolveGateMixin:
    async def resolve_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.resolve(*args, **kwargs)
        status = str(getattr(result, "status", "INVALID"))
        gate = from_service_result("SHOW", result, status=status)
        candidates = list(getattr(result, "candidates", []) or [])
        missing = ["showtime"] if status == "INPUT_INCOMPLETE" else []
        return gate.model_copy(update={"missing_fields": missing, "candidates": candidates})


class SeatFactsGateMixin:
    async def resolve_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = await self.resolve(*args, **kwargs)
        status = str(getattr(result, "status", "UNAVAILABLE"))
        if status == "WPLUS_AREA_RESOLVED":
            status = "WPLUS_AREA_RESOLVED"
        gate = from_service_result("SEAT", result, status=status)
        missing = ["selected_seats"] if status in {"MANUAL_MARK_REQUIRED", "INPUT_INCOMPLETE"} else []
        return gate.model_copy(update={"missing_fields": missing})


class CostResolutionGateMixin:
    def resolve_cost_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = self.resolve(*args, **kwargs)
        if inspect.isawaitable(result):
            raise TypeError("CostResolutionGateMixin expects a synchronous resolver")
        return from_service_result("COST", result, status=str(getattr(result, "status", "INVALID")))


class PricingGateMixin:
    def price_gate(self, *args: Any, **kwargs: Any) -> GateResult:
        result = self.price(*args, **kwargs)
        if inspect.isawaitable(result):
            raise TypeError("PricingGateMixin expects a synchronous pricing service")
        return from_service_result("PRICING", result, status="PRICED" if getattr(result, "status", "") == "PRICED" else "INVALID")
