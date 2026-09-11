from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from collections.abc import Mapping
from typing import Any


_CHINESE_NUMBERS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_REFERENCE_MARKERS = ("那场", "这场", "刚才那场", "这个场次", "这个影院", "刚才那个影院", "还是刚才那个影院", "这个电影", "这部电影", "刚才截图那个", "就这个")


@dataclass(frozen=True)
class ConversationFactPatch:
    """A deterministic, non-authoritative update to conversation facts."""

    facts: dict[str, Any]
    referenced: bool = False
    requires_requote: bool = False
    reason: str | None = None

    @property
    def recognized(self) -> bool:
        return bool(self.facts or self.referenced)

    @property
    def is_reference(self) -> bool:
        return self.referenced

    def to_dict(self) -> dict[str, Any]:
        return {
            "facts": dict(self.facts),
            "referenced": self.referenced,
            "requires_requote": self.requires_requote,
            "reason": self.reason,
            "recognized": self.recognized,
        }


class ConversationFactPatchParser:
    """Parse only explicit buyer fact patches and references.

    This parser never reads prices, inventory, seat availability, or counts from
    a screenshot.  Quantity is accepted only when the buyer explicitly says it
    (for example, ``两张``); selected seat labels remain separate facts.
    """

    def parse(
        self, text: str, facts: Mapping[str, Any] | None = None,
    ) -> ConversationFactPatch:
        normalized = unicodedata.normalize("NFKC", str(text or "")).strip()
        if not normalized:
            return ConversationFactPatch({}, reason="empty_text")
        compact = re.sub(r"[\s，。！？!?、,；;]+", "", normalized)
        current = facts if isinstance(facts, Mapping) else {}
        patch: dict[str, Any] = {}
        referenced = any(marker in compact for marker in _REFERENCE_MARKERS)

        time_value = self._time_patch(normalized)
        if time_value is not None:
            patch["showtime_start"] = time_value

        count = self._count_patch(compact)
        if count is not None:
            patch["ticket_count"] = count

        if any(marker in compact for marker in ("这个影院", "刚才那个影院", "还是刚才那个影院")):
            cinema = self._value(current, "cinema", "cinema_name")
            if cinema:
                patch["cinema"] = cinema
        if any(marker in compact for marker in ("这个电影", "这部电影", "刚才截图那个")):
            movie = self._value(current, "movie", "movie_name")
            if movie:
                patch["movie"] = movie
        if compact in {"第二场", "第2场", "第三场", "第3场", "第一场", "第1场"}:
            ordinal = int(re.search(r"[一二三123]", compact).group(0).translate(str.maketrans("一二三", "123")))
            patch["showtime_ordinal"] = ordinal
            referenced = True

        # “那场/这场/刚才那场” is a request to revalidate the current
        # structured context, not permission to reuse its old quote.
        return ConversationFactPatch(
            facts=patch,
            referenced=referenced,
            requires_requote=bool(patch or referenced),
            reason="explicit_fact_patch" if patch else ("reference" if referenced else None),
        )

    @staticmethod
    def _value(source: Mapping[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = str(source.get(key) or "").strip()
            if value:
                return value
        return None

    @staticmethod
    def _time_patch(value: str) -> str | None:
        match = re.search(r"(?<!\d)([01]?\d|2[0-3])\s*(?::|：|点|时)\s*([0-5]?\d)\s*(?:分)?(?!\d)", value)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def _count_patch(value: str) -> int | None:
        match = re.search(r"(?<!\d)([0-9]{1,2})\s*(?:张|张票|票)(?!\d)", value)
        if match:
            count = int(match.group(1))
            return count if 1 <= count <= 20 else None
        for word, count in _CHINESE_NUMBERS.items():
            if re.search(rf"{re.escape(word)}\s*(?:张|张票|票)", value):
                return count
        return None


def merge_conversation_facts(
    base: Mapping[str, Any] | None, patch: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(base) if isinstance(base, Mapping) else {}
    for key, value in (patch.items() if isinstance(patch, Mapping) else ()):
        if value is not None and value != "":
            merged[str(key)] = value
    return merged
