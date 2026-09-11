from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

EXPECTED_CONTRACTS = {
    "backend": "wanda-v4-canonical-agent-runtime",
    "plugin": "wanda-seat-autoquote-runtime",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_artifact(directory: Path, component: str, artifact: dict[str, object], source_commit: str) -> dict[str, object]:
    filename = str(artifact["file"])
    path = directory / filename
    if Path(filename).name != filename or not path.is_file():
        raise ValueError(f"artifact_missing:{component}")
    actual_sha256 = _sha256(path)
    if actual_sha256 != str(artifact["sha256"]):
        raise ValueError(f"artifact_hash_mismatch:{component}")
    with zipfile.ZipFile(path) as archive:
        metadata = json.loads(archive.read("release-metadata.json"))
        file_count = len(archive.namelist()) - 1
    if (
        metadata.get("component") != component
        or metadata.get("source_commit") != source_commit
        or metadata.get("runtime_contract") != EXPECTED_CONTRACTS[component]
        or metadata.get("dirty_source") is not False
    ):
        raise ValueError(f"artifact_metadata_invalid:{component}")
    return {"ready": True, "sha256": actual_sha256, "file_count": file_count}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify canonical V4 and plugin release candidate archives")
    parser.add_argument("release_directory", type=Path)
    args = parser.parse_args()
    directory = args.release_directory.resolve()
    try:
        manifest = json.loads((directory / "release-manifest.json").read_text(encoding="utf-8"))
        source_commit = str(manifest["source_commit"])
        artifacts = manifest["artifacts"]
        if manifest.get("dirty_source") is not False or len(source_commit) != 40 or not isinstance(artifacts, dict):
            raise ValueError("release_manifest_invalid")
        reports = {
            component: _verify_artifact(directory, component, artifacts[component], source_commit)
            for component in EXPECTED_CONTRACTS
        }
        output = {"ready": True, "code": "ready", "components": reports}
    except Exception:
        output = {"ready": False, "code": "release_manifest_invalid", "components": {}}
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0 if output["ready"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
