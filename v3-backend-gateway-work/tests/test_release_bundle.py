from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from app.release_bundle import build_component_archive


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
