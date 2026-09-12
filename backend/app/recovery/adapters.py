from __future__ import annotations

from .models import GateResult, SafetyClass


LEGACY_GATE_MAP = {
    "ROUTE_UNRESOLVED": "CINEMA_ROUTE",
    "SHOW_UNRESOLVED": "SHOW",
    "SELECTED_SEATS_REQUIRED": "SEAT",
    "LIANGPIAO_FACTS_INCOMPLETE": "SEAT",
    "PROBE_REQUIRED": "COST",
    "PRICING_UNAVAILABLE": "PRICING",
}


def from_legacy(status: str, *, reason: str | None = None, **values: object) -> GateResult:
    gate = LEGACY_GATE_MAP.get(status, "QUOTE")
    hard = status in {"IDENTITY_INCOMPLETE", "QUOTE_INPUT_INVALID"}
    return GateResult(
        gate=gate, status=status, success=status in {"QUOTED", "PRICED", "RESOLVED"},
        safety_class=SafetyClass.HARD_SAFETY if hard else SafetyClass.RECOVERABLE,
        reason_code=reason or status, metadata=dict(values),
    )


def from_service_result(gate: str, result: object, *, status: str | None = None) -> GateResult:
    """Convert an existing V2 fact model without losing its business payload.

    This is the migration seam used while individual services are moved. It
    deliberately copies only public model fields into ``facts``/``metadata``;
    it never invents provider facts or changes the source result.
    """
    if hasattr(result, "model_dump"):
        values = result.model_dump(mode="json")
    elif isinstance(result, dict):
        values = dict(result)
    else:
        values = {"value": result}
    resolved_status = status or str(values.get("status") or "UNRESOLVED")
    success = resolved_status in {
        "RECOGNIZED", "PARTIAL", "RESOLVED", "WANDA_SELF", "LIANGPIAO",
        "EXACT_SEATS_RESOLVED", "WPLUS_AREA_RESOLVED", "SEATS_NOT_SELECTED",
        "SEAT_AREA_ONLY", "COST_READY", "PRICED", "QUOTED", "SENT",
    }
    missing = []
    reason = values.get("reason") or values.get("resolution_reason")
    if resolved_status in {"INPUT_INCOMPLETE", "MISSING_COST", "SELECTED_SEATS_REQUIRED"}:
        reason = reason or resolved_status
    return GateResult(
        gate=gate,
        status=resolved_status,
        success=success,
        safety_class=SafetyClass.RECOVERABLE,
        facts=values,
        missing_fields=missing,
        reason_code=str(reason) if reason else None,
        provider_verified=resolved_status in {"COST_READY", "PRICED", "QUOTED"},
        metadata={"legacy_model": type(result).__name__},
    )
