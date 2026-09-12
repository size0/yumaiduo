from __future__ import annotations

from typing import Any

from .models import GateResult, SafetyClass


def reply_eligibility_gate(result: dict[str, Any]) -> GateResult:
    quote = result.get("quote")
    if isinstance(quote, dict) and result.get("status") == "QUOTED":
        return GateResult(gate="REPLY", status="AMOUNT_REPLY_ALLOWED", success=True,
                          safety_class=SafetyClass.RECOVERABLE, facts={"quote": quote},
                          provider_verified=True)
    status = str(result.get("status") or "")
    if status in {"NEED_CLARIFICATION", "ROUTE_UNRESOLVED", "CINEMA_REQUIRED", "CINEMA_TEXT_INSUFFICIENT", "SHOW_UNRESOLVED", "INPUT_INCOMPLETE", "PROVIDER_UNAVAILABLE", "COST_UNAVAILABLE", "PROBE_REQUIRED", "SEAT_NOT_FOUND", "MANUAL_MARK_REQUIRED"}:
        return GateResult(gate="REPLY", status="CLARIFICATION_REQUIRED" if status == "NEED_CLARIFICATION" else "NON_AMOUNT_REPLY_ALLOWED",
                          success=True, safety_class=SafetyClass.SOFT_WARNING,
                          facts={"status": status, "reason": result.get("reason")})
    return GateResult(gate="REPLY", status="NO_SAFE_REPLY", success=False,
                      safety_class=SafetyClass.HARD_SAFETY, reason_code="QUOTE_RECORD_REQUIRED")


class ReplyEligibilityService:
    def evaluate(self, result: dict[str, Any]) -> GateResult:
        return reply_eligibility_gate(result)
