from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.direct_gateway_preflight import validate_direct_gateway_environment


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate secret-safe Wanda direct gateway deployment prerequisites")
    parser.add_argument("--require-enabled", action="store_true", help="fail when the direct gateway is disabled")
    args = parser.parse_args()
    report = asyncio.run(validate_direct_gateway_environment(require_enabled=args.require_enabled))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ready"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
