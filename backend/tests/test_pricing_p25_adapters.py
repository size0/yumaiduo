from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.pricing import (
    LiangpiaoPricingBand,
    PricingError,
    PricingRulesSnapshot,
    V4PricingEngine,
    WandaPricingBand,
)
from app.pricing.compat import to_quote_record, to_real_quote, to_selected_seat_quote_result
from app.pricing.provider_adapters import LiangpiaoPricingFactsAdapter, WandaPricingFactsAdapter


def rules() -> PricingRulesSnapshot:
    return PricingRulesSnapshot(
        enabled=True,
        revision=16,
        rule_version="r16",
        rounding_increment_cents=10,
        wanda_rules=(
            WandaPricingBand(0, 50, 1000), WandaPricingBand(50, 60, 900),
            WandaPricingBand(60, 80, 500), WandaPricingBand(80, 90, 300),
            WandaPricingBand(90, 100, 0),
        ),
        liangpiao_price_mode="LIMIT",
        liangpiao_rules=(LiangpiaoPricingBand(0, 50, 10), LiangpiaoPricingBand(50, 100, 5)),
        liangpiao_fixed_rules=(LiangpiaoPricingBand(0, 50, 10), LiangpiaoPricingBand(50, 100, 5)),
        liangpiao_fixed_rules_explicit=True,
    )


def test_wanda_adapter_maps_official_regular_wplus_vip_and_route() -> None:
    facts = WandaPricingFactsAdapter().adapt({
        "cinemaId": 107, "showId": "show-1", "hallName": "1号厅", "isVip": False,
        "seats": [
            {"seatId": "r1", "seatNumber": "1排1座", "areaId": "35", "areaName": "普通区",
             "zoneType": "REGULAR", "originalPriceCents": "7000", "regularMemberPrice": "6115",
             "channelFeeCents": "0", "available": True},
            {"seatId": "w1", "seatNumber": "1排2座", "areaId": "36", "areaName": "W+",
             "zoneType": "WPLUS", "physicalWplus": True, "originalPriceCents": 7290,
             "wplusMemberPrice": "6190", "available": True},
        ],
    })
    assert facts.provider == "WANDA"
    assert facts.quote_route == "WANDA_SELF"
    assert facts.seats[0].member_cost_cents == 6115
    assert facts.seats[1].member_cost_cents == 6190
    assert facts.seats[0].channel_fee_cents == 0
    assert facts.seats[0].cost_source == "wanda_official"
    assert V4PricingEngine().quote(facts, rules()).total_quote_cents == 12910


def test_wanda_adapter_consumes_only_verified_probe_result() -> None:
    payload = {"showId": "show-1", "seats": [{
        "seatId": "w1", "seatNumber": "1排1座", "areaId": "36", "areaName": "W+",
        "physicalWplus": True, "originalPriceCents": "7290", "available": True,
    }]}
    adapter = WandaPricingFactsAdapter()
    facts = adapter.adapt(payload, probe_results=[{
        "probeResultId": "probe-1", "seatId": "w1", "originalPriceCents": "7290",
        "memberCostCents": "6190", "releaseVerified": True,
    }])
    assert facts.seats[0].member_cost_cents == 6190
    assert facts.seats[0].cost_source == "active_probe"
    assert facts.seats[0].probe_result_id == "probe-1"
    with pytest.raises(PricingError, match="释放未验证"):
        adapter.adapt(payload, probe_results=[{
            "probeResultId": "probe-2", "seatId": "w1", "originalPriceCents": "7290",
            "memberCostCents": "6190", "releaseVerified": False,
        }])


def test_wanda_adapter_accepts_actual_probe_result_shape() -> None:
    facts = WandaPricingFactsAdapter().adapt({
        "showId": "show-1", "seats": [{
            "seatId": "w1", "seatNumber": "1排1座", "areaId": "36", "areaCode": "36",
            "areaName": "W+", "physicalWplus": True, "available": True,
        }],
    }, probe_results=[{
        "probe_id": "probe-actual", "status": "SUCCESS", "release_verified": True,
        "seat_type_prices": [{
            "area_code": "36", "zone_type": "WPLUS", "representative_seat_id": "probe-seat",
            "original_price_cents": 7290, "member_price_cents": 6190,
        }],
    }])
    assert facts.seats[0].member_cost_cents == 6190
    assert facts.seats[0].probe_result_id == "probe-actual"


def test_wanda_adapter_does_not_use_displayed_price_as_original() -> None:
    facts = WandaPricingFactsAdapter().adapt({
        "showId": "show-1", "seats": [{"seatId": "r1", "seatNumber": "1排1座",
        "displayedPrice": "70", "regularMemberPrice": "6115", "available": True}],
    })
    assert facts.seats[0].original_price_cents is None
    with pytest.raises(PricingError) as raised:
        V4PricingEngine().quote(facts, rules())
    assert raised.value.code == "authoritative_original_price_required"


@pytest.mark.parametrize("mode", ["LIMIT", "FIXED"])
def test_liangpiao_adapter_preserves_route_and_cent_amount_fields(mode: str) -> None:
    payload = {
        "available": True, "showId": "lp-show", "estimated": mode == "LIMIT",
        "estimateAmount": "6500", "totalAmount": "7000", "marketAmount": "8000",
        "ticketMode": "STANDARD", "priceMode": mode, "areaQuoteStrategy": "HIGHEST",
        "seats": [{"rowNo": 1, "colNo": 2, "seatNo": "1排2座", "areaId": "A"},
                  {"rowNo": 1, "colNo": 3, "seatNo": "1排3座", "areaId": "B"}],
    }
    facts = LiangpiaoPricingFactsAdapter().from_preflight(payload)
    assert facts.provider == "LIANGPIAO"
    assert facts.quote_route == f"LIANGPIAO_{mode}"
    assert facts.price_mode == mode and facts.ticket_mode == "STANDARD"
    assert facts.area_quote_strategy == "HIGHEST"
    assert facts.show_id == "lp-show" and len(facts.seats) == 2
    assert facts.provider_estimate_amount_cents == 6500
    assert facts.provider_total_amount_cents == 7000
    assert facts.provider_market_amount_cents == 8000
    result = V4PricingEngine().quote(facts, rules())
    assert result.quote_route == f"LIANGPIAO_{mode}"
    assert result.total_quote_cents == (6830 if mode == "LIMIT" else 7350)


def test_liangpiao_adapter_distinguishes_limit_estimate_and_upper_limit() -> None:
    facts = LiangpiaoPricingFactsAdapter().from_preflight({
        "available": True, "showId": "lp", "estimated": True,
        "estimateAmount": "6500", "totalAmount": "7000", "marketAmount": "8000",
        "priceMode": "LIMIT", "seats": [{"rowNo": 1, "colNo": 1}],
    })
    assert facts.provider_estimate_amount_cents == 6500
    assert facts.provider_total_amount_cents == 7000
    assert facts.provider_max_amount_cents == 7000
    result = V4PricingEngine().quote(facts, rules())
    assert result.base_total_cents == 6500
    assert result.max_price_cents == 7000
    assert result.total_quote_cents == 6830


def test_liangpiao_limit_estimated_false_still_uses_estimate_amount() -> None:
    facts = LiangpiaoPricingFactsAdapter().from_preflight({
        "available": True, "showId": "lp", "estimated": False,
        "estimateAmount": "6500", "totalAmount": "7000", "marketAmount": "8000",
        "priceMode": "LIMIT", "seats": [{"rowNo": 1, "colNo": 1}],
    })
    assert facts.estimated is False
    assert V4PricingEngine().quote(facts, rules()).base_total_cents == 6500


def test_wanda_vip_adapter_to_engine_uses_vip_formula() -> None:
    facts = WandaPricingFactsAdapter().adapt({
        "showId": "vip-show", "isVip": True,
        "seats": [{"seatId": "v1", "seatNumber": "1排1座", "originalPriceCents": "7000", "available": True}],
    })
    result = V4PricingEngine().quote(facts, rules())
    assert result.quote_route == "WANDA_SELF"
    assert result.price_source == "realtime_vip_area"
    assert result.total_quote_cents == 6300


def test_liangpiao_fixed_uses_total_and_rejects_estimated_fixed() -> None:
    adapter = LiangpiaoPricingFactsAdapter()
    facts = adapter.from_preflight({
        "available": True, "showId": "lp", "estimated": False,
        "totalAmount": "7000", "marketAmount": "8000", "priceMode": "FIXED",
        "ticketMode": "FAST", "seats": [{"rowNo": 1, "colNo": 1}],
    })
    assert facts.provider_estimate_amount_cents is None
    assert facts.provider_total_amount_cents == 7000
    assert V4PricingEngine().quote(facts, rules()).total_quote_cents == 7350
    with pytest.raises(PricingError) as raised:
        adapter.from_preflight({
            "available": True, "showId": "lp", "estimated": True,
            "totalAmount": "7000", "priceMode": "FIXED", "seats": [{"rowNo": 1, "colNo": 1}],
        })
    assert raised.value.code == "LIANGPIAO_PREFLIGHT_INVALID"


def test_liangpiao_market_missing_is_preserved_for_engine_policy() -> None:
    facts = LiangpiaoPricingFactsAdapter().from_preflight({
        "available": True, "showId": "lp", "estimated": False,
        "totalAmount": "7000", "priceMode": "FIXED", "seats": [{"rowNo": 1, "colNo": 1}],
    })
    assert facts.provider_market_amount_cents is None
    disabled = PricingRulesSnapshot(enabled=False)
    assert V4PricingEngine().quote(facts, disabled).total_quote_cents == 7000
    with pytest.raises(PricingError) as raised:
        V4PricingEngine().quote(facts, rules())
    assert raised.value.code == "LIANGPIAO_PRICING_BASE_MISSING"


def test_liangpiao_unavailable_and_string_amount_validation() -> None:
    adapter = LiangpiaoPricingFactsAdapter()
    with pytest.raises(PricingError) as raised:
        adapter.from_preflight({"available": False, "showId": "lp", "priceMode": "FIXED"})
    assert raised.value.code == "LIANGPIAO_PREFLIGHT_UNAVAILABLE"
    with pytest.raises(PricingError):
        adapter.from_preflight({
            "available": True, "showId": "lp", "totalAmount": "70.00", "priceMode": "FIXED",
            "seats": [{"rowNo": 1, "colNo": 1}],
        })
    with pytest.raises(PricingError) as raised:
        adapter.from_preflight({
            "available": True, "showId": "lp", "totalAmount": "7000", "priceMode": "FIXED",
            "seats": [{"rowNo": 1, "colNo": 1, "available": False}],
        })
    assert raised.value.code == "LIANGPIAO_SEAT_UNAVAILABLE"


def test_quote_result_compatibility_maps_without_recalculation() -> None:
    facts = LiangpiaoPricingFactsAdapter().from_preflight({
        "available": True, "showId": "lp", "estimated": True,
        "estimateAmount": "6500", "totalAmount": "7000", "marketAmount": "8000",
        "priceMode": "LIMIT", "seats": [{"rowNo": 1, "colNo": 1}, {"rowNo": 1, "colNo": 2}],
    })
    result = V4PricingEngine().quote(facts, rules())
    legacy = to_real_quote(result, quote_date="2026-09-02", matched_cinema_name="Cinema", matched_movie_name="Movie")
    assert legacy.total_quote_cents == result.total_quote_cents
    assert legacy.max_price_cents == 7000
    assert legacy.price_source == "liangpiao_realtime_preflight"
    assert legacy.price_mode == "LIMIT"
    selected = to_selected_seat_quote_result(
        result, facts=facts, quote_id="lp-quote", quote_hash="a" * 64,
        expires_at=datetime(2026, 9, 2, tzinfo=timezone.utc), generation=3, trace_id="trace",
    )
    assert selected.quote_id == "lp-quote"
    assert selected.buyer_amount_fen == result.total_quote_cents
    assert selected.max_price_fen == result.max_price_cents
    assert selected.price_mode == "LIMIT"
    assert selected.generation == 3


def test_quote_record_mapping_keeps_quote_id_separate_from_record_id() -> None:
    facts = LiangpiaoPricingFactsAdapter().from_preflight({
        "available": True, "showId": "lp", "estimated": False, "totalAmount": "7000",
        "marketAmount": "8000", "priceMode": "FIXED", "seats": [{"rowNo": 1, "colNo": 1}],
    })
    result = V4PricingEngine().quote(facts, rules())
    record = to_quote_record(
        result, record_id="event-123", quote_id="quote-456", tenant_id="t", shop_id="s",
        buyer_id="b", chat_id="c", created_at="2026-09-02T00:00:00+00:00",
        quote_expires_at="2026-09-02T00:15:00+00:00", item_id="item",
    )
    assert record["record_id"] == "event-123"
    assert record["quote_id"] == "quote-456"
    assert record["record_id"] != record["quote_id"]
    assert record["quote_route"] == "liangpiao_exact"
    assert record["route"] == "liangpiao_exact"
    assert record["provider_route"] == "LIANGPIAO_FIXED"
    assert record["pricing_quote_route"] == "LIANGPIAO_FIXED"
    assert record["total_quote_cents"] == result.total_quote_cents
    assert record["max_price_cents"] == result.max_price_cents
    with pytest.raises(PricingError, match="quote_id"):
        to_quote_record(
            result, record_id="event-123", quote_id="", tenant_id="t", shop_id="s",
            buyer_id="b", chat_id="c", created_at="2026-09-02T00:00:00+00:00",
            quote_expires_at="2026-09-02T00:15:00+00:00",
        )
