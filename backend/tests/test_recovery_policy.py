import pytest

from app.recovery import GateResult, QuoteRecoveryOrchestrator, RecoveryAction, RecoveryPolicy, SafetyClass
from app.recovery.adapters import from_legacy
from app.recovery.context import QuotePipelineContext
from app.recovery.invalidation import invalidated_fields
from app.recovery.routing import select_runtime
from app.conversation_fact_store import ConversationFactStore
from datetime import datetime, timedelta, timezone


def test_success_continues():
    result = GateResult(gate="SHOW", status="RESOLVED", success=True, safety_class=SafetyClass.RECOVERABLE)
    assert RecoveryPolicy().evaluate(result).action is RecoveryAction.CONTINUE


def test_hard_safety_stops_pipeline():
    result = GateResult(gate="IDENTITY", status="IDENTITY_INCOMPLETE", success=False, safety_class=SafetyClass.HARD_SAFETY)
    decision = RecoveryPolicy().evaluate(result)
    assert decision.action is RecoveryAction.STOP
    assert decision.stop_scope == "STOP_QUOTE_PIPELINE"


def test_missing_fields_asks_minimal_clarification():
    result = GateResult(gate="CINEMA_ROUTE", status="ROUTE_UNRESOLVED", success=False,
                        safety_class=SafetyClass.RECOVERABLE, missing_fields=["city"])
    decision = RecoveryPolicy().evaluate(result)
    assert decision.action is RecoveryAction.ASK_CLARIFICATION
    assert decision.missing_fields == ["city"]


def test_candidates_ask_selection():
    result = GateResult(gate="SHOW", status="SHOW_UNRESOLVED", success=False,
                        safety_class=SafetyClass.RECOVERABLE, candidates=[{"show_id": "1"}])
    assert RecoveryPolicy().evaluate(result).action is RecoveryAction.ASK_CLARIFICATION


def test_retryable_retries():
    result = GateResult(gate="COST", status="PROVIDER_UNAVAILABLE", success=False,
                        safety_class=SafetyClass.RECOVERABLE, retryable=True)
    assert RecoveryPolicy().evaluate(result).action is RecoveryAction.RETRY


def test_soft_warning_falls_back_to_warning():
    result = GateResult(gate="REPLY", status="PARTIAL", success=False, safety_class=SafetyClass.SOFT_WARNING)
    assert RecoveryPolicy().evaluate(result).action is RecoveryAction.WARNING


def test_legacy_adapter_preserves_gate_and_reason():
    result = from_legacy("PROBE_REQUIRED", reason="provider_probe_required", cinema="万达")
    assert result.gate == "COST"
    assert result.reason_code == "provider_probe_required"
    assert result.metadata["cinema"] == "万达"


def test_dependency_invalidation_reaches_quote():
    assert "quote_record" in invalidated_fields("cinema")
    assert "pricing" in invalidated_fields("ticket_count")


def test_context_has_stable_identity_and_generation():
    context = QuotePipelineContext(identity={"shop_id": "1"}, generation=2)
    assert context.identity["shop_id"] == "1"
    assert context.generation == 2


def test_context_merge_prioritizes_current_and_invalidates_quote_chain():
    context = QuotePipelineContext(conversation_facts={"cinema": "A", "date": "2026-09-12"}, show="old", quote_record="old")
    context.merge_facts({"cinema": "B"}, stored={"cinema": "stale", "movie": "M"})
    assert context.conversation_facts["cinema"] == "B"
    assert context.show is None
    assert context.quote_record is None


def test_context_merge_aliases_invalidate_show_and_seat():
    context = QuotePipelineContext(conversation_facts={"showtime_start": "14:00"}, show="old", cost="old", quote_record="old")
    context.merge_facts({"showtime_start": "15:00", "selected_seats": ["3排4座"]})
    assert context.show is None
    assert context.cost is None
    assert context.quote_record is None


def test_facts_are_isolated_and_expire(tmp_path):
    store = ConversationFactStore(tmp_path / "facts.sqlite", ttl_seconds=60)
    base = dict(tenant_id="t", shop_id="s", chat_id="c", purchase_context_id="p")
    store.save(**base, buyer_id="buyer-a", facts={"city": "合肥"}, source="test", observed_at=datetime.now(timezone.utc))
    assert store.load_context(**base, buyer_id="buyer-b")["available"] is False
    expired = store.load_context(**base, buyer_id="buyer-a", now=datetime.now(timezone.utc) + timedelta(seconds=120))
    assert expired["available"] is False
    assert expired["expired"] is True


def test_runtime_routing_selects_one_path():
    legacy, recovery = object(), object()
    assert select_runtime(recovery_enabled=False, legacy_enabled=True, image_event=True, text_event=False,
                          recovery_runtime=recovery, legacy_runtime=legacy) is legacy
    assert select_runtime(recovery_enabled=True, legacy_enabled=True, image_event=True, text_event=False,
                          recovery_runtime=recovery, legacy_runtime=legacy) is recovery
    assert select_runtime(recovery_enabled=True, legacy_enabled=True, image_event=False, text_event=True,
                          recovery_runtime=recovery, legacy_runtime=legacy) is recovery
    assert select_runtime(recovery_enabled=False, legacy_enabled=False, image_event=True, text_event=False,
                          recovery_runtime=recovery, legacy_runtime=legacy) is None


@pytest.mark.asyncio
async def test_orchestrator_stops_at_first_non_continuation():
    async def route(context):
        return GateResult(gate="CINEMA_ROUTE", status="RESOLVED", success=True, safety_class=SafetyClass.RECOVERABLE)

    async def show(context):
        return GateResult(gate="SHOW", status="SHOW_UNRESOLVED", success=False,
                          safety_class=SafetyClass.RECOVERABLE, missing_fields=["showtime"])

    async def cost(context):
        raise AssertionError("later stages must not run")

    context, result, decision = await QuoteRecoveryOrchestrator([route, show, cost]).run(QuotePipelineContext())
    assert context.current_gate == "SHOW"
    assert result.status == "SHOW_UNRESOLVED"
    assert decision.action is RecoveryAction.ASK_CLARIFICATION


@pytest.mark.asyncio
async def test_orchestrator_executes_retry_handler():
    attempts = {"count": 0}

    def stage(context):
        attempts["count"] += 1
        return GateResult(gate="COST", status="PROVIDER_UNAVAILABLE", success=False,
                          safety_class=SafetyClass.RECOVERABLE, retryable=True)

    def retry(context, result):
        return GateResult(gate="COST", status="COST_READY", success=True, safety_class=SafetyClass.RECOVERABLE)

    context, result, decision = await QuoteRecoveryOrchestrator(
        [stage], retry_handlers={"COST": retry}, max_recovery_attempts=1,
    ).run(QuotePipelineContext())
    assert attempts["count"] == 1
    assert result.status == "COST_READY"
    assert decision.action is RecoveryAction.CONTINUE


@pytest.mark.asyncio
async def test_orchestrator_retry_budget_is_per_run():
    orchestrator = QuoteRecoveryOrchestrator(
        [lambda context: GateResult(gate="COST", status="PROVIDER_UNAVAILABLE", success=False,
                                    safety_class=SafetyClass.RECOVERABLE, retryable=True)],
        retry_handlers={"COST": lambda context, result: GateResult(
            gate="COST", status="COST_READY", success=True, safety_class=SafetyClass.RECOVERABLE)},
        max_recovery_attempts=1,
    )
    first = await orchestrator.run(QuotePipelineContext())
    second = await orchestrator.run(QuotePipelineContext())
    assert first[1].status == "COST_READY"
    assert second[1].status == "COST_READY"


@pytest.mark.asyncio
async def test_orchestrator_isolates_stage_exception():
    async def broken_stage(context):
        raise RuntimeError("provider adapter exploded")

    context, result, decision = await QuoteRecoveryOrchestrator([broken_stage]).run(QuotePipelineContext())

    assert context.current_gate == "PIPELINE"
    assert result.status == "STAGE_EXCEPTION"
    assert result.reason_code == "RuntimeError"
    assert result.metadata == {"stage": "broken_stage", "stage_index": 0}
    assert decision.action is RecoveryAction.STOP
    assert decision.stop_scope == "STOP_QUOTE_PIPELINE"
