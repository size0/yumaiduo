from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from .errors import ProbeDisabledError


# Legacy quote calls retain their historical default. Agent quote.preview enters
# this context explicitly so it can never fall through to the old active path.
_ACTIVE_PROBE_CONTEXT: ContextVar[bool] = ContextVar("active_probe_context", default=True)


class ProbePolicy:
    @classmethod
    def from_settings(cls, settings: object) -> "ProbePolicy":
        return cls(
            active_probe_enabled=bool(getattr(settings, "wanda_active_probe_enabled", False)),
            agent_harness_read_only=bool(getattr(settings, "agent_harness_read_only", True)),
        )

    def __init__(
        self,
        *,
        active_probe_enabled: bool = False,
        agent_harness_read_only: bool = True,
        release_recheck_delays_seconds: tuple[float, ...] = (0.0, 2.0, 5.0),
        reconciliation_delays_seconds: tuple[float, ...] = (15.0, 15.0),
        account_lease_ttl_seconds: float = 180.0,
        unknown_create_hold_seconds: float = 900.0,
    ) -> None:
        self.active_probe_enabled = bool(active_probe_enabled)
        self.agent_harness_read_only = bool(agent_harness_read_only)
        self.release_recheck_delays_seconds = tuple(float(item) for item in release_recheck_delays_seconds)
        self.reconciliation_delays_seconds = tuple(float(item) for item in reconciliation_delays_seconds)
        self.account_lease_ttl_seconds = float(account_lease_ttl_seconds)
        self.unknown_create_hold_seconds = float(unknown_create_hold_seconds)
        if self.account_lease_ttl_seconds <= 0 or self.unknown_create_hold_seconds <= self.account_lease_ttl_seconds:
            raise ValueError("probe_lease_ttl_invalid")
        if self.release_recheck_delays_seconds != (0.0, 2.0, 5.0):
            raise ValueError("probe_release_delays_must_be_0_2_5")
        if any(item < 0 for item in self.reconciliation_delays_seconds):
            raise ValueError("probe_reconciliation_delay_invalid")

    def ensure_allowed(self) -> None:
        if self.agent_harness_read_only:
            raise ProbeDisabledError("agent_harness_read_only")
        if not self.active_probe_enabled:
            raise ProbeDisabledError()

    @staticmethod
    def context_allows_active_probe() -> bool:
        return _ACTIVE_PROBE_CONTEXT.get()


@contextmanager
def disable_active_probe() -> Iterator[None]:
    token = _ACTIVE_PROBE_CONTEXT.set(False)
    try:
        yield
    finally:
        _ACTIVE_PROBE_CONTEXT.reset(token)
