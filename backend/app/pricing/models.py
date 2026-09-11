from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping, Sequence

from .errors import PricingError

ProviderName = Literal["WANDA", "LIANGPIAO"]
QuoteRoute = Literal["WANDA_SELF", "LIANGPIAO_LIMIT", "LIANGPIAO_FIXED"]
QuoteScope = Literal["exact_seats", "area_preview", "area_probe"]
PriceMode = Literal["FIXED", "LIMIT"]


def _text(value: object) -> str:
    return str(value or "").strip()


@dataclass(frozen=True, slots=True)
class WandaPricingBand:
    min_discount_percent: float
    max_discount_percent: float
    fixed_adjustment_cents: int
    markup_percent: float | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.min_discount_percent < 100 or not 0 < self.max_discount_percent <= 100:
            raise PricingError("pricing_rules_invalid", "Wanda折扣区间必须位于0%至100%。")
        if self.max_discount_percent <= self.min_discount_percent:
            raise PricingError("pricing_rules_invalid", "Wanda折扣区间上限必须大于下限。")
        if not isinstance(self.fixed_adjustment_cents, int) or isinstance(self.fixed_adjustment_cents, bool):
            raise PricingError("pricing_rules_invalid", "Wanda固定调整必须是整数分。")


@dataclass(frozen=True, slots=True)
class LiangpiaoPricingBand:
    min_discount_percent: float
    max_discount_percent: float
    markup_percent: float

    def __post_init__(self) -> None:
        if not 0 <= self.min_discount_percent < 100 or not 0 < self.max_discount_percent <= 100:
            raise PricingError("pricing_rules_invalid", "良票折扣区间必须位于0%至100%。")
        if self.max_discount_percent <= self.min_discount_percent:
            raise PricingError("pricing_rules_invalid", "良票折扣区间上限必须大于下限。")


@dataclass(frozen=True, slots=True)
class PricingRulesSnapshot:
    """Explicit immutable V4 policy; it has no storage or provider side effects."""

    enabled: bool = False
    revision: int = 0
    rule_version: str = "pricing-unversioned"
    rounding_increment_cents: int = 10
    regular_adjustment_cents: int = 100
    wplus_member_price_threshold_cents: int = 6_000
    wplus_adjustment_cents: int = -290
    vip_fixed_cost_cents: int = 5_000  # Legacy/dead candidate retained for parity.
    vip_discount_threshold_cents: int = 6_000
    vip_high_price_discount_percent: int = 90
    vip_low_price_discount_cents: int = 200
    liangpiao_price_mode: PriceMode = "FIXED"
    wanda_rules: Sequence[WandaPricingBand] = ()
    liangpiao_rules: Sequence[LiangpiaoPricingBand] = ()
    liangpiao_fixed_rules: Sequence[LiangpiaoPricingBand] = ()
    liangpiao_fixed_rules_explicit: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 0:
            raise PricingError("pricing_rules_invalid", "规则revision无效。")
        if self.rounding_increment_cents not in {1, 10, 100}:
            raise PricingError("pricing_rules_invalid", "取整单位必须是1、10或100分。")
        if self.liangpiao_price_mode not in {"FIXED", "LIMIT"}:
            raise PricingError("pricing_rules_invalid", "良票price_mode无效。")
        object.__setattr__(self, "wanda_rules", tuple(self.wanda_rules))
        object.__setattr__(self, "liangpiao_rules", tuple(self.liangpiao_rules))
        object.__setattr__(self, "liangpiao_fixed_rules", tuple(self.liangpiao_fixed_rules))
        for name, bands in (("wanda_rules", self.wanda_rules), ("liangpiao_rules", self.liangpiao_rules), ("liangpiao_fixed_rules", self.liangpiao_fixed_rules)):
            previous = 0.0
            for band in bands:
                if abs(float(band.min_discount_percent) - previous) > 1e-9:
                    raise PricingError("pricing_rules_invalid", f"{name}区间必须连续覆盖0至100%。")
                previous = float(band.max_discount_percent)
            if bands and abs(previous - 100.0) > 1e-9:
                raise PricingError("pricing_rules_invalid", f"{name}区间必须覆盖到100%。")

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any]) -> "PricingRulesSnapshot":
        nested = source.get("rules")
        payload = nested if isinstance(nested, Mapping) else source
        wanda = tuple(WandaPricingBand(float(item["min_discount_percent"]), float(item["max_discount_percent"]), int(item.get("fixed_adjustment_cents", 0)), None if item.get("markup_percent") is None else float(item["markup_percent"])) for item in payload.get("wanda_rules", []) if isinstance(item, Mapping))
        liangpiao = tuple(LiangpiaoPricingBand(float(item["min_discount_percent"]), float(item["max_discount_percent"]), float(item["markup_percent"])) for item in payload.get("liangpiao_rules", []) if isinstance(item, Mapping))
        fixed = tuple(LiangpiaoPricingBand(float(item["min_discount_percent"]), float(item["max_discount_percent"]), float(item["markup_percent"])) for item in payload.get("liangpiao_fixed_rules", []) if isinstance(item, Mapping))
        return cls(
            enabled=bool(payload.get("enabled", False)),
            revision=int(source.get("revision", payload.get("revision", 0)) or 0),
            rule_version=_text(source.get("rule_version", payload.get("rule_version", "pricing-unversioned"))) or "pricing-unversioned",
            rounding_increment_cents=int(payload.get("rounding_increment_cents", 10)),
            regular_adjustment_cents=int(payload.get("regular_adjustment_cents", 100)),
            wplus_member_price_threshold_cents=int(payload.get("wplus_member_price_threshold_cents", 6_000)),
            wplus_adjustment_cents=int(payload.get("wplus_adjustment_cents", -290)),
            vip_fixed_cost_cents=int(payload.get("vip_fixed_cost_cents", 5_000)),
            vip_discount_threshold_cents=int(payload.get("vip_discount_threshold_cents", 6_000)),
            vip_high_price_discount_percent=int(payload.get("vip_high_price_discount_percent", 90)),
            vip_low_price_discount_cents=int(payload.get("vip_low_price_discount_cents", 200)),
            liangpiao_price_mode=str(payload.get("liangpiao_price_mode", "FIXED")).upper(),
            wanda_rules=wanda, liangpiao_rules=liangpiao, liangpiao_fixed_rules=fixed,
            liangpiao_fixed_rules_explicit=bool(payload.get("liangpiao_fixed_rules_explicit", "liangpiao_fixed_rules" in payload)),
        )


@dataclass(frozen=True, slots=True)
class PricingSeatFact:
    seat_id: str
    seat_label: str
    area_id: str = ""
    area_code: str = ""
    area_name: str = ""
    zone_type: str = "REGULAR"
    physical_wplus: bool = False
    original_price_cents: int | None = None
    member_cost_cents: int | None = None
    channel_fee_cents: int = 0
    availability_verified: bool = True
    cost_source: str = ""
    probe_result_id: str | None = None

    def __post_init__(self) -> None:
        if not _text(self.seat_id) or not _text(self.seat_label):
            raise PricingError("pricing_input_invalid", "座位必须有seat_id和seat_label。")
        if self.original_price_cents is not None and (not isinstance(self.original_price_cents, int) or isinstance(self.original_price_cents, bool) or self.original_price_cents <= 0):
            raise PricingError("pricing_input_invalid", "官方原价必须为正整数分。")
        if self.member_cost_cents is not None and (not isinstance(self.member_cost_cents, int) or isinstance(self.member_cost_cents, bool) or self.member_cost_cents <= 0):
            raise PricingError("pricing_input_invalid", "会员成本必须为正整数分。")
        if self.channel_fee_cents < 0:
            raise PricingError("pricing_input_invalid", "渠道费不能为负数。")
        if self.availability_verified is not True:
            raise PricingError("pricing_input_unverified", "PricingFacts只能包含已验证的可售事实。")
        if not _text(self.cost_source):
            raise PricingError("pricing_input_invalid", "成本事实必须有cost_source。")


@dataclass(frozen=True, slots=True)
class PricingFacts:
    """Verified provider facts only; no selling-price fields are permitted here."""

    provider: ProviderName
    show_id: str
    quote_route: QuoteRoute | None = None
    cinema_id: str | int | None = None
    hall_name: str = ""
    is_vip: bool = False
    quantity: int | None = None
    seats: Sequence[PricingSeatFact] = ()
    quote_scope: QuoteScope = "exact_seats"
    area_reference: PricingSeatFact | None = None
    price_mode: PriceMode | None = None
    ticket_mode: str = "STANDARD"
    area_quote_strategy: str | None = None
    provider_total_amount_cents: int | None = None
    provider_estimate_amount_cents: int | None = None
    provider_buyer_amount_cents: int | None = None
    provider_amount_cents: int | None = None
    provider_market_amount_cents: int | None = None
    provider_max_amount_cents: int | None = None
    provider_quote_id: str | None = None
    provider_quote_hash: str | None = None
    provider_pricing_rule_version: str | None = None
    preflight_verified: bool = False
    estimated: bool = False

    def __post_init__(self) -> None:
        provider = _text(self.provider).upper()
        if provider not in {"WANDA", "LIANGPIAO"}:
            raise PricingError("pricing_input_invalid", "provider必须是WANDA或LIANGPIAO。")
        object.__setattr__(self, "provider", provider)
        if not _text(self.show_id):
            raise PricingError("pricing_input_invalid", "show_id不能为空。")
        mode = self.price_mode.upper() if isinstance(self.price_mode, str) else self.price_mode
        if mode is not None and mode not in {"FIXED", "LIMIT"}:
            raise PricingError("pricing_input_invalid", "price_mode无效。")
        object.__setattr__(self, "price_mode", mode)
        default_mode = mode or "FIXED"
        route = self.quote_route or ("WANDA_SELF" if provider == "WANDA" else f"LIANGPIAO_{default_mode}")
        if route not in {"WANDA_SELF", "LIANGPIAO_LIMIT", "LIANGPIAO_FIXED"}:
            raise PricingError("pricing_input_invalid", "quote_route无效。")
        if provider == "WANDA" and route != "WANDA_SELF":
            raise PricingError("pricing_input_invalid", "Wanda只能使用WANDA_SELF报价路由。")
        if provider == "LIANGPIAO" and route not in {"LIANGPIAO_LIMIT", "LIANGPIAO_FIXED"}:
            raise PricingError("pricing_input_invalid", "良票只能使用对应price_mode报价路由。")
        if provider == "LIANGPIAO" and self.price_mode is not None and route != f"LIANGPIAO_{self.price_mode}":
            raise PricingError("pricing_input_invalid", "quote_route必须与price_mode一致。")
        object.__setattr__(self, "quote_route", route)
        if self.area_quote_strategy not in {None, "AVERAGE", "HIGHEST", "LOWEST"}:
            raise PricingError("pricing_input_invalid", "area_quote_strategy无效。")
        if not _text(self.ticket_mode):
            raise PricingError("pricing_input_invalid", "ticket_mode不能为空。")
        if self.quantity is not None and (not isinstance(self.quantity, int) or isinstance(self.quantity, bool) or not 1 <= self.quantity <= 20):
            raise PricingError("pricing_input_invalid", "quantity必须是1至20的整数或空值。")
        if self.quote_scope not in {"exact_seats", "area_preview", "area_probe"}:
            raise PricingError("pricing_input_invalid", "quote_scope无效。")
        object.__setattr__(self, "seats", tuple(self.seats))
        for field_name in ("provider_total_amount_cents", "provider_estimate_amount_cents", "provider_buyer_amount_cents", "provider_amount_cents", "provider_market_amount_cents", "provider_max_amount_cents"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
                raise PricingError("pricing_input_invalid", f"{field_name}必须是正整数分。")
        if self.provider == "LIANGPIAO" and self.preflight_verified is not True:
            raise PricingError("pricing_input_unverified", "Liangpiao PricingFacts必须来自已通过的预检。")
        if self.provider == "WANDA" and self.quote_scope == "exact_seats" and not self.seats:
            raise PricingError("pricing_input_invalid", "Wanda精确报价至少需要一个座位事实。")
        if self.provider == "LIANGPIAO" and not self.seats:
            raise PricingError("pricing_input_invalid", "良票精确报价至少需要一个座位事实。")
        if self.quote_scope != "exact_seats" and self.area_reference is None and not self.seats:
            raise PricingError("pricing_input_invalid", "区域预览需要area_reference或代表座位。")

    def with_probe_cost(self, *, probe_result_id: str, seat_id: str, original_price_cents: int, member_cost_cents: int, release_verified: bool) -> "PricingFacts":
        if release_verified is not True:
            raise PricingError("probe_release_not_verified", "Probe释放未验证，不能进入报价。")
        if not _text(probe_result_id) or not _text(seat_id) or original_price_cents <= 0 or member_cost_cents <= 0:
            raise PricingError("pricing_input_invalid", "Probe成本事实无效。")
        updated = list(self.seats)
        found = False
        for index, item in enumerate(updated):
            if item.seat_id != seat_id:
                continue
            if item.original_price_cents not in (None, original_price_cents):
                raise PricingError("pricing_fact_conflict", "Probe原价与已有官方原价不一致。")
            updated[index] = replace(item, original_price_cents=original_price_cents, member_cost_cents=member_cost_cents, cost_source="active_probe", probe_result_id=probe_result_id)
            found = True
            break
        reference = self.area_reference
        if reference is not None and reference.seat_id == seat_id:
            if reference.original_price_cents not in (None, original_price_cents):
                raise PricingError("pricing_fact_conflict", "Probe原价与区域参考原价不一致。")
            reference = replace(reference, original_price_cents=original_price_cents, member_cost_cents=member_cost_cents, cost_source="active_probe", probe_result_id=probe_result_id)
            found = True
        if not found:
            raise PricingError("pricing_seat_not_found", "Probe座位不在PricingFacts中。")
        return replace(self, seats=tuple(updated), area_reference=reference)


@dataclass(frozen=True, slots=True)
class PricedSeat:
    seat_id: str
    seat_label: str
    zone_type: str
    original_price_cents: int
    member_cost_cents: int | None
    channel_fee_cents: int
    unit_quote_cents: int


@dataclass(frozen=True, slots=True)
class QuoteResult:
    provider: ProviderName
    quote_scope: QuoteScope
    seat_zone_type: str
    seat_type: Literal["wplus", "regular", "mixed"] | None
    member_unit_price_cents: int | None
    original_unit_price_cents: int | None
    base_unit_cents: int | None
    base_total_cents: int | None
    unit_quote_cents: int | None
    total_quote_cents: int | None
    channel_fee_total_cents: int | None
    ticket_count: int | None
    needs_ticket_count: bool
    seat_quotes: tuple[PricedSeat, ...]
    price_mode: PriceMode | None = None
    max_price_cents: int | None = None
    provider_amount_cents: int | None = None
    operator_pricing_applied: bool = False
    operator_markup_percent: float | None = None
    pricing_rule_version: str | None = None
    quote_route: QuoteRoute = "WANDA_SELF"
    price_source: str | None = None
    provider_quote_id: str | None = None
    provider_quote_hash: str | None = None
    pricing_source: str = ""
    semantic_flags: tuple[str, ...] = ()
    # Integration lineage is attached after the pure engine calculation.  The
    # engine neither generates IDs nor persists records; these optional fields
    # keep the authoritative QuoteResult as the only price-result DTO.
    quote_id: str | None = None
    record_id: str | None = None
    event_id: str | None = None
    recognition_snapshot_id: str | None = None
    generation: int | None = None
    quote_expires_at: str | None = None
    supersedes_quote_id: str | None = None
    provider_max_amount_cents: int | None = None
    buyer_quote_cents: int | None = None
    order_max_price_cents: int | None = None
    calculation_evidence: Mapping[str, Any] = field(default_factory=dict)
    recognition_source: str | None = None
    show_resolution_source: str | None = None
    matched_show_id: str | None = None
    matched_movie_name: str | None = None
    matched_date: str | None = None
    matched_showtime_start: str | None = None
    matched_hall_name: str | None = None
    member_cost_source: str | None = None
    transaction_authorized: bool = True
