from __future__ import annotations

from app.release_health import (
    EXPECTED_AGENT_RUNTIME_VERSION,
    EXPECTED_V3_RUNTIME_CONTRACT,
    validate_runtime_health,
)


def test_release_health_accepts_only_the_exact_runtime_contract_pair() -> None:
    report = validate_runtime_health(
        {"status": "ok", "runtime_contract": EXPECTED_V3_RUNTIME_CONTRACT},
        {
            "ok": True,
            "registered": True,
            "application": {"ok": True, "agent_runtime_version": EXPECTED_AGENT_RUNTIME_VERSION},
        },
    )
    assert report == {
        "ready": True,
        "code": "ready",
        "v3_contract_match": True,
        "plugin_contract_match": True,
        "plugin_registered": True,
    }


def test_release_health_fails_closed_for_stale_or_unhealthy_services() -> None:
    stale_v3 = validate_runtime_health(
        {"status": "ok", "runtime_contract": "old"},
        {"ok": True, "registered": True, "application": {"ok": True, "agent_runtime_version": EXPECTED_AGENT_RUNTIME_VERSION}},
    )
    assert stale_v3["ready"] is False
    assert stale_v3["code"] == "v3_runtime_contract_mismatch"

    stale_plugin = validate_runtime_health(
        {"status": "ok", "runtime_contract": EXPECTED_V3_RUNTIME_CONTRACT},
        {"ok": True, "registered": True, "application": {"ok": True, "agent_runtime_version": "old"}},
    )
    assert stale_plugin["code"] == "plugin_runtime_contract_mismatch"

    unregistered = validate_runtime_health(
        {"status": "ok", "runtime_contract": EXPECTED_V3_RUNTIME_CONTRACT},
        {"ok": True, "registered": False, "application": {"ok": True, "agent_runtime_version": EXPECTED_AGENT_RUNTIME_VERSION}},
    )
    assert unregistered["code"] == "plugin_not_registered"


def test_release_health_report_never_copies_service_payload_fields() -> None:
    report = validate_runtime_health(
        {"status": "ok", "runtime_contract": EXPECTED_V3_RUNTIME_CONTRACT, "secret": "v3-secret"},
        {"ok": True, "registered": True, "token": "plugin-secret", "application": {"ok": True, "agent_runtime_version": EXPECTED_AGENT_RUNTIME_VERSION}},
    )
    assert "v3-secret" not in str(report)
    assert "plugin-secret" not in str(report)
