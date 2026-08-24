from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.release_evidence import ReleaseEvidenceStore
from app.release_guard import evaluate_release_guard, record_probe_failure, run_release_guard


def test_release_guard_enforces_zero_tolerance_and_window_thresholds() -> None:
    base = {"services_active": True, "restart_delta": 0, "health_match": True, "metrics": {}}
    assert evaluate_release_guard(base) == (True, "ready")
    assert evaluate_release_guard({**base, "metrics": {"cross_tenant_access": 1}}) == (False, "zero_tolerance_incident")
    assert evaluate_release_guard({**base, "metrics": {"completed_turns": 20, "hard_failures": 2}}) == (False, "hard_failure_rate_threshold")
    assert evaluate_release_guard({**base, "metrics": {"completed_turns": 20, "p95_latency_ms": 60_001}}) == (False, "latency_threshold")
    assert evaluate_release_guard({**base, "metrics": {"consecutive_gateway_failures": 3}}) == (False, "gateway_failure_threshold")


def test_three_consecutive_guard_probe_failures_trigger_atomic_rollback(tmp_path: Path) -> None:
    store = ReleaseEvidenceStore(tmp_path / "probe-evidence.json")
    rollback_calls: list[bool] = []
    for _ in range(2):
        assert record_probe_failure(store, lambda: rollback_calls.append(True))["rollback_triggered"] is False
    assert record_probe_failure(store, lambda: rollback_calls.append(True))["rollback_triggered"] is True
    assert rollback_calls == [True]


def test_active_guard_failure_records_evidence_and_calls_atomic_rollback(tmp_path: Path) -> None:
    store = ReleaseEvidenceStore(tmp_path / "evidence.json")
    rollback_calls: list[bool] = []
    v3 = {
        "status": "ok", "runtime_version": "runtime-v37", "source_commit": "a" * 40,
        "manifest_sha256": "b" * 64, "artifact_sha256": "c" * 64, "release_generation": 4,
    }
    plugin = {
        "ok": True, "registered": True,
        "application": {
            "agent_runtime_version": "runtime-v37", "source_commit": "a" * 40,
            "manifest_sha256": "b" * 64, "artifact_sha256": "d" * 64,
            "release_generation": 4, "conversation_agent_mode": "active", "release_evidence_id": "evidence-active",
            "release_guard_metrics": {"cross_tenant_access": 1},
        },
    }
    store.get = lambda _evidence_id: {
        "source_commit": "a" * 40, "manifest_sha256": "b" * 64,
        "v3_artifact_sha256": "c" * 64, "plugin_artifact_sha256": "d" * 64,
        "v3_working_directory": "/v3", "plugin_working_directory": "/plugin",
    }
    result = run_release_guard(
        evidence_store=store, v3_health=v3, plugin_health=plugin,
        v3_service={"active": True, "nrestarts": 0, "working_directory": "/v3"}, plugin_service={"active": True, "nrestarts": 0, "working_directory": "/plugin"},
        rollback=lambda: rollback_calls.append(True), now=datetime(2026, 8, 24, tzinfo=UTC),
    )
    assert result["code"] == "zero_tolerance_incident"
    assert result["rollback_triggered"] is True
    assert rollback_calls == [True]
    assert store.latest_guard_check()["release_generation"] == 4
