from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .canonical_conversation_agent import CanonicalAgentToolBackend
from .conversation_fact_patch_parser import ConversationFactPatchParser, merge_conversation_facts
from .conversation_fact_store import ConversationFactStore, _event_identity


class ConversationQuoteContinuation:
    """Resolve an explicit follow-up before asking a model to choose a tool.

    Only TTL-scoped, identity-isolated request facts are inherited. No old
    QuoteRecord, provider ID, cost or price is an input to the new quote.
    """

    def __init__(self, *, fact_store: ConversationFactStore, quote_runtime: Any) -> None:
        self._facts = fact_store
        self._backend = CanonicalAgentToolBackend(quote_runtime=quote_runtime)
        self._parser = ConversationFactPatchParser()

    async def process(self, body: Mapping[str, Any]) -> dict[str, Any] | None:
        envelope = body.get("envelope") or {}
        payload = envelope.get("payload") or {}
        text = str(payload.get("content") or payload.get("text") or "")
        stored = self._facts.context_for_event(body)
        facts = stored.get("facts") or {}
        patch = self._parser.parse(text, facts)
        # References such as 'still that cinema' remain model-led. This path
        # only continues an explicit time, resolved candidate or quantity.
        if not patch.facts and patch.reason != "show_selection_unresolved":
            return None
        if not stored.get("available"):
            return None
        identity = _event_identity(body)
        identity["purchase_context_id"] = str(stored.get("purchase_context_id") or identity["purchase_context_id"])
        if not all(identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id")):
            return None
        merged = merge_conversation_facts(facts, patch.facts)
        # A changed show/movie/cinema/date is a new authority boundary. Never
        # carry verification or provider IDs from the previous selection.
        if any(key in patch.facts for key in ("showtime_start", "showtime_ordinal", "movie", "cinema", "quote_date")):
            merged.pop("verified", None)
            merged.pop("show_id", None)
        context = {
            "identity": dict(identity), "candidate_facts": merged,
            "current_purchase_context": {"request_id": identity["event_id"] or identity["message_id"], "message_id": identity["message_id"]},
        }
        if patch.reason == "show_selection_unresolved":
            return self._result(context, "场次还不能唯一确定，请确认具体开场时间。")
        request, missing = self._backend._quote_request({}, context)
        if missing:
            labels = {"city": "城市", "cinema": "影院", "movie": "影片", "quote_date": "日期", "showtime_start": "开场时间", "ticket_count": "张数"}
            names = [labels[key] for key in missing if key in labels]
            return self._result(context, "请补充" + "、".join(names) + "。") if names else None
        # Persist explicit selection even if its official lookup fails: a
        # subsequent quantity must not resurrect the previous screenshot time.
        save_identity = {key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id")}
        self._facts.save(**save_identity, facts=merged, source="explicit_quote_followup", event_id=identity["event_id"])
        result = await self._backend.update_quote_request({}, context)
        trace = [{"tool": "request_quote", "raw_arguments": patch.facts, "validated_arguments": patch.facts, "result": result}]
        if result.get("status") == "QUOTED":
            quote = result.get("quote") or {}
            show_id = quote.get("wanda_show_id") or quote.get("show_id")
            verified = {**merged, **({"show_id": show_id, "verified": True} if show_id else {})}
            self._facts.save(**save_identity, facts=verified, source="official_quote_followup", event_id=identity["event_id"], fact_tier="verified")
            return self._result(context, "", trace)
        if result.get("status") == "SHOW_UNRESOLVED":
            return self._result(context, f"我没核到 {request.showtime_start} 这一场，请确认开场时间。", trace)
        reply = str(result.get("current_runtime_reply") or "这场暂时无法取得可核验价格，请稍后重试。")
        return self._result(context, reply, trace)

    @staticmethod
    def _result(context: Mapping[str, Any], reply: str, trace: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"status": "AGENT_REPLY_READY", "reason": "conversation_quote_continuation", "reply": reply, "context": dict(context), "tool_trace": trace or [], "actions": []}

