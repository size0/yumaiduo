from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class WandaShowSource(Protocol):
    """Read-only Wanda showtime source; no seat, price, or order methods."""

    async def get_showtimes(
        self, wanda_store_id: str, show_date: str,
    ) -> Mapping[str, Any]: ...
