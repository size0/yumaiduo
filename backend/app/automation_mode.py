from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


AutomationMode = Literal["rules", "hybrid", "agent", "full"]
DEFAULT_AUTOMATION_MODE: AutomationMode = "hybrid"


class AutomationModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AutomationMode


class AutomationModeView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AutomationMode = DEFAULT_AUTOMATION_MODE
    scope: Literal["shop", "conversation"]
    revision: int = Field(ge=0)
    updated_at: str


def normalize_automation_mode(value: object) -> AutomationMode:
    mode = str(value or "").strip().lower()
    if mode not in {"rules", "hybrid", "agent", "full"}:
        raise ValueError("automation_mode_invalid")
    return mode  # type: ignore[return-value]
