from __future__ import annotations

from typing import Any
from datetime import datetime, timezone
import math

from .models import GateResult, SafetyClass


IDENTITY_FIELDS = ("tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id")


def validate_amount_reply_authority(result: dict[str, Any]) -> tuple[bool, str]:
    """Require positive proof before any persisted quote amount is exposed."""
    if result.get("quote_persist_status") != "QUOTE_PERSISTED":
        return False, "QUOTE_NOT_PERSISTED"
    quote = result.get("quote")
    if not isinstance(quote, dict) or not str(quote.get("record_id") or "").strip():
        return False, "QUOTE_RECORD_MISSING"
    identity = result.get("identity")
    if not isinstance(identity, dict) or any(not str(identity.get(key) or "").strip() for key in IDENTITY_FIELDS):
        return False, "IDENTITY_INCOMPLETE"
    if any(str(quote.get(key) or "").strip() != str(identity.get(key) or "").strip() for key in IDENTITY_FIELDS):
        return False, "IDENTITY_MISMATCH"
    verified_show_id = str(result.get("verified_show_id") or "").strip()
    record_show_id = str(quote.get("wanda_show_id") or quote.get("liangpiao_show_id") or quote.get("show_id") or "").strip()
    if not verified_show_id:
        return False, "VERIFIED_SHOW_MISSING"
    if not record_show_id or record_show_id != verified_show_id:
        return False, "SHOW_MISMATCH"
    pipeline_generation = result.get("pipeline_generation")
    record_generation = quote.get("generation")
    if pipeline_generation is None or record_generation is None:
        return False, "GENERATION_MISSING"
    if record_generation != pipeline_generation:
        return False, "GENERATION_MISMATCH"
    if (str(quote.get("status") or "").lower() in {"expired", "superseded", "stale", "invalidated"}
            or quote.get("invalidated_reason") or quote.get("superseded_by_quote_id") or quote.get("stale") is True):
        return False, "QUOTE_STALE"
    expires_at = str(quote.get("expires_at") or quote.get("quote_expires_at") or "").strip()
    if not expires_at:
        return False, "QUOTE_EXPIRED"
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            return False, "QUOTE_EXPIRED"
    except ValueError:
        return False, "QUOTE_EXPIRED"
    if "provider_preflight_verified" in quote and not bool(quote.get("provider_preflight_verified")):
        return False, "QUOTE_STALE"
    amount = quote.get("total_sell_price_fen", quote.get("total_quote_cents"))
    if amount is None and str(quote.get("request_type") or "") == "WPLUS_AREA":
        unit = quote.get("unit_sell_price_fen", quote.get("unit_quote_cents"))
        count = quote.get("ticket_count")
        if isinstance(unit, (int, float)) and not isinstance(unit, bool) and isinstance(count, int) and not isinstance(count, bool):
            amount = unit * count
    if (amount is None or isinstance(amount, bool) or not isinstance(amount, (int, float))
            or not math.isfinite(amount) or amount <= 0):
        return False, "AMOUNT_MISSING"
    return True, "AUTHORIZED"


def reply_eligibility_gate(result: dict[str, Any]) -> GateResult:
    authority_ok, authority_reason = validate_amount_reply_authority(result)
    if authority_ok and result.get("status") == "QUOTED":
        return GateResult(gate="REPLY", status="AMOUNT_REPLY_ALLOWED", success=True,
                          safety_class=SafetyClass.RECOVERABLE, facts={"quote": result["quote"]},
                          provider_verified=True)
    status = str(result.get("status") or "")
    if authority_reason == "AMOUNT_MISSING" and isinstance(result.get("quote"), dict):
        return GateResult(gate="REPLY", status="NON_AMOUNT_REPLY_ALLOWED", success=True,
                          safety_class=SafetyClass.SOFT_WARNING,
                          facts={"status": status, "reason": authority_reason})
    if status in {"NEED_CLARIFICATION", "ROUTE_UNRESOLVED", "CINEMA_REQUIRED", "CINEMA_TEXT_INSUFFICIENT", "SHOW_UNRESOLVED", "INPUT_INCOMPLETE", "PROVIDER_UNAVAILABLE", "COST_UNAVAILABLE", "PROBE_REQUIRED", "SEAT_NOT_FOUND", "MANUAL_MARK_REQUIRED"}:
        return GateResult(gate="REPLY", status="CLARIFICATION_REQUIRED" if status == "NEED_CLARIFICATION" else "NON_AMOUNT_REPLY_ALLOWED",
                          success=True, safety_class=SafetyClass.SOFT_WARNING,
                          facts={"status": status, "reason": result.get("reason")})
    return GateResult(gate="REPLY", status="NO_SAFE_REPLY", success=False,
                      safety_class=SafetyClass.HARD_SAFETY, reason_code=authority_reason)


class ReplyEligibilityService:
    def evaluate(self, result: dict[str, Any]) -> GateResult:
        return reply_eligibility_gate(result)
