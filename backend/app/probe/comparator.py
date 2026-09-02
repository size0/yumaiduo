from __future__ import annotations

from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from .models import ProbeResult


class ShadowClassification(StrEnum):
    MATCH = "MATCH"
    ACCEPTABLE_DIFFERENCE = "ACCEPTABLE_DIFFERENCE"
    MISMATCH = "MISMATCH"


class ProbeFieldDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=200)
    expected: Any = None
    actual: Any = None
    allowed: bool = False


class ShadowComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: ShadowClassification
    diffs: list[ProbeFieldDiff] = Field(default_factory=list, max_length=100)


class ProbeShadowComparator:
    """Compare only Probe facts; pricing and implementation identities are excluded."""

    _FIELDS = (
        "status", "error_code", "seat_type_prices", "cancel_confirmed",
        "release_verified", "release_timing_class",
    )

    def compare(
        self,
        expected: ProbeResult,
        actual: ProbeResult,
        *,
        acceptable_fields: Iterable[str] = (),
    ) -> ShadowComparison:
        expected = ProbeResult.model_validate(expected)
        actual = ProbeResult.model_validate(actual)
        allowed = set(acceptable_fields)
        diffs: list[ProbeFieldDiff] = []
        for field in self._FIELDS:
            left = _normalize(field, getattr(expected, field))
            right = _normalize(field, getattr(actual, field))
            if left != right:
                diffs.append(ProbeFieldDiff(field=field, expected=left, actual=right, allowed=field in allowed))
        if not diffs:
            classification = ShadowClassification.MATCH
        elif all(item.allowed for item in diffs):
            classification = ShadowClassification.ACCEPTABLE_DIFFERENCE
        else:
            classification = ShadowClassification.MISMATCH
        return ShadowComparison(classification=classification, diffs=diffs)


def _normalize(field: str, value: object) -> object:
    if field == "seat_type_prices":
        return sorted(
            [item.model_dump(mode="json") for item in value],
            key=lambda item: (item["area_code"], item["zone_type"], item["representative_seat_id"]),
        )
    return value.value if isinstance(value, StrEnum) else value
