"""Recovery must not revive old quotes or silently restore withdrawn facts."""

from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tests.test_request_batch import actual_flow
from application import platform_orders
from application.request_state import project


def seeded(actual_flow, text):
    make, session, quotes, service, transport = actual_flow
    q = quotes.get(session.conversation.quote_id)
    session.image_context["ticket_details"] = dict(
        movie=q.movie,
        date="2030-10-18",
        time="14:20",
        seats=[
            dict(rowNo=9, colNo=10, seatNo="9排10座"),
            dict(rowNo=9, colNo=11, seatNo="9排11座"),
        ],
        ticket_count=2,
        source_image=session.image_context["url"],
        pending=False,
        demand_id=session.conversation.demand_id,
        demand_rev=session.conversation.demand_rev,
    )
    t = make(text)
    t.request_plan_enabled = True
    return make, session, quotes, service, t


def test_withdrawn_time_never_returns_from_previous_image_when_only_new_seats_arrive(
    actual_flow,
):
    make, session, quotes, service, t = seeded(actual_flow, "换场次，时间还没定")
    old = session.conversation.quote_id
    assert t.execute("clarify_ticket_request", dict(invalidated_fields=["time"]))["ok"]
    saved = session.image_context["ticket_details"]
    assert "time" not in saved and "seats" not in saved and saved["movie"]
    assert project(session.conversation, session.image_context)["blocked"]
    nxt = make("8排13座 8排14座")
    nxt.request_plan_enabled = True
    r = nxt.execute(
        "supplement_ticket_details", dict(seats=nxt.text, request_fields=["seats"])
    )
    assert r["ok"] and not session.conversation.quote_id
    assert "time" not in session.image_context["ticket_details"]
    assert "time" in r["missing_fields"] and quotes.get(old).status == "SUPERSEDED"
    service.flow.api.ch4_show_list.assert_not_called()


def test_only_seat_withdrawal_preserves_confirmed_show_and_quotes_once(actual_flow):
    make, session, quotes, service, t = seeded(actual_flow, "不要原座位")
    assert t.execute("clarify_ticket_request", dict(invalidated_fields=["seats"]))["ok"]
    assert session.image_context["ticket_details"]["time"] == "14:20"
    nxt = make("8排13座 8排14座")
    nxt.request_plan_enabled = True
    r = nxt.execute(
        "supplement_ticket_details", dict(seats=nxt.text, request_fields=["seats"])
    )
    q = quotes.get(session.conversation.quote_id)
    assert (
        r["ok"]
        and q.show.endswith("14:20")
        and q.selected_seats == ["8排13座", "8排14座"]
    )
    assert not project(session.conversation, session.image_context)["blocked"]
    assert service.flow.api.ch4_show_list.call_count == 1


@pytest.mark.parametrize(
    "name",
    [
        "request_latest_quote_reprice",
        "confirm_inquiry_purchase",
        "confirm_inquiry_replacement",
    ],
)
def test_clarification_pending_blocks_transaction_before_original_handler(
    actual_flow, name
):
    make, session, quotes, service, t = seeded(actual_flow, "换场次")
    t.execute("clarify_ticket_request", dict(invalidated_fields=["time"]))
    with patch.object(t, name, side_effect=AssertionError("must not delegate")):
        assert t.execute(name, {})["status"] == "REQUEST_FIELDS_INCOMPLETE"
    assert not t.read_quote()["quote"]["tradable"]


def test_overdeclared_unproven_field_rejected_without_pending_or_quote_mutation(
    actual_flow,
):
    make, session, quotes, service, _ = actual_flow
    text = "两张，8排13座 8排14座"
    t = make(text)
    t.request_plan_enabled = True
    old = session.conversation.quote_id
    r = t.execute(
        "update_ticket_request",
        dict(
            intent="revise",
            quantity=2,
            quantity_text="两张",
            request_fields=["ticket_count", "seats", "date"],
            field_evidence={"ticket_count": "两张", "seats": "8排13座 8排14座"},
        ),
    )
    assert r["status"] == "REQUEST_FIELDS_UNSUPPORTED" and r["unsupported_fields"] == [
        "date"
    ]
    assert "agent_request_plan" not in session.image_context
    assert session.conversation.quote_id == old and quotes.get(old).status == "READY"
    good = t.execute(
        "update_ticket_request",
        dict(
            intent="revise",
            quantity=2,
            quantity_text="两张",
            request_fields=["ticket_count", "seats"],
            field_evidence={"ticket_count": "两张", "seats": "8排13座 8排14座"},
        ),
    )
    assert good["status"] == "REQUEST_PLAN_STAGED"
    assert t.execute("supplement_ticket_details", dict(seats="8排13座 8排14座"))["ok"]
    assert quotes.get(session.conversation.quote_id).selected_seats == [
        "8排13座",
        "8排14座",
    ]


def test_valid_time_evidence_cannot_be_replaced_with_another_literal_in_same_text(
    actual_flow,
):
    make, session, quotes, service, _ = actual_flow
    t = make("两张，原来下午2:20，改下午3:20")
    t.request_plan_enabled = True
    t.execute(
        "update_ticket_request",
        dict(
            intent="revise",
            quantity=2,
            quantity_text="两张",
            request_fields=["ticket_count", "time"],
            field_evidence={"ticket_count": "两张", "time": "下午3:20"},
        ),
    )
    r = t.execute("supplement_ticket_details", dict(time="下午2:20"))
    assert r["status"] == "INVALID_FIELD_EVIDENCE"
    service.flow.api.ch4_show_list.assert_not_called()


@pytest.mark.parametrize("failed", [False, True])
def test_expiry_renewal_invalidates_ready_quote_and_only_success_reopens_collection(
    actual_flow, failed
):
    make, session, quotes, service, _ = actual_flow
    t = make("两张，8排13座 8排14座")
    t.request_plan_enabled = True
    old = session.conversation.quote_id
    t.execute(
        "update_ticket_request",
        dict(
            intent="revise",
            quantity=2,
            quantity_text="两张",
            request_fields=["ticket_count", "seats"],
            field_evidence={"ticket_count": "两张", "seats": "8排13座 8排14座"},
        ),
    )
    assert quotes.get(old).status == "READY"
    session.image_context["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=25)
    ).isoformat()
    nxt = make("继续核对")
    nxt.request_plan_enabled = True
    assert (
        nxt.execute("request_latest_quote_reprice", {})["status"]
        == "REQUEST_REVALIDATION_REQUIRED"
    )
    if failed:
        service.flow.api.ch4_show_list.side_effect = TimeoutError("fixed")
    r = nxt.execute("recover_image_context", {})
    assert quotes.get(old).status == "SUPERSEDED" and not session.conversation.quote_id
    assert not nxt.read_quote()["quote"]["tradable"]
    if failed:
        assert r["status"] == "REQUEST_REVALIDATION_REQUIRED"
        assert "request_revalidated_at" not in session.image_context
        session.image_context.pop("agent_request_plan")
        assert not make("检查").read_quote()["quote"]["tradable"]
    else:
        assert r["status"] == "REQUEST_CONTEXT_REVALIDATED"
        assert project(session.conversation, session.image_context)["collecting"]
        final = make("8排13座 8排14座")
        final.request_plan_enabled = True
        assert final.execute(
            "supplement_ticket_details",
            dict(seats=final.text, request_fields=["seats"]),
        )["ok"]
        assert final.read_quote()["quote"]["tradable"]
        assert session.conversation.quote_id != old


def test_disappearing_platform_record_returns_not_found():
    row = dict(
        tenant_id="t", shop_id="s", buyer_id="b", chat_id="c", platform_order_id="1"
    )
    store = SimpleNamespace(
        platforms=SimpleNamespace(get=Mock(side_effect=[row, None]))
    )

    def bad(*_):
        raise TimeoutError("fixed")

    assert (
        platform_orders.refresh_record(store, row, reader=bad)["status"] == "NOT_FOUND"
    )


def test_verified_terminal_mirrors_stop_automatic_polling_but_are_not_deleted():
    rows = [
        dict(platform_status=s, sync_state="VERIFIED", next_sync_at=0)
        for s in (4, 6, 2)
    ]
    store = SimpleNamespace(platforms=SimpleNamespace(all=lambda: deepcopy(rows)))
    with (
        patch.object(platform_orders, "_LAST_TICK", 0),
        patch.object(platform_orders, "refresh_record") as refresh,
    ):
        assert platform_orders.process_due(store, clock=100) == 1
        assert refresh.call_args.args[1]["platform_status"] == 2
    assert len(rows) == 3


def test_city_correction_waits_for_complete_new_request_then_runs_one_verification(
    actual_flow,
):
    from application.ticket_flow import FlowResult

    make, session, quotes, service, t = seeded(actual_flow, "城市错了")
    original = deepcopy(quotes.get(session.conversation.quote_id))
    assert t.execute("clarify_ticket_request", dict(invalidated_fields=["city"]))["ok"]
    text = f"太原 {original.movie} 2030-10-18 下午2:20 8排13座 8排14座"
    nxt = make(text)
    nxt.request_plan_enabled = True
    fields = dict(
        city="太原",
        movie=original.movie,
        date="2030-10-18",
        time="下午2:20",
        seats="8排13座 8排14座",
    )

    def verified(conv, url, **kw):
        assert kw["buyer_request"]["time"] == "14:20"
        assert kw["city_name"] == "太原" and len(kw["buyer_request"]["seats"]) == 2
        original.quote_id = "new-city"
        original.status = "READY"
        quotes.create(original)
        conv.quote_id = original.quote_id
        return FlowResult(
            ok=True, template_keys=["QUOTE_CARD"], messages=["new verified quote"]
        )

    service.flow.on_screenshot = Mock(side_effect=verified)
    r = nxt.execute(
        "supplement_city",
        dict(city="太原", request_fields=list(fields), field_evidence=fields),
    )
    assert r["status"] == "REQUEST_PLAN_STAGED"
    service.flow.on_screenshot.assert_not_called()
    r = nxt.execute(
        "supplement_ticket_details", {k: v for k, v in fields.items() if k != "city"}
    )
    assert r["ok"] and session.conversation.quote_id == "new-city"
    assert not session.image_context["ticket_details"]["cleared_fields"]
    assert not project(session.conversation, session.image_context)["blocked"]
    service.flow.on_screenshot.assert_called_once()


@pytest.mark.parametrize("case", ["identity", "paid", "disabled", "state"])
def test_expired_recovery_preserves_original_identity_payment_and_automation_guards(
    actual_flow, case
):
    from domain.order import Order
    from domain.conversation import ConvState

    make, session, quotes, service, _ = actual_flow
    old = session.conversation.quote_id
    session.image_context["agent_request_plan"] = dict(
        **t_identity(session),
        pending=True,
        source_image=session.image_context["url"],
        expected=["time"],
        values={},
    )
    session.image_context["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=25)
    ).isoformat()
    t = make("重新核对")
    t.request_plan_enabled = True
    if case == "identity":
        session.image_context["buyer_id"] = "another"
    if case == "disabled":
        t.cfg.recognize_quote.auto_recognize = False
    if case == "state":
        session.conversation.state = ConvState.AWAIT_TICKET
    if case == "paid":
        service.order_store.create(
            Order(
                order_id="paid",
                quote_id=old,
                idempotency_key="paid",
                **t.identity,
                payment_state="CONFIRMED",
                state="PAID",
                amount_fen=1,
            )
        )
    r = t.execute("recover_image_context", {})
    assert not r["ok"] and "request_revalidated_at" not in session.image_context
    assert quotes.get(old).status == "READY" and not t.read_quote()["quote"]["tradable"]
    service.flow.api.ch4_show_list.assert_not_called()


def t_identity(session):
    return {
        k: getattr(session.conversation, k)
        for k in (
            "tenant_id",
            "shop_id",
            "buyer_id",
            "chat_id",
            "demand_id",
            "demand_rev",
        )
    }


def test_completed_wanda_quote_does_not_gain_a_new_image_age_ttl(actual_flow):
    make, session, quotes, service, _ = actual_flow
    session.image_context["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=25)
    ).isoformat()
    assert make("查价格").read_quote()["quote"]["tradable"]


def test_pending_request_blocks_trade_but_not_read_only_platform_payment_facts(
    actual_flow,
):
    from application.purchase_actions import inspect
    from domain.order import Order

    make, session, quotes, service, t = seeded(actual_flow, "换场次")
    qid = session.conversation.quote_id
    t.execute("clarify_ticket_request", dict(invalidated_fields=["time"]))
    service.order_store.create(
        Order(
            order_id="paid-later",
            quote_id=qid,
            idempotency_key="paid-later",
            **t.identity,
            demand_id=session.conversation.demand_id,
            demand_rev=session.conversation.demand_rev,
            provider_order_id="123",
            platform_order_id="123",
            provider="WANDA",
            payment_state="CONFIRMED",
            state="PAID",
            amount_fen=10000,
        )
    )
    with patch(
        "application.purchase_actions.platform_order",
        return_value=dict(
            orderStatus=2,
            payment="10000",
            payTime="2030-10-18T00:00:00Z",
            grouponPending=False,
        ),
    ) as read:
        facts = inspect(t)
    read.assert_called_once()
    assert (
        facts["ok"]
        and facts["platform_status"] == 2
        and facts["platform_amount_yuan"] == "100.00"
    )
    assert (
        facts["payment_blocked"]
        and not facts["quote_valid"]
        and facts["unpaid"] is False
    )
