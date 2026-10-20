"""Continuous buyer conversations through the actual guarded tool handlers."""

from datetime import datetime, timezone
from copy import deepcopy
from unittest.mock import patch

import json
import pytest

from tests.test_request_batch import actual_flow
from domain.order import Order
from application.ticket_details import CHINA


def tool(make, text):
    t = make(text)
    t.request_plan_enabled = True
    return t


def add_paid(session, service, *, demand_id=None):
    conv = session.conversation
    order = Order(
        order_id="prior-paid",
        quote_id="paid-quote",
        idempotency_key="prior-paid",
        **{
            k: getattr(conv, k) for k in ("tenant_id", "shop_id", "buyer_id", "chat_id")
        },
        demand_id=demand_id or conv.demand_id,
        demand_rev=conv.demand_rev,
        state="PAID",
        payment_state="CONFIRMED",
        amount_fen=10000,
    )
    service.order_store.create(order)
    return order


def test_quantity_before_any_image_is_saved_for_the_next_image(actual_flow):
    make, session, quotes, service, _ = actual_flow
    session.image_context = {}
    session.conversation.quote_id = ""
    t = tool(make, "两张")
    r = t.execute(
        "update_ticket_request",
        dict(
            intent="revise",
            quantity=2,
            quantity_text="两张",
            request_fields=["ticket_count"],
        ),
    )
    assert (
        r["ok"] and session.image_context["awaiting_image_request"]["ticket_count"] == 2
    )
    service.flow.api.ch4_show_list.assert_not_called()
    assert t.flow_result is None


def test_explicit_new_purchase_can_save_details_after_an_old_paid_order(actual_flow):
    make, session, quotes, service, _ = actual_flow
    old = add_paid(session, service)
    t = tool(make, "另外买一张")
    assert t.execute("begin_new_purchase", dict(quantity=1, quantity_text="一张"))["ok"]
    nxt = tool(make, "下午2:20")
    r = nxt.execute(
        "supplement_ticket_details", dict(time=nxt.text, request_fields=["time"])
    )
    assert (
        r["ok"] and session.image_context["awaiting_image_request"]["time"] == "14:20"
    )
    assert session.image_context["awaiting_image_request"]["ticket_count"] == 1
    assert service.order_store.get(old.order_id) == old
    service.flow.api.ch4_show_list.assert_not_called()


def test_current_purchase_paid_order_still_blocks_fields_even_from_prior_revision(
    actual_flow,
):
    make, session, quotes, service, _ = actual_flow
    t = tool(make, "另外买一张")
    t.execute("begin_new_purchase", dict(quantity=1, quantity_text="一张"))
    add_paid(session, service)
    session.conversation.demand_rev += 1
    session.image_context["demand_rev"] = session.conversation.demand_rev
    r = tool(make, "下午2:20").execute(
        "supplement_ticket_details", dict(time="下午2:20", request_fields=["time"])
    )
    assert not r["ok"] and r["status"] == "PAID_ORDER_LOCKED"


def test_relative_date_is_frozen_when_declared_not_recomputed_after_midnight(
    actual_flow,
):
    make, session, quotes, service, _ = actual_flow
    text = "明天，8排13座 8排14座"
    with patch(
        "application.ticket_details.now",
        return_value=datetime(2030, 10, 17, 23, 59, tzinfo=CHINA),
    ):
        first = tool(make, text)
        r = first.execute(
            "supplement_ticket_details",
            dict(
                date="明天",
                request_fields=["date", "seats"],
                field_evidence={"date": "明天", "seats": "8排13座 8排14座"},
            ),
        )
        assert r["status"] == "REQUEST_PLAN_STAGED"
        assert (
            first.read_buyer_context()["context"]["demand"]["request_plan"][
                "canonical_values"
            ]["date"]
            == "2030-10-18"
        )
    with patch(
        "application.ticket_details.now",
        return_value=datetime(2030, 10, 18, 0, 1, tzinfo=CHINA),
    ):
        second = tool(make, "8排13座 8排14座")
        r = second.execute(
            "supplement_ticket_details",
            dict(seats=second.text, request_fields=["seats"]),
        )
    q = (
        quotes.get(session.conversation.quote_id)
        if session.conversation.quote_id
        else None
    )
    assert r["ok"] and q and q.show.startswith("2030-10-18")
    assert session.image_context["ticket_details"]["date"] == "2030-10-18"


def test_time_period_from_previous_message_is_not_lost_when_only_clock_fragment_was_submitted(
    actual_flow,
):
    make, session, quotes, service, _ = actual_flow
    text = "下午2:20，8排13座 8排14座"
    first = tool(make, text)
    assert first.execute(
        "supplement_ticket_details",
        dict(
            time="2:20",
            request_fields=["time", "seats"],
            field_evidence={"time": "2:20", "seats": "8排13座 8排14座"},
        ),
    )["ok"]
    second = tool(make, "8排13座 8排14座")
    r = second.execute(
        "supplement_ticket_details", dict(seats=second.text, request_fields=["seats"])
    )
    assert r["ok"] and session.conversation.quote_id
    assert session.image_context["ticket_details"]["time"] == "14:20"
    assert not session.image_context["ticket_details"]["ambiguous_time"]


def test_new_purchase_confirmation_reuses_its_quote_despite_old_paid_order(actual_flow):
    from domain.conversation import ConvState

    make, session, quotes, service, _ = actual_flow
    oldq = deepcopy(quotes.get(session.conversation.quote_id))
    oldctx = deepcopy(session.image_context)
    add_paid(session, service)
    t = tool(make, "另外买两张")
    t.execute("begin_new_purchase", dict(quantity=2, quantity_text="两张"))
    # Simulate the scoped state left by a successfully verified new image.
    session.image_context.update(
        url="https://example.test/new-purchase.jpg",
        verified=True,
        recognition_facts=oldctx["recognition_facts"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    session.image_context.pop("awaiting_image_request")
    oldq.quote_id = "new-purchase"
    oldq.demand_id = session.conversation.demand_id
    oldq.demand_rev = session.conversation.demand_rev
    quotes.create(oldq)
    session.conversation.quote_id = oldq.quote_id
    session.conversation.state = ConvState.QUOTED
    r = tool(make, "两张").execute(
        "update_ticket_request",
        dict(
            intent="revise",
            quantity=2,
            quantity_text="两张",
            request_fields=["ticket_count"],
        ),
    )
    assert r["ok"] and r["status"] == "UNCHANGED"
    assert session.conversation.quote_id == oldq.quote_id
    service.flow.api.ch4_show_list.assert_not_called()


def test_bad_literal_date_is_a_field_error_not_an_internal_query_failure(actual_flow):
    make, session, quotes, service, _ = actual_flow
    old = session.conversation.quote_id
    r = tool(make, "2030-13-45").execute(
        "supplement_ticket_details", dict(date="2030-13-45", request_fields=["date"])
    )
    assert not r["ok"] and r["status"] == "INVALID_FIELD_EVIDENCE"
    assert session.conversation.quote_id == old and quotes.get(old).status == "READY"


def test_old_unanchored_relative_date_plan_requires_confirmation(actual_flow):
    make, session, quotes, service, _ = actual_flow
    ctx = session.image_context
    ctx["agent_request_plan"] = dict(
        **{
            k: ctx[k]
            for k in (
                "tenant_id",
                "shop_id",
                "buyer_id",
                "chat_id",
                "demand_id",
                "demand_rev",
            )
        },
        pending=True,
        source_image=ctx["url"],
        expected=["date", "seats"],
        values={"date": "明天"},
        field_evidence={"date": "明天", "seats": "8排13座 8排14座"},
        origins={"date": {"value": "明天", "evidence": "明天"}},
    )
    r = tool(make, "8排13座 8排14座").execute(
        "supplement_ticket_details",
        dict(seats="8排13座 8排14座", request_fields=["seats"]),
    )
    assert not r["ok"] and r["status"] == "INVALID_FIELD_EVIDENCE"
    service.flow.api.ch4_show_list.assert_not_called()


def test_relative_date_declared_before_midnight_stays_anchored_even_if_submitted_later(
    actual_flow,
):
    make, session, quotes, service, _ = actual_flow
    text = "两张，明天，8排13座 8排14座"
    t = tool(make, text)
    with patch(
        "application.ticket_details.now",
        return_value=datetime(2030, 10, 17, 23, 59, tzinfo=CHINA),
    ):
        r = t.execute(
            "update_ticket_request",
            dict(
                intent="revise",
                quantity=2,
                quantity_text="两张",
                request_fields=["ticket_count", "date", "seats"],
                field_evidence={
                    "ticket_count": "两张",
                    "date": "明天",
                    "seats": "8排13座 8排14座",
                },
            ),
        )
        assert r["status"] == "REQUEST_PLAN_STAGED"
    with patch(
        "application.ticket_details.now",
        return_value=datetime(2030, 10, 18, 0, 1, tzinfo=CHINA),
    ):
        assert t.execute(
            "supplement_ticket_details", dict(date="明天", seats="8排13座 8排14座")
        )["ok"]
    assert quotes.get(session.conversation.quote_id).show.startswith("2030-10-18")


def test_clock_without_a_period_remains_ambiguous_after_continuation(actual_flow):
    make, session, quotes, service, _ = actual_flow
    t = tool(make, "2:20，8排13座 8排14座")
    t.execute(
        "supplement_ticket_details",
        dict(
            time="2:20",
            request_fields=["time", "seats"],
            field_evidence={"time": "2:20", "seats": "8排13座 8排14座"},
        ),
    )
    second = tool(make, "8排13座 8排14座")
    r = second.execute(
        "supplement_ticket_details", dict(seats=second.text, request_fields=["seats"])
    )
    assert r["ok"] and session.image_context["ticket_details"]["ambiguous_time"]
    assert not session.conversation.quote_id and "time_period" in r["missing_fields"]
    service.flow.api.ch4_show_list.assert_not_called()


@pytest.mark.parametrize(
    "wrong", ["child_identity", "parent_identity", "revision", "source_image"]
)
def test_pending_status_never_authorizes_projection_of_foreign_plan(actual_flow, wrong):
    make, session, quotes, service, _ = actual_flow
    ctx = session.image_context
    plan = dict(
        **{
            k: ctx[k]
            for k in (
                "tenant_id",
                "shop_id",
                "buyer_id",
                "chat_id",
                "demand_id",
                "demand_rev",
            )
        },
        pending=True,
        source_image=ctx["url"],
        expected=["date"],
        values={"date": "PRIVATE_PLAN_DATE"},
        canonical_values={"date": "PRIVATE_CANONICAL_DATE"},
    )
    ctx["agent_request_plan"] = plan
    ctx["ticket_details"] = {"pending": True}
    if wrong == "child_identity":
        plan["buyer_id"] = "foreign"
    if wrong == "parent_identity":
        ctx["buyer_id"] = "foreign"
    if wrong == "revision":
        plan["demand_rev"] -= 1
    if wrong == "source_image":
        plan["source_image"] = "https://example.test/other.jpg"
    context = tool(make, "读本次需求").read_buyer_context()["context"]
    assert context["demand"]["request_plan"] is None
    assert "PRIVATE_PLAN_DATE" not in json.dumps(context)
    assert "PRIVATE_CANONICAL_DATE" not in json.dumps(context)


def test_preimage_plan_has_explicit_scope_and_can_be_read_back(actual_flow):
    make, session, quotes, service, _ = actual_flow
    session.image_context = {}
    session.conversation.quote_id = ""
    t = tool(make, "两张，下午2:20")
    t.execute(
        "update_ticket_request",
        dict(
            intent="revise",
            quantity=2,
            quantity_text="两张",
            request_fields=["ticket_count", "time"],
            field_evidence={"ticket_count": "两张", "time": "下午2:20"},
        ),
    )
    plan = t.read_buyer_context()["context"]["demand"]["request_plan"]
    assert plan["values"]["ticket_count"] == 2 and plan["missing_fields"] == ["time"]


def test_assigned_demand_cannot_use_an_image_without_version_keys(actual_flow):
    make, session, quotes, service, _ = actual_flow
    old = session.conversation.quote_id
    session.image_context.pop("demand_id")
    session.image_context.pop("demand_rev")
    r = tool(make, "下午2:20").execute(
        "supplement_ticket_details", dict(time="下午2:20", request_fields=["time"])
    )
    assert not r["ok"] and r["status"] == "STALE_REFERENCE"
    assert session.conversation.quote_id == old and quotes.get(old).status == "READY"
    service.flow.api.ch4_show_list.assert_not_called()
