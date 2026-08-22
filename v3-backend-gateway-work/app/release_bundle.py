"""Deterministic tracked-source release bundle construction."""
from __future__ import annotations

import hashlib
import hmac
import json
import stat
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


def require_release_files(tracked_files: Sequence[str], required_files: Sequence[str]) -> None:
    available = set(map(str, tracked_files))
    missing = sorted(set(map(str, required_files)) - available)
    if missing:
        raise ValueError(f"required release files are missing: {', '.join(missing)}")


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


def _verification_report(ready: bool, code: str, file_count: int = 0) -> dict[str, object]:
    return {"ready": ready, "code": code, "file_count": file_count}


def verify_component_archive(
    archive_path: Path,
    *,
    expected_sha256: str,
    expected_component: str,
    expected_source_commit: str,
    expected_runtime_contract: str,
) -> dict[str, object]:
    """Verify archive integrity, identity, manifest completeness, and extraction safety."""
    try:
        actual_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    except OSError:
        return _verification_report(False, "archive_unavailable")
    if not hmac.compare_digest(actual_digest, str(expected_sha256).lower()):
        return _verification_report(False, "archive_hash_mismatch")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or "RELEASE-MANIFEST.json" not in names:
                return _verification_report(False, "archive_entries_invalid")
            for info in infos:
                name = PurePosixPath(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or "\\" in info.filename
                    or stat.S_IFMT(mode) == stat.S_IFLNK
                ):
                    return _verification_report(False, "archive_path_unsafe")
            manifest_bytes = archive.read("RELEASE-MANIFEST.json")
            if len(manifest_bytes) > 1024 * 1024:
                return _verification_report(False, "archive_manifest_invalid")
            manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, KeyError, zipfile.BadZipFile):
        return _verification_report(False, "archive_invalid")
    if not isinstance(manifest, dict):
        return _verification_report(False, "archive_manifest_invalid")
    packaged_files = sorted(name for name in names if name != "RELEASE-MANIFEST.json")
    declared_files = manifest.get("files")
    identity_matches = (
        manifest.get("component") == expected_component
        and manifest.get("source_commit") == expected_source_commit
        and manifest.get("runtime_contract") == expected_runtime_contract
    )
    if not identity_matches:
        return _verification_report(False, "archive_identity_mismatch")
    if (
        not isinstance(declared_files, list)
        or sorted(map(str, declared_files)) != packaged_files
        or manifest.get("file_count") != len(packaged_files)
    ):
        return _verification_report(False, "archive_manifest_mismatch")
    return _verification_report(True, "ready", len(packaged_files))
