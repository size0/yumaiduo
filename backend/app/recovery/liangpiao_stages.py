from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..pricing.errors import PricingError
from ..selected_seat_quote_service import (
    QuoteServiceError,
    SelectedSeat,
    SelectedSeatQuoteRequest,
)


class LiangpiaoStageService:
    """Independent read/pricing/persist stages for Liangpiao exact-seat quotes."""

    def __init__(self, quote_service: Any, facts_adapter: Any, pricing_engine: Any,
                 quote_store: Any, rules_provider: Any) -> None:
        self.quote_service = quote_service
        self.facts_adapter = facts_adapter
        self.pricing_engine = pricing_engine
        self.quote_store = quote_store
        self.rules_provider = rules_provider

    def prepare_request(self, recognition: Any, route: Any, identity: Mapping[str, str]) -> SelectedSeatQuoteRequest:
        seats: list[SelectedSeat] = []
        for raw in recognition.selected_seats or []:
            match = re.search(r"(\d+)\s*[排行]\s*(\d+)\s*[座號号]", str(raw))
            if not match:
                raise ValueError("SELECTED_SEATS_REQUIRED")
            seats.append(SelectedSeat(row_no=int(match.group(1)), col_no=int(match.group(2)), seat_no=str(raw)))
        if not seats:
            raise ValueError("SELECTED_SEATS_REQUIRED")
        raw = recognition.raw_provider_result if isinstance(recognition.raw_provider_result, dict) else {}
        values = raw.get("rawResults") if isinstance(raw.get("rawResults"), dict) else raw
        cinema_id = route.liangpiao_cinema_id or values.get("cinemaId") or values.get("cinema_id")
        if not cinema_id:
            raise QuoteServiceError("LIANGPIAO_CINEMA_ID_REQUIRED", "cinema id missing")
        show_id = values.get("showId") or values.get("show_id")
        return SelectedSeatQuoteRequest(
            tenant_id=identity["tenant_id"], conversation_id=identity["chat_id"] or identity["event_id"],
            cinema_id=int(cinema_id), show_id=str(show_id) if show_id else None,
            cinema_name=recognition.cinema_text, movie_name=recognition.movie,
            show_date=recognition.show_date, showtime_start=recognition.start_time,
            hall_name=recognition.hall, seats=seats, generation=1,
            trace_id=f'{identity["event_id"]}:liangpiao',
        )

    async def preflight(self, request: SelectedSeatQuoteRequest) -> Any:
        return await self.quote_service.quote(request)

    def cost_facts(self, preflight: Any, request: SelectedSeatQuoteRequest) -> Any:
        snapshot = preflight.snapshot if isinstance(preflight.snapshot, dict) else {}
        payload = snapshot.get("preflight_response")
        if not isinstance(payload, dict):
            raise QuoteServiceError("LIANGPIAO_PREFLIGHT_RESPONSE_MISSING", "preflight response missing")
        return self.facts_adapter.from_preflight(
            payload,
            request={"showId": preflight.show_id, "priceMode": preflight.price_mode,
                     "seats": [seat.model_dump(mode="json") for seat in preflight.seats]},
        )

    def price(self, facts: Any) -> Any:
        pricing = self.pricing_engine.quote(facts, self.rules_provider())
        if pricing.total_quote_cents is None:
            raise PricingError("LIANGPIAO_TOTAL_MISSING")
        return pricing

    def persist(self, pricing: Any, preflight: Any, request: SelectedSeatQuoteRequest,
                recognition: Any, identity: Mapping[str, str]) -> dict[str, Any] | None:
        snapshot = preflight.snapshot if isinstance(preflight.snapshot, dict) else {}
        return self.quote_store.persist_liangpiao(
            pricing, tenant_id=identity["tenant_id"], shop_id=identity["shop_id"],
            buyer_id=identity["buyer_id"], chat_id=identity["chat_id"],
            purchase_context_id=identity["purchase_context_id"],
            request_id=f'{identity["event_id"]}:recovery-liangpiao',
            city=recognition.city_text or "", cinema_id=str(request.cinema_id),
            cinema_name=recognition.cinema_text or "", movie=recognition.movie or "",
            quote_date=recognition.show_date or "", showtime_start=recognition.start_time or "",
            hall=recognition.hall or "", show_id=preflight.show_id,
            selected_seats=[seat.model_dump(mode="json") for seat in preflight.seats],
            event_id=identity["event_id"], recognition_id=recognition.provider_recognize_id,
            message_id=identity.get("message_id"), provider_snapshot_id=preflight.quote_id,
            provider_preflight_expires_at=snapshot.get("provider_preflight_expires_at"),
        )
