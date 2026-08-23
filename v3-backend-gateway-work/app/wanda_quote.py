from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from fastapi import HTTPException

from .local_catalog import LocalWandaCatalog
from .wanda_quote_gateway import LocalTicketGateway, TicketGateway, _gateway_auth_headers, _showtime_start
from .wanda_showtime_matcher import ShowtimeMatcher, _match_has_cinema, is_wanda_cinema_name
from .wanda_quote_diagnostics import (
    _diagnostic_failure,
    _match_diagnostics,
    _quote_failure_code,
    _quote_failure_message,
)
from .wanda_quote_domain import (
    REGULAR_SEAT_MARKUP_CENTS, SeatFact, _all_seats_released, _bounded_quote_for_seat,
    _data, _identifier, _locked_offer_unit_cents, _partition, _positive_int, _requested_zone,
    _round_quote_cents_to_tenth, _seat_facts, _select_seats, _text, _unit_quote_cents,
    _wplus_probe_candidates,
)
from .wanda_direct_gateway import DirectGatewayError, build_wanda_direct_gateway_from_env
from .schemas import AvailableWplusSeatsResponse, QuoteRealtimeRequest, QuoteRealtimeResponse, QuoteShowtimeResolveResponse, Recognition, SeatQuote, SeatZoneType


RELEASE_RECHECK_DELAYS_SECONDS: Final = (0.0, 2.0, 5.0)
_SHOWTIME_LOCKS: dict[str, asyncio.Lock] = {}


def _showtime_lock(showtime_id: str) -> asyncio.Lock:
    return _SHOWTIME_LOCKS.setdefault(showtime_id, asyncio.Lock())


@dataclass(frozen=True)
class LockedMemberOffer:
    unit_price_cents: int
    pricing_account_ref: str | None = None


class RealtimeQuoteService:
    def __init__(
        self,
        gateway: TicketGateway | None = None,
        *,
        cinema_catalog: LocalWandaCatalog | None = None,
        allow_friday_member_day: bool | None = None,
        direct_lock_gateway: Any | None = None,
    ) -> None:
        self._gateway = gateway or LocalTicketGateway()
        self._showtime_matcher = ShowtimeMatcher(cinema_catalog)
        self._direct_lock_gateway = direct_lock_gateway if direct_lock_gateway is not None else build_wanda_direct_gateway_from_env()
        self._allow_friday_member_day = (
            allow_friday_member_day
            if allow_friday_member_day is not None
            else os.getenv("WANDA_ALLOW_FRIDAY_MEMBER_DAY", "false").lower() == "true"
        )

    async def aclose(self) -> None:
        """Drain bounded delayed seat-release checks before service shutdown."""
        wait_for_rechecks = getattr(self._direct_lock_gateway, "wait_for_background_rechecks", None)
        if callable(wait_for_rechecks):
            await wait_for_rechecks()

    async def resolve_showtime(self, recognition: Recognition) -> QuoteShowtimeResolveResponse:
        """Resolve the joint showtime identity without reading seats or creating a temporary order."""
        recognition, joint_match_required = self._showtime_matcher.catalog_match_input(recognition)
        try:
            gateway = await self._gateway.for_quote()
            match, recognition, _showtime_id, cinema_id, matched_cinema_name = await self._showtime_matcher.match_joint_identity(
                gateway, recognition, joint_match_required,
            )
        except HTTPException as error:
            matched = locals().get("match") or getattr(error, "match_result", None)
            if joint_match_required and isinstance(matched, Mapping) and not _match_has_cinema(matched):
                error = HTTPException(status_code=422, detail="影院无法在官方影院库唯一匹配")
            raise _diagnostic_failure(error, step="resolve_showtime", recognition=recognition, match=matched) from error
        resolved = recognition.model_copy(update={"cinema": matched_cinema_name}) if matched_cinema_name else recognition
        return QuoteShowtimeResolveResponse(recognition=resolved, matched_cinema_name=matched_cinema_name)

    async def available_wplus_seats(self, recognition: Recognition, row: int) -> AvailableWplusSeatsResponse:
        """List current read-only W+ availability for one explicitly requested row."""
        recognition, joint_match_required = self._showtime_matcher.catalog_match_input(recognition)
        try:
            gateway = await self._gateway.for_quote()
            match, recognition, showtime_id, cinema_id, matched_cinema_name = await self._showtime_matcher.match_joint_identity(
                gateway, recognition, joint_match_required,
            )
            realtime = await gateway.realtime_seats(showtime_id)
        except HTTPException as error:
            matched = locals().get("match") or getattr(error, "match_result", None)
            if joint_match_required and isinstance(matched, Mapping) and not _match_has_cinema(matched):
                error = HTTPException(status_code=422, detail="影院无法在官方影院库唯一匹配")
            raise _diagnostic_failure(
                error,
                step="available_seats",
                recognition=recognition,
                match=matched,
                realtime=locals().get("realtime"),
            ) from error
        wplus_facts = [seat for seat in _seat_facts(realtime) if seat.zone_type is SeatZoneType.WPLUS]
        row_prefix = f"{row}排"
        labels = sorted(
            {seat.label for seat in wplus_facts if seat.label.startswith(row_prefix)},
            key=lambda label: int(re.search(r"排(\d{1,3})座", label).group(1)) if re.search(r"排(\d{1,3})座", label) else 10_000,
        )
        return AvailableWplusSeatsResponse(
            row=row,
            seats=labels[:30],
            available_count=len(labels),
            wplus_offer_available=any(seat.wplus_member_price_cents is not None for seat in wplus_facts),
            matched_cinema_name=matched_cinema_name,
        )

    async def _locked_member_offer(
        self,
        gateway: TicketGateway,
        *,
        showtime_id: str,
        cinema_id: str,
        seats: Sequence[SeatFact],
        stage_timings: dict[str, int] | None = None,
    ) -> LockedMemberOffer:
        if not seats:
            raise HTTPException(status_code=422, detail="未找到足够的 W+座位用于临时锁座核价")
        partition = _partition(seats)
        if self._direct_lock_gateway is not None:
            direct_started = time.perf_counter()
            try:
                result = await self._direct_lock_gateway.probe_activity_offers({
                    "cinema_id": cinema_id,
                    "showtime_id": showtime_id,
                    "seat_ids": [seat.seat_id for seat in seats],
                    "seat_payloads": [
                        f"{seat.seat_id},{seat.original_price_cents},{seat.channel_fee_cents},0"
                        for seat in seats
                    ],
                    "partition": partition,
                    "total_price_cents": sum(seat.original_price_cents for seat in seats),
                })
                offers = result.get("offers") if isinstance(result, Mapping) else None
                if not isinstance(offers, Mapping) or result.get("release_verified") is not True:
                    raise DirectGatewayError("temporary_lock_release_unverified")
                member_unit = _locked_offer_unit_cents(
                    offers,
                    quantity=len(seats),
                    allow_friday=self._allow_friday_member_day,
                )
                pricing_account_ref = _text(result.get("pricing_account_ref"))
                if not re.fullmatch(r"[a-f0-9]{32}", pricing_account_ref):
                    raise DirectGatewayError("temporary_lock_state_unknown")
                if stage_timings is not None:
                    stage_timings["temporary_lock"] = round((time.perf_counter() - direct_started) * 1000)
                    stage_timings["available_offers"] = 0
                    stage_timings["cancel"] = 0
                    stage_timings["release_recheck"] = 0
                return LockedMemberOffer(member_unit, pricing_account_ref)
            except DirectGatewayError as error:
                if error.code == "temporary_lock_release_unverified":
                    raise HTTPException(status_code=502, detail="临时锁座未确认释放，已停止报价") from error
                if error.code in {"wplus_account_unavailable", "account_lease_unavailable"}:
                    raise HTTPException(status_code=422, detail="线上账号池没有可用的 W+ 会员账号") from error
                raise HTTPException(status_code=502, detail="万达临时锁座核价失败") from error
        order_id = ""
        quote_error: BaseException | None = None
        member_unit: int | None = None
        released = False
        async with _showtime_lock(showtime_id):
            try:
                lock_started = time.perf_counter()
                lock_result = await gateway.lock({
                    "showtime_id": showtime_id,
                    "seat_ids": [
                        f"{seat.seat_id},{seat.original_price_cents},{seat.channel_fee_cents},0"
                        for seat in seats
                    ],
                    "total_price": sum(seat.original_price_cents for seat in seats),
                    "mobile": gateway.account_mobile(),
                    "phone": gateway.account_mobile(),
                    "cinema_id": cinema_id,
                    "quantity": len(seats),
                    "partition": partition,
                    # Probe seat labels are deliberately not persisted or
                    # exposed as the buyer's selected fulfillment seats.
                    "seat_names": [],
                })
                if stage_timings is not None:
                    stage_timings["temporary_lock"] = round((time.perf_counter() - lock_started) * 1000)
                lock_data = _data(lock_result)
                order_id = _identifier(lock_data.get("orderId") or lock_data.get("order_id"))
                if not order_id:
                    raise HTTPException(status_code=502, detail="万达临时锁座失败，未生成核价订单")
                offers_started = time.perf_counter()
                offers = await gateway.available_offers(
                    cinema_id=cinema_id,
                    showtime_id=showtime_id,
                    partition=partition,
                    order_id=order_id,
                )
                if stage_timings is not None:
                    stage_timings["available_offers"] = round((time.perf_counter() - offers_started) * 1000)
                member_unit = _locked_offer_unit_cents(
                    offers,
                    quantity=len(seats),
                    allow_friday=self._allow_friday_member_day,
                )
            except BaseException as error:
                quote_error = error
            finally:
                if order_id:
                    try:
                        cancel_started = time.perf_counter()
                        cancelled = await asyncio.shield(gateway.cancel(order_id))
                        if stage_timings is not None:
                            stage_timings["cancel"] = round((time.perf_counter() - cancel_started) * 1000)
                        if cancelled:
                            release_started = time.perf_counter()
                            for delay_seconds in RELEASE_RECHECK_DELAYS_SECONDS:
                                if delay_seconds:
                                    await asyncio.shield(asyncio.sleep(delay_seconds))
                                refreshed = await asyncio.shield(gateway.realtime_seats(showtime_id))
                                released = _all_seats_released(refreshed, seats)
                                if released:
                                    break
                            if stage_timings is not None:
                                stage_timings["release_recheck"] = round((time.perf_counter() - release_started) * 1000)
                    except BaseException:
                        released = False
        if order_id and not released:
            raise HTTPException(status_code=502, detail="临时锁座未确认释放，已停止报价")
        if quote_error is not None:
            raise quote_error
        if member_unit is None:
            raise HTTPException(status_code=422, detail="未找到唯一可用的 W+会员专享优惠价")
        return LockedMemberOffer(member_unit)

    async def quote(
        self,
        request: QuoteRealtimeRequest,
        *,
        wplus_adjustment_cents: int = -290,
        wplus_member_price_threshold_cents: int = 6000,
        regular_adjustment_cents: int = REGULAR_SEAT_MARKUP_CENTS,
    ) -> QuoteRealtimeResponse:
        quote_started = time.perf_counter()
        timings_ms: dict[str, int] = {}
        recognition, joint_match_required = self._showtime_matcher.catalog_match_input(request.recognition)
        request = request.model_copy(update={"recognition": recognition})

        official_count = (
            len(recognition.official_selection.selected_seat_numbers)
            if recognition.official_selection.is_selected
            else 0
        )
        if request.ticket_count is not None and official_count and request.ticket_count != official_count:
            error = HTTPException(status_code=422, detail="文字张数与官方已选座张数不一致")
            raise _diagnostic_failure(error, step="validate_ticket_count", recognition=recognition)

        account_started = time.perf_counter()
        try:
            gateway = await self._gateway.for_quote()
            timings_ms["account"] = round((time.perf_counter() - account_started) * 1000)
        except HTTPException as error:
            raise _diagnostic_failure(error, step="account", recognition=recognition) from error
        # Hand-drawn marks neither select seats nor imply a ticket count.
        # When the platform has no confirmed selected seat, quote one W+
        # probe seat and ask the buyer how many tickets are needed.
        requested_count = request.ticket_count or official_count or None
        is_count_known = requested_count is not None
        match_started = time.perf_counter()
        try:
            match, recognition, showtime_id, cinema_id, matched_cinema_name = await self._showtime_matcher.match_joint_identity(
                gateway, request.recognition, joint_match_required,
            )
            timings_ms["match"] = round((time.perf_counter() - match_started) * 1000)
            request = request.model_copy(update={"recognition": recognition})
        except HTTPException as error:
            matched = locals().get("match") or getattr(error, "match_result", None)
            if joint_match_required and isinstance(matched, Mapping) and not _match_has_cinema(matched):
                error = HTTPException(status_code=422, detail="影院无法在官方影院库唯一匹配")
            raise _diagnostic_failure(error, step="match", recognition=recognition, match=matched) from error
        realtime_started = time.perf_counter()
        try:
            realtime = await gateway.realtime_seats(showtime_id)
            timings_ms["realtime_seats"] = round((time.perf_counter() - realtime_started) * 1000)
        except HTTPException as error:
            raise _diagnostic_failure(error, step="realtime_seats", recognition=recognition, match=match) from error
        all_seats = _seat_facts(realtime)
        selected_labels = request.recognition.official_selection.selected_seat_numbers
        selection_quantity = (
            len(selected_labels)
            if request.recognition.official_selection.is_selected and selected_labels
            else (requested_count or 1)
        )
        target_zone = (
            _requested_zone(request.recognition)
            if request.recognition.official_selection.is_selected
            else SeatZoneType.WPLUS
        )
        try:
            selected, is_exact, zone = _select_seats(request.recognition, all_seats, selection_quantity)
        except HTTPException as error:
            raise _diagnostic_failure(error, step="select_seats", recognition=recognition, match=match, realtime=realtime) from error
        if not is_exact and request.ticket_count is None and official_count == 0:
            requested_count = None
            is_count_known = False
        exact_zones = {seat.zone_type for seat in selected} if is_exact else set()
        exact_uniform_selection = is_exact and len(exact_zones) == 1
        offer_quantity = len(selected) if exact_uniform_selection else (1 if is_exact else (requested_count or 1))
        if exact_uniform_selection:
            # A verified regular/discount/premium selection can expose the same
            # named W+ member offer through available-offers. Probe those exact
            # seats instead of requiring an unrelated available W+ area.
            offer_seats = selected
        elif is_exact:
            # Mixed-zone selections retain the established single-offer probe;
            # never average a multi-zone available-offers total across seats.
            offer_seats = _wplus_probe_candidates(all_seats)[:offer_quantity]
        else:
            offer_seats = selected
        if len(offer_seats) != offer_quantity:
            error = HTTPException(status_code=422, detail="未找到足够的 W+座位用于临时锁座核价")
            raise _diagnostic_failure(error, step="select_offer_seats", recognition=recognition, match=match, realtime=realtime)
        locked_offer_started = time.perf_counter()
        try:
            locked_offer = await self._locked_member_offer(
                gateway,
                showtime_id=showtime_id,
                cinema_id=cinema_id,
                seats=offer_seats,
                stage_timings=timings_ms,
            )
            member_unit = locked_offer.unit_price_cents
            pricing_account_ref = locked_offer.pricing_account_ref
            timings_ms["locked_offer"] = round((time.perf_counter() - locked_offer_started) * 1000)
        except HTTPException as error:
            raise _diagnostic_failure(error, step="locked_offer", recognition=recognition, match=match, realtime=realtime) from error

        calculate_started = time.perf_counter()
        if is_exact:
            try:
                seat_quotes = [
                    SeatQuote(
                        seat_number=seat.label,
                        seat_zone_type=seat.zone_type,
                        original_price_cents=seat.original_price_cents,
                        member_price_cents=member_unit,
                        channel_fee_cents=seat.channel_fee_cents,
                        unit_quote_cents=_bounded_quote_for_seat(
                            SeatFact(
                                seat.seat_id, seat.area_code, seat.original_price_cents,
                                member_unit, seat.channel_fee_cents, seat.label, seat.zone_type,
                            ),
                            seat.zone_type,
                            wplus_adjustment_cents=wplus_adjustment_cents,
                            wplus_member_price_threshold_cents=wplus_member_price_threshold_cents,
                            regular_adjustment_cents=regular_adjustment_cents,
                        ),
                    )
                    for seat in selected
                ]
            except HTTPException as error:
                raise _diagnostic_failure(error, step="calculate_quote", recognition=recognition, match=match, realtime=realtime) from error
            quote_values = {item.unit_quote_cents for item in seat_quotes}
            member_values = {item.member_price_cents for item in seat_quotes}
            zone_values = {item.seat_zone_type for item in seat_quotes}
            timings_ms["calculate_quote"] = round((time.perf_counter() - calculate_started) * 1000)
            timings_ms["total"] = round((time.perf_counter() - quote_started) * 1000)
            return QuoteRealtimeResponse(
                quote_scope="exact_seats",
                seat_zone_type=next(iter(zone_values)) if len(zone_values) == 1 else SeatZoneType.UNKNOWN,
                member_unit_price_cents=next(iter(member_values)) if len(member_values) == 1 else None,
                unit_quote_cents=next(iter(quote_values)) if len(quote_values) == 1 else None,
                total_quote_cents=sum(item.unit_quote_cents for item in seat_quotes),
                channel_fee_total_cents=sum(item.channel_fee_cents for item in seat_quotes),
                seat_quotes=seat_quotes,
                ticket_count=len(seat_quotes),
                needs_ticket_count=False,
                pricing_source="万达临时锁座 available-offers + 后台报价规则",
                pricing_account_ref=pricing_account_ref,
                detail="官方已选座已逐座核对；临时锁座读取优惠后已取消并确认座位恢复可售",
                matched_cinema_name=matched_cinema_name,
                timings_ms=timings_ms,
            )

        probe_seat = selected[0]
        quote_zone = target_zone if target_zone is not SeatZoneType.UNKNOWN else zone
        try:
            unit_quote = _bounded_quote_for_seat(
                SeatFact(
                    probe_seat.seat_id, probe_seat.area_code, probe_seat.original_price_cents,
                    member_unit, probe_seat.channel_fee_cents, probe_seat.label, probe_seat.zone_type,
                ),
                quote_zone,
                wplus_adjustment_cents=wplus_adjustment_cents,
                wplus_member_price_threshold_cents=wplus_member_price_threshold_cents,
                regular_adjustment_cents=regular_adjustment_cents,
            )
        except HTTPException as error:
            raise _diagnostic_failure(error, step="calculate_quote", recognition=recognition, match=match, realtime=realtime) from error
        total = unit_quote * requested_count if is_count_known and requested_count is not None else None
        detail = "图中未显示官方已选座，按万达实时座位图和后台规则试价；请确认需要几张"
        timings_ms["calculate_quote"] = round((time.perf_counter() - calculate_started) * 1000)
        timings_ms["total"] = round((time.perf_counter() - quote_started) * 1000)
        return QuoteRealtimeResponse(
            quote_scope="area_probe",
            seat_zone_type=quote_zone,
            member_unit_price_cents=member_unit,
            unit_quote_cents=unit_quote,
            total_quote_cents=total,
            channel_fee_total_cents=probe_seat.channel_fee_cents * requested_count if is_count_known and requested_count is not None else None,
            ticket_count=requested_count,
            needs_ticket_count=not is_count_known,
            pricing_source="万达临时锁座 available-offers + 后台报价规则",
            pricing_account_ref=pricing_account_ref,
            detail=f"{detail}；临时试价座位已取消并确认恢复可售",
            matched_cinema_name=matched_cinema_name,
            timings_ms=timings_ms,
        )
