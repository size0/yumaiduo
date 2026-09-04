from types import SimpleNamespace

from app.external_write_gates import action_gate_reason, allowed_command_types, settings_gate_snapshot


def settings(**overrides):
    values = {
        "external_writes_enabled": True,
        "message_send_enabled": False,
        "xianyu_reprice_enabled": False,
        "liangpiao_order_create_enabled": False,
        "wanda_provider_writes_enabled": False,
        "refund_enabled": False,
        "ship_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_global_fuse_overrides_every_action_permit():
    value = settings(
        external_writes_enabled=False,
        message_send_enabled=True,
        xianyu_reprice_enabled=True,
        liangpiao_order_create_enabled=True,
        wanda_provider_writes_enabled=True,
        refund_enabled=True,
        ship_enabled=True,
    )
    assert action_gate_reason(value, "send_message") == "external_writes_disabled"
    assert action_gate_reason(value, "change_order_price") == "external_writes_disabled"
    assert allowed_command_types(value) == frozenset()


def test_message_canary_does_not_open_transaction_writes():
    value = settings(message_send_enabled=True)
    assert action_gate_reason(value, "send_message") is None
    assert action_gate_reason(value, "send_price_change_confirmation") is None
    assert action_gate_reason(value, "change_order_price") == "xianyu_reprice_disabled"
    assert action_gate_reason(value, "create_liangpiao_order") == "liangpiao_order_create_disabled"
    assert action_gate_reason(value, "submit_fulfillment") == "ship_disabled"
    assert allowed_command_types(value) == frozenset({
        "send_message", "send_price_change_confirmation", "send_image", "guard_unverified_order",
    })


def test_gate_snapshot_contains_only_boolean_gate_state():
    value = settings(message_send_enabled=True)
    assert settings_gate_snapshot(value) == {
        "external_writes_enabled": True,
        "message_send_enabled": True,
        "xianyu_reprice_enabled": False,
        "liangpiao_order_create_enabled": False,
        "wanda_provider_writes_enabled": False,
        "refund_enabled": False,
        "ship_enabled": False,
    }
