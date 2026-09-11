from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


class CanonicalAgentAuditStore:
    """Small additive audit projection backed by the existing RulesFirst DB.

    This adapter deliberately does not expose or mutate transaction/order
    state. Payloads use the RulesFirst store's existing protected serializer.
    """

    def __init__(self, base_store: Any) -> None:
        self._base = base_store
        with self._base._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL,
                    buyer_id TEXT NOT NULL, chat_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    status TEXT NOT NULL, reply_origin TEXT, failure_reason TEXT,
                    model_config_id TEXT, model_config_revision INTEGER, model_provider TEXT,
                    model_base_url_host TEXT, model_name TEXT, context_protected TEXT,
                    command_id TEXT, sent_message_id TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, UNIQUE(tenant_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS agent_runs_session_idx
                    ON agent_runs(tenant_id, shop_id, buyer_id, chat_id, created_at);
                CREATE TABLE IF NOT EXISTS agent_tool_calls (
                    tool_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL, tool_name TEXT NOT NULL,
                    arguments_protected TEXT, result_protected TEXT, status TEXT NOT NULL,
                    error_reason TEXT, created_at TEXT NOT NULL, UNIQUE(run_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS agent_tool_calls_run_idx
                    ON agent_tool_calls(run_id, sequence);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _text(value: object) -> str | None:
        text = str(value or "").strip()
        return text or None

    @classmethod
    def _safe(cls, value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "[truncated]"
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key).lower()
                if any(marker in name for marker in (
                    "api_key", "apikey", "secret", "password", "authorization",
                    "token", "image_bytes", "raw_image", "raw_provider_result",
                )):
                    continue
                result[str(key)] = cls._safe(item, depth + 1)
            return result
        if isinstance(value, list):
            return [cls._safe(item, depth + 1) for item in value[:100]]
        if isinstance(value, str):
            return value[:4_000]
        return value

    def create_agent_run(self, **kwargs: Any) -> dict[str, Any]:
        identity = tuple(str(kwargs.get(key) or "").strip() for key in (
            "tenant_id", "shop_id", "buyer_id", "chat_id", "event_id",
        ))
        if any(not value for value in identity):
            raise ValueError("agent_run_identity_invalid")
        run_id = str(kwargs.get("run_id") or "agent-run-" + hashlib.sha256(
            "\0".join(identity).encode()
        ).hexdigest()[:40])
        timestamp = self._now()
        context = kwargs.get("context")
        with self._base._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO agent_runs(
                    run_id,tenant_id,shop_id,buyer_id,chat_id,event_id,status,reply_origin,
                    model_config_id,model_config_revision,model_provider,model_base_url_host,
                    model_name,context_protected,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, *identity, str(kwargs.get("status") or "running"), self._text(kwargs.get("reply_origin")), 
                 self._text(kwargs.get("model_config_id")), kwargs.get("model_config_revision"),
                 self._text(kwargs.get("model_provider")), self._text(kwargs.get("model_base_url_host")),
                 self._text(kwargs.get("model_name")),
                 self._base._protect(self._safe(context)) if context is not None else None,
                 timestamp, timestamp),
            )
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise RuntimeError("agent_run_not_persisted")
        return self._view(row)

    def update_agent_run(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        for field in ("status", "reply_origin", "failure_reason", "command_id", "sent_message_id"):
            if field in kwargs and kwargs[field] is not None:
                updates[field] = self._text(kwargs[field])
        if kwargs.get("context") is not None:
            updates["context_protected"] = self._base._protect(self._safe(kwargs["context"]))
        updates["updated_at"] = self._now()
        assignments = ",".join(f"{field}=?" for field in updates)
        with self._base._connect() as connection:
            connection.execute(f"UPDATE agent_runs SET {assignments} WHERE run_id=?", (*updates.values(), run_id))
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("agent_run_missing")
        return self._view(row)

    def append_agent_tool_call(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        with self._base._connect() as connection:
            next_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM agent_tool_calls WHERE run_id=?", (run_id,)
            ).fetchone()
            sequence = int(kwargs.get("sequence", next_row[0]))
            connection.execute(
                """INSERT OR IGNORE INTO agent_tool_calls(
                    run_id,sequence,tool_name,arguments_protected,result_protected,status,error_reason,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, sequence, str(kwargs.get("tool_name") or "unknown"),
                 self._base._protect(self._safe(kwargs.get("arguments"))) if kwargs.get("arguments") is not None else None,
                 self._base._protect(self._safe(kwargs.get("result"))) if kwargs.get("result") is not None else None,
                 str(kwargs.get("status") or "succeeded"), self._text(kwargs.get("error_reason")), self._now()),
            )
            row = connection.execute(
                "SELECT * FROM agent_tool_calls WHERE run_id=? AND sequence=?", (run_id, sequence)
            ).fetchone()
        return self._tool_view(row)

    def get_agent_run(self, run_id: str) -> dict[str, Any] | None:
        with self._base._connect() as connection:
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            calls = connection.execute(
                "SELECT * FROM agent_tool_calls WHERE run_id=? ORDER BY sequence,tool_call_id", (run_id,)
            ).fetchall()
        value = self._view(row)
        value["tool_calls"] = [self._tool_view(call) for call in calls]
        return value

    def list_agent_runs(self, tenant_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._base._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM agent_runs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [value for row in rows if (value := self.get_agent_run(str(row[0]))) is not None]

    def _view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(row)
        return {"run_id": row["run_id"], "tenant_id": row["tenant_id"], "shop_id": row["shop_id"],
                "buyer_id": row["buyer_id"], "chat_id": row["chat_id"], "event_id": row["event_id"],
                "status": row["status"], "reply_origin": row.get("reply_origin"),
                "failure_reason": row.get("failure_reason"), "model_config_id": row.get("model_config_id"),
                "model_config_revision": row.get("model_config_revision"), "model_provider": row.get("model_provider"),
                "model_base_url_host": row.get("model_base_url_host"), "model_name": row.get("model_name"),
                "context": self._base._unprotect(row.get("context_protected")),
                "command_id": row.get("command_id"), "sent_message_id": row.get("sent_message_id"),
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at")}

    def _tool_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(row)
        return {"tool_call_id": row["tool_call_id"], "run_id": row["run_id"], "sequence": row["sequence"],
                "tool_name": row["tool_name"], "arguments": self._base._unprotect(row.get("arguments_protected")),
                "result": self._base._unprotect(row.get("result_protected")), "status": row["status"],
                "error_reason": row.get("error_reason"), "created_at": row.get("created_at")}
