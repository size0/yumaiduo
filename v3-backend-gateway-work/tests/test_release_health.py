from __future__ import annotations

from app.release_health import (
    EXPECTED_AGENT_RUNTIME_VERSION,
    EXPECTED_V3_RUNTIME_CONTRACT,
    validate_runtime_health,
)


def identity_pair() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "status": "ok", "runtime_contract": EXPECTED_V3_RUNTIME_CONTRACT,
            "runtime_version": EXPECTED_AGENT_RUNTIME_VERSION, "source_commit": "a" * 40,
            "manifest_sha256": "b" * 64, "artifact_sha256": "c" * 64, "release_generation": 7,
        },
        {
            "ok": True, "registered": True,
            "application": {
                "ok": True, "agent_runtime_version": EXPECTED_AGENT_RUNTIME_VERSION, "source_commit": "a" * 40,
                "manifest_sha256": "b" * 64, "artifact_sha256": "d" * 64, "release_generation": 7,
            },
        },
    )


def test_release_health_accepts_only_the_exact_runtime_contract_pair() -> None:
    v3, plugin = identity_pair()
    report = validate_runtime_health(v3, plugin)
    assert report == {
        "ready": True,
        "code": "ready",
        "v3_contract_match": True,
        "plugin_contract_match": True,
        "plugin_registered": True,
        "release_identity_match": True,
    }


def test_release_health_fails_closed_for_stale_or_unhealthy_services() -> None:
    valid_v3, valid_plugin = identity_pair()
    stale_v3 = validate_runtime_health({**valid_v3, "runtime_contract": "old"}, valid_plugin)
    assert stale_v3["ready"] is False
    assert stale_v3["code"] == "v3_runtime_contract_mismatch"

    stale_plugin = validate_runtime_health(valid_v3, {**valid_plugin, "application": {**valid_plugin["application"], "agent_runtime_version": "old"}})
    assert stale_plugin["code"] == "plugin_runtime_contract_mismatch"

    unregistered = validate_runtime_health(valid_v3, {**valid_plugin, "registered": False})
    assert unregistered["code"] == "plugin_not_registered"

    mismatched_generation = validate_runtime_health(valid_v3, {**valid_plugin, "application": {**valid_plugin["application"], "release_generation": 8}})
    assert mismatched_generation["code"] == "release_identity_mismatch"


def test_release_health_report_never_copies_service_payload_fields() -> None:
    v3, plugin = identity_pair()
    report = validate_runtime_health({**v3, "secret": "v3-secret"}, {**plugin, "token": "plugin-secret"})
    assert "v3-secret" not in str(report)
    assert "plugin-secret" not in str(report)
