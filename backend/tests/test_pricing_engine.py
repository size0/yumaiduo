from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.models import PricingRulesUpdate
from app.pricing import (
    LiangpiaoPricingBand,
    PricingError,
    PricingFacts,
    PricingRulesSnapshot,
    PricingSeatFact,
    V4PricingEngine,
    WandaPricingBand,
)
from app.pricing.adapters import ProbeCostFacts, snapshot_from_rules
from app.wanda_direct_quote import WandaDirectQuoteService

FIXTURE = Path(__file__).parent / "fixtures" / "pricing" / "production_revision_16.json"


def wanda_rules(*bands: tuple[float, float, int]) -> tuple[WandaPricingBand, ...]:
    return tuple(WandaPricingBand(*band) for band in bands)


def rules(**updates: object) -> PricingRulesSnapshot:
    values: dict[str, object] = {
        "enabled": True, "revision": 1, "rule_version": "pricing-test", "rounding_increment_cents": 10,
        "wanda_rules": wanda_rules((0, 50, 1000), (50, 60, 900), (60, 80, 500), (80, 90, 300), (90, 100, 0)),
    }
    values.update(updates)
    return PricingRulesSnapshot(**values)


def seat(label: str, original: int | None, member: int | None, *, area_id: str = "35", area_code: str = "35", area_name: str = "普通区", physical_wplus: bool = False, cost_source: str = "official_settle_price") -> PricingSeatFact:
    return PricingSeatFact(
        seat_id=f"id-{label}", seat_label=label, area_id=area_id, area_code=area_code, area_name=area_name,
        zone_type="WPLUS" if physical_wplus else "REGULAR", physical_wplus=physical_wplus,
        original_price_cents=original, member_cost_cents=member, availability_verified=True, cost_source=cost_source,
    )


def wanda_facts(*items: PricingSeatFact, quantity: int | None = None, is_vip: bool = False, scope: str = "exact_seats") -> PricingFacts:
    return PricingFacts(provider="WANDA", cinema_id="cinema-1", show_id="show-1", hall_name="1号厅", is_vip=is_vip, quantity=len(items) if quantity is None else quantity, seats=items, quote_scope=scope)


def liangpiao_facts(**updates: object) -> PricingFacts:
    values: dict[str, object] = {
        "provider": "LIANGPIAO", "show_id": "lp-show-1", "quantity": 2,
        "seats": [seat("1排1座", None, None), seat("1排2座", None, None)], "price_mode": "LIMIT",
        "provider_estimate_amount_cents": 6500, "provider_total_amount_cents": 7000,
        "provider_market_amount_cents": 8000, "preflight_verified": True,
        "provider_quote_id": "lpq-1", "provider_quote_hash": "a" * 64,
        "provider_pricing_rule_version": "liangpiao-provider-r1",
    }
    values.update(updates)
    return PricingFacts(**values)


def test_rules_disabled_preserves_current_wanda_fallback() -> None:
    result = V4PricingEngine().quote(wanda_facts(seat("1排1座", 7000, 6115)), rules(enabled=False, wanda_rules=()))
    assert (result.unit_quote_cents, result.total_quote_cents, result.pricing_rule_version) == (6115, 6115, None)


@pytest.mark.parametrize(("member", "expected"), [(4999, 6000), (5000, 5900), (6000, 6500), (8000, 8300), (9000, 9000), (10000, 10000)])
def test_production_wanda_band_boundaries(member: int, expected: int) -> None:
    assert V4PricingEngine().quote(wanda_facts(seat("1排1座", 10000, member)), rules()).unit_quote_cents == expected


def test_wanda_rounding_floor_ceiling_and_floor_error() -> None:
    engine = V4PricingEngine()
    assert engine.quote(wanda_facts(seat("1排1座", 10000, 6115)), rules()).unit_quote_cents == 6620
    assert engine.quote(wanda_facts(seat("1排1座", 10000, 9500)), rules(wanda_rules=wanda_rules((0, 100, -500)))).unit_quote_cents == 9500
    assert engine.quote(wanda_facts(seat("1排1座", 6290, 6000)), rules(wanda_rules=wanda_rules((0, 100, 1000)))).unit_quote_cents == 6290
    with pytest.raises(PricingError) as raised:
        engine.quote(wanda_facts(seat("1排1座", 99, 99)), rules())
    assert raised.value.code == "pricing_member_floor_exceeds_original_cap"


def test_wplus_and_special_non_wplus_mixed_seats_are_priced_independently() -> None:
    result = V4PricingEngine().quote(wanda_facts(
        seat("1排1座", 6290, 5500, area_id="36", area_code="36", area_name="W+专享", physical_wplus=True),
        seat("1排2座", 7290, 6190, area_name="特惠区"),
    ), rules())
    assert [item.unit_quote_cents for item in result.seat_quotes] == [5800, 6490]
    assert result.unit_quote_cents is None and result.total_quote_cents == 12290
    assert result.seat_type == "mixed" and result.seat_zone_type == "混合区域"


@pytest.mark.parametrize(("original", "expected"), [(5790, 5590), (6000, 5800), (6100, 5490), (8000, 7200)])
def test_vip_ignores_dead_fixed_cost(original: int, expected: int) -> None:
    engine = V4PricingEngine()
    baseline = engine.quote(wanda_facts(seat("1排1座", original, None), is_vip=True), rules(vip_fixed_cost_cents=5000))
    changed = engine.quote(wanda_facts(seat("1排1座", original, None), is_vip=True), rules(vip_fixed_cost_cents=99999))
    assert baseline.unit_quote_cents == expected and changed.unit_quote_cents == expected


def test_area_preview_supports_unknown_and_known_quantity() -> None:
    facts = PricingFacts(
        provider="WANDA", show_id="show-1", quantity=None, quote_scope="area_preview",
        area_reference=seat("代表座", 6290, 5500, area_id="36", area_code="36", area_name="W+专享", physical_wplus=True),
    )
    engine = V4PricingEngine()
    unknown = engine.quote(facts, rules())
    known = engine.quote(replace(facts, quantity=3), rules())
    assert (unknown.unit_quote_cents, unknown.total_quote_cents, unknown.needs_ticket_count) == (5800, None, True)
    assert (known.unit_quote_cents, known.total_quote_cents, known.ticket_count, known.needs_ticket_count) == (5800, 17400, 3, False)


def test_probe_adapter_requires_release_and_updates_only_cost_facts() -> None:
    facts = wanda_facts(seat("1排1座", 6290, None, physical_wplus=True, area_id="36", area_code="36", area_name="W+专享"))
    probe = ProbeCostFacts(probe_result_id="probe-1", seat_id="id-1排1座", original_price_cents=6290, member_cost_cents=5500, release_verified=True)
    assert probe.release_verified is True
    with pytest.raises(PricingError) as raised:
        facts.with_probe_cost(**{**probe.__dict__, "release_verified": False})
    assert raised.value.code == "probe_release_not_verified"
    updated = facts.with_probe_cost(**probe.__dict__)
    assert (updated.seats[0].member_cost_cents, updated.seats[0].cost_source, updated.seats[0].probe_result_id) == (5500, "active_probe", "probe-1")


def test_liangpiao_limit_markup_rounding_and_max_semantics() -> None:
    snapshot = rules(wanda_rules=(), liangpiao_rules=(LiangpiaoPricingBand(0, 100, 10),))
    result = V4PricingEngine().quote(liangpiao_facts(provider_estimate_amount_cents=6115, provider_total_amount_cents=7000, provider_market_amount_cents=8000), snapshot)
    assert (result.provider_amount_cents, result.total_quote_cents, result.unit_quote_cents, result.max_price_cents) == (6115, 6730, 3365, 7000)
    assert result.semantic_flags == ("PRICING_SEMANTIC_REVIEW_REQUIRED",)


def test_liangpiao_fixed_uses_total_and_explicit_fixed_bands() -> None:
    snapshot = rules(wanda_rules=(), liangpiao_fixed_rules=(LiangpiaoPricingBand(0, 100, 5),), liangpiao_fixed_rules_explicit=True)
    result = V4PricingEngine().quote(liangpiao_facts(price_mode="FIXED", provider_total_amount_cents=10000, provider_market_amount_cents=12000), snapshot)
    assert (result.provider_amount_cents, result.total_quote_cents, result.unit_quote_cents, result.max_price_cents) == (10000, 10500, 5250, 10000)


def test_liangpiao_non_divisible_and_disabled_paths() -> None:
    engine = V4PricingEngine()
    result = engine.quote(liangpiao_facts(provider_estimate_amount_cents=5555, provider_total_amount_cents=6000), rules(enabled=False, wanda_rules=()))
    assert result.total_quote_cents == 5555 and result.unit_quote_cents is None
    assert engine.quote(liangpiao_facts(price_mode="FIXED", provider_estimate_amount_cents=None, provider_total_amount_cents=None, provider_buyer_amount_cents=5500), rules(enabled=True, wanda_rules=())).total_quote_cents == 5500


def test_snapshot_adapter_and_production_revision_fixture() -> None:
    source = PricingRulesUpdate(enabled=True, wanda_rules=[{"min_discount_percent": 0, "max_discount_percent": 100, "fixed_adjustment_cents": 290}])
    adapted = snapshot_from_rules(source, revision=7, rule_version="r7")
    assert (adapted.revision, adapted.rule_version, adapted.wanda_rules[0].fixed_adjustment_cents) == (7, "r7", 290)
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    production = PricingRulesSnapshot.from_mapping(payload)
    engine = V4PricingEngine()
    assert engine.quote(wanda_facts(seat("1排1座", 6290, 5500)), production).unit_quote_cents == 5800
    assert engine.quote(wanda_facts(seat("1排1座", 7290, 6190)), production).unit_quote_cents == 6490


def test_input_validation_and_liangpiao_failures() -> None:
    with pytest.raises(PricingError):
        PricingRulesSnapshot(revision="bad")
    with pytest.raises(PricingError):
        PricingRulesSnapshot(rounding_increment_cents=5)
    with pytest.raises(PricingError):
        PricingRulesSnapshot(wanda_rules=wanda_rules((0, 50, 0), (60, 100, 0)))
    with pytest.raises(PricingError):
        PricingFacts(provider="LIANGPIAO", show_id="x")
    with pytest.raises(PricingError):
        PricingFacts(provider="WANDA", show_id="x", quote_scope="area_preview")
    engine = V4PricingEngine()
    with pytest.raises(PricingError) as raised:
        engine.quote(liangpiao_facts(price_mode="FIXED", estimated=True), rules(wanda_rules=()))
    assert raised.value.code == "LIANGPIAO_PREFLIGHT_INVALID"
    with pytest.raises(PricingError) as raised:
        engine.quote(liangpiao_facts(provider_market_amount_cents=None), rules(wanda_rules=(), liangpiao_rules=(LiangpiaoPricingBand(0, 100, 5),)))
    assert raised.value.code == "LIANGPIAO_PRICING_BASE_MISSING"


def test_representative_cases_match_legacy_pure_pricing_helper() -> None:
    old = PricingRulesUpdate(enabled=True, wanda_rules=[
        {"min_discount_percent": 0, "max_discount_percent": 50, "fixed_adjustment_cents": 1000},
        {"min_discount_percent": 50, "max_discount_percent": 60, "fixed_adjustment_cents": 900},
        {"min_discount_percent": 60, "max_discount_percent": 80, "fixed_adjustment_cents": 500},
        {"min_discount_percent": 80, "max_discount_percent": 90, "fixed_adjustment_cents": 300},
        {"min_discount_percent": 90, "max_discount_percent": 100, "fixed_adjustment_cents": 0},
    ])
    snapshot = snapshot_from_rules(old, revision=1, rule_version="parity")
    for original, member in ((6290, 5500), (7290, 6190), (10000, 6115), (10000, 9000)):
        actual = V4PricingEngine().quote(wanda_facts(seat("1排1座", original, member)), snapshot).unit_quote_cents
        expected = WandaDirectQuoteService._priced_unit(original_price=original, member_price=member, is_wplus=False, rules=old)
        assert actual == expected


def test_adapters_cover_mapping_and_snapshot_passthrough() -> None:
    direct = PricingRulesSnapshot(enabled=True, rule_version="direct")
    assert snapshot_from_rules(direct) is direct
    mapped = snapshot_from_rules({"revision": 4, "rule_version": "mapped", "rules": {"enabled": True}})
    assert (mapped.revision, mapped.rule_version, mapped.enabled) == (4, "mapped", True)
    assert snapshot_from_rules(object()).rule_version == "pricing-unversioned"


def test_rules_and_facts_reject_invalid_values() -> None:
    with pytest.raises(PricingError):
        WandaPricingBand(0, 100, "290")
    with pytest.raises(PricingError):
        PricingRulesSnapshot(liangpiao_fixed_rules=(LiangpiaoPricingBand(0, 90, 1),))
    with pytest.raises(PricingError):
        PricingFacts(provider="WANDA", show_id="s", quantity=True)
    with pytest.raises(PricingError):
        PricingFacts(provider="WANDA", show_id="s", quote_scope="area_probe")


def test_liangpiao_fixed_explicit_empty_policy_does_not_fallback() -> None:
    result = V4PricingEngine().quote(
        liangpiao_facts(price_mode="FIXED", provider_total_amount_cents=5000, provider_market_amount_cents=6000),
        rules(wanda_rules=(), liangpiao_rules=(LiangpiaoPricingBand(0, 100, 10),), liangpiao_fixed_rules=(), liangpiao_fixed_rules_explicit=True),
    )
    assert result.total_quote_cents == 5000
    assert result.operator_pricing_applied is False
