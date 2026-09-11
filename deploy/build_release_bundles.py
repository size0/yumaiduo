from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
EXPECTED_CONTRACTS = {
    "backend": "wanda-v4-canonical-agent-runtime",
    "plugin": "wanda-seat-autoquote-runtime",
}
COMPONENTS = {
    "backend": "backend",
    "plugin": "plugin-runtime/wanda-seat-autoquote",
}
REQUIRED_FILES = {
    "backend": (
        "backend/app/main.py",
        "backend/app/canonical_conversation_agent.py",
        "backend/app/canonical_event_handler.py",
    ),
    "plugin": (
        "plugin-runtime/wanda-seat-autoquote/index.mjs",
        "plugin-runtime/wanda-seat-autoquote/package.json",
        "plugin-runtime/wanda-seat-autoquote/package-lock.json",
    ),
}


def _git(*arguments: str) -> str:
    return subprocess.check_output(
        ["git", *arguments], cwd=REPOSITORY, text=True, encoding="utf-8",
    ).strip()


def _tracked_files(component: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", component], cwd=REPOSITORY,
    )
    return [item.decode("utf-8") for item in output.split(b"\0") if item]


def _assert_required_files(tracked_files: list[str], required: tuple[str, ...]) -> None:
    tracked = set(tracked_files)
    missing = [path for path in required if path not in tracked]
    if missing:
        raise RuntimeError(f"release_required_files_missing: {','.join(missing)}")


def _write_archive(
    output: Path, component: str, tracked_files: list[str], metadata: dict[str, object],
) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in tracked_files:
            source = REPOSITORY / path
            if not source.is_file():
                raise RuntimeError(f"tracked_release_file_missing: {path}")
            info = zipfile.ZipInfo(path)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())
        info = zipfile.ZipInfo("release-metadata.json")
        info.date_time = (1980, 1, 1, 0, 0, 0)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, json.dumps({**metadata, "component": component}, sort_keys=True).encode("utf-8"))
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deterministic canonical V4 and plugin release archives")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--allow-dirty", action="store_true", help="development only; production releases must remain clean")
    args = parser.parse_args()
    dirty = bool(_git("status", "--porcelain", "--untracked-files=all"))
    if dirty and not args.allow_dirty:
        print(json.dumps({"ready": False, "code": "git_worktree_dirty"}, sort_keys=True))
        return 1

    commit = _git("rev-parse", "HEAD")
    output_dir = (args.output_dir or REPOSITORY / "dist" / "release-candidates" / commit[:12]).resolve()
    artifacts: dict[str, dict[str, str]] = {}
    for label, component in COMPONENTS.items():
        tracked_files = _tracked_files(component)
        _assert_required_files(tracked_files, REQUIRED_FILES[label])
        output = output_dir / f"{label}-{commit[:12]}.zip"
        digest = _write_archive(
            output, label, tracked_files,
            {"source_commit": commit, "runtime_contract": EXPECTED_CONTRACTS[label], "dirty_source": dirty},
        )
        (output.with_suffix(output.suffix + ".sha256")).write_text(
            f"{digest}  {output.name}\n", encoding="ascii",
        )
        artifacts[label] = {"file": output.name, "sha256": digest}

    release_manifest = {"source_commit": commit, "dirty_source": dirty, "artifacts": artifacts}
    manifest_path = output_dir / "release-manifest.json"
    manifest_path.write_text(json.dumps(release_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    print(json.dumps({
        "ready": not dirty, "code": "ready" if not dirty else "dirty_development_bundle",
        "output_dir": str(output_dir), "manifest_sha256": manifest_digest,
    }, sort_keys=True))
    return 0 if not dirty else 2


if __name__ == "__main__":
    raise SystemExit(main())
