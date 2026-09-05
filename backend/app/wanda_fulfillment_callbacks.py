from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping, MutableMapping
from typing import Any

from .models import WandaFulfillmentCallbackRequest
from .quote_record_store import QuoteRecordStore
from .rules_first_store import RulesFirstStore
from .transaction_state_store import StateRevisionConflict, TransactionState, TransactionStateStore


class CallbackError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CallbackVerifier:
    def __init__(
        self,
        secret: str,
        *,
        app_key: str = "",
        max_skew_seconds: int = 300,
        replay_cache: MutableMapping[str, float] | None = None,
    ) -> None:
        if not str(secret or "").strip():
            raise ValueError("wanda_fulfillment_callback_secret_required")
        self._secret = str(secret).encode("utf-8")
        self._app_key = str(app_key or "").strip()
        self._max_skew = max(30, int(max_skew_seconds))
        self._replay = replay_cache if replay_cache is not None else {}

    def sign(self, raw_body: bytes, timestamp: str, nonce: str) -> str:
        material = f"{self._app_key}{timestamp}{nonce}".encode("utf-8") + raw_body
        return hmac.new(self._secret, material, hashlib.sha256).hexdigest()

    def verify(
        self,
        raw_body: bytes,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        now: float | None = None,
    ) -> None:
        try:
            timestamp_value = int(str(timestamp))
        except (TypeError, ValueError) as error:
            raise CallbackError("WANDA_FULFILLMENT_CALLBACK_TIMESTAMP_INVALID", "Wanda 回调时间戳无效。") from error
        current = float(time.time() if now is None else now)
        if abs(current - timestamp_value) > self._max_skew:
            raise CallbackError("WANDA_FULFILLMENT_CALLBACK_EXPIRED", "Wanda 回调已过期。")
        nonce_value = str(nonce or "").strip()
        signature_value = str(signature or "").strip().lower()
        if not nonce_value or not signature_value:
            raise CallbackError("WANDA_FULFILLMENT_CALLBACK_SIGNATURE_MISSING", "Wanda 回调签名不完整。")
        previous = self._replay.get(nonce_value)
        if previous is not None and current - previous <= self._max_skew:
            raise CallbackError("WANDA_FULFILLMENT_CALLBACK_REPLAY", "重复回调已忽略。")
        expected = self.sign(raw_body, str(timestamp_value), nonce_value)
        if not hmac.compare_digest(expected, signature_value):
            raise CallbackError("WANDA_FULFILLMENT_CALLBACK_SIGNATURE_INVALID", "Wanda 回调签名校验失败。")
        self._replay[nonce_value] = current
        for key, value in list(self._replay.items()):
            if current - value > self._max_skew:
                self._replay.pop(key, None)


class WandaFulfillmentCallbackHandler:
    _IN_PROGRESS_STATES = frozenset({"RECEIVED", "IN_PROGRESS"})
    _SEND_RECONCILE_STATES = frozenset({"SENT", "RECONCILED"})

    def __init__(
        self,
        verifier: CallbackVerifier,
        *,
        state_store: TransactionStateStore,
        quote_store: QuoteRecordStore,
        rules_store: RulesFirstStore,
        enabled: bool = False,
    ) -> None:
        self._verifier = verifier
        self._states = state_store
        self._quotes = quote_store
        self._rules = rules_store
        self._enabled = bool(enabled)

    async def handle(
        self,
        raw_body: bytes | str,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
    ) -> dict[str, Any]:
        if not self._enabled:
            raise CallbackError("WANDA_FULFILLMENT_CALLBACK_DISABLED", "Wanda 回调开关未开启。")
        raw = raw_body.encode("utf-8") if isinstance(raw_body, str) else bytes(raw_body)
        durable_replay = getattr(self._rules, "has_wanda_fulfillment_callback_nonce", None)
        if callable(durable_replay) and durable_replay(nonce):
            raise CallbackError("WANDA_FULFILLMENT_CALLBACK_REPLAY", "重复回调已忽略。")
        self._verifier.verify(raw, signature=signature, timestamp=timestamp, nonce=nonce)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CallbackError("WANDA_FULFILLMENT_CALLBACK_JSON_INVALID", "Wanda 回调不是有效 JSON。") from error
        if not isinstance(body, Mapping):
            raise CallbackError("WANDA_FULFILLMENT_CALLBACK_JSON_INVALID", "Wanda 回调不是对象。")
        request = WandaFulfillmentCallbackRequest.model_validate(body)
        state = self._require_state(request)
        quote = self._require_quote(request)
        self._validate_binding(request, state, quote)
        delivery_status = str(request.delivery_status or "").strip().upper()
        if delivery_status in self._SEND_RECONCILE_STATES or request.send_reconciliation:
            return await self._reconcile_send(request, state)
        if delivery_status == "FAILED":
            return await self._mark_manual_hold(request, state, "wanda_fulfillment_send_failed")
        if request.status in self._IN_PROGRESS_STATES:
            return await self._mark_in_progress(request, state)
        return await self._queue_ticket_send(request, state)

    def _require_state(self, request: WandaFulfillmentCallbackRequest) -> TransactionState:
        state = self._states.get(
            tenant_id=request.tenant_id,
            shop_id=request.shop_id,
            buyer_id=request.buyer_id,
            chat_id=request.chat_id,
        )
        if state is None:
            raise CallbackError("WANDA_FULFILLMENT_TRANSACTION_MISSING", "未找到对应交易状态。")
        if state.state_id != request.transaction_id:
            raise CallbackError("WANDA_FULFILLMENT_TRANSACTION_MISMATCH", "回调 transaction_id 与当前交易不一致。")
        if state.order_id and state.order_id != request.order_id:
            raise CallbackError("WANDA_FULFILLMENT_ORDER_MISMATCH", "回调订单号与当前交易不一致。")
        if state.active_quote_record_id and state.active_quote_record_id != request.quote_id:
            raise CallbackError("WANDA_FULFILLMENT_QUOTE_MISMATCH", "回调 quote_id 与当前交易不一致。")
        if state.confirmed_quote_record_id and state.confirmed_quote_record_id != request.quote_id:
            raise CallbackError("WANDA_FULFILLMENT_QUOTE_MISMATCH", "回调 quote_id 与确认单不一致。")
        return state

    def _require_quote(self, request: WandaFulfillmentCallbackRequest) -> Mapping[str, Any]:
        quote = self._quotes.find_by_order(
            tenant_id=request.tenant_id,
            order_id=request.order_id,
            shop_id=request.shop_id,
            buyer_id=request.buyer_id,
            chat_id=request.chat_id,
        )
        if quote is None:
            quote = self._quotes.get_record(tenant_id=request.tenant_id, record_id=request.quote_id)
        if quote is None:
            raise CallbackError("WANDA_FULFILLMENT_QUOTE_MISSING", "未找到对应 quote。")
        if str(quote.get("record_id") or quote.get("quote_id") or "").strip() != request.quote_id:
            raise CallbackError("WANDA_FULFILLMENT_QUOTE_MISMATCH", "回调 quote_id 与当前 quote 不一致。")
        stored_order = str(quote.get("order_id") or quote.get("platform_order_id") or "").strip()
        if stored_order and stored_order != request.order_id:
            raise CallbackError("WANDA_FULFILLMENT_QUOTE_MISMATCH", "回调订单号与 quote 绑定不一致。")
        return quote

    def _validate_binding(
        self,
        request: WandaFulfillmentCallbackRequest,
        state: TransactionState,
        quote: Mapping[str, Any],
    ) -> None:
        allowed_states = {
            "PAID_WAITING_FULFILLMENT",
            "WAITING_WPLUS_MARK",
            "READY_FOR_MANUAL_TICKETING",
            "FULFILLMENT_IN_PROGRESS",
            "MANUAL_HOLD",
            "TICKET_SENT",
            "COMPLETED",
        }
        if state.flow_state not in allowed_states:
            raise CallbackError("WANDA_FULFILLMENT_STATE_NOT_READY", "当前交易尚未进入出票流程。")
        quote_state = str(quote.get("status") or "").strip().lower()
        if quote_state and quote_state not in {"succeeded", "active", "delivered"}:
            raise CallbackError("WANDA_FULFILLMENT_QUOTE_NOT_READY", "当前 quote 不可用于出票。")

    async def _mark_in_progress(
        self,
        request: WandaFulfillmentCallbackRequest,
        state: TransactionState,
    ) -> dict[str, Any]:
        updated = self._transition(
            request,
            state,
            flow_state="FULFILLMENT_IN_PROGRESS",
            transition_code="wanda_fulfillment_in_progress",
            updates={
                "order_id": request.order_id,
                "provider_status": "in_progress",
                "fulfillment_status": "claimed",
            },
        )
        return self._response(
            request,
            code="WANDA_FULFILLMENT_PROGRESS_RECORDED",
            state=updated,
            delivery_status="pending",
        )

    async def _queue_ticket_send(
        self,
        request: WandaFulfillmentCallbackRequest,
        state: TransactionState,
    ) -> dict[str, Any]:
        ticket_message = self._ticket_message(request.ticket_code, request.ticket_code_version)
        event_body = self._event_body(request, ticket_message)
        accepted = self._rules.enqueue_event(event_body)
        actions = [
            {
                "id": f"{request.event_id}:send-ticket-code",
                "type": "send_message",
                "text": ticket_message,
                "source": "wanda_fulfillment_callback",
                "rule_governed": True,
                "dedupe_key": self._command_dedupe_key(request),
            },
        ]
        claimed = self._rules.claim_event(tenant_id=request.tenant_id, event_id=request.event_id)
        if claimed is not None:
            commands = self._rules.complete_event(
                int(claimed["inbox_id"]),
                str(claimed["lease_token"]),
                commands=actions,
                state_revision=state.revision,
                result={
                    "status": "succeeded",
                    "delivery_status": "pending",
                    "accepted": accepted.get("accepted", True),
                },
            )
        else:
            commands = self._rules.append_commands(
                tenant_id=request.tenant_id,
                event_id=request.event_id,
                commands=actions,
                state_revision=state.revision,
            )
        command = commands[0] if commands else None
        updated = self._transition(
            request,
            state,
            flow_state="READY_FOR_MANUAL_TICKETING",
            transition_code="wanda_ticket_code_queued",
            updates={
                "order_id": request.order_id,
                "provider_status": "completed",
                "fulfillment_status": "ticket_issued",
                "ticket_codes": [request.ticket_code],
                "fulfillment_task_id": command.get("command_id") if command else None,
            },
        )
        return self._response(
            request,
            code="WANDA_FULFILLMENT_COMMAND_QUEUED",
            state=updated,
            delivery_status="pending",
            command_id=str(command["command_id"]) if command else None,
        )

    async def _reconcile_send(
        self,
        request: WandaFulfillmentCallbackRequest,
        state: TransactionState,
    ) -> dict[str, Any]:
        updated = self._transition(
            request,
            state,
            flow_state="TICKET_SENT",
            transition_code="wanda_send_reconciled",
            updates={
                "order_id": request.order_id,
                "provider_status": "sent",
                "fulfillment_status": "shipped",
                "ticket_codes": [request.ticket_code],
                "fulfillment_task_id": request.sent_message_id or state.fulfillment_task_id,
            },
        )
        return self._response(
            request,
            code="WANDA_FULFILLMENT_SEND_RECONCILED",
            state=updated,
            delivery_status="sent",
            command_id=request.sent_message_id or state.fulfillment_task_id,
        )

    async def _mark_manual_hold(
        self,
        request: WandaFulfillmentCallbackRequest,
        state: TransactionState,
        reason: str,
    ) -> dict[str, Any]:
        updated = self._transition(
            request,
            state,
            flow_state="MANUAL_HOLD",
            transition_code="wanda_fulfillment_manual_hold",
            updates={
                "order_id": request.order_id,
                "provider_status": "failed",
                "fulfillment_status": "failed",
            },
        )
        return self._response(
            request,
            code="WANDA_FULFILLMENT_MANUAL_HOLD",
            state=updated,
            delivery_status="failed",
            reason=reason,
            status="manual_hold",
        )

    def _transition(
        self,
        request: WandaFulfillmentCallbackRequest,
        state: TransactionState,
        *,
        flow_state: str,
        transition_code: str,
        updates: dict[str, Any],
    ) -> TransactionState:
        try:
            return self._states.transition(
                tenant_id=request.tenant_id,
                shop_id=request.shop_id,
                buyer_id=request.buyer_id,
                chat_id=request.chat_id,
                expected_revision=state.revision,
                event_id=request.event_id,
                transition_code=transition_code,
                flow_state=flow_state,
                updates=updates,
                allow_compatible_bootstrap=True,
            )
        except StateRevisionConflict as error:
            raise CallbackError("WANDA_FULFILLMENT_STATE_REVISION_CONFLICT", "交易状态已被其它事件更新。") from error
        except ValueError as error:
            raise CallbackError("WANDA_FULFILLMENT_STATE_UPDATE_REJECTED", "Wanda 回调状态推进失败。") from error

    def _response(
        self,
        request: WandaFulfillmentCallbackRequest,
        *,
        code: str,
        state: TransactionState,
        delivery_status: str,
        command_id: str | None = None,
        reason: str | None = None,
        status: str = "ok",
    ) -> dict[str, Any]:
        response = {
            "status": status,
            "code": code,
            "duplicate": False,
            "state_after": state.flow_state,
            "state_revision": state.revision,
            "delivery_status": delivery_status,
            "ticket_code_version": request.ticket_code_version,
            "command_id": command_id,
            "reason": reason,
            "event_id": request.event_id,
            "idempotency_key": request.idempotency_key,
            "order_id": request.order_id,
            "quote_id": request.quote_id,
            "transaction_id": request.transaction_id,
        }
        return {key: value for key, value in response.items() if value is not None}

    def _ticket_message(self, ticket_code: str, ticket_code_version: str) -> str:
        ticket = str(ticket_code or "").strip()
        version = str(ticket_code_version or "").strip()
        if version:
            return f"出票已完成，取票码：{ticket}\n版本：{version}"
        return f"出票已完成，取票码：{ticket}"

    def _event_body(self, request: WandaFulfillmentCallbackRequest, ticket_message: str) -> dict[str, Any]:
        event_id = str(request.event_id or "").strip() or f"wanda-fulfillment:{request.order_id}:{request.ticket_code_version}"
        return {
            "envelope": {
                "tenantId": request.tenant_id,
                "id": event_id,
                "event": "wanda.fulfillment.callback",
                "payload": {
                    "text": ticket_message,
                    "content": ticket_message,
                    "orderId": request.order_id,
                    "quoteId": request.quote_id,
                    "transactionId": request.transaction_id,
                    "ticketCodeVersion": request.ticket_code_version,
                },
            },
            "session": {
                "accountUnb": request.shop_id,
                "peerUnb": request.buyer_id,
                "chatId": request.chat_id,
            },
        }

    def _command_dedupe_key(self, request: WandaFulfillmentCallbackRequest) -> str:
        return ":".join(
            [
                "wanda-fulfillment",
                request.tenant_id,
                request.shop_id,
                request.order_id,
                request.quote_id,
                request.transaction_id,
                request.ticket_code_version,
                "send-ticket-code",
            ],
        )


def _text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _pick(source: object, *fields: str) -> str | None:
    if not isinstance(source, Mapping):
        return None
    for field in fields:
        value = _text(source.get(field))
        if value:
            return value
    return None
