"""Resolve the information the agent itself just asked the buyer to supply."""

from unittest.mock import patch, Mock
from copy import deepcopy
from datetime import datetime, timezone, timedelta

import pytest

from tests.test_request_batch import actual_flow


def tool(make, text):
    t = make(text)
    t.request_plan_enabled = True
    return t


def test_period_only_answer_resolves_saved_ambiguous_clock(actual_flow):
    make, session, quotes, service, _ = actual_flow
    first = tool(make, "2:20，8排13座 8排14座")
    r = first.execute(
        "supplement_ticket_details",
        dict(time="2:20", seats="8排13座 8排14座", request_fields=["time", "seats"]),
    )
    assert (
        r["ok"] and session.image_context["ticket_details"]["awaiting"] == "time_period"
    )
    assert not session.conversation.quote_id
    second = tool(make, "下午")
    r = second.execute(
        "supplement_ticket_details", dict(time="下午", request_fields=["time"])
    )
    q = (
        quotes.get(session.conversation.quote_id)
        if session.conversation.quote_id
        else None
    )
    assert r["ok"] and q and q.show.endswith("14:20")
    assert not session.image_context["ticket_details"]["ambiguous_time"]


def test_city_before_image_is_saved_without_recognition(actual_flow):
    make, session, quotes, service, _ = actual_flow
    session.image_context = {}
    session.conversation.quote_id = ""
    service.flow.on_screenshot = Mock(
        side_effect=AssertionError("no image to recognize")
    )
    r = tool(make, "广州").execute(
        "supplement_city", dict(city="广州", request_fields=["city"])
    )
    assert r["ok"] and session.image_context["awaiting_image_request"]["city"] == "广州"
    service.flow.on_screenshot.assert_not_called()


def test_city_and_quantity_before_image_are_merged_without_a_null_image_call(
    actual_flow,
):
    make, session, quotes, service, _ = actual_flow
    session.image_context = {}
    session.conversation.quote_id = ""
    service.flow.on_screenshot = Mock(
        side_effect=AssertionError("no image to recognize")
    )
    t = tool(make, "广州，两张")
    assert t.execute(
        "supplement_city",
        dict(
            city="广州",
            request_fields=["city", "ticket_count"],
            field_evidence={"city": "广州", "ticket_count": "两张"},
        ),
    )["ok"]
    r = t.execute(
        "update_ticket_request", dict(intent="revise", quantity=2, quantity_text="两张")
    )
    assert r["ok"] and session.image_context["awaiting_image_request"]["city"] == "广州"
    assert session.image_context["awaiting_image_request"]["ticket_count"] == 2
    service.flow.on_screenshot.assert_not_called()


def test_exact_seats_resolve_a_preference_from_the_previous_turn(actual_flow):
    make, session, quotes, service, _ = actual_flow
    first = tool(make, "中间位置")
    assert first.execute(
        "supplement_ticket_details",
        dict(position_preference=first.text, request_fields=["position_preference"]),
    )["ok"]
    assert not session.conversation.quote_id
    second = tool(make, "8排13座 8排14座")
    r = second.execute(
        "supplement_ticket_details", dict(seats=second.text, request_fields=["seats"])
    )
    q = (
        quotes.get(session.conversation.quote_id)
        if session.conversation.quote_id
        else None
    )
    assert r["ok"] and q and q.selected_seats == ["8排13座", "8排14座"]
    assert "position_preference" not in session.image_context["ticket_details"]


def test_new_preference_withdraws_previous_exact_seats_without_pricing_them(
    actual_flow,
):
    make, session, quotes, service, _ = actual_flow
    first = tool(make, "8排13座 8排14座")
    service.flow.api.ch4_show_list.side_effect = TimeoutError("fixed")
    first.execute(
        "supplement_ticket_details", dict(seats=first.text, request_fields=["seats"])
    )
    assert not session.conversation.quote_id
    service.flow.api.ch4_show_list.reset_mock()
    second = tool(make, "后排中间")
    r = second.execute(
        "supplement_ticket_details",
        dict(position_preference=second.text, request_fields=["position_preference"]),
    )
    assert r["ok"] and not session.conversation.quote_id
    assert session.image_context["ticket_details"]["position_preference"] == "后排中间"
    assert not session.image_context["ticket_details"].get("seats")
    service.flow.api.ch4_show_list.assert_not_called()


@pytest.mark.parametrize(
    "bad", ["none", "parent", "version", "stale", "source", "future"]
)
def test_only_current_fresh_preimage_city_is_used_as_a_recognition_hint(
    actual_flow, bad
):
    from application.seat_request import preimage_city

    make, session, quotes, service, _ = actual_flow
    session.image_context = {}
    session.conversation.quote_id = ""
    assert tool(make, "广州").execute(
        "supplement_city", dict(city="广州", request_fields=["city"])
    )["ok"]
    ctx = deepcopy(session.image_context)
    request = ctx["awaiting_image_request"]
    if bad == "parent":
        ctx["buyer_id"] = "another"
    if bad == "version":
        request["demand_rev"] -= 1
    if bad == "stale":
        ctx["created_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    if bad == "source":
        request["source_image"] = "https://example.test/old.jpg"
    if bad == "future":
        ctx["created_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
    assert preimage_city(request, ctx, session.conversation, 24) == (
        "广州" if bad == "none" else ""
    )


def test_next_image_receives_saved_city_via_the_service_entry():
    from tests.test_buyer_assistant import BuyerAssistantTests
    from application.buyer_assistant import BuyerAssistantTools

    f = BuyerAssistantTests()
    f.setUp()
    try:
        f.session.image_context = {}
        t = BuyerAssistantTools(f.svc, f.session, "广州", f.cfg, None)
        t.enable_semantic_routing()
        t.request_plan_enabled = True
        t.semantic_intent = "revise"
        assert t.execute("supplement_city", dict(city="广州", request_fields=["city"]))[
            "ok"
        ]
        with patch.object(f.svc, "_blacklist_gate", return_value=None):
            f.svc.on_message(
                "c", image_url="https://example.test/new.jpg", config=f.cfg
            )
        args = f.svc.flow.on_screenshot.call_args.kwargs
        assert args["city_name"] == "广州" and args["buyer_request"]["city"] == "广州"
    finally:
        f.tearDown()


def test_period_only_without_a_pending_clock_does_not_guess_from_old_quote(actual_flow):
    make, session, quotes, service, _ = actual_flow
    old = session.conversation.quote_id
    r = tool(make, "下午").execute(
        "supplement_ticket_details", dict(time="下午", request_fields=["time"])
    )
    assert not r["ok"] and r["status"] == "INVALID_FIELD_EVIDENCE"
    assert session.conversation.quote_id == old and quotes.get(old).status == "READY"
    service.flow.api.ch4_show_list.assert_not_called()


@pytest.mark.parametrize("wrong", ["version", "image"])
def test_period_cannot_borrow_clock_from_a_different_pending_request(
    actual_flow, wrong
):
    make, session, quotes, service, _ = actual_flow
    ctx = session.image_context
    ctx["ticket_details"] = dict(
        pending=True,
        ambiguous_time=True,
        time="02:20",
        demand_id=session.conversation.demand_id,
        demand_rev=session.conversation.demand_rev,
        source_image=ctx["url"],
    )
    if wrong == "version":
        ctx["ticket_details"]["demand_rev"] -= 1
    else:
        ctx["ticket_details"]["source_image"] = "https://example.test/other.jpg"
    t = tool(make, "下午")
    r = t.execute(
        "supplement_ticket_details", dict(time="下午", request_fields=["time"])
    )
    assert not r["ok"] and t.flow_result is None
    service.flow.api.ch4_show_list.assert_not_called()


def test_same_turn_preference_and_exact_seats_are_not_silently_overridden(actual_flow):
    make, session, quotes, service, _ = actual_flow
    t = tool(make, "中间位置，8排13座 8排14座")
    assert t.execute(
        "supplement_ticket_details",
        dict(
            position_preference="中间位置",
            request_fields=["position_preference", "seats"],
            field_evidence={
                "position_preference": "中间位置",
                "seats": "8排13座 8排14座",
            },
        ),
    )["ok"]
    r = t.execute("supplement_ticket_details", dict(seats="8排13座 8排14座"))
    assert not r["ok"] and r["status"] == "INVALID_FIELD_EVIDENCE"
    assert not session.conversation.quote_id
    service.flow.api.ch4_show_list.assert_not_called()


def test_rejected_refinement_does_not_mutate_pending_plan_and_can_be_corrected(
    actual_flow,
):
    make, session, quotes, service, _ = actual_flow
    first = tool(make, "中间位置")
    first.execute(
        "supplement_ticket_details",
        dict(position_preference=first.text, request_fields=["position_preference"]),
    )
    before = deepcopy(session.image_context["agent_request_plan"])
    t = tool(make, "9排10座 9排11座，改8排13座 8排14座")
    r = t.execute(
        "supplement_ticket_details",
        dict(
            seats="9排10座 9排11座",
            request_fields=["seats"],
            field_evidence={"seats": "8排13座 8排14座"},
        ),
    )
    assert not r["ok"] and session.image_context["agent_request_plan"] == before
    assert not getattr(t, "request_plan", None)
    assert t.execute(
        "supplement_ticket_details",
        dict(seats="8排13座 8排14座", request_fields=["seats"]),
    )["ok"]
    assert quotes.get(session.conversation.quote_id).selected_seats == [
        "8排13座",
        "8排14座",
    ]


def test_internal_text_city_saver_cannot_bypass_image_city_verification(actual_flow):
    from application.ticket_details import supplement

    make, session, quotes, service, _ = actual_flow
    old = session.conversation.quote_id
    t = tool(make, "广州")
    r = supplement(t, t.text, city="广州")
    assert not r["ok"] and r["status"] == "INVALID_FIELD_EVIDENCE"
    assert session.conversation.quote_id == old and quotes.get(old).status == "READY"
    service.flow.api.ch4_show_list.assert_not_called()
