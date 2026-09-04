from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.probe.capture_runner import (
    CAPTURE_HARNESS_VERSION,
    CapturePreflight,
    CaptureRunner,
    FailureInjection,
    PreflightFacts,
    ProbeWriteAllowlist,
    SingleCreateFuse,
    load_preflight,
)


TARGET = {
    "show_id": "show-101",
    "seat_id": "seat-537",
    "area_code": "36",
    "zone_type": "W+",
    "account_ref": "account-ref-1",
}


def preflight(tmp_path: Path) -> CapturePreflight:
    facts = PreflightFacts(
        run_id="run-test-1",
        probe_id="probe-test-1",
        **TARGET,
        account_ref_hash="account-hash",
        account_eligible=True,
        show_valid=True,
        seat_available=True,
        buyer_overlap=False,
        existing_show_probe_conflict=False,
        hold_present=False,
        release_pending=False,
    )
    return CapturePreflight(tmp_path / "runs", facts, development_commit="dev-commit")


def test_runner_version_and_preflight_are_target_bound_and_isolated(tmp_path: Path) -> None:
    prepared = preflight(tmp_path)
    path = prepared.persist()
    loaded = load_preflight(path)
    assert CAPTURE_HARNESS_VERSION == "M2.7+"
    assert loaded.facts.show_id == TARGET["show_id"]
    assert loaded.facts.seat_id == TARGET["seat_id"]
    assert loaded.facts.account_ref == TARGET["account_ref"]
    if os.name != "nt":
        assert oct((path.parent).stat().st_mode & 0o777) == "0o700"
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert loaded.facts.production_data_touched is False


@pytest.mark.asyncio
async def test_dry_run_state_is_written_under_the_isolated_run_only(tmp_path: Path) -> None:
    await CaptureRunner(preflight(tmp_path)).run_dry_run()
    run_dir = preflight(tmp_path).run_directory
    for name in ("probe-store.json", "show-lock.json", "account-lease.json"):
        assert (run_dir / name).is_file()
        assert json.loads((run_dir / name).read_text(encoding="utf-8"))["production_data_touched"] is False


def test_single_create_fuse_is_a_hard_process_level_limit() -> None:
    fuse = SingleCreateFuse(max_create_attempts=1)
    fuse.claim()
    with pytest.raises(RuntimeError, match="single_create_fuse_tripped"):
        fuse.claim()


def test_provider_write_allowlist_denies_everything_except_probe_create_cancel() -> None:
    allowlist = ProbeWriteAllowlist()
    assert allowlist.check("POST", "/order/create_order.api") is True
    assert allowlist.check("POST", "/order/cancel.api") is True
    assert allowlist.check("GET", "/order/create_order.api") is False
    assert allowlist.check("POST", "/activity/pay.api") is False
    assert allowlist.check("POST", "/order/confirm.api") is False


@pytest.mark.asyncio
async def test_dry_run_normal_lifecycle_replay_comparator_and_pricing(tmp_path: Path) -> None:
    result = await CaptureRunner(preflight(tmp_path)).run_dry_run()
    assert result.normal_lifecycle == "PASS"
    assert result.replay == "PASS"
    assert result.comparator == "MATCH"
    assert result.pricing_integration == "PASS"
    assert result.real_provider_write_occurred is False
    assert result.create_attempts == 1
    assert result.trace[-1] == "FINAL"
    assert result.redaction == "PASS"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    list(FailureInjection),
)
async def test_all_failure_injection_paths_are_fail_safe(tmp_path: Path, fault: FailureInjection) -> None:
    result = await CaptureRunner(preflight(tmp_path), fault=fault).run_dry_run()
    assert result.failure_injection == "PASS"
    assert result.real_provider_write_occurred is False
    assert result.create_attempts <= 1
    assert result.second_create_detected is False
    if fault == FailureInjection.CREATE_TIMEOUT:
        assert result.create_unknown_hold is True
    if fault == FailureInjection.CREATE_RESPONSE_CAPTURE_FAILURE:
        assert result.order_id_durable is True
        assert result.cleanup_independent is True
    if fault in {FailureInjection.RELEASE_5_FAILURE}:
        assert result.release_unverified_hold is True
    if fault == FailureInjection.ORDER_ID_DURABILITY_CRASH:
        assert result.order_id_durable is True


def test_preflight_rejects_target_mutation_and_production_paths(tmp_path: Path) -> None:
    prepared = preflight(tmp_path)
    with pytest.raises(ValueError, match="target_binding_mismatch"):
        prepared.assert_execute_target({**TARGET, "seat_id": "other-seat"})
    payload = json.loads(prepared.persist().read_text(encoding="utf-8"))
    assert payload["isolation"]["formal_transaction_state_path"] is None
    assert payload["isolation"]["outbox_path"] is None
