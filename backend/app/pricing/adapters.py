from __future__ import annotations

from typing import Any, Mapping

from .models import PricingRulesSnapshot


def _mapping(source: object) -> Mapping[str, Any]:
    if isinstance(source, Mapping):
        nested = source.get("rules")
        return nested if isinstance(nested, Mapping) else source
    dump = getattr(source, "model_dump", None)
    if callable(dump):
        value = dump(mode="json")
        return value if isinstance(value, Mapping) else {}
    return {}


def snapshot_from_rules(source: object, *, revision: int = 0, rule_version: str = "pricing-unversioned") -> PricingRulesSnapshot:
    """Adapt an explicitly supplied rules DTO; never read storage here."""
    if isinstance(source, PricingRulesSnapshot):
        return source
    payload = _mapping(source)
    if isinstance(source, Mapping):
        revision = int(source.get("revision", revision) or revision)
        rule_version = str(source.get("rule_version", rule_version) or rule_version)
        explicit = bool(source.get("liangpiao_fixed_rules_explicit", "liangpiao_fixed_rules" in source))
    else:
        if getattr(source, "revision", None) is not None:
            revision = int(source.revision)
        if getattr(source, "rule_version", None) is not None:
            rule_version = str(source.rule_version or rule_version)
        explicit = "liangpiao_fixed_rules" in getattr(source, "model_fields_set", set())
    return PricingRulesSnapshot.from_mapping({**dict(payload), "revision": revision, "rule_version": rule_version, "liangpiao_fixed_rules_explicit": explicit})


class ProbeCostFacts:
    """Neutral DTO for the minimum Probe result consumed by PricingFacts."""

    def __init__(self, *, probe_result_id: str, seat_id: str, original_price_cents: int, member_cost_cents: int, release_verified: bool) -> None:
        self.probe_result_id = probe_result_id
        self.seat_id = seat_id
        self.original_price_cents = original_price_cents
        self.member_cost_cents = member_cost_cents
        self.release_verified = release_verified
