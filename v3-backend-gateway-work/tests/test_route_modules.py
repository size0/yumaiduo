from __future__ import annotations

from fastapi import FastAPI

from app.quote_reply import quote_reply_text, validate_quote_reply_template
from app.routes.agent_reply import create_agent_reply_router
from app.routes.plugin_bridge import create_plugin_bridge_router
from app.routes.quote_preview import create_quote_preview_router
from app.schemas import QuoteRealtimeResponse, Recognition


def test_plugin_bridge_router_owns_expected_operator_endpoints() -> None:
    router = create_plugin_bridge_router(FastAPI())
    routes = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("GET", "/api/xianyu-plugin/bridge/runtime-settings") in routes
    assert ("PUT", "/api/xianyu-plugin/bridge/agent-canary-approval") in routes
    assert ("PUT", "/api/xianyu-plugin/bridge/agent-release") in routes
    assert ("PUT", "/api/xianyu-plugin/bridge/quote-policy") in routes
    assert ("POST", "/api/xianyu-plugin/bridge/conversation-experiences") in routes
    assert ("POST", "/api/xianyu-plugin/bridge/corrections") in routes
    assert ("PUT", "/api/xianyu-plugin/bridge/corrections/{correction_id}") in routes


def test_quote_preview_and_agent_routers_own_ai_execution_endpoints() -> None:
    app = FastAPI()
    quote_routes = {
        (method, route.path)
        for route in create_quote_preview_router(app, lambda: {}).routes
        for method in getattr(route, "methods", set())
    }
    agent_routes = {
        (method, route.path)
        for route in create_agent_reply_router(app, lambda: {}).routes
        for method in getattr(route, "methods", set())
    }
    assert ("POST", "/api/quotes/preview-quote") in quote_routes
    assert ("POST", "/api/quotes/preview-ingest") in quote_routes
    assert ("POST", "/api/agents/turn") in agent_routes
    assert ("POST", "/api/agents/v2/completions") in agent_routes
    assert ("POST", "/api/replies/preview-ingest") in agent_routes


def test_quote_reply_module_rejects_fabricated_facts_and_renders_verified_amounts() -> None:
    try:
        validate_quote_reply_template("保证有票，价格39元")
    except Exception as error:
        assert getattr(error, "status_code", None) == 422
    else:
        raise AssertionError("unsafe template must be rejected")

    quote = QuoteRealtimeResponse.model_validate({
        "status": "quoted", "quote_scope": "exact_seats", "seat_zone_type": "W+",
        "pricing_source": "wanda_realtime_member_offer", "ticket_count": 2,
        "unit_quote_cents": 5700, "total_quote_cents": 11400,
        "needs_ticket_count": False, "detail": "verified",
    })
    recognition = Recognition.model_validate({"city": "济南", "cinema": "世茂广场店", "movie": "奥德赛"})
    text = quote_reply_text(quote, recognition, "{城市}{影院}《{影片}》{张数}张合计{合计}元")
    assert text == "济南世茂广场店《奥德赛》2张合计114.00元"
