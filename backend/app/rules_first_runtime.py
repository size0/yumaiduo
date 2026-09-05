from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
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
        fulfillment_mark_handler: Callable[[Mapping[str, Any]], Awaitable[dict[str, object] | None]] | None = None,
        payment_validation_handler: Callable[[Mapping[str, Any]], Awaitable[dict[str, object] | None]] | None = None,
        liangpiao_fulfillment_handler: Callable[[Mapping[str, Any], Mapping[str, Any]], Awaitable[dict[str, object] | None]] | None = None,
        idle_seconds: float = 0.1,
    ) -> None:
        self._store = store
        self._engine = rule_engine
        self._coordinator = coordinator
        self._states = state_store
        self._event_preprocessor = event_preprocessor
        self._fulfillment_mark_handler = fulfillment_mark_handler
        self._payment_validation_handler = payment_validation_handler
        self._liangpiao_fulfillment_handler = liangpiao_fulfillment_handler
        self._idle_seconds = max(0.01, float(idle_seconds))
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    def accept(self, body: Mapping[str, Any]) -> dict[str, object]:
        accepted = self._store.enqueue_event(body)
        self._wake.set()
        return accepted

    def accept_canonical_result(
        self, body: Mapping[str, Any], result: Mapping[str, Any],
        *, source: str = "canonical_quote_runtime",
        decision_reason: str = "canonical_quote_reply_ready",
    ) -> dict[str, object]:
        """Durably commit a synchronously produced canonical reply.

        Canonical image processing is intentionally read-only and runs before
        this method.  This method only uses the existing RulesFirst inbox,
        transaction reducer, and command outbox; it never calls a provider or
        a platform sender.
        """
        accepted = self._store.enqueue_event(body)
        if accepted.get("duplicate") is True:
            return {**accepted, "canonical_result_duplicate": True, "commands": []}
        envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
        tenant_id = str(envelope.get("tenantId") or envelope.get("tenant_id") or "").strip()
        event_id = str(envelope.get("id") or envelope.get("eventId") or "").strip()
        if not tenant_id or not event_id:
            return {**accepted, "canonical_result_error": "identity_incomplete", "commands": []}
        claimed = self._store.claim_event(
            tenant_id=tenant_id, event_id=event_id,
        )
        if claimed is None:
            return {**accepted, "canonical_result_pending": True, "commands": []}
        rendered_text = str(result.get("current_runtime_reply") or "").strip()
        rendered_replies = result.get("current_runtime_replies")
        agent_run_id = str(result.get("agent_run_id") or "").strip()
        actions: list[dict[str, object]] = []
        if isinstance(rendered_replies, list) and rendered_replies:
            for index, item in enumerate(rendered_replies, start=1):
                if not isinstance(item, Mapping):
                    continue
                text = str(item.get("text") or "").strip()
                kind = str(item.get("kind") or "").strip()
                if not text or not kind:
                    continue
                suffix = "summary" if kind == "purchase_summary" else "price" if kind == "price" else str(index)
                action_id = f"{event_id}:reply" if suffix == "price" else f"{event_id}:reply:{suffix}"
                actions.append({
                    "id": action_id,
                    "type": "send_message",
                    "text": text,
                    "rule_governed": True,
                    "source": source,
                    "canonical_reply_kind": f"{result.get('canonical_reply_kind') or ''}:{kind}",
                    "canonical_reply_sequence": index,
                    "dedupe_key": f"reply:{event_id}:{suffix}",
                    **({"agent_run_id": agent_run_id} if agent_run_id else {}),
                })
        elif rendered_text:
            actions.append({
                "id": f"{event_id}:reply",
                "type": "send_message",
                "text": rendered_text,
                "rule_governed": True,
                "source": source,
                "canonical_reply_kind": str(result.get("canonical_reply_kind") or ""),
                "dedupe_key": f"reply:{event_id}",
                **({"agent_run_id": agent_run_id} if agent_run_id else {}),
            })
        canonical_result = dict(result)
        canonical_result["decision"] = {
            "mode": "canonical",
            "reason": decision_reason,
            "actions": actions,
        }
        try:
            reduced = self._coordinator.record_event_decision(body, canonical_result)
            decision = reduced.get("decision") if isinstance(reduced.get("decision"), Mapping) else {}
            final_actions = [item for item in decision.get("actions", []) if isinstance(item, Mapping)]
            rule = reduced.get("rule_decision") if isinstance(reduced.get("rule_decision"), Mapping) else {}
            revision = rule.get("state_revision") if isinstance(rule.get("state_revision"), int) else 0
            commands = self._store.complete_event(
                claimed["inbox_id"], claimed["lease_token"], commands=final_actions,
                state_revision=revision,
                result={
                    "transition_code": rule.get("transition_code"),
                    "state_after": rule.get("state_after"),
                },
            )
            return {
                **accepted, "canonical_result": reduced,
                "current_runtime_reply": rendered_text,
                "current_runtime_replies": rendered_replies if isinstance(rendered_replies, list) else [],
                "commands": commands,
            }
        except Exception as error:
            self._store.fail_event(claimed["inbox_id"], claimed["lease_token"], str(error))
            raise

    def accept_agent_result(
        self, body: Mapping[str, Any], result: Mapping[str, Any],
    ) -> dict[str, object]:
        """Commit an agent reply through the same inbox/outbox path as quotes."""
        normalized = dict(result)
        normalized["current_runtime_reply"] = (
            str(result.get("reply") or "").strip()
            if result.get("status") == "AGENT_REPLY_READY" else ""
        )
        normalized.setdefault("canonical_reply_kind", "canonical_conversation_agent")
        return self.accept_canonical_result(
            body, normalized, source="canonical_conversation_agent",
            decision_reason="canonical_agent_reply_ready",
        )

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
            result = None
            if self._fulfillment_mark_handler is not None:
                result = await self._fulfillment_mark_handler(body)
            if result is None and self._payment_validation_handler is not None:
                result = await self._payment_validation_handler(body)
            if (
                result is not None
                and result.get("validation_status") == "VERIFIED_PAID"
                and not result.get("duplicate")
                and self._liangpiao_fulfillment_handler is not None
            ):
                fulfillment = await self._liangpiao_fulfillment_handler(body, result)
                if fulfillment is not None:
                    result = fulfillment
            if result is None:
                result = await self._engine.process_event(body)
                reduced = self._coordinator.record_event_decision(body, result)
            else:
                reduced = result
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

    def claim_commands(
        self, *, limit: int = 10, command_types: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._store.claim_commands(
            limit=limit, lease_seconds=60, command_types=command_types,
        )

    def open_transaction_write_fuse(self) -> int:
        return self._open_write_fuse(
            "agent_harness_read_only",
            command_types=frozenset({
                "change_order_price", "create_liangpiao_order",
                "cancel_failed_liangpiao_source_order", "cancel_paid_amount_mismatch",
                "cancel_order", "submit_fulfillment", "send_ticket", "refund_or_intercept",
            }),
        )

    def open_write_fuse(self) -> int:
        return self._open_write_fuse("external_write_fuse_open")

    def _open_write_fuse(self, reason: str, *, command_types: frozenset[str] | None = None) -> int:
        commands = self._store.cancel_pending_commands(reason, command_types=command_types)
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
            self._create_manual_task(command, updated.revision, reason=reason)
        return len(commands)

    def record_command_result(
        self, *, command_id: str, lease_token: str, result: Mapping[str, Any],
    ) -> dict[str, Any]:
        command = self._store.get_command(command_id)
        if command is None:
            raise KeyError("command_missing")
        recorded = self._store.record_command_result(command_id, lease_token, result)
        self._record_agent_delivery(command, result, recorded)
        self._ensure_new_flow_transaction(command)
        if recorded["status"] == "reconciling":
            return {"ok": True, **recorded, "actions_created": 0}

        action = command["action"]
        body = {
            "event_id": command["event_id"],
            "action_id": action.get("id"),
            "command_type": command.get("command_type"),
            "order_id": action.get("order_id"),
            "action": dict(action),
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

    def _record_agent_delivery(
        self, command: Mapping[str, Any], result: Mapping[str, Any], recorded: Mapping[str, Any],
    ) -> None:
        action = command.get("action") if isinstance(command.get("action"), Mapping) else {}
        run_id = str(action.get("agent_run_id") or "").strip()
        if not run_id:
            return
        try:
            if str(recorded.get("status")) == "succeeded":
                message_id = str(
                    result.get("message_id") or result.get("messageId")
                    or result.get("sent_message_id") or result.get("sentMessageId") or ""
                ).strip()
                if message_id:
                    self._store.record_agent_run_delivery(
                        run_id, command_id=str(command.get("command_id") or ""),
                        sent_message_id=message_id, status="sent",
                    )
                else:
                    self._store.update_agent_run(
                        run_id, status="delivered_without_message_id",
                        command_id=str(command.get("command_id") or ""),
                    )
            elif str(recorded.get("status")) in {"failed", "unknown", "cancelled"}:
                self._store.update_agent_run(
                    run_id, status="delivery_failed",
                    command_id=str(command.get("command_id") or ""),
                    failure_reason=f"message_{recorded.get('status')}",
                )
        except Exception:
            LOGGER.warning(
                "event=agent_delivery_audit_failed command_id=%s run_id=%s",
                command.get("command_id"), run_id,
            )

    def _ensure_new_flow_transaction(self, command: Mapping[str, Any]) -> None:
        action = command.get("action") if isinstance(command.get("action"), Mapping) else {}
        snapshot = action.get("quote_snapshot") if isinstance(action.get("quote_snapshot"), Mapping) else {}
        if snapshot.get("flow_version") != "V4_NEW_FLOW_V2":
            return
        if getattr(self._states, "authority_name", None) != "rules_first_sqlite":
            raise ValueError("new_flow_transaction_state_authority_invalid")
        identity = {
            field: str(action.get(field) or snapshot.get(field) or "").strip()
            for field in ("tenant_id", "shop_id", "buyer_id", "chat_id")
        }
        order_id = str(action.get("platform_order_id") or snapshot.get("order_id") or "").strip()
        target = snapshot.get("target_amount_cents")
        if not all(identity.values()) or not order_id or not isinstance(target, int) or isinstance(target, bool):
            raise ValueError("new_flow_command_identity_invalid")
        current = self._states.get(**identity)
        if current is None:
            current = self._states.get_or_create(**identity)
        if current.flow_state in {"PRICE_CHANGING", "WAITING_PAYMENT", "PAID_WAITING_FULFILLMENT", "TICKET_SENT"}:
            return
        if current.order_id and current.order_id != order_id:
            return
        if current.flow_state in {"COMPLETED", "CANCELLED", "REFUNDED", "MANUAL_HOLD"}:
            return
        self._states.transition(
            **identity, expected_revision=current.revision,
            event_id=f"{command['event_id']}:new-flow-command",
            transition_code="new_flow_reprice_command_created",
            flow_state="PRICE_CHANGING",
            updates={
                "quote_status": "ready", "confirmation_status": "confirmed",
                "order_status": "bound", "price_change_status": "pending",
                "order_id": order_id, "target_amount_cents": target,
                "price_change_command_id": str(action.get("id") or command["command_id"]),
                "expected_inputs": [],
            },
            allow_compatible_bootstrap=True,
        )

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
