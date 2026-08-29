from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


_STAGES = ("consultation", "order_pending", "payment", "shipping_refund")


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
    customer_service_knowledge: str = Field(default="", max_length=3_000)
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
                return ConversationPolicy.model_validate_json(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return ConversationPolicy()

    def save(self, update: dict[str, Any]) -> ConversationPolicy:
        with self._lock:
            current = self.current()
            payload = current.model_dump()
            payload.update({
                key: value for key, value in update.items()
                if key in ConversationPolicy.model_fields and key not in {"revision", "updated_at"}
            })
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
