from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .settings_store import SecretProtector, default_secret_protector

_ALLOWED_TEMPLATE_VARIABLES = {"movie", "showtime", "cinema", "date", "hall", "seats"}
_TEMPLATE_VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ReminderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    pre_show_minutes: int = Field(default=5, ge=1, le=180)
    post_show_minutes: int = Field(default=5, ge=0, le=180)
    pre_show_template: str = Field(
        default="观影提醒：{movie} 将于 {showtime} 放映，记得提前到影院取票、检票入场，祝观影愉快～",
        min_length=1,
        max_length=1_000,
    )
    revision: int = Field(default=0, ge=0)
    updated_at: str | None = None

    @field_validator("pre_show_template")
    @classmethod
    def valid_template(cls, value: str) -> str:
        unsupported = set(_TEMPLATE_VARIABLE.findall(value)) - _ALLOWED_TEMPLATE_VARIABLES
        if unsupported:
            raise ValueError("unsupported_reminder_template_variable")
        return value


class ReminderStore:
    """Encrypted settings and idempotent viewing-reminder tasks."""

    def __init__(self, path: Path, *, protector: SecretProtector | None = None, max_tasks: int = 10_000) -> None:
        self._path = path
        self._protector = protector or default_secret_protector()
        self._max_tasks = max_tasks
        self._lock = RLock()

    def settings(self) -> ReminderSettings:
        with self._lock:
            state = self._read()
            try:
                return ReminderSettings.model_validate(state.get("settings") or {})
            except ValueError:
                return ReminderSettings()

    def save_settings(self, update: Mapping[str, Any]) -> ReminderSettings:
        with self._lock:
            state = self._read()
            current = self.settings()
            values = current.model_dump()
            values.update({key: value for key, value in update.items() if key in ReminderSettings.model_fields})
            values["revision"] = current.revision + 1
            values["updated_at"] = datetime.now(timezone.utc).isoformat()
            saved = ReminderSettings.model_validate(values)
            state["settings"] = saved.model_dump()
            self._write(state)
            return saved

    def plan_order(
        self, facts: Mapping[str, Any], *, now: datetime | None = None,
        pre_show_template: str | None = None,
    ) -> list[dict[str, Any]]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            state = self._read()
            settings = ReminderSettings.model_validate(state.get("settings") or {})
            if not settings.enabled:
                return []
            normalized = self._facts(facts)
            show_start = self._show_datetime(normalized["quote_date"], normalized["showtime_start"])
            if show_start is None or current >= show_start:
                return []
            tasks = [item for item in state.get("tasks", []) if isinstance(item, dict)]
            planned: list[dict[str, Any]] = []
            pre_due = show_start - timedelta(minutes=settings.pre_show_minutes)
            planned.append(self._upsert_task(
                tasks, normalized, kind="pre_show_text", due_at=pre_due,
                expires_at=show_start,
                message=self._render(pre_show_template or settings.pre_show_template, normalized),
            ))
            show_end = self._show_datetime(normalized["quote_date"], normalized.get("showtime_end"))
            if show_end is not None:
                if show_end <= show_start:
                    show_end += timedelta(days=1)
                planned.append(self._upsert_task(
                    tasks, normalized, kind="post_show_receipt",
                    due_at=show_end + timedelta(minutes=settings.post_show_minutes),
                    expires_at=show_end + timedelta(days=7), message=None,
                ))
            state["tasks"] = sorted(tasks, key=lambda item: str(item.get("due_at") or ""))[-self._max_tasks :]
            self._write(state)
            return [dict(item) for item in planned]

    def list(self, tenant_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            return []
        with self._lock:
            tasks = [dict(item) for item in self._read().get("tasks", []) if item.get("tenant_id") == tenant]
        tasks.sort(key=lambda item: str(item.get("due_at") or ""), reverse=True)
        return tasks[: max(1, min(int(limit), 500))]

    def claim_due(self, *, now: datetime | None = None, limit: int = 10, lease_seconds: int = 90) -> list[dict[str, Any]]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        claimed: list[dict[str, Any]] = []
        with self._lock:
            state = self._read()
            tasks = [item for item in state.get("tasks", []) if isinstance(item, dict)]
            for task in tasks:
                expires = self._datetime(task.get("expires_at"))
                if task.get("status") in {"pending", "claimed"} and expires is not None and current >= expires:
                    task.update({"status": "missed", "completed_at": current.isoformat(), "lease_token": None, "lease_until": None})
                    continue
                lease_until = self._datetime(task.get("lease_until"))
                available = task.get("status") == "pending" or (task.get("status") == "claimed" and lease_until and current >= lease_until)
                due = self._datetime(task.get("next_attempt_at") or task.get("due_at"))
                if not available or due is None or due > current:
                    continue
                token = uuid4().hex
                task.update({
                    "status": "claimed", "lease_token": token,
                    "lease_until": (current + timedelta(seconds=lease_seconds)).isoformat(),
                    "attempts": int(task.get("attempts") or 0) + 1,
                })
                claimed.append(dict(task))
                if len(claimed) >= max(1, min(int(limit), 50)):
                    break
            state["tasks"] = tasks
            self._write(state)
        return claimed

    def complete(self, task_id: str, lease_token: str, result: Mapping[str, Any] | None = None) -> bool:
        return self._finish(task_id, lease_token, status="completed", result=result)

    def fail(self, task_id: str, lease_token: str, reason: str, *, now: datetime | None = None) -> bool:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            state = self._read()
            task = next((item for item in state.get("tasks", []) if item.get("task_id") == task_id), None)
            if task is None or task.get("lease_token") != lease_token or task.get("status") != "claimed":
                return False
            attempts = int(task.get("attempts") or 0)
            if attempts >= 5:
                task.update({"status": "failed", "completed_at": current.isoformat()})
            else:
                task.update({"status": "pending", "next_attempt_at": (current + timedelta(seconds=min(300, 15 * 2 ** (attempts - 1)))).isoformat()})
            task.update({"last_error": str(reason or "reminder_failed")[:160], "lease_token": None, "lease_until": None})
            self._write(state)
            return True

    def _finish(self, task_id: str, lease_token: str, *, status: str, result: Mapping[str, Any] | None) -> bool:
        with self._lock:
            state = self._read()
            task = next((item for item in state.get("tasks", []) if item.get("task_id") == task_id), None)
            if task is None:
                return False
            if task.get("status") == status:
                return True
            if task.get("status") != "claimed" or task.get("lease_token") != lease_token:
                return False
            task.update({
                "status": status, "completed_at": datetime.now(timezone.utc).isoformat(),
                "lease_token": None, "lease_until": None,
                "result": dict(result or {}),
            })
            self._write(state)
            return True

    @staticmethod
    def _facts(value: Mapping[str, Any]) -> dict[str, str | None]:
        required = ("tenant_id", "shop_id", "buyer_id", "chat_id", "order_id", "movie", "cinema", "quote_date", "showtime_start")
        facts = {key: str(value.get(key) or "").strip() or None for key in (
            *required, "showtime_end", "hall", "seat_display",
        )}
        if any(not facts[key] for key in required):
            raise ValueError("reminder_authoritative_facts_incomplete")
        return facts

    @staticmethod
    def _show_datetime(day: str | None, clock: str | None) -> datetime | None:
        if not day or not clock:
            return None
        try:
            return datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M").replace(tzinfo=_SHANGHAI).astimezone(timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _render(template: str, facts: Mapping[str, str | None]) -> str:
        values = {
            "movie": facts.get("movie"), "showtime": facts.get("showtime_start"),
            "cinema": facts.get("cinema"), "date": facts.get("quote_date"),
            "hall": facts.get("hall"), "seats": facts.get("seat_display"),
        }
        rendered = template
        for name in _ALLOWED_TEMPLATE_VARIABLES:
            rendered = rendered.replace("{" + name + "}", str(values.get(name) or ""))
        return rendered.strip()

    @staticmethod
    def _upsert_task(
        tasks: list[dict[str, Any]], facts: Mapping[str, str | None], *, kind: str,
        due_at: datetime, expires_at: datetime, message: str | None,
    ) -> dict[str, Any]:
        material = f'{facts["tenant_id"]}:{facts["order_id"]}:{kind}:{facts["quote_date"]}:{facts["showtime_start"]}'
        task_id = "rem-" + hashlib.sha256(material.encode()).hexdigest()[:32]
        existing = next((item for item in tasks if item.get("task_id") == task_id), None)
        if existing is not None:
            return existing
        task: dict[str, Any] = {
            "task_id": task_id, "kind": kind, "status": "pending", "attempts": 0,
            **facts, "due_at": due_at.astimezone(timezone.utc).isoformat(),
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
            "idempotency_key": f"wanda-reminder:{task_id}", "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if message:
            task["message"] = message
        tasks.append(task)
        return task

    @staticmethod
    def _datetime(value: object) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            protected = payload.get("state_protected") if isinstance(payload, dict) else None
            state = json.loads(self._protector.unprotect(protected)) if isinstance(protected, str) and protected else {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state.setdefault("settings", {})
        state.setdefault("tasks", [])
        return state

    def _write(self, state: Mapping[str, Any]) -> None:
        protected = self._protector.protect(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
        payload = {"version": 1, "state_protected": protected}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)
