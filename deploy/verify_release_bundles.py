from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "v3-backend-gateway-work"))

from app.release_bundle import verify_component_archive

EXPECTED_CONTRACTS = {
    "v3": "wanda-v3-v11-pricing-account-evidence",
    "plugin": "wanda-agent-runtime-v28-pricing-evidence-gate",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify deterministic V3 and plugin release candidate archives")
    parser.add_argument("release_directory", type=Path)
    args = parser.parse_args()
    directory = args.release_directory.resolve()
    try:
        release_manifest = json.loads((directory / "release-manifest.json").read_text(encoding="utf-8"))
        source_commit = str(release_manifest["source_commit"])
        artifacts = release_manifest["artifacts"]
        if release_manifest.get("dirty_source") is not False or len(source_commit) != 40 or not isinstance(artifacts, dict):
            raise ValueError("invalid release manifest")
        reports = {}
        for component, runtime_contract in EXPECTED_CONTRACTS.items():
            artifact = artifacts[component]
            filename = str(artifact["file"])
            if Path(filename).name != filename:
                raise ValueError("unsafe artifact filename")
            reports[component] = verify_component_archive(
                directory / filename,
                expected_sha256=str(artifact["sha256"]),
                expected_component=component,
                expected_source_commit=source_commit,
                expected_runtime_contract=runtime_contract,
            )
        ready = all(report["ready"] is True for report in reports.values())
        output = {"ready": ready, "code": "ready" if ready else "artifact_verification_failed", "components": reports}
    except Exception:
        output = {"ready": False, "code": "release_manifest_invalid", "components": {}}
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0 if output["ready"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
