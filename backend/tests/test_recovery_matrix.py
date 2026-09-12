import pytest

from app.recovery import GateResult, RecoveryAction, RecoveryPolicy, SafetyClass
from app.recovery.invalidation import invalidated_fields


@pytest.mark.parametrize(
    ("status", "success", "missing", "candidates", "expected"),
    [
        ("RECOGNIZED", True, [], [], RecoveryAction.CONTINUE),
        ("PARTIAL", True, ["showtime"], [], RecoveryAction.CONTINUE),
        ("CITY_REQUIRED", False, ["city"], [], RecoveryAction.ASK_CLARIFICATION),
        ("CINEMA_REQUIRED", False, ["cinema"], [], RecoveryAction.ASK_CLARIFICATION),
        ("WANDA_CINEMA_NOT_UNIQUE", False, [], [{"id": "a"}, {"id": "b"}], RecoveryAction.ASK_CLARIFICATION),
        ("INPUT_INCOMPLETE", False, ["showtime"], [], RecoveryAction.ASK_CLARIFICATION),
        ("NOT_FOUND", False, [], [], RecoveryAction.FALLBACK),
        ("SEATS_NOT_SELECTED", True, [], [], RecoveryAction.CONTINUE),
        ("MANUAL_MARK_REQUIRED", False, ["selected_seats"], [], RecoveryAction.ASK_CLARIFICATION),
        ("PROBE_REQUIRED", False, [], [], RecoveryAction.FALLBACK),
        ("COST_UNAVAILABLE", False, [], [], RecoveryAction.FALLBACK),
        ("PROVIDER_UNAVAILABLE", False, [], [], RecoveryAction.FALLBACK),
        ("MISSING_COST", False, ["cost"], [], RecoveryAction.ASK_CLARIFICATION),
        ("RULE_NOT_FOUND", False, [], [], RecoveryAction.FALLBACK),
        ("INVALID", False, [], [], RecoveryAction.FALLBACK),
        ("PERSIST_FAILED", False, [], [], RecoveryAction.RETRY),
        ("AMOUNT_REPLY_ALLOWED", True, [], [], RecoveryAction.CONTINUE),
        ("NON_AMOUNT_REPLY_ALLOWED", True, [], [], RecoveryAction.CONTINUE),
        ("NO_SAFE_REPLY", False, [], [], RecoveryAction.STOP),
    ],
)
def test_recovery_policy_matrix(status, success, missing, candidates, expected):
    result = GateResult(gate="MATRIX", status=status, success=success,
                        safety_class=SafetyClass.HARD_SAFETY if status == "NO_SAFE_REPLY" else SafetyClass.RECOVERABLE,
                        missing_fields=missing, candidates=candidates, retryable=status == "PERSIST_FAILED")
    assert RecoveryPolicy().evaluate(result).action is expected


def test_invalidation_matrix():
    assert set(invalidated_fields("cinema")) >= {"show", "seat_facts", "cost", "pricing", "quote_record"}
    assert set(invalidated_fields("movie")) >= {"show", "seat_facts", "cost", "pricing", "quote_record"}
    assert set(invalidated_fields("date")) >= {"show", "seat_facts", "cost", "pricing", "quote_record"}
    assert set(invalidated_fields("show")) >= {"seat_facts", "cost", "pricing", "quote_record"}
    assert set(invalidated_fields("seat")) >= {"cost", "pricing", "quote_record"}
    assert set(invalidated_fields("ticket_count")) >= {"pricing", "quote_record"}
