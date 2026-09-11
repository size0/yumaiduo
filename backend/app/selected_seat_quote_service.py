from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .models import PricingRulesUpdate
from .rule_contracts import GateEvidence, ReplyPlan


class QuoteServiceError(RuntimeError):
    """A deterministic, buyer-safe failure from the selected-seat quote gate."""

    def __init__(self, code: str, message: str, *, manual_hold: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.manual_hold = manual_hold


class SelectedSeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_no: int = Field(ge=1, le=999)
    col_no: int = Field(ge=1, le=999)
    seat_no: str | None = Field(default=None, max_length=80)
    area_id: str | None = Field(default=None, max_length=80)

    @property
    def key(self) -> tuple[int, int, str | None, str | None]:
        return self.row_no, self.col_no, self.seat_no, self.area_id


class SelectedSeatQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=200)
    conversation_id: str = Field(min_length=1, max_length=200)
    cinema_id: int = Field(gt=0)
    show_id: str | None = Field(default=None, max_length=120)
    cinema_name: str | None = Field(default=None, max_length=240)
    movie_name: str | None = Field(default=None, max_length=160)
    show_date: str | None = Field(default=None, max_length=40)
    showtime_start: str | None = Field(default=None, max_length=20)
    hall_name: str | None = Field(default=None, max_length=120)
    seats: list[SelectedSeat] = Field(min_length=1, max_length=6)
    ticket_mode: Literal["STANDARD", "FAST", "FLASH"] = "STANDARD"
    price_mode: Literal["FIXED", "LIMIT"] = "FIXED"
    # Liangpiao area pricing strategy.  Keep this optional so existing shops
    # continue to use the provider default, while allowing preflight and
    # order/create to carry an explicit, auditable choice.
    area_quote_strategy: Literal["AVERAGE", "HIGHEST", "LOWEST"] | None = None
    generation: int = Field(default=1, ge=1)
    trace_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=120)


class SelectedSeatQuoteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    code: str = "OK"
    quote_id: str = Field(min_length=1, max_length=160)
    quote_hash: str = Field(min_length=64, max_length=64)
    show_id: str = Field(min_length=1, max_length=120)
    price_mode: Literal["FIXED", "LIMIT"] = "FIXED"
    seats: list[SelectedSeat] = Field(min_length=1, max_length=6)
    provider_amount_fen: int = Field(gt=0)
    buyer_amount_fen: int = Field(gt=0)
    max_price_fen: int | None = Field(default=None, gt=0)
    operator_pricing_applied: bool = False
    operator_markup_percent: float | None = None
    pricing_rule_version: str = Field(min_length=1, max_length=120)
    expires_at: datetime
    preflight_verified: bool = True
    generation: int = Field(ge=1)
    trace_id: str = Field(min_length=1, max_length=120)
    snapshot: dict[str, Any] = Field(default_factory=dict)
    reply_plan: dict[str, Any] = Field(default_factory=dict)


class LiangpiaoQuoteClient(Protocol):
    async def show_list(self, **kwargs: Any) -> Mapping[str, Any]: ...
    async def seat_list(self, **kwargs: Any) -> Mapping[str, Any]: ...
    async def order_preflight(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _value(source: object, *names: str) -> Any:
    if isinstance(source, Mapping):
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
    else:
        for name in names:
            value = getattr(source, name, None)
            if value is not None:
                return value
    return None


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _fen(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # provider contracts use integer fen
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _seat(value: object) -> SelectedSeat | None:
    row = _value(value, "rowNo", "row_no", "row")
    col = _value(value, "colNo", "col_no", "column", "col")
    if row is None or col is None:
        label = _text(_value(value, "seatNo", "seat_no", "seatName", "seat_number", "name")) or ""
        import re
        match = re.search(r"(\d+)\s*[排行]\s*(\d+)\s*[座號号]", label)
        if match:
            row, col = match.groups()
        else:
            return None
    try:
        return SelectedSeat(
            row_no=int(row), col_no=int(col),
            seat_no=_text(_value(value, "seatNo", "seat_no", "seatName", "seat_number", "name")),
            area_id=_text(_value(value, "areaId", "area_id", "areaCode", "area_code")),
        )
    except (TypeError, ValueError):
        return None


def _items(data: Mapping[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, Mapping):
            nested = value.get("items") or value.get("shows") or value.get("seats")
            if isinstance(nested, list):
                return nested
    return []


class SelectedSeatQuoteService:
    """Resolve an exact official show/seat set, preflight it and persist a snapshot."""

    def __init__(self, client: LiangpiaoQuoteClient, *, quote_store: object | None = None,
                 ttl_seconds: int = 600, pricing_rule_version: str = "liangpiao-server",
                 pricing_rules: object | None = None) -> None:
        self._client = client
        self._quote_store = quote_store
        self._ttl_seconds = max(60, min(int(ttl_seconds), 1800))
        self._pricing_rule_version = pricing_rule_version
        self._pricing_rules_provider = pricing_rules

    async def quote(self, request: SelectedSeatQuoteRequest | Mapping[str, Any]) -> SelectedSeatQuoteResult:
        req = request if isinstance(request, SelectedSeatQuoteRequest) else SelectedSeatQuoteRequest.model_validate(request)
        if len({seat.key for seat in req.seats}) != len(req.seats):
            raise QuoteServiceError("LIANGPIAO_SEAT_DUPLICATE", "选座列表包含重复座位。")
        show_id = req.show_id
        show = None
        if not show_id:
            if not req.show_date:
                raise QuoteServiceError(
                    "LIANGPIAO_SHOW_DATE_MISSING", "良票精确报价需要可核验的场次日期。",
                )
            # Liangpiao's show/list contract only accepts cinemaId, showDate,
            # and optional movieId. Movie name, start time, and hall are
            # verified locally against the authoritative response below.
            show_data = await self._client.show_list(
                cinema_id=req.cinema_id, show_date=req.show_date,
            )
            matches = self._match_shows(_items(show_data, "list", "shows", "items", "data"), req)
            if len(matches) != 1:
                code = "LIANGPIAO_SHOW_NOT_FOUND" if not matches else "LIANGPIAO_SHOW_AMBIGUOUS"
                raise QuoteServiceError(code, "无法唯一确认当前影片场次。")
            show = matches[0]
            show_id = _text(_value(show, "showId", "show_id", "id"))
        if not show_id:
            raise QuoteServiceError("LIANGPIAO_SHOW_ID_MISSING", "场次缺少官方 showId。")

        # The seat/list contract is keyed by the provider show ID only. The
        # Liangpiao cinema ID was used to resolve the show, but must not be
        # sent to this endpoint as a second identifier.
        seat_data = await self._client.seat_list(show_id=show_id)
        provider_seats = [_seat(item) for item in _items(seat_data, "seats", "seatList", "items", "data")]
        provider_seats = [item for item in provider_seats if item is not None]
        selected: list[SelectedSeat] = []
        for wanted in req.seats:
            # Providers may label the same column as “座” or “列”. The
            # numeric row/column coordinates are the stable identity; a
            # display-label spelling difference must not make an available
            # seat look unavailable.
            matches = [item for item in provider_seats if item.row_no == wanted.row_no and item.col_no == wanted.col_no]
            if wanted.area_id:
                matches = [item for item in matches if item.area_id == wanted.area_id]
            if len(matches) != 1:
                raise QuoteServiceError("LIANGPIAO_SEAT_UNAVAILABLE", "已选座位无法在实时座位图中确认。")
            raw = next((item for item in _items(seat_data, "seats", "seatList", "items", "data")
                        if _seat(item) == matches[0]), None)
            status = str(_value(raw, "status", "saleStatus", "sale_status", "state") or "").lower()
            if status in {"sold", "unavailable", "occupied", "locked", "不可售", "已售"} or _value(raw, "available", "isAvailable") is False:
                raise QuoteServiceError("LIANGPIAO_SEAT_UNAVAILABLE", "已选座位当前不可售。")
            selected.append(matches[0])

        # Keep the provider request limited to the documented preflight
        # contract. Generation and trace ID remain internal audit facts in the
        # snapshot/result; cinemaId is intentionally not accepted here.
        preflight_payload = {
            "show_id": show_id,
            "seats": [self._seat_payload(item) for item in selected],
            "ticket_mode": req.ticket_mode, "price_mode": req.price_mode,
        }
        if req.area_quote_strategy is not None:
            preflight_payload["area_quote_strategy"] = req.area_quote_strategy
        preflight = await self._client.order_preflight(**preflight_payload)
        available = _value(preflight, "available")
        if available is not True:
            reason = _text(_value(preflight, "reason", "message", "msg"))
            raise QuoteServiceError(
                "LIANGPIAO_PREFLIGHT_FAILED",
                reason or "良票官方预检未确认所选座位可售。",
            )
        if _value(preflight, "ok", "success") is False or str(_value(preflight, "status") or "").lower() in {"failed", "error"}:
            raise QuoteServiceError("LIANGPIAO_PREFLIGHT_FAILED", "良票预检未通过。")
        estimated = _value(preflight, "estimated")
        total_amount = _fen(_value(preflight, "totalAmount", "total_amount_fen", "totalAmountFen"))
        estimate_amount = _fen(_value(
            preflight, "estimateAmount", "estimate_amount_fen", "estimateAmountFen",
        ))
        # FIXED uses totalAmount as the payable one-price amount. LIMIT shows
        # estimateAmount to the buyer while retaining totalAmount as the
        # provider-side upper limit for order/create.
        if req.price_mode == "FIXED":
            buyer_amount = total_amount or _fen(_value(
                preflight, "buyerAmountFen", "buyer_amount_fen", "buyerAmount",
                "totalPayPriceFen", "totalPayPrice",
            ))
            max_price = buyer_amount
            if estimated is True:
                raise QuoteServiceError("LIANGPIAO_PREFLIGHT_INVALID", "良票预检只返回预估金额，无法确认固定报价。")
        else:
            buyer_amount = estimate_amount
            max_price = total_amount or estimate_amount
            if buyer_amount is None:
                raise QuoteServiceError("LIANGPIAO_PREFLIGHT_INVALID", "良票预检未返回有效预估金额。")
        provider_base_amount = buyer_amount
        market_amount = _fen(_value(preflight, "marketAmount", "market_amount_fen", "marketAmountFen"))
        buyer_amount, operator_markup_percent = self._apply_operator_pricing(
            buyer_amount, market_amount, req.price_mode,
        )
        provider_amount = _fen(_value(
            preflight, "providerAmountFen", "provider_amount_fen", "providerAmount",
            "supplierAmountFen",
        )) or provider_base_amount
        if provider_amount is None or buyer_amount is None:
            raise QuoteServiceError("LIANGPIAO_PREFLIGHT_INVALID", "良票预检未返回有效金额。")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._ttl_seconds)
        snapshot = {
            "tenant_id": req.tenant_id, "conversation_id": req.conversation_id,
            "cinema_id": req.cinema_id, "show_id": show_id,
            "seats": [self._seat_payload(item) for item in selected],
            "ticket_mode": req.ticket_mode, "price_mode": req.price_mode,
            "area_quote_strategy": req.area_quote_strategy,
            "preflight_request": preflight_payload, "preflight_response": dict(preflight),
            "provider_amount_fen": provider_amount, "buyer_amount_fen": buyer_amount,
            "provider_base_amount_fen": provider_base_amount,
            "market_amount_fen": market_amount,
            "max_price_fen": max_price,
            "operator_pricing_applied": operator_markup_percent is not None,
            "operator_markup_percent": operator_markup_percent,
            "pricing_rule_version": _text(_value(preflight, "pricingRuleVersion", "pricing_rule_version")) or self._pricing_rule_version,
            "provider_quote_id": _text(_value(preflight, "quoteId", "quote_id")),
            "provider_quote_hash": _text(_value(preflight, "quoteHash", "quote_hash")),
            "provider_preflight_expires_at": _text(_value(
                preflight, "expiresAt", "expires_at", "quoteExpiresAt", "quote_expires_at",
            )),
            "generation": req.generation, "trace_id": req.trace_id, "expires_at": expires_at.isoformat(),
        }
        quote_hash = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        # The preflight evidence store keys by quote_id.  Derive that key from
        # the canonical request identity so a retried event reuses one evidence
        # row instead of creating another random provider snapshot.
        quote_identity = {
            "tenant_id": req.tenant_id, "conversation_id": req.conversation_id,
            "show_id": show_id, "seats": preflight_payload["seats"],
            "ticket_mode": req.ticket_mode, "price_mode": req.price_mode,
            "area_quote_strategy": req.area_quote_strategy, "generation": req.generation,
        }
        quote_id = "lpq-" + hashlib.sha256(
            json.dumps(quote_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]
        plan = ReplyPlan(
            template_key="flow.quote.ready", template_version=1,
            variables={"showtime_summary": show_id, "ticket_count": len(selected),
                       "quoted_total_amount": f"{buyer_amount / 100:.2f}"},
            protected_facts={"show_id": show_id, "seats": [self._seat_payload(item) for item in selected],
                             "buyer_amount_fen": buyer_amount, "quote_hash": quote_hash},
            required_phrases=[], optional_ai_text=None, send_policy="replace_stale",
            gate_evidence={
                "showtime_summary": GateEvidence(source="valid_quote_record", value=show_id, reference_id=quote_id),
                "ticket_count": GateEvidence(source="valid_quote_record", value=len(selected), reference_id=quote_id),
                "quoted_total_amount": GateEvidence(source="valid_quote_record", value=f"{buyer_amount / 100:.2f}", reference_id=quote_id),
            },
        )
        result = SelectedSeatQuoteResult(
            quote_id=quote_id, quote_hash=quote_hash, show_id=show_id,
            price_mode=req.price_mode, seats=selected, provider_amount_fen=provider_amount, buyer_amount_fen=buyer_amount,
            max_price_fen=max_price, operator_pricing_applied=operator_markup_percent is not None,
            operator_markup_percent=operator_markup_percent,
            pricing_rule_version=snapshot["pricing_rule_version"], expires_at=expires_at,
            generation=req.generation, trace_id=req.trace_id, snapshot=snapshot,
            reply_plan=plan.model_dump(mode="json"),
        )
        save = getattr(self._quote_store, "save_selected_seat_quote", None)
        if callable(save):
            save({**result.model_dump(mode="json"), "tenant_id": req.tenant_id,
                  "conversation_id": req.conversation_id})
        return result

    def _apply_operator_pricing(
        self, base_amount: int | None, market_amount: int | None, price_mode: str = "LIMIT",
    ) -> tuple[int | None, float | None]:
        if base_amount is None or self._pricing_rules_provider is None:
            return base_amount, None
        source = self._pricing_rules_provider() if callable(self._pricing_rules_provider) else self._pricing_rules_provider
        rules = PricingRulesUpdate.model_validate(source)
        bands = rules.liangpiao_rules
        if str(price_mode).upper() == "FIXED":
            # ``model_fields_set`` distinguishes an explicit empty fixed
            # policy from an old caller that does not know this field yet.
            bands = (
                rules.liangpiao_fixed_rules
                if "liangpiao_fixed_rules" in rules.model_fields_set
                else rules.liangpiao_rules
            )
        if not rules.enabled or not bands:
            return base_amount, None
        if market_amount is None or market_amount <= 0:
            raise QuoteServiceError("LIANGPIAO_PRICING_BASE_MISSING", "良票预检缺少原价，无法按后台规则计算报价。")
        discount_percent = Decimal(base_amount * 100) / Decimal(market_amount)
        selected = None
        for index, band in enumerate(bands):
            maximum = Decimal(str(band.max_discount_percent))
            if Decimal(str(band.min_discount_percent)) <= discount_percent < maximum or (
                index == len(bands) - 1 and discount_percent <= maximum
            ):
                selected = band
                break
        if selected is None:
            raise QuoteServiceError("LIANGPIAO_PRICING_BAND_MISSING", "良票预估价未匹配后台报价区间。")
        multiplier = Decimal(100 + selected.markup_percent) / Decimal(100)
        raw = Decimal(base_amount) * multiplier
        increment = Decimal(rules.rounding_increment_cents)
        rounded = int((raw / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * increment)
        if rounded <= 0:
            raise QuoteServiceError("LIANGPIAO_PRICING_RESULT_INVALID", "良票后台规则计算结果无效。")
        return rounded, float(selected.markup_percent)

    @staticmethod
    def _seat_payload(seat: SelectedSeat) -> dict[str, Any]:
        return {"rowNo": seat.row_no, "colNo": seat.col_no, "seatNo": seat.seat_no, "areaId": seat.area_id}

    @staticmethod
    def _show_date(value: object) -> str | None:
        raw = _text(_value(value, "date", "showDate", "show_date", "startTime"))
        if not raw:
            return None
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
        return date_match.group(1) if date_match else raw

    @staticmethod
    def _show_start_time(value: object) -> str | None:
        raw = _text(_value(value, "startTime", "showtimeStart", "showtime_start", "time"))
        if not raw:
            return None
        time_match = re.search(r"(?:T|\s|^)(\d{1,2}):(\d{2})", raw)
        return f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else raw

    @classmethod
    def _match_shows(cls, values: list[Any], request: SelectedSeatQuoteRequest) -> list[Any]:
        matches = []
        for value in values:
            if request.movie_name and _text(_value(value, "film", "movieName", "movie_name", "movie")) != request.movie_name:
                continue
            if request.show_date and cls._show_date(value) != request.show_date:
                continue
            if request.showtime_start and cls._show_start_time(value) != request.showtime_start:
                continue
            if request.hall_name and _text(_value(value, "hall", "hallName", "hall_name")) != request.hall_name:
                continue
            matches.append(value)
        return matches
