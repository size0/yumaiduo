from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping
from uuid import uuid4

from app.config import Settings
from app.pricing.engine import V4PricingEngine
from app.pricing.models import PricingRulesSnapshot, QuoteResult
from app.quote_record_store import QuoteRecordStore
from app.canonical_buyer_reply import CanonicalBuyerReplyRenderer
from app.show_resolve_v2.models import ShowResolutionResult
from app.wanda_pricing_v2.models import WandaPricingResult


QuoteQueryStatus = str
AUTO_PRICING_SOURCE = "AUTO_PRICING"
MANUAL_OPERATOR_SOURCE = "MANUAL_OPERATOR"
AUTO_QUOTE_TTL_SECONDS = 1_800
MANUAL_QUOTE_TTL_SECONDS = 7_200


@dataclass(frozen=True)
class ManualQuoteInput:
    """Already-structured operator price; no natural-language parsing belongs here."""

    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    purchase_context_id: str
    request_id: str
    city: str
    cinema: str
    movie: str
    quote_date: str
    showtime_start: str
    hall: str
    seat_display: str
    quote_scope: str
    seat_zone_type: str
    price_basis: str
    quote_source: str = MANUAL_OPERATOR_SOURCE
    # Provider fulfillment route is independent from quote source. ``None``
    # is retained for old library callers; it must never be guessed as Wanda.
    provider_route: Literal["WANDA_SELF", "LIANGPIAO"] | None = None
    unit_sell_price_fen: int | None = None
    ticket_count: int | None = None
    total_sell_price_fen: int | None = None
    showtime_end: str | None = None
    seat_request_type: str | None = None
    format: str | None = None
    language: str | None = None
    selected_seats: list[dict[str, Any]] | None = None
    event_id: str | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class CanonicalQuoteRequest:
    """Structured quote facts supplied by an upper layer; never NLP-parsed here."""

    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    purchase_context_id: str
    request_id: str
    city: str
    cinema: str
    movie: str
    quote_date: str
    showtime_start: str
    hall: str | None = None
    dimension: str | None = None
    language: str | None = None
    seat_request_type: Literal["WPLUS_AREA", "EXACT_SEATS"] = "WPLUS_AREA"
    ticket_count: int | None = None
    ticket_mode: Literal["STANDARD", "FAST", "FLASH"] = "STANDARD"
    area_quote_strategy: Literal["AVERAGE", "HIGHEST", "LOWEST"] | None = None
    selected_seats: list[str] | None = None
    has_manual_mark: bool | None = None
    image_url: str | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class QuoteQueryResult:
    status: QuoteQueryStatus
    quotes: list[dict[str, Any]]

    @property
    def count(self) -> int:
        return len(self.quotes)


class QuoteV2Service:
    """Persist and query V2 pricing results without any transaction side effect."""

    def __init__(
        self,
        store: QuoteRecordStore,
        *,
        settings: Settings | None = None,
        ttl_seconds: int | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        configured_ttl = ttl_seconds if ttl_seconds is not None else (
            settings.quote_record_ttl_seconds if settings is not None
            else Settings.from_env().quote_record_ttl_seconds
        )
        self.store = store
        # TTL is configuration for the store operation, not a quote lifecycle
        # decision. QuoteRecordStore remains the only component that computes
        # expiry, generation, supersession, and eligibility.
        self._configured_ttl_seconds = configured_ttl
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def persist(
        self,
        pricing_result: WandaPricingResult,
        show_facts: ShowResolutionResult,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        wanda_city_id: str,
        cinema_name: str,
        purchase_context_id: str,
        request_id: str,
        event_id: str | None = None,
        recognition_id: str | None = None,
        message_id: str | None = None,
        has_manual_mark: bool | None = None,
        mark_image_reference: str | None = None,
        mark_message_id: str | None = None,
        original_selected_seats: list[Any] | None = None,
        same_type_reference: Any | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Persist one immutable pricing snapshot; return None when no quote exists."""
        if pricing_result.status == "PRICING_REQUIRES_COST":
            return None
        if pricing_result.status != "PRICED":
            raise ValueError("pricing_result_not_persistable")
        identity = _identity(tenant_id, shop_id, buyer_id, chat_id)
        context = _required(purchase_context_id, "purchase_context_id")
        request = _required(request_id, "request_id")
        if event_id and request == event_id:
            raise ValueError("request_id_event_id_must_be_distinct")
        created = _utc(created_at or self._now_provider())
        quote_id = _new_id("quote")
        record_id = _new_id("record")
        if quote_id == record_id or quote_id == event_id or record_id == event_id:
            raise RuntimeError("quote_identity_collision")
        record = self._record_from_result(
            pricing_result, show_facts,
            identity=identity, city_id=wanda_city_id, cinema_name=cinema_name,
            context=context, request_id=request, event_id=event_id,
            recognition_id=recognition_id, message_id=message_id,
            has_manual_mark=has_manual_mark, mark_image_reference=mark_image_reference,
            mark_message_id=mark_message_id,
            original_selected_seats=original_selected_seats,
            same_type_reference=same_type_reference,
            reference_only=same_type_reference is not None,
            quote_id=quote_id, record_id=record_id, created=created,
        )
        return _project(self.store.save_quote(
            record, ttl_seconds=self._configured_ttl_seconds, now=created,
        ))

    def persist_liangpiao(
        self,
        pricing_result: QuoteResult,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        purchase_context_id: str,
        request_id: str,
        city: str,
        cinema_id: str,
        cinema_name: str,
        movie: str,
        quote_date: str,
        showtime_start: str,
        hall: str,
        show_id: str,
        selected_seats: list[dict[str, Any]] | None = None,
        ticket_mode: str | None = None,
        area_quote_strategy: str | None = None,
        provider_market_amount_fen: int | None = None,
        pricing_rule_revision: int | None = None,
        seller_pricing_rule_version: str | None = None,
        movie_id: str | None = None,
        event_id: str | None = None,
        recognition_id: str | None = None,
        message_id: str | None = None,
        provider_preflight_expires_at: str | None = None,
        provider_snapshot_id: str | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Persist a Liangpiao ``QuoteResult`` in the shared quote authority.

        The provider preflight remains evidence in ``RulesFirstStore``; this
        method only creates the seller-facing, canonical QuoteRecord.  It does
        not call Liangpiao, create an order, or enqueue a message.
        """
        if pricing_result.provider != "LIANGPIAO" or pricing_result.total_quote_cents is None:
            if pricing_result.total_quote_cents is None:
                return None
            raise ValueError("liangpiao_pricing_result_invalid")
        identity = _identity(tenant_id, shop_id, buyer_id, chat_id)
        context = _required(purchase_context_id, "purchase_context_id")
        request = _required(request_id, "request_id")
        created = _utc(created_at or self._now_provider())
        record = {
            "record_id": _new_id("record"), "quote_id": _new_id("quote"),
            "event_id": event_id, "request_id": request,
            "recognition_id": recognition_id, "message_id": message_id,
            "tenant_id": identity[0], "shop_id": identity[1],
            "buyer_id": identity[2], "chat_id": identity[3],
            "purchase_context_id": context, "item_id": context,
            "created_at": created.isoformat(), "status": "succeeded",
            "source": AUTO_PRICING_SOURCE, "provider": "LIANGPIAO",
            "provider_route": "LIANGPIAO",
            "quote_route": pricing_result.quote_route.lower(),
            "pricing_quote_route": pricing_result.quote_route,
            "canonical_quote_route": "LIANGPIAO",
            "liangpiao_cinema_id": _required(cinema_id, "liangpiao_cinema_id"),
            "liangpiao_movie_id": movie_id,
            "liangpiao_show_id": _required(show_id, "liangpiao_show_id"),
            "provider_quote_id": pricing_result.provider_quote_id,
            "provider_quote_hash": pricing_result.provider_quote_hash,
            "provider_preflight_expires_at": provider_preflight_expires_at,
            "provider_snapshot_id": provider_snapshot_id,
            "ticket_mode": ticket_mode, "price_mode": pricing_result.price_mode,
            "area_quote_strategy": area_quote_strategy,
            "city": _required(city, "city"),
            "cinema": _required(cinema_name, "cinema_name"),
            "movie": _required(movie, "movie"),
            "quote_date": _required(quote_date, "quote_date"),
            "showtime_start": _required(showtime_start, "showtime_start"),
            "hall": _required(hall, "hall"),
            "request_type": "EXACT_SEATS",
            "quote_scope": pricing_result.quote_scope,
            "seat_zone_type": pricing_result.seat_zone_type,
            "selected_seats": list(selected_seats or []),
            "seat_display": "、".join(
                str(item.get("seatNo") or item.get("seat_no") or item.get("seatName") or "")
                for item in (selected_seats or [])
            ) or "良票选座",
            "ticket_count": pricing_result.ticket_count,
            "needs_ticket_count": pricing_result.needs_ticket_count,
            "pricing_rule_revision": pricing_rule_revision,
            "pricing_rule_version": seller_pricing_rule_version or pricing_result.pricing_rule_version,
            "provider_pricing_rule_version": pricing_result.pricing_rule_version,
            "unit_sell_price_fen": pricing_result.unit_quote_cents,
            "total_sell_price_fen": pricing_result.total_quote_cents,
            "unit_quote_cents": pricing_result.unit_quote_cents,
            "total_quote_cents": pricing_result.total_quote_cents,
            "provider_amount_fen": pricing_result.provider_amount_cents,
            "provider_base_amount_fen": pricing_result.base_total_cents,
            "provider_market_amount_fen": provider_market_amount_fen,
            "provider_max_amount_fen": pricing_result.max_price_cents,
            "calculation_evidence": dict(pricing_result.calculation_evidence),
            "semantic_flags": list(pricing_result.semantic_flags),
        }
        return _project(self.store.save_quote(
            record, ttl_seconds=self._configured_ttl_seconds, now=created,
        ))

    def persist_record(
        self, record: dict[str, Any], *, ttl_seconds: int | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Persist an already structured canonical record; no provider calls."""
        created = _utc(created_at or self._now_provider())
        return _project(self.store.save_quote(
            dict(record), ttl_seconds=ttl_seconds or self._configured_ttl_seconds, now=created,
        ))

    def persist_manual(
        self,
        manual: ManualQuoteInput,
        *,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist one explicit operator quote in the shared QuoteRecordStore.

        ``manual`` is intentionally a structured DTO. This method does not
        inspect messages, call a provider, or invoke the pricing engine.
        """
        _validate_manual_input(manual)
        created = _utc(created_at or self._now_provider())
        quote_id = _new_id("manual-quote")
        record_id = _new_id("manual-record")
        total = (
            manual.unit_sell_price_fen * manual.ticket_count
            if manual.price_basis == "UNIT" and manual.ticket_count is not None
            else manual.total_sell_price_fen
        )
        record = {
            "record_id": record_id, "quote_id": quote_id,
            "event_id": manual.event_id, "request_id": manual.request_id,
            "message_id": manual.message_id,
            "tenant_id": manual.tenant_id, "shop_id": manual.shop_id,
            "buyer_id": manual.buyer_id, "chat_id": manual.chat_id,
            "purchase_context_id": manual.purchase_context_id,
            "item_id": manual.purchase_context_id, "created_at": created.isoformat(),
            "status": "succeeded", "source": manual.quote_source,
            "provider": (
                "WANDA" if manual.provider_route == "WANDA_SELF"
                else "LIANGPIAO" if manual.provider_route == "LIANGPIAO" else None
            ),
            "provider_route": manual.provider_route,
            "quote_route": "manual_operator",
            "canonical_quote_route": manual.provider_route,
            "city": manual.city, "cinema": manual.cinema, "movie": manual.movie,
            "quote_date": manual.quote_date, "showtime_start": manual.showtime_start,
            "showtime_end": manual.showtime_end, "hall": manual.hall,
            "format": manual.format, "language": manual.language,
            "seat_request_type": manual.seat_request_type,
            "quote_scope": manual.quote_scope, "seat_zone_type": manual.seat_zone_type,
            "seat_display": manual.seat_display,
            "selected_seats": list(manual.selected_seats or []),
            "has_selected_seats": bool(manual.selected_seats),
            "price_basis": manual.price_basis,
            "unit_sell_price_fen": manual.unit_sell_price_fen,
            "total_sell_price_fen": total,
            "ticket_count": manual.ticket_count,
            "needs_ticket_count": manual.price_basis == "UNIT" and manual.ticket_count is None,
            "cost_items": [], "cost_sources": [],
            "pricing_rule_revision": None, "pricing_rule_version": None,
            "unit_quote_cents": manual.unit_sell_price_fen,
            "total_quote_cents": total,
        }
        return _project(self.store.save_quote(
            record, ttl_seconds=MANUAL_QUOTE_TTL_SECONDS, now=created,
        ))

    def manual_quote_applicability(
        self,
        record: dict[str, Any],
        *,
        ticket_count: int | None,
        at: datetime | None = None,
    ) -> str:
        """Evaluate manual-price quantity semantics without reviving expired quotes."""
        reference = _utc(at or self._now_provider())
        if record.get("source") != MANUAL_OPERATOR_SOURCE:
            return "NOT_MANUAL_QUOTE"
        if not self.store.is_quote_active(record, at=reference):
            return "QUOTE_EXPIRED"
        if record.get("price_basis") == "UNIT":
            return "APPLICABLE"
        if record.get("price_basis") == "TOTAL" and ticket_count == record.get("ticket_count"):
            return "APPLICABLE"
        return "REQUOTE_REQUIRED"

    def list_active_quotes(
        self,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        *,
        at: datetime | None = None,
    ) -> QuoteQueryResult:
        _identity(tenant_id, shop_id, buyer_id, chat_id)
        status, records = self.store.list_current_quotes(
            tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id,
            chat_id=chat_id, at=_utc(at or self._now_provider()),
        )
        return QuoteQueryResult(status, [_project(item) for item in records])

    def get_record(self, tenant_id: str, record_id: str) -> dict[str, Any] | None:
        return _project(self.store.get_record(tenant_id=tenant_id, record_id=record_id))

    @staticmethod
    def _record_from_result(
        pricing: WandaPricingResult,
        show: ShowResolutionResult,
        *,
        identity: tuple[str, str, str, str],
        city_id: str,
        cinema_name: str,
        context: str,
        request_id: str,
        event_id: str | None,
        recognition_id: str | None,
        message_id: str | None,
        has_manual_mark: bool | None,
        mark_image_reference: str | None,
        mark_message_id: str | None,
        original_selected_seats: list[Any] | None,
        same_type_reference: Any | None,
        reference_only: bool,
        quote_id: str,
        record_id: str,
        created: datetime,
    ) -> dict[str, Any]:
        selected = [
            {
                "seat_id": quote.seat_id,
                "seat_label": quote.seat_label,
                "cost_fen": quote.cost_fen,
                "sell_price_fen": quote.sell_price_fen,
                "cost_source": quote.cost_source,
            }
            for quote in pricing.seat_quotes
        ]
        if reference_only and original_selected_seats:
            selected = [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                for item in original_selected_seats
            ]
        seat_display = "、".join(item["seat_label"] for item in selected) or "W+区域"
        costs = [item.model_dump(mode="json") for item in pricing.cost_items]
        first_cost = costs[0] if costs else {}
        zones = {str(item.get("zone_type") or "").strip() for item in costs}
        zones.discard("")
        seat_zone_type = next(iter(zones)) if len(zones) == 1 else "混合区域"
        return {
            "record_id": record_id,
            "quote_id": quote_id,
            "event_id": event_id,
            "request_id": request_id,
            "recognition_id": recognition_id,
            "message_id": message_id,
            "has_manual_mark": has_manual_mark,
            "has_selected_seats": bool(selected),
            "mark_image_reference": mark_image_reference,
            "mark_message_id": mark_message_id,
            "tenant_id": identity[0], "shop_id": identity[1],
            "buyer_id": identity[2], "chat_id": identity[3],
            "purchase_context_id": context, "item_id": context,
            "created_at": created.isoformat(),
            "status": "succeeded",
            "provider": "WANDA", "provider_route": "WANDA_SELF",
            "quote_route": "wanda_self", "canonical_quote_route": "WANDA_SELF",
            "wanda_city_id": _required(city_id, "wanda_city_id"),
            "wanda_store_id": _required(show.wanda_store_id, "wanda_store_id"),
            "wanda_show_id": _required(show.wanda_show_id, "wanda_show_id"),
            "city": city_id, "cinema": _required(cinema_name, "cinema_name"),
            "movie": _required(show.movie_name, "movie"),
            "quote_date": _required(show.show_date, "show_date"),
            "showtime_start": _required(show.start_time, "start_time"),
            "hall": _required(show.hall_name, "hall"),
            "request_type": pricing.request_type,
            "quote_scope": (
                "same_type_reference_preview" if reference_only
                else "exact_seats" if pricing.request_type == "EXACT_SEATS" else "area_preview"
            ),
            "seat_zone_type": seat_zone_type or "W+",
            "selected_seats": selected,
            "seat_display": seat_display,
            "ticket_count": None if reference_only else pricing.ticket_count,
            "needs_ticket_count": False if reference_only else pricing.needs_ticket_count,
            "same_type_reference_only": reference_only,
            "same_type_reference": (
                same_type_reference.model_dump(mode="json")
                if reference_only and hasattr(same_type_reference, "model_dump") else
                dict(same_type_reference) if reference_only and isinstance(same_type_reference, Mapping) else None
            ),
            "cost_source": first_cost.get("cost_source"),
            "cost_fen": first_cost.get("cost_fen"),
            "cost_items": costs,
            "cost_sources": pricing.cost_sources,
            "pricing_rule_revision": pricing.pricing_rule_revision,
            "pricing_rule_version": pricing.pricing_rule_version,
            "unit_sell_price_fen": pricing.unit_sell_price_fen,
            "total_sell_price_fen": None if reference_only else pricing.total_sell_price_fen,
            "unit_quote_cents": pricing.unit_sell_price_fen,
            "total_quote_cents": None if reference_only else pricing.total_sell_price_fen,
            "seat_quotes": [quote.model_dump(mode="json") for quote in pricing.seat_quotes],
            # QuoteRecordStore assigns every lifecycle field, including the
            # terms fingerprint and quote hash.
            "source": AUTO_PRICING_SOURCE,
        }


class CanonicalQuoteRuntime:
    """Read-only composition for the Phase 9B.2.2 quote path.

    This class deliberately stops at ``QuoteRecordStore``.  It has no order,
    payment, callback, outbound-message, probe, or transaction dependencies.
    Provider-specific IDs are accepted only inside their provider branch and
    are stored as lineage metadata rather than promoted to shared IDs.
    """

    def __init__(
        self,
        *,
        recognition_service: Any,
        cinema_route_service: Any,
        show_resolve_service: Any,
        seat_facts_service: Any,
        cost_resolution_service: Any,
        wanda_pricing_service: Any,
        selected_seat_quote_service: Any,
        pricing_rules_provider: Any,
        quote_service: QuoteV2Service,
        liangpiao_facts_adapter: Any,
        manual_mark_detector: Any | None = None,
        pricing_engine: V4PricingEngine | None = None,
        reply_renderer: CanonicalBuyerReplyRenderer | None = None,
    ) -> None:
        self._recognition = recognition_service
        self._route = cinema_route_service
        self._show = show_resolve_service
        self._seats = seat_facts_service
        self._cost = cost_resolution_service
        self._wanda_pricing = wanda_pricing_service
        self._liangpiao_quote = selected_seat_quote_service
        self._rules_provider = pricing_rules_provider
        self._quotes = quote_service
        self._liangpiao_facts = liangpiao_facts_adapter
        self._manual_mark_detector = manual_mark_detector
        self._reply_renderer = reply_renderer
        self._engine = pricing_engine or V4PricingEngine()

    async def aclose(self) -> None:
        close = getattr(self._recognition, "aclose", None)
        if callable(close):
            await close()
        close_detector = getattr(self._manual_mark_detector, "aclose", None)
        if callable(close_detector):
            await close_detector()

    async def quote_structured(self, request: CanonicalQuoteRequest) -> dict[str, Any]:
        """Run a structured request through the same canonical fact pipeline.

        This is an integration seam for a future caller. It intentionally does
        not accept text, invoke an LLM, or infer a request type from prose.
        """
        if not isinstance(request, CanonicalQuoteRequest):
            raise ValueError("canonical_quote_request_invalid")
        if request.ticket_count is not None and (
            isinstance(request.ticket_count, bool) or not 1 <= request.ticket_count <= 20
        ):
            raise ValueError("canonical_ticket_count_invalid")
        if request.seat_request_type == "EXACT_SEATS" and not request.selected_seats:
            return {"status": "SEAT_FACTS_UNAVAILABLE", "reason": "EXACT_SEATS_REQUIRED"}
        from app.recognition_v2.models import RecognitionResult
        identity = {
            "event_id": request.request_id, "tenant_id": request.tenant_id,
            "shop_id": request.shop_id, "buyer_id": request.buyer_id,
            "chat_id": request.chat_id, "purchase_context_id": request.purchase_context_id,
            "message_id": request.message_id or "",
        }
        recognition = RecognitionResult(
            city_text=request.city, cinema_text=request.cinema, movie=request.movie,
            show_date=request.quote_date, start_time=request.showtime_start,
            hall=request.hall, language=request.language, dimension=request.dimension,
            selected_seats=list(request.selected_seats or []),
            has_selected_seats=bool(request.selected_seats),
            has_manual_mark=request.has_manual_mark,
        )
        return await self.quote_recognition(
            recognition, identity=identity, image_url=request.image_url,
            ticket_count=request.ticket_count, ticket_mode=request.ticket_mode,
            area_quote_strategy=request.area_quote_strategy,
        )

    async def refresh_expired_auto_quote(
        self, bound_quote: dict[str, Any], order: Mapping[str, Any], body: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Refresh provider facts and pricing for payment validation only.

        The returned amount is evidence for Phase 10.  This method never calls
        ``persist``/``save_quote`` and never returns a replacement active quote.
        """
        if not isinstance(bound_quote, Mapping):
            return None
        if bound_quote.get("provider_route") == "LIANGPIAO":
            return await self._refresh_expired_liangpiao_quote(bound_quote)
        if bound_quote.get("provider_route") != "WANDA_SELF":
            return None
        show = await self._show.resolve({
            "route": "WANDA_SELF", "wanda_store_id": bound_quote.get("wanda_store_id"),
            "movie": bound_quote.get("movie"), "show_date": bound_quote.get("quote_date"),
            "start_time": bound_quote.get("showtime_start"), "hall": bound_quote.get("hall"),
            "language": bound_quote.get("language"), "dimension": bound_quote.get("dimension"),
        })
        if show.status != "RESOLVED":
            return None
        selected = bound_quote.get("selected_seats") if isinstance(bound_quote.get("selected_seats"), list) else []
        selected_labels = [
            str(item.get("seat_label") or item.get("seat_number") or item.get("label") or "").strip()
            for item in selected if isinstance(item, Mapping)
        ]
        selected_labels = [item for item in selected_labels if item]
        request_type = bound_quote.get("request_type")
        seat_facts = await self._seats.resolve({
            "route": "WANDA_SELF", "wanda_store_id": show.wanda_store_id,
            "wanda_show_id": show.wanda_show_id, "selected_seats": selected_labels,
            # The quote already carried the structured seat/mark decision;
            # validation refresh must not run image/NLP recognition again.
            "has_manual_mark": True if request_type == "EXACT_SEATS" else False,
        })
        if seat_facts.status not in {"EXACT_SEATS_RESOLVED", "WPLUS_AREA_RESOLVED"}:
            return None
        cost = self._cost.resolve(show, seat_facts)
        if cost.status != "COST_READY":
            return None
        pricing = self._wanda_pricing.price(
            cost, show, seat_facts, self._rules(),
            ticket_count=(bound_quote.get("ticket_count") if request_type == "WPLUS_AREA" else None),
        )
        if pricing.status != "PRICED" or not isinstance(pricing.total_sell_price_fen, int) or pricing.total_sell_price_fen <= 0:
            return None
        return {
            "expected_amount_cents": pricing.total_sell_price_fen,
            "provider_route": "WANDA_SELF",
            "provider_facts_refreshed": True,
            "pricing_rule_revision": pricing.pricing_rule_revision,
            "pricing_rule_version": pricing.pricing_rule_version,
            "pricing_evidence": dict(pricing.calculation_evidence),
        }

    async def _refresh_expired_liangpiao_quote(
        self, bound_quote: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if self._liangpiao_quote is None or self._liangpiao_facts is None:
            return None
        try:
            cinema_id = int(bound_quote.get("liangpiao_cinema_id"))
            seats = bound_quote.get("selected_seats")
            if cinema_id <= 0 or not isinstance(seats, list) or not seats:
                return None
            request = {
                "tenant_id": bound_quote.get("tenant_id"),
                "conversation_id": bound_quote.get("chat_id"),
                "cinema_id": cinema_id, "show_id": bound_quote.get("liangpiao_show_id"),
                "cinema_name": bound_quote.get("cinema"), "movie_name": bound_quote.get("movie"),
                "show_date": bound_quote.get("quote_date"),
                "showtime_start": bound_quote.get("showtime_start"),
                "hall_name": bound_quote.get("hall"), "seats": seats,
                "ticket_mode": bound_quote.get("ticket_mode") or "STANDARD",
                "price_mode": bound_quote.get("price_mode") or "FIXED",
                "area_quote_strategy": bound_quote.get("area_quote_strategy"),
                "trace_id": f'{bound_quote.get("record_id", "payment")}:payment-preflight',
            }
            preflight = await self._liangpiao_quote.quote(request)
            response = preflight.snapshot.get("preflight_response") if isinstance(preflight.snapshot, dict) else None
            if not isinstance(response, dict):
                return None
            facts = self._liangpiao_facts.from_preflight(
                response,
                request={"showId": preflight.show_id, "priceMode": preflight.price_mode,
                         "seats": [item.model_dump(mode="json") for item in preflight.seats]},
            )
            pricing = self._engine.quote(facts, self._rules())
            if pricing.total_quote_cents is None or pricing.total_quote_cents <= 0:
                return None
            return {
                "expected_amount_cents": pricing.total_quote_cents,
                "provider_route": "LIANGPIAO",
                "provider_facts_refreshed": True,
                "provider_snapshot_id": preflight.quote_id,
                "pricing_rule_revision": pricing.pricing_rule_revision,
                "pricing_rule_version": pricing.pricing_rule_version,
                "pricing_evidence": dict(pricing.calculation_evidence),
            }
        except Exception:
            return None

    async def process_image_event(self, body: dict[str, Any]) -> dict[str, Any]:
        envelope = body.get("envelope") if isinstance(body.get("envelope"), dict) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        image_urls = payload.get("imageUrls", payload.get("image_urls"))
        if not isinstance(image_urls, list) or not image_urls or not isinstance(image_urls[0], str):
            return {"status": "NO_IMAGE"}
        image_url = image_urls[0].strip()
        if not image_url or len(image_url) > 2_048:
            return {"status": "IMAGE_INVALID"}
        identity = self._event_identity(body)
        if identity is None:
            return {"status": "IDENTITY_INCOMPLETE"}
        try:
            recognition = await self._recognition.recognize(
                image_url, trace_id=identity["event_id"], idempotency_key=identity["event_id"],
            )
            return await self.quote_recognition(
                recognition, identity=identity, image_url=image_url,
            )
        except Exception as error:
            # A provider or fact failure is a structured no-quote result, not
            # permission to re-enter the Legacy NLP/quote path.
            return {"status": "CANONICAL_QUOTE_UNAVAILABLE", "reason": type(error).__name__}

    async def quote_recognition(
        self, recognition: Any, *, identity: dict[str, str], image_url: str | None = None,
        ticket_count: int | None = None, ticket_mode: str = "STANDARD",
        area_quote_strategy: str | None = None,
    ) -> dict[str, Any]:
        # Recognition quality is normally a fail-closed gate. For a Wanda
        # exact-seat request we still read authoritative SeatFacts once: an
        # unavailable target may qualify for a single same-type reference
        # price, while an available target remains blocked until confirmed.
        quality_reasons = list(getattr(recognition, "seat_confirm_reasons", []) or [])
        quality_blocked = bool(
            getattr(recognition, "seat_confirm_required", False)
            or getattr(recognition, "price_mismatch", False)
            or "PRICE_MISMATCH" in quality_reasons
        )
        route = await self._route.resolve(recognition)
        if quality_blocked and route.route != "WANDA_SELF":
            result = {
                "status": "RECOGNITION_QUALITY_UNAVAILABLE", "reason": "PRICE_MISMATCH",
                "recognition_quality_status": "PRICE_MISMATCH", "quote": None,
                "route": "RECOGNITION_QUALITY",
                "recognition": recognition.model_dump(
                    mode="json", exclude={"raw_provider_result"},
                ) if hasattr(recognition, "model_dump") else None,
            }
            return self._render_buyer_reply(result, recognition=recognition)
        if route.route == "UNRESOLVED":
            result = {"status": "ROUTE_UNRESOLVED", "reason": route.resolution_reason}
        else:
            rules = self._rules()
            if route.route == "WANDA_SELF":
                result = await self._quote_wanda(
                    recognition, route, identity, image_url, rules, ticket_count=ticket_count,
                    recognition_quality_blocked=quality_blocked,
                )
            elif route.route == "LIANGPIAO":
                result = await self._quote_liangpiao(
                    recognition, route, identity, rules,
                    ticket_mode=ticket_mode, area_quote_strategy=area_quote_strategy,
                )
            else:
                result = {"status": "ROUTE_UNRESOLVED", "reason": "UNKNOWN_ROUTE"}
        result = {
            **result,
            "route": route.route,
            "recognition": recognition.model_dump(
                mode="json", exclude={"raw_provider_result"},
            ) if hasattr(recognition, "model_dump") else None,
        }
        return self._render_buyer_reply(result, recognition=recognition)

    def _render_buyer_reply(self, result: dict[str, Any], *, recognition: Any) -> dict[str, Any]:
        if self._reply_renderer is None:
            return result
        rendered = self._reply_renderer.render(result)
        replies = rendered.get("messages")
        normalized_replies = [
            {"kind": str(item["kind"]), "text": str(item["text"])}
            for item in replies
            if isinstance(item, Mapping) and item.get("kind") and item.get("text")
        ] if isinstance(replies, list) else []
        return {
            **result,
            "current_runtime_reply": rendered["text"],
            "current_runtime_replies": normalized_replies,
            "canonical_reply_kind": rendered["kind"],
        }

    async def _quote_wanda(
        self, recognition: Any, route: Any, identity: dict[str, str],
        image_url: str | None, rules: PricingRulesSnapshot, *, ticket_count: int | None = None,
        recognition_quality_blocked: bool = False,
    ) -> dict[str, Any]:
        show = await self._show.resolve({
            "route": "WANDA_SELF", "wanda_store_id": route.wanda_store_id,
            "movie": recognition.movie, "show_date": recognition.show_date,
            "start_time": recognition.start_time, "hall": recognition.hall,
            "language": recognition.language, "dimension": recognition.dimension,
        })
        if show.status != "RESOLVED":
            return {
                "status": "SHOW_UNRESOLVED", "reason": show.resolution_reason,
                "show_facts": show.model_dump(mode="json") if hasattr(show, "model_dump") else None,
            }
        selected_seats = list(recognition.selected_seats or [])
        manual_mark = recognition.has_manual_mark
        if not selected_seats:
            # A missing formal seat selection is the normal W+ quote case. It
            # does not require a hand-mark detector or a hand-marked image.
            manual_mark = False
        seat_facts = await self._seats.resolve(
            {
                "route": "WANDA_SELF", "wanda_store_id": route.wanda_store_id,
                "wanda_show_id": show.wanda_show_id,
                "selected_seats": selected_seats,
                "has_manual_mark": manual_mark,
                "image_url": image_url,
            },
            manual_mark_detector=(
                self._manual_mark_detector
                if selected_seats and manual_mark is None else None
            ),
        )
        if seat_facts.status == "MANUAL_MARK_REQUIRED":
            return {
                "status": "MANUAL_MARK_REQUIRED", "reason": seat_facts.resolution_reason,
                "manual_mark_result": seat_facts.has_manual_mark,
                "seat_facts_status": seat_facts.status,
                "seat_facts": seat_facts.model_dump(mode="json"),
            }
        same_type_reference = (
            seat_facts.status == "SEAT_UNAVAILABLE"
            and seat_facts.same_type_reference is not None
        )
        if recognition_quality_blocked and not same_type_reference:
            return {
                "status": "RECOGNITION_QUALITY_UNAVAILABLE", "reason": "PRICE_MISMATCH",
                "recognition_quality_status": "PRICE_MISMATCH", "quote": None,
                "manual_mark_result": seat_facts.has_manual_mark,
                "seat_facts_status": seat_facts.status,
                "seat_facts": seat_facts.model_dump(mode="json"),
            }
        if seat_facts.status not in {"EXACT_SEATS_RESOLVED", "WPLUS_AREA_RESOLVED", "SEAT_UNAVAILABLE"}:
            return {
                "status": "SEAT_FACTS_UNAVAILABLE", "reason": seat_facts.resolution_reason,
                "manual_mark_result": seat_facts.has_manual_mark,
                "seat_facts_status": seat_facts.status,
                "seat_facts": seat_facts.model_dump(mode="json"),
            }
        cost = self._cost.resolve(show, seat_facts)
        if cost.status != "COST_READY":
            return {
                "status": cost.status, "reason": cost.reason or "COST_FACTS_NOT_READY",
                "manual_mark_result": seat_facts.has_manual_mark,
                "seat_facts_status": seat_facts.status,
                "probe_executed": cost.probe_executed,
                "pricing_called": cost.pricing_called, "quote": None,
                "cost_facts": cost.model_dump(mode="json"),
            }
        pricing = self._wanda_pricing.price(
            cost, show, seat_facts, rules,
            ticket_count=(ticket_count if seat_facts.seat_request_type == "WPLUS_AREA" else None),
        )
        if pricing.status != "PRICED":
            return {
                "status": "PRICING_UNAVAILABLE", "reason": pricing.reason,
                "manual_mark_result": seat_facts.has_manual_mark,
                "seat_facts_status": seat_facts.status,
                "cost_facts_status": cost.status,
                "pricing_status": pricing.status,
                "cost_facts": cost.model_dump(mode="json"),
                "pricing_result": pricing.model_dump(mode="json"),
            }
        record = self._quotes.persist(
            pricing, show, tenant_id=identity["tenant_id"], shop_id=identity["shop_id"],
            buyer_id=identity["buyer_id"], chat_id=identity["chat_id"],
            wanda_city_id=route.wanda_city_id or "", cinema_name=route.wanda_cinema_name or recognition.cinema_text or "",
            purchase_context_id=identity["purchase_context_id"], request_id=f'{identity["event_id"]}:canonical-quote',
            event_id=identity["event_id"], recognition_id=recognition.provider_recognize_id,
            message_id=identity.get("message_id"),
            has_manual_mark=seat_facts.has_manual_mark if selected_seats else None,
            mark_image_reference=image_url if manual_mark is True else None,
            mark_message_id=identity.get("message_id") if manual_mark is True else None,
            original_selected_seats=seat_facts.exact_seats if same_type_reference else None,
            same_type_reference=seat_facts.same_type_reference if same_type_reference else None,
        )
        return {
            "status": "QUOTED", "route": "WANDA_SELF", "quote": record,
            "manual_mark_result": seat_facts.has_manual_mark if selected_seats else None,
            "seat_facts_status": seat_facts.status,
        }

    async def _quote_liangpiao(
        self, recognition: Any, route: Any, identity: dict[str, str], rules: PricingRulesSnapshot,
        *, ticket_mode: str = "STANDARD", area_quote_strategy: str | None = None,
    ) -> dict[str, Any]:
        ids = _liangpiao_final_ids(recognition.raw_provider_result)
        cinema_id = ids.get("cinema_id")
        show_id = ids.get("show_id")
        if not cinema_id or not recognition.selected_seats:
            return {"status": "LIANGPIAO_FACTS_INCOMPLETE", "reason": "PROVIDER_IDS_OR_SELECTED_SEATS_REQUIRED"}
        seats = _recognition_seats(recognition.selected_seats)
        if not seats:
            return {"status": "LIANGPIAO_FACTS_INCOMPLETE", "reason": "SEAT_COORDINATES_REQUIRED"}
        price_mode = rules.liangpiao_price_mode
        request = {
            "tenant_id": identity["tenant_id"], "conversation_id": identity["chat_id"],
            "cinema_id": int(cinema_id), "show_id": show_id,
            "cinema_name": recognition.cinema_text, "movie_name": recognition.movie,
            "show_date": recognition.show_date, "showtime_start": recognition.start_time,
            "hall_name": recognition.hall, "seats": seats, "price_mode": price_mode,
            "ticket_mode": ticket_mode, "area_quote_strategy": area_quote_strategy,
            "trace_id": f'{identity["event_id"]}:liangpiao-preflight',
        }
        try:
            preflight = await self._liangpiao_quote.quote(request)
        except Exception as error:
            return {"status": "LIANGPIAO_PREFLIGHT_UNAVAILABLE", "reason": type(error).__name__}
        provider_response = preflight.snapshot.get("preflight_response") if isinstance(preflight.snapshot, dict) else None
        if not isinstance(provider_response, dict):
            return {"status": "LIANGPIAO_PREFLIGHT_INVALID", "reason": "PREFLIGHT_RESPONSE_MISSING"}
        facts = self._liangpiao_facts.from_preflight(
            provider_response,
            request={"showId": preflight.show_id, "priceMode": preflight.price_mode,
                     "seats": [item.model_dump(mode="json") for item in preflight.seats]},
        )
        pricing = self._engine.quote(facts, rules)
        if pricing.total_quote_cents is None:
            return {"status": "PRICING_UNAVAILABLE", "reason": "LIANGPIAO_TOTAL_MISSING"}
        record = self._quotes.persist_liangpiao(
            pricing, tenant_id=identity["tenant_id"], shop_id=identity["shop_id"],
            buyer_id=identity["buyer_id"], chat_id=identity["chat_id"],
            purchase_context_id=identity["purchase_context_id"],
            request_id=f'{identity["event_id"]}:canonical-quote', city=recognition.city_text or "良票",
            cinema_id=str(cinema_id), cinema_name=recognition.cinema_text or "良票影院",
            movie=recognition.movie, quote_date=recognition.show_date,
            showtime_start=recognition.start_time, hall=recognition.hall or "未知影厅",
            show_id=preflight.show_id, movie_id=ids.get("movie_id"), selected_seats=[
                item.model_dump(mode="json") for item in preflight.seats
            ], event_id=identity["event_id"], recognition_id=recognition.provider_recognize_id,
            message_id=identity.get("message_id"), provider_snapshot_id=preflight.quote_id,
            provider_preflight_expires_at=_provider_expiry(provider_response),
            ticket_mode=ticket_mode,
            area_quote_strategy=area_quote_strategy,
            provider_market_amount_fen=facts.provider_market_amount_cents,
            pricing_rule_revision=rules.revision,
            seller_pricing_rule_version=rules.rule_version,
        )
        return {"status": "QUOTED", "route": pricing.quote_route, "quote": record}

    def _rules(self) -> PricingRulesSnapshot:
        value = self._rules_provider() if callable(self._rules_provider) else self._rules_provider
        if isinstance(value, PricingRulesSnapshot):
            return value
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if not isinstance(value, dict):
            value = {}
        return PricingRulesSnapshot.from_mapping(value)

    @staticmethod
    def _event_identity(body: dict[str, Any]) -> dict[str, str] | None:
        envelope = body.get("envelope") if isinstance(body.get("envelope"), dict) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        session = body.get("session") if isinstance(body.get("session"), dict) else {}
        def pick(*values: Any) -> str:
            return next((str(value).strip() for value in values if str(value or "").strip()), "")
        result = {
            "event_id": pick(envelope.get("id")), "tenant_id": pick(envelope.get("tenantId")),
            "shop_id": pick(session.get("accountUnb"), payload.get("accountUnb")),
            "buyer_id": pick(session.get("peerUnb"), payload.get("peerUnb")),
            "chat_id": pick(session.get("chatId"), payload.get("chatId")),
            "message_id": pick(payload.get("messageId"), payload.get("remoteMessageId")),
        }
        if not all(result[key] for key in ("event_id", "tenant_id", "shop_id", "buyer_id", "chat_id")):
            return None
        result["purchase_context_id"] = pick(payload.get("itemId"), payload.get("item_id"), f'chat:{result["chat_id"]}')
        return result


def _liangpiao_final_ids(raw: Any) -> dict[str, str]:
    from collections.abc import Mapping
    data = raw.get("data") if isinstance(raw, Mapping) and isinstance(raw.get("data"), Mapping) else {}
    final = data.get("finalResults") if isinstance(data.get("finalResults"), Mapping) else {}
    def pick(*names: str) -> str | None:
        for name in names:
            value = str(final.get(name) or "").strip()
            if value:
                return value
        return None
    return {
        "cinema_id": pick("cinemaId", "cinema_id"),
        "movie_id": pick("movieId", "movie_id", "filmId", "film_id"),
        "show_id": pick("showId", "show_id"),
    }


def _recognition_seats(values: list[str]) -> list[Any]:
    import re
    from app.selected_seat_quote_service import SelectedSeat
    result: list[Any] = []
    for value in values:
        match = re.search(r"(\d+)\s*[排行]\s*(\d+)\s*[座號号]", str(value))
        if not match:
            return []
        result.append(SelectedSeat(row_no=int(match.group(1)), col_no=int(match.group(2)), seat_no=str(value)))
    return result


def _provider_expiry(payload: dict[str, Any]) -> str | None:
    for key in ("expiresAt", "expires_at", "quoteExpiresAt", "quote_expires_at"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _identity(tenant_id: str, shop_id: str, buyer_id: str, chat_id: str) -> tuple[str, str, str, str]:
    values = tuple(_required(value, field) for value, field in zip(
        (tenant_id, shop_id, buyer_id, chat_id),
        ("tenant_id", "shop_id", "buyer_id", "chat_id"),
        strict=True,
    ))
    return values  # type: ignore[return-value]


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200:
        raise ValueError(f"{field}_invalid")
    return text


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("datetime_invalid")
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _project(record: dict[str, Any] | None) -> dict[str, Any] | None:
    return dict(record) if record is not None else None


def _validate_manual_input(manual: ManualQuoteInput) -> None:
    if not isinstance(manual, ManualQuoteInput):
        raise ValueError("manual_quote_input_invalid")
    if manual.quote_source != MANUAL_OPERATOR_SOURCE:
        raise ValueError("manual_quote_source_invalid")
    if manual.provider_route not in {None, "WANDA_SELF", "LIANGPIAO"}:
        raise ValueError("manual_quote_provider_route_invalid")
    if manual.price_basis not in {"UNIT", "TOTAL"}:
        raise ValueError("manual_quote_price_basis_invalid")
    for field in (
        "tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id",
        "request_id", "city", "cinema", "movie", "quote_date", "showtime_start",
        "hall", "seat_display", "quote_scope", "seat_zone_type",
    ):
        _required(getattr(manual, field), field)
    if manual.event_id and manual.event_id == manual.request_id:
        raise ValueError("request_id_event_id_must_be_distinct")
    for field in ("unit_sell_price_fen", "total_sell_price_fen"):
        value = getattr(manual, field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise ValueError(f"manual_quote_{field}_invalid")
    if manual.ticket_count is not None and (
        not isinstance(manual.ticket_count, int)
        or isinstance(manual.ticket_count, bool)
        or not 1 <= manual.ticket_count <= 20
    ):
        raise ValueError("manual_quote_ticket_count_invalid")
    if manual.price_basis == "UNIT":
        if manual.unit_sell_price_fen is None:
            raise ValueError("manual_quote_unit_price_required")
        if manual.total_sell_price_fen is not None:
            raise ValueError("manual_quote_unit_total_must_be_derived")
    elif manual.ticket_count is None or manual.total_sell_price_fen is None:
        raise ValueError("manual_quote_total_requires_count_and_total")
