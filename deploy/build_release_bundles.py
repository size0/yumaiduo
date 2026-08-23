from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "v3-backend-gateway-work"))

from app.release_bundle import build_component_archive, require_release_files

EXPECTED_V3_CONTRACT = "wanda-v3-v15-content-hash-vision-cache"
EXPECTED_AGENT_RUNTIME = "wanda-agent-runtime-v33-shadow-evaluation-switch"


def _git(*arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=REPOSITORY, text=True, encoding="utf-8").strip()


def _tracked_files(component: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", component],
        cwd=REPOSITORY,
    )
    return [item.decode("utf-8") for item in output.split(b"\0") if item]


def _assert_contracts() -> None:
    v3_source = (REPOSITORY / "v3-backend-gateway-work/app/main.py").read_text(encoding="utf-8")
    plugin_source = (REPOSITORY / "plugin-auto-reply-work/src/agent/shadow-agent-runtime.mjs").read_text(encoding="utf-8")
    if EXPECTED_V3_CONTRACT not in v3_source or EXPECTED_AGENT_RUNTIME not in plugin_source:
        raise RuntimeError("release runtime contract constants are stale")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deterministic tracked-source V3 and plugin release archives")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--allow-dirty", action="store_true", help="development only; production releases must remain clean")
    args = parser.parse_args()
    dirty = bool(_git("status", "--porcelain", "--untracked-files=all"))
    if dirty and not args.allow_dirty:
        print(json.dumps({"ready": False, "code": "git_worktree_dirty"}, sort_keys=True))
        return 1
    _assert_contracts()
    commit = _git("rev-parse", "HEAD")
    output_dir = (args.output_dir or REPOSITORY / "dist" / "release-candidates" / commit[:12]).resolve()
    components = {
        "v3": ("v3-backend-gateway-work", EXPECTED_V3_CONTRACT),
        "plugin": ("plugin-auto-reply-work", EXPECTED_AGENT_RUNTIME),
    }
    required_files = {
        "v3": ["v3-backend-gateway-work/app/main.py", "v3-backend-gateway-work/requirements.txt"],
        "plugin": [
            "plugin-auto-reply-work/index.mjs",
            "plugin-auto-reply-work/package-lock.json",
            "plugin-auto-reply-work/vendor/plugin-sdk-server/dist/index.js",
        ],
    }
    artifacts: dict[str, dict[str, object]] = {}
    for label, (component, runtime_contract) in components.items():
        output = output_dir / f"{label}-{commit[:12]}.zip"
        tracked_files = _tracked_files(component)
        require_release_files(tracked_files, required_files[label])
        digest = build_component_archive(
            REPOSITORY,
            component,
            tracked_files,
            output,
            {
                "component": label,
                "source_commit": commit,
                "runtime_contract": runtime_contract,
                "dirty_source": dirty,
            },
        )
        artifacts[label] = {"file": output.name, "sha256": digest}
        (output.with_suffix(output.suffix + ".sha256")).write_text(f"{digest}  {output.name}\n", encoding="ascii")
    release_manifest = {
        "source_commit": commit,
        "dirty_source": dirty,
        "artifacts": artifacts,
    }
    manifest_path = output_dir / "release-manifest.json"
    manifest_path.write_text(json.dumps(release_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    print(json.dumps({"ready": not dirty, "code": "ready" if not dirty else "dirty_development_bundle", "output_dir": str(output_dir), "manifest_sha256": manifest_digest}, sort_keys=True))
    return 0 if not dirty else 2


if __name__ == "__main__":
    raise SystemExit(main())
