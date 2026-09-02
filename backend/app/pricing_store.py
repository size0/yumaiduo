from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from .models import PricingRulesUpdate, PricingRulesView


class PricingRulesStore:
    """Atomic, versioned persistence for bounded integer-cent pricing rules."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()

    def current(self) -> PricingRulesUpdate:
        with self._lock:
            payload = self._read_payload()
            source = payload.get("rules") if isinstance(payload.get("rules"), dict) else payload
            if isinstance(source, dict) and "regular_adjustment_cents" not in source:
                discount = source.get("wplus_original_discount_cents", 290)
                source = {
                    "enabled": bool(source.get("enabled", False)),
                    "wplus_friday_member_day_enabled": bool(source.get("wplus_friday_member_day_enabled", True)),
                    "regular_adjustment_cents": source.get(
                        "regular_markup_cents", source.get("fixed_markup_cents", 100),
                    ),
                    "wplus_member_price_threshold_cents": source.get(
                        "wplus_member_threshold_cents", 6_000,
                    ),
                    "wplus_adjustment_cents": source.get(
                        "wplus_adjustment_cents", -abs(int(discount)),
                    ),
                    "rounding_increment_cents": source.get("rounding_increment_cents", 10),
                    "liangpiao_price_mode": source.get("liangpiao_price_mode", "FIXED"),
                    "liangpiao_fixed_rules": source.get("liangpiao_fixed_rules", source.get("liangpiao_rules", [])),
                }
            elif isinstance(source, dict) and "liangpiao_fixed_rules" not in source:
                # Migrate legacy policies without silently changing the
                # existing price behaviour.  A later explicit [] remains a
                # deliberate fixed-channel rule reset.
                source = {**source, "liangpiao_fixed_rules": source.get("liangpiao_rules", [])}
            try:
                return self._normalize(PricingRulesUpdate.model_validate(source))
            except (TypeError, ValueError):
                return PricingRulesUpdate()

    def view(self) -> PricingRulesView:
        with self._lock:
            payload = self._read_payload()
            rules = self.current()
            revision = payload.get("revision", 0)
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                revision = 0
            return PricingRulesView(
                **rules.model_dump(),
                revision=revision,
                rule_version=self.rule_version(rules, revision),
                updated_at=payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else None,
                calculation_summary=(
                    "良票按预估价格/原价的折扣率匹配动态加价区间；"
                    "万达按官方优惠价/原价的折扣率匹配固定调整区间；"
                    "万达普通座/W+座按优惠价/原价折扣率匹配万达固定调整区间；"
                    "VIP厅按运营配置的VIP专属规则；所有结果按设置取整，且不低于优惠价、不高于官方原价。"
                    if rules.wanda_rules or rules.liangpiao_rules else
                    "周五会员日开关开启时优先使用官方W+周五活动价；然后叠加报价规则。"
                    "普通座按实时会员价加普通区调整；W+会员价不高于阈值时取"
                    "max(实时原价加W+调整,会员价)，高于阈值时直接取会员价；"
                    "万达普通座/W+座按优惠价/原价折扣率匹配固定调整；VIP厅按运营配置的VIP专属规则；"
                    "逐座按设置轮整，且不低于会员成本、不高于实时原价。"
                ),
            )

    def save(self, update: PricingRulesUpdate) -> PricingRulesView:
        with self._lock:
            payload = self._read_payload()
            previous_revision = payload.get("revision", 0)
            revision = previous_revision + 1 if isinstance(previous_revision, int) and not isinstance(previous_revision, bool) else 1
            normalized = self._normalize(update)
            # Older panels do not know about dynamic bands and omit these
            # fields. Preserve the active bands instead of letting a stale
            # client silently downgrade the live pricing policy. An explicit
            # empty list remains available as a deliberate reset.
            previous = self.current()
            omitted_bands = {
                field: getattr(previous, field)
                for field in ("liangpiao_rules", "liangpiao_fixed_rules", "wanda_rules", "liangpiao_price_mode")
                if field not in update.model_fields_set
            }
            if omitted_bands:
                normalized = normalized.model_copy(update=omitted_bands)
            saved = {
                "version": 1,
                "revision": revision,
                "rules": normalized.model_dump(mode="json"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_payload(saved)
            return self.view()

    @staticmethod
    def _normalize(rules: PricingRulesUpdate) -> PricingRulesUpdate:
        # The operations UI defines this field as “W+原价减免”. Treat a
        # positive persisted value as a discount magnitude rather than an
        # accidental markup that would be capped back to the full original price.
        adjustment = int(rules.wplus_adjustment_cents)
        if adjustment > 0:
            return rules.model_copy(update={"wplus_adjustment_cents": -adjustment})
        return rules

    @staticmethod
    def rule_version(rules: PricingRulesUpdate, revision: int = 0) -> str:
        digest = hashlib.sha256(
            json.dumps(rules.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]
        return f"pricing-r{revision}-{digest}"

    def _read_payload(self) -> dict[str, object]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_payload(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self._path)
