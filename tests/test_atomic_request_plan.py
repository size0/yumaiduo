"""A declared complete field set is the only quote boundary for an agent turn."""

from copy import deepcopy
from datetime import datetime, timezone, timedelta
from itertools import permutations
from unittest.mock import Mock, patch

import pytest

from tests.test_request_batch import actual_flow
from tests import _check_channel_route as f


PLAN = ["ticket_count", "seats", "time"]
TEXT = "两张，改8排13座 8排14座，下午3:20"


def edits(text=TEXT):
    return dict(
        ticket_count=(
            "update_ticket_request",
            dict(intent="revise", quantity=2, quantity_text="两张", evidence=text),
        ),
        seats=(
            "supplement_ticket_details",
            dict(evidence=text, seats="8排13座 8排14座"),
        ),
        time=("supplement_ticket_details", dict(evidence=text, time="下午3:20")),
    )


def enable(t):
    t.request_plan_enabled = True
    execute = t.execute

    def with_fixed_proofs(name, args):
        args = dict(args)
        if name not in (
            "update_ticket_request",
            "supplement_ticket_details",
            "supplement_city",
        ):
            return execute(name, args)
        proofs = {
            k: v
            for k, v in {
                "ticket_count": "两张" if "两张" in t.text else "一张",
                "seats": "8排13座 8排14座",
                "time": "下午3:20",
                "city": "太原",
            }.items()
            if v in t.text
        }
        args.setdefault("field_evidence", proofs)
        # The fixed scripts know which fields they intend to submit.
        if isinstance(args.get("request_fields"), list):
            args["field_evidence"] = {
                k: v
                for k, v in args["field_evidence"].items()
                if k in args["request_fields"]
            }
        return execute(name, args)

    t.execute = with_fixed_proofs
    return t


@pytest.mark.parametrize("order", list(permutations(PLAN)))
def test_all_field_orders_do_not_quote_before_complete_and_never_use_old_time(
    actual_flow, order
):
    make, session, quotes, service, transport = actual_flow
    t = enable(make(TEXT))
    calls = edits()
    old = session.conversation.quote_id
    for i, key in enumerate(order):
        name, args = calls[key]
        result = t.execute(name, dict(args, request_fields=PLAN))
        if i < 2:
            assert result["status"] == "REQUEST_PLAN_STAGED" and t.flow_result is None
            service.flow.api.ch4_show_list.assert_not_called()
            assert not t.read_quote()["quote"]["tradable"]
        else:
            # The fake catalogue has no 15:20 show, so old 14:20 must not quote.
            assert session.conversation.quote_id == ""
            assert not t.flow_result or "QUOTE_CARD" not in t.flow_result.template_keys
    assert quotes.get(old).status == "SUPERSEDED"
    assert session.image_context["agent_request_plan"]["pending"]
    assert service.flow.api.ch4_show_list.call_count == 1


def valid_new_show(service, clock="15:20"):
    date = f"2030-10-18T{clock}:00+08:00"
    service.flow.api.ch4_show_list.return_value[0]["startTime"] = date
    service.flow.api.ch4_show_detail.return_value["startTime"] = date
    shows = deepcopy(f.SHOWS)
    group = shows["data"]["showtimeFilmInf"][0]["showtimeFilmDateInf"][0]
    group["showtimesInf"]["showtimeList"][0]["realtime"] = int(
        datetime.fromisoformat(date).timestamp() * 1000
    )
    return shows


def test_valid_combined_request_quotes_once_after_last_field(actual_flow):
    make, session, quotes, service, transport = actual_flow
    t = enable(make(TEXT))
    calls = edits()
    with patch.dict(f.SHOWS, valid_new_show(service)):
        for key in PLAN:
            name, args = calls[key]
            result = t.execute(name, dict(args, request_fields=PLAN))
    q = quotes.get(session.conversation.quote_id)
    assert result["ok"] and q.show.endswith("15:20")
    assert q.selected_seats == ["8排13座", "8排14座"] and q.ticket_count == 2
    assert t.flow_result.template_keys.count("QUOTE_CARD") == 1
    assert service.flow.api.ch4_show_list.call_count == 1
    assert "agent_request_plan" not in session.image_context


@pytest.mark.parametrize("bad", ["version", "identity", "stale", "unverified"])
def test_first_stage_checks_scope_before_any_quote_write(actual_flow, bad):
    make, session, quotes, service, transport = actual_flow
    old = session.conversation.quote_id
    if bad == "version":
        session.image_context["demand_rev"] -= 1
    elif bad == "identity":
        session.image_context["chat_id"] = "another"
    elif bad == "stale":
        session.image_context["created_at"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
    else:
        session.image_context["unverified_image"] = "https://example.test/new.jpg"
    t = enable(make("一张"))
    result = t.execute(
        "update_ticket_request",
        dict(
            intent="revise",
            quantity=1,
            quantity_text="一张",
            evidence="一张",
            request_fields=["ticket_count"],
        ),
    )
    assert not result["ok"] and result["status"] == "STALE_REFERENCE"
    assert session.conversation.quote_id == old and quotes.get(old).status == "READY"
    assert "agent_request_plan" not in session.image_context


@pytest.mark.parametrize("plan", [None, [{}], [[]], ["time", "time"], ["unknown"]])
def test_missing_or_malformed_plan_has_no_business_effect(actual_flow, plan):
    make, session, quotes, service, transport = actual_flow
    t = enable(make(TEXT))
    old = session.conversation.quote_id
    result = t.execute(
        "update_ticket_request",
        dict(
            intent="revise",
            quantity=2,
            quantity_text="两张",
            evidence=TEXT,
            request_fields=plan,
        ),
    )
    assert not result["ok"] and result["status"] == "REQUEST_PLAN_REQUIRED"
    assert session.conversation.quote_id == old and quotes.get(old).status == "READY"
    service.flow.api.ch4_show_list.assert_not_called()


@pytest.mark.parametrize("bad", ["identity", "version"])
def test_text_only_context_cannot_be_relabelled_and_import_foreign_fields(
    actual_flow, bad
):
    make, session, quotes, service, transport = actual_flow
    old = session.conversation.quote_id
    session.image_context.pop("url")
    session.image_context["awaiting_image_request"] = {"movie": "旧买家影片"}
    if bad == "identity":
        session.image_context["buyer_id"] = "other"
    else:
        session.image_context["demand_rev"] -= 1
    before = deepcopy(session.image_context)
    t = enable(make("下午3:20"))
    result = t.execute(
        "supplement_ticket_details",
        dict(evidence=t.text, time=t.text, request_fields=["time"]),
    )
    assert result["status"] == "STALE_REFERENCE" and session.image_context == before
    assert session.conversation.quote_id == old and quotes.get(old).status == "READY"
    service.flow.api.ch4_show_list.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        "request_latest_quote_reprice",
        "recover_image_context",
        "confirm_previous_image",
        "confirm_image_cinema",
    ],
)
def test_partial_plan_cannot_bypass_collection_through_other_tool(actual_flow, name):
    make, session, quotes, service, transport = actual_flow
    t = enable(make(TEXT))
    name0, args = edits()["ticket_count"]
    assert (
        t.execute(name0, dict(args, request_fields=PLAN))["status"]
        == "REQUEST_PLAN_STAGED"
    )
    result = t.execute(name, {})
    assert not result["ok"] and result["status"] == "REQUEST_FIELDS_INCOMPLETE"
    assert t.flow_result is None
    service.flow.api.ch4_show_list.assert_not_called()
    assert t.get_purchase_guidance()["payment_blocked"]


def test_explicit_correction_can_leave_incomplete_plan_without_reviving_quote(
    actual_flow,
):
    make, session, quotes, service, transport = actual_flow
    old = session.conversation.quote_id
    first = enable(make(TEXT))
    name, args = edits()["seats"]
    first.execute(name, dict(args, request_fields=PLAN))
    correction = enable(make("不要原来的位置"))
    result = correction.execute(
        "clarify_ticket_request", dict(evidence=correction.text)
    )
    assert result["ok"] and "agent_request_plan" not in session.image_context
    assert not session.conversation.quote_id and quotes.get(old).status == "SUPERSEDED"
    service.flow.api.ch4_show_list.assert_not_called()


def test_new_turn_can_correct_failed_complete_plan_using_saved_other_fields(
    actual_flow,
):
    make, session, quotes, service, transport = actual_flow
    first = enable(make(TEXT))
    for key in PLAN:
        name, args = edits()[key]
        first.execute(name, dict(args, request_fields=PLAN))
    assert (
        not session.conversation.quote_id
        and session.image_context["agent_request_plan"]["pending"]
    )
    second = enable(make("改下午4:20"))
    with patch.dict(f.SHOWS, valid_new_show(service, "16:20")):
        result = second.execute(
            "supplement_ticket_details",
            dict(evidence=second.text, time="下午4:20", request_fields=["time"]),
        )
    q = quotes.get(session.conversation.quote_id)
    assert result["ok"] and q.show.endswith("16:20") and q.ticket_count == 2
    assert q.selected_seats == ["8排13座", "8排14座"]
    assert "agent_request_plan" not in session.image_context


def test_partial_fields_survive_new_turn_and_finish_without_reasking_saved_fields(
    actual_flow,
):
    make, session, quotes, service, transport = actual_flow
    first = enable(make(TEXT))
    for key in ("ticket_count", "seats"):
        name, args = edits()[key]
        first.execute(name, dict(args, request_fields=PLAN))
    second = enable(make("下午3:20"))
    context = second.read_buyer_context()["context"]["demand"]["request_plan"]
    assert (
        context["missing_fields"] == ["time"]
        and context["values"]["seats"] == "8排13座 8排14座"
    )
    with patch.dict(f.SHOWS, valid_new_show(service)):
        result = second.execute(
            "supplement_ticket_details",
            dict(evidence=second.text, time=second.text, request_fields=PLAN),
        )
    q = quotes.get(session.conversation.quote_id)
    assert (
        result["ok"]
        and q.show.endswith("15:20")
        and q.selected_seats == ["8排13座", "8排14座"]
    )


def test_city_plus_other_fields_passes_complete_request_to_one_recognition(actual_flow):
    make, session, quotes, service, transport = actual_flow
    text = "太原，两张，8排13座 8排14座，下午3:20"
    t = enable(make(text))
    plan = ["city", "ticket_count", "seats", "time"]
    from application.ticket_flow import FlowResult

    service.flow.on_screenshot = Mock(
        return_value=FlowResult(
            ok=False,
            messages=["该新场次尚未核实"],
            template_keys=["TEXT_IMAGE_PENDING"],
        )
    )
    t.execute("supplement_city", dict(city="太原", request_fields=plan))
    t.execute(
        "update_ticket_request",
        dict(
            intent="revise",
            quantity=2,
            quantity_text="两张",
            evidence=text,
            request_fields=plan,
        ),
    )
    t.execute(
        "supplement_ticket_details",
        dict(evidence=text, seats="8排13座 8排14座", request_fields=plan),
    )
    service.flow.on_screenshot.assert_not_called()
    t.execute(
        "supplement_ticket_details",
        dict(evidence=text, time="下午3:20", request_fields=plan),
    )
    service.flow.on_screenshot.assert_called_once()
    kw = service.flow.on_screenshot.call_args.kwargs
    assert kw["city_name"] == "太原" and kw["buyer_request"]["time"] == "15:20"
    assert [s["seatNo"] for s in kw["buyer_request"]["seats"]] == ["8排13座", "8排14座"]
    assert session.image_context["agent_request_plan"]["pending"]


def test_independent_inquiry_can_continue_after_begin_with_old_paid_order(actual_flow):
    from domain.order import Order

    make, session, quotes, service, transport = actual_flow
    old = session.conversation.quote_id
    service.order_store.create(
        Order(
            order_id="old-paid",
            quote_id=old,
            idempotency_key="old-paid",
            **{
                k: getattr(session.conversation, k)
                for k in ("tenant_id", "shop_id", "buyer_id", "chat_id")
            },
            payment_state="CONFIRMED",
            state="PAID",
            amount_fen=10000,
        )
    )
    t = enable(make("另外问下午3:20那场价格"))
    t.semantic_intent = "inquire"
    assert t.execute("begin_new_inquiry", dict(evidence=t.text))["ok"]
    result = t.execute(
        "supplement_ticket_details",
        dict(evidence=t.text, time="下午3:20", request_fields=["time"]),
    )
    assert result["ok"] and session.conversation.demand_inquiry
    assert session.image_context["ticket_details"]["time"] == "15:20"
    assert not session.conversation.quote_id and not service.flow.api.create_calls
    assert service.order_store.get("old-paid").quote_id == old
    assert service.order_store.get("old-paid").payment_state == "CONFIRMED"


@pytest.mark.parametrize("guard", ["disabled", "state"])
def test_city_combined_plan_preserves_automation_and_state_guards(actual_flow, guard):
    from domain.conversation import ConvState

    make, session, quotes, service, transport = actual_flow
    old = session.conversation.quote_id
    t = enable(make("太原，一张"))
    if guard == "disabled":
        t.cfg.recognize_quote.auto_recognize = False
    else:
        session.conversation.state = ConvState.AWAIT_TICKET
    service.flow.on_screenshot = Mock()
    result = t.execute(
        "supplement_city", dict(city="太原", request_fields=["city", "ticket_count"])
    )
    assert not result["ok"] and session.conversation.quote_id == old
    assert (
        quotes.get(old).status == "READY"
        and "agent_request_plan" not in session.image_context
    )
    service.flow.on_screenshot.assert_not_called()
