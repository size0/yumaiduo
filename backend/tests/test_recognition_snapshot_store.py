from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models import MovieImageInfo
from app.recognition_snapshot_store import (
    RecognitionSnapshotAccessDenied,
    RecognitionSnapshotConflict,
    RecognitionSnapshotPayloadTooLarge,
    RecognitionSnapshotStore,
)


class Protector:
    def protect(self, value: str) -> str:
        return "sealed:" + value[::-1]

    def unprotect(self, value: str) -> str:
        if not value.startswith("sealed:"):
            raise ValueError("invalid protected payload")
        return value.removeprefix("sealed:")[::-1]


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def identity() -> dict[str, str]:
    return {
        "tenant_id": "tenant-1",
        "shop_id": "shop-1",
        "buyer_id": "buyer-1",
        "chat_id": "chat-1",
    }


def recognition(*, marker: str = "provider-marker", cinema_id: int = 1486) -> MovieImageInfo:
    raw_results = {
        "cinema": "影院截图原文",
        "futureRawField": {"marker": marker, "nested": [1, {"x": True}]},
    }
    final_results = {
        "matchLevel": "EXACT",
        "cinemaId": cinema_id,
        "futureFinalField": {"opaque": marker},
    }
    raw_response = {
        "code": 0,
        "requestId": "request-1",
        "data": {
            "recognizeId": "recognize-1",
            "futureTopLevel": {"preserveMe": marker},
            "rawResults": raw_results,
            "finalResults": final_results,
        },
    }
    return MovieImageInfo(
        cinema_id=cinema_id,
        cinema_name="标准影院",
        recognition_id="recognize-1",
        provider_request_id="request-1",
        trace_id="trace-1",
        match_level="EXACT",
        provider_match_level="EXACT",
        raw_results=raw_results,
        final_results=final_results,
        raw_response=raw_response,
    )


def create_snapshot(
    store: RecognitionSnapshotStore,
    *,
    target_id: str = "target-a",
    event_id: str = "message-1",
    value: MovieImageInfo | None = None,
):
    return store.create(
        **identity(),
        target_id=target_id,
        event_id=event_id,
        recognition=value or recognition(),
    )


def test_snapshot_survives_restart_and_preserves_unknown_provider_fields(tmp_path: Path) -> None:
    path = tmp_path / "recognition-snapshots.sqlite3"
    store = RecognitionSnapshotStore(path, protector=Protector())
    created = create_snapshot(store)

    restarted = RecognitionSnapshotStore(path, protector=Protector())
    restored = restarted.get_current(**identity(), target_id="target-a")

    assert restored == created
    assert restored is not None
    assert restored.raw_results["futureRawField"]["nested"][1] == {"x": True}
    assert restored.final_results["futureFinalField"] == {"opaque": "provider-marker"}
    assert restored.raw_response["data"]["futureTopLevel"] == {"preserveMe": "provider-marker"}
    assert restored.normalized.raw_response == restored.raw_response
    assert len(restored.payload_hash) == 64

    with sqlite3.connect(path) as connection:
        protected = connection.execute(
            "SELECT payload_protected FROM recognition_snapshots WHERE snapshot_id = ?",
            (created.snapshot_id,),
        ).fetchone()[0]
    assert protected.startswith("sealed:")
    assert "provider-marker" not in protected


def test_snapshot_normalization_excludes_computed_fields_compatibly(tmp_path: Path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "snapshots.sqlite3", protector=Protector())
    created = create_snapshot(store)

    with sqlite3.connect(tmp_path / "snapshots.sqlite3") as connection:
        protected = connection.execute(
            "SELECT payload_protected FROM recognition_snapshots WHERE snapshot_id = ?",
            (created.snapshot_id,),
        ).fetchone()[0]

    payload = json.loads(Protector().unprotect(protected))
    assert "seat_display" not in payload["normalized"]
    assert "seat_display_mode" not in payload["normalized"]
    assert "fulfillment_route" not in payload["normalized"]


def test_each_target_has_an_independent_current_snapshot(tmp_path: Path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "snapshots.sqlite3", protector=Protector())
    first_a = create_snapshot(store, target_id="target-a", event_id="event-a1")
    first_b = create_snapshot(
        store,
        target_id="target-b",
        event_id="event-b1",
        value=recognition(marker="target-b", cinema_id=2002),
    )
    second_a = create_snapshot(
        store,
        target_id="target-a",
        event_id="event-a2",
        value=recognition(marker="target-a-new", cinema_id=3003),
    )

    assert store.get_current(**identity(), target_id="target-a") == second_a
    assert store.get_current(**identity(), target_id="target-b") == first_b
    assert first_a.is_current is True
    retired_a = store.get_by_id(**identity(), snapshot_id=first_a.snapshot_id)
    assert retired_a is not None
    assert retired_a.snapshot_id == first_a.snapshot_id
    assert retired_a.is_current is False


def test_compare_and_swap_rejects_stale_revision_and_is_event_idempotent(tmp_path: Path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "snapshots.sqlite3", protector=Protector())
    created = create_snapshot(store)
    replacement = recognition(marker="after-confirm", cinema_id=9090)

    updated = store.compare_and_swap(
        **identity(),
        snapshot_id=created.snapshot_id,
        target_id="target-a",
        expected_revision=1,
        event_id="confirm-result-1",
        recognition=replacement,
    )
    duplicate = store.compare_and_swap(
        **identity(),
        snapshot_id=created.snapshot_id,
        target_id="target-a",
        expected_revision=1,
        event_id="confirm-result-1",
        recognition=recognition(marker="must-be-ignored", cinema_id=8080),
    )

    assert updated.revision == 2
    assert updated.normalized.cinema_id == 9090
    assert duplicate == updated
    with pytest.raises(RecognitionSnapshotConflict, match="revision_conflict"):
        store.compare_and_swap(
            **identity(),
            snapshot_id=created.snapshot_id,
            target_id="target-a",
            expected_revision=1,
            event_id="confirm-result-2",
            recognition=replacement,
        )


def test_append_confirmation_is_atomic_bounded_and_idempotent(tmp_path: Path) -> None:
    store = RecognitionSnapshotStore(
        tmp_path / "snapshots.sqlite3",
        protector=Protector(),
        max_confirmation_history=2,
    )
    current = create_snapshot(store)
    current = store.append_confirmation(
        **identity(),
        snapshot_id=current.snapshot_id,
        target_id="target-a",
        expected_revision=current.revision,
        event_id="confirm-1",
        confirmation={"cinemaId": 1486, "futureConfirmationField": {"keep": True}},
    )
    duplicate = store.append_confirmation(
        **identity(),
        snapshot_id=current.snapshot_id,
        target_id="target-a",
        expected_revision=1,
        event_id="confirm-1",
        confirmation={"cinemaId": 9999},
    )
    assert duplicate == current

    for index in (2, 3):
        current = store.append_confirmation(
            **identity(),
            snapshot_id=current.snapshot_id,
            target_id="target-a",
            expected_revision=current.revision,
            event_id=f"confirm-{index}",
            confirmation={"showId": str(index)},
        )

    assert current.revision == 4
    assert [item.event_id for item in current.confirmation_history] == ["confirm-2", "confirm-3"]
    assert current.confirmation_history[-1].details == {"showId": "3"}


def test_snapshot_access_is_denied_across_identity_or_target(tmp_path: Path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "snapshots.sqlite3", protector=Protector())
    created = create_snapshot(store)

    assert store.get_current(
        tenant_id="tenant-2", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1", target_id="target-a",
    ) is None
    with pytest.raises(RecognitionSnapshotAccessDenied, match="identity_mismatch"):
        store.get_by_id(
            tenant_id="tenant-2",
            shop_id="shop-1",
            buyer_id="buyer-1",
            chat_id="chat-1",
            snapshot_id=created.snapshot_id,
        )
    with pytest.raises(RecognitionSnapshotAccessDenied, match="target_mismatch"):
        store.compare_and_swap(
            **identity(),
            snapshot_id=created.snapshot_id,
            target_id="target-b",
            expected_revision=1,
            event_id="bad-target",
            recognition=recognition(),
        )


def test_expired_snapshot_is_not_current_but_remains_auditable_by_id(tmp_path: Path) -> None:
    clock = Clock()
    store = RecognitionSnapshotStore(
        tmp_path / "snapshots.sqlite3", protector=Protector(), ttl_seconds=10, clock=clock,
    )
    created = create_snapshot(store)
    clock.advance(seconds=11)

    assert store.get_current(**identity(), target_id="target-a") is None
    restored = store.get_by_id(**identity(), snapshot_id=created.snapshot_id)
    assert restored is not None
    assert restored.snapshot_id == created.snapshot_id
    with pytest.raises(RecognitionSnapshotConflict, match="expired"):
        store.append_confirmation(
            **identity(), snapshot_id=created.snapshot_id, target_id="target-a",
            expected_revision=1, event_id="too-late", confirmation={"cinemaId": 1},
        )


def test_payload_size_limit_fails_explicitly_without_partial_row(tmp_path: Path) -> None:
    path = tmp_path / "snapshots.sqlite3"
    store = RecognitionSnapshotStore(path, protector=Protector(), max_payload_bytes=500)
    oversized = recognition(marker="x" * 2_000)

    with pytest.raises(RecognitionSnapshotPayloadTooLarge, match="payload_too_large") as caught:
        create_snapshot(store, value=oversized)

    assert caught.value.actual_bytes > caught.value.max_bytes
    assert store.get_current(**identity(), target_id="target-a") is None


def test_create_is_idempotent_per_slot_event_and_rejects_changed_payload(tmp_path: Path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "snapshots.sqlite3", protector=Protector())
    first = create_snapshot(store)
    duplicate = create_snapshot(store)
    assert duplicate == first

    with pytest.raises(RecognitionSnapshotConflict, match="event_payload_conflict"):
        create_snapshot(store, value=recognition(marker="changed"))


def test_snapshot_json_contract_does_not_leak_private_store_fields(tmp_path: Path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "snapshots.sqlite3", protector=Protector())
    snapshot = create_snapshot(store)
    payload = json.loads(snapshot.model_dump_json())

    assert payload["target_id"] == "target-a"
    assert payload["revision"] == 1
    assert "payload_protected" not in payload
