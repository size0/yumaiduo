from __future__ import annotations

import re
from datetime import date
from collections.abc import Callable

from .models import MovieImageInfo, RealQuote
from .selected_seat_quote_service import (
    QuoteServiceError,
    SelectedSeat as LiangpiaoSeat,
    SelectedSeatQuoteRequest,
)


_SEAT_PATTERN = re.compile(r"^(\d{1,3})排(\d{1,3})座$")


class LiangpiaoExactQuoteAdapter:
    """Adapt Liangpiao's read-only exact-seat preflight into the quote contract."""

    def __init__(self, service: object, *, price_mode_provider: Callable[[], str] | None = None) -> None:
        self._service = service
        self._price_mode_provider = price_mode_provider

    async def quote(
        self, recognition: MovieImageInfo, *, tenant_id: str, conversation_id: str,
    ) -> RealQuote:
        if not recognition.cinema_id or not recognition.show_id:
            raise QuoteServiceError("LIANGPIAO_IDENTITY_MISSING", "良票缺少可核验的影院或场次 ID。")
        seats = [self._seat(value) for value in recognition.selected_seats]
        if not seats or any(value is None for value in seats):
            raise QuoteServiceError("LIANGPIAO_EXACT_SEATS_REQUIRED", "非万达影院需要明确座位后才能精确报价。")
        price_mode = self._price_mode_provider() if self._price_mode_provider else "FIXED"
        if price_mode not in {"FIXED", "LIMIT"}:
            price_mode = "FIXED"
        request = SelectedSeatQuoteRequest(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            cinema_id=recognition.cinema_id,
            show_id=recognition.show_id,
            cinema_name=recognition.cinema_name,
            movie_name=recognition.movie_name,
            show_date=recognition.date.isoformat() if isinstance(recognition.date, date) else None,
            showtime_start=recognition.showtime_start,
            hall_name=recognition.hall_name,
            seats=[value for value in seats if value is not None],
            ticket_mode="STANDARD",
            price_mode=price_mode,
        )
        result = await self._service.quote(request)
        count = len(result.seats)
        unit = result.buyer_amount_fen // count if result.buyer_amount_fen % count == 0 else None
        return RealQuote(
            quote_scope="exact_seats",
            quote_date=recognition.date,
            seat_zone_type="LIANGPIAO",
            seat_type="mixed",
            unit_quote_cents=unit,
            total_quote_cents=result.buyer_amount_fen,
            seat_quotes=[],
            ticket_count=count,
            needs_ticket_count=False,
            pricing_source=(
                "良票实时选座预检 + 后台良票报价规则"
                if result.operator_pricing_applied else "良票实时选座预检"
            ),
            price_source="liangpiao_realtime_preflight",
            price_mode=result.price_mode,
            max_price_cents=result.max_price_fen,
            pricing_rule_version=result.pricing_rule_version,
            provider_quote_id=result.quote_id,
            provider_quote_hash=result.quote_hash,
            quote_generation=result.generation,
            detail=(
                f"良票 LIMIT 预估价，已叠加后台良票规则（{result.operator_markup_percent:g}%）；"
                "最终以出票/结算为准，下单时会再次实时校验。"
                if price_mode == "LIMIT" and result.operator_pricing_applied else
                "良票 LIMIT 预估上限，最终以出票/结算为准；下单时会再次实时校验。"
                if price_mode == "LIMIT" else
                f"良票 FIXED 官方一口价，已叠加后台良票规则（{result.operator_markup_percent:g}%）；"
                "下单时会再次实时校验，若价格变化则不自动下单。"
                if result.operator_pricing_applied else
                "良票 FIXED 官方一口价；下单时会再次实时校验，若价格变化则不自动下单。"
            ),
            matched_cinema_name=recognition.cinema_name,
            matched_city_name=recognition.city,
            matched_movie_name=recognition.movie_name,
            matched_showtime_start=recognition.showtime_start,
            matched_hall_name=recognition.hall_name,
        )

    @staticmethod
    def _seat(value: object) -> LiangpiaoSeat | None:
        row_no = getattr(value, "row_no", None)
        col_no = getattr(value, "col_no", None)
        seat_number = str(getattr(value, "seat_number", "") or "").strip()
        if row_no is None or col_no is None:
            match = _SEAT_PATTERN.fullmatch(seat_number)
            if not match:
                return None
            row_no, col_no = (int(item) for item in match.groups())
        try:
            return LiangpiaoSeat(
                row_no=int(row_no), col_no=int(col_no), seat_no=seat_number,
                area_id=getattr(value, "area_id", None),
            )
        except (TypeError, ValueError):
            return None
