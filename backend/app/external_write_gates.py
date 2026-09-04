"""Independent production gates for external actions.

``EXTERNAL_WRITES_ENABLED`` remains the process-wide safety fuse.  The action
flags are narrower permits and can never open writes when the global fuse is
closed.  Keeping this mapping in one small module makes the backend claim
boundary auditable and prevents a newly added executor from accidentally
sharing the message canary permit.
"""
from __future__ import annotations

from typing import Protocol


class WriteGateSettings(Protocol):
    external_writes_enabled: bool
    message_send_enabled: bool
    xianyu_reprice_enabled: bool
    liangpiao_order_create_enabled: bool
    wanda_provider_writes_enabled: bool
    refund_enabled: bool
    ship_enabled: bool


MESSAGE_ACTIONS = frozenset({
    "send_message", "send_price_change_confirmation", "send_image", "guard_unverified_order",
})
REPRICE_ACTIONS = frozenset({"change_order_price", "switch_fixed", "quote.switch_fixed"})
LIANGPIAO_ORDER_ACTIONS = frozenset({"create_liangpiao_order"})
REFUND_ACTIONS = frozenset({
    "cancel_paid_amount_mismatch", "cancel_failed_liangpiao_source_order", "cancel_order", "order.cancel",
    "refund_or_intercept",
})
SHIP_ACTIONS = frozenset({"submit_fulfillment", "send_ticket"})
# This gate is reserved for Wanda/provider-side writes (currently the
# fulfillment path). Xianyu reprice, Liangpiao order creation and refunds have
# their own permits and must not be coupled to it.
PROVIDER_WRITE_ACTIONS = frozenset(SHIP_ACTIONS)


def _enabled(settings: object, name: str, default: bool = False) -> bool:
    # Settings loaded from production always has these fields.  The permissive
    # fallback keeps small injected test configs backwards compatible; omitted
    # fields are not a production configuration.
    return bool(getattr(settings, name, default))


def action_gate_reason(settings: object, action_type: str) -> str | None:
    """Return a stable deny reason, or ``None`` when the action is permitted."""
    normalized = str(action_type or "").strip()
    if not _enabled(settings, "external_writes_enabled"):
        return "external_writes_disabled"
    if normalized in MESSAGE_ACTIONS and not _enabled(settings, "message_send_enabled"):
        return "message_send_disabled"
    if normalized in REPRICE_ACTIONS and not _enabled(settings, "xianyu_reprice_enabled"):
        return "xianyu_reprice_disabled"
    if normalized in LIANGPIAO_ORDER_ACTIONS and not _enabled(settings, "liangpiao_order_create_enabled"):
        return "liangpiao_order_create_disabled"
    if normalized in REFUND_ACTIONS and not _enabled(settings, "refund_enabled"):
        return "refund_disabled"
    if normalized in SHIP_ACTIONS and not _enabled(settings, "ship_enabled"):
        return "ship_disabled"
    if normalized in PROVIDER_WRITE_ACTIONS and not _enabled(settings, "wanda_provider_writes_enabled"):
        return "wanda_provider_writes_disabled"
    return None


def allowed_command_types(settings: object) -> frozenset[str]:
    """List command types safe to lease under the current action permits."""
    candidates = MESSAGE_ACTIONS | REPRICE_ACTIONS | LIANGPIAO_ORDER_ACTIONS | REFUND_ACTIONS | SHIP_ACTIONS
    return frozenset(item for item in candidates if action_gate_reason(settings, item) is None)


def settings_gate_snapshot(settings: object) -> dict[str, bool]:
    """Safe, non-secret gate state for deployment diagnostics."""
    names = (
        "external_writes_enabled", "message_send_enabled", "xianyu_reprice_enabled",
        "liangpiao_order_create_enabled", "wanda_provider_writes_enabled", "refund_enabled", "ship_enabled",
    )
    return {name: _enabled(settings, name) for name in names}
