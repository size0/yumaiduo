from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import MovieImageInfo, PricingRulesUpdate
from app.pricing_store import PricingRulesStore
from app.wanda_direct_quote import WandaDirectQuoteService


class UnusedRecognitionService:
    async def recognize(self, *_args, **_kwargs) -> MovieImageInfo:
        raise AssertionError("recognition should not be called")


def test_pricing_rules_api_is_bounded_versioned_and_persistent(tmp_path: Path) -> None:
    path = tmp_path / "pricing-rules.json"
    store = PricingRulesStore(path)
    client = TestClient(create_app(
        service=UnusedRecognitionService(), pricing_rules_store=store,
    ))

    initial = client.get("/api/settings/operations")
    assert initial.status_code == 200
    assert initial.json()["enabled"] is False
    assert initial.json()["wplus_friday_member_day_enabled"] is True
    saved = client.put("/api/settings/operations", json={
        "enabled": True,
        "wplus_friday_member_day_enabled": False,
        "regular_adjustment_cents": 100,
        "wplus_member_price_threshold_cents": 6000,
        "wplus_adjustment_cents": -290,
        "rounding_increment_cents": 10,
    })
    assert saved.status_code == 200
    body = saved.json()
    assert body["revision"] == 1
    assert body["wplus_friday_member_day_enabled"] is False
    assert body["rule_version"].startswith("pricing-r1-")
    assert PricingRulesStore(path).current().enabled is True
    assert "自由" not in path.read_text(encoding="utf-8")

    rejected = client.put("/api/settings/operations", json={
        "enabled": True,
        "regular_adjustment_cents": -100_001,
        "wplus_member_price_threshold_cents": 6000,
        "wplus_adjustment_cents": -290,
        "rounding_increment_cents": 10,
    })
    assert rejected.status_code == 422


def test_positive_wplus_discount_is_normalized_to_a_negative_adjustment(tmp_path: Path) -> None:
    path = tmp_path / "pricing-rules.json"
    path.write_text(
        '{"revision":3,"rules":{"enabled":true,"regular_adjustment_cents":100,'
        '"wplus_member_price_threshold_cents":6000,"wplus_adjustment_cents":290,'
        '"rounding_increment_cents":10}}',
        encoding="utf-8",
    )
    store = PricingRulesStore(path)

    loaded = store.current()
    saved = store.save(loaded.model_copy(update={"wplus_adjustment_cents": 290}))

    assert loaded.wplus_adjustment_cents == -290
    assert saved.wplus_adjustment_cents == -290
    assert '"wplus_adjustment_cents": -290' in path.read_text(encoding="utf-8")


def test_legacy_dual_pricing_file_preserves_separate_seat_rules(tmp_path: Path) -> None:
    path = tmp_path / "pricing-rules.json"
    path.write_text(
        '{"revision":7,"rules":{"enabled":true,"regular_markup_cents":180,'
        '"wplus_member_threshold_cents":6000,"wplus_original_discount_cents":290,'
        '"rounding_increment_cents":10}}',
        encoding="utf-8",
    )

    rules = PricingRulesStore(path).current()

    assert rules.enabled is True
    assert rules.regular_adjustment_cents == 180
    assert rules.wplus_member_price_threshold_cents == 6000
    assert rules.wplus_adjustment_cents == -290


@pytest.mark.asyncio
async def test_backend_pricing_rules_apply_distinct_regular_and_wplus_formulas() -> None:
    rules = PricingRulesUpdate(
        enabled=True, regular_adjustment_cents=100,
        wplus_member_price_threshold_cents=6000, wplus_adjustment_cents=-290,
        rounding_increment_cents=10,
    )
    service = WandaDirectQuoteService(Settings(), pricing_rules=rules)
    try:
        ordinary = {
            "label": "6排16座", "area_id": "35", "area_name": "优选区",
            "price": 7290, "channel_fee": 0, "wplus": False,
        }
        wplus = {
            "label": "7排8座", "area_id": "36", "area_name": "W+专享",
            "price": 6290, "channel_fee": 0, "wplus": True,
        }
        quote = service._exact_quote(
            [ordinary, wplus],
            {("35", 7290, 0): 6190, ("36", 6290, 0): 5500},
            {"cinema_name": "测试影城"},
            "奥德赛",
        )
    finally:
        await service.aclose()

    assert quote.seat_quotes[0].member_price_cents == 6190
    assert quote.seat_quotes[0].unit_quote_cents == 6290
    assert quote.seat_quotes[1].member_price_cents == 5500
    assert quote.seat_quotes[1].unit_quote_cents == 6000
    assert quote.total_quote_cents == 12290
    assert quote.pricing_rule_version is not None
    assert "后台报价规则" in quote.pricing_source


@pytest.mark.asyncio
async def test_saved_rules_apply_to_existing_quote_service_without_restart(tmp_path: Path) -> None:
    store = PricingRulesStore(tmp_path / "pricing-rules.json")
    service = WandaDirectQuoteService(Settings(), pricing_rules=store.current)
    seat = {
        "label": "6排16座", "area_id": "35", "area_name": "普通区",
        "price": 7290, "channel_fee": 0, "wplus": False,
    }
    try:
        before = service._exact_quote(
            [seat], {("35", 7290, 0): 6190}, {"cinema_name": "测试影城"}, "奥德赛",
        )
        store.save(PricingRulesUpdate(enabled=True, regular_adjustment_cents=100))
        after = service._exact_quote(
            [seat], {("35", 7290, 0): 6190}, {"cinema_name": "测试影城"}, "奥德赛",
        )
    finally:
        await service.aclose()
    assert before.unit_quote_cents == 6190
    assert before.pricing_rule_version is None
    assert after.unit_quote_cents == 6290
    assert after.pricing_rule_version is not None


def test_wplus_formula_examples_and_price_boundaries() -> None:
    rules = PricingRulesUpdate(enabled=True)
    assert WandaDirectQuoteService._priced_unit(
        original_price=6990, member_price=5500, is_wplus=True, rules=rules,
    ) == 6700
    assert WandaDirectQuoteService._priced_unit(
        original_price=6200, member_price=5950, is_wplus=True, rules=rules,
    ) == 5950
    assert WandaDirectQuoteService._priced_unit(
        original_price=6990, member_price=6100, is_wplus=True, rules=rules,
    ) == 6100
    assert WandaDirectQuoteService._priced_unit(
        original_price=7000, member_price=6950, is_wplus=False, rules=rules,
    ) == 7000


def test_integer_cent_rounding_is_deterministic_and_never_below_member_cost() -> None:
    rules = PricingRulesUpdate(enabled=True, regular_adjustment_cents=100, rounding_increment_cents=10)
    assert WandaDirectQuoteService._priced_unit(
        original_price=7000, member_price=6115, is_wplus=False, rules=rules,
    ) == 6220
