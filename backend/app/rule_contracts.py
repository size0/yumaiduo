from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


FlowState = Literal[
    "NEW", "COLLECTING", "FACTS_READY", "QUOTED", "CONFIRMED",
    "ORDER_BOUND", "PRICE_CHANGING", "WAITING_PAYMENT",
    "PAID_WAITING_FULFILLMENT", "WAITING_WPLUS_MARK", "READY_FOR_MANUAL_TICKETING",
    "FULFILLMENT_IN_PROGRESS", "TICKET_SENT",
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
