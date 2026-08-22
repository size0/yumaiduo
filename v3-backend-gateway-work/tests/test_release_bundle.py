from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from app.release_bundle import build_component_archive, require_release_files, verify_component_archive


def test_release_archive_is_deterministic_and_contains_only_declared_files(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    component = repository / "component"
    component.mkdir(parents=True)
    (component / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (component / "config.env").write_text("SECRET=unsafe\n", encoding="utf-8")
    output_one = tmp_path / "one.zip"
    output_two = tmp_path / "two.zip"
    files = ["component/app.py"]
    manifest = {"source_commit": "a" * 40, "component": "test", "runtime_contract": "contract-v1"}

    first = build_component_archive(repository, "component", files, output_one, manifest)
    second = build_component_archive(repository, "component", files, output_two, manifest)

    assert first == second == hashlib.sha256(output_one.read_bytes()).hexdigest()
    assert output_one.read_bytes() == output_two.read_bytes()
    with zipfile.ZipFile(output_one) as archive:
        assert archive.namelist() == ["RELEASE-MANIFEST.json", "app.py"]
        stored_manifest = json.loads(archive.read("RELEASE-MANIFEST.json"))
        assert stored_manifest["source_commit"] == "a" * 40
        assert stored_manifest["file_count"] == 1
        assert "config.env" not in archive.namelist()

    verified = verify_component_archive(
        output_one,
        expected_sha256=first,
        expected_component="test",
        expected_source_commit="a" * 40,
        expected_runtime_contract="contract-v1",
    )
    assert verified == {"ready": True, "code": "ready", "file_count": 1}

    output_one.write_bytes(output_one.read_bytes() + b"tampered")
    tampered = verify_component_archive(
        output_one,
        expected_sha256=first,
        expected_component="test",
        expected_source_commit="a" * 40,
        expected_runtime_contract="contract-v1",
    )
    assert tampered == {"ready": False, "code": "archive_hash_mismatch", "file_count": 0}


def test_release_builder_requires_runtime_dependency_entrypoints() -> None:
    require_release_files(["component/index.mjs", "component/vendor/dist/index.js"], ["component/vendor/dist/index.js"])
    try:
        require_release_files(["component/index.mjs"], ["component/vendor/dist/index.js"])
    except ValueError as error:
        assert "component/vendor/dist/index.js" in str(error)
    else:
        raise AssertionError("missing runtime dependency entrypoint was accepted")


def test_release_archive_rejects_paths_outside_the_component(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / "component").mkdir(parents=True)
    (repository / "outside.txt").write_text("unsafe", encoding="utf-8")
    try:
        build_component_archive(
            repository,
            "component",
            ["outside.txt"],
            tmp_path / "bad.zip",
            {"source_commit": "b" * 40, "component": "test"},
        )
    except ValueError as error:
        assert "outside component" in str(error)
    else:
        raise AssertionError("outside path was packaged")


def test_release_verifier_rejects_archive_path_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    manifest = {
        "component": "test", "source_commit": "c" * 40, "runtime_contract": "contract-v1",
        "file_count": 1, "files": ["../outside.txt"],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("RELEASE-MANIFEST.json", json.dumps(manifest))
        archive.writestr("../outside.txt", "unsafe")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    report = verify_component_archive(
        archive_path,
        expected_sha256=digest,
        expected_component="test",
        expected_source_commit="c" * 40,
        expected_runtime_contract="contract-v1",
    )
    assert report == {"ready": False, "code": "archive_path_unsafe", "file_count": 0}
