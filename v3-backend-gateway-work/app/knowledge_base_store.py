from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

Category = Literal["vision", "quote", "reply"]
CATEGORIES = {"vision", "quote", "reply"}
EXPERIENCE_TOPICS = {"问候与结束语", "图片要求", "服务范围", "服务流程", "沟通方式"}
EXPERIENCE_OUTCOMES = {"buyer_acknowledged", "buyer_progressed"}
AGENT_SCENES = {"general", "intake", "quote_followup", "order", "fulfillment", "aftersale"}
UNSAFE_EXPERIENCE = re.compile(
    r"(?:[0-9０-９]|[零一二三四五六七八九十百千万]{2,}|元|块钱|价格|优惠|折扣|会员价|"
    r"订单|付款|支付|改价|出票|发货|退款|库存|余票|可售|微信|手机号|电话|https?://|www\.|@)",
    re.IGNORECASE,
)


class KnowledgeBaseStore:
    """Tenant-aware reviewed rules. Conversation experience always starts disabled and draft."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            self._write({"entries": []})

    def list(self, tenant_id: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            entries = self._read()["entries"]
            visible = [entry for entry in entries if self._visible_to_tenant(entry, tenant_id)]
            return sorted(visible, key=lambda x: (x["category"], str(x.get("scene", "general")), x["sort_order"], x["created_at"]))

    def active(self, category: str, tenant_id: str | None = None) -> list[str]:
        return [
            str(entry["content"])
            for entry in self.list(tenant_id)
            if entry["category"] == category and entry["enabled"] and entry["status"] == "approved"
        ]

    def active_for_agent(self, scene: str, tenant_id: str, *, max_rules: int = 8, max_chars: int = 4000) -> list[str]:
        """Return only reviewed reply knowledge for the current conversation scene.

        Scene-specific rules are ordered before general rules. The hard count and
        character limits keep unrelated knowledge out of the planner context.
        Transaction facts still come exclusively from authoritative tools.
        """
        selected_scene = scene if scene in AGENT_SCENES else "general"
        count_limit = max(1, min(int(max_rules), 20))
        char_limit = max(200, min(int(max_chars), 8000))
        entries = [
            entry for entry in self.list(tenant_id)
            if entry["category"] == "reply"
            and entry["enabled"]
            and entry["status"] == "approved"
            and str(entry.get("scene", "general")) in {"general", selected_scene}
        ]
        entries.sort(key=lambda item: (
            0 if str(item.get("scene", "general")) == selected_scene and selected_scene != "general" else 1,
            int(item.get("sort_order", 0)), str(item.get("created_at", "")),
        ))
        result: list[str] = []
        used = 0
        for entry in entries:
            content = str(entry["content"]).strip()
            if not content or used + len(content) > char_limit:
                continue
            result.append(content)
            used += len(content)
            if len(result) >= count_limit:
                break
        return result

    def create(self, payload: dict[str, object], tenant_id: str | None = None) -> dict[str, object]:
        with self._lock:
            now = self._now()
            entry = self._validate(payload)
            entry.update({
                "id": uuid4().hex,
                "status": "draft",
                "enabled": False,
                "created_at": now,
                "updated_at": now,
                "versions": [],
                **({"tenant_id": self._tenant(tenant_id)} if tenant_id else {}),
            })
            state = self._read()
            state["entries"].append(entry)
            self._write(state)
            return entry

    def update(self, entry_id: str, payload: dict[str, object], tenant_id: str | None = None) -> dict[str, object]:
        with self._lock:
            state = self._read()
            for entry in state["entries"]:
                if entry["id"] != entry_id or not self._visible_to_tenant(entry, tenant_id):
                    continue
                old = {key: entry.get(key, "general" if key == "scene" else None) for key in ("title", "content", "category", "scene", "enabled", "status", "sort_order")}
                entry.update(self._validate(payload, existing=entry))
                if "status" in payload:
                    entry["status"] = self._status(payload["status"])
                if "enabled" in payload:
                    if not isinstance(payload["enabled"], bool):
                        raise ValueError("enabled must be boolean")
                    entry["enabled"] = payload["enabled"]
                entry["versions"].append({"at": self._now(), "value": old})
                entry["versions"] = entry["versions"][-20:]
                entry["updated_at"] = self._now()
                self._write(state)
                return entry
            raise KeyError(entry_id)

    def record_experience(self, tenant_id: str, candidate: dict[str, object]) -> dict[str, object]:
        tenant = self._tenant(tenant_id)
        safe = self._validate_experience(candidate)
        fingerprint_source = "\n".join((tenant, safe["topic"], safe["question_pattern"], safe["response_guidance"], safe["example_reply"]))
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        now = self._now()
        with self._lock:
            state = self._read()
            for entry in state["entries"]:
                if entry.get("tenant_id") == tenant and entry.get("experience_fingerprint") == fingerprint:
                    entry["evidence_count"] = min(int(entry.get("evidence_count", 1)) + 1, 10_000)
                    entry["last_observed_at"] = now
                    entry["updated_at"] = now
                    self._write(state)
                    return entry
            entry: dict[str, object] = {
                "id": uuid4().hex,
                "tenant_id": tenant,
                "category": "reply",
                "scene": "general",
                "title": f"会话经验：{safe['topic']}",
                "content": f"适用问题：{safe['question_pattern']}\n回复策略：{safe['response_guidance']}\n参考表达：{safe['example_reply']}",
                "sort_order": 0,
                "status": "draft",
                "enabled": False,
                "source": "conversation_experience",
                "outcome_signal": safe["outcome_signal"],
                "confidence": safe["confidence"],
                "evidence_count": 1,
                "experience_fingerprint": fingerprint,
                "created_at": now,
                "updated_at": now,
                "last_observed_at": now,
                "versions": [],
            }
            state["entries"].append(entry)
            self._write(state)
            return entry

    def _validate(self, payload: dict[str, object], existing: dict[str, object] | None = None) -> dict[str, object]:
        category = str(payload.get("category", existing.get("category") if existing else "")).strip()
        scene = str(payload.get("scene", existing.get("scene", "general") if existing else "general")).strip()
        title = str(payload.get("title", existing.get("title") if existing else "")).strip()
        content = str(payload.get("content", existing.get("content") if existing else "")).strip()
        order = payload.get("sort_order", existing.get("sort_order", 0) if existing else 0)
        if category not in CATEGORIES or scene not in AGENT_SCENES or not title or len(title) > 100 or not content or len(content) > 2000 or isinstance(order, bool) or not isinstance(order, int):
            raise ValueError("invalid knowledge entry")
        return {"category": category, "scene": scene, "title": title, "content": content, "sort_order": order}

    @staticmethod
    def _validate_experience(candidate: dict[str, object]) -> dict[str, object]:
        if not isinstance(candidate, dict):
            raise ValueError("invalid conversation experience")
        topic = str(candidate.get("topic", "")).strip()
        question = str(candidate.get("question_pattern", "")).strip()
        guidance = str(candidate.get("response_guidance", "")).strip()
        example = str(candidate.get("example_reply", "")).strip()
        outcome = str(candidate.get("outcome_signal", "")).strip()
        confidence = candidate.get("confidence")
        if (
            topic not in EXPERIENCE_TOPICS
            or not 5 <= len(question) <= 120
            or not 5 <= len(guidance) <= 300
            or not 1 <= len(example) <= 300
            or outcome not in EXPERIENCE_OUTCOMES
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.85 <= float(confidence) <= 1
            or UNSAFE_EXPERIENCE.search(" ".join((question, guidance, example)))
        ):
            raise ValueError("unsafe conversation experience")
        return {
            "topic": topic,
            "question_pattern": question,
            "response_guidance": guidance,
            "example_reply": example,
            "outcome_signal": outcome,
            "confidence": float(confidence),
        }

    @staticmethod
    def _status(value: object) -> str:
        if value not in {"draft", "approved"}:
            raise ValueError("invalid status")
        return str(value)

    @staticmethod
    def _tenant(value: str | None) -> str:
        tenant = str(value or "").strip()
        if not tenant or len(tenant) > 128:
            raise ValueError("invalid tenant")
        return tenant

    @staticmethod
    def _visible_to_tenant(entry: dict[str, object], tenant_id: str | None) -> bool:
        entry_tenant = str(entry.get("tenant_id", "")).strip()
        if tenant_id is None:
            return True
        return not entry_tenant or entry_tenant == str(tenant_id).strip()

    def _read(self) -> dict[str, list[dict[str, object]]]:
        return json.loads(self.path.read_text("utf-8"))

    def _write(self, value: object) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
