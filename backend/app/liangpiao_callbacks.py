from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import MutableMapping
from typing import Any, Mapping

from .rule_contracts import ReplyPlan


class CallbackError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CallbackVerifier:
    """Verify provider callback signatures and reject replayed nonces."""

    def __init__(self, secret: str, *, max_skew_seconds: int = 300,
                 replay_cache: MutableMapping[str, float] | None = None) -> None:
        if not str(secret or "").strip():
            raise ValueError("liangpiao_callback_secret_required")
        self._secret = str(secret).encode("utf-8")
        self._max_skew = max(30, int(max_skew_seconds))
        self._replay = replay_cache if replay_cache is not None else {}

    def sign(self, raw_body: bytes, timestamp: str, nonce: str) -> str:
        material = f"{timestamp}{nonce}".encode("utf-8") + raw_body
        return hmac.new(self._secret, material, hashlib.sha256).hexdigest()

    def verify(self, raw_body: bytes, *, signature: str, timestamp: str, nonce: str,
               now: float | None = None) -> None:
        try:
            timestamp_value = int(str(timestamp))
        except (TypeError, ValueError) as error:
            raise CallbackError("LIANGPIAO_CALLBACK_TIMESTAMP_INVALID", "回调时间戳无效。") from error
        current = float(time.time() if now is None else now)
        if abs(current - timestamp_value) > self._max_skew:
            raise CallbackError("LIANGPIAO_CALLBACK_EXPIRED", "回调已过期。")
        nonce_value = str(nonce or "").strip()
        signature_value = str(signature or "").strip().lower()
        if not nonce_value or not signature_value:
            raise CallbackError("LIANGPIAO_CALLBACK_SIGNATURE_MISSING", "回调签名不完整。")
        previous = self._replay.get(nonce_value)
        if previous is not None and current - previous <= self._max_skew:
            raise CallbackError("LIANGPIAO_CALLBACK_REPLAY", "重复回调已忽略。")
        expected = self.sign(raw_body, str(timestamp_value), nonce_value)
        if not hmac.compare_digest(expected, signature_value):
            raise CallbackError("LIANGPIAO_CALLBACK_SIGNATURE_INVALID", "回调签名校验失败。")
        self._replay[nonce_value] = current
        for key, value in list(self._replay.items()):
            if current - value > self._max_skew:
                self._replay.pop(key, None)


class LiangpiaoCallbackHandler:
    """Map verified callbacks to one transaction and legal state transitions."""

    _ORDER = {"created": 1, "pending": 1, "paid": 2, "processing": 3, "shipped": 4, "ticket_sent": 5, "completed": 6, "cancelled": 99}

    def __init__(self, verifier: CallbackVerifier, *, state_store: object | None = None,
                 mapping_store: object | None = None, client: object | None = None,
                 enabled: bool = False) -> None:
        self._verifier = verifier
        self._states = state_store
        self._mapping = mapping_store if mapping_store is not None else {}
        self._client = client
        self._enabled = bool(enabled)

    async def handle(self, raw_body: bytes | str, *, signature: str, timestamp: str,
                     nonce: str) -> dict[str, Any]:
        if not self._enabled:
            raise CallbackError("LIANGPIAO_CALLBACK_DISABLED", "良票回调开关未开启。")
        raw = raw_body.encode("utf-8") if isinstance(raw_body, str) else bytes(raw_body)
        self._verifier.verify(raw, signature=signature, timestamp=timestamp, nonce=nonce)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CallbackError("LIANGPIAO_CALLBACK_JSON_INVALID", "回调内容不是有效 JSON。") from error
        if not isinstance(body, Mapping):
            raise CallbackError("LIANGPIAO_CALLBACK_JSON_INVALID", "回调内容不是对象。")
        out_order_no = _text(_pick(body, "outOrderNo", "out_order_no"))
        provider_order_no = _text(_pick(body, "providerOrderNo", "provider_order_no", "orderNo", "orderId", "order_id"))
        if not out_order_no and not provider_order_no:
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_ORDER_ID_MISSING"}
        mapping = self._lookup(out_order_no, provider_order_no)
        if mapping is None:
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_MAPPING_MISSING"}
        mapped_provider = _text(mapping.get("provider_order_no"))
        if provider_order_no and mapped_provider and provider_order_no != mapped_provider:
            await self._hold(mapping, "liangpiao_callback_order_mapping_mismatch")
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_MAPPING_MISMATCH"}
        expected_payload = mapping.get("payload") if isinstance(mapping.get("payload"), Mapping) else {}
        callback_amount = _pick(body, "buyerAmountFen", "buyer_amount_fen", "amountFen", "amount_fen", "amount")
        if callback_amount is not None:
            try:
                amount_matches = int(callback_amount) == int(expected_payload.get("maxPrice"))
            except (TypeError, ValueError):
                amount_matches = False
            if expected_payload.get("maxPrice") is not None and not amount_matches:
                await self._hold(mapping, "liangpiao_callback_amount_mismatch")
                return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_AMOUNT_MISMATCH"}
        callback_seats = _pick(body, "seats", "selectedSeats", "selected_seats")
        expected_seats = expected_payload.get("seats") if isinstance(expected_payload, Mapping) else None
        if isinstance(callback_seats, list) and isinstance(expected_seats, list) and callback_seats != expected_seats:
            await self._hold(mapping, "liangpiao_callback_seats_mismatch")
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_SEATS_MISMATCH"}
        status = _normalize_status(_pick(body, "status", "orderStatus", "order_status", "event"))
        if status is None:
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_STATUS_MISSING"}
        previous_status = str(mapping.get("provider_status") or "created").lower()
        if self._ORDER.get(status, 0) < self._ORDER.get(previous_status, 0):
            return {"status": "ok", "code": "LIANGPIAO_CALLBACK_STALE", "ignored": True}
        if status in {"ticket_sent", "completed"} and not _has_ticket_evidence(body):
            detail = await self._order_detail(out_order_no, provider_order_no)
            if detail:
                body = {**dict(body), **dict(detail)}
            if not _has_ticket_evidence(body):
                await self._hold(mapping, "liangpiao_ticket_evidence_missing")
                return {"status": "manual_hold", "code": "LIANGPIAO_TICKET_EVIDENCE_MISSING"}
        target = {"created": "ORDER_BOUND", "pending": "ORDER_BOUND", "paid": "PAID_WAITING_FULFILLMENT",
                  "processing": "FULFILLMENT_IN_PROGRESS", "shipped": "FULFILLMENT_IN_PROGRESS",
                  "ticket_sent": "TICKET_SENT", "completed": "COMPLETED", "cancelled": "CANCELLED"}[status]
        await self._transition(mapping, target, status, body)
        response = {"status": "ok", "code": "LIANGPIAO_CALLBACK_APPLIED", "state_after": target,
                    "out_order_no": out_order_no, "provider_order_no": provider_order_no}
        if target == "TICKET_SENT":
            response["reply_plan"] = ReplyPlan(
                template_key="flow.fulfillment.ticket_sent", template_version=1,
                variables={}, protected_facts={
                    "out_order_no": out_order_no, "provider_order_no": provider_order_no,
                    "ticket_evidence_hash": hashlib.sha256(json.dumps(dict(body), sort_keys=True).encode()).hexdigest(),
                }, required_phrases=[], optional_ai_text=None,
                send_policy="once_per_state_revision", gate_evidence={},
            ).model_dump(mode="json")
        return response

    def _lookup(self, out_order_no: str | None, provider_order_no: str | None) -> Mapping[str, Any] | None:
        if isinstance(self._mapping, Mapping):
            for key in (out_order_no, provider_order_no):
                if key and isinstance(self._mapping.get(key), Mapping):
                    return self._mapping[key]
        getter = getattr(self._mapping, "find_liangpiao_order", None)
        if callable(getter):
            value = getter(out_order_no=out_order_no, provider_order_no=provider_order_no)
            return value if isinstance(value, Mapping) else None
        return None

    async def _order_detail(self, out_order_no: str | None, provider_order_no: str | None) -> Mapping[str, Any] | None:
        if self._client is None:
            return None
        try:
            value = await self._client.order_detail(outOrderNo=out_order_no, providerOrderNo=provider_order_no)
        except Exception:
            return None
        return value if isinstance(value, Mapping) else None

    async def _hold(self, mapping: Mapping[str, Any], reason: str) -> None:
        await self._transition(mapping, "MANUAL_HOLD", reason, {})

    async def _transition(self, mapping: Mapping[str, Any], target: str, code: str, body: Mapping[str, Any]) -> None:
        transition = getattr(self._states, "transition", None)
        if not callable(transition):
            return
        identity = {name: mapping.get(name) for name in ("tenant_id", "shop_id", "buyer_id", "chat_id")}
        if not all(str(value or "").strip() for value in identity.values()):
            return
        current = getattr(self._states, "get", lambda **_: None)(**identity)
        if current is None:
            return
        try:
            transition(**identity, expected_revision=int(getattr(current, "revision", 0)),
                        event_id=str(_pick(body, "eventId", "event_id") or "liangpiao-callback:" + str(mapping.get("out_order_no") or "unknown")),
                        transition_code=code, flow_state=target, updates={
                            "order_status": "shipped" if target in {"TICKET_SENT", "COMPLETED"} else "bound",
                            "fulfillment_status": "ticket_issued" if target in {"TICKET_SENT", "COMPLETED"} else "pending",
                        })
        except Exception as error:
            raise CallbackError("LIANGPIAO_CALLBACK_STATE_CONFLICT", "回调状态冲突，已转人工核对。") from error


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _pick(source: Mapping[str, Any], *names: str) -> object:
    for name in names:
        if source.get(name) is not None:
            return source.get(name)
    return None


def _normalize_status(value: object) -> str | None:
    text = str(value or "").strip().lower()
    aliases = {"order.paid": "paid", "payment_success": "paid", "40": "paid", "ticket_sent": "ticket_sent",
               "出票成功": "ticket_sent", "issued": "ticket_sent", "3": "shipped", "4": "completed"}
    return aliases.get(text, text if text in LiangpiaoCallbackHandler._ORDER else None)


def _has_ticket_evidence(body: Mapping[str, Any]) -> bool:
    for key in ("ticketCode", "ticket_code", "ticketUrl", "ticket_url", "voucher", "ticketNo", "ticket_no"):
        if _text(body.get(key)):
            return True
    return str(_pick(body, "status", "orderStatus") or "").lower() in {"ticket_sent", "completed"} and bool(body.get("ticket"))
