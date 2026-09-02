"""Final reply safety gate for unsupported commerce claims and secrets."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ReplyValidation:
    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    text: str = ""


class ReplyValidator:
    _SECRET = re.compile(r"(?i)(bearer\s+|sk-[a-z0-9]|csrf|cookie|access[_ -]?token)")
    _UNPROVEN = re.compile(r"(已出票|出票成功|已支付|支付成功|退款成功|已退款|订单已完成|已锁定座位)")
    _PRICE = re.compile(r"(?:¥|￥|\d+(?:\.\d+)?)\s*元")
    _INVENTORY = re.compile(r"(有票|余票|库存充足|座位可用|可选座位)")
    _SEAT_SELECTION = re.compile(r"(已选|已锁定|选中|确认了).{0,12}座位")
    _ORDER_STATUS = re.compile(r"订单(?:状态|目前|当前|已)")

    def validate(self, text: str | None, *, authoritative_facts: Mapping[str, Any] | None = None, pending_tool_calls: int = 0, tool_errors: int = 0) -> ReplyValidation:
        value = str(text or "").strip()
        reasons: list[str] = []
        if not value:
            reasons.append("empty_reply")
        if pending_tool_calls:
            reasons.append("pending_tool_calls")
        if tool_errors:
            reasons.append("unexplained_tool_error")
        if self._SECRET.search(value):
            reasons.append("sensitive_data")
        facts = authoritative_facts or {}
        if self._UNPROVEN.search(value) and not self._supports_claim(value, facts):
            reasons.append("unproven_transaction_claim")
        if self._PRICE.search(value) and not self._has_quote_fact(facts):
            reasons.append("unproven_price_claim")
        if self._INVENTORY.search(value) and not self._has_inventory_fact(facts):
            reasons.append("unproven_inventory_claim")
        if self._SEAT_SELECTION.search(value) and not self._has_seat_fact(facts):
            reasons.append("unproven_seat_claim")
        if self._ORDER_STATUS.search(value) and not self._has_order_status_fact(facts):
            reasons.append("unproven_order_status_claim")
        return ReplyValidation(not reasons, tuple(reasons), value)

    @staticmethod
    def _supports_claim(text: str, facts: Mapping[str, Any]) -> bool:
        status = str(facts.get("order_status") or facts.get("status") or "").lower()
        order = facts.get("order")
        if isinstance(order, Mapping):
            status = status or str(order.get("status") or order.get("order_status") or "").lower()
        if "出票" in text:
            return status in {"ticketed", "fulfilled", "出票成功", "已出票"}
        if "支付" in text:
            return status in {"paid", "ticketed", "fulfilled", "已支付", "支付成功"}
        if "退款" in text:
            return status in {"refunded", "退款成功", "已退款"}
        return bool(facts.get("transaction_confirmed"))

    @staticmethod
    def _has_quote_fact(facts: Mapping[str, Any]) -> bool:
        for key in ("current_quote", "quote", "official_quote"):
            value = facts.get(key)
            if isinstance(value, Mapping) and any(
                value.get(field) is not None
                for field in ("unit_quote_cents", "total_quote_cents", "buyer_amount_fen", "amount_cents")
            ):
                return True
        targets = facts.get("image_quote_targets")
        if isinstance(targets, list):
            for target in targets:
                if not isinstance(target, Mapping):
                    continue
                quote = target.get("quote") or target.get("official_quote")
                if isinstance(quote, Mapping) and any(
                    quote.get(field) is not None
                    for field in ("unit_quote_cents", "total_quote_cents", "buyer_amount_fen", "amount_cents")
                ):
                    return True
        return bool(facts.get("_agent_authoritative_quote_verified")) or any(
            facts.get(key) is not None for key in ("unit_quote_cents", "total_quote_cents", "price_verified")
        )

    @staticmethod
    def _has_inventory_fact(facts: Mapping[str, Any]) -> bool:
        return bool(facts.get("inventory_verified") or facts.get("available_seats") or facts.get("seat_inventory"))

    @staticmethod
    def _has_seat_fact(facts: Mapping[str, Any]) -> bool:
        confirmed = facts.get("confirmed_facts")
        if isinstance(confirmed, Mapping) and confirmed.get("seats"):
            return True
        return bool(facts.get("seats") or facts.get("selected_seats") or facts.get("seat_verified"))

    @staticmethod
    def _has_order_status_fact(facts: Mapping[str, Any]) -> bool:
        return bool(
            facts.get("_agent_order_status_verified")
            or facts.get("order_status")
            or facts.get("status")
            or facts.get("order")
        )
