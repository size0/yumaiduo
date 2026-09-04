from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import MutableMapping
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .rule_contracts import GateEvidence, ReplyPlan


class CallbackError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CallbackVerifier:
    """Verify provider callback signatures and reject replayed nonces."""

    def __init__(self, secret: str, *, app_key: str = "", max_skew_seconds: int = 300,
                 replay_cache: MutableMapping[str, float] | None = None) -> None:
        if not str(secret or "").strip():
            raise ValueError("liangpiao_callback_secret_required")
        self._secret = str(secret).encode("utf-8")
        self._app_key = str(app_key or "").strip()
        self._max_skew = max(30, int(max_skew_seconds))
        self._replay = replay_cache if replay_cache is not None else {}

    def sign(self, raw_body: bytes, timestamp: str, nonce: str) -> str:
        material = f"{self._app_key}{timestamp}{nonce}".encode("utf-8") + raw_body
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

    _ORDER = {
        "created": 1, "pending": 1, "paid": 2, "submitting": 3, "ticketing": 4,
        "processing": 4, "shipped": 4, "ticket_sent": 5, "completed": 6, "settled": 6,
        "failed": 99, "cancelled": 99, "refunded": 99, "refund_rejected": 99,
    }

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
        durable_replay = getattr(self._mapping, "has_liangpiao_callback_nonce", None)
        if callable(durable_replay) and durable_replay(nonce):
            raise CallbackError("LIANGPIAO_CALLBACK_REPLAY", "重复回调已忽略。")
        self._verifier.verify(raw, signature=signature, timestamp=timestamp, nonce=nonce)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CallbackError("LIANGPIAO_CALLBACK_JSON_INVALID", "回调内容不是有效 JSON。") from error
        if not isinstance(body, Mapping):
            raise CallbackError("LIANGPIAO_CALLBACK_JSON_INVALID", "回调内容不是对象。")
        event_data = body.get("data") if isinstance(body.get("data"), Mapping) else body
        event_data = {**dict(event_data), "event": body.get("event", event_data.get("event"))}
        out_order_no = _text(_pick(event_data, "outOrderNo", "out_order_no"))
        provider_order_no = _text(_pick(event_data, "providerOrderNo", "provider_order_no", "orderNo", "orderId", "order_id"))
        if not out_order_no and not provider_order_no:
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_ORDER_ID_MISSING"}
        mapping = self._lookup(out_order_no, provider_order_no)
        if mapping is None:
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_MAPPING_MISSING"}
        binding_status = self._binding_status(
            mapping, event_data, out_order_no=out_order_no,
            provider_order_no=provider_order_no,
        )
        if binding_status == "stale":
            return {
                "status": "ok", "code": "LIANGPIAO_CALLBACK_STALE_GENERATION",
                "ignored": True,
            }
        if binding_status == "mismatch":
            await self._hold(mapping, "liangpiao_callback_current_binding_mismatch")
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_MAPPING_MISMATCH"}
        mapped_out = _text(mapping.get("out_order_no"))
        if out_order_no and mapped_out and out_order_no != mapped_out:
            await self._hold(mapping, "liangpiao_callback_order_mapping_mismatch")
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_MAPPING_MISMATCH"}
        mapped_provider = _text(mapping.get("provider_order_no"))
        if provider_order_no and mapped_provider and provider_order_no != mapped_provider:
            await self._hold(mapping, "liangpiao_callback_order_mapping_mismatch")
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_MAPPING_MISMATCH"}
        expected_payload = mapping.get("payload") if isinstance(mapping.get("payload"), Mapping) else {}
        callback_amount = _pick(event_data, "buyerAmountFen", "buyer_amount_fen", "amountFen", "amount_fen", "amount")
        if callback_amount is not None:
            try:
                amount_matches = int(callback_amount) == int(expected_payload.get("maxPrice"))
            except (TypeError, ValueError):
                amount_matches = False
            if expected_payload.get("maxPrice") is not None and not amount_matches:
                await self._hold(mapping, "liangpiao_callback_amount_mismatch")
                return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_AMOUNT_MISMATCH"}
        callback_seats = _pick(event_data, "seats", "selectedSeats", "selected_seats")
        expected_seats = expected_payload.get("seats") if isinstance(expected_payload, Mapping) else None
        if isinstance(callback_seats, list) and isinstance(expected_seats, list) and callback_seats != expected_seats:
            await self._hold(mapping, "liangpiao_callback_seats_mismatch")
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_SEATS_MISMATCH"}
        status = _normalize_status(_pick(event_data, "status", "orderStatus", "order_status", "event"))
        if status is None:
            return {"status": "manual_hold", "code": "LIANGPIAO_CALLBACK_STATUS_MISSING"}
        previous_status = str(mapping.get("provider_status") or "created").lower()
        if self._ORDER.get(status, 0) < self._ORDER.get(previous_status, 0):
            return {"status": "ok", "code": "LIANGPIAO_CALLBACK_STALE", "ignored": True}
        if status in {"ticket_sent", "completed", "settled"} and not _has_ticket_evidence(event_data):
            detail = await self._order_detail(out_order_no, provider_order_no)
            if detail:
                event_data = {**event_data, **dict(detail)}
            if not _has_ticket_evidence(event_data):
                await self._hold(mapping, "liangpiao_ticket_evidence_missing")
                return {"status": "manual_hold", "code": "LIANGPIAO_TICKET_EVIDENCE_MISSING"}
        target = {"created": "ORDER_BOUND", "pending": "ORDER_BOUND", "paid": "PAID_WAITING_FULFILLMENT",
                  "submitting": "FULFILLMENT_IN_PROGRESS", "ticketing": "FULFILLMENT_IN_PROGRESS",
                  "processing": "FULFILLMENT_IN_PROGRESS", "shipped": "FULFILLMENT_IN_PROGRESS", "ticket_sent": "TICKET_SENT",
                  "settled": "TICKET_SENT", "completed": "COMPLETED", "failed": "MANUAL_HOLD",
                  "cancelled": "MANUAL_HOLD", "refunded": "REFUND_PENDING",
                  "refund_rejected": "MANUAL_HOLD"}[status]
        update_order = getattr(self._mapping, "update_liangpiao_order", None)
        if callable(update_order) and out_order_no:
            update_order(out_order_no, provider_status=status, snapshot_updates={
                "callback_status": status, "ticket_codes": _ticket_codes(event_data),
                "pickup_url": _text(_pick(event_data, "pickupUrl", "pickup_url")),
                "failure_reason": _text(_pick(event_data, "failReason", "fail_reason", "message")),
            })
        revision = await self._transition(mapping, target, status, event_data)
        if revision < 0:
            return {
                "status": "ok", "code": "LIANGPIAO_CALLBACK_STALE_GENERATION",
                "ignored": True,
            }
        response = {"status": "ok", "code": "LIANGPIAO_CALLBACK_APPLIED", "state_after": target,
                    "state_revision": revision, "out_order_no": out_order_no, "provider_order_no": provider_order_no}
        if status in {"ticket_sent", "completed"}:
            codes = _ticket_codes(event_data)
            response["reply_dedupe_key"] = f"liangpiao:{out_order_no or provider_order_no}:ticketed:{_pick(event_data, 'version') or 0}"
            response["reply_plan"] = ReplyPlan(
                template_key="flow.fulfillment.liangpiao_ticketed", template_version=1,
                variables={"取票码": "、".join(codes), "取票链接": _text(_pick(event_data, "pickupUrl", "pickup_url")) or ""},
                protected_facts={"out_order_no": out_order_no, "provider_order_no": provider_order_no,
                                 "ticket_codes": codes, "ticket_evidence_hash": hashlib.sha256(json.dumps(dict(event_data), sort_keys=True).encode()).hexdigest()},
                required_phrases=[], optional_ai_text=None, send_policy="once_per_state_revision", gate_evidence={
                    "取票码": GateEvidence(source="audited_fulfillment_event", value="、".join(codes)).model_dump(),
                    "取票链接": GateEvidence(source="audited_fulfillment_event", value=_text(_pick(event_data, "pickupUrl", "pickup_url")) or "").model_dump(),
                },
            ).model_dump(mode="json")
        elif status in {"failed", "cancelled", "refund_rejected"}:
            response["reply_dedupe_key"] = f"liangpiao:{out_order_no or provider_order_no}:{status}"
            # A provider FAILED order is terminal and its frozen amount is
            # already released by Liangpiao.  The callback path must never
            # enqueue a refund API call.  We only expose a guarded offer for
            # LIMIT orders; the buyer still has to explicitly agree before a
            # FIXED preflight/create flow can be started.
            price_mode = _price_mode(mapping, expected_payload, event_data)
            fallback_available = bool(
                status == "failed"
                and price_mode == "LIMIT"
                and mapping.get("allow_fixed_fallback", True) is not False
            )
            reply_template_key = (
                "flow.fulfillment.liangpiao_limit_refund_pending"
                if fallback_available else "flow.fulfillment.liangpiao_fixed_failed"
            )
            refund_amount = _money_value(_pick(event_data, "refundAmount", "refund_amount"))
            loss_amount = _money_value(_pick(event_data, "lossAmount", "loss_amount"))
            response["reply_plan"] = ReplyPlan(
                template_key=reply_template_key, template_version=1,
                variables={"失败原因": _text(_pick(event_data, "failReason", "fail_reason", "message")) or "良票平台未能完成出票"},
                protected_facts={"out_order_no": out_order_no, "provider_order_no": provider_order_no,
                                 "failure_hash": hashlib.sha256(json.dumps(dict(event_data), sort_keys=True).encode()).hexdigest(),
                                 "price_mode": price_mode, "fallback_available": fallback_available,
                                 # Keep amount facts in the protected audit
                                 # payload; never interpolate upstream amounts
                                 # into a buyer-facing failure sentence.
                                 "refund_amount_fen": refund_amount, "loss_amount_fen": loss_amount,
                                 "refund_api_allowed": False},
                required_phrases=[], optional_ai_text=None, send_policy="once_per_state_revision", gate_evidence={
                    "失败原因": GateEvidence(source="audited_fulfillment_event", value=_text(_pick(event_data, "failReason", "fail_reason", "message")) or "良票平台未能完成出票").model_dump(),
                },
            ).model_dump(mode="json")
            response["fallback"] = {
                "available": fallback_available,
                "from_price_mode": price_mode,
                "to_price_mode": "FIXED" if fallback_available else None,
                "requires_buyer_consent": True,
                "refund_api_allowed": False,
                "refund_amount_fen": refund_amount,
                "loss_amount_fen": loss_amount,
            }
            if fallback_available and mapping.get("flow_version") != "V4_LIANGPIAO_FULFILLMENT_V1":
                action = self._platform_source_cancel_action(mapping)
                response["platform_actions"] = [action] if action is not None else []
        return response

    def _platform_source_cancel_action(self, mapping: Mapping[str, Any]) -> dict[str, Any] | None:
        """Authorize cancellation of the bound Xianyu order, never the FAILED provider order."""
        identity = {
            name: str(mapping.get(name) or "").strip()
            for name in ("tenant_id", "shop_id", "buyer_id", "chat_id")
        }
        getter = getattr(self._states, "get", None)
        if not callable(getter) or not all(identity.values()):
            return None
        current = getter(**identity)
        if current is None:
            return None
        order_id = _text(
            getattr(current, "fixed_switch_source_platform_order_id", None)
            or getattr(current, "order_id", None)
        )
        source_out_order_no = _text(
            getattr(current, "fixed_switch_source_order_no", None)
            or mapping.get("out_order_no")
        )
        generation = _positive_int(getattr(current, "generation", None))
        if not order_id or not source_out_order_no or generation is None:
            return None
        action_id = f"liangpiao:{source_out_order_no}:cancel-source-order"
        return {
            "id": action_id,
            "type": "cancel_failed_liangpiao_source_order",
            "order_id": order_id,
            **identity,
            "source_out_order_no": source_out_order_no,
            "source_generation": generation,
            "source_provider_status": "failed",
            "source_price_mode": "LIMIT",
            "callback_verified": True,
            "refund_authorization": "verified_current_limit_failure",
            "dedupe_key": (
                f"liangpiao:{source_out_order_no}:{order_id}:cancel-source-order:g{generation}"
            ),
        }

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

    def _mapping_generation(self, mapping: Mapping[str, Any]) -> int | None:
        payload = mapping.get("payload") if isinstance(mapping.get("payload"), Mapping) else {}
        candidates: list[object] = [
            mapping.get("generation"), mapping.get("quote_generation"),
            payload.get("generation"), payload.get("quoteGeneration"), payload.get("quote_generation"),
        ]
        quote_id = _text(mapping.get("quote_id"))
        quote_getter = getattr(self._mapping, "get_selected_seat_quote", None)
        if quote_id and callable(quote_getter):
            quote = quote_getter(quote_id)
            if isinstance(quote, Mapping):
                candidates.extend((quote.get("generation"), quote.get("quote_generation")))
        for candidate in candidates:
            try:
                generation = int(candidate)
            except (TypeError, ValueError):
                continue
            if generation >= 1:
                return generation
        return None

    def _binding_status(
        self, mapping: Mapping[str, Any], event_data: Mapping[str, Any], *,
        out_order_no: str | None, provider_order_no: str | None,
    ) -> str:
        """Return active, stale, or mismatch for the current transaction binding.

        Historical provider events are valid audit facts for their old order,
        but they must never mutate the active generation or produce a buyer
        reply carrying old ticket codes.
        """
        identity = {
            name: mapping.get(name)
            for name in ("tenant_id", "shop_id", "buyer_id", "chat_id")
        }
        getter = getattr(self._states, "get", None)
        if not callable(getter) or not all(str(value or "").strip() for value in identity.values()):
            return "active"
        current = getter(**identity)
        if current is None:
            return "active"

        current_generation = _positive_int(getattr(current, "generation", None))
        mapping_generation = self._mapping_generation(mapping)
        callback_generation = _positive_int(_pick(
            event_data, "quoteGeneration", "quote_generation",
        ))
        mapped_out = _text(mapping.get("out_order_no"))
        mapped_provider = _text(mapping.get("provider_order_no"))
        callback_mapping_mismatch = bool(
            (out_order_no and mapped_out and out_order_no != mapped_out)
            or (
                provider_order_no and mapped_provider
                and provider_order_no != mapped_provider
            )
        )
        if callback_mapping_mismatch:
            return "mismatch"
        if current_generation is not None and mapping_generation is not None:
            if mapping_generation < current_generation:
                return "stale"
            if mapping_generation > current_generation:
                return "mismatch"
        if callback_generation is not None:
            expected_generation = mapping_generation or current_generation
            if expected_generation is not None and callback_generation != expected_generation:
                return "stale" if callback_generation < expected_generation else "mismatch"

        current_out = _text(getattr(current, "out_order_no", None))
        current_provider = _text(getattr(current, "provider_order_no", None))
        if getattr(current, "flow_state", None) in {"REFUND_PENDING", "REFUNDED", "MANUAL_HOLD", "CANCELLED"}:
            return "stale"
        current_mapping_mismatch = bool(
            (current_out and mapped_out and current_out != mapped_out)
            or (
                current_provider and mapped_provider
                and current_provider != mapped_provider
            )
        )
        current_callback_mismatch = bool(
            (current_out and out_order_no and current_out != out_order_no)
            or (
                current_provider and provider_order_no
                and current_provider != provider_order_no
            )
        )
        if current_mapping_mismatch or current_callback_mismatch:
            # A mapping without generation metadata is legacy data. Distinct
            # current order identifiers still prove that this is an old event;
            # ignoring it is safer than placing the new order on hold.
            if mapping_generation is None or (
                current_generation is not None
                and mapping_generation is not None
                and mapping_generation < current_generation
            ):
                return "stale"
            return "mismatch"
        return "active"

    async def _order_detail(self, out_order_no: str | None, provider_order_no: str | None) -> Mapping[str, Any] | None:
        # Liangpiao's documented detail endpoint accepts only the provider
        # orderNo. outOrderNo is our idempotency/mapping key and must never be
        # substituted into an undocumented request shape.
        if self._client is None or not provider_order_no:
            return None
        try:
            value = await self._client.order_detail(orderNo=provider_order_no)
        except Exception:
            return None
        return value if isinstance(value, Mapping) else None

    async def _hold(self, mapping: Mapping[str, Any], reason: str) -> None:
        await self._transition(mapping, "MANUAL_HOLD", reason, {})

    async def _transition(self, mapping: Mapping[str, Any], target: str, code: str, body: Mapping[str, Any]) -> int:
        transition = getattr(self._states, "transition", None)
        if not callable(transition):
            return int(getattr(self._states, "revision", 0))
        identity = {name: mapping.get(name) for name in ("tenant_id", "shop_id", "buyer_id", "chat_id")}
        if not all(str(value or "").strip() for value in identity.values()):
            return int(getattr(self._states, "revision", 0))
        current = getattr(self._states, "get", lambda **_: None)(**identity)
        if current is None:
            return int(getattr(self._states, "revision", 0))
        mapping_generation = self._mapping_generation(mapping)
        current_generation = _positive_int(getattr(current, "generation", None))
        mapped_out = _text(mapping.get("out_order_no"))
        mapped_provider = _text(mapping.get("provider_order_no"))
        current_out = _text(getattr(current, "out_order_no", None))
        current_provider = _text(getattr(current, "provider_order_no", None))
        if (
            (
                mapping_generation is not None
                and current_generation is not None
                and mapping_generation != current_generation
            )
            or (mapped_out and current_out and mapped_out != current_out)
            or (
                mapped_provider and current_provider
                and mapped_provider != current_provider
            )
        ):
            return -1
        try:
            callback_event_id = _pick(body, "eventId", "event_id") or (
                "liangpiao-callback:" + str(mapping.get("out_order_no") or "unknown") + ":"
                + str(code) + ":" + str(_pick(body, "version") or "0")
            )
            order_status = (
                "failed" if str(code).lower() == "failed" else
                "cancelled" if str(code).lower() == "cancelled" else
                "shipped" if target in {"TICKET_SENT", "COMPLETED"} else
                "paid" if target == "MANUAL_HOLD" else "bound"
            )
            fulfillment_status = (
                "ticket_issued" if target in {"TICKET_SENT", "COMPLETED"} else
                "failed" if str(code).lower() == "failed" else
                "cancelled" if str(code).lower() == "cancelled" else
                "none" if target == "MANUAL_HOLD" else "pending"
            )
            payload = mapping.get("payload") if isinstance(mapping.get("payload"), Mapping) else {}
            price_mode = _price_mode(mapping, payload, body)
            fixed_switch_updates: dict[str, Any] = {}
            if str(code).lower() == "failed":
                fixed_switch_updates = {
                    "fixed_switch_status": "pending" if price_mode == "LIMIT" else "failed",
                    "fixed_switch_expires_at": (
                        datetime.now(timezone.utc) + timedelta(minutes=30)
                    ).isoformat() if price_mode == "LIMIT" else None,
                    "fixed_switch_source_order_no": str(mapping.get("out_order_no") or "").strip() or None,
                    "fixed_switch_source_order_status": (
                        "refund_pending" if price_mode == "LIMIT" else "failed"
                    ),
                    "fixed_switch_source_platform_order_id": (
                        getattr(current, "fixed_switch_source_platform_order_id", None)
                        or getattr(current, "order_id", None)
                    ),
                    "fixed_switch_confirmation_event_id": None,
                    "fixed_switch_quote_id": None,
                    "fixed_switch_quote_hash": None,
                    "fixed_switch_quote_generation": None,
                    "fixed_switch_quote_confirmation_status": "none",
                }
            updated = transition(**identity, expected_revision=int(getattr(current, "revision", 0)),
                        event_id=str(callback_event_id),
                        transition_code=code, flow_state=target, updates={
                            "order_status": (
                                "refund_pending"
                                if (
                                    str(code).lower() == "failed" and price_mode == "LIMIT"
                                ) or str(code).lower() == "refunded"
                                else order_status
                            ),
                            **({"payment_status": "refund_pending"}
                               if (
                                   str(code).lower() == "failed" and price_mode == "LIMIT"
                               ) or str(code).lower() == "refunded" else {}),
                            "fulfillment_status": fulfillment_status,
                            "provider_status": str(code).lower(),
                            **fixed_switch_updates,
                            **({"ticket_codes": _ticket_codes(body), "pickup_url": _text(_pick(body, "pickupUrl", "pickup_url"))} if target in {"TICKET_SENT", "COMPLETED"} else {}),
                        })
            return int(getattr(updated, "revision", getattr(self._states, "revision", 0)))
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
    aliases = {
        "order.paid": "paid", "payment_success": "paid", "40": "paid",
        "created": "created", "order.created": "created", "pending": "pending",
        "submitting": "submitting", "order.submitting": "submitting",
        "ticketing": "ticketing", "processing": "processing", "order.processing": "processing",
        "ticket_sent": "ticket_sent", "ticketed": "ticket_sent", "order.ticketed": "ticket_sent", "ticket.updated": "ticket_sent",
        "settled": "settled", "order.settled": "settled",
        "出票成功": "ticket_sent", "issued": "ticket_sent", "3": "shipped", "4": "completed",
        "order.failed": "failed", "failed": "failed", "出票失败": "failed",
        "cancelled": "cancelled", "canceled": "cancelled",
        "refunded": "refunded", "order.refunded": "refunded",
        "refund_rejected": "refund_rejected", "refund.rejected": "refund_rejected",
    }
    return aliases.get(text, text if text in LiangpiaoCallbackHandler._ORDER else None)


def _price_mode(mapping: Mapping[str, Any], payload: Mapping[str, Any], event_data: Mapping[str, Any]) -> str:
    """Read the immutable mode captured at order creation.

    Callback payloads are not authoritative for pricing mode, so prefer the
    locally persisted create payload and only use callback fields as a legacy
    compatibility fallback.  Unknown values are treated as FIXED (no automatic
    fallback) rather than accidentally opening a second channel.
    """
    candidates = (
        payload.get("priceMode"), payload.get("price_mode"),
        mapping.get("price_mode"), mapping.get("priceMode"),
        event_data.get("priceMode"), event_data.get("price_mode"),
    )
    for value in candidates:
        mode = str(value or "").strip().upper()
        if mode in {"LIMIT", "FIXED"}:
            return mode
    return "FIXED"


def _money_value(value: object) -> int | None:
    """Normalize provider money fields to fen for protected audit data."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return amount if amount >= 0 else None


def _positive_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _ticket_codes(body: Mapping[str, Any]) -> list[str]:
    values = body.get("tickets")
    codes = []
    if isinstance(values, list):
        codes.extend(_text(item.get("ticketCode") or item.get("ticket_code")) for item in values if isinstance(item, Mapping))
    for key in ("ticketCode", "ticket_code", "ticketNo", "ticket_no", "pickupCode", "pickup_code"):
        value = _text(body.get(key))
        if value:
            codes.append(value)
    return list(dict.fromkeys(code for code in codes if code))


def _has_ticket_evidence(body: Mapping[str, Any]) -> bool:
    return bool(_ticket_codes(body)) or any(
        _text(body.get(key)) for key in (
            "ticketUrl", "ticket_url", "voucher", "ticketNo", "ticket_no", "ticketLink",
            "pickupCode", "pickup_code", "qrUrl", "qr_url", "pickupUrl", "pickup_url",
        )
    )
