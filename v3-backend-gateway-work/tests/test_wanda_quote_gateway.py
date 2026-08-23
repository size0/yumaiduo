from __future__ import annotations

import asyncio

from app.schemas import Recognition
from app.wanda_quote_gateway import LocalTicketGateway, TicketGateway, gateway_auth_headers


def test_gateway_boundary_exposes_only_transport_operations_and_internal_auth(monkeypatch) -> None:
    for operation in ("for_quote", "match", "realtime_seats", "lock", "available_offers", "cancel"):
        assert operation in TicketGateway.__dict__
        assert hasattr(LocalTicketGateway, operation)

    monkeypatch.delenv("WANDA_QUOTE_GATEWAY_KEY", raising=False)
    assert gateway_auth_headers() == {}
    monkeypatch.setenv("WANDA_QUOTE_GATEWAY_KEY", "bridge-test-key")
    assert gateway_auth_headers() == {"X-Plugin-Bridge-Key": "bridge-test-key"}


def test_gateway_match_transports_bounded_visual_identity_without_auto_selecting_seats() -> None:
    captured: dict[str, object] = {}

    class CapturingGateway(LocalTicketGateway):
        async def _request(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
            captured.update({"method": method, "path": path, **kwargs})
            return {"data": {}}

    recognition = Recognition.model_validate({
        "city": "济南", "cinema": "济南万达影城世茂广场店", "movie": "奥德赛",
        "date": "2026-08-23", "showtime": "12:35-15:27", "hall": "8号厅",
        "official_selection": {"is_selected": True, "selected_seat_numbers": ["9排14座"], "selected_count": 1},
    })
    asyncio.run(CapturingGateway(account_phone="13800138000").match(recognition))

    body = captured["json"]
    assert captured["path"] == "/api/order/match"
    assert isinstance(body, dict)
    assert body["mode"] == "screenshot"
    assert body["auto_select_seats"] is False
    assert body["hints"]["city"] == "济南"
    assert body["hints"]["seats"] == ["9排14座"]
