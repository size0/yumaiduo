from __future__ import annotations

from typing import Any

from .models import GateResult, SafetyClass


def reply_eligibility_gate(result: dict[str, Any]) -> GateResult:
    quote = result.get("quote")
    persist_status = str(result.get("quote_persist_status") or "")
    identity = result.get("identity") if isinstance(result.get("identity"), dict) else {}
    verified_show_id = str(result.get("verified_show_id") or "").strip()
    authority_ok = isinstance(quote, dict) and bool(str(quote.get("record_id") or "").strip())
    if persist_status and persist_status != "QUOTE_PERSISTED":
        authority_ok = False
    for key in ("tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id"):
        expected = str(identity.get(key) or "").strip()
        if expected and str(quote.get(key) or "").strip() != expected:
            authority_ok = False
    if verified_show_id and str(quote.get("wanda_show_id") or quote.get("show_id") or "").strip() != verified_show_id:
        authority_ok = False
    if authority_ok and result.get("status") == "QUOTED":
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
