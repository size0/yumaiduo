from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "v3-backend-gateway-work"))

from app.release_health import fetch_health_json, validate_runtime_health


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify deployed V3 and plugin runtime contracts")
    parser.add_argument("--v3-health-url", required=True)
    parser.add_argument("--plugin-health-url", required=True)
    args = parser.parse_args()
    try:
        report = validate_runtime_health(
            fetch_health_json(args.v3_health_url),
            fetch_health_json(args.plugin_health_url),
        )
    except Exception:
        report = {
            "ready": False,
            "code": "health_request_failed",
            "v3_contract_match": False,
            "plugin_contract_match": False,
            "plugin_registered": False,
            "release_identity_match": False,
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ready"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
