from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .reply_template_store import ReplyTemplates
from .rule_contracts import GateEvidence, ReplyPlan


_REPLY_DEFAULTS = ReplyTemplates()


@dataclass(frozen=True)
class RuleTemplateDefinition:
    key: str
    version: int
    text: str
    allowed_states: frozenset[str]
    required_variables: frozenset[str] = frozenset()
    variable_sources: Mapping[str, frozenset[str]] = field(default_factory=dict)
    required_phrases: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = ()
    retry_policy: str = "same_revision_once"
    timeout_policy: str = "fallback_to_fixed_template"
    handoff_reason: str | None = None
    ai_rephrase_allowed: bool = False


def _definition(
    key: str, text: str, states: set[str], *, variables: set[str] | None = None,
    sources: Mapping[str, set[str]] | None = None, required: tuple[str, ...],
    handoff: str | None = None, ai: bool = False,
) -> RuleTemplateDefinition:
    return RuleTemplateDefinition(
        key=key, version=1, text=text, allowed_states=frozenset(states),
        required_variables=frozenset(variables or set()),
        variable_sources={name: frozenset(values) for name, values in (sources or {}).items()},
        required_phrases=required,
        forbidden_claims=("未经核验的价格", "未经核验的付款状态", "未经核验的出票状态"),
        handoff_reason=handoff, ai_rephrase_allowed=ai,
    )


RULE_TEMPLATES: dict[str, RuleTemplateDefinition] = {
    "flow.intake.request_image_count": _definition(
        "flow.intake.request_image_count",
        "请发送能看清影院、影片、日期场次和座位的截图，并告诉我需要几张。",
        {"NEW", "COLLECTING"}, required=("请发送", "并告诉我需要几张"), ai=True,
    ),
    "flow.intake.ask_missing_all": _definition(
        "flow.intake.ask_missing_all", "还缺：{missing_fields}。请一次补充完整后再为您核价。",
        {"COLLECTING"}, variables={"missing_fields"},
        sources={"missing_fields": {"rule_state"}}, required=("还缺：", "请一次补充完整"),
    ),
    "flow.quote.processing": _definition(
        "flow.quote.processing", "正在核验场次和实时价格，请先不要下单或付款。",
        {"FACTS_READY"}, required=("正在核验", "请先不要下单或付款"),
    ),
    "flow.quote.ready": _definition(
        "flow.quote.ready",
        "已核验：{showtime_summary}，{ticket_count}张，报价合计{quoted_total_amount}元。"
        "请直接提交订单；系统按已确认张数核算，改价完成前请勿付款。",
        {"QUOTED"}, variables={"showtime_summary", "ticket_count", "quoted_total_amount"},
        sources={
            "showtime_summary": {"valid_quote_record"}, "ticket_count": {"valid_quote_record"},
            "quoted_total_amount": {"valid_quote_record"},
        }, required=("系统按已确认张数核算", "改价完成前请勿付款"),
    ),
    "flow.quote.confirm_clarify": _definition(
        "flow.quote.confirm_clarify",
        "请直接告诉我需要几张；确认数量后直接提交订单，拍下后先不要付款。",
        {"QUOTED"}, required=("需要几张", "先不要付款"),
    ),
    "flow.order.submit_unpaid": _definition(
        "flow.order.submit_unpaid", "请直接提交订单，系统按已确认的{ticket_count}张核算并改价，先不要付款。",
        {"CONFIRMED"}, variables={"ticket_count"},
        sources={"ticket_count": {"rule_state", "valid_quote_record"}}, required=("先不要付款",),
    ),
    "flow.order.detected_hold_payment": _definition(
        "flow.order.detected_hold_payment", "已看到订单，正在核验并改价；完成前请勿付款。",
        {"ORDER_BOUND", "PRICE_CHANGING"}, required=("完成前请勿付款",),
    ),
    "flow.price_change.pending": _definition(
        "flow.price_change.pending", "改价结果尚未核验，请保持待付款。",
        {"PRICE_CHANGING"}, required=("尚未核验", "请保持待付款"),
    ),
    "flow.price_change.verified": _definition(
        "flow.price_change.verified",
        _REPLY_DEFAULTS.price_change_confirmation_template.replace("{订单金额}", "{verified_total_amount}"),
        {"WAITING_PAYMENT"}, variables={"verified_total_amount"},
        sources={"verified_total_amount": {"official_order_readback"}},
        required=("改价已完成", "核对后付款"),
    ),
    "flow.price_change.failed": _definition(
        "flow.price_change.failed",
        _REPLY_DEFAULTS.price_change_failure_template.replace("{失败原因}", "平台改价未完成"),
        {"MANUAL_HOLD"}, required=("不要付款", "等待重新核对"), handoff="price_change_failed",
    ),
    "flow.price_change.unknown": _definition(
        "flow.price_change.unknown",
        _REPLY_DEFAULTS.price_change_failure_template.replace("{失败原因}", "结果尚未完成官方核验"),
        {"PRICE_CHANGING", "MANUAL_HOLD"}, required=("不要付款", "等待重新核对"),
        handoff="price_change_unknown",
    ),
    "flow.payment.received": _definition(
        "flow.payment.received", "付款已确认，订单已进入出票流程；出票后会发送取票信息。",
        {"PAID_WAITING_FULFILLMENT"}, required=("付款已确认", "出票流程"),
    ),
    "flow.payment.manual_review": _definition(
        "flow.payment.manual_review",
        "检测到订单已付款，但付款前未完成金额核验，已暂停出票并转人工处理，请稍候。",
        {"MANUAL_HOLD"}, required=("付款前未完成金额核验", "已暂停出票", "转人工处理"),
        handoff="payment_before_amount_verification",
    ),
    "flow.fulfillment.in_progress": _definition(
        "flow.fulfillment.in_progress", "订单正在出票处理中，请耐心等待。",
        {"FULFILLMENT_IN_PROGRESS"}, required=("正在出票处理",),
    ),
    "flow.fulfillment.ticket_sent": _definition(
        "flow.fulfillment.ticket_sent", "出票信息已经发送，请按已发送的取票指引核对。",
        {"TICKET_SENT"}, required=("出票信息已经发送",),
    ),
    "flow.fulfillment.liangpiao_ticketed": _definition(
        "flow.fulfillment.liangpiao_ticketed", _REPLY_DEFAULTS.liangpiao_ticketed_template,
        {"TICKET_SENT", "COMPLETED"}, variables={"取票码", "取票链接"},
        sources={"取票码": {"audited_fulfillment_event"}, "取票链接": {"audited_fulfillment_event"}},
        required=("出票成功", "取票码"),
    ),
    "flow.fulfillment.liangpiao_failed": _definition(
        "flow.fulfillment.liangpiao_failed", _REPLY_DEFAULTS.liangpiao_failed_template,
        {"MANUAL_HOLD"}, variables={"失败原因"}, sources={"失败原因": {"audited_fulfillment_event"}},
        required=("出票失败", "释放"), handoff="liangpiao_ticketing_failed",
    ),
    "flow.fulfillment.liangpiao_failed_switch_offer": _definition(
        "flow.fulfillment.liangpiao_failed_switch_offer",
        "特惠渠道出票失败了，订单资金已按平台结果释放。需要我按同场次的一口价继续出票吗？",
        {"MANUAL_HOLD"}, required=("出票失败", "一口价", "继续出票"),
        handoff="liangpiao_fixed_switch_pending",
    ),
    "flow.fulfillment.fixed_quote_ready": _definition(
        "flow.fulfillment.fixed_quote_ready",
        "可以按一口价继续出票：{quoted_total_amount}元（报价有效至{quote_expires_at}）。请确认后重新拍下，付款前不要提交其他订单。",
        {"QUOTED"}, variables={"quoted_total_amount", "quote_expires_at"},
        sources={
            "quoted_total_amount": {"valid_quote_record"},
            "quote_expires_at": {"valid_quote_record"},
        }, required=("一口价", "请确认后重新拍下"),
    ),
    "flow.manual.created": _definition(
        "flow.manual.created", "当前信息需要人工核验，请勿付款；处理完成后会继续通知。",
        {"MANUAL_HOLD", "ORDER_UNVERIFIED"}, required=("需要人工核验", "请勿付款"),
        handoff="manual_review_required",
    ),
}


def validate_reply_plan(plan: ReplyPlan, *, state: str) -> str:
    definition = RULE_TEMPLATES.get(plan.template_key)
    if definition is None:
        raise ValueError("reply_plan_template_unknown")
    if plan.template_version != definition.version:
        raise ValueError("reply_plan_template_version_mismatch")
    if state not in definition.allowed_states:
        raise ValueError("reply_plan_state_not_allowed")
    if plan.optional_ai_text and not definition.ai_rephrase_allowed:
        raise ValueError("reply_plan_ai_rephrase_forbidden")
    missing = definition.required_variables - plan.variables.keys()
    if missing:
        raise ValueError("reply_plan_required_variable_missing")
    for variable, allowed_sources in definition.variable_sources.items():
        raw_evidence = plan.gate_evidence.get(variable)
        if raw_evidence is None:
            raise ValueError("reply_plan_variable_evidence_missing")
        evidence = GateEvidence.model_validate(raw_evidence)
        if evidence.source not in allowed_sources:
            raise ValueError("reply_plan_variable_source_invalid")
        if evidence.value != plan.variables.get(variable):
            raise ValueError("reply_plan_variable_evidence_mismatch")
    try:
        rendered = definition.text.format_map({key: str(value) for key, value in plan.variables.items()})
    except KeyError as error:
        raise ValueError("reply_plan_required_variable_missing") from error
    for phrase in (*definition.required_phrases, *plan.required_phrases):
        if phrase not in rendered:
            raise ValueError("reply_plan_required_phrase_missing")
    if plan.optional_ai_text:
        rendered = plan.optional_ai_text
        for phrase in definition.required_phrases:
            if phrase not in rendered:
                raise ValueError("reply_plan_required_phrase_missing")
    return rendered
