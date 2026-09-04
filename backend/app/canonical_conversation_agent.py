from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol


AGENT_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {"type": "function", "function": {"name": "get_current_context", "description": "Read the current purchase context and all fact tiers.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_quote", "description": "Read the current valid quote and its lifecycle state.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "update_quote_request", "description": "Add structured missing purchase fields; backend validates and prices them.", "parameters": {"type": "object", "properties": {"ticket_count": {"type": ["integer", "null"], "minimum": 1, "maximum": 20}, "selected_seats": {"type": ["array", "null"], "items": {"type": "string"}}, "showtime_start": {"type": ["string", "null"]}, "hall": {"type": ["string", "null"]}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "select_quote", "description": "Select an existing quote for the current purchase context.", "parameters": {"type": "object", "properties": {"quote_index": {"type": "integer", "minimum": 0}}, "required": ["quote_index"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_order", "description": "Read the authoritative order snapshot.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_transaction", "description": "Read the authoritative transaction state.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_show_options", "description": "Read show options for an already identified movie and cinema.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_seat_status", "description": "Read authoritative realtime seat status for the current request.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
)


class AgentModel(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: tuple[dict[str, Any], ...]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class AgentContext:
    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    purchase_context_id: str
    fishmore_im_history: list[dict[str, Any]] = field(default_factory=list)
    fishmore_history_available: bool = True
    recent_canonical_recognition: dict[str, Any] | None = None
    current_purchase_context: dict[str, Any] = field(default_factory=dict)
    current_quote: dict[str, Any] | None = None
    quote_records: list[dict[str, Any]] = field(default_factory=list)
    confirmed_facts: dict[str, Any] = field(default_factory=dict)
    candidate_facts: dict[str, Any] = field(default_factory=dict)
    expired_facts: list[dict[str, Any]] = field(default_factory=list)
    buyer_raw_messages: list[dict[str, Any]] = field(default_factory=list)
    order_binding: dict[str, Any] = field(default_factory=dict)
    authoritative_order: dict[str, Any] | None = None
    transaction_state: dict[str, Any] = field(default_factory=dict)
    payment_validation_evidence: dict[str, Any] = field(default_factory=dict)
    provider_fulfillment_state: dict[str, Any] = field(default_factory=dict)
    human_manual_context: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": {
                "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                "buyer_id": self.buyer_id, "chat_id": self.chat_id,
                "purchase_context_id": self.purchase_context_id,
            },
            "fishmore_im_history": self.fishmore_im_history,
            "fishmore_history_available": self.fishmore_history_available,
            "recent_canonical_recognition": self.recent_canonical_recognition,
            "current_purchase_context": self.current_purchase_context,
            "current_quote": self.current_quote,
            "quote_records": self.quote_records,
            "confirmed_facts": self.confirmed_facts,
            "candidate_facts": self.candidate_facts,
            "expired_facts": self.expired_facts,
            "buyer_raw_messages": self.buyer_raw_messages,
            "order_binding": self.order_binding,
            "authoritative_order": self.authoritative_order,
            "transaction_state": self.transaction_state,
            "payment_validation_evidence": self.payment_validation_evidence,
            "provider_fulfillment_state": self.provider_fulfillment_state,
            "human_manual_context": self.human_manual_context,
        }


class OpenAICompatibleAgentModel:
    """Small read-only chat-completions adapter; no platform tools are exposed."""

    def __init__(self, *, api_key: str, base_url: str, model: str, timeout_seconds: float = 30) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds

    async def complete(self, messages: list[dict[str, Any]], tools: tuple[dict[str, Any], ...]) -> Mapping[str, Any]:
        if not self._api_key:
            return {"reply": "当前无法读取会话状态，请稍等人工确认。"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "messages": messages, "tools": list(tools), "temperature": 0},
            )
        response.raise_for_status()
        message = response.json().get("choices", [{}])[0].get("message", {})
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls:
            return {"tool_calls": calls}
        return {"reply": _text(message.get("content"))}


class AgentContextBuilder:
    """Build an ephemeral aggregate view from existing authorities.

    This class intentionally has no persistence of its own. Quote, transaction,
    event and platform history remain owned by their existing stores/services.
    """

    def __init__(self, *, quote_store: Any | None = None, transaction_store: Any | None = None, recognition_store: Any | None = None) -> None:
        self._quote_store = quote_store
        self._transaction_store = transaction_store
        self._recognition_store = recognition_store

    async def build(self, body: Mapping[str, Any]) -> AgentContext:
        envelope = _mapping(body.get("envelope"))
        payload = _mapping(envelope.get("payload"))
        session = _mapping(body.get("session"))
        identity = {
            "tenant_id": _pick(envelope, "tenantId", "tenant_id") or _text(body.get("tenant_id")),
            "shop_id": _pick(session, "accountUnb", "account_unb") or _pick(payload, "accountUnb", "account_unb"),
            "buyer_id": _pick(session, "peerUnb", "peer_unb") or _pick(payload, "peerUnb", "peer_unb"),
            "chat_id": _pick(session, "chatId", "chat_id") or _pick(payload, "chatId", "chat_id"),
            "purchase_context_id": _pick(payload, "itemId", "item_id") or "",
        }
        if not identity["purchase_context_id"]:
            identity["purchase_context_id"] = f'chat:{identity["chat_id"]}'

        history = [_history_item(item) for item in _list(body.get("recent_messages") or body.get("recentMessages"))]
        current_message = _history_item({
            **payload, "direction": "buyer", "messageId": _pick(payload, "remoteMessageId", "remote_message_id", "messageId", "message_id"),
        })
        if current_message.get("text") and not any(
            item.get("message_id") == current_message.get("message_id") for item in history
        ):
            history.append(current_message)
        history.sort(key=lambda item: (item.get("timestamp") or 0, item.get("message_id") or ""))
        buyer_messages = [item for item in history if item.get("direction") == "buyer"]
        human_messages = [
            item for item in history
            if item.get("direction") == "seller" and item.get("agent_generated") is not True
        ]

        quote_records = _list_of_mappings(body.get("quote_records"))
        current_quote = _mapping_or_none(body.get("current_quote"))
        purchase_context = _mapping(body.get("current_purchase_context"))
        if current_quote is None:
            current_quote = _mapping_or_none(purchase_context.get("current_quote"))
        if current_quote is None and self._quote_store is not None and all(identity.values()):
            try:
                _, active = self._quote_store.list_current_quotes(
                    tenant_id=identity["tenant_id"], shop_id=identity["shop_id"],
                    buyer_id=identity["buyer_id"], chat_id=identity["chat_id"], at=datetime.now(timezone.utc),
                )
                quote_records = [dict(item) for item in active]
                current_quote = quote_records[0] if quote_records else None
            except Exception:
                quote_records = []
        if current_quote is not None and not quote_records:
            quote_records = [dict(current_quote)]

        transaction = _mapping_or_none(body.get("transaction_state"))
        if transaction is None and self._transaction_store is not None and all(identity.values()):
            try:
                state = self._transaction_store.get(**identity)
                transaction = state.model_dump(mode="json") if state is not None else None
            except Exception:
                transaction = None
        transaction_view = transaction or {"status": "absent"}
        recognition = _mapping_or_none(body.get("canonical_recognition"))
        if recognition is None and self._recognition_store is not None and identity["chat_id"]:
            try:
                previous = self._recognition_store.recent(identity["chat_id"])
                if previous:
                    latest = previous[-1]
                    recognition = latest.model_dump(mode="json") if hasattr(latest, "model_dump") else _mapping_or_none(latest)
            except Exception:
                recognition = None
        confirmed = _confirmed_facts(current_quote, transaction)
        candidate = dict(recognition or {})
        expired = _list_of_mappings(body.get("expired_facts"))
        return AgentContext(
            **identity,
            fishmore_im_history=history,
            fishmore_history_available=body.get("authoritative_history_available") is not False,
            recent_canonical_recognition=recognition,
            current_purchase_context=purchase_context,
            current_quote=current_quote,
            quote_records=quote_records,
            confirmed_facts=confirmed,
            candidate_facts=candidate,
            expired_facts=expired,
            buyer_raw_messages=buyer_messages,
            order_binding=_mapping(body.get("order_binding")),
            authoritative_order=_mapping_or_none(body.get("authoritative_order")),
            transaction_state=transaction_view,
            payment_validation_evidence=_mapping(body.get("payment_validation_evidence")),
            provider_fulfillment_state=_mapping(body.get("provider_fulfillment_state")),
            human_manual_context=human_messages,
        )


class CanonicalConversationAgent:
    """Natural-language entry point for canonical-enabled conversations.

    The model may express or request structured facts only through high-level
    tools. It cannot invoke provider/order/payment/fulfillment operations.
    """

    def __init__(self, context_builder: AgentContextBuilder, model: AgentModel, *, tool_backend: Any | None = None, max_tool_rounds: int = 4) -> None:
        self._context_builder = context_builder
        self._model = model
        self._tool_backend = tool_backend
        self._max_tool_rounds = max(1, min(int(max_tool_rounds), 8))

    async def process(self, body: Mapping[str, Any]) -> dict[str, Any]:
        context = await self._context_builder.build(body)
        if body.get("authoritative_history_available") is False:
            return {
                "status": "AGENT_REPLY_UNAVAILABLE", "reason": "conversation_snapshot_unavailable",
                "context": context.to_dict(), "tool_trace": [], "actions": [],
            }
        current_text = _current_text(body)
        system = (
            "你是Canonical购票会话助手。只根据后端提供的上下文和高层工具工作；"
            "不要使用关键词、正则或固定意图规则。confirmed_facts才是已确认事实，"
            "candidate_facts需要核验，expired_facts不可直接使用。不要自行计算价格，"
            "不要改变金额、座位可售性、付款、出票、退款或订单状态。需要补充信息时只追问真正缺失或歧义的字段。"
            "普通非购票消息也要自然回答，但不能覆盖交易事实。"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "system", "content": "后端上下文JSON：" + json.dumps(context.to_dict(), ensure_ascii=False, separators=(",", ":"))},
            {"role": "user", "content": current_text},
        ]
        trace: list[dict[str, Any]] = []
        for _ in range(self._max_tool_rounds):
            response = await self._model.complete(messages, AGENT_TOOL_SCHEMAS)
            tool_calls = response.get("tool_calls") if isinstance(response, Mapping) else None
            if isinstance(tool_calls, list) and tool_calls:
                assistant_message = {"role": "assistant", "tool_calls": tool_calls}
                messages.append(assistant_message)
                for call in tool_calls:
                    result = await self._invoke_tool(call, context)
                    name = _tool_name(call)
                    trace.append({"tool": name, "result": result})
                    messages.append({
                        "role": "tool", "name": name,
                        "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    })
                continue
            reply = _text(response.get("reply")) if isinstance(response, Mapping) else None
            if reply:
                return {
                    "status": "AGENT_REPLY_READY", "reply": reply,
                    "context": context.to_dict(), "tool_trace": trace,
                    "actions": [{
                        "type": "send_message", "text": reply,
                        "source": "canonical_conversation_agent", "rule_governed": True,
                    }],
                }
            break
        return {
            "status": "AGENT_REPLY_UNAVAILABLE", "reason": "agent_response_missing",
            "context": context.to_dict(), "tool_trace": trace, "actions": [],
        }

    async def _invoke_tool(self, call: Any, context: AgentContext) -> dict[str, Any]:
        name = _tool_name(call)
        arguments = call.get("arguments", {}) if isinstance(call, Mapping) else {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return {"status": "error", "reason": "tool_arguments_invalid"}
        if not isinstance(arguments, Mapping):
            return {"status": "error", "reason": "tool_arguments_invalid"}
        if self._tool_backend is not None:
            method = getattr(self._tool_backend, name, None)
            if callable(method):
                value = method(dict(arguments), context.to_dict())
                if hasattr(value, "__await__"):
                    value = await value
                return _tool_result(value)
        view = context.to_dict()
        defaults = {
            "get_current_context": view,
            "get_quote": {"status": "success", "quote": context.current_quote, "quotes": context.quote_records},
            "get_order": {"status": "success", "order": context.authoritative_order},
            "get_transaction": {"status": "success", "transaction": context.transaction_state},
            "get_show_options": {"status": "success", "options": []},
            "get_seat_status": {"status": "success", "seat_facts": context.candidate_facts.get("seat_facts")},
        }
        if name in defaults:
            return {"status": "success", "data": defaults[name]}
        return {"status": "error", "reason": "tool_backend_unavailable"}


def _confirmed_facts(quote: Mapping[str, Any] | None, transaction: Mapping[str, Any] | None) -> dict[str, Any]:
    if quote is None:
        return {}
    fields = (
        "city", "cinema", "movie", "quote_date", "showtime_start", "hall",
        "request_type", "quote_scope", "seat_zone_type", "unit_sell_price_fen",
        "total_sell_price_fen", "ticket_count", "quote_state", "transaction_authorized",
        "selected_seats",
    )
    result = {field: quote[field] for field in fields if field in quote and quote[field] is not None}
    if transaction is not None:
        result["transaction_flow_state"] = transaction.get("flow_state")
        result["transaction_revision"] = transaction.get("revision")
    return result


def _current_text(body: Mapping[str, Any]) -> str:
    payload = _mapping(_mapping(body.get("envelope")).get("payload"))
    value = payload.get("content") or payload.get("text")
    if isinstance(value, Mapping):
        value = value.get("text") or value.get("content")
    return _text(value) or ""


def _history_item(value: Any) -> dict[str, Any]:
    item = _mapping(value)
    direction = _text(item.get("direction") or item.get("role") or item.get("sender"))
    if direction in {"inbound", "buyer", "received", "receive", "peer", "user"}:
        direction = "buyer"
    elif direction in {"outbound", "seller", "sent", "staff", "human", "shop", "assistant"}:
        direction = "seller"
    else:
        direction = "unknown"
    content = item.get("content")
    if isinstance(content, Mapping):
        content = content.get("text") or content.get("content")
    return {
        "message_id": _pick(item, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id"),
        "direction": direction,
        "message_type": _pick(item, "messageType", "message_type"),
        "text": _text(content or item.get("text")),
        "timestamp": item.get("sentAtMs") or item.get("sent_at_ms") or item.get("timestamp") or item.get("createdAt") or item.get("created_at"),
        "agent_generated": item.get("agent_generated") is True,
        "has_image": item.get("messageType") in {2, "2"} or bool(item.get("imageUrls") or item.get("image_urls")),
    }


def _tool_name(call: Any) -> str:
    if not isinstance(call, Mapping):
        return ""
    function = call.get("function") if isinstance(call.get("function"), Mapping) else call
    return _text(function.get("name")) or ""


def _tool_result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {"status": "success", "data": value}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _list_of_mappings(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in _list(value) if isinstance(item, Mapping)]


def _pick(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(item.get(key))
        if value:
            return value
    return ""


def _text(value: Any) -> str:
    return str(value or "").strip()
