from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_first_store import RulesFirstStore  # noqa: E402


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


store = RulesFirstStore(Path(sys.argv[2]), protector=PlainProtector())
stage = sys.argv[1]
index = sys.argv[3]
now = datetime(2026, 8, 26, tzinfo=timezone.utc)
if stage == "inbox":
    store.enqueue_event(event(index), now=now)
elif stage == "event_claim":
    store.claim_event(now=now, lease_seconds=1)
elif stage == "command_claim":
    store.claim_commands(now=now, lease_seconds=1)
elif stage == "result":
    store.record_command_result(
        sys.argv[4], sys.argv[5], {"status": "succeeded", "message_id": f"message-{index}"}, now=now,
    )
else:
    raise RuntimeError("unknown stage")
os._exit(77)
