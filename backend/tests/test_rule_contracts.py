from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.rule_contracts import AiAssistResult, ReplyPlan, RuleDecision
from app.rule_templates import RULE_TEMPLATES, validate_reply_plan


def test_rule_decision_and_ai_assist_contracts_are_bounded_and_forbid_authority_fields() -> None:
    decision = RuleDecision.model_validate({
        "state_before": "PRICE_CHANGING",
        "state_after": "WAITING_PAYMENT",
        "transition_code": "price_change_verified",
        "state_revision": 8,
        "actions": [{"type": "send_message", "template_key": "flow.price_change.verified"}],
    })
    assert decision.state_revision == 8

    assist = AiAssistResult.model_validate({
        "intent_candidate": "ask_price",
        "confidence": 0.82,
        "source_message_ids": ["message-1"],
    })
    assert assist.intent_candidate == "ask_price"

    with pytest.raises(ValidationError):
        AiAssistResult.model_validate({"confirmed_quote_record_id": "quote-1", "confidence": 1})
    with pytest.raises(ValidationError):
        RuleDecision.model_validate({
            "state_before": "NEW", "state_after": "COLLECTING",
            "transition_code": "x", "state_revision": 1,
            "actions": [{}] * 9,
        })


def test_verified_price_reply_requires_official_readback_evidence() -> None:
    plan = ReplyPlan.model_validate({
        "template_key": "flow.price_change.verified",
        "template_version": 1,
        "variables": {"verified_total_amount": "88.00"},
        "protected_facts": {"price_change_status": "succeeded"},
        "gate_evidence": {
            "verified_total_amount": {
                "source": "official_order_readback",
                "value": "88.00",
            },
        },
        "send_policy": "once_per_state_revision",
    })

    rendered = validate_reply_plan(plan, state="WAITING_PAYMENT")
    assert rendered == "改价已完成，订单金额已调整为88.00，请在订单页核对后付款。"

    missing_evidence = plan.model_copy(update={"gate_evidence": {}})
    with pytest.raises(ValueError, match="reply_plan_variable_evidence_missing"):
        validate_reply_plan(missing_evidence, state="WAITING_PAYMENT")

    wrong_source = plan.model_copy(update={
        "gate_evidence": {
            "verified_total_amount": {"source": "buyer_message", "value": "88.00"},
        },
    })
    with pytest.raises(ValueError, match="reply_plan_variable_source_invalid"):
        validate_reply_plan(wrong_source, state="WAITING_PAYMENT")


def test_transaction_templates_reject_ai_rephrasing_and_wrong_state() -> None:
    plan = ReplyPlan.model_validate({
        "template_key": "flow.payment.received",
        "template_version": 1,
        "variables": {},
        "protected_facts": {"payment_status": "verified_paid"},
        "gate_evidence": {},
        "optional_ai_text": "AI说已经出票成功",
        "send_policy": "once_per_state_revision",
    })
    with pytest.raises(ValueError, match="reply_plan_ai_rephrase_forbidden"):
        validate_reply_plan(plan, state="PAID_WAITING_FULFILLMENT")

    no_ai = plan.model_copy(update={"optional_ai_text": None})
    with pytest.raises(ValueError, match="reply_plan_state_not_allowed"):
        validate_reply_plan(no_ai, state="QUOTED")


def test_every_fixed_template_renders_in_each_declared_state_with_approved_evidence() -> None:
    for key, definition in RULE_TEMPLATES.items():
        variables = {name: f"{name}-value" for name in definition.required_variables}
        evidence = {
            name: {
                "source": sorted(definition.variable_sources[name])[0],
                "value": value,
                "reference_id": f"test-{name}",
            }
            for name, value in variables.items()
        }
        plan = ReplyPlan.model_validate({
            "template_key": key, "template_version": definition.version,
            "variables": variables, "protected_facts": {},
            "gate_evidence": evidence, "send_policy": "once_per_state_revision",
        })
        for state in definition.allowed_states:
            rendered = validate_reply_plan(plan, state=state)
            assert rendered
            assert all(phrase in rendered for phrase in definition.required_phrases)


def test_fixed_template_catalog_declares_required_safety_metadata() -> None:
    expected = {
        "flow.intake.request_image_count", "flow.intake.ask_missing_all",
        "flow.quote.processing", "flow.quote.ready", "flow.quote.confirm_clarify",
        "flow.order.submit_unpaid", "flow.order.detected_hold_payment",
        "flow.price_change.pending", "flow.price_change.verified",
        "flow.price_change.failed", "flow.price_change.unknown",
        "flow.payment.received", "flow.fulfillment.in_progress",
        "flow.fulfillment.ticket_sent", "flow.manual.created",
    }
    assert expected <= RULE_TEMPLATES.keys()
    for definition in RULE_TEMPLATES.values():
        assert definition.allowed_states
        assert definition.required_phrases
        assert definition.forbidden_claims
        assert definition.retry_policy
        assert definition.timeout_policy
