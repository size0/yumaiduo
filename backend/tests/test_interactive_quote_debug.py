from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.interactive_quote_debug import run_demo  # noqa: E402


@pytest.mark.asyncio
async def test_debugger_runs_real_quote_store_agent_and_ordered_outbox() -> None:
    result = await run_demo(verbose=False)

    image = result["image"]
    continuation = result["continuation"]
    assert image["quote_result"]["quote"]["unit_sell_price_fen"] == 6120
    assert len(image["commands"]) == 2
    assert continuation["quote_result"]["quote"]["ticket_count"] == 2
    assert continuation["quote_result"]["quote"]["total_sell_price_fen"] == 12240
    assert len(continuation["commands"]) == 2
    assert [item["action"]["canonical_reply_sequence"] for item in continuation["commands"]] == [1, 2]
    assert {item["action"]["type"] for item in continuation["commands"]} == {"send_message"}
    assert {"raw_arguments", "validated_arguments", "result"} <= set(
        continuation["agent_result"]["tool_trace"][0]
    )
