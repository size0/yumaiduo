from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .errors import ProbeError


class LiveSeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seat_id: str = Field(min_length=1, max_length=240)
    label: str = Field(min_length=1, max_length=120)
    area_code: str = Field(min_length=1, max_length=120)
    zone_type: str = Field(min_length=1, max_length=120)
    available: bool
    wplus: bool
    original_price_cents: int | None = Field(default=None, gt=0, le=2_000_000)


class ProbeSeatSelector:
    def select_exact(self, requested_labels: list[str], live_seats: list[LiveSeat]) -> list[LiveSeat]:
        wanted = [str(label).strip() for label in requested_labels if str(label).strip()]
        by_label = {seat.label: seat for seat in live_seats}
        selected = [by_label.get(label) for label in wanted]
        if len(selected) != len(wanted) or any(item is None for item in selected):
            raise ProbeError("official_selection_unverifiable", "截图座位无法与实时座位逐座匹配。")
        seats = [item for item in selected if item is not None]
        if any(not seat.available for seat in seats):
            raise ProbeError("official_selection_unverifiable", "截图座位当前不可售。")
        return self._representatives(seats, preserve_all=False)

    def select_area(self, live_seats: list[LiveSeat]) -> list[LiveSeat]:
        available = [seat for seat in live_seats if seat.available and seat.wplus]
        if not available:
            raise ProbeError("wplus_area_unavailable", "没有可用于 Area Probe 的实时可售 W+ 座位。")
        return self._representatives(available, preserve_all=False)

    @staticmethod
    def _representatives(seats: list[LiveSeat], *, preserve_all: bool) -> list[LiveSeat]:
        if preserve_all:
            return seats
        groups: dict[tuple[str, str], LiveSeat] = {}
        for seat in seats:
            groups.setdefault((seat.area_code, seat.zone_type), seat)
        return list(groups.values())
