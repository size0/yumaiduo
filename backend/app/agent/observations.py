from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Observation:
    """Provider-neutral, bounded information returned to the Agent."""

    ok: bool
    code: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    candidates: tuple[Mapping[str, Any], ...] = ()
    missing_fields: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        if not self.code or len(self.code) > 120:
            raise ValueError("observation_code_invalid")
        if len(self.candidates) > 100 or len(self.missing_fields) > 50 or len(self.conflicts) > 50:
            raise ValueError("observation_size_invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "facts": dict(self.facts),
            "candidates": [dict(item) for item in self.candidates],
            "missing_fields": list(self.missing_fields),
            "conflicts": list(self.conflicts),
            "message": self.message,
        }

    @classmethod
    def success(
        cls,
        code: str,
        *,
        facts: Mapping[str, Any] | None = None,
        candidates: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None = None,
        message: str = "",
    ) -> "Observation":
        return cls(
            ok=True,
            code=code,
            facts=facts or {},
            candidates=tuple(candidates or ()),
            message=message,
        )

    @classmethod
    def warning(
        cls,
        code: str,
        *,
        facts: Mapping[str, Any] | None = None,
        missing_fields: list[str] | tuple[str, ...] | None = None,
        conflicts: list[str] | tuple[str, ...] | None = None,
        message: str = "",
    ) -> "Observation":
        return cls(
            ok=False,
            code=code,
            facts=facts or {},
            missing_fields=tuple(missing_fields or ()),
            conflicts=tuple(conflicts or ()),
            message=message,
        )
