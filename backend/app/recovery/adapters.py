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
