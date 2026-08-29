from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .observability import LOGGER
from .rules_first_store import RulesFirstStore
from .transaction_state_store import TransactionStateStore


class EventRuleEngine(Protocol):
    async def process_event(self, body: Mapping[str, Any]) -> dict[str, object]: ...
    def process_action_result(self, body: Mapping[str, Any]) -> dict[str, object]: ...


class RuleCoordinator(Protocol):
    def record_event_decision(
        self, body: Mapping[str, Any], result: dict[str, object],
    ) -> dict[str, object]: ...
    def record_action_result(self, *, tenant_id: str, body: Mapping[str, Any]) -> object | None: ...


class RulesFirstRuntime:
    """Durable event reducer and command producer.

    HTTP only appends to the inbox. This worker is the sole path from an event
    to durable commands; platform executors claim commands separately.
    """

    def __init__(
        self, store: RulesFirstStore, rule_engine: EventRuleEngine,
        coordinator: RuleCoordinator, state_store: TransactionStateStore,
        *, event_preprocessor: Callable[[Mapping[str, Any]], object] | None = None,
        idle_seconds: float = 0.1,
    ) -> None:
        self._store = store
        self._engine = rule_engine
        self._coordinator = coordinator
        self._states = state_store
        self._event_preprocessor = event_preprocessor
        self._idle_seconds = max(0.01, float(idle_seconds))
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    def accept(self, body: Mapping[str, Any]) -> dict[str, object]:
        accepted = self._store.enqueue_event(body)
        self._wake.set()
        return accepted

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="rules-first-event-worker")
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            processed = await self.drain_once()
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._idle_seconds)
            except TimeoutError:
                pass

    async def drain_once(self) -> bool:
        claimed = self._store.claim_event()
        if claimed is None:
            return False
        try:
            body = claimed["body"]
            if self._event_preprocessor is not None:
                self._event_preprocessor(body)
            result = await self._engine.process_event(body)
            reduced = self._coordinator.record_event_decision(body, result)
            decision = reduced.get("decision") if isinstance(reduced.get("decision"), Mapping) else {}
            actions = decision.get("actions") if isinstance(decision.get("actions"), list) else []
            rule = reduced.get("rule_decision") if isinstance(reduced.get("rule_decision"), Mapping) else {}
            revision = rule.get("state_revision") if isinstance(rule.get("state_revision"), int) else 0
            handoff_reason = str(rule.get("handoff_reason") or "").strip()
            if rule.get("state_after") == "MANUAL_HOLD" and handoff_reason:
                self._create_manual_task_from_event(body, revision, handoff_reason)
            self._store.complete_event(
                claimed["inbox_id"], claimed["lease_token"],
                commands=[item for item in actions if isinstance(item, Mapping)],
                state_revision=revision,
                result={
                    "transition_code": rule.get("transition_code"),
                    "state_after": rule.get("state_after"),
                },
            )
            return True
        except Exception as error:
            LOGGER.error(
                "event=rules_first_event_failed event_id=%s error_type=%s",
                claimed.get("event_id"), type(error).__name__,
            )
            self._store.fail_event(claimed["inbox_id"], claimed["lease_token"], str(error))
            await asyncio.sleep(self._idle_seconds)
            return True

    def claim_commands(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._store.claim_commands(limit=limit, lease_seconds=60)

    def open_write_fuse(self) -> int:
        commands = self._store.cancel_pending_commands("external_write_fuse_open")
        for command in commands:
            context = command.get("context") if isinstance(command.get("context"), Mapping) else {}
            envelope = context.get("envelope") if isinstance(context.get("envelope"), Mapping) else {}
            payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
            session = context.get("session") if isinstance(context.get("session"), Mapping) else {}
            identity = {
                "tenant_id": str(command.get("tenant_id") or "").strip(),
                "shop_id": str(session.get("accountUnb") or payload.get("accountUnb") or "").strip(),
                "buyer_id": str(session.get("peerUnb") or payload.get("peerUnb") or "").strip(),
                "chat_id": str(session.get("chatId") or payload.get("chatId") or "").strip(),
            }
            if not all(identity.values()):
                continue
            current = self._states.get(**identity)
            if current is None or current.flow_state in {"COMPLETED", "CANCELLED", "REFUNDED"}:
                continue
            updated = self._states.transition(
                **identity, expected_revision=current.revision,
                event_id=f"write-fuse:{command['command_id']}",
                transition_code="external_write_fuse_opened", flow_state="MANUAL_HOLD",
                updates={"price_change_status": "rejected"} if command["command_type"] == "change_order_price" else {},
            )
            self._create_manual_task(command, updated.revision, reason="external_write_fuse_open")
        return len(commands)

    def record_command_result(
        self, *, command_id: str, lease_token: str, result: Mapping[str, Any],
    ) -> dict[str, Any]:
        command = self._store.get_command(command_id)
        if command is None:
            raise KeyError("command_missing")
        recorded = self._store.record_command_result(command_id, lease_token, result)
        if recorded["status"] == "reconciling":
            return {"ok": True, **recorded, "actions_created": 0}

        action = command["action"]
        body = {
            "event_id": command["event_id"],
            "action_id": action.get("id"),
            "command_type": command.get("command_type"),
            "order_id": action.get("order_id"),
            "result": dict(result),
        }
        follow_up = self._engine.process_action_result(body)
        rule = self._coordinator.record_action_result(tenant_id=command["tenant_id"], body=body)
        revision = int(getattr(rule, "state_revision", command["state_revision"]))
        final_status = str(recorded["status"])
        rule_state_after = str(getattr(rule, "state_after", ""))
        rule_handoff = str(getattr(rule, "handoff_reason", "") or "").strip()
        if rule_state_after == "MANUAL_HOLD" and rule_handoff:
            self._create_manual_task(command, revision, reason=rule_handoff)
        elif final_status in {"failed", "unknown"}:
            self._create_manual_task(command, revision, reason=f"{command['command_type']}_{final_status}")
        actions = follow_up.get("actions") if isinstance(follow_up.get("actions"), list) else []
        if rule_state_after == "MANUAL_HOLD":
            actions = []
        created = self._store.append_commands(
            tenant_id=command["tenant_id"], event_id=command["event_id"],
            commands=[item for item in actions if isinstance(item, Mapping)],
            state_revision=revision,
        ) if actions else []
        return {"ok": True, **recorded, "actions_created": len(created)}

    def _create_manual_task_from_event(
        self, body: Mapping[str, Any], revision: int, reason: str,
    ) -> None:
        envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        session = body.get("session") if isinstance(body.get("session"), Mapping) else {}
        tenant_id = str(envelope.get("tenantId") or "").strip()
        shop_id = str(session.get("accountUnb") or payload.get("accountUnb") or "").strip()
        buyer_id = str(session.get("peerUnb") or payload.get("peerUnb") or "").strip()
        chat_id = str(session.get("chatId") or payload.get("chatId") or "").strip()
        if not all((tenant_id, shop_id, buyer_id, chat_id)):
            return
        current = self._states.get(
            tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id, chat_id=chat_id,
        )
        if current is None:
            return
        self._store.create_manual_task(
            tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id, chat_id=chat_id,
            transaction_id=current.state_id, transaction_revision=revision, reason=reason,
            details={"event_id": envelope.get("id")},
        )

    def _create_manual_task(self, command: Mapping[str, Any], revision: int, *, reason: str) -> None:
        context = command.get("context") if isinstance(command.get("context"), Mapping) else {}
        envelope = context.get("envelope") if isinstance(context.get("envelope"), Mapping) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        session = context.get("session") if isinstance(context.get("session"), Mapping) else {}
        tenant_id = str(command.get("tenant_id") or "").strip()
        shop_id = str(session.get("accountUnb") or payload.get("accountUnb") or "").strip()
        buyer_id = str(session.get("peerUnb") or payload.get("peerUnb") or "").strip()
        chat_id = str(session.get("chatId") or payload.get("chatId") or "").strip()
        if not all((tenant_id, shop_id, buyer_id, chat_id)):
            return
        current = self._states.get(
            tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id, chat_id=chat_id,
        )
        transaction_id = current.state_id if current is not None else f"unbound:{command['event_id']}"
        self._store.create_manual_task(
            tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id, chat_id=chat_id,
            transaction_id=transaction_id, transaction_revision=revision, reason=reason,
            details={"command_id": command["command_id"], "event_id": command["event_id"]},
        )
