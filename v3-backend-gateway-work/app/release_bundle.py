"""Deterministic tracked-source release bundle construction."""
from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _zip_info(name: str, mode: int = 0o644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    return info


def build_component_archive(
    repository_root: Path,
    component_name: str,
    tracked_files: Sequence[str],
    output_path: Path,
    manifest: Mapping[str, object],
) -> str:
    """Create a deterministic archive from an explicit tracked-file allowlist."""
    repository = repository_root.resolve()
    component = (repository / component_name).resolve()
    prefix = f"{PurePosixPath(component_name).as_posix().rstrip('/')}/"
    selected: list[tuple[str, Path]] = []
    for raw_name in sorted(set(map(str, tracked_files))):
        normalized = PurePosixPath(raw_name).as_posix()
        if not normalized.startswith(prefix):
            raise ValueError(f"release path is outside component: {normalized}")
        archive_name = normalized[len(prefix):]
        if not archive_name or archive_name.startswith("../"):
            raise ValueError(f"invalid release path: {normalized}")
        source = (repository / normalized).resolve()
        if component not in source.parents or not source.is_file() or source.is_symlink():
            raise ValueError(f"invalid release source: {normalized}")
        selected.append((archive_name, source))

    release_manifest = {
        **dict(manifest),
        "file_count": len(selected),
        "files": [name for name, _source in selected],
    }
    manifest_bytes = (json.dumps(release_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(_zip_info("RELEASE-MANIFEST.json"), manifest_bytes)
        for archive_name, source in selected:
            mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
            archive.writestr(_zip_info(archive_name, mode), source.read_bytes())
    return hashlib.sha256(output_path.read_bytes()).hexdigest()
