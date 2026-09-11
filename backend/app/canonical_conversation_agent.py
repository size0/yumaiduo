from __future__ import annotations

import json
import os
import time
import re
import asyncio
import logging
from time import monotonic
from dataclasses import dataclass, field

import httpx
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse
from decimal import Decimal, InvalidOperation

from .quote_v2.service import CanonicalQuoteRequest

LOGGER = logging.getLogger(__name__)


AGENT_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {"type": "function", "function": {"name": "get_current_context", "description": "Read the current purchase context and all fact tiers.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_current_quote", "description": "Read the current valid quote and its lifecycle state.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_transaction_state", "description": "Read the authoritative transaction state.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_order", "description": "Read the authoritative order snapshot.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_show_options", "description": "Read show options for an already identified movie and cinema.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_seat_status", "description": "Read authoritative realtime seat status for the current request.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "update_purchase_request", "description": "Add structured missing purchase fields; backend validates and prices them.", "parameters": {"type": "object", "properties": {"ticket_count": {"type": ["integer", "null"], "minimum": 1, "maximum": 20}, "selected_seats": {"type": ["array", "null"], "items": {"type": "string"}}, "city": {"type": ["string", "null"], "maxLength": 200}, "cinema": {"type": ["string", "null"], "maxLength": 200}, "movie": {"type": ["string", "null"], "maxLength": 200}, "quote_date": {"type": ["string", "null"], "maxLength": 200}, "showtime_start": {"type": ["string", "null"]}, "hall": {"type": ["string", "null"]}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "request_quote", "description": "Request a quote for the current structured purchase request; backend calculates the result.", "parameters": {"type": "object", "properties": {"ticket_count": {"type": ["integer", "null"], "minimum": 1, "maximum": 20}, "selected_seats": {"type": ["array", "null"], "items": {"type": "string"}}, "city": {"type": ["string", "null"], "maxLength": 200}, "cinema": {"type": ["string", "null"], "maxLength": 200}, "movie": {"type": ["string", "null"], "maxLength": 200}, "quote_date": {"type": ["string", "null"], "maxLength": 200}, "showtime_start": {"type": ["string", "null"]}, "hall": {"type": ["string", "null"]}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "select_existing_quote", "description": "Select an existing quote for the current purchase context.", "parameters": {"type": "object", "properties": {"quote_index": {"type": "integer", "minimum": 0}}, "required": ["quote_index"], "additionalProperties": False}}},
)


@dataclass(frozen=True)
class ReplyGuardResult:
    """Result of validating model prose against authoritative facts.

    The guard is deliberately a post-generation safety boundary.  It does
    not classify the buyer's intent and it never creates business facts; it
    only prevents a free-form model response from asserting a transaction
    fact that was not present in the context or returned by a successful
    high-level tool.
    """

    allowed: bool
    reason: str | None = None
    violations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "violations": list(self.violations),
        }


class AgentReplyGuard:
    """Fail closed on unsupported price/order/seat/payment assertions."""

    _PRICE_RE = re.compile(r"(?:[¥￥]\s*)?(\d+(?:\.\d{1,2})?)\s*(?:元|块|人民币|/张|每张)")
    _SEAT_RE = re.compile(r"\d+\s*(?:排|行)\s*\d+\s*座")
    _PRICE_ASSERTION_RE = re.compile(r"(?:价格|报价|单价|总价|合计|费用).{0,12}(?:是|为|：|:|[¥￥]|\d)")
    _SEAT_ASSERTION_RE = re.compile(r"(?:可售|有票|无票|售罄|不可售|已售|锁定|空闲)")
    _ORDER_ASSERTION_RE = re.compile(r"(?:订单(?:号|已|状态)|已下单|出票(?:中|成功|完成)?|已出票|发货|退款(?:中|成功|完成)?)")
    _PAYMENT_ASSERTION_RE = re.compile(r"(?:已支付|已付款|支付成功|付款成功|已到账|未付款|待支付|支付失败|付款失败)")
    _UNCERTAINTY_RE = re.compile(r"(?:无法|暂未|不确定|待确认|请人工|稍等|没有查询到|无法确认|需要确认|尚未查到)")

    def validate(
        self,
        reply: str,
        context: AgentContext,
        tool_trace: list[Mapping[str, Any]] | None = None,
    ) -> ReplyGuardResult:
        text = _text(reply)
        if not text:
            return ReplyGuardResult(False, "reply_empty", ("empty_reply",))

        evidence = self._authoritative_evidence(context, tool_trace or [])
        violations: list[str] = []
        prices = self._prices(text)
        if prices and not prices.issubset(evidence["prices"]):
            violations.append("unverified_price")
        elif self._PRICE_ASSERTION_RE.search(text) and not evidence["prices"] and not self._is_uncertain(text):
            violations.append("price_without_quote_evidence")

        seats = {_normalize_seat(item) for item in self._SEAT_RE.findall(text)}
        if seats and not seats.issubset(evidence["seats"]):
            violations.append("unverified_seat")
        if self._SEAT_ASSERTION_RE.search(text) and not evidence["seat_status"] and not self._is_uncertain(text):
            violations.append("seat_status_without_evidence")

        if self._ORDER_ASSERTION_RE.search(text) and not evidence["order"] and not self._is_uncertain(text):
            violations.append("order_without_evidence")
        if self._PAYMENT_ASSERTION_RE.search(text) and not evidence["payment"] and not self._is_uncertain(text):
            violations.append("payment_without_evidence")

        if violations:
            return ReplyGuardResult(False, "reply_fact_unverified", tuple(violations))
        return ReplyGuardResult(True)

    def _authoritative_evidence(
        self, context: AgentContext, tool_trace: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Extract only backend-owned views; candidate recognition is excluded."""
        roots: list[Any] = [
            context.confirmed_facts,
            context.current_quote,
            context.authoritative_order,
            context.transaction_state,
            context.payment_validation_evidence,
            context.provider_fulfillment_state,
            context.same_type_reference_quote,
        ]
        for item in tool_trace:
            tool_name = str(item.get("tool") or "") if isinstance(item, Mapping) else ""
            # Context/options helpers are informational only.  In particular,
            # get_current_context and the fallback seat reader may contain
            # candidate recognition and must never become authority merely
            # because the model invoked a tool.
            if tool_name in {"get_current_context", "get_show_options"}:
                continue
            result = item.get("result") if isinstance(item, Mapping) else None
            if not isinstance(result, Mapping) or result.get("status") not in {"success", "QUOTED", "QUOTE_UPDATED", "QUOTE_SELECTED"}:
                continue
            if tool_name == "get_seat_status" and result.get("authoritative") is not True:
                continue
            roots.append(result)
        prices: set[int] = set()
        seats: set[str] = set()
        seat_status = False
        order = False
        payment = False

        def visit(value: Any, key: str = "") -> None:
            nonlocal seat_status, order, payment
            if isinstance(value, Mapping):
                for child_key, child in value.items():
                    name = str(child_key).lower()
                    if _is_price_key(name):
                        amount = _amount_to_fen(child, is_fen=name.endswith(("_fen", "fen", "_cents")))
                        if amount is not None:
                            prices.add(amount)
                    if name in {"selected_seats", "seats", "seat_labels"} and isinstance(child, (list, tuple)):
                        seats.update(_normalize_seat(item) for item in child if _normalize_seat(item))
                    if name in {"seat_available", "seat_status", "seat_facts", "availability", "available_seats"}:
                        seat_status = True
                    if name in {"order", "order_id", "platform_order_id", "order_status", "out_order_no"} and child not in (None, "", [], {}):
                        order = True
                    if name in {"payment", "payment_status", "payment_validation_evidence", "validation_status"} and child not in (None, "", [], {}):
                        payment = True
                    visit(child, name)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    visit(child, key)

        for root in roots:
            visit(root)
        return {"prices": prices, "seats": seats, "seat_status": seat_status, "order": order, "payment": payment}

    def _prices(self, text: str) -> set[int]:
        values: set[int] = set()
        for match in self._PRICE_RE.finditer(text):
            try:
                values.add(round(float(match.group(1)) * 100))
            except ValueError:
                continue
        return values

    def _is_uncertain(self, text: str) -> bool:
        if not self._UNCERTAINTY_RE.search(text):
            return False
        # An uncertainty preface must not launder a definitive claim in the
        # same reply (for example, "无法确认，但已支付").
        definitive = re.compile(
            r"(?:已支付|已付款|支付成功|付款成功|已到账|未付款|待支付|支付失败|付款失败|"
            r"已下单|出票(?:中|成功|完成)?|已出票|发货|退款(?:中|成功|完成)?|"
            r"(?:可售|有票|无票|售罄|不可售|已售|锁定|空闲)|"
            r"(?:价格|报价|单价|总价|合计|费用).{0,12}(?:是|为|：|:|[¥￥]|\d)"
            r")"
        )
        return not definitive.search(text)


class AgentModel(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: tuple[dict[str, Any], ...]) -> Mapping[str, Any]: ...


class _UnavailableAgentModel:
    """Fail closed when the UI-owned model authority cannot be resolved."""

    async def complete(self, messages: list[dict[str, Any]], tools: tuple[dict[str, Any], ...]) -> Mapping[str, Any]:
        raise RuntimeError("model_config_resolution_failed")


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
    inherited_screenshot_context: dict[str, Any] = field(default_factory=dict)
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
    same_type_reference_quote: dict[str, Any] | None = None
    manual_mark_result: Any = None

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
            "inherited_screenshot_context": self.inherited_screenshot_context,
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
            "same_type_reference_quote": self.same_type_reference_quote,
            "manual_mark_result": self.manual_mark_result,
        }


class AgentModelFailure(RuntimeError):
    """Safe model-boundary evidence; never includes raw requests or responses."""

    def __init__(self, stage: str, **details: Any) -> None:
        super().__init__(stage)
        self.diagnostic = {"stage": stage, **details}


class OpenAICompatibleAgentModel:
    """Small read-only chat-completions adapter; no platform tools are exposed."""

    def __init__(
        self, *, api_key: str, base_url: str, model: str, timeout_seconds: float = 30,
        temperature: float = 0, max_tokens: int | None = None,
        config_metadata: Mapping[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._config_metadata = dict(config_metadata or {})
        self._transport = transport

    def audit_view(self) -> dict[str, Any]:
        """Return model identity without credentials or authorization material."""
        metadata = dict(self._config_metadata)
        metadata.setdefault("provider", "OpenAI-compatible")
        metadata.setdefault("model", self._model)
        metadata.setdefault("base_url", self._base_url)
        metadata.setdefault("timeout_seconds", self._timeout)
        metadata.setdefault("temperature", self._temperature)
        metadata.setdefault("max_tokens", self._max_tokens)
        return metadata

    @staticmethod
    def _completion_url(base_url: str) -> str:
        normalized = base_url.strip().rstrip("/")
        if normalized.endswith("/chat/completions"):
            return normalized
        if not normalized.endswith("/v1"):
            normalized += "/v1"
        return normalized + "/chat/completions"

    async def complete(self, messages: list[dict[str, Any]], tools: tuple[dict[str, Any], ...]) -> Mapping[str, Any]:
        if not self._api_key:
            raise AgentModelFailure("configuration", error_code="missing_api_key")
        client_options = {"timeout": self._timeout}
        if self._transport is not None:
            client_options["transport"] = self._transport
        started = monotonic()
        try:
            async with httpx.AsyncClient(**client_options) as client:
                response = await asyncio.wait_for(client.post(
                self._completion_url(self._base_url),
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model, "messages": messages,
                    **({"tools": list(tools)} if tools else {}), "temperature": self._temperature,
                    **({"max_tokens": self._max_tokens} if self._max_tokens is not None else {}),
                },
                ), timeout=self._timeout)
        except (httpx.HTTPError, asyncio.TimeoutError) as error:
            raise AgentModelFailure(
                "timeout" if isinstance(error, (httpx.TimeoutException, asyncio.TimeoutError)) else "transport",
                exception_type=type(error).__name__, elapsed_ms=round((monotonic() - started) * 1000),
            ) from None
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        if request_id and (not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id) or self._api_key in request_id):
            request_id = None
        evidence = {"http_status": response.status_code, "request_id": request_id,
                    "elapsed_ms": round((monotonic() - started) * 1000)}
        if not response.is_success:
            raise AgentModelFailure("http", **evidence, error_code=f"http_{response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise AgentModelFailure("parse", **evidence, error_code="invalid_json") from None
        choices = data.get("choices") if isinstance(data, Mapping) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise AgentModelFailure("parse", **evidence, error_code="chat_choices_missing")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise AgentModelFailure("parse", **evidence, error_code="chat_message_missing")
        if choice.get("finish_reason") == "length":
            raise AgentModelFailure("truncated", **evidence)
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise AgentModelFailure("refusal", **evidence)
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls:
            ids = []
            for call in calls:
                function = call.get("function") if isinstance(call, Mapping) else None
                if (not isinstance(call, Mapping) or call.get("type") != "function"
                    or not isinstance(call.get("id"), str) or not call["id"]
                    or not isinstance(function, Mapping) or not isinstance(function.get("name"), str)
                    or not isinstance(function.get("arguments"), str)):
                    raise AgentModelFailure("parse", **evidence, error_code="invalid_tool_call")
                ids.append(call["id"])
            if len(set(ids)) != len(ids):
                raise AgentModelFailure("parse", **evidence, error_code="duplicate_tool_call_id")
            return {"tool_calls": calls}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AgentModelFailure("empty_response", **evidence)
        return {"reply": content.strip()}


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
        # Carry the inbound event identity into the ephemeral context so each
        # distinct buyer message gets a stable idempotency key while retries of
        # the same event remain safe.
        event_id = _pick(envelope, "id", "eventId", "event_id")
        message_id = _pick(payload, "remoteMessageId", "remote_message_id", "messageId", "message_id")
        if event_id or message_id:
            purchase_context = {
                **dict(purchase_context),
                "request_id": purchase_context.get("request_id") or event_id or message_id,
                "event_id": purchase_context.get("event_id") or event_id,
                "message_id": purchase_context.get("message_id") or message_id,
            }
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
        if "canonical_quote_record_id" in purchase_context:
            latest_id = purchase_context.get("canonical_quote_record_id")
            # A newer image may still need a city/show clarification. Do not
            # silently complete it with the previous image's quote facts.
            current_quote = next((item for item in quote_records if latest_id and item.get("record_id") == latest_id), None)
        if not _pick(payload, "itemId", "item_id"):
            identity["purchase_context_id"] = str(
                purchase_context.get("purchase_context_id")
                or (current_quote or {}).get("purchase_context_id")
                or identity["purchase_context_id"]
            )

        same_type_reference = _mapping_or_none(body.get("same_type_reference_quote"))
        if same_type_reference is None:
            same_type_reference = _mapping_or_none(body.get("same_type_reference"))
        if same_type_reference is None:
            same_type_reference = _mapping_or_none(purchase_context.get("same_type_reference"))
        if same_type_reference is None and current_quote is not None:
            same_type_reference = _mapping_or_none(current_quote.get("same_type_reference"))
        transaction = _mapping_or_none(body.get("transaction_state"))
        if transaction is None and self._transaction_store is not None and all(identity.values()):
            try:
                state = self._transaction_store.get(**{key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")})
                transaction = state.model_dump(mode="json") if state is not None else None
            except Exception:
                transaction = None
        transaction_view = transaction or {"status": "absent"}
        recognition = _mapping_or_none(body.get("canonical_recognition"))
        if recognition is not None:
            source = _text(recognition.get("source"))
            if source and source != "LIANGPIAO":
                raise ValueError("canonical_recognition_source_invalid")
        if recognition is None and self._recognition_store is not None and identity["chat_id"]:
            try:
                previous = self._recognition_store.recent(identity["chat_id"])
                if previous:
                    latest = previous[-1]
                    recognition = latest.model_dump(mode="json") if hasattr(latest, "model_dump") else _mapping_or_none(latest)
            except Exception:
                recognition = None
        manual_mark_result = body.get("manual_mark_result")
        if manual_mark_result is None and recognition is not None:
            manual_mark_result = recognition.get("manual_mark_result")
        confirmed = _confirmed_facts(current_quote, transaction)
        candidate = dict(recognition or {})
        inherited_screenshot = _inherited_screenshot_context(recognition)
        expired = _list_of_mappings(body.get("expired_facts"))
        return AgentContext(
            **identity,
            fishmore_im_history=history,
            fishmore_history_available=body.get("authoritative_history_available") is not False,
            recent_canonical_recognition=recognition,
            inherited_screenshot_context=inherited_screenshot,
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
            same_type_reference_quote=same_type_reference,
            manual_mark_result=manual_mark_result,
        )


class CanonicalAgentToolBackend:
    """Deterministic backend for the agent's high-level tools.

    The model only supplies changes to a purchase request.  This adapter
    combines those changes with the already-built context and delegates quote
    calculation to :class:`CanonicalQuoteRuntime`; prices and provider facts
    never cross the model boundary.  Read tools use the existing stores when
    available and otherwise expose the context snapshot assembled for this
    event.
    """

    def __init__(
        self,
        *,
        quote_runtime: Any | None = None,
        quote_store: Any | None = None,
        transaction_store: Any | None = None,
        order_reader: Any | None = None,
        show_options_reader: Any | None = None,
        seat_status_reader: Any | None = None,
    ) -> None:
        self._quote_runtime = quote_runtime
        self._quote_store = quote_store
        self._transaction_store = transaction_store
        self._order_reader = order_reader
        self._show_options_reader = show_options_reader
        self._seat_status_reader = seat_status_reader

    async def update_quote_request(
        self, updates: Mapping[str, Any], context: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self._quote_runtime is None or not callable(
            getattr(self._quote_runtime, "quote_structured", None),
        ):
            return {"status": "error", "reason": "quote_runtime_unavailable"}
        request, missing = self._quote_request(updates, context)
        if missing:
            return {"status": "QUOTE_REQUEST_INCOMPLETE", "missing_fields": missing}
        try:
            result = self._quote_runtime.quote_structured(request)
            if hasattr(result, "__await__"):
                result = await result
        except (TypeError, ValueError):
            return {"status": "QUOTE_REQUEST_INVALID"}
        except Exception:
            # Provider/runtime failures are a semantic tool failure.  Do not
            # leak provider details into the model prompt.
            return {"status": "QUOTE_REQUEST_FAILED"}
        return _tool_result(result)

    async def get_current_context(
        self, _arguments: Mapping[str, Any], context: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {"status": "success", "data": dict(context)}

    async def get_quote(
        self, _arguments: Mapping[str, Any], context: Mapping[str, Any],
    ) -> dict[str, Any]:
        identity = _context_identity(context)
        if self._quote_store is not None and all(identity.values()):
            try:
                quote_identity = {key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")}
                status, quotes = self._quote_store.list_current_quotes(
                    **quote_identity, at=datetime.now(timezone.utc),
                )
                return {"status": "success", "quote_status": status, "quotes": quotes}
            except Exception:
                return {"status": "error", "reason": "quote_read_failed"}
        return {
            "status": "success", "quote": context.get("current_quote"),
            "quotes": context.get("quote_records") or [],
        }

    async def get_current_quote(self, arguments: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        return await self.get_quote(arguments, context)

    async def get_transaction(
        self, _arguments: Mapping[str, Any], context: Mapping[str, Any],
    ) -> dict[str, Any]:
        identity = _context_identity(context)
        if self._transaction_store is not None and all(identity.values()):
            try:
                transaction_identity = {key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")}
                state = self._transaction_store.get(**transaction_identity)
                value = state.model_dump(mode="json") if hasattr(state, "model_dump") else state
                return {"status": "success", "transaction": value or {"status": "absent"}}
            except Exception:
                return {"status": "error", "reason": "transaction_read_failed"}
        return {"status": "success", "transaction": context.get("transaction_state")}

    async def get_transaction_state(self, arguments: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        return await self.get_transaction(arguments, context)

    async def get_order(
        self, arguments: Mapping[str, Any], context: Mapping[str, Any],
    ) -> dict[str, Any]:
        value = await self._read_external(self._order_reader, arguments, context)
        if value is not None:
            return _tool_result(value)
        return {"status": "success", "order": context.get("authoritative_order")}

    async def get_show_options(
        self, arguments: Mapping[str, Any], context: Mapping[str, Any],
    ) -> dict[str, Any]:
        value = await self._read_external(self._show_options_reader, arguments, context)
        if value is not None:
            return _tool_result(value)
        purchase = _mapping(context.get("current_purchase_context"))
        return {"status": "success", "options": purchase.get("show_options") or []}

    async def get_seat_status(
        self, arguments: Mapping[str, Any], context: Mapping[str, Any],
    ) -> dict[str, Any]:
        value = await self._read_external(self._seat_status_reader, arguments, context)
        if value is not None:
            return _tool_result(value)
        candidate = _mapping(context.get("candidate_facts"))
        return {"status": "success", "seat_facts": candidate.get("seat_facts")}

    async def select_quote(
        self, arguments: Mapping[str, Any], context: Mapping[str, Any],
    ) -> dict[str, Any]:
        quotes = context.get("quote_records")
        index = arguments.get("quote_index") if isinstance(arguments, Mapping) else None
        if not isinstance(quotes, list) or isinstance(index, bool) or not isinstance(index, int):
            return {"status": "error", "reason": "quote_index_invalid"}
        if index < 0 or index >= len(quotes):
            return {"status": "error", "reason": "quote_index_out_of_range"}
        return {"status": "success", "quote": quotes[index], "selected_quote_index": index}

    async def select_existing_quote(self, arguments: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        return await self.select_quote(arguments, context)

    async def update_purchase_request(self, updates: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        return await self.update_quote_request(updates, context)

    async def request_quote(self, updates: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        return await self.update_quote_request(updates, context)

    async def _read_external(
        self, reader: Any | None, arguments: Mapping[str, Any], context: Mapping[str, Any],
    ) -> Any | None:
        if not callable(reader):
            return None
        value = reader(dict(arguments), dict(context))
        if hasattr(value, "__await__"):
            value = await value
        return value

    @staticmethod
    def _quote_request(
        updates: Mapping[str, Any], context: Mapping[str, Any],
    ) -> tuple[CanonicalQuoteRequest | None, list[str]]:
        identity = _context_identity(context)
        purchase = _mapping(context.get("current_purchase_context"))
        quote = _mapping(context.get("current_quote"))
        recognition = _mapping(context.get("recent_canonical_recognition"))
        confirmed = _mapping(context.get("confirmed_facts"))
        candidate = _mapping(context.get("candidate_facts"))
        inherited_screenshot = _mapping(context.get("inherited_screenshot_context"))
        inherited_facts = _mapping(inherited_screenshot.get("facts"))
        # A previous screenshot is still candidate evidence, but it is the
        # structured input for resolving a short follow-up.  Keep it separate
        # from confirmed_facts so it cannot authorize price, inventory, or
        # transaction claims, while ensuring a model tool call does not drop
        # city/cinema/movie/date merely because the current turn says “that
        # one” or supplies only a showtime/quantity.
        sources = (quote, purchase, confirmed, inherited_facts, recognition, candidate)

        def value(*keys: str) -> Any:
            for source in sources:
                for key in keys:
                    item = source.get(key)
                    if item is not None and item != "":
                        return item
            return None

        def updated(name: str, *aliases: str) -> Any:
            for key in (name, *aliases):
                item = updates.get(key) if isinstance(updates, Mapping) else None
                if item is not None and item != "":
                    return item
            return value(name, *aliases)

        selected = updated("selected_seats", "seats")
        if selected is not None and not isinstance(selected, list):
            selected = None
        request_type = str(value("seat_request_type", "request_type", "quote_scope") or "").upper()
        if request_type not in {"WPLUS_AREA", "EXACT_SEATS"}:
            request_type = "EXACT_SEATS" if selected else "WPLUS_AREA"
        required = {
            "tenant_id": identity.get("tenant_id"),
            "shop_id": identity.get("shop_id"),
            "buyer_id": identity.get("buyer_id"),
            "chat_id": identity.get("chat_id"),
            "purchase_context_id": identity.get("purchase_context_id"),
            "city": updated("city", "city_text"),
            "cinema": updated("cinema", "cinema_text"),
            "movie": updated("movie"),
            "quote_date": updated("quote_date", "show_date", "date"),
            "showtime_start": updated("showtime_start", "start_time", "showtime"),
        }
        missing = [name for name, item in required.items() if not str(item or "").strip()]
        if missing:
            return None, missing
        request_id = str(
            purchase.get("request_id") or purchase.get("event_id") or purchase.get("message_id")
            or value("request_id", "event_id", "message_id")
            or f'agent:{identity["purchase_context_id"]}'
        ).strip()
        ticket_count = updated("ticket_count", "quantity")
        if ticket_count is not None:
            if type(ticket_count) is not int or not 1 <= ticket_count <= 20:
                return None, ["ticket_count"]
        return CanonicalQuoteRequest(
            tenant_id=str(identity["tenant_id"]), shop_id=str(identity["shop_id"]),
            buyer_id=str(identity["buyer_id"]), chat_id=str(identity["chat_id"]),
            purchase_context_id=str(identity["purchase_context_id"]), request_id=request_id,
            city=str(required["city"]), cinema=str(required["cinema"]),
            movie=str(required["movie"]), quote_date=str(required["quote_date"]),
            showtime_start=str(required["showtime_start"]),
            cinema_address=_optional_text(updated("cinema_address", "address", "cinemaAddress")),
            hall=_optional_text(updated("hall")),
            dimension=_optional_text(value("dimension", "format")),
            language=_optional_text(value("language")), seat_request_type=request_type,
            ticket_count=ticket_count, ticket_mode=str(value("ticket_mode") or "STANDARD"),
            area_quote_strategy=_optional_text(value("area_quote_strategy")),
            selected_seats=[
                str(item.get("seat_label") or item.get("seat_number") or item.get("label") or "")
                if isinstance(item, Mapping) else str(item)
                for item in selected
            ] if selected else None,
            has_manual_mark=value("has_manual_mark"), image_url=_optional_text(value("image_url")),
            message_id=_optional_text(purchase.get("message_id") or value("message_id")),
        ), []


class CanonicalConversationAgent:
    """Natural-language entry point for canonical-enabled conversations.

    The model may express or request structured facts only through high-level
    tools. It cannot invoke provider/order/payment/fulfillment operations.
    """

    def __init__(
        self,
        context_builder: AgentContextBuilder,
        model: AgentModel,
        *,
        tool_backend: Any | None = None,
        max_tool_rounds: int = 4,
        reply_guard: AgentReplyGuard | None = None,
        audit_store: Any | None = None,
        model_resolver: Callable[[str, str, str], Any] | None = None,
    ) -> None:
        self._context_builder = context_builder
        self._model = model
        self._tool_backend = tool_backend
        self._max_tool_rounds = max(1, min(int(max_tool_rounds), 8))
        self._reply_guard = reply_guard or AgentReplyGuard()
        self._audit_store = audit_store
        self._model_resolver = model_resolver

    async def process(self, body: Mapping[str, Any]) -> dict[str, Any]:
        trace_started = time.monotonic()
        context = await self._context_builder.build(body)
        model, model_metadata = self._resolve_model(context)
        run_id = self._start_audit_run(context, body, model_metadata)
        if body.get("authoritative_history_available") is False:
            result = {
                "status": "AGENT_REPLY_UNAVAILABLE", "reason": "conversation_snapshot_unavailable",
                "context": context.to_dict(), "tool_trace": [], "actions": [], "agent_run_id": run_id,
            }
            self._finish_audit_run(run_id, result, context, started_at=trace_started)
            return result
        current_text = _current_text(body)
        if os.getenv("CANONICAL_TRACE_LOG") == "1":
            LOGGER.info("canonical_trace stage=input event_id=%s text=%s", _pick(_mapping(body.get("envelope")), "id", "eventId"), current_text)
        system = (
            "你是Canonical购票会话助手。只根据后端提供的上下文和高层工具工作；"
            "不要使用关键词、正则或固定意图规则。confirmed_facts才是已确认事实，"
            "candidate_facts需要核验，expired_facts不可直接使用。不要自行计算价格，"
            "不要改变金额、座位可售性、付款、出票、退款或订单状态。需要补充信息时只追问真正缺失或歧义的字段。"
            "普通非购票消息也要自然回答，但不能覆盖交易事实。"
            "inherited_screenshot_context 是同一会话最近一张截图的结构化候选事实；它不是实时库存、最终价格或交易确认，"
            "但必须用于当前短跟进的指代解析和补全 purchase request。当前消息只改变它明确指出的字段；"
            "例如‘13点10分那场’只更新 showtime_start，‘两张’只更新 ticket_count，‘就这个’、‘这个场次’、‘第二场’、"
            "‘IMAX那场’、‘刚才截图那个’、‘还是刚才那个影院’都不得重新索要截图、影院、影片、日期或已知场次。"
            "只有真正缺失或存在多个候选的字段才可以追问。"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "system", "content": "后端上下文JSON：" + json.dumps(context.to_dict(), ensure_ascii=False, separators=(",", ":"))},
            {"role": "user", "content": current_text},
        ]
        trace: list[dict[str, Any]] = []
        # The tool budget counts execution rounds, not the final prose turn.
        # Always allow the model to consume the final tool results once, with
        # no tools offered. Do not execute calls returned beyond the budget.
        for round_index in range(self._max_tool_rounds + 1):
            try:
                response = await model.complete(messages, AGENT_TOOL_SCHEMAS if round_index < self._max_tool_rounds else ())
            except Exception as error:
                result = {
                    "status": "AGENT_REPLY_UNAVAILABLE", "reason": "agent_model_failed",
                    "reply": "", "context": context.to_dict(), "tool_trace": trace, "actions": [],
                    "agent_run_id": run_id,
                    "model_diagnostic": (
                        dict(error.diagnostic) if isinstance(error, AgentModelFailure)
                        else {"stage": "model_client", "exception_type": type(error).__name__}
                    ),
                }
                self._finish_audit_run(run_id, result, context, started_at=trace_started)
                return result
            tool_calls = response.get("tool_calls") if isinstance(response, Mapping) else None
            if isinstance(tool_calls, list) and tool_calls:
                if round_index == self._max_tool_rounds:
                    result = {
                        "status": "AGENT_REPLY_UNAVAILABLE", "reason": "agent_tool_round_limit",
                        "reply": "", "context": context.to_dict(), "tool_trace": trace,
                        "actions": [], "agent_run_id": run_id,
                        "model_diagnostic": {"stage": "tool_round_limit"},
                    }
                    self._finish_audit_run(run_id, result, context, started_at=trace_started)
                    return result
                assistant_message = {"role": "assistant", "tool_calls": tool_calls}
                messages.append(assistant_message)
                for call in tool_calls:
                    result = await self._invoke_tool(call, context)
                    name = _tool_name(call)
                    raw_arguments, parsed_arguments = _tool_arguments(call)
                    trace.append({
                        "tool": name,
                        "raw_arguments": raw_arguments,
                        "validated_arguments": (
                            parsed_arguments
                            if result.get("status") in {"success", "QUOTED", "QUOTE_UPDATED", "QUOTE_SELECTED"}
                            else {}
                        ),
                        "result": result,
                    })
                    self._record_tool_audit(run_id, name, call, result)
                    if os.getenv("CANONICAL_TRACE_LOG") == "1":
                        LOGGER.info("canonical_trace stage=tool tool=%s raw=%s validated=%s result=%s", name, json.dumps(call, ensure_ascii=False, separators=(",", ":")), json.dumps(call.get("arguments", {}), ensure_ascii=False, separators=(",", ":")), json.dumps(result, ensure_ascii=False, separators=(",", ":")))
                    messages.append({
                        "role": "tool", "name": name,
                        "tool_call_id": _tool_call_id(call),
                        "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    })
                continue
            reply = _text(response.get("reply")) if isinstance(response, Mapping) else None
            if reply:
                guard = self._reply_guard.validate(reply, context, trace)
                if not guard.allowed:
                    result = {
                        "status": "AGENT_REPLY_UNAVAILABLE", "reason": guard.reason,
                        # Retain the rejected candidate only in the protected
                        # audit projection; it is never sent as final_reply.
                        "reply": "", "model_reply": reply, "reply_guard": guard.to_dict(),
                        "context": context.to_dict(), "tool_trace": trace, "actions": [], "agent_run_id": run_id,
                    }
                    self._finish_audit_run(run_id, result, context, started_at=trace_started)
                    return result
                result = {
                    "status": "AGENT_REPLY_READY", "reply": reply,
                    "context": context.to_dict(), "tool_trace": trace, "agent_run_id": run_id,
                    "actions": [{
                    "type": "send_message", "text": reply,
                        "source": "canonical_conversation_agent", "rule_governed": True,
                        **({"agent_run_id": run_id} if run_id else {}),
                    }],
                }
                self._finish_audit_run(run_id, result, context, started_at=trace_started)
                return result
            break
        result = {
            "status": "AGENT_REPLY_UNAVAILABLE", "reason": "agent_response_missing", "reply": "",
            "context": context.to_dict(), "tool_trace": trace, "actions": [], "agent_run_id": run_id,
        }
        self._finish_audit_run(run_id, result, context, started_at=trace_started)
        return result

    def _resolve_model(self, context: AgentContext) -> tuple[AgentModel, dict[str, Any]]:
        """Resolve the active UI-owned config once per run; never put its key in context."""
        fallback = self._model
        metadata = _safe_model_metadata(fallback)
        resolver = self._model_resolver
        if not callable(resolver):
            return fallback, metadata
        try:
            resolved = resolver(context.tenant_id, context.shop_id, purpose="conversation_agent")
        except Exception:
            return _UnavailableAgentModel(), {"config_resolution_failed": True}
        if isinstance(resolved, Mapping):
            candidate = resolved.get("model")
            candidate_metadata = resolved.get("metadata")
            if hasattr(candidate, "complete"):
                return candidate, _safe_metadata_mapping(candidate_metadata, candidate)
        if hasattr(resolved, "complete"):
            return resolved, _safe_model_metadata(resolved)
        if all(hasattr(resolved, name) for name in ("api_key", "base_url", "model")):
            audit = resolved.audit_view() if callable(getattr(resolved, "audit_view", None)) else {}
            return OpenAICompatibleAgentModel(
                api_key=str(resolved.api_key or ""), base_url=str(resolved.base_url), model=str(resolved.model),
                timeout_seconds=float(getattr(resolved, "timeout_seconds", 30)),
                temperature=float(getattr(resolved, "temperature", 0)),
                max_tokens=getattr(resolved, "max_tokens", None), config_metadata=audit,
            ), _safe_metadata_mapping(audit, None)
        return _UnavailableAgentModel(), {"config_resolution_failed": True}

    def _start_audit_run(
        self, context: AgentContext, body: Mapping[str, Any], model_metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        store = self._audit_store
        create = getattr(store, "create_agent_run", None) if store is not None else None
        if not callable(create):
            return None
        envelope = _mapping(body.get("envelope"))
        event_id = _pick(envelope, "id", "eventId", "event_id")
        try:
            record = create(
                tenant_id=context.tenant_id, shop_id=context.shop_id,
                buyer_id=context.buyer_id, chat_id=context.chat_id,
                event_id=event_id, status="running", context=context.to_dict(),
                model_config_id=_text(model_metadata.get("config_id")) if model_metadata else None,
                model_config_revision=_safe_int(model_metadata.get("config_revision")) if model_metadata else None,
                model_provider=_text(model_metadata.get("provider")) if model_metadata else None,
                model_base_url_host=_url_host(model_metadata.get("base_url")) if model_metadata else None,
                model_name=_text(model_metadata.get("model")) if model_metadata else None,
            )
            return _text(record.get("run_id")) if isinstance(record, Mapping) else None
        except Exception:
            return None

    def _record_tool_audit(
        self, run_id: str | None, name: str, call: Any, result: Mapping[str, Any],
    ) -> None:
        store = self._audit_store
        append = getattr(store, "append_agent_tool_call", None) if store is not None else None
        if not run_id or not callable(append):
            return
        arguments = call.get("arguments", {}) if isinstance(call, Mapping) else {}
        if isinstance(call, Mapping) and isinstance(call.get("function"), Mapping):
            arguments = call["function"].get("arguments", arguments)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        try:
            append(
                run_id, tool_name=name,
                arguments=arguments if isinstance(arguments, Mapping) else {},
                result=dict(result), status=("succeeded" if result.get("status") in {"success", "QUOTED", "QUOTE_UPDATED", "QUOTE_SELECTED"} else "failed"),
                error_reason=_text(result.get("reason")),
            )
        except Exception:
            return

    def _finish_audit_run(
        self, run_id: str | None, result: Mapping[str, Any], context: AgentContext,
        *, started_at: float | None = None,
    ) -> None:
        if result.get("model_diagnostic"):
            # Some release compositions do not inject an audit store. Keep
            # safe boundary evidence in the normal service log regardless.
            LOGGER.warning("event=canonical_agent_model_failure diagnostic=%s",
                           json.dumps(result["model_diagnostic"], separators=(",", ":")))
        store = self._audit_store
        update = getattr(store, "update_agent_run", None) if store is not None else None
        if not run_id or not callable(update):
            return
        status = "ready" if result.get("status") == "AGENT_REPLY_READY" else "failed"
        try:
            buyer_messages = context.buyer_raw_messages
            current_input = buyer_messages[-1].get("text") if buyer_messages else ""
            audit_trace = {
                "user_input": _text(current_input),
                "tool_calls": list(result.get("tool_trace") or []),
                "model_reply": _text(result.get("model_reply") or result.get("reply")),
                "final_reply": _text(result.get("reply")) if status == "ready" else "",
                "reply_guard": result.get("reply_guard") if isinstance(result.get("reply_guard"), Mapping) else None,
                "duration_ms": round((time.monotonic() - started_at) * 1000, 1) if started_at else None,
                "end_reason": _text(result.get("reason")) or ("reply_ready" if status == "ready" else "agent_failed"),
            }
            update(
                run_id, status=status,
                reply_origin="canonical_conversation_agent" if status == "ready" else None,
                context={**context.to_dict(), "agent_trace": audit_trace, **(
                    {"model_diagnostic": result["model_diagnostic"]} if result.get("model_diagnostic") else {}
                )},
                failure_reason=_text(result.get("reason")) if status != "ready" else None,
            )
        except Exception:
            return

    async def _invoke_tool(self, call: Any, context: AgentContext) -> dict[str, Any]:
        name = _tool_name(call)
        if name not in {schema["function"]["name"] for schema in AGENT_TOOL_SCHEMAS}:
            return {"status": "error", "reason": "tool_not_allowed"}
        arguments = call.get("arguments", {}) if isinstance(call, Mapping) else {}
        if isinstance(call, Mapping) and isinstance(call.get("function"), Mapping):
            arguments = call["function"].get("arguments", arguments)
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
                try:
                    value = method(dict(arguments), context.to_dict())
                    if hasattr(value, "__await__"):
                        value = await value
                    return _tool_result(value)
                except Exception:
                    return {"status": "error", "reason": "tool_execution_failed"}
        view = context.to_dict()
        defaults = {
            "get_current_context": view,
            "get_quote": {"status": "success", "quote": context.current_quote, "quotes": context.quote_records},
            "get_order": {"status": "success", "order": context.authoritative_order},
            "get_transaction": {"status": "success", "transaction": context.transaction_state},
            "get_show_options": {"status": "success", "options": []},
            "get_seat_status": {"status": "success", "seat_facts": context.candidate_facts.get("seat_facts")},
        }
        defaults["get_current_quote"] = defaults["get_quote"]
        defaults["get_transaction_state"] = defaults["get_transaction"]
        if name in defaults:
            return {"status": "success", "data": defaults[name]}
        return {"status": "error", "reason": "tool_backend_unavailable"}


def _safe_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _url_host(value: Any) -> str | None:
    try:
        host = urlparse(str(value or "")).hostname
    except ValueError:
        host = None
    return host or None


def _safe_metadata_mapping(value: Any, model: Any | None) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else _safe_model_metadata(model)
    return {
        key: source[key] for key in (
            "config_id", "config_revision", "scope", "purpose", "provider", "base_url", "model",
            "timeout_seconds", "temperature", "max_tokens", "supported_capabilities",
        ) if key in source
    }


def _safe_model_metadata(model: Any) -> dict[str, Any]:
    audit = getattr(model, "audit_view", None)
    return _safe_metadata_mapping(audit() if callable(audit) else {}, None)


def _inherited_screenshot_context(recognition: Mapping[str, Any] | None) -> dict[str, Any]:
    """Expose screenshot facts as carry-forward candidates, not authorities.

    The event store supplies the latest successful recognition from the same
    tenant/shop/buyer/chat.  Keeping a small normalized projection makes the
    boundary explicit: an agent can resolve references against the prior
    screenshot, but only quote/order tools can turn those facts into an
    authoritative result.
    """
    source = _mapping(recognition)
    if not source:
        return {}
    aliases = {
        "city": ("city", "city_text"),
        "cinema": ("cinema", "cinema_text"),
        "cinema_address": ("cinema_address", "address"),
        "movie": ("movie", "movie_name"),
        "quote_date": ("quote_date", "show_date", "date"),
        "showtime_start": ("showtime_start", "start_time", "showtime"),
        "hall": ("hall", "hall_name"),
        "language": ("language",),
        "dimension": ("dimension", "format"),
        "selected_seats": ("selected_seats", "seats"),
    }
    facts: dict[str, Any] = {}
    for name, keys in aliases.items():
        for key in keys:
            value = source.get(key)
            if isinstance(value, list):
                if value:
                    facts[name] = list(value)
                    break
            elif value is not None and str(value).strip():
                facts[name] = value
                break
    if not facts:
        return {}
    return {
        "available": True,
        "fact_tier": "candidate",
        "source": "recent_canonical_screenshot",
        "facts": facts,
        "carry_forward_fields": sorted(facts),
    }


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


def _tool_arguments(call: Any) -> tuple[Any, dict[str, Any]]:
    if not isinstance(call, Mapping):
        return {}, {}
    function = call.get("function") if isinstance(call.get("function"), Mapping) else call
    raw = function.get("arguments", {}) if isinstance(function, Mapping) else {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw, {}
        return raw, dict(parsed) if isinstance(parsed, Mapping) else {}
    return raw, dict(raw) if isinstance(raw, Mapping) else {}


def _tool_name(call: Any) -> str:
    if not isinstance(call, Mapping):
        return ""
    function = call.get("function") if isinstance(call.get("function"), Mapping) else call
    return _text(function.get("name")) or ""


def _tool_call_id(call: Any) -> str:
    if not isinstance(call, Mapping):
        return "tool-call"
    value = call.get("id")
    if not value and isinstance(call.get("function"), Mapping):
        value = call["function"].get("id")
    return _text(value) or "tool-call"


def _tool_result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {"status": "success", "data": value}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _context_identity(context: Mapping[str, Any]) -> dict[str, str]:
    identity = _mapping(context.get("identity"))
    return {
        "tenant_id": _text(identity.get("tenant_id")),
        "shop_id": _text(identity.get("shop_id")),
        "buyer_id": _text(identity.get("buyer_id")),
        "chat_id": _text(identity.get("chat_id")),
        "purchase_context_id": _text(identity.get("purchase_context_id")),
    }


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


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


def _is_price_key(name: str) -> bool:
    return name in {
        "unit_sell_price_fen", "total_sell_price_fen", "sell_price_fen",
        "unit_quote_cents", "total_quote_cents", "target_amount_cents",
        "paid_amount_fen", "paid_amount_cents", "expected_amount_cents",
    }


def _amount_to_fen(value: Any, *, is_fen: bool) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    amount = number if is_fen else number * 100
    return int(amount) if amount == amount.to_integral_value() else None


def _normalize_seat(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    match = re.search(r"(\d+)\s*(?:排|行)\s*(\d+)\s*座", text)
    return f"{match.group(1)}排{match.group(2)}座" if match else text
