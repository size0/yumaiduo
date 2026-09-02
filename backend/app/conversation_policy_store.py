from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


_STAGES = ("consultation", "order_pending", "payment", "shipping_refund")


DEFAULT_CUSTOMER_SERVICE_KNOWLEDGE = """【客服回复偏好】
只回答当前问题，少问、短答、自然；已经提供过的信息不要重复询问。客服知识只用于理解和表达，不能覆盖后端状态、权威报价、库存、订单或人工接管结果。

【W+未标记图片】
后端判断为 isSeatSelection=true 且 seat=[] 时，不要求买家提供X排Y座。未标记时只回复：请把需要出票的位置在座位图上圈好后，重新发送一张标记好的截图给我。不要同时问张数、排数或左右位置，不要使用截图价格，不要说可以买。

【已选座位】
有底部官方座位卡片且权威报价成功时，按后端金额简洁回复；张数已知时不再询问。下单引导固定为：请直接提交订单，拍下后先不要付款，我这边改价。

【失败与异常】
识别、报价、订单状态不确定时只说明未完成核验并给出必要的下一步；不猜价格、库存、座位或交易结果，不展示工具名、内部ID、供应商错误码或“模拟流程”。
"""


class ConversationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_hours: int = Field(default=24, ge=1, le=24)
    memory_depth: int = Field(default=50, ge=5, le=50)
    ai_reply_enabled: bool = True
    stage_gate_enabled: bool = False
    intervention_start: str = "consultation"
    intervention_end: str = "payment"
    human_takeover_delay_seconds: int = Field(default=20, ge=5, le=300)
    agent_persona: str = Field(default="热情、专业、简洁的电影票在线客服", min_length=1, max_length=500)
    business_background: str = Field(
        default="提供万达官方影院电影票代订服务；价格、库存、座位和出票状态必须以实时权威结果为准。",
        min_length=1, max_length=2_000,
    )
    persona_background: str = Field(default="", max_length=2_500)
    customer_service_knowledge: str = Field(
        default=DEFAULT_CUSTOMER_SERVICE_KNOWLEDGE, max_length=3_000,
    )
    reply_style: str = Field(
        default="像真人客服聊天，先回答问题，再给下一步；自然、简短、礼貌，不虚构状态。",
        min_length=1, max_length=1_000,
    )
    human_service_hours: str = Field(default="每日 09:00-24:00", min_length=1, max_length=500)
    revision: int = Field(default=0, ge=0)
    updated_at: str | None = None

    @model_validator(mode="after")
    def valid_stage_range(self) -> "ConversationPolicy":
        if self.intervention_start not in _STAGES or self.intervention_end not in _STAGES:
            raise ValueError("invalid_business_stage")
        if _STAGES.index(self.intervention_start) > _STAGES.index(self.intervention_end):
            raise ValueError("invalid_business_stage_range")
        return self

    @property
    def ttl_seconds(self) -> int:
        return self.memory_hours * 60 * 60


class ConversationPolicyStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()

    def current(self) -> ConversationPolicy:
        with self._lock:
            if not self._path.exists():
                return ConversationPolicy()
            try:
                policy = ConversationPolicy.model_validate_json(
                    self._path.read_text(encoding="utf-8"),
                )
            except (OSError, ValueError):
                return ConversationPolicy()
            # The old operator policy required a literal “正确” confirmation
            # and broadly handed uncertain cases to humans. That guidance
            # conflicts with the current state-driven W+ flow. Migrate only
            # this known legacy text; preserve other operator customizations.
            legacy_knowledge = policy.customer_service_knowledge
            if (
                "明确回复“正确”" in legacy_knowledge
                or "未收到“正确”" in legacy_knowledge
            ):
                return policy.model_copy(update={
                    "customer_service_knowledge": DEFAULT_CUSTOMER_SERVICE_KNOWLEDGE,
                })
            return policy

    def save(self, update: dict[str, Any]) -> ConversationPolicy:
        with self._lock:
            current = self.current()
            payload = current.model_dump()
            payload.update({
                key: value for key, value in update.items()
                if key in ConversationPolicy.model_fields and key not in {"revision", "updated_at"}
            })
            # ``business_background`` is the single canonical field.  Accept
            # legacy ``persona_background`` input for one migration pass, then
            # clear the legacy value so future reads cannot disagree.
            if "business_background" in update:
                payload["persona_background"] = ""
            elif "persona_background" in update and str(update.get("persona_background") or "").strip():
                payload["business_background"] = str(update["persona_background"]).strip()
                payload["persona_background"] = ""
            payload["revision"] = current.revision + 1
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            saved = ConversationPolicy.model_validate(payload)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.{uuid4().hex}.tmp")
            temporary.write_text(saved.model_dump_json(), encoding="utf-8")
            try:
                os.chmod(temporary, 0o600)
                os.replace(temporary, self._path)
            finally:
                temporary.unlink(missing_ok=True)
            return saved
