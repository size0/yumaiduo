from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .canonical import (
    ActivityOffersResult,
    CancelResult,
    CreateOrderResult,
    OrderStatusResult,
    SeatAvailabilityResult,
    canonical_activity,
    canonical_create_order,
    canonical_order_status,
)
from .models import ProbeResult
from .account_pool import ProbeAccount


class V3CaptureReplayProvider:
    """Replay sanitized V3 files through the neutral canonical protocol."""

    fixture = True

    def __init__(self, fixture_dir: Path) -> None:
        self._dir = Path(fixture_dir)
        self.manifest = self._load("manifest.json")
        required = {"fixture_version", "provider", "cinema_id", "show_id", "seat_type", "capture_schema_version", "captured_at"}
        if not required.issubset(self.manifest) or self.manifest.get("source") != "V3" or self.manifest.get("sensitive_data_removed") is not True:
            raise ValueError("invalid_v3_capture_manifest")
        self._cancelled = False
        self._release_index = 0

    def _load(self, name: str) -> dict[str, Any]:
        value = json.loads((self._dir / name).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"capture_{name}_invalid")
        return value

    async def create_probe_order(self, *, account: ProbeAccount, show_id: str, seat_ids: list[str]) -> CreateOrderResult:
        return canonical_create_order(self._load("create_order.response.json"))

    async def get_order_status(self, *, temporary_order_reference: str) -> OrderStatusResult:
        name = "cancel_status.response.json" if self._cancelled else "lock_status.response.json"
        return canonical_order_status(self._load(name))

    async def get_activity_offers(self, *, temporary_order_reference: str) -> ActivityOffersResult:
        return canonical_activity(self._load("activity_offers.response.json"))

    async def cancel_probe_order(self, *, temporary_order_reference: str) -> CancelResult:
        self._cancelled = True
        value = self._load("cancel.response.json")
        return CancelResult(accepted=value.get("code") in (0, "0") or value.get("ok") is True)

    async def get_available_seats(self, *, show_id: str, seat_ids: list[str]) -> SeatAvailabilityResult:
        index = min(self._release_index, 2)
        self._release_index += 1
        names = ("seat_release_0s.response.json", "seat_release_2s.response.json", "seat_release_5s.response.json")
        return SeatAvailabilityResult(available_seat_ids=_available_ids(self._load(names[index])))

    def expected_result(self) -> ProbeResult:
        return ProbeResult.model_validate(self._load("expected_probe_result.json"))


def _available_ids(value: Mapping[str, Any]) -> set[str]:
    ids = value.get("available_seat_ids") or value.get("availableSeatIds")
    if isinstance(ids, list):
        return {str(item) for item in ids}
    seats = value.get("seats") or value.get("availableSeats")
    if isinstance(seats, list):
        result: set[str] = set()
        for item in seats:
            if isinstance(item, Mapping):
                value = item.get("seat_id") or item.get("seatId") or item.get("id")
                if value is not None:
                    result.add(str(value))
            elif item is not None:
                result.add(str(item))
        return result
    return set()
