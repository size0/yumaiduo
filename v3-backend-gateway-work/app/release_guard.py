"""Thirty-second release guard decision logic and systemd-friendly one-shot runner."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .release_evidence import ReleaseEvidenceStore
from .release_health import fetch_health_json

ZERO_METRICS = (
    "high_risk_actions", "false_transaction_facts", "duplicate_writes", "unknown_result_retries",
    "cross_tenant_access", "authoritative_inconsistencies", "unsafe_final_replies", "post_deadline_effects",
)


def evaluate_release_guard(snapshot: dict[str, Any]) -> tuple[bool, str]:
    if snapshot.get("services_active") is not True:
        return False, "service_inactive"
    if int(snapshot.get("restart_delta", 0)) > 0:
        return False, "service_restarted"
    if int(snapshot.get("warning_count", 0)) > 0:
        return False, "service_warning_detected"
    if snapshot.get("health_match") is not True:
        return False, "release_identity_mismatch"
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    if any(int(metrics.get(field, 0) or 0) != 0 for field in ZERO_METRICS):
        return False, "zero_tolerance_incident"
    if int(metrics.get("consecutive_gateway_failures", 0) or 0) >= 3:
        return False, "gateway_failure_threshold"
    completed = int(metrics.get("completed_turns", 0) or 0)
    hard_failures = int(metrics.get("hard_failures", 0) or 0)
    if completed >= 20 and hard_failures / completed > 0.05:
        return False, "hard_failure_rate_threshold"
    if completed >= 20 and float(metrics.get("p95_latency_ms", 0) or 0) > 60_000:
        return False, "latency_threshold"
    return True, "ready"


def _systemd_property(service: str, name: str) -> str:
    return subprocess.check_output(
        ["systemctl", "show", service, f"--property={name}", "--value"], text=True, timeout=5,
    ).strip()


def _systemd_state(service: str) -> dict[str, Any]:
    active = _systemd_property(service, "ActiveState")
    restarts = _systemd_property(service, "NRestarts")
    warnings = subprocess.check_output(
        ["journalctl", "--unit", service, "--since", "35 seconds ago", "--priority", "warning", "--no-pager", "--output", "cat"],
        text=True, timeout=5,
    ).splitlines()
    return {
        "active": active == "active",
        "nrestarts": int(restarts) if restarts.isdigit() else -1,
        "working_directory": _systemd_property(service, "WorkingDirectory"),
        "warning_count": len([line for line in warnings if line.strip()]),
    }


def _rollback(url: str, bridge_key: str) -> None:
    parsed = urlsplit(url)
    loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not (parsed.scheme == "https" or loopback) or parsed.username or parsed.password:
        raise ValueError("rollback URL must use HTTPS or loopback HTTP")
    body = json.dumps({"action": "rollback"}).encode("utf-8")
    request = Request(url, data=body, method="PUT", headers={
        "Content-Type": "application/json", "X-Plugin-Bridge-Key": bridge_key,
    })
    with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed loopback URL from validated operations config.
        if response.status != 200:
            raise RuntimeError("atomic rollback failed")


def run_release_guard(
    *,
    evidence_store: ReleaseEvidenceStore,
    v3_health: dict[str, Any],
    plugin_health: dict[str, Any],
    v3_service: dict[str, Any],
    plugin_service: dict[str, Any],
    rollback: Callable[[], None],
    now: datetime | None = None,
) -> dict[str, Any]:
    application = plugin_health.get("application") if isinstance(plugin_health.get("application"), dict) else {}
    previous = evidence_store.latest_guard_check() or {}
    previous_metrics = previous.get("metrics") if isinstance(previous.get("metrics"), dict) else {}
    restart_total = int(v3_service.get("nrestarts", -1)) + int(plugin_service.get("nrestarts", -1))
    previous_restarts = int(previous_metrics.get("restart_total", restart_total))
    identity_fields = ("runtime_version", "source_commit", "manifest_sha256", "artifact_sha256", "release_generation")
    health_match = (
        v3_health.get("status") == "ok" and plugin_health.get("ok") is True and plugin_health.get("registered") is True
        and v3_health.get("runtime_version") == application.get("agent_runtime_version")
        and v3_health.get("source_commit") == application.get("source_commit")
        and v3_health.get("manifest_sha256") == application.get("manifest_sha256")
        and v3_health.get("release_generation") == application.get("release_generation")
        and all(v3_health.get(field) not in (None, "") for field in identity_fields[:-1])
    )
    if application.get("conversation_agent_mode") == "active":
        evidence = evidence_store.get(str(application.get("release_evidence_id", "")))
        health_match = bool(health_match and evidence
            and evidence.get("source_commit") == v3_health.get("source_commit")
            and evidence.get("manifest_sha256") == v3_health.get("manifest_sha256")
            and evidence.get("v3_artifact_sha256") == v3_health.get("artifact_sha256")
            and evidence.get("plugin_artifact_sha256") == application.get("artifact_sha256")
            and evidence.get("v3_working_directory") == v3_service.get("working_directory")
            and evidence.get("plugin_working_directory") == plugin_service.get("working_directory"))
    metrics = dict(application.get("release_guard_metrics") if isinstance(application.get("release_guard_metrics"), dict) else {})
    metrics["restart_total"] = restart_total
    snapshot = {
        "services_active": v3_service.get("active") is True and plugin_service.get("active") is True,
        "restart_delta": max(0, restart_total - previous_restarts),
        "warning_count": int(v3_service.get("warning_count", 0)) + int(plugin_service.get("warning_count", 0)),
        "health_match": health_match,
        "metrics": metrics,
    }
    ok, code = evaluate_release_guard(snapshot)
    check = evidence_store.record_guard_check({
        "ok": ok, "code": code, "checked_at": (now or datetime.now(UTC)).isoformat(),
        "source_commit": str(v3_health.get("source_commit", "")),
        "manifest_sha256": str(v3_health.get("manifest_sha256", "")),
        "v3_artifact_sha256": str(v3_health.get("artifact_sha256", "")),
        "plugin_artifact_sha256": str(application.get("artifact_sha256", "")),
        "release_generation": int(v3_health.get("release_generation", 0) or 0), "metrics": metrics,
    })
    if not ok and application.get("conversation_agent_mode") == "active":
        rollback()
        check["rollback_triggered"] = True
    else:
        check["rollback_triggered"] = False
    return check


def record_probe_failure(evidence_store: ReleaseEvidenceStore, rollback: Callable[[], None]) -> dict[str, Any]:
    previous = evidence_store.latest_guard_check() or {}
    metrics = dict(previous.get("metrics") if isinstance(previous.get("metrics"), dict) else {})
    failures = int(metrics.get("guard_probe_failures", 0)) + 1
    metrics["guard_probe_failures"] = failures
    result = evidence_store.record_guard_check({
        **previous, "ok": False, "code": "guard_probe_failed", "checked_at": datetime.now(UTC).isoformat(), "metrics": metrics,
    })
    if failures >= 3:
        rollback(); result["rollback_triggered"] = True
    else:
        result["rollback_triggered"] = False
    return result


def main() -> int:
    store = ReleaseEvidenceStore(Path(os.environ["WANDA_RELEASE_EVIDENCE_PATH"]))
    v3_url = os.getenv("WANDA_V3_HEALTH_URL", "http://127.0.0.1:8011/health")
    plugin_url = os.getenv("WANDA_PLUGIN_HEALTH_URL", "http://127.0.0.1:4003/healthz")
    rollback_url = os.getenv("WANDA_AGENT_RELEASE_URL", "http://127.0.0.1:8011/api/xianyu-plugin/bridge/agent-release")
    bridge_key = os.environ["WANDA_PLUGIN_BRIDGE_KEY"]
    rollback = lambda: _rollback(rollback_url, bridge_key)
    try:
        result = run_release_guard(
            evidence_store=store,
            v3_health=fetch_health_json(v3_url), plugin_health=fetch_health_json(plugin_url),
            v3_service=_systemd_state("wanda-v3-backend.service"),
            plugin_service=_systemd_state("wanda-seat-autoquote.service"),
            rollback=rollback,
        )
    except Exception as error:  # Fail closed without leaking credentials.
        try:
            result = record_probe_failure(store, rollback)
        except Exception:
            result = {"ok": False, "code": "guard_probe_failure_unrecorded", "rollback_triggered": False}
        result["error"] = type(error).__name__
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
