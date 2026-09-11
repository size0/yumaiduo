from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class IncidentObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1, max_length=160)
    show_id: str = Field(min_length=1, max_length=240)
    seat_id: str = Field(min_length=1, max_length=240)
    account_ref_hash: str = Field(min_length=1, max_length=160)
    target_seat_available: bool
    show_available: int = Field(ge=0)
    show_total: int = Field(gt=0)
    expected_probe_seat_ids: list[str] = Field(min_length=1, max_length=100)
    probe_seat_availability_samples: list[dict[str, bool]] = Field(min_length=1, max_length=100)
    target_show_readable: bool
    target_provider_anomaly: bool
    target_seat_abnormal_occupancy: bool
    observation_window_elapsed: bool
    observation_sample_count: int = Field(default=0, ge=0, le=100)
    observation_window_seconds: int = Field(default=0, ge=0, le=86_400)
    no_matching_probe_order: bool
    # Deprecated global flag retained as environment evidence only. It is not a Gate blocker.
    no_provider_anomalies: bool = True


class ManualCloseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eligible: bool
    reasons: list[str] = Field(default_factory=list)
    result_state: str


class ManualCloseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    closed: bool
    state: str
    show_hold: str
    account_hold: str
    create_commit: str
    cleanup: str
    final_external_state: str
    closed_at: str | None = None


class ManualIncidentCloseStore:
    """Local durable record for an explicitly approved incident close."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, value: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent)
        temporary = Path(name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:  # pragma: no cover - Windows CI has no fchmod
                os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            if os.name != "nt":
                directory_fd = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))


class ManualIncidentCloseGate:
    """Evaluate and explicitly record a safe, non-cancel incident close."""

    def __init__(self, store: ManualIncidentCloseStore) -> None:
        self._store = store

    def evaluate(self, observation: IncidentObservation) -> ManualCloseDecision:
        reasons: list[str] = []
        expected = set(observation.expected_probe_seat_ids)
        samples_stable = bool(expected) and all(
            expected.issubset({seat_id for seat_id, available in sample.items() if available})
            for sample in observation.probe_seat_availability_samples
        )
        if not observation.target_seat_available or not samples_stable:
            reasons.append("expected_probe_seat_not_stable")
        if not observation.target_show_readable:
            reasons.append("target_show_unreadable")
        if not observation.observation_window_elapsed or observation.observation_sample_count < 3 or observation.observation_window_seconds < 900:
            reasons.append("observation_window_incomplete")
        if not observation.no_matching_probe_order:
            reasons.append("matching_probe_order_present")
        if observation.target_provider_anomaly:
            reasons.append("target_provider_anomaly_present")
        if observation.target_seat_abnormal_occupancy:
            reasons.append("target_seat_abnormal_occupancy")
        return ManualCloseDecision(
            eligible=not reasons,
            reasons=reasons,
            result_state="CLOSED_UNVERIFIED_SAFE" if not reasons else "CREATE_UNKNOWN_HOLD",
        )

    def close(self, observation: IncidentObservation, *, approved: bool) -> ManualCloseResult:
        decision = self.evaluate(observation)
        if not approved or not decision.eligible:
            return ManualCloseResult(
                closed=False, state=decision.result_state, show_hold="retained", account_hold="retained",
                create_commit="UNKNOWN", cleanup="UNVERIFIED", final_external_state="AVAILABLE" if observation.target_seat_available else "UNKNOWN",
            )
        closed_at = datetime.now(timezone.utc).isoformat()
        result = ManualCloseResult(
            closed=True, state="CLOSED_UNVERIFIED_SAFE", show_hold="released", account_hold="released",
            create_commit="UNKNOWN", cleanup="UNVERIFIED", final_external_state="AVAILABLE", closed_at=closed_at,
        )
        self._store.write({
            "incident_id": observation.incident_id, "show_id": observation.show_id, "seat_id": observation.seat_id,
            "account_ref_hash": observation.account_ref_hash, **result.model_dump(mode="json"),
        })
        return result
