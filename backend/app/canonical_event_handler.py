from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .canonical_buyer_reply import CanonicalBuyerReplyRenderer


class CanonicalEventHandler:
    """Handle buyer text after the existing durable inbox lease is acquired."""

    def __init__(self, *, agent: Any, shop_store: Any, inbox: Any, quote_context_writer: Any = None,
                 reply_renderer: Any = None, quote_continuation: Any = None) -> None:
        self._agent = agent
        self._shops = shop_store
        self._inbox = inbox
        self._quote_context_writer = quote_context_writer
        self._reply_renderer = reply_renderer or CanonicalBuyerReplyRenderer()
        self._quote_continuation = quote_continuation

    async def process_event(self, body: Mapping[str, Any]) -> dict[str, Any] | None:
        envelope = _mapping(body.get("envelope"))
        payload = _mapping(envelope.get("payload"))
        session = _mapping(body.get("session"))
        if envelope.get("event") != "im.message.received":
            return None
        images = payload.get("imageUrls", payload.get("image_urls"))
        if isinstance(images, list) and images:
            return None
        # Transaction/review cards remain owned by their existing reducers.
        if str(payload.get("messageType", payload.get("message_type", "1"))) not in {"", "1"}:
            return None
        tenant = str(envelope.get("tenantId") or envelope.get("tenant_id") or "")
        shop = str(payload.get("accountUnb") or payload.get("account_unb") or session.get("accountUnb") or session.get("account_unb") or "")
        if not self._shops.is_canonical_quote_enabled(tenant, shop):
            return None
        if not self._shops.is_enabled(tenant, shop):
            return self._outcome("CANONICAL_TEXT_DISABLED")
        # Canonical quote rollout and Canonical conversation rollout are
        # independent. With conversation disabled, return None so the existing
        # Legacy text reducer can consume persisted Conversation Facts.
        if not self._shops.is_canonical_conversation_enabled(tenant, shop):
            return None
        roles = {str(payload.get(key) or "").lower() for key in ("direction", "sender", "senderType", "fromRole", "role")}
        if payload.get("agent_generated") is True or roles & {"seller", "outbound", "sent", "staff", "human", "operator", "merchant", "system"}:
            return self._outcome("CANONICAL_NON_BUYER_MESSAGE")
        if not str(payload.get("content") or payload.get("text") or "").strip():
            return self._outcome("CANONICAL_EMPTY_TEXT")
        try:
            stored_context = self._inbox.latest_canonical_context(body)
            enriched = {**dict(body), **stored_context}
            result = await self._quote_continuation.process(body) if self._quote_continuation is not None else None
            if result is None:
                result = await self._agent.process(enriched)
        except Exception:
            return self._outcome("AGENT_REPLY_UNAVAILABLE", reason="canonical_agent_failed")
        quoted = next((entry.get("result") for entry in reversed(result.get("tool_trace") or [])
                       if isinstance(entry, Mapping) and isinstance(entry.get("result"), Mapping)
                       and entry["result"].get("status") == "QUOTED"), None)
        # A successful re-quote is expressed from its final sell-price snapshot,
        # never from model arithmetic or provider cost text.
        reply = str(result.get("reply") or "") if result.get("status") == "AGENT_REPLY_READY" else ""
        # Once a W+ area quote exists, this buyer question is fully answered by
        # the area quote. Keep the response deterministic and do not let a slow
        # model turn it into an unverifiable seat-level claim.
        current_quote = _mapping(_mapping(result.get("context")).get("current_quote"))
        current_text = str(payload.get("content") or payload.get("text") or "").strip()
        if current_quote.get("request_type") == "WPLUS_AREA" and _is_area_purchase_question(current_text):
            reply = "可以购买，需要几张呢"
            result["status"] = "AGENT_REPLY_READY"
            result["reason"] = "wplus_area_purchase_question"
        rendered: dict[str, Any] | None = None
        if quoted is not None:
            rendered = self._reply_renderer.render(quoted)
            reply = str(rendered.get("text") or "")
            if callable(self._quote_context_writer):
                self._quote_context_writer(body, quoted)
        outcome = self._outcome("AGENT_REPLY_READY" if reply else "AGENT_REPLY_UNAVAILABLE", reason=result.get("reason"))
        outcome.update({"agent_run_id": result.get("agent_run_id"), "tool_trace": result.get("tool_trace") or []})
        if quoted:
            outcome.update({key: quoted[key] for key in ("quote", "recognition") if key in quoted})
        outcome["canonical_purchase_context_id"] = str(
            _mapping(outcome.get("quote")).get("purchase_context_id")
            or _mapping(stored_context.get("current_purchase_context")).get("purchase_context_id")
            or payload.get("itemId") or payload.get("item_id") or ""
        )
        if reply:
            event_id = str(envelope.get("id") or envelope.get("eventId") or "")
            messages = rendered.get("messages") if isinstance(rendered, Mapping) else None
            ordered = [item for item in messages if isinstance(item, Mapping) and str(item.get("text") or "").strip()] if isinstance(messages, list) else []
            if not ordered:
                ordered = [{"kind": "reply", "text": reply}]
            outcome["current_runtime_reply"] = reply
            outcome["current_runtime_replies"] = [{"kind": str(item.get("kind") or "reply"), "text": str(item["text"])} for item in ordered]
            outcome["decision"]["actions"] = [{
                "id": f"{event_id}:canonical-reply:{index}", "type": "send_message", "text": str(item["text"]),
                "rule_governed": True, "source": "canonical_conversation_agent",
                "dedupe_key": f"canonical-reply:{event_id}:{index}",
                "quote_record_id": _mapping(outcome.get("quote")).get("record_id"),
            } for index, item in enumerate(ordered)]
        return outcome

    @staticmethod
    def _outcome(status: str, *, reason: str | None = None) -> dict[str, Any]:
        return {"canonical_agent_status": status, "decision": {
            "mode": "canonical", "reason": reason or status.lower(), "actions": [],
        }}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _is_area_purchase_question(text: str) -> bool:
    normalized = "".join(str(text or "").split())
    return "能买吗" in normalized or "可以买吗" in normalized or "能购买吗" in normalized
