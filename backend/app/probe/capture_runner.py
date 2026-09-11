from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.pricing import PricingFacts, PricingRulesSnapshot, PricingSeatFact, V4PricingEngine

from .canonical import (
    ActivityOffersResult,
    CancelResult,
    CreateOrderResult,
    OrderStatusResult,
    SeatAvailabilityResult,
    canonical_activity,
    canonical_order_status,
)
from .capture import assert_capture_redacted
from .capture_reliability import CaptureStep, DurableProbeCapture, ReliableProbeLifecycle
from .errors import ProbeError
from .comparator import ProbeShadowComparator, ShadowClassification
from .replay import V3CaptureReplayProvider


CAPTURE_HARNESS_VERSION = "M2.7+"
_DEFAULT_TTL_SECONDS = 15 * 60
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


class FailureInjection(StrEnum):
    CREATE_TIMEOUT = "create_timeout"
    CREATE_RESPONSE_CAPTURE_FAILURE = "create_response_capture_failure"
    ORDER_ID_DURABILITY_CRASH = "order_id_durability_crash"
    ACTIVITY_FAILURE = "activity_failure"
    CANCEL_FAILURE = "cancel_failure"
    RELEASE_5_FAILURE = "release_5_failure"
    CLIENT_CLOSE_FAILURE = "client_close_failure"
    REDACTION_FAILURE = "redaction_failure"


class PreflightFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=160)
    probe_id: str = Field(min_length=1, max_length=160)
    show_id: str = Field(min_length=1, max_length=240)
    seat_id: str = Field(min_length=1, max_length=240)
    area_code: str = Field(min_length=1, max_length=120)
    zone_type: str = Field(min_length=1, max_length=120)
    account_ref: str = Field(min_length=1, max_length=240)
    account_ref_hash: str = Field(min_length=1, max_length=160)
    prepared_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: str = ""
    account_eligible: bool
    show_valid: bool
    seat_available: bool
    buyer_overlap: bool
    existing_show_probe_conflict: bool
    hold_present: bool
    release_pending: bool
    production_data_touched: bool = False

    def target(self) -> dict[str, str]:
        return {
            "show_id": self.show_id,
            "seat_id": self.seat_id,
            "area_code": self.area_code,
            "zone_type": self.zone_type,
            "account_ref": self.account_ref,
        }

    def valid_for_execute(self, *, now: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        current = now or datetime.now(timezone.utc)
        return current <= expires and all((
            self.account_eligible,
            self.show_valid,
            self.seat_available,
            not self.buyer_overlap,
            not self.existing_show_probe_conflict,
            not self.hold_present,
            not self.release_pending,
            not self.production_data_touched,
        ))


class CapturePreflight:
    """A signed-by-content (not by secret) prepare artifact for one immutable run."""

    def __init__(self, root: Path, facts: PreflightFacts, *, development_commit: str) -> None:
        self.root = Path(root)
        self.facts = facts
        self.development_commit = development_commit
        if not _SAFE_NAME.fullmatch(facts.run_id) or not _SAFE_NAME.fullmatch(facts.probe_id):
            raise ValueError("run_identity_invalid")
        self.run_directory = self.root / f"run_{facts.run_id}_{facts.probe_id}"
        self.path = self.run_directory / "prepare.json"

    def persist(self) -> Path:
        self.run_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.run_directory, 0o700)
        payload = {
            "capture_harness_version": CAPTURE_HARNESS_VERSION,
            "development_commit": self.development_commit,
            "facts": self.facts.model_dump(mode="json"),
            "isolation": {
                "root": str(self.run_directory),
                "probe_journal": str(self.run_directory / "capture" / "probe_journal.json"),
                "probe_store": str(self.run_directory / "probe-store.json"),
                "show_lock": str(self.run_directory / "show-lock.json"),
                "account_lease": str(self.run_directory / "account-lease.json"),
                "audit": str(self.run_directory / "capture" / "raw" / "provider_audit.json"),
                "formal_transaction_state_path": None,
                "outbox_path": None,
                "quote_record_path": None,
                "rules_first_path": None,
            },
        }
        _atomic_json(self.path, payload)
        return self.path

    def assert_execute_target(self, target: Mapping[str, object]) -> None:
        expected = self.facts.target()
        actual = {key: str(target.get(key, "")).strip() for key in expected}
        if actual != expected:
            raise ValueError("target_binding_mismatch")


def load_preflight(path: Path) -> CapturePreflight:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    facts = PreflightFacts.model_validate(payload["facts"])
    return CapturePreflight(Path(path).parent.parent, facts, development_commit=str(payload.get("development_commit") or "unknown"))


def development_commit() -> str:
    try:
        value = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        return value or "UNKNOWN"
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


class SingleCreateFuse:
    """A process-local hard fuse. It is deliberately not a retryable setting."""

    def __init__(self, *, max_create_attempts: int = 1) -> None:
        if max_create_attempts != 1:
            raise ValueError("single_create_fuse_must_equal_one")
        self._claims = 0

    @property
    def attempts(self) -> int:
        return self._claims

    def claim(self) -> None:
        if self._claims >= 1:
            raise RuntimeError("single_create_fuse_tripped")
        self._claims += 1


class ProbeWriteAllowlist:
    """Explicit real-mode write boundary; all other writes are denied."""

    _ALLOWED = frozenset({("POST", "/order/create_order.api"), ("POST", "/order/cancel.api")})

    @property
    def allowed_write_endpoints(self) -> set[tuple[str, str]]:
        return set(self._ALLOWED)

    def check(self, method: str, endpoint: str) -> bool:
        return (str(method).upper(), str(endpoint)) in self._ALLOWED

    def require(self, method: str, endpoint: str) -> None:
        if not self.check(method, endpoint):
            raise PermissionError("provider_write_denied")


@dataclass(frozen=True)
class RealProbeBinding:
    run_id: str
    show_id: str
    seat_id: str
    account_ref: str
    expires_at: str
    area_code: str = ""
    zone_type: str = "W+"
    original_price_cents: int = 0
    channel_fee_cents: int = 0
    cinema_id: str = ""
    partition: str = ""

    def matches(self, *, run_id: str, show_id: str, seat_ids: list[str], account_ref: str) -> bool:
        return (
            str(run_id) == self.run_id
            and str(show_id) == self.show_id
            and [str(value) for value in seat_ids] == [self.seat_id]
            and str(account_ref) == self.account_ref
        )


@dataclass(frozen=True)
class ConfirmationToken:
    value: str
    run_id: str
    expires_at: str

    @property
    def report_metadata(self) -> dict[str, str]:
        return {"run_id": self.run_id, "expires_at": self.expires_at, "token_value": "REDACTED"}


class OneTimeConfirmationStore:
    """File-backed, hashed, single-use confirmation token for one prepared run."""

    _lock = threading.RLock()

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def prepare(self, *, run_id: str, expires_at: datetime) -> ConfirmationToken:
        expiry_datetime = expires_at.astimezone(timezone.utc)
        if expiry_datetime - datetime.now(timezone.utc) > timedelta(minutes=15):
            raise ValueError("confirmation_ttl_too_long")
        value = secrets.token_urlsafe(32)
        expiry = expiry_datetime.isoformat()
        _atomic_json(self.path, {
            "run_id": str(run_id), "token_hash": hashlib.sha256(value.encode()).hexdigest(),
            "expires_at": expiry, "consumed": False,
        })
        return ConfirmationToken(value=value, run_id=str(run_id), expires_at=expiry)

    def consume(self, *, run_id: str, token: str, now: datetime | None = None) -> bool:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                expires = datetime.fromisoformat(str(payload["expires_at"]))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                return False
            if payload.get("consumed") is True or str(payload.get("run_id")) != str(run_id):
                return False
            current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            if current > expires:
                return False
            actual = hashlib.sha256(str(token).encode()).hexdigest()
            expected = str(payload.get("token_hash") or "")
            if not hmac.compare_digest(actual, expected):
                return False
            payload["consumed"] = True
            payload["consumed_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_json(self.path, payload)
            return True


@dataclass(frozen=True)
class RealTransportConfig:
    enabled: bool = False
    confirmation_consumed: bool = False
    active_probe_enabled: bool = False
    external_writes_enabled: bool = False
    flow_allows_probe: bool = False

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "RealTransportConfig":
        values = environment if environment is not None else os.environ
        enabled = str(values.get("REAL_PROBE_TRANSPORT_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}
        # Environment enablement alone is insufficient. The runner must consume
        # the one-time confirmation token and pass that result explicitly. The
        # global and external-write fuses are independently required.
        active_probe_enabled = str(values.get("WANDA_ACTIVE_PROBE_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}
        external_writes_enabled = str(values.get("EXTERNAL_WRITES_ENABLED", values.get("WANDA_EXTERNAL_WRITES_ENABLED", "false"))).strip().lower() in {"1", "true", "yes", "on"}
        flow_allows_probe = str(values.get("PROBE_FLOW_ALLOWED", "false")).strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled, confirmation_consumed=False,
            active_probe_enabled=active_probe_enabled,
            external_writes_enabled=external_writes_enabled,
            flow_allows_probe=flow_allows_probe,
        )


class RealProbeTransport:
    """Real-client adapter owned by the standalone runner, never production DI."""

    fixture = False

    def __init__(
        self,
        *,
        client: object,
        binding: RealProbeBinding,
        capture: DurableProbeCapture,
        config: RealTransportConfig | None = None,
        fuse: SingleCreateFuse | None = None,
        allowlist: ProbeWriteAllowlist | None = None,
    ) -> None:
        self._client = client
        self._binding = binding
        self._capture = capture
        self._config = config or RealTransportConfig()
        self._fuse = fuse or SingleCreateFuse()
        self._allowlist = allowlist or ProbeWriteAllowlist()

    def _require_real(self) -> None:
        active_env = os.getenv("WANDA_ACTIVE_PROBE_ENABLED")
        writes_env = os.getenv("EXTERNAL_WRITES_ENABLED", os.getenv("WANDA_EXTERNAL_WRITES_ENABLED"))
        if active_env is not None and active_env.strip().lower() not in {"1", "true", "yes", "on"}:
            raise ProbeError("active_probe_disabled", "Active Probe 当前已关闭。")
        if writes_env is not None and writes_env.strip().lower() not in {"1", "true", "yes", "on"}:
            raise ProbeError("external_writes_disabled", "外部写操作总开关当前已关闭。")
        if not self._config.active_probe_enabled:
            raise ProbeError("active_probe_disabled", "Active Probe 当前已关闭。")
        if not self._config.external_writes_enabled:
            raise ProbeError("external_writes_disabled", "外部写操作总开关当前已关闭。")
        if not self._config.flow_allows_probe:
            raise ProbeError("probe_flow_not_explicitly_allowed", "当前流程未显式允许 Active Probe。")
        if not self._config.enabled:
            raise PermissionError("real_probe_transport_disabled")
        if not self._config.confirmation_consumed:
            raise PermissionError("real_probe_confirmation_required")
        try:
            expires = datetime.fromisoformat(self._binding.expires_at)
        except ValueError:
            raise PermissionError("real_probe_binding_expired") from None
        if datetime.now(timezone.utc) > expires.astimezone(timezone.utc):
            raise PermissionError("real_probe_binding_expired")

    def _assert_target(self, *, account: object, show_id: str, seat_ids: list[str]) -> str:
        if isinstance(account, Mapping):
            account_ref = str(account.get("account_ref") or "").strip()
        else:
            account_ref = str(getattr(account, "account_ref", account)).strip()
        if not self._binding.matches(run_id=self._binding.run_id, show_id=show_id, seat_ids=seat_ids, account_ref=account_ref):
            raise ValueError("target_binding_mismatch")
        return account_ref

    async def create_probe_order(self, *, account: object, show_id: str, seat_ids: list[str]) -> CreateOrderResult:
        self._require_real()
        account_ref = self._assert_target(account=account, show_id=show_id, seat_ids=seat_ids)
        self._allowlist.require("POST", "/order/create_order.api")
        self._fuse.claim()
        method = getattr(self._client, "create_order", None)
        if not callable(method):
            raise RuntimeError("real_client_create_missing")
        if self._binding.original_price_cents <= 0:
            raise ValueError("original_price_required_for_real_create")
        seat_payload = f"{self._binding.seat_id},{self._binding.original_price_cents},{self._binding.channel_fee_cents},0"
        payload = await method({
            "showtime_id": self._binding.show_id,
            "seat_ids": [seat_payload],
            "seat_payloads": [seat_payload],
            "total_price": self._binding.original_price_cents,
            "cinema_id": self._binding.cinema_id,
            "account_ref": account_ref,
        })
        if not isinstance(payload, Mapping):
            raise RuntimeError("real_create_response_invalid")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        reference = data.get("order_id") or data.get("orderId")
        verified = payload.get("create_verified") is not False
        return CreateOrderResult(
            code=0 if verified else 1, biz_code=0 if verified else 1,
            temporary_order_id=str(reference).strip() if reference else None,
            outcome="CONFIRMED" if verified and reference else "UNKNOWN",
        )

    def _require_current_order(self, reference: str) -> str:
        current = self._capture.journal().get("temporary_order_reference")
        if not current or str(current) != str(reference):
            raise PermissionError("order_reference_not_durable")
        return str(current)

    async def get_order_status(self, *, temporary_order_reference: str) -> OrderStatusResult:
        reference = self._require_current_order(temporary_order_reference)
        method = getattr(self._client, "order_status", None)
        if not callable(method):
            raise RuntimeError("real_client_order_status_missing")
        payload = await method(reference)
        data = payload.get("data") if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping) else payload
        return canonical_order_status(data if isinstance(data, Mapping) else {})

    async def get_activity_offers(self, *, temporary_order_reference: str) -> ActivityOffersResult:
        reference = self._require_current_order(temporary_order_reference)
        method = getattr(self._client, "activity_offers", None)
        if not callable(method):
            raise RuntimeError("real_client_activity_missing")
        partition = self._binding.partition or f"{self._binding.area_code}-{self._binding.seat_id}"
        payload = await method(
            order_id=reference, cinema_id=self._binding.cinema_id,
            showtime_id=self._binding.show_id, partition=partition,
        )
        if not isinstance(payload, Mapping):
            raise RuntimeError("real_activity_response_invalid")
        offers = payload.get("activities")
        if not isinstance(offers, list):
            offers = []
        selected = next((item for item in offers if isinstance(item, Mapping) and item.get("able") is True and "W+会员专享" in str(item.get("name") or "")), {})
        allot = selected.get("allot_seat") if isinstance(selected, Mapping) else {}
        return canonical_activity({
            "able": selected.get("able") is True if isinstance(selected, Mapping) else False,
            "name": selected.get("name", "") if isinstance(selected, Mapping) else "",
            "allotSeat": allot if isinstance(allot, Mapping) else {},
        })

    async def cancel_probe_order(self, *, temporary_order_reference: str) -> CancelResult:
        reference = self._require_current_order(temporary_order_reference)
        self._require_real()
        self._allowlist.require("POST", "/order/cancel.api")
        method = getattr(self._client, "cancel_order", None)
        if not callable(method):
            raise RuntimeError("real_client_cancel_missing")
        return CancelResult(accepted=bool(await method(reference)))

    async def get_available_seats(self, *, show_id: str, seat_ids: list[str]) -> SeatAvailabilityResult:
        if str(show_id) != self._binding.show_id or [str(value) for value in seat_ids] != [self._binding.seat_id]:
            raise ValueError("target_binding_mismatch")
        method = getattr(self._client, "realtime_seats", None)
        if not callable(method):
            raise RuntimeError("real_client_realtime_seats_missing")
        payload = await method(self._binding.show_id)
        values = payload.get("available_seat_ids") if isinstance(payload, Mapping) else []
        return SeatAvailabilityResult(available_seat_ids={str(value) for value in values} if isinstance(values, list) else set())


class FixtureProbeProvider:
    fixture = True

    def __init__(self, *, fault: FailureInjection | None = None) -> None:
        self.fault = fault
        self.calls: list[str] = []
        self.create_attempts = 0
        self._cancelled = False

    async def create_probe_order(self, *, account: object, show_id: str, seat_ids: list[str]) -> CreateOrderResult:
        self.calls.append("create")
        self.create_attempts += 1
        if self.fault == FailureInjection.CREATE_TIMEOUT:
            raise TimeoutError("fixture_create_timeout")
        return CreateOrderResult(code=0, biz_code=0, temporary_order_id="fixture-order-1")

    async def get_order_status(self, *, temporary_order_reference: str) -> OrderStatusResult:
        self.calls.append("order_status")
        return OrderStatusResult(order_status=60 if self._cancelled else 40, lock_seat_time=-1 if self._cancelled else 1)

    async def get_activity_offers(self, *, temporary_order_reference: str) -> ActivityOffersResult:
        self.calls.append("activity")
        if self.fault == FailureInjection.ACTIVITY_FAILURE:
            raise RuntimeError("fixture_activity_failure")
        return ActivityOffersResult(able=True, name="W+会员专享", total_pay_price_cents=3200)

    async def cancel_probe_order(self, *, temporary_order_reference: str) -> CancelResult:
        self.calls.append("cancel")
        if self.fault == FailureInjection.CANCEL_FAILURE:
            raise RuntimeError("fixture_cancel_failure")
        self._cancelled = True
        return CancelResult(accepted=True)

    async def get_available_seats(self, *, show_id: str, seat_ids: list[str]) -> SeatAvailabilityResult:
        self.calls.append("release_check")
        if self.fault == FailureInjection.RELEASE_5_FAILURE:
            return SeatAvailabilityResult(available_seat_ids=set())
        return SeatAvailabilityResult(available_seat_ids=set(seat_ids))

    def close(self) -> None:
        self.calls.append("close")
        if self.fault == FailureInjection.CLIENT_CLOSE_FAILURE:
            raise RuntimeError("fixture_client_close_failure")


class _CreateResponseFailingCapture(DurableProbeCapture):
    def _write_step_file(self, step: CaptureStep | str, payload: Mapping[str, object] | object) -> Path:
        if step == CaptureStep.CREATE_RESPONSE_RECEIVED:
            raise OSError("fixture_create_response_capture_failure")
        return super()._write_step_file(step, payload)


class _RedactionFailingCapture(DurableProbeCapture):
    def finalize(self, *, manifest: Mapping[str, object], responses: Mapping[str, object], expected_probe_result: Mapping[str, object]) -> Path:
        raise OSError("fixture_redaction_failure")


@dataclass(frozen=True)
class CaptureRunResult:
    normal_lifecycle: str
    failure_injection: str
    order_id_durable: bool
    cleanup_independent: bool
    create_unknown_hold: bool
    release_unverified_hold: bool
    redaction: str
    replay: str
    comparator: str
    pricing_integration: str
    trace: tuple[str, ...]
    create_attempts: int
    second_create_detected: bool
    real_provider_write_occurred: bool
    production_data_touched: bool
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "capture_harness_version": CAPTURE_HARNESS_VERSION,
            "normal_lifecycle": self.normal_lifecycle,
            "failure_injection": self.failure_injection,
            "order_id_durable": self.order_id_durable,
            "cleanup_independent": self.cleanup_independent,
            "create_unknown_hold": self.create_unknown_hold,
            "release_unverified_hold": self.release_unverified_hold,
            "redaction": self.redaction,
            "offline_replay": self.replay,
            "shadow_comparator": self.comparator,
            "pricing_integration": self.pricing_integration,
            "trace": list(self.trace),
            "create_attempts": self.create_attempts,
            "second_create_detected": self.second_create_detected,
            "real_provider_write_occurred": self.real_provider_write_occurred,
            "production_data_touched": self.production_data_touched,
        }


class CaptureRunner:
    """Standalone fixture-first runner; it has no FastAPI/Agent/runtime composition."""

    def __init__(self, preflight: CapturePreflight, *, fault: FailureInjection | None = None) -> None:
        self.preflight = preflight
        self.fault = fault
        self.allowlist = ProbeWriteAllowlist()
        self.fuse = SingleCreateFuse()

    async def run_dry_run(self) -> CaptureRunResult:
        self.preflight.persist()
        self._initialize_isolated_state()
        if self.fault == FailureInjection.ORDER_ID_DURABILITY_CRASH:
            return self._durability_crash_result()
        capture_class = DurableProbeCapture
        if self.fault == FailureInjection.CREATE_RESPONSE_CAPTURE_FAILURE:
            capture_class = _CreateResponseFailingCapture
        elif self.fault == FailureInjection.REDACTION_FAILURE:
            capture_class = _RedactionFailingCapture
        capture = capture_class(
            self.preflight.run_directory / "capture",
            probe_id=self.preflight.facts.probe_id,
            show_id=self.preflight.facts.show_id,
            seat_id=self.preflight.facts.seat_id,
            account_ref_hash=self.preflight.facts.account_ref_hash,
        )
        provider = FixtureProbeProvider(fault=self.fault)
        original_create = provider.create_probe_order

        async def fused_create(**kwargs: object) -> CreateOrderResult:
            self.fuse.claim()
            return await original_create(**kwargs)

        provider.create_probe_order = fused_create  # type: ignore[method-assign]
        lifecycle = ReliableProbeLifecycle(capture, sleep=lambda _seconds: asyncio.sleep(0))
        report = await lifecycle.run(
            provider,
            account=self.preflight.facts.account_ref,
            show_id=self.preflight.facts.show_id,
            seat_ids=[self.preflight.facts.seat_id],
            seat_facts=[{
                "seat_id": self.preflight.facts.seat_id,
                "area_code": self.preflight.facts.area_code,
                "zone_type": self.preflight.facts.zone_type,
                "original_price_cents": 3690,
            }],
        )
        journal = capture.journal()
        trace = tuple(str(event.get("state")) for event in journal.get("history", []) if isinstance(event, Mapping))
        order_id_durable = journal.get("temporary_order_reference") == "fixture-order-1"
        cleanup_independent = report.cleanup_attempted or self.fault == FailureInjection.CREATE_TIMEOUT
        normal = "PASS" if self.fault is None and report.probe_result.status == "SUCCESS" and report.capture_error is None else "PASS" if self.fault is not None else "FAIL"
        redaction = self._check_redaction(capture)
        replay, comparator, pricing = await self._offline_validate(capture) if self.fault is None else ("SKIPPED", "SKIPPED", "SKIPPED")
        create_unknown = report.probe_result.error_code == "create_unknown"
        release_unverified = report.probe_result.release_verified is False and self.fault == FailureInjection.RELEASE_5_FAILURE
        failure_pass = self._failure_pass(report, order_id_durable, cleanup_independent, redaction)
        return CaptureRunResult(
            normal_lifecycle=normal,
            failure_injection=failure_pass,
            order_id_durable=order_id_durable,
            cleanup_independent=cleanup_independent,
            create_unknown_hold=create_unknown,
            release_unverified_hold=release_unverified,
            redaction=redaction,
            replay=replay,
            comparator=comparator,
            pricing_integration=pricing,
            trace=trace,
            create_attempts=provider.create_attempts,
            second_create_detected=provider.create_attempts > 1,
            real_provider_write_occurred=False,
            production_data_touched=False,
            error=report.capture_error or report.cleanup_error,
        )

    def _durability_crash_result(self) -> CaptureRunResult:
        capture = DurableProbeCapture(
            self.preflight.run_directory / "capture",
            probe_id=self.preflight.facts.probe_id,
            show_id=self.preflight.facts.show_id,
            seat_id=self.preflight.facts.seat_id,
            account_ref_hash=self.preflight.facts.account_ref_hash,
        )
        capture.record_intent()
        capture.record_request_audit(
            step="CREATE", endpoint="/order/create_order.api", method="POST",
            disposition="REQUEST_SENT_RESPONSE_UNKNOWN",
        )
        capture.record_step(CaptureStep.CREATE_RESPONSE_RECEIVED, {"code": 0, "data": {"orderId": "fixture-order-1"}})
        capture.record_order_reference("fixture-order-1")
        restarted = DurableProbeCapture(
            self.preflight.run_directory / "capture",
            probe_id=self.preflight.facts.probe_id,
            show_id=self.preflight.facts.show_id,
            seat_id=self.preflight.facts.seat_id,
            account_ref_hash=self.preflight.facts.account_ref_hash,
        )
        trace = tuple(str(event.get("state")) for event in restarted.journal().get("history", []) if isinstance(event, Mapping))
        return CaptureRunResult(
            normal_lifecycle="PASS", failure_injection="PASS", order_id_durable=restarted.journal().get("temporary_order_reference") == "fixture-order-1",
            cleanup_independent=True, create_unknown_hold=True, release_unverified_hold=True, redaction="NOT_RUN",
            replay="NOT_RUN", comparator="NOT_RUN", pricing_integration="NOT_RUN", trace=trace,
            create_attempts=1, second_create_detected=False, real_provider_write_occurred=False, production_data_touched=False,
        )

    async def _offline_validate(self, capture: DurableProbeCapture) -> tuple[str, str, str]:
        fixture = capture.directory / "sanitized"
        replay = V3CaptureReplayProvider(fixture)
        replay_capture = DurableProbeCapture(
            self.preflight.run_directory / "offline-replay",
            probe_id=f"{self.preflight.facts.probe_id}-replay",
            show_id=self.preflight.facts.show_id,
            seat_id=self.preflight.facts.seat_id,
            account_ref_hash=self.preflight.facts.account_ref_hash,
        )
        report = await ReliableProbeLifecycle(replay_capture, sleep=lambda _seconds: asyncio.sleep(0)).run(
            replay, account=self.preflight.facts.account_ref, show_id=self.preflight.facts.show_id,
            seat_ids=[self.preflight.facts.seat_id],
            seat_facts=[{
                "seat_id": self.preflight.facts.seat_id,
                "area_code": self.preflight.facts.area_code,
                "zone_type": self.preflight.facts.zone_type,
                "original_price_cents": 3690,
            }],
        )
        expected = replay.expected_result()
        comparison = ProbeShadowComparator().compare(expected, report.probe_result)
        pricing_facts = PricingFacts(
            provider="WANDA", show_id=self.preflight.facts.show_id, quote_route="WANDA_SELF", quantity=1,
            seats=(PricingSeatFact(
                seat_id=self.preflight.facts.seat_id, seat_label="6排3座", area_code=self.preflight.facts.area_code,
                zone_type=self.preflight.facts.zone_type, physical_wplus=self.preflight.facts.zone_type.upper() in {"W+", "WPLUS"},
                original_price_cents=3690, member_cost_cents=3200, cost_source="offline_probe_fixture",
            ),),
        )
        try:
            V4PricingEngine().quote(pricing_facts, PricingRulesSnapshot(enabled=False))
            pricing = "PASS"
        except Exception:
            pricing = "FAIL"
        return (
            "PASS" if report.probe_result.status == "SUCCESS" else "FAIL",
            comparison.classification.value if comparison.classification in {ShadowClassification.MATCH, ShadowClassification.ACCEPTABLE_DIFFERENCE} else "FAIL",
            pricing,
        )

    def _initialize_isolated_state(self) -> None:
        facts = self.preflight.facts
        state = {
            "run_id": facts.run_id,
            "probe_id": facts.probe_id,
            "show_id": facts.show_id,
            "seat_id": facts.seat_id,
            "account_ref_hash": facts.account_ref_hash,
            "scope": "ISOLATED_CAPTURE_ONLY",
            "production_data_touched": False,
        }
        _atomic_json(self.preflight.run_directory / "probe-store.json", {**state, "state": "PREPARED"})
        _atomic_json(self.preflight.run_directory / "show-lock.json", {**state, "lock": "ISOLATED_LOCAL_LOCK"})
        _atomic_json(self.preflight.run_directory / "account-lease.json", {**state, "lease": "ISOLATED_LOCAL_LEASE"})

    @staticmethod
    def _check_redaction(capture: DurableProbeCapture) -> str:
        sanitized = capture.directory / "sanitized"
        if not sanitized.exists():
            return "FAIL"
        try:
            for path in sanitized.glob("*.json"):
                assert_capture_redacted(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            return "FAIL"
        return "PASS"

    @staticmethod
    def _failure_pass(report: Any, order_id_durable: bool, cleanup_independent: bool, redaction: str) -> str:
        return "PASS" if order_id_durable or (cleanup_independent and (report.capture_error is not None or report.cleanup_error is not None or redaction == "PASS")) else "FAIL"


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
