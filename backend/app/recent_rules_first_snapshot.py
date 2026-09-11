from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RecentRulesFirstSnapshotError(ValueError):
    pass


def load_recent_rules_first_snapshot(directory: Path) -> dict[str, Any]:
    root = Path(directory)
    manifest_path = root / "manifest.json"
    snapshot_path = root / "snapshot.json"
    if not manifest_path.is_file() or not snapshot_path.is_file():
        raise RecentRulesFirstSnapshotError("recent_rules_first_snapshot_missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if manifest.get("sensitive_data_removed") is not True:
        raise RecentRulesFirstSnapshotError("recent_rules_first_snapshot_not_sanitized")
    if snapshot.get("canonical_utterance_matrix") is None:
        raise RecentRulesFirstSnapshotError("recent_rules_first_snapshot_missing_matrix")
    if len(snapshot.get("canonical_utterance_matrix") or []) != int(manifest.get("canonical_utterance_count") or 0):
        raise RecentRulesFirstSnapshotError("recent_rules_first_snapshot_count_mismatch")
    return {
        "manifest": manifest,
        "snapshot": snapshot,
        "directory": str(root),
    }
