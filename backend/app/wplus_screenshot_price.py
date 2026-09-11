from __future__ import annotations

# Seller-approved lower bound for the special screenshot W+ path. This is a
# quote floor, not a source of cost facts and not an areaId/settlePrice value.
WPLUS_SCREENSHOT_MINIMUM_UNIT_PRICE_FEN = 3_590


def quote_from_screenshot_wplus_total(
    total_price_fen: int,
    *,
    ticket_count: int,
    minimum_unit_price_fen: int | None = None,
) -> dict[str, int | None]:
    """Convert an explicit bottom-of-screenshot W+ total into a safe quote.

    The displayed total is rounded upward to whole yuan so the seller quote
    never undercuts the amount shown in the screenshot.  A unit amount is
    returned only when the rounded total divides evenly across the known
    selected-seat count; callers can still quote the exact total otherwise.
    """
    if isinstance(total_price_fen, bool) or not isinstance(total_price_fen, int) or total_price_fen <= 0:
        raise ValueError("screenshot_wplus_total_invalid")
    if isinstance(ticket_count, bool) or not isinstance(ticket_count, int) or not 1 <= ticket_count <= 20:
        raise ValueError("screenshot_wplus_ticket_count_invalid")
    if minimum_unit_price_fen is not None and (
        isinstance(minimum_unit_price_fen, bool)
        or not isinstance(minimum_unit_price_fen, int)
        or minimum_unit_price_fen <= 0
    ):
        raise ValueError("screenshot_wplus_floor_invalid")

    rounded_total = ((total_price_fen + 99) // 100) * 100
    if minimum_unit_price_fen is not None:
        rounded_total = max(rounded_total, minimum_unit_price_fen * ticket_count)
    unit = rounded_total // ticket_count if rounded_total % ticket_count == 0 else None
    return {"total_sell_price_fen": rounded_total, "unit_sell_price_fen": unit}
