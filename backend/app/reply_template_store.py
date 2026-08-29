from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KeywordReplyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    keywords: list[str] = Field(min_length=1, max_length=10)
    match_mode: Literal["exact", "contains"] = "contains"
    reply: str = Field(min_length=1, max_length=1_000)
    image_asset_id: str | None = Field(default=None, pattern=r"^ki-[0-9a-f]{40}$")
    image_filename: str | None = Field(default=None, max_length=160)
    image_tenant_id: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized or any(len(value) > 50 for value in normalized):
            raise ValueError("keyword_reply_keywords_invalid")
        return normalized

    @field_validator("reply")
    @classmethod
    def reject_template_variables(cls, value: str) -> str:
        if "{" in value or "}" in value:
            raise ValueError("keyword_reply_variables_unsupported")
        if re.search(r"(?:[¥￥]\s*\d|\d+(?:\.\d{1,2})?\s*元)", value):
            raise ValueError("keyword_reply_price_claim_unsupported")
        return value.strip()


class ReplyTemplates(BaseModel):
    # Forward-compatible readers must survive a newer template catalog during
    # rolling deployment. Writers still emit only fields known by this model.
    model_config = ConfigDict(extra="ignore")

    recognition_waiting_template: str = Field(
        default="正在识别截图并核对影院、场次和座位，请稍候。",
        min_length=1,
        max_length=1_000,
    )
    recognition_failure_other_template: str = Field(
        default="这张截图暂时无法完成识别：{失败原因}\n请重新发送清晰、完整的当前场次与选座截图。",
        min_length=1,
        max_length=2_000,
    )
    recognition_template: str = Field(
        default="已识别到这张电影票截图：\n影片：{影片}\n城市：{城市}\n影院：{影院}\n日期：{日期}\n场次：{场次}\n影厅：{影厅}\n{座位}\n{报价内容}",
        min_length=1,
        max_length=4_000,
    )
    cinema_match_failure_template: str = Field(
        default="我看到了截图中的影院是{影院}，但暂时无法唯一匹配万达官方门店。请补充“城市＋影院全名”，我会继续按原截图核对。",
        min_length=1,
        max_length=2_000,
    )
    unsupported_cinema_template: str = Field(
        default="目前仅支持万达影城，截图中的其他影院暂不支持代订。",
        min_length=1,
        max_length=1_000,
    )
    missing_fields_template: str = Field(
        default="截图中还缺少：{缺失信息}。请补充这些信息后，我会继续按原截图核价。",
        min_length=1,
        max_length=2_000,
    )
    showtime_changed_template: str = Field(
        default="当前场次信息可能已经变化。为避免报错座位，请重新发送场次和座位都清楚的最新截图。",
        min_length=1,
        max_length=2_000,
    )
    exact_quote_template: str = Field(
        default="{报价名称}：{逐座报价}\n报价合计：{报价合计}\n{报价说明}\n最终支付金额仍以正式下单和支付结果为准。",
        min_length=1,
        max_length=3_000,
    )
    area_quote_template: str = Field(
        default="{报价名称}：{报价单价}\n{张数提示}\n{报价说明}\n最终支付金额仍以正式下单和支付结果为准。",
        min_length=1,
        max_length=3_000,
    )
    quote_unavailable_template: str = Field(
        default="实时报价暂未取得：{失败原因}\n请刷新场次截图后再发我核价。",
        min_length=1,
        max_length=2_000,
    )
    quote_expired_template: str = Field(
        default="之前的实时报价已超过15分钟，不能继续按旧价格下单。请重新发送当前场次和选座截图，我会重新核价。",
        min_length=1,
        max_length=1_000,
    )
    no_quote_template: str = Field(
        default="请核对影片、影院和场次；截图金额不作为最终报价。",
        min_length=1,
        max_length=2_000,
    )
    guidance_template: str = Field(
        default="你好，请上传或发送当前电影场次和选座截图，我会识别影院、影片、场次和座位并查询实时报价。",
        min_length=1,
        max_length=2_000,
    )
    quote_above_fan_price_template: str = Field(
        default="本次后台实时报价为{报价金额}，高于粉丝自购价{粉丝自购价}，请先不要拍下，等待进一步确认。",
        min_length=1,
        max_length=2_000,
    )
    payment_success_pending_ticket_template: str = Field(
        default="已确认支付成功，订单正在等待出票；出票结果以订单状态和后续通知为准。",
        min_length=1,
        max_length=1_000,
    )
    order_shipped_template: str = Field(
        default="订单已付款并已发货，请留意已经发送的出票信息；后续以订单状态和人工通知为准。",
        min_length=1,
        max_length=1_000,
    )
    movie_reminder_template: str = Field(
        default="观影提醒：{movie} 将于 {showtime} 放映，影院：{cinema}。记得提前取票、检票入场，祝您观影愉快～",
        min_length=1,
        max_length=1_000,
    )
    order_pending_with_quote_template: str = Field(
        default="已看到您拍下的订单，并找到对应报价记录{报价金额}；正在核对订单金额，请先不要付款。",
        min_length=1,
        max_length=1_000,
    )
    order_pending_without_quote_template: str = Field(
        default="已看到您拍下的订单，但暂未找到可确认的报价记录；请先不要付款，并重新发送当前场次与选座截图。",
        min_length=1,
        max_length=1_000,
    )
    price_change_confirmation_template: str = Field(
        default="改价已完成，订单金额已调整为{订单金额}，请在订单页核对后付款。",
        min_length=1,
        max_length=1_000,
    )
    post_order_recognition_reprice_template: str = Field(
        default="拍下后已重新识别截图并取得报价{报价金额}，正在重新核对订单金额，请先不要付款。",
        min_length=1,
        max_length=1_000,
    )
    price_change_failure_template: str = Field(
        default="订单改价未完成：{失败原因}\n请先不要付款，等待重新核对。",
        min_length=1,
        max_length=1_000,
    )
    quote_confirmation_clarify_template: str = Field(
        default="请直接告诉我需要几张；确认数量后直接提交订单，拍下后先不要付款。",
        min_length=1, max_length=1_000,
    )
    quote_ticket_count_request_template: str = Field(
        default="请问需要几张？",
        min_length=1, max_length=1_000,
    )
    quote_quantity_order_guidance_template: str = Field(
        default=(
            "已确认需要{张数}张，{报价单价}一张，合计{报价合计}元。\n"
            "{座位说明}\n"
            "{下单引导}"
        ),
        min_length=1, max_length=2_000,
    )
    quote_quantity_marked_seat_template: str = Field(
        default="人工会按照您图片中标记的位置出票，请放心下单。",
        min_length=1, max_length=1_000,
    )
    quote_quantity_flexible_seat_template: str = Field(
        default="人工会根据实时可售情况安排座位，请放心下单。",
        min_length=1, max_length=1_000,
    )
    quote_quantity_default_seat_template: str = Field(
        default="人工会根据您确认的座位要求和实时可售情况安排出票，请放心下单。",
        min_length=1, max_length=1_000,
    )
    order_submit_unpaid_template: str = Field(
        default=(
            "请直接提交订单，拍下后先不要付款；系统会按已确认的{张数}张核算并改价；"
            "收到“改价已完成，可以付款”的通知后再付款。"
        ),
        min_length=1, max_length=1_000,
    )
    order_detected_hold_payment_template: str = Field(
        default="已看到订单，正在核验并改价；完成前请勿付款。", max_length=1_000,
    )
    pending_order_image_quote_unavailable_template: str = Field(
        default=(
            "已看到您发送的待付款订单截图，但当前订单还没有绑定有效确认报价。"
            "请发送包含影院、影片、日期和开场时间的完整场次或选座截图；"
            "核价后回复需要的张数或“确认报价”，我会继续处理改价。"
        ),
        min_length=1, max_length=1_000,
    )
    payment_manual_review_template: str = Field(
        default="检测到订单已付款，但付款前未完成金额核验，已暂停出票并转人工处理，请稍候。",
        min_length=1, max_length=1_000,
    )
    manual_review_template: str = Field(
        default="当前信息需要人工核验，请勿付款；处理完成后会继续通知。",
        min_length=1, max_length=1_000,
    )
    ai_disabled_structured_intake_template: str = Field(
        default="识图辅助暂未开启，请按以下格式发送：城市、影院、影片、日期、场次、影厅、座位、张数。",
        min_length=1, max_length=1_000,
    )
    order_before_quote_confirmation_template: str = Field(
        default="已看到订单并取得当前报价{报价金额}。由于订单早于本次报价，请明确回复“确认报价”；确认前请勿付款。",
        min_length=1, max_length=1_000,
    )
    paid_mismatch_closed_template: str = Field(
        default="检测到付款金额与官方核验金额不一致，订单已关闭，款项将按平台流程原路退回。请退款到账后重新拍下，并等待改价完成通知后再付款。",
        min_length=1, max_length=1_000,
    )
    paid_mismatch_refund_template: str = Field(
        default="检测到付款金额与官方核验金额不一致，但系统暂未确认订单已关闭。请立即在订单页申请退款，退款完成后重新拍下，并等待改价完成通知后再付款。",
        min_length=1, max_length=1_000,
    )
    keyword_replies: list[KeywordReplyRule] = Field(default_factory=list, max_length=30)
    revision: int = Field(default=0, ge=0)
    updated_at: str | None = None


_TEMPLATE_LABELS = {
    "recognition_waiting_template": "识别等待文案",
    "recognition_failure_other_template": "识别失败文案－其它",
    "recognition_template": "识图主体",
    "cinema_match_failure_template": "影院匹配失败补问文案",
    "unsupported_cinema_template": "非万达影院文案",
    "missing_fields_template": "截图缺项补问文案",
    "showtime_changed_template": "场次信息变化文案",
    "exact_quote_template": "精确座位报价",
    "area_quote_template": "区域单价报价",
    "quote_unavailable_template": "无法取得报价",
    "quote_expired_template": "报价过期文案",
    "no_quote_template": "未执行报价",
    "guidance_template": "上传引导",
    "quote_above_fan_price_template": "报价超过粉丝自购价文案",
    "payment_success_pending_ticket_template": "支付成功待出票文案",
    "order_shipped_template": "已发货订单文案",
    "movie_reminder_template": "观影提醒",
    "order_pending_with_quote_template": "拍下未确认－有报价记录",
    "order_pending_without_quote_template": "拍下未确认－无报价记录",
    "price_change_confirmation_template": "改价完成文案",
    "post_order_recognition_reprice_template": "拍下后识别成功重新改价",
    "price_change_failure_template": "改价失败文案",
    "quote_confirmation_clarify_template": "报价确认补问",
    "quote_ticket_count_request_template": "报价缺少张数补问",
    "quote_quantity_order_guidance_template": "张数确认完整回复",
    "quote_quantity_marked_seat_template": "张数确认-图片标记座位说明",
    "quote_quantity_flexible_seat_template": "张数确认-实时可售安排说明",
    "quote_quantity_default_seat_template": "张数确认-通用座位说明",
    "order_submit_unpaid_template": "确认后下单引导",
    "order_detected_hold_payment_template": "已拍下等待改价",
    "pending_order_image_quote_unavailable_template": "待付款订单截图缺少有效报价",
    "payment_manual_review_template": "提前付款人工核验",
    "manual_review_template": "通用人工核验",
    "ai_disabled_structured_intake_template": "识图关闭结构化引导",
    "order_before_quote_confirmation_template": "订单早于报价确认",
    "paid_mismatch_closed_template": "付款金额不符且已关闭",
    "paid_mismatch_refund_template": "付款金额不符退款引导",
}


_SAFE_GLOBAL_VARIABLES = {
    "影片", "城市", "影院", "日期", "场次", "影厅", "座位",
    "报价名称", "逐座报价", "报价合计", "报价单价", "报价金额",
}


_ALLOWED_VARIABLES = {
    "recognition_waiting_template": set(),
    "recognition_failure_other_template": {"失败原因"},
    "recognition_template": {
        "影片", "城市", "影院", "日期", "场次", "影厅", "座位",
        "报价内容", "报价单价", "报价合计", "报价金额",
    },
    "cinema_match_failure_template": {"影院"},
    "unsupported_cinema_template": set(),
    "missing_fields_template": {"缺失信息"},
    "showtime_changed_template": set(),
    "exact_quote_template": {"影片", "城市", "影院", "日期", "场次", "影厅", "座位", "报价名称", "逐座报价", "报价合计", "报价说明", "规则版本"},
    "area_quote_template": {"影片", "城市", "影院", "日期", "场次", "影厅", "座位", "报价名称", "报价单价", "张数提示", "报价说明", "规则版本"},
    "quote_unavailable_template": {"失败原因"},
    "quote_expired_template": set(),
    "no_quote_template": set(),
    "guidance_template": set(),
    "quote_above_fan_price_template": {"报价金额", "粉丝自购价"},
    "payment_success_pending_ticket_template": set(),
    "order_shipped_template": set(),
    "movie_reminder_template": {"movie", "showtime", "cinema", "date", "hall", "seats"},
    "order_pending_with_quote_template": {"报价金额"},
    "order_pending_without_quote_template": set(),
    "price_change_confirmation_template": {"订单金额"},
    "post_order_recognition_reprice_template": {"报价金额"},
    "price_change_failure_template": {"失败原因"},
    "quote_confirmation_clarify_template": set(),
    "quote_ticket_count_request_template": set(),
    "quote_quantity_order_guidance_template": {
        "张数", "报价单价", "报价合计", "座位说明", "下单引导",
    },
    "quote_quantity_marked_seat_template": set(),
    "quote_quantity_flexible_seat_template": set(),
    "quote_quantity_default_seat_template": set(),
    "order_submit_unpaid_template": {"张数"},
    "order_detected_hold_payment_template": set(),
    "pending_order_image_quote_unavailable_template": set(),
    "payment_manual_review_template": set(),
    "manual_review_template": set(),
    "ai_disabled_structured_intake_template": set(),
    "order_before_quote_confirmation_template": {"报价金额"},
    "paid_mismatch_closed_template": set(),
    "paid_mismatch_refund_template": set(),
}


def render_template(template: str, variables: dict[str, object]) -> str:
    normalized = {key: str(value or "") for key, value in variables.items()}
    formatter = Formatter()
    empty_fact_labels = {"影片：", "城市：", "影院：", "日期：", "场次：", "影厅：", "座位：", "报价合计："}
    lines: list[str] = []
    for source_line in template.splitlines():
        names = {name for _, name, _, _ in formatter.parse(source_line) if name}
        if any(not normalized.get(name, "").strip() for name in names):
            continue
        rendered_line = source_line.format_map(normalized).rstrip()
        if rendered_line.strip() in empty_fact_labels:
            continue
        # Blank source lines are intentional visual section separators. Keep
        # them instead of flattening an operator-authored multiline template.
        lines.append(rendered_line if rendered_line.strip() else "")
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)[:4_000]


class ReplyTemplateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()

    def _validate_variables(self, values: ReplyTemplates) -> None:
        formatter = Formatter()
        for field, field_allowed in _ALLOWED_VARIABLES.items():
            allowed = field_allowed | _SAFE_GLOBAL_VARIABLES
            template = getattr(values, field)
            try:
                names = {name for _, name, spec, conversion in formatter.parse(template) if name}
            except ValueError as error:
                raise ValueError("invalid_template_syntax") from error
            unsupported = {
                name for name in names
                if "." in name or "[" in name or name not in allowed
            }
            if unsupported:
                label = _TEMPLATE_LABELS[field]
                invalid_text = "、".join(f"{{{name}}}" for name in sorted(unsupported))
                allowed_text = "、".join(f"{{{name}}}" for name in sorted(allowed)) or "无变量"
                raise ValueError(
                    f"unsupported_template_variable: {label}不支持变量{invalid_text}；可用变量：{allowed_text}"
                )
            if any(spec or conversion for _, name, spec, conversion in formatter.parse(template) if name):
                raise ValueError("unsupported_template_variable")
        payment_hold_fields = (
            "order_submit_unpaid_template",
            "order_detected_hold_payment_template", "price_change_failure_template",
            "manual_review_template",
        )
        for field in payment_hold_fields:
            text = getattr(values, field)
            if field == "order_detected_hold_payment_template" and not text.strip():
                continue
            if "付款" not in text or not any(marker in text for marker in ("不要付款", "别付款", "勿付款", "暂停付款")):
                if field == "order_detected_hold_payment_template":
                    raise ValueError(
                        f"required_safety_phrase_missing:{field}:等待改价期间必须明确写明先不要付款；"
                        "付款完成说明请配置到改价完成文案"
                    )
                raise ValueError(f"required_safety_phrase_missing:{field}")
        required_groups = {
            "quote_quantity_order_guidance_template": (
                ("{张数}",), ("{报价单价}",), ("{报价合计}",), ("{座位说明}",),
                ("{下单引导}",),
            ),
            "price_change_confirmation_template": (("{订单金额}",), ("付款", "支付")),
            "payment_success_pending_ticket_template": (("支付", "付款"), ("出票",)),
            "payment_manual_review_template": (("暂停出票",), ("人工",)),
            "order_shipped_template": (("发货", "出票"),),
        }
        for field, groups in required_groups.items():
            text = getattr(values, field)
            if any(not any(candidate in text for candidate in group) for group in groups):
                raise ValueError(f"required_safety_phrase_missing:{field}")

    def current(self) -> ReplyTemplates:
        with self._lock:
            if not self._path.exists():
                return ReplyTemplates()
            try:
                loaded = ReplyTemplates.model_validate_json(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return ReplyTemplates()
            updates: dict[str, str] = {}
            legacy_confirmation = "为避免误改价，请明确回复“确认报价”，或按当前报价张数直接拍下订单。"
            if loaded.quote_confirmation_clarify_template == legacy_confirmation:
                updates["quote_confirmation_clarify_template"] = ReplyTemplates().quote_confirmation_clarify_template
            old_quantity_clarify = (
                "请直接告诉我需要几张；确认数量后可按当前张数拍下订单，拍下后先不要付款。"
            )
            if loaded.quote_confirmation_clarify_template == old_quantity_clarify:
                updates["quote_confirmation_clarify_template"] = ReplyTemplates().quote_confirmation_clarify_template
            legacy_count_request = "好的，请先告诉我需要几张；确认数量后我再引导您提交订单。"
            if loaded.quote_ticket_count_request_template == legacy_count_request:
                updates["quote_ticket_count_request_template"] = ReplyTemplates().quote_ticket_count_request_template
            old_quantity_guidance = (
                "已确认需要{张数}张，{报价单价}一张，合计{报价合计}元。\n"
                "{座位说明}\n"
                "请提交{张数}张订单，拍下后先不要付款；系统会将订单金额调整为{报价合计}元，"
                "收到“改价已完成，可以付款”的通知后再付款。"
            )
            quantity_independent_guidance = (
                "已确认需要{张数}张，{报价单价}一张，合计{报价合计}元。\n"
                "{座位说明}\n"
                "请直接提交订单，拍下后先不要付款；系统会按已确认的{张数}张将订单金额调整为{报价合计}元，"
                "收到“改价已完成，可以付款”的通知后再付款。"
            )
            if loaded.quote_quantity_order_guidance_template in {
                old_quantity_guidance, quantity_independent_guidance,
            }:
                updates["quote_quantity_order_guidance_template"] = (
                    ReplyTemplates().quote_quantity_order_guidance_template
                )
            old_order_submit = (
                "请按{张数}张提交订单，拍下后先不要付款；"
                "收到“改价已完成，可以付款”的通知后再付款。"
            )
            if loaded.order_submit_unpaid_template == old_order_submit:
                updates["order_submit_unpaid_template"] = ReplyTemplates().order_submit_unpaid_template
            return loaded.model_copy(update=updates) if updates else loaded

    def save(self, update: dict[str, Any]) -> ReplyTemplates:
        with self._lock:
            current = self.current()
            payload = current.model_dump()
            payload.update({key: value for key, value in update.items() if key in ReplyTemplates.model_fields})
            payload["revision"] = current.revision + 1
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            saved = ReplyTemplates.model_validate(payload)
            self._validate_variables(saved)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.{uuid4().hex}.tmp")
            temporary.write_text(saved.model_dump_json(), encoding="utf-8")
            try:
                os.chmod(temporary, 0o600)
                os.replace(temporary, self._path)
            finally:
                temporary.unlink(missing_ok=True)
            return saved
