from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .canonical import ActivityOffersResult, CancelResult, CreateOrderResult, OrderStatusResult, SeatAvailabilityResult
from .capture import write_v3_capture
from .models import ProbeResult, ProbeSeatTypePrice


class CaptureStep(StrEnum):
    PRE_CREATE = "PRE_CREATE"
    CREATE_REQUEST_STARTED = "CREATE_REQUEST_STARTED"
    CREATE_RESPONSE_RECEIVED = "CREATE_RESPONSE_RECEIVED"
    ORDER_ID_EXTRACTED = "ORDER_ID_EXTRACTED"
    LOCK_STATUS = "LOCK_STATUS"
    ACTIVITY_RESPONSE = "ACTIVITY_RESPONSE"
    MEMBER_PRICE_EXTRACTED = "MEMBER_PRICE_EXTRACTED"
    CANCEL_REQUEST_STARTED = "CANCEL_REQUEST_STARTED"
    CANCEL_RESPONSE = "CANCEL_RESPONSE"
    CANCEL_STATUS = "CANCEL_STATUS"
    RELEASE_0 = "RELEASE_0"
    RELEASE_2 = "RELEASE_2"
    RELEASE_5 = "RELEASE_5"
    FINAL = "FINAL"


class RequestDisposition(StrEnum):
    REQUEST_NOT_SENT = "REQUEST_NOT_SENT"
    REQUEST_SENT_RESPONSE_UNKNOWN = "REQUEST_SENT_RESPONSE_UNKNOWN"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"


class ProbeRequestAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probe_id: str = Field(min_length=1, max_length=160)
    step: str = Field(min_length=1, max_length=80)
    endpoint: str = Field(min_length=1, max_length=240)
    method: str = Field(min_length=1, max_length=16)
    started_at: str = Field(min_length=1, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)
    http_status: int | None = None
    provider_request_id: str | None = Field(default=None, max_length=240)
    disposition: RequestDisposition
    result_classification: str | None = Field(default=None, max_length=120)


@dataclass(frozen=True)
class CloseProviderResult:
    method: str | None
    failed: bool
    error_code: str
    error_type: str | None = None


async def close_provider_client(
    client: object,
    *,
    on_error: Callable[[str], None] | None = None,
) -> CloseProviderResult:
    """Close a provider using its actual interface without masking probe state.

    The V11 Wanda client creates and closes an HTTP client per request and has no
    ``aclose`` method. Other clients may expose either ``aclose`` or ``close``.
    Missing close support is therefore a successful no-op, while an actual close
    exception is returned as an isolated ``CLIENT_CLOSE_FAILED`` result.
    """
    method_name: str | None = None
    method: Callable[[], object] | None = None
    for candidate in ("aclose", "close"):
        value = getattr(client, candidate, None)
        if callable(value):
            method_name = candidate
            method = value
            break
    if method is None:
        return CloseProviderResult(None, False, "CLIENT_CLOSE_NOT_REQUIRED")
    try:
        result = method()
        if inspect.isawaitable(result):
            await result
    except Exception as error:  # pragma: no cover - exact provider exception varies
        if on_error is not None:
            on_error("CLIENT_CLOSE_FAILED")
        return CloseProviderResult(method_name, True, "CLIENT_CLOSE_FAILED", type(error).__name__)
    return CloseProviderResult(method_name, False, "CLIENT_CLOSE_OK")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:  # pragma: no cover - Windows CI has no fchmod
            os.chmod(temporary, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        os.chmod(path, mode)
    finally:
        temporary.unlink(missing_ok=True)


class DurableProbeCapture:
    """Restricted raw capture with a write-ahead journal and atomic step files."""

    def __init__(self, directory: Path, *, probe_id: str, show_id: str, seat_id: str, account_ref_hash: str) -> None:
        self.directory = Path(directory)
        self.raw_directory = self.directory / "raw"
        self.steps_directory = self.raw_directory / "steps"
        self.journal_path = self.directory / "probe_journal.json"
        self.audit_path = self.raw_directory / "provider_audit.json"
        self.probe_id = probe_id
        self.show_id = show_id
        self.seat_id = seat_id
        self.account_ref_hash = account_ref_hash
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.raw_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.steps_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.directory, self.raw_directory, self.steps_directory):
            os.chmod(path, 0o700)

    def record_intent(self, *, started_at: str | None = None) -> None:
        self._record_state("CREATE_INTENT", started_at=started_at or _now())
        self.record_step(CaptureStep.PRE_CREATE, {"state": "CREATE_INTENT"})

    def record_step(self, step: CaptureStep | str, payload: Mapping[str, object] | object) -> Path:
        target = self._write_step_file(step, payload)
        self._record_state(str(step), step_file=str(target.relative_to(self.directory)))
        return target

    def record_create_response(self, payload: Mapping[str, object], temporary_order_reference: str | None) -> Path:
        """Atomically journal response receipt and orderId before raw payload I/O."""
        now = _now()
        current = self.journal() or {
            "probe_id": self.probe_id,
            "show_id": self.show_id,
            "seat_id": self.seat_id,
            "account_ref_hash": self.account_ref_hash,
            "history": [],
        }
        history = current.setdefault("history", [])
        if not isinstance(history, list):
            raise ValueError("probe_journal_history_invalid")
        history.append({"state": CaptureStep.CREATE_RESPONSE_RECEIVED, "at": now})
        state: str = CaptureStep.CREATE_RESPONSE_RECEIVED
        if temporary_order_reference:
            current["temporary_order_reference"] = str(temporary_order_reference)
            history.append({"state": CaptureStep.ORDER_ID_EXTRACTED, "at": now})
            state = CaptureStep.ORDER_ID_EXTRACTED
        current["state"] = state
        current["updated_at"] = now
        _atomic_write_json(self.journal_path, current)
        return self._write_step_file(CaptureStep.CREATE_RESPONSE_RECEIVED, payload)

    def _write_step_file(self, step: CaptureStep | str, payload: Mapping[str, object] | object) -> Path:
        step_name = str(step)
        if not step_name or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in step_name):
            raise ValueError("capture_step_invalid")
        existing = list(self.steps_directory.glob("*-*.json"))
        target = self.steps_directory / f"{len(existing) + 1:04d}-{step_name}.json"
        _atomic_write_json(target, payload)
        return target

    def record_order_reference(self, temporary_order_reference: str) -> None:
        if not str(temporary_order_reference).strip():
            raise ValueError("temporary_order_reference_required")
        self._record_state(CaptureStep.ORDER_ID_EXTRACTED, temporary_order_reference=str(temporary_order_reference))

    def record_provider_audit(self, audit: ProbeRequestAudit) -> None:
        clean = audit.model_dump(mode="json")
        existing: list[object] = []
        if self.audit_path.exists():
            loaded = json.loads(self.audit_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                raise ValueError("provider_audit_invalid")
            existing = loaded
        existing.append(clean)
        _atomic_write_json(self.audit_path, existing)

    def record_request_audit(
        self,
        *,
        step: str,
        endpoint: str,
        method: str,
        disposition: RequestDisposition,
        started_at: str | None = None,
        finished_at: str | None = None,
        http_status: int | None = None,
        provider_request_id: str | None = None,
        result_classification: str | None = None,
    ) -> None:
        self.record_provider_audit(ProbeRequestAudit(
            probe_id=self.probe_id, step=step, endpoint=endpoint, method=method,
            started_at=started_at or _now(), finished_at=finished_at,
            http_status=http_status, provider_request_id=provider_request_id,
            disposition=disposition, result_classification=result_classification,
        ))

    def finalize(
        self,
        *,
        manifest: Mapping[str, object],
        responses: Mapping[str, object],
        expected_probe_result: Mapping[str, object],
    ) -> Path:
        target = write_v3_capture(self.directory / "sanitized", manifest=manifest, responses=responses, expected_probe_result=expected_probe_result)
        self.record_step(CaptureStep.FINAL, {"sanitized_fixture": str(target), "sensitive_data_removed": True})
        return target

    def journal(self) -> dict[str, object]:
        if not self.journal_path.exists():
            return {}
        return json.loads(self.journal_path.read_text(encoding="utf-8"))

    def _record_state(self, state: str, **metadata: object) -> None:
        current = self.journal() or {
            "probe_id": self.probe_id,
            "show_id": self.show_id,
            "seat_id": self.seat_id,
            "account_ref_hash": self.account_ref_hash,
            "history": [],
        }
        if metadata.get("temporary_order_reference") is not None:
            current["temporary_order_reference"] = metadata["temporary_order_reference"]
        event = {"state": state, "at": _now()}
        event.update({key: value for key, value in metadata.items() if key != "temporary_order_reference"})
        history = current.setdefault("history", [])
        if not isinstance(history, list):
            raise ValueError("probe_journal_history_invalid")
        history.append(event)
        current["state"] = state
        current["updated_at"] = event["at"]
        _atomic_write_json(self.journal_path, current)


class CaptureProvider(Protocol):
    async def create_probe_order(self, *, account: object, show_id: str, seat_ids: list[str]) -> CreateOrderResult: ...
    async def get_order_status(self, *, temporary_order_reference: str) -> OrderStatusResult: ...
    async def get_activity_offers(self, *, temporary_order_reference: str) -> ActivityOffersResult: ...
    async def cancel_probe_order(self, *, temporary_order_reference: str) -> CancelResult: ...
    async def get_available_seats(self, *, show_id: str, seat_ids: list[str]) -> SeatAvailabilityResult: ...


@dataclass
class CaptureRunReport:
    probe_result: ProbeResult
    capture_error: str | None
    cleanup_error: str | None
    cleanup_attempted: bool
    client_close_failed: bool
    client_close_error: str | None


class ReliableProbeLifecycle:
    """Run the frozen lifecycle with independent durable capture and cleanup."""

    def __init__(self, capture: DurableProbeCapture, *, sleep: Callable[[float], Awaitable[object]]) -> None:
        self.capture = capture
        self._sleep = sleep

    async def run(
        self,
        provider: CaptureProvider,
        *,
        account: object,
        show_id: str,
        seat_ids: list[str],
        seat_facts: Sequence[Mapping[str, object]] | None = None,
        client: object | None = None,
    ) -> CaptureRunReport:
        capture_error: str | None = None
        cleanup_error: str | None = None
        primary_error: str | None = None
        observed_order_reference: str | None = None
        cancel_confirmed = False
        release_verified = False
        member_price: int | None = None
        release_timing = "UNVERIFIED"
        cleanup_attempted = False
        releases: list[dict[str, object]] = []
        responses: dict[str, object] = {}
        try:
            self.capture.record_intent()
            self._record_step(CaptureStep.CREATE_REQUEST_STARTED, {}, "CREATE", "/order/create_order.api", disposition=RequestDisposition.REQUEST_NOT_SENT)
            self.capture.record_request_audit(step="CREATE", endpoint="/order/create_order.api", method="POST", disposition=RequestDisposition.REQUEST_SENT_RESPONSE_UNKNOWN)
            try:
                created = await provider.create_probe_order(account=account, show_id=show_id, seat_ids=list(seat_ids))
            except Exception:
                # A create transport exception is always ambiguous. Never retry it.
                primary_error = "create_unknown"
                raise
            # Atomically journal response receipt and the independently extracted
            # order reference before raw response-body I/O. A writer failure must
            # not lose the reference needed by cleanup.
            observed_order_reference = created.temporary_order_id
            responses["create_order.response.json"] = created.model_dump(mode="json")
            try:
                self.capture.record_create_response(responses["create_order.response.json"], observed_order_reference)
                self.capture.record_request_audit(
                    step="CREATE", endpoint="/order/create_order.api", method="POST",
                    disposition=RequestDisposition.RESPONSE_RECEIVED, finished_at=_now(),
                )
            except Exception as error:
                capture_error = capture_error or (str(error)[:160] or type(error).__name__)
                if observed_order_reference and self.capture.journal().get("temporary_order_reference") != observed_order_reference:
                    try:
                        self.capture.record_order_reference(observed_order_reference)
                    except Exception as journal_error:
                        capture_error = capture_error or (str(journal_error)[:160] or type(journal_error).__name__)
            if observed_order_reference:
                if created.outcome == "UNKNOWN":
                    primary_error = "create_unknown"
                elif created.outcome != "CONFIRMED" or created.code not in (0, "0") or created.biz_code not in (0, "0"):
                    primary_error = "temporary_lock_state_unknown"
            else:
                primary_error = "create_unknown" if created.outcome == "UNKNOWN" else "temporary_lock_state_unknown"
            if primary_error is None and observed_order_reference:
                status = await provider.get_order_status(temporary_order_reference=observed_order_reference)
                responses["lock_status.response.json"] = status.model_dump(mode="json")
                self._record_step(CaptureStep.LOCK_STATUS, responses["lock_status.response.json"], "LOCK_STATUS", "/order/order_status.api", disposition=RequestDisposition.RESPONSE_RECEIVED)
                if status.order_status not in (40, "40") or status.lock_seat_time is None or status.lock_seat_time < 0:
                    primary_error = "temporary_lock_state_unknown"
            if primary_error is None and observed_order_reference:
                activity = await provider.get_activity_offers(temporary_order_reference=observed_order_reference)
                activity_payload: dict[str, object] = {"able": activity.able, "name": activity.name}
                activity_price = activity.member_price_cents or activity.total_pay_price_cents
                if activity_price is not None:
                    activity_payload["allotSeat"] = {"totalPayPrice": activity_price}
                if activity.seat_type_prices:
                    activity_payload["seat_type_prices"] = [item.model_dump(mode="json") for item in activity.seat_type_prices]
                responses["activity_offers.response.json"] = activity_payload
                self._record_step(CaptureStep.ACTIVITY_RESPONSE, responses["activity_offers.response.json"], "ACTIVITY", "/mkt/activity/secret/list.api", disposition=RequestDisposition.RESPONSE_RECEIVED)
                if not activity.able or "W+会员专享" not in activity.name:
                    primary_error = "activity_offers_failed"
                member_price = activity.member_price_cents or activity.total_pay_price_cents
                if member_price is None:
                    primary_error = primary_error or "wplus_price_unavailable"
                else:
                    self._record_step(CaptureStep.MEMBER_PRICE_EXTRACTED, {"member_price_cents": member_price}, "MEMBER_PRICE", "/mkt/activity/secret/list.api")
        except Exception as error:
            primary_error = primary_error or (str(error)[:160] or type(error).__name__)
            if isinstance(error, OSError) or "capture" in str(error).lower():
                capture_error = capture_error or str(error)
        finally:
            if observed_order_reference:
                cleanup_attempted = True
                cancel_result: CancelResult | None = None
                try:
                    self._record_step(CaptureStep.CANCEL_REQUEST_STARTED, {}, "CANCEL", "/order/cancel.api", disposition=RequestDisposition.REQUEST_NOT_SENT)
                    self.capture.record_request_audit(step="CANCEL", endpoint="/order/cancel.api", method="POST", disposition=RequestDisposition.REQUEST_SENT_RESPONSE_UNKNOWN)
                except Exception as error:
                    capture_error = capture_error or str(error)
                try:
                    cancel_result = await provider.cancel_probe_order(temporary_order_reference=observed_order_reference)
                    responses["cancel.response.json"] = {"code": 0 if cancel_result.accepted else 1}
                    try:
                        self._record_step(CaptureStep.CANCEL_RESPONSE, responses["cancel.response.json"], "CANCEL", "/order/cancel.api", disposition=RequestDisposition.RESPONSE_RECEIVED)
                    except Exception as error:
                        capture_error = capture_error or str(error)
                except Exception as error:
                    cleanup_error = cleanup_error or (str(error)[:160] or "cancel_failed")
                try:
                    status = await provider.get_order_status(temporary_order_reference=observed_order_reference)
                    responses["cancel_status.response.json"] = status.model_dump(mode="json")
                    try:
                        self._record_step(CaptureStep.CANCEL_STATUS, responses["cancel_status.response.json"], "CANCEL_STATUS", "/order/order_status.api", disposition=RequestDisposition.RESPONSE_RECEIVED)
                    except Exception as error:
                        capture_error = capture_error or str(error)
                    cancel_confirmed = bool(cancel_result and cancel_result.accepted and status.order_status in (60, "60") and status.lock_seat_time == -1)
                except Exception as error:
                    cleanup_error = cleanup_error or (str(error)[:160] or "cancel_status_unknown")
                previous = 0.0
                for delay, step in ((0.0, CaptureStep.RELEASE_0), (2.0, CaptureStep.RELEASE_2), (5.0, CaptureStep.RELEASE_5)):
                    if delay > previous:
                        try:
                            await self._sleep(delay - previous)
                        except asyncio.CancelledError:
                            cleanup_error = cleanup_error or "release_wait_cancelled"
                        except Exception as error:
                            cleanup_error = cleanup_error or (str(error)[:160] or "release_wait_failed")
                    previous = delay
                    try:
                        available = await provider.get_available_seats(show_id=show_id, seat_ids=list(seat_ids))
                        value = {"available_seat_ids": sorted(available.available_seat_ids), "expected_seat_available": set(seat_ids).issubset(available.available_seat_ids)}
                    except Exception as error:
                        value = {"available_seat_ids": [], "expected_seat_available": False}
                        cleanup_error = cleanup_error or (str(error)[:160] or "release_check_failed")
                    releases.append(value)
                    try:
                        self._record_step(step, value, "RELEASE", "/order/real_time_seat.api", disposition=RequestDisposition.RESPONSE_RECEIVED)
                    except Exception as error:
                        capture_error = capture_error or str(error)
                    if value["expected_seat_available"]:
                        release_verified = True
                        release_timing = {CaptureStep.RELEASE_0: "IMMEDIATE", CaptureStep.RELEASE_2: "AFTER_2S", CaptureStep.RELEASE_5: "AFTER_5S"}[step]
            else:
                cleanup_error = cleanup_error or ("create_unknown" if primary_error == "create_unknown" else None)

            if observed_order_reference and not cancel_confirmed:
                cleanup_error = cleanup_error or "temporary_lock_release_unverified"
            if observed_order_reference and not release_verified:
                cleanup_error = cleanup_error or "temporary_lock_release_unverified"
            result_error = primary_error or cleanup_error
            if result_error:
                result_error = str(result_error)[:120]
            facts = self._facts(member_price, seat_facts)
            expected = {
                "probe_id": self.capture.probe_id,
                "provider": "WANDA",
                "show_id": show_id,
                "status": "SUCCESS" if result_error is None else "FAILED",
                "seat_type_prices": [item.model_dump(mode="json") for item in facts],
                "cancel_confirmed": cancel_confirmed,
                "release_verified": release_verified,
                "release_timing_class": release_timing if release_verified else "UNVERIFIED",
                "error_code": result_error,
            }
            try:
                result = ProbeResult.model_validate(expected)
            except Exception as error:
                result_error = "probe_result_build_failed"
                result = ProbeResult(
                    probe_id=self.capture.probe_id, show_id=show_id, status="FAILED",
                    cancel_confirmed=cancel_confirmed, release_verified=release_verified,
                    release_timing_class=release_timing if release_verified else "UNVERIFIED",
                    error_code=result_error,
                )
                capture_error = capture_error or (str(error)[:160] or result_error)
            responses.setdefault("lock_status.response.json", {})
            responses.setdefault("activity_offers.response.json", {})
            responses.setdefault("cancel.response.json", {})
            responses.setdefault("cancel_status.response.json", {})
            while len(releases) < 3:
                releases.append({"available_seat_ids": [], "expected_seat_available": False})
            responses.setdefault("seat_release_0s.response.json", releases[0])
            responses.setdefault("seat_release_2s.response.json", releases[1])
            responses.setdefault("seat_release_5s.response.json", releases[2])
            try:
                self.capture.finalize(
                    manifest={"fixture_version": "v3-reliable-001", "provider": "WANDA", "cinema_id": "capture-only", "show_id": show_id, "seat_type": "W+", "capture_schema_version": "1", "source": "V3"},
                    responses=responses,
                    expected_probe_result=expected,
                )
            except Exception as error:
                capture_error = capture_error or (str(error)[:160] or type(error).__name__)
        close_result = await close_provider_client(client if client is not None else provider, on_error=lambda code: self._best_effort_state(code))
        if close_result.failed:
            close_error = close_result.error_type or close_result.error_code
        else:
            close_error = None
        return CaptureRunReport(
            probe_result=result,
            capture_error=capture_error,
            cleanup_error=cleanup_error,
            cleanup_attempted=cleanup_attempted,
            client_close_failed=close_result.failed,
            client_close_error=close_error,
        )

    def _record_step(self, step: CaptureStep, payload: object, audit_step: str, endpoint: str, *, disposition: RequestDisposition = RequestDisposition.REQUEST_SENT_RESPONSE_UNKNOWN) -> None:
        self.capture.record_step(step, payload)
        self.capture.record_provider_audit(ProbeRequestAudit(
            probe_id=self.capture.probe_id, step=audit_step, endpoint=endpoint, method="POST" if step not in {CaptureStep.RELEASE_0, CaptureStep.RELEASE_2, CaptureStep.RELEASE_5} else "GET",
            started_at=_now(), finished_at=_now() if disposition == RequestDisposition.RESPONSE_RECEIVED else None,
            http_status=None, provider_request_id=None, disposition=disposition,
            result_classification=disposition.value,
        ))

    def _best_effort_state(self, state: str) -> None:
        try:
            self.capture._record_state(state)
        except Exception:
            pass

    @staticmethod
    def _facts(member_price: int | None, seat_facts: Sequence[Mapping[str, object]] | None) -> list[ProbeSeatTypePrice]:
        if member_price is None or not seat_facts:
            return []
        facts: list[ProbeSeatTypePrice] = []
        for seat in seat_facts:
            facts.append(ProbeSeatTypePrice(
                area_code=str(seat.get("area_code") or "UNKNOWN"),
                zone_type=str(seat.get("zone_type") or "UNKNOWN"),
                representative_seat_id=str(seat.get("seat_id") or seat.get("representative_seat_id") or "UNKNOWN"),
                original_price_cents=int(seat.get("original_price_cents") or 1),
                member_price_cents=member_price,
            ))
        return facts
