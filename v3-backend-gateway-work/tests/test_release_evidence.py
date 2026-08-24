from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.release_evidence import ReleaseEvidenceService, ReleaseEvidenceStore


def evaluation() -> dict[str, object]:
    return {
        "consecutive_passes": 3, "failure_replay_count": 100, "image_sample_count": 100,
        "tool_selection_accuracy": 95, "recoverable_success_rate": 95, "unrecoverable_safe_handoff_rate": 100,
        "image_full_path_rate": 95, "completion_rate": 95, "identity_accuracy": 95, "schema_valid_rate": 100,
        "precondition_replan_rate": 5, "p95_latency_ms": 60_000,
        "high_risk_actions": 0, "false_transaction_facts": 0, "duplicate_writes": 0,
        "unknown_result_retries": 0, "cross_tenant_access": 0, "authoritative_inconsistencies": 0,
        "unsafe_final_replies": 0, "post_deadline_effects": 0,
    }


def record() -> dict[str, object]:
    return {
        "evidence_id": "evidence-v37-final", "release_id": "release-v37-final",
        "runtime_version": "wanda-agent-runtime-v37-model-led-native-tools",
        "source_commit": "a" * 40, "manifest_sha256": "b" * 64,
        "plugin_artifact_sha256": "c" * 64, "v3_artifact_sha256": "d" * 64,
        "evaluation_report_sha256": "e" * 64, "evaluation": evaluation(),
        "base_generation": 3, "target_generation": 4, "rollback_verified": True,
        "v3_working_directory": "/opt/wanda-v3-backend/releases/final",
        "plugin_working_directory": "/opt/wanda-preview-plugin/releases/final",
        "native_complete": True, "plugin_worker_count": 5, "outbox_ready": True,
    }


def test_evidence_is_immutable_and_requires_three_independent_eval_passes(tmp_path: Path) -> None:
    store = ReleaseEvidenceStore(tmp_path / "evidence.json")
    issued = store.issue(record())
    assert len(issued["evidence_sha256"]) == 64
    changed = record(); changed["source_commit"] = "f" * 40
    with pytest.raises(ValueError, match="immutable"):
        store.issue(changed)
    invalid = record(); invalid["evidence_id"] = "evidence-v37-other"; invalid["evaluation"] = {**evaluation(), "consecutive_passes": 2}
    with pytest.raises(ValueError, match="thresholds"):
        store.issue(invalid)


def test_activation_rechecks_plugin_identity_and_fresh_guard(tmp_path: Path) -> None:
    store = ReleaseEvidenceStore(tmp_path / "evidence.json"); issued = store.issue(record())
    now = datetime(2026, 8, 24, tzinfo=UTC)
    store.record_guard_check({
        "ok": True, "checked_at": now.isoformat(), "source_commit": "a" * 40, "manifest_sha256": "b" * 64,
        "plugin_artifact_sha256": "c" * 64, "v3_artifact_sha256": "d" * 64, "release_generation": 3,
    })
    plugin = {"ok": True, "registered": True, "application": {
        "agent_runtime_version": issued["runtime_version"], "source_commit": "a" * 40, "manifest_sha256": "b" * 64,
        "artifact_sha256": "c" * 64, "release_generation": 3,
        "native_complete": True, "agent_outbox": {},
        "worker": "running", "agent_worker": "running", "agent_outbox_worker": "running",
        "historical_evaluation_worker": "running", "human_comparison_worker": "running",
    }}
    service = ReleaseEvidenceService(
        store, identity={"runtime_version": issued["runtime_version"], "source_commit": "a" * 40, "manifest_sha256": "b" * 64, "artifact_sha256": "d" * 64},
        fetch_health=lambda _url: plugin,
        working_directory_for=lambda service: "/opt/wanda-v3-backend/releases/final" if service == "wanda-v3-backend.service" else "/opt/wanda-preview-plugin/releases/final",
        now=lambda: now,
    )
    assert service.validate_for_activation(issued["evidence_id"], current_generation=3)["release_id"] == "release-v37-final"
    stale = ReleaseEvidenceService(
        store, identity=service.identity, fetch_health=lambda _url: plugin,
        working_directory_for=service.working_directory_for, now=lambda: now + timedelta(seconds=61),
    )
    with pytest.raises(ValueError, match="guard"):
        stale.validate_for_activation(issued["evidence_id"], current_generation=3)
