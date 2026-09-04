from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class WandaRealtimeSeatSource(Protocol):
    """Read-only realtime seat source; it must not expose write operations."""

    async def get_realtime_seats(self, wanda_show_id: str) -> Mapping[str, Any]: ...
