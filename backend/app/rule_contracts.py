from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


FlowState = Literal[
    "NEW", "COLLECTING", "FACTS_READY", "QUOTED", "CONFIRMED",
    "ORDER_BOUND", "PRICE_CHANGING", "WAITING_PAYMENT",
    "PAID_WAITING_FULFILLMENT", "FULFILLMENT_IN_PROGRESS", "TICKET_SENT",
    "COMPLETED", "QUOTE_EXPIRED", "ORDER_UNVERIFIED", "MANUAL_HOLD",
    "CANCELLED", "REFUND_PENDING", "REFUNDED",
]


class RuleDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_before: FlowState
    state_after: FlowState
    transition_code: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_.:-]+$")
    state_revision: int = Field(ge=0)
    actions: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    handoff_reason: str | None = Field(default=None, max_length=240)


class AiAssistResult(BaseModel):
    """Read-only AI candidates; it deliberately has no intent/authority field.

    Transaction routing is owned by the state coordinator.  Keeping a free-form
    ``intent_candidate`` here caused callers to treat model guesses as decisions,
    so the field was removed from the public contract.
    """

    model_config = ConfigDict(extra="forbid")

    visual_fact_candidates: dict[str, Any] = Field(default_factory=dict)
    extracted_field_candidates: dict[str, Any] = Field(default_factory=dict)
    knowledge_answer_candidate: str | None = Field(default=None, max_length=3_000)
    reply_candidate: str | None = Field(default=None, max_length=3_000)
    exception_diagnosis: str | None = Field(default=None, max_length=1_000)
    confidence: float = Field(default=0, ge=0, le=1)
    source_message_ids: list[str] = Field(default_factory=list, max_length=50)


AgentAction = Literal[
    # Public JSON protocol. The legacy action labels remain accepted while
    # older tenant prompts and recorded fixtures are migrated.
    "reply", "tool_call", "handoff", "finish",
    "ask", "quote", "confirm", "create_order", "change_price",
    "cancel_order", "urge_order", "ticket_status", "switch_fixed", "human",
]
WRITE_ACTIONS = frozenset({
    "create_order", "change_price", "cancel_order", "urge_order", "switch_fixed",
    "change_order_price", "submit_fulfillment", "send_ticket", "refund_or_intercept",
})
WRITE_TOOL_SUFFIXES = frozenset({
    "create", "change_price", "cancel", "urge", "switch_fixed",
    "change_order_price", "submit_fulfillment", "send_ticket", "refund_or_intercept",
})


class AgentToolCall(BaseModel):
    """A tool request proposed by the model; execution remains backend-owned."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_.:-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = Field(default="", max_length=200)


class AgentTurnPlan(BaseModel):
    """Structured JSON turn consumed by the guarded Agent loop.

    ``message``/``tool`` are the provider-neutral protocol. ``reply`` and
    ``tool_calls`` are retained only as a migration bridge for older prompts
    and OpenAI-compatible responses.
    """

    model_config = ConfigDict(extra="forbid")

    action: AgentAction = "reply"
    message: str = Field(default="", max_length=3_000)
    tool: str | None = Field(default=None, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list, max_length=20)
    reply: str = Field(default="", max_length=3_000)
    tool_calls: list[AgentToolCall] = Field(default_factory=list, max_length=8)
    required_fields: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_write_budget(self) -> "AgentTurnPlan":
        write_count = sum(
            1 for call in self.tool_calls
            if call.name.rsplit(".", 1)[-1] in WRITE_ACTIONS
            or call.name in WRITE_ACTIONS
            or call.name.rsplit(".", 1)[-1] in WRITE_TOOL_SUFFIXES
        )
        if self.action == "tool_call" and self.tool and (
            self.tool.rsplit(".", 1)[-1] in WRITE_ACTIONS
            or self.tool in WRITE_ACTIONS
            or self.tool.rsplit(".", 1)[-1] in WRITE_TOOL_SUFFIXES
        ):
            write_count += 1
        if write_count > 1:
            raise ValueError("agent_write_action_budget_exceeded")
        return self


class GateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal[
        "valid_quote_record", "official_order_readback", "official_order_event",
        "audited_fulfillment_event", "rule_state", "buyer_message",
    ]
    value: Any
    reference_id: str | None = Field(default=None, max_length=240)


class ReplyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_key: str = Field(min_length=1, max_length=160, pattern=r"^flow\.[a-z0-9_.-]+$")
    template_version: int = Field(ge=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    protected_facts: dict[str, Any] = Field(default_factory=dict)
    required_phrases: list[str] = Field(default_factory=list, max_length=20)
    optional_ai_text: str | None = Field(default=None, max_length=1_000)
    send_policy: Literal["once_per_state_revision", "replace_stale", "manual_only"]
    gate_evidence: dict[str, GateEvidence] = Field(default_factory=dict)
