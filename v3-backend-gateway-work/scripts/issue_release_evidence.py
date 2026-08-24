"""Issue immutable release evidence from local, server-collected artifacts only."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from app.release_evidence import ReleaseEvidenceStore, canonical_sha256, release_identity_from_env
from app.release_health import fetch_health_json


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def working_directory(service: str) -> str:
    return subprocess.check_output(["systemctl", "show", service, "--property=WorkingDirectory", "--value"], text=True, timeout=5).strip()


def aggregate_evaluations(reports: list[dict[str, object]]) -> tuple[dict[str, object], str]:
    if len(reports) != 3 or any(report.get("runtime_version") != "wanda-agent-runtime-v37-model-led-native-tools" for report in reports):
        raise ValueError("exactly three v37 evaluation reports are required")
    for report in reports:
        declared = str(report.get("report_sha256", ""))
        actual = canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
        if declared != actual:
            raise ValueError("evaluation report digest mismatch")
    fingerprints = [canonical_sha256({"datasets": report.get("datasets"), "versions": report.get("versions")}) for report in reports]
    if len(set(fingerprints)) != 1 or {int(report.get("run_index", 0)) for report in reports} != {1, 2, 3}:
        raise ValueError("evaluation runs must use one frozen dataset/version set and run indexes 1..3")
    metrics = [report.get("metrics") for report in reports]
    if any(not isinstance(item, dict) for item in metrics):
        raise ValueError("evaluation metrics missing")
    minimum_fields = (
        "failure_replay_count", "image_sample_count", "tool_selection_accuracy", "recoverable_success_rate",
        "unrecoverable_safe_handoff_rate", "image_full_path_rate", "completion_rate", "identity_accuracy", "schema_valid_rate",
    )
    maximum_fields = ("precondition_replan_rate", "p95_latency_ms")
    zero_fields = (
        "high_risk_actions", "false_transaction_facts", "duplicate_writes", "unknown_result_retries",
        "cross_tenant_access", "authoritative_inconsistencies", "unsafe_final_replies", "post_deadline_effects",
    )
    aggregate = {field: min(float(item[field]) for item in metrics) for field in minimum_fields}
    aggregate.update({field: max(float(item[field]) for item in metrics) for field in maximum_fields})
    aggregate.update({field: sum(int(item[field]) for item in metrics) for field in zero_fields})
    aggregate["consecutive_passes"] = 3
    return aggregate, canonical_sha256(reports)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--evidence-store", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, action="append", required=True)
    parser.add_argument("--rollback-record", type=Path, required=True)
    args = parser.parse_args()

    if len(args.evaluation) != 3:
        raise ValueError("--evaluation must be supplied exactly three times")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.evaluation]
    aggregate, report_sha = aggregate_evaluations(reports)
    rollback = json.loads(args.rollback_record.read_text(encoding="utf-8"))
    if rollback.get("verified") is not True or rollback.get("restored_same_release") is not True:
        raise ValueError("real rollback drill evidence is required")

    v3 = fetch_health_json("http://127.0.0.1:8011/health")
    plugin = fetch_health_json("http://127.0.0.1:4003/healthz")
    application = plugin.get("application") if isinstance(plugin.get("application"), dict) else {}
    identity = release_identity_from_env()
    if sha256_file(args.manifest) != identity["manifest_sha256"]:
        raise ValueError("release manifest digest does not match process environment")
    if v3.get("source_commit") != identity["source_commit"] or v3.get("manifest_sha256") != identity["manifest_sha256"]:
        raise ValueError("live V3 identity does not match process environment")
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    if manifest.get("source_commit") != identity["source_commit"] or v3.get("artifact_sha256") != artifacts.get("v3", {}).get("sha256"):
        raise ValueError("live V3 artifact does not match release manifest")
    if application.get("source_commit") != identity["source_commit"] or application.get("manifest_sha256") != identity["manifest_sha256"] or application.get("artifact_sha256") != artifacts.get("plugin", {}).get("sha256"):
        raise ValueError("live plugin artifact does not match release manifest")
    base_generation = int(v3.get("release_generation", 0))
    record = {
        "evidence_id": args.evidence_id, "release_id": args.release_id,
        "runtime_version": identity["runtime_version"], "source_commit": identity["source_commit"],
        "manifest_sha256": identity["manifest_sha256"],
        "plugin_artifact_sha256": str(artifacts.get("plugin", {}).get("sha256", "")),
        "v3_artifact_sha256": str(artifacts.get("v3", {}).get("sha256", "")),
        "evaluation_report_sha256": report_sha, "evaluation": aggregate,
        "base_generation": base_generation, "target_generation": base_generation + 1,
        "rollback_verified": True,
        "rollback_record_sha256": sha256_file(args.rollback_record),
        "v3_working_directory": working_directory("wanda-v3-backend.service"),
        "plugin_working_directory": working_directory("wanda-seat-autoquote.service"),
        "native_complete": application.get("native_complete") is True,
        "plugin_worker_count": sum(application.get(field) == "running" for field in ("worker", "agent_worker", "agent_outbox_worker", "historical_evaluation_worker", "human_comparison_worker")),
        "outbox_ready": isinstance(application.get("agent_outbox"), dict),
        "issued_at": datetime.now(UTC).isoformat(),
    }
    issued = ReleaseEvidenceStore(args.evidence_store).issue(record)
    print(json.dumps({"ready": True, "evidence_id": issued["evidence_id"], "evidence_sha256": issued["evidence_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
