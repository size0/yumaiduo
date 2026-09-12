from __future__ import annotations

from typing import Any
from datetime import datetime, timezone

from .models import GateResult, SafetyClass


def reply_eligibility_gate(result: dict[str, Any]) -> GateResult:
    quote = result.get("quote")
    persist_status = str(result.get("quote_persist_status") or "")
    identity = result.get("identity") if isinstance(result.get("identity"), dict) else {}
    verified_show_id = str(result.get("verified_show_id") or "").strip()
    pipeline_generation = result.get("pipeline_generation")
    authority_ok = isinstance(quote, dict) and bool(str(quote.get("record_id") or "").strip())
    if persist_status and persist_status != "QUOTE_PERSISTED":
        authority_ok = False
    for key in ("tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id"):
        expected = str(identity.get(key) or "").strip()
        if expected and str(quote.get(key) or "").strip() != expected:
            authority_ok = False
    if verified_show_id and str(quote.get("wanda_show_id") or quote.get("show_id") or "").strip() != verified_show_id:
        authority_ok = False
    if pipeline_generation is not None and "generation" in quote:
        authority_ok = authority_ok and quote.get("generation") == pipeline_generation
    expires_at = str(quote.get("expires_at") or quote.get("quote_expires_at") or "").strip() if isinstance(quote, dict) else ""
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                authority_ok = False
        except ValueError:
            authority_ok = False
    if isinstance(quote, dict) and "provider_preflight_verified" in quote and not bool(quote.get("provider_preflight_verified")):
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
