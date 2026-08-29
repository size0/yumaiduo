from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.rules_first_store import RulesFirstStore


class PlainProtector:
    def protect(self, value: str) -> str:
        return f"enc:{value}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


def event(index: str) -> dict[str, object]:
    return {
        "envelope": {
            "id": f"kill-{index}", "tenantId": "tenant-1", "event": "buyer.message",
            "payload": {
                "accountUnb": "shop-1", "peerUnb": f"buyer-{index}", "chatId": f"chat-{index}",
            },
        },
    }


def kill(worker: Path, stage: str, database: Path, index: str, *extra: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(worker), stage, str(database), index, *extra],
        check=False, capture_output=True, timeout=20,
    )
    assert completed.returncode == 77, completed.stderr.decode(errors="replace")


def complete_empty_event(store: RulesFirstStore, *, now: datetime) -> None:
    claimed = store.claim_event(now=now, lease_seconds=1)
    assert claimed is not None
    store.complete_event(claimed["inbox_id"], claimed["lease_token"], commands=[], state_revision=0, now=now)


def create_send_command(store: RulesFirstStore, index: str, *, now: datetime) -> None:
    store.enqueue_event(event(index), now=now)
    claimed = store.claim_event(now=now, lease_seconds=1)
    assert claimed is not None
    store.complete_event(
        claimed["inbox_id"], claimed["lease_token"], state_revision=1, now=now,
        commands=[{"id": f"kill-{index}:reply", "type": "send_message", "text": "fixed"}],
    )


def test_four_real_process_kill_boundaries_each_recover_for_50_rounds(tmp_path: Path) -> None:
    worker = Path(__file__).with_name("rules_first_crash_worker.py")
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    later = now + timedelta(seconds=2)
    for round_index in range(50):
        database = tmp_path / f"round-{round_index}.sqlite3"
        store = RulesFirstStore(database, protector=PlainProtector())

        inbox_index = f"{round_index}-inbox"
        kill(worker, "inbox", database, inbox_index)
        complete_empty_event(store, now=now)

        event_index = f"{round_index}-event"
        store.enqueue_event(event(event_index), now=now)
        kill(worker, "event_claim", database, event_index)
        complete_empty_event(store, now=later)

        command_index = f"{round_index}-command"
        create_send_command(store, command_index, now=now)
        kill(worker, "command_claim", database, command_index)
        reclaimed = store.claim_commands(now=later, lease_seconds=1)
        command = next(item for item in reclaimed if item["event_id"] == f"kill-{command_index}")
        store.record_command_result(
            command["command_id"], command["lease_token"],
            {"status": "succeeded", "message_id": f"message-{command_index}"}, now=later,
        )

        result_index = f"{round_index}-result"
        create_send_command(store, result_index, now=now)
        claimed = next(
            item for item in store.claim_commands(now=now)
            if item["event_id"] == f"kill-{result_index}"
        )
        kill(worker, "result", database, result_index, claimed["command_id"], claimed["lease_token"])
        duplicate = store.record_command_result(
            claimed["command_id"], claimed["lease_token"],
            {"status": "succeeded", "message_id": f"message-{result_index}"}, now=later,
        )
        assert duplicate["status"] == "succeeded"
