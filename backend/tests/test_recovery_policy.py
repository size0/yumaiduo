import pytest

from app.recovery import GateResult, QuoteRecoveryOrchestrator, RecoveryAction, RecoveryPolicy, SafetyClass
from app.recovery.adapters import from_legacy
from app.recovery.context import QuotePipelineContext
from app.recovery.invalidation import invalidated_fields


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
