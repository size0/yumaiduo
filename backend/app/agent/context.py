from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .observations import Observation


@dataclass(frozen=True, slots=True)
class AgentContext:
    identity: Mapping[str, str]
    current_event: Mapping[str, Any]
    recent_messages: tuple[Mapping[str, Any], ...] = ()
    observations: tuple[Observation, ...] = ()
    business_state: Mapping[str, Any] = field(default_factory=dict)
    available_tools: tuple[Mapping[str, Any], ...] = ()
    trace_id: str = ""
    conversation_revision: int = 0
    latest_buyer_message_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": dict(self.identity),
            "conversation": {"recent_messages": [dict(item) for item in self.recent_messages]},
            "current_event": dict(self.current_event),
            "observations": [item.as_dict() for item in self.observations],
            "business_state": dict(self.business_state),
            "available_tools": [dict(item) for item in self.available_tools],
            "trace_id": self.trace_id,
            "conversation_revision": self.conversation_revision,
            "latest_buyer_message_id": self.latest_buyer_message_id,
        }

    def with_observation(self, observation: Observation) -> "AgentContext":
        return AgentContext(
            identity=self.identity,
            current_event=self.current_event,
            recent_messages=self.recent_messages,
            observations=(*self.observations, observation),
            business_state=self.business_state,
            available_tools=self.available_tools,
            trace_id=self.trace_id,
            conversation_revision=self.conversation_revision,
            latest_buyer_message_id=self.latest_buyer_message_id,
        )
