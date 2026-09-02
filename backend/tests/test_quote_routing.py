from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.cinema_routing import CinemaRouteResult
from app.models import MovieImageInfo, RealQuote
from app.plugin_automation import RulesFirstDecisionEngine


@dataclass
class FakeRouter:
    route: CinemaRouteResult

    async def resolve(self, recognition: MovieImageInfo) -> CinemaRouteResult:
        return self.route


class FakeWandaQuote:
    def __init__(self) -> None:
        self.calls = 0
        self.mapped_ids: list[str] = []

    async def quote(self, _: MovieImageInfo) -> RealQuote:
        self.calls += 1
        return RealQuote(quote_scope="area_preview", seat_zone_type="W+", unit_quote_cents=5000)

    async def quote_mapped(self, _: MovieImageInfo, *, wanda_cinema_id: str) -> RealQuote:
        self.mapped_ids.append(wanda_cinema_id)
        return await self.quote(_)


class FakeLiangpiaoQuote:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def quote(self, _: MovieImageInfo, *, tenant_id: str, conversation_id: str) -> RealQuote:
        self.calls.append((tenant_id, conversation_id))
        return RealQuote(
            quote_scope="exact_seats", seat_zone_type="LIANGPIAO", total_quote_cents=5200,
            price_source="liangpiao_realtime_preflight", pricing_source="良票实时选座预检",
        )


def recognition() -> MovieImageInfo:
    return MovieImageInfo(cinema_name="测试影院", selected_seats=[{"seat_number": "1排1座"}], selected_count_visible=1)


@pytest.mark.asyncio
async def test_wanda_route_uses_wanda_quote_even_with_explicit_seats() -> None:
    wanda = FakeWandaQuote()
    lp = FakeLiangpiaoQuote()
    engine = RulesFirstDecisionEngine(
        object(), wanda, cinema_route_resolver=FakeRouter(
            CinemaRouteResult("WANDA_SELF", recognition(), wanda_cinema_id="590"),
        ), liangpiao_exact_quote_adapter=lp,
    )

    _, quote = await engine._quote_for_recognition(recognition(), {
        "tenant_id": "t", "shop_id": "s", "chat_id": "c",
    })

    assert quote.quote_scope == "area_preview"
    assert wanda.calls == 1
    assert wanda.mapped_ids == ["590"]
    assert lp.calls == []


@pytest.mark.asyncio
async def test_non_wanda_route_uses_liangpiao_exact_quote() -> None:
    wanda = FakeWandaQuote()
    lp = FakeLiangpiaoQuote()
    engine = RulesFirstDecisionEngine(
        object(), wanda, cinema_route_resolver=FakeRouter(
            CinemaRouteResult("LIANGPIAO_EXACT", recognition()),
        ), liangpiao_exact_quote_adapter=lp,
    )

    _, quote = await engine._quote_for_recognition(recognition(), {
        "tenant_id": "t", "shop_id": "s", "chat_id": "c",
    })

    assert quote.quote_scope == "exact_seats"
    assert wanda.calls == 0
    assert lp.calls == [("t", "t:s:c")]
